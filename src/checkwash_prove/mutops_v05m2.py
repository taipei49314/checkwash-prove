"""v0.5-m2: muJava's method-level mutation operators (Ma and Offutt, update July 2016) mapped to Python,
with the compound-assignment operator PY-ASRS (Nelson, 2026-11-08) in place of m1's ASRS.

Reference generator for the catalogue in CATALOGUE.md and catalogue.json (same folder). Standard library only.
Reference interpreter: CPython 3.11 (positions and grammar come from its ast module). Sources must be UTF-8.
The catalogue is frozen: a change to any operator, site rule or replacement set is a new version (v0.5-m3, ...),
never an edit of this one. v0.5-m1 stays frozen as the earlier version.

  python -I mutops.py --selftest
  python -I mutops.py FILE [--lines 10,12-30] [--out mutants.json]

Library use: generate(source_bytes, lines=None) -> (mutants, stats). Each mutant is a dict with id, op, line,
col (1-based line; 0-based UTF-8 byte column, as in ast), before, after, what and source (the mutated file).
"""
from __future__ import annotations

import argparse
import ast
import codecs
import hashlib
import io
import json
import re
import sys
import tokenize
import warnings

VERSION = "v0.5-m2"
# Catalogue order. When two operators produce the same mutated file, the first one in this order keeps it.
ORDER = ["AORB", "AOIU", "AODU", "ROR", "COR", "COI", "COD", "SOR", "LOR", "LOI", "LOD", "PY-ASRS",
         "SDL", "VDL", "CDL", "ODL"]
ARITH = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**"}
SHIFT = {ast.LShift: "<<", ast.RShift: ">>"}
BITWISE = {ast.BitAnd: "&", ast.BitOr: "|", ast.BitXor: "^"}
REL = {ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<=", ast.Eq: "==", ast.NotEq: "!="}
ORDERED = (ast.Gt, ast.GtE, ast.Lt, ast.LtE)
IDENTITY = (ast.Is, ast.IsNot)
MEMBERSHIP = (ast.In, ast.NotIn)
# PY-ASRS (Nelson, 2026-11-08): every existing augmented assignment gets the other twelve operators of set B.
# A replacement inside the same group is PY-ASRS-SAME, across groups PY-ASRS-X: 13 x 12 = 156 directed rules.
# Only the operator token of AugAssign changes; target and value stay; nothing is expanded to a plain assignment.
PY_ASRS_SET = ["+=", "-=", "*=", "@=", "/=", "//=", "%=", "**=", "<<=", ">>=", "&=", "^=", "|="]
PY_ASRS_GROUPS = {"arithmetic": ["+=", "-=", "*=", "@=", "/=", "//=", "%=", "**="], "shift": ["<<=", ">>="],
                  "bitwise": ["&=", "^=", "|="]}
PY_ASRS_GROUP_OF = {op: g for g, ops in PY_ASRS_GROUPS.items() for op in ops}
VARREF = (ast.Name, ast.Attribute, ast.Subscript)
SKIP_SUBTREES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.JoinedStr, ast.pattern)
KEPT_NODES = (ast.expr, ast.stmt, ast.comprehension, ast.match_case, ast.excepthandler, ast.keyword, ast.withitem)
BOM = b"\xef\xbb\xbf"

OP_TOKEN = re.compile(r"\bis\s+not\b|\bnot\s+in\b|\bis\b|\bin\b|\band\b|\bor\b"
                      r"|\*\*|//|<<|>>|<=|>=|==|!=|[-+*/%@&|^<>]")
AUG_TOKEN = re.compile(r"\*\*=|//=|<<=|>>=|[-+*/%@&|^]=")
COMMENT = re.compile(r"#[^\n]*")


def is_docstring(stmt):
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def is_load_ref(node):
    return isinstance(node, VARREF) and isinstance(node.ctx, ast.Load)


def position(node):
    return getattr(node, "lineno", 0), getattr(node, "col_offset", 0)


class Generator:
    def __init__(self, data: bytes, lines=None):
        encoding = tokenize.detect_encoding(io.BytesIO(data).readline)[0]
        try:
            canonical = codecs.lookup(encoding).name
        except LookupError:
            canonical = encoding
        if canonical not in ("utf-8", "utf-8-sig", "ascii"):
            raise ValueError(f"{VERSION} handles UTF-8 sources only (ASCII is accepted); this file declares {encoding}")
        self.data = data
        self.bom = len(BOM) if data.startswith(BOM) else 0
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.tree = ast.parse(data)
        self.lines = set(lines) if lines is not None else None
        self.starts = [0]
        for line in data.splitlines(keepends=True):
            self.starts.append(self.starts[-1] + len(line))
        self.mutants, self.seen, self.invalid, self.duplicates = [], {data}, 0, 0
        # Method level: the bodies of def and async def (methods and nested functions included). Lambdas are
        # mutated only where they occur inside such a body.
        self.functions = [n for n in ast.walk(self.tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.nodes = self._expression_nodes()
        self.not_operands = {id(n.operand) for n in self.nodes
                             if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not)}

    # ---- positions and text -------------------------------------------------------------
    def off(self, line, col):
        return self.starts[line - 1] + col + (self.bom if line == 1 else 0)

    def span(self, node):
        return self.off(node.lineno, node.col_offset), self.off(node.end_lineno, node.end_col_offset)

    def text(self, a, b):
        return self.data[a:b].decode("utf-8")

    def src(self, node):
        return self.text(*self.span(node))

    def paren(self, node):
        return f"({self.src(node)})"

    def token(self, a, b, regex):
        """The one operator token between two operands (bytes a..b), skipping parentheses and comments."""
        region = self.text(a, b)
        masked = COMMENT.sub(lambda m: " " * len(m.group()), region)
        found = list(regex.finditer(masked))
        if len(found) != 1:
            return None
        m = found[0]
        return (a + len(region[:m.start()].encode("utf-8")), a + len(region[:m.end()].encode("utf-8")),
                " ".join(m.group().split()))

    def site_ok(self, node):
        return self.lines is None or getattr(node, "lineno", None) in self.lines

    # ---- method-level scope -------------------------------------------------------------
    def _body(self, fn):
        body = list(fn.body)
        return body[1:] if body and is_docstring(body[0]) else body

    def _expression_nodes(self):
        """Every node inside method bodies. Skipped: docstrings; nested def and class statements (a nested
        function's body is visited as a function of its own; decorators, defaults and annotations are not);
        lambda parameters and defaults; annotations; f-strings; match patterns."""
        out, ids = [], set()
        for fn in self.functions:
            stack = list(self._body(fn))
            while stack:
                node = stack.pop()
                if isinstance(node, SKIP_SUBTREES) or id(node) in ids:
                    continue
                ids.add(id(node))
                if isinstance(node, KEPT_NODES):
                    out.append(node)
                for field, value in ast.iter_fields(node):
                    if field in ("annotation", "returns", "type_comment") or (
                            isinstance(node, ast.Lambda) and field == "args"):
                        continue
                    if isinstance(value, ast.AST):
                        stack.append(value)
                    elif isinstance(value, list):
                        stack.extend(v for v in value if isinstance(v, ast.AST))
        out.sort(key=position)
        return out

    def _statements(self):
        out = []

        def visit(stmts):
            for s in stmts:
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Pass, ast.Global,
                                  ast.Nonlocal)):
                    continue
                if isinstance(s, ast.AnnAssign) and s.value is None:
                    continue
                if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant):
                    continue
                out.append(s)
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(s, field, None) or [])
                for h in getattr(s, "handlers", None) or []:
                    visit(h.body)
                for c in getattr(s, "cases", None) or []:
                    visit(c.body)

        for fn in self.functions:
            visit(self._body(fn))
        return sorted(out, key=position)

    # ---- recording ----------------------------------------------------------------------
    def add(self, op, node, a, b, new, what):
        mutated = self.data[:a] + new.encode("utf-8") + self.data[b:]
        if mutated in self.seen:
            self.duplicates += mutated != self.data
            return
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                compile(mutated, "<mutant>", "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            self.invalid += 1
            return
        self.seen.add(mutated)
        self.mutants.append({"op": op, "line": node.lineno, "col": node.col_offset, "before": self.text(a, b),
                             "after": new, "what": what, "source": mutated})

    # ---- shared pieces ------------------------------------------------------------------
    def _boolop_text(self, values, ops):
        """and/or rebuilt left-associated from operands and operators, fully parenthesised."""
        text = self.paren(values[0])
        for op, value in zip(ops, values[1:]):
            text = f"({text} {op} {self.paren(value)})"
        return text

    @staticmethod
    def _membership_operand(n, j):
        """True when operand j of comparison n belongs to a membership link (in / not in)."""
        return (j > 0 and isinstance(n.ops[j - 1], MEMBERSHIP)) or (j < len(n.ops) and isinstance(n.ops[j], MEMBERSHIP))

    def _compare_without(self, n, drop_operand, drop_op):
        operands = [n.left, *n.comparators]
        tokens = []
        for i in range(len(n.ops)):
            t = self.token(self.span(operands[i])[1], self.span(operands[i + 1])[0], OP_TOKEN)
            if not t:
                return None
            tokens.append(t[2])
        keep_operands = [o for j, o in enumerate(operands) if j != drop_operand]
        keep_tokens = [t for i, t in enumerate(tokens) if i != drop_op]
        if len(keep_operands) == 1:
            return self.paren(keep_operands[0])
        text = self.paren(keep_operands[0])
        for tok, operand in zip(keep_tokens, keep_operands[1:]):
            text += f" {tok} {self.paren(operand)}"
        return f"({text})"

    def _rebuild_binop(self, op_name, table):
        for n in self.nodes:
            if isinstance(n, ast.BinOp) and type(n.op) in table and self.site_ok(n):
                cur = table[type(n.op)]
                a, b = self.span(n)
                for alt in table.values():
                    if alt != cur:
                        self.add(op_name, n, a, b, f"({self.paren(n.left)} {alt} {self.paren(n.right)})",
                                 f"{cur} -> {alt}")

    def _operands(self, ops):
        out = []
        for n in self.nodes:
            if isinstance(n, ast.BinOp) and type(n.op) in ops:
                out += [n.left, n.right]
            elif isinstance(n, ast.AugAssign) and type(n.op) in ops:
                out.append(n.value)
            elif isinstance(n, ast.Compare):
                operands = [n.left, *n.comparators]
                for i, op in enumerate(n.ops):
                    if isinstance(op, ORDERED):
                        out += [operands[i], operands[i + 1]]
        uniq = {id(o): o for o in out if is_load_ref(o) and self.site_ok(o)}
        return sorted(uniq.values(), key=position)

    def _insert_unary(self, op_name, symbol, ops):
        for o in self._operands(ops):
            a, b = self.span(o)
            self.add(op_name, o, a, b, f"({symbol}{self.text(a, b)})", f"insert {symbol}")

    def _delete_unary(self, op_name, kinds):
        for n in self.nodes:
            if isinstance(n, ast.UnaryOp) and isinstance(n.op, kinds) and self.site_ok(n):
                a, b = self.span(n)
                self.add(op_name, n, a, b, self.paren(n.operand), "delete unary operator")

    # ---- operators (catalogue order) ----------------------------------------------------
    def gen_AORB(self):
        self._rebuild_binop("AORB", ARITH)

    def gen_AOIU(self):
        self._insert_unary("AOIU", "-", ARITH)

    def gen_AODU(self):
        self._delete_unary("AODU", (ast.USub, ast.UAdd))

    def gen_ROR(self):
        for n in self.nodes:
            if not isinstance(n, ast.Compare) or not self.site_ok(n):
                continue
            operands = [n.left, *n.comparators]
            for i, op in enumerate(n.ops):
                if not isinstance(op, tuple(REL) + IDENTITY):
                    continue
                t = self.token(self.span(operands[i])[1], self.span(operands[i + 1])[0], OP_TOKEN)
                if not t:
                    continue
                alts = [v for v in REL.values() if v != t[2]] if type(op) in REL else [
                    "is not" if isinstance(op, ast.Is) else "is"]
                for alt in alts:
                    self.add("ROR", n, t[0], t[1], alt, f"{t[2]} -> {alt}")
            if any(type(op) in REL for op in n.ops):
                a, b = self.span(n)
                for value in ("True", "False"):
                    self.add("ROR", n, a, b, value, f"predicate -> {value}")

    def gen_COR(self):
        for n in self.nodes:
            if not isinstance(n, ast.BoolOp) or not self.site_ok(n):
                continue
            cur = "and" if isinstance(n.op, ast.And) else "or"
            a, b = self.span(n)
            count = len(n.values) - 1
            for k in range(count):
                for alt in ("or" if cur == "and" else "and", "^"):
                    ops = [cur] * count
                    ops[k] = alt
                    self.add("COR", n, a, b, self._boolop_text(n.values, ops), f"{cur} -> {alt} (operator {k + 1})")

    def gen_COI(self):
        targets = []
        for n in self.nodes:
            if isinstance(n, (ast.If, ast.While, ast.IfExp, ast.Assert)):
                targets.append(n.test)
            elif isinstance(n, ast.comprehension):
                targets.extend(n.ifs)
            elif isinstance(n, ast.match_case) and n.guard is not None:
                targets.append(n.guard)
            if isinstance(n, (ast.Compare, ast.BoolOp)):
                targets.append(n)
            if isinstance(n, ast.BoolOp):
                targets.extend(n.values)
        uniq = {id(t): t for t in targets
                if self.site_ok(t) and id(t) not in self.not_operands
                and not (isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not))}
        for t in sorted(uniq.values(), key=position):
            a, b = self.span(t)
            self.add("COI", t, a, b, f"(not ({self.text(a, b)}))", "insert not")

    def gen_COD(self):
        self._delete_unary("COD", ast.Not)

    def gen_SOR(self):
        for n in self.nodes:
            if isinstance(n, ast.BinOp) and type(n.op) in SHIFT and self.site_ok(n):
                t = self.token(self.span(n.left)[1], self.span(n.right)[0], OP_TOKEN)
                if t:
                    alt = ">>" if t[2] == "<<" else "<<"
                    self.add("SOR", n, t[0], t[1], alt, f"{t[2]} -> {alt}")

    def gen_LOR(self):
        self._rebuild_binop("LOR", BITWISE)

    def gen_LOI(self):
        self._insert_unary("LOI", "~", {**ARITH, **BITWISE, **SHIFT})

    def gen_LOD(self):
        self._delete_unary("LOD", ast.Invert)

    def gen_PY_ASRS(self):
        for n in self.nodes:
            if isinstance(n, ast.AugAssign) and self.site_ok(n):
                t = self.token(self.span(n.target)[1], self.span(n.value)[0], AUG_TOKEN)
                if not t or t[2] not in PY_ASRS_GROUP_OF:
                    continue
                for alt in PY_ASRS_SET:
                    if alt != t[2]:
                        label = "PY-ASRS-SAME" if PY_ASRS_GROUP_OF[alt] == PY_ASRS_GROUP_OF[t[2]] else "PY-ASRS-X"
                        self.add(label, n, t[0], t[1], alt, f"{t[2]} -> {alt}")

    def gen_SDL(self):
        for s in self._statements():
            if self.site_ok(s):
                a, b = self.span(s)
                whole = hasattr(s, "body") or hasattr(s, "cases")
                self.add("SDL", s, a, b, "pass", f"delete {type(s).__name__}" + (" (whole block)" if whole else ""))

    def _delete_operand(self, op_name, pick, label):
        for n in self.nodes:
            if not self.site_ok(n):
                continue
            if isinstance(n, ast.BinOp) and not isinstance(n.op, ast.MatMult):
                a, b = self.span(n)
                for drop, keep in ((n.left, n.right), (n.right, n.left)):
                    if pick(drop):
                        self.add(op_name, n, a, b, self.paren(keep), f"delete {label} {self.src(drop)!r}")
            elif isinstance(n, ast.Compare):
                a, b = self.span(n)
                for j, drop in enumerate([n.left, *n.comparators]):
                    op_index = j - 1 if j > 0 else 0
                    if pick(drop) and not self._membership_operand(n, j):
                        new = self._compare_without(n, j, op_index)
                        if new:
                            self.add(op_name, n, a, b, new, f"delete {label} {self.src(drop)!r}")
            elif isinstance(n, ast.BoolOp):
                a, b = self.span(n)
                cur = "and" if isinstance(n.op, ast.And) else "or"
                for j, drop in enumerate(n.values):
                    if pick(drop):
                        rest = [v for k, v in enumerate(n.values) if k != j]
                        self.add(op_name, n, a, b, self._boolop_text(rest, [cur] * (len(rest) - 1)),
                                 f"delete {label} {self.src(drop)!r}")

    def gen_VDL(self):
        self._delete_operand("VDL", is_load_ref, "variable")

    def gen_CDL(self):
        self._delete_operand("CDL", lambda o: isinstance(o, ast.Constant), "constant")

    def gen_ODL(self):
        for n in self.nodes:
            if not self.site_ok(n):
                continue
            if isinstance(n, ast.BinOp) and not isinstance(n.op, ast.MatMult):
                a, b = self.span(n)
                self.add("ODL", n, a, b, self.paren(n.right), "delete operator and left operand")
                self.add("ODL", n, a, b, self.paren(n.left), "delete operator and right operand")
            elif isinstance(n, ast.Compare):
                a, b = self.span(n)
                for i, op in enumerate(n.ops):
                    if isinstance(op, MEMBERSHIP):
                        continue
                    for drop, side in ((i, "left"), (i + 1, "right")):
                        if self._membership_operand(n, drop):
                            continue
                        new = self._compare_without(n, drop, i)
                        if new:
                            self.add("ODL", n, a, b, new, f"delete operator {i + 1} and its {side} operand")
            elif isinstance(n, ast.BoolOp):
                # Binary, left-associated view (as COR): operator k joins (v0 .. v[k-1]) and v[k].
                a, b = self.span(n)
                cur = "and" if isinstance(n.op, ast.And) else "or"
                for k in range(1, len(n.values)):
                    left_gone = n.values[k:]
                    right_gone = n.values[:k] + n.values[k + 1:]
                    self.add("ODL", n, a, b, self._boolop_text(left_gone, [cur] * (len(left_gone) - 1)),
                             f"delete operator {k} and its left operand")
                    self.add("ODL", n, a, b, self._boolop_text(right_gone, [cur] * (len(right_gone) - 1)),
                             f"delete operator {k} and its right operand")
            elif isinstance(n, ast.UnaryOp):
                a, b = self.span(n)
                self.add("ODL", n, a, b, self.paren(n.operand), "delete unary operator")
            elif isinstance(n, ast.AugAssign) and not isinstance(n.op, ast.MatMult):
                t = self.token(self.span(n.target)[1], self.span(n.value)[0], AUG_TOKEN)
                if t:
                    self.add("ODL", n, t[0], t[1], "=", f"{t[2]} -> =")

    def run(self):
        for op in ORDER:
            getattr(self, "gen_" + op.replace("-", "_"))()
        counts = {}
        for m in self.mutants:
            counts[m["op"]] = counts.get(m["op"], 0) + 1
            m["id"] = f"{m['op']}-{counts[m['op']]:03d}"
        return self.mutants, {"version": VERSION, "mutants": len(self.mutants), "by_operator": counts,
                              "invalid_skipped": self.invalid, "duplicates_skipped": self.duplicates}


def generate(data: bytes, lines=None):
    return Generator(data, lines).run()


def parse_lines(spec):
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if re.fullmatch(r"\d+", part):
            out.add(int(part))
        elif re.fullmatch(r"\d+-\d+", part):
            lo, hi = map(int, part.split("-"))
            if lo > hi:
                raise ValueError(f"bad --lines range {part!r}: the start is after the end")
            out.update(range(lo, hi + 1))
        else:
            raise ValueError(f"bad --lines part {part!r}; use numbers and ranges like 10,12-30")
    return out


SAMPLE = '''\
import os

LIMIT = 1 + 2  # module level: never mutated
F = lambda v: v + 1  # module-level lambda: never mutated


def f(a, b, c, xs, obj, cb=lambda w: w - 1):
    """The docstring is never mutated."""
    total = a + b * c
    total -= 1
    flags = a & b | c
    shifted = a << 2
    neg = -a
    inv = ~b
    r = (a + b) * c
    if a > b and not obj is None:
        total += a
    elif a == b:
        total = 0
    while c <= 3 or a != b:
        c = c + 1
    if a < b <= c:
        pass
    y = [x for x in xs if x in (1, 2)]
    z = a if b < c else (a - b)
    g = lambda q: q * 2
    p = c - a * b
    u = -a ** b
    w = a ^ b | c
    ok = obj is None
    return total


class K:
    attr = 5 * 5  # class body: never mutated

    def m(self, n):
        n <<= 1
        n &= 3
        return n
'''


def selftest():
    data = SAMPLE.encode()
    muts, stats = generate(data)
    by = {}
    for m in muts:
        by.setdefault(m["op"], []).append(m)
        compile(m["source"], "<mutant>", "exec", dont_inherit=True)
        assert m["line"] not in (3, 4, 7, 8, 35), f"mutated outside method bodies or the docstring: {m}"

    def mutated_line(m):
        return m["source"].decode().splitlines()[m["line"] - 1].strip()

    def has(op, line, text):
        return any(m["line"] == line and mutated_line(m) == text for m in by.get(op, []))

    expected = [
        ("AORB", 9, "total = ((a) - (b * c))"), ("AORB", 9, "total = a + ((b) // (c))"),
        ("AORB", 9, "total = a + ((b) ** (c))"), ("AORB", 15, "r = ((a + b) / (c))"),
        ("AORB", 27, "p = c - ((a) + (b))"), ("AORB", 28, "u = -((a) + (b))"),
        ("AORB", 26, "g = lambda q: ((q) / (2))"),
        ("AOIU", 9, "total = (-a) + b * c"), ("AOIU", 20, "while (-c) <= 3 or a != b:"),
        ("AODU", 13, "neg = (a)"),
        ("ROR", 16, "if a >= b and not obj is None:"), ("ROR", 16, "if a > b and not obj is not None:"),
        ("ROR", 16, "if True and not obj is None:"), ("ROR", 18, "elif a != b:"), ("ROR", 22, "if a < b < c:"),
        ("ROR", 30, "ok = obj is not None"),
        ("COR", 16, "if ((a > b) or (not obj is None)):"), ("COR", 16, "if ((a > b) ^ (not obj is None)):"),
        ("COR", 20, "while ((c <= 3) and (a != b)):"), ("COR", 20, "while ((c <= 3) ^ (a != b)):"),
        ("COI", 16, "if (not (a > b and not obj is None)):"), ("COI", 16, "if (not (a > b)) and not obj is None:"),
        ("COI", 24, "y = [x for x in xs if (not (x in (1, 2)))]"), ("COI", 25, "z = a if (not (b < c)) else (a - b)"),
        ("COI", 30, "ok = (not (obj is None))"),
        ("COD", 16, "if a > b and (obj is None):"),
        ("SOR", 12, "shifted = a >> 2"),
        ("LOR", 11, "flags = ((a & b) ^ (c))"), ("LOR", 11, "flags = ((a) | (b)) | c"),
        ("LOR", 29, "w = ((a ^ b) & (c))"),
        ("LOI", 11, "flags = (~a) & b | c"), ("LOD", 14, "inv = (b)"),
        ("PY-ASRS-SAME", 10, "total += 1"), ("PY-ASRS-SAME", 10, "total //= 1"),
        ("PY-ASRS-SAME", 10, "total **= 1"), ("PY-ASRS-SAME", 10, "total @= 1"),
        ("PY-ASRS-X", 10, "total >>= 1"), ("PY-ASRS-X", 10, "total &= 1"),
        ("PY-ASRS-SAME", 38, "n >>= 1"), ("PY-ASRS-X", 38, "n -= 1"),
        ("PY-ASRS-SAME", 39, "n |= 3"), ("PY-ASRS-X", 39, "n += 3"),
        ("SDL", 10, "pass"), ("SDL", 16, "pass"), ("SDL", 18, "pass"), ("SDL", 31, "pass"),
        ("VDL", 9, "total = (b * c)"), ("VDL", 20, "while (3) or a != b:"), ("VDL", 22, "if ((a) <= (c)):"),
        ("CDL", 12, "shifted = (a)"), ("CDL", 20, "while (c) or a != b:"),
        ("ODL", 9, "total = (a)"), ("ODL", 10, "total = 1"), ("ODL", 22, "if ((a) < (c)):"),
        ("ODL", 16, "if (not obj is None):"), ("ODL", 16, "if (a > b):"),
    ]
    absent = [
        ("AOIU", 18, "elif (-a) == b:"), ("ROR", 30, "ok = True"), ("ROR", 16, "if a > b and not True:"),
        ("COI", 16, "if a > b and not (not (obj is None)):"), ("VDL", 24, "y = [x for x in xs if ((1, 2))]"),
        ("AORB", 9, "total = a - b * c"),
    ]
    missing = [c for c in expected if not has(*c)]
    assert not missing, f"expected mutants missing: {missing}"
    present = [c for c in absent if has(*c)]
    assert not present, f"mutants that must not exist: {present}"
    assert not any(m["line"] == 24 and m["op"] in ("ROR", "VDL", "CDL", "ODL") for m in muts), "membership mutated"
    assert len({m["source"] for m in muts}) == len(muts), "duplicate mutated files"
    assert len({m["id"] for m in muts}) == len(muts), "duplicate ids"
    assert stats["invalid_skipped"] == 0, f"invalid mutants generated: {stats['invalid_skipped']}"
    scoped = generate(data, lines={10})[0]
    assert scoped and all(m["line"] == 10 for m in scoped), "line filter"

    bom_muts, bom_stats = generate(BOM + b"def f(a, b):\n    return a + b\n")
    assert any(m["source"].endswith(b"return ((a) - (b))\n") for m in bom_muts), "BOM offsets"
    assert bom_stats["invalid_skipped"] == 0, "BOM produced invalid mutants"
    first_line = generate(BOM + b"def f(a, b): return a + b\n")[0]
    assert any(m["source"] == BOM + b"def f(a, b): return ((a) - (b))\n" for m in first_line), "BOM on line 1"
    try:
        generate(b"# -*- coding: latin-1 -*-\ndef f(a):\n    return a\n")
        raise AssertionError("non-UTF-8 source accepted")
    except ValueError:
        pass
    walrus_muts, walrus_stats = generate(b"def f(a, b, x: (y := int) = 0):\n    return a + b\n")
    assert walrus_muts and walrus_stats["invalid_skipped"] == 0, "compile inherited this module's future flags"
    matmul = generate(b"def f(o, m):\n    o.m @= m\n")[0]
    assert not any(m["after"] == "=" for m in matmul), "@= deleted by ODL"
    assert sum(m["op"].startswith("PY-ASRS") for m in matmul) == 12, "PY-ASRS on @= must give 12 candidates"
    for line in (10, 17, 38, 39):
        got = [m for m in muts if m["line"] == line and m["op"].startswith("PY-ASRS")]
        assert len(got) == 12, f"PY-ASRS site on line {line} gave {len(got)} candidates, not 12"
    fixed = generate(b"class C:\n    def pop(self):\n        self._push_count -= 1\n")[0]
    assert any(m["op"] == "PY-ASRS-X" and m["what"] == "-= -> >>=" and
               m["source"].endswith(b"self._push_count >>= 1\n") for m in fixed), "fixed candidate -= 1 -> >>= 1"
    for bad in ("2-", "30-12"):
        try:
            parse_lines(bad)
            raise AssertionError(f"bad --lines {bad!r} accepted")
        except ValueError:
            pass
    for cookie in (b"# coding: utf8\n", b"# -*- coding: ascii -*-\n"):
        assert generate(cookie + b"def f(a):\n    return a + 1\n")[0], f"UTF-8-compatible cookie rejected: {cookie!r}"
    chained = [m["source"] for m in generate(b"def f(a, b, c):\n    return a < b in c, a in b < c, a is b not in c\n")[0]]
    rewired = [s for s in chained if b"((a) in (c))" in s or b"((a) not in (c))" in s]
    assert not rewired, "a deletion rewired a membership link"
    warn_src = b"def f(a, b):\n    s = '\\d'\n    return a is b < 1\n"
    plain = len(generate(warn_src)[0])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        strict = len(generate(warn_src)[0])
    assert plain == strict, "mutant validity depends on the caller's warning filters"
    print(f"selftest ok: {stats['mutants']} mutants on the sample, {len(expected)} expected found, "
          f"{len(absent)} forbidden absent, PY-ASRS (12 per site, -= 1 -> >>= 1 as PY-ASRS-X) and "
          f"encoding/BOM/future/@=/--lines checks passed; "
          f"by operator {stats['by_operator']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--lines", help="only sites starting on these lines, e.g. 10,12-30")
    ap.add_argument("--out", help="write the mutants (without file contents) as JSON")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return 0
    if not args.file:
        ap.error("FILE is required unless --selftest")
    try:
        lines = parse_lines(args.lines) if args.lines else None
    except ValueError as e:
        ap.error(str(e))
    with open(args.file, "rb") as fh:
        data = fh.read()
    try:
        muts, stats = generate(data, lines)
    except (ValueError, SyntaxError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    stats["file"] = args.file
    stats["file_sha256"] = hashlib.sha256(data).hexdigest()
    rows = [{k: v for k, v in m.items() if k != "source"} for m in muts]
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"stats": stats, "mutants": rows}, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
