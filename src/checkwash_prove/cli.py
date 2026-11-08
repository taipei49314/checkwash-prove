"""The `checkwash-prove` command.

A separate command from `checkwash`, as checkwash's satellite rule asks (docs/satellite-execution.md in checkwash):
this package never imports checkwash, and checkwash never imports it.
"""
from __future__ import annotations

import argparse
import codecs
import json
import math
import os
import re
import shutil
import subprocess
import sys

from checkwash_prove import __version__

DEFAULT_TIMEOUT = 30.0
DEFAULT_SUITE_TIMEOUT = 900.0
NAME = "checkwash-prove"


class GitError(Exception):
    """Git could not say where the repository is."""


def write_text(text: str, stream=None) -> None:
    """Keep Unicode streams intact; visibly escape legacy output, never '?'.

    A legacy Windows Python pipe and its shell can disagree on their code page. ASCII escapes survive either decoder.
    """
    if stream is None:
        stream = sys.stdout
    encoding = getattr(stream, "encoding", None)
    if encoding:
        try:
            normalized = codecs.lookup(encoding).name
        except LookupError:
            normalized = "ascii"
        if not normalized.startswith(("utf-8", "utf-16", "utf-32")):
            text = text.encode("ascii", errors="backslashreplace").decode("ascii")
        else:
            text = text.encode(encoding, errors="backslashreplace").decode(encoding)
    stream.write(text)


def write_machine(text: str) -> None:
    """JSON on standard output is UTF-8 bytes, whatever the console's code page."""
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(text)
        return
    sys.stdout.flush()
    buffer.write(text.encode("utf-8"))
    buffer.flush()


def _advise(text: str) -> None:
    """Lines for standard error. They never reach stdout, where a closed stderr would send them ahead of a JSON
    report, nor the exit code; and the repository's text in them (paths, pytest output, Git's messages) is escaped,
    as in the text report, with a message's later lines marked as its continuation."""
    if sys.stderr is None:
        return
    try:
        from checkwash_prove._text import visible
    except Exception:  # noqa: BLE001 - a damaged install still reports, with everything but printable ASCII escaped
        def visible(line: str) -> str:
            return "".join(c if " " <= c <= "~" else f"\\u{ord(c):04x}" for c in line)
    first, *rest = text.splitlines() or [""]
    try:
        write_text("".join(line + "\n" for line in [visible(first), *("  | " + visible(r) for r in rest)]),
                   sys.stderr)
        sys.stderr.flush()
    except (OSError, ValueError):
        # A stderr that cannot be written (a closed pipe) is dropped, so Python's flush at exit cannot fail on it
        # and turn the exit code into 120.
        sys.stderr = None


def _git_read(repo: str, args: list[str]) -> bytes:
    # Without Git's location variables (GIT_DIR from a hook): --repo names the repository.
    from checkwash_prove.engine import _clean_env

    try:
        proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, check=False, timeout=60,
                              stdin=subprocess.DEVNULL, env=_clean_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError("git read could not complete") from exc
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def repository_root(repo: str) -> tuple[str, str]:
    """(path, prefix): where every Git call runs for `--repo`.

    From a subdirectory Git answers in two path bases: `diff` is root-relative while `ls-tree` sees only the subtree,
    so `path` is Git's toplevel there and `prefix` is the subdirectory (`pkg/sub/`). At the root, and where Git has no
    work tree (a bare repository, inside `.git`), `repo` comes back unchanged with an empty prefix. The toplevel is a
    second query because `--show-toplevel` fails without a work tree; it is never `repo` joined with `--show-cdup`,
    which lands elsewhere through a symlinked directory. Any failure is a GitError.
    """
    raw = _git_read(repo, ["rev-parse", "--is-inside-work-tree", "--show-prefix"])
    answer = re.fullmatch(rb"(true|false)\n([^\n]*)\n", raw)
    if answer is None:
        raise GitError("git rev-parse returned an invalid work-tree answer")
    if answer.group(1) != b"true" or not answer.group(2):
        return repo, ""
    try:
        prefix = answer.group(2).decode("utf-8", "strict")
    except UnicodeError as exc:
        raise GitError("git rev-parse returned an undecodable prefix") from exc
    if not prefix.endswith("/") or any(part in {"", ".", ".."} for part in prefix[:-1].split("/")):
        raise GitError(f"git rev-parse returned an invalid prefix {prefix!r}")
    raw = _git_read(repo, ["rev-parse", "--show-toplevel"])
    try:
        top = raw.decode("utf-8", "strict")
    except UnicodeError as exc:
        raise GitError("git rev-parse returned an undecodable repository root") from exc
    if not top.endswith("\n") or "\n" in top[:-1]:
        raise GitError("git rev-parse returned an incomplete repository root")
    top = top[:-1]
    if not top or not os.path.isabs(top) or not os.path.isdir(top):
        raise GitError(f"git rev-parse returned an unusable repository root {top!r}")
    return top, prefix


def range_usage_error(spec: str) -> str | None:
    from checkwash_prove.engine import parse_range

    if parse_range(spec) is None:
        return f"error: {NAME} needs a range BASE..HEAD or BASE...HEAD of two commits, got {spec!r}\n"
    return None


def interpreter(given: str | None) -> str:
    """An absolute interpreter path: the tests run with the temporary tree as their working directory."""
    if not given:
        return sys.executable
    if os.sep in given or (os.altsep and os.altsep in given) or os.path.exists(given):
        return os.path.abspath(given)
    return shutil.which(given) or given


def exit_code(report: dict, fail_on_loss: bool) -> int:
    """0, or with --fail-on-loss: 1 for a loss; 2 when the comparison did not happen or some of it could not be
    decided (a whole-suite run with no usable result, a loss that did not repeat alone); 0 when there was nothing to
    compare or every mutant run was decided without a loss."""
    if not fail_on_loss:
        return 0
    if report["protection_losses"]:
        return 1
    if report.get("nothing_to_compare") or str(report.get("status", "")).startswith("listed"):
        return 0
    categories = report.get("mutation", {}).get("categories", {})
    if not report.get("compared"):
        status = " | ".join(str(report.get("status")).splitlines())
        _advise(f"{NAME}: not compared, so neither passed nor failed: {status}")
        return 2
    undecided = categories.get("undecided", 0) + categories.get("unconfirmed loss", 0)
    if undecided:
        _advise(f"{NAME}: {undecided} mutant(s) could not be decided (undecided, or a loss that did not repeat "
                "alone), so neither passed nor failed")
        return 2
    return 0


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"needs a number of seconds above 0, got {text!r}")
    return value


def _count(least: int):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            value = least - 1
        if value < least:
            raise argparse.ArgumentTypeError(f"needs a whole number of at least {least}, got {text!r}")
        return value
    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=NAME,
        description="Experimental. Runs a change's old and new tests against catalogue mutants to show, with a "
                    "concrete fault, protection the change dropped. It executes the repository's code at both "
                    "revisions: run it only on code you would run anyway.",
    )
    parser.add_argument("--version", action="version", version=f"{NAME} {__version__}")
    parser.add_argument("range", help="BASE..HEAD, or BASE...HEAD to compare from the merge base (use this one "
                                      "for a branch)")
    parser.add_argument("--repo", default=".",
                        help="any directory of the Git repository (default: .); runs from the repository root")
    parser.add_argument("--format", choices=["term", "json"], default="term")
    parser.add_argument("--out", metavar="FILE", help="also write the JSON report to FILE")
    parser.add_argument("--python", default=None, help="interpreter that runs the tests (default: this one)")
    parser.add_argument("--import-root", default=None,
                        help="directory under the root that holds the product packages (default: src if present)")
    parser.add_argument("--workers", type=_count(1), default=1,
                        help="mutant runs side by side (default 1; tests that share ports or files can interfere)")
    parser.add_argument("--timeout", type=_seconds, default=DEFAULT_TIMEOUT,
                        help="least seconds a run against one mutant gets; it gets three times its clean run's "
                             "time plus 10 when that is more (default 30)")
    parser.add_argument("--suite-timeout", type=_seconds, default=DEFAULT_SUITE_TIMEOUT,
                        help="most seconds a clean run gets (default 900)")
    parser.add_argument("--max-mutants", type=_count(0), default=200, help="cap on mutants run (default 200)")
    parser.add_argument("--only", metavar="ID", help="run one mutant, e.g. src/pkg/mod.py:467:8:PY-ASRS-X:2")
    parser.add_argument("--list", action="store_true",
                        help="list the mutants with their ids, in run order, without running them")
    parser.add_argument("--strict-warnings", action="store_true",
                        help="keep DeprecationWarning settings when old tests run on the new code")
    parser.add_argument("--fail-on-loss", action="store_true",
                        help="exit 1 when a protection loss is found; 2 when the comparison did not happen or "
                             "part of it could not be decided; 0 otherwise, 'nothing to compare' included")
    parser.add_argument("--keep", action="store_true",
                        help="keep the temporary trees (their directory is printed on standard error)")
    return parser


def run(args: argparse.Namespace) -> int:
    from checkwash_prove.engine import Options, ProveError, normalise_root, prove, render

    python = interpreter(args.python)
    import_root = normalise_root(args.import_root) if args.import_root else None
    replay = []
    if args.python:
        replay += ["--python", python]
    if import_root:
        replay += ["--import-root", import_root]
    if args.timeout != DEFAULT_TIMEOUT:
        replay += ["--timeout", f"{args.timeout:g}"]
    if args.suite_timeout != DEFAULT_SUITE_TIMEOUT:
        replay += ["--suite-timeout", f"{args.suite_timeout:g}"]
    if args.strict_warnings:
        replay += ["--strict-warnings"]
    opts = Options(python=python, workers=args.workers, timeout=args.timeout, suite_timeout=args.suite_timeout,
                   max_mutants=args.max_mutants, import_root=import_root, strict_warnings=args.strict_warnings,
                   only=args.only, keep=args.keep, list_only=args.list, replay=replay)
    try:
        report = prove(args.repo, args.range, opts)
    except ProveError as exc:
        _advise(f"error: {exc}")
        return 2
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    if args.format == "json":
        write_machine(text)
    else:
        write_text(render(report), sys.stdout)
    return exit_code(report, args.fail_on_loss)


def _engine_error(detail: str) -> int:
    _advise(f"{NAME} engine error: {detail}")
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Everything after parsing is inside the guard, the engine's import included: an uncaught exception exits 1,
    # which --fail-on-loss means as a loss. argparse's own usage errors exit 2.
    try:
        # The range is checked before Git is asked anything, so a malformed one is a usage error wherever it is typed.
        usage = range_usage_error(args.range)
        if usage:
            _advise(usage)
            return 2
        given = args.repo
        args.repo, prefix = repository_root(given)
        if prefix:
            _advise(f"{NAME}: using the repository root {args.repo}, not its subdirectory {prefix}")
        return run(args)
    except (GitError, OSError, RecursionError) as exc:
        return _engine_error(str(exc))
    except Exception as exc:  # noqa: BLE001 - a crash must never read as a loss, which --fail-on-loss reports as 1
        return _engine_error(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
