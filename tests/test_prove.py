"""`checkwash-prove` (experimental): findings come from test runs, not from reading.

For a range BASE..HEAD, prove runs the old versions of the changed test files
against the new code, mutates the product lines the passing ones execute with
the frozen catalogue v0.5-m2, and reports a protection loss only when a mutant
is caught by the old tests and lets the new changed tests and the whole new
suite pass, twice, the second time alone. These tests build small Git
repositories and run the real procedure on them, so each claim below is a
run's outcome: a weakened assertion is a loss; a slow test, a file a test
leaves behind, an interrupted clean run, a test the whole suite still has, a
branch behind main or HEAD's own pytest configuration is not; an old test the
new code fails is "needs clarification"; and no mutants or no old tests is
said as such, never as a clean result.
"""
import ast
import hashlib
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys

import pytest

import checkwash_prove
from checkwash_prove.cli import GitError, exit_code, interpreter, main, range_usage_error, repository_root
from checkwash_prove.engine import (
    CATALOGUE_SHA256,
    CATALOGUE_VERSION,
    Options,
    ProveError,
    RunResult,
    _XDIST_OPTION,
    _killed,
    changed_lines,
    is_test_module,
    is_test_path,
    normalise_root,
    parse_range,
    Naming,
    Tree,
    _shown,
    naming_rules,
    prove,
    render,
    verify_catalogue,
)

SQUARE = b"def square(x):\n    return x * x\n"
STRONG = b"from pkg.calc import square\n\n\ndef test_square():\n    assert square(3) == 9\n"
# square(2) == 4 also holds for x + x and x ** x: the new test lets both through.
WEAK = STRONG.replace(b"square(3) == 9", b"square(2) == 4")
FROZEN_GENERATOR = "67096f7340a281585aaeaf801ae0ac29c4eb1a777c358ee973f02058cc7f4e14"


def _git(repo, *args):
    proc = subprocess.run(
        ["git", "-c", "user.name=checkwash-test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
        cwd=repo, capture_output=True, check=True,
    )
    return proc.stdout.decode("utf-8").strip()


def _write(repo, files):
    for rel, data in files.items():
        path = repo / rel
        if data is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")


def _repo(tmp_path, base, head):
    """A repository with two commits, BASE and HEAD; a None value deletes the file in HEAD."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, base)
    _write(repo, head)
    return repo, _git(repo, "rev-parse", "HEAD~1"), _git(repo, "rev-parse", "HEAD")


def _opts(**kw):
    # One worker: these tests share CI with the suite's wall-clock performance gates.
    return Options(python=sys.executable, **{"workers": 1, **kw})


def _prove(repo, base, head, **kw):
    return prove(str(repo), f"{base}..{head}", _opts(**kw))


def _weakened(tmp_path):
    return _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG},
        {"tests/test_calc.py": WEAK},
    )


def _loss_ids(report):
    return [loss["id"] for loss in report["protection_losses"]]


# ---- input checks --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("spec", "parsed"), [
    ("main..HEAD", ("main", "HEAD", False)),
    ("abc123..def456", ("abc123", "def456", False)),
    ("v1.0..HEAD~2", ("v1.0", "HEAD~2", False)),
    ("main...HEAD", ("main", "HEAD", True)),
])
def test_two_and_three_dot_ranges_are_accepted(spec, parsed):
    assert parse_range(spec) == parsed
    assert range_usage_error(spec) is None


@pytest.mark.parametrize("spec", ["HEAD", "..HEAD", "main..", "main....HEAD", "...HEAD", "", "a..b..c", "a..b...c"])
def test_anything_but_a_range_is_a_usage_error(spec):
    assert "BASE..HEAD or BASE...HEAD" in range_usage_error(spec)


def test_the_cli_refuses_a_single_revision_before_touching_git(tmp_path, capsys):
    # tmp_path is no repository: had Git been asked first, it would be a Git error.
    assert main(["HEAD", "--repo", str(tmp_path)]) == 2
    assert "prove needs a range BASE..HEAD or BASE...HEAD" in capsys.readouterr().err


def test_the_vendored_catalogue_is_the_frozen_generator():
    # The catalogue is the evidence rule: a mutant counts because the frozen
    # v0.5-m2 generator produced it. The hash is written here as well as in
    # the engine, so editing the file and the engine's constant still fails.
    data = (pathlib.Path(checkwash_prove.__file__).parent / "mutops_v05m2.py").read_bytes()
    assert hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() == FROZEN_GENERATOR
    assert CATALOGUE_VERSION == "v0.5-m2"
    assert verify_catalogue() == CATALOGUE_SHA256 == FROZEN_GENERATOR


@pytest.mark.parametrize(
    ("path", "suite", "module"),
    [
        ("tests/test_calc.py", True, True),
        ("tests/conftest.py", True, False),
        ("conftest.py", True, False),
        ("pkg/calc_test.py", True, True),
        ("tests/helpers.py", True, False),
        ("Tests/helpers.py", True, False),
        # Data the old tests read goes back to BASE with them.
        ("tests/data/expected.json", True, False),
        ("src/pkg/testing.py", False, False),
        ("src/pkg/calc.py", False, False),
        ("docs/test_plan.md", False, False),
    ],
)
def test_which_files_belong_to_the_test_suite(path, suite, module):
    assert is_test_path(path) is suite
    assert is_test_module(path) is module


@pytest.mark.parametrize(("given", "root"), [("src", "src"), ("./src/", "src"), ("src\\", "src"),
                                             ("python\\src", "python/src"), (".", "."), ("./", ".")])
def test_an_import_root_is_named_the_same_however_it_is_typed(given, root):
    assert normalise_root(given) == root


def test_a_relative_interpreter_becomes_absolute(tmp_path, monkeypatch):
    # The tests run with the temporary tree as their working directory.
    monkeypatch.chdir(tmp_path)
    assert interpreter(os.path.join("venv", "bin", "python")) == str(tmp_path / "venv" / "bin" / "python")
    assert interpreter(None) == sys.executable


def test_changed_lines_are_the_head_lines_the_range_added_or_replaced(tmp_path):
    repo, base, head = _repo(
        tmp_path,
        {"pkg/a.py": b"a = 1\nb = 2\nc = 3\n", "pkg/gone.py": b"x = 1\ny = 2\n", "my pkg/b.py": b"v = 1\n"},
        {"pkg/a.py": b"a = 1\nb = 20\nc = 3\nd = 4\n", "pkg/gone.py": b"x = 1\n", "my pkg/b.py": b"v = 2\n"},
    )
    # A file that only lost lines is still a changed file, with no changed line;
    # a path with a space is read from the change list, not parsed out of a patch.
    assert changed_lines(str(repo), base, head, ["pkg/a.py", "pkg/gone.py", "my pkg/b.py"]) == {
        "pkg/a.py": {2, 4}, "pkg/gone.py": set(), "my pkg/b.py": {1}}


PASSED = {("tests.test_calc", "test_square"): "passed"}


@pytest.mark.parametrize(
    ("clean", "run", "caught"),
    [
        (RunResult(0, PASSED, {}), RunResult(1, {("tests.test_calc", "test_square"): "failed"}, {}),
         ["tests.test_calc::test_square"]),
        (RunResult(0, PASSED, {}), RunResult("timeout", {}, {}), ["(timeout)"]),
        # CI passes a skipped test, so a mutant that makes a test skip got through.
        (RunResult(0, PASSED, {}), RunResult(0, {("tests.test_calc", "test_square"): "skipped"}, {}), None),
        (RunResult(0, PASSED, {}), RunResult(0, PASSED, {}), None),
        # A collection error fails CI though no test that passed has failed.
        (RunResult(0, PASSED, {}), RunResult(2, PASSED, {}), ["(pytest exit 2)"]),
        # A run that died without results, while the clean one had passes.
        (RunResult(0, PASSED, {}), RunResult(5, {}, {}), ["(pytest exit 5, no results)"]),
        (RunResult(1, PASSED, {}), RunResult(1, {}, {}), ["(pytest exit 1, no results)"]),
        # The clean run already exited 1 (an old test that needs clarification): the same exit is no catch.
        (RunResult(1, PASSED, {}), RunResult(1, PASSED, {}), None),
        # Nor is an interruption the clean run had too.
        (RunResult(2, PASSED, {}), RunResult(2, PASSED, {}), None),
        # A module that did not collect on the clean code either is no catch; one that stops collecting is.
        (RunResult(1, {**PASSED, ("", "tests.test_old"): "failed"}, {}),
         RunResult(1, {("", "tests.test_old"): "failed", ("", "tests.test_calc"): "failed"}, {}),
         ["tests.test_calc"]),
        (RunResult(1, {**PASSED, ("", "tests.test_old"): "failed"}, {}),
         RunResult(1, {**PASSED, ("", "tests.test_old"): "failed"}, {}), None),
        # A mutant that lets a module import that did not on the clean code
        # trades one failure for another: that part of the suite was red already.
        (RunResult(1, {**PASSED, ("", "tests.test_old"): "failed"}, {}),
         RunResult(1, {**PASSED, ("tests.test_old", "test_x"): "failed",
                       ("tests.test_old.TestY", "test_z"): "failed"}, {}), None),
        # A test that was skipped on the clean code and fails now fails CI.
        (RunResult(0, {**PASSED, ("tests.test_calc", "test_later"): "skipped"}, {}),
         RunResult(1, {**PASSED, ("tests.test_calc", "test_later"): "failed"}, {}),
         ["tests.test_calc::test_later"]),
    ],
)
def test_what_counts_as_caught_is_what_ci_would_fail(clean, run, caught):
    assert _killed(clean, run) == caught


@pytest.mark.parametrize(("report", "flag", "code"), [
    ({"protection_losses": [1], "status": "done", "compared": True}, False, 0),
    ({"protection_losses": [1], "status": "done", "compared": True}, True, 1),
    ({"protection_losses": [], "status": "done", "compared": True, "mutation": {"categories": {"both": 3}}}, True, 0),
    ({"protection_losses": [], "status": "done", "compared": True,
      "mutation": {"categories": {"both": 2, "undecided": 1}}}, True, 2),
    ({"protection_losses": [], "status": "nothing to compare: x", "nothing_to_compare": True}, True, 0),
    ({"protection_losses": [], "status": "no mutants: x"}, True, 2),
    ({"protection_losses": [], "status": "listed: no mutant was run"}, True, 0),
    # A loss that did not repeat alone was not decided either way.
    ({"protection_losses": [], "status": "done", "compared": True,
      "mutation": {"categories": {"both": 2, "unconfirmed loss": 1}}}, True, 2),
])
def test_fail_on_loss_never_passes_a_run_that_compared_nothing(report, flag, code):
    assert exit_code(report, flag) == code


# ---- the satellite -------------------------------------------------------------------------------------------------

def test_the_satellite_never_imports_checkwash():
    # checkwash's satellite rule: an execution companion lives outside checkwash and depends on checkwash's published
    # JSON at most, never on its code. Loading every module here must not load checkwash, installed or not.
    code = ("import sys, checkwash_prove, checkwash_prove.cli, checkwash_prove.engine, checkwash_prove.mutops_v05m2\n"
            "print(sorted(m for m in sys.modules if m == 'checkwash' or m.startswith('checkwash.')))\n")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"


def test_python_dash_m_is_the_same_command():
    proc = subprocess.run([sys.executable, "-m", "checkwash_prove", "--version"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == f"checkwash-prove {checkwash_prove.__version__}"


def test_outside_git_is_an_engine_error_not_a_loss(tmp_path, monkeypatch, capsys):
    # Exit 1 is "a loss" under --fail-on-loss, so nothing that stops the run may exit 1.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert main(["HEAD~1..HEAD", "--repo", str(tmp_path), "--fail-on-loss"]) == 2
    assert "checkwash-prove engine error: git rev-parse" in capsys.readouterr().err


def test_a_crash_exits_2_not_1(tmp_path, monkeypatch, capsys):
    repo, base, head = _weakened(tmp_path)

    def crash(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("checkwash_prove.engine.prove", crash)
    assert main([f"{base}..{head}", "--repo", str(repo), "--fail-on-loss"]) == 2
    assert "checkwash-prove engine error: RuntimeError: boom" in capsys.readouterr().err


def test_a_subdirectory_runs_from_the_repository_root(tmp_path, capsys):
    # From a subdirectory Git lists trees relative to it, so every Git call runs at the root, and stderr says so.
    repo, base, head = _weakened(tmp_path)
    code = main([f"{base}..{head}", "--repo", str(repo / "tests"), "--format", "json",
                 "--only", "pkg/calc.py:2:11:AORB:1", "--fail-on-loss"])
    captured = capsys.readouterr()

    assert code == 1
    assert _loss_ids(json.loads(captured.out)) == ["pkg/calc.py:2:11:AORB:1"]
    assert "checkwash-prove: using the repository root " in captured.err
    assert "not its subdirectory tests/" in captured.err


def test_the_terminal_report_escapes_what_the_repository_controls():
    # Test ids, paths and test output are the repository's: an escape sequence, a bidirectional override or a line
    # break in one must not reach the terminal as such.
    report = {
        "catalogue": {"version": CATALOGUE_VERSION}, "base": "a" * 40, "head": "b" * 40, "from_merge_base": False,
        "tests": {"old": ["tests/test_x.py"], "new": ["tests/test_x.py"]}, "status": "done",
        "needs_clarification": [{"test": "tests/test_x.py::test_\x1b[31mred", "reason": "fails on the new code",
                                 "detail": "AssertionError: ‮gnp.exe"}],
        "protection_losses": [{"path": "pkg/calc.py", "line": 2, "op": "AORB", "before": "x * x", "after": "x + x",
                               "old_caught_by": ["tests/test_x.py::test_a\nprotection losses: 0 x"],
                               "replay": "checkwash-prove a..b --only pkg/calc.py:2:11:AORB:1"}],
        "unconfirmed_losses": [], "mutation": {}, "notes": [],
    }
    lines = render(report).splitlines()

    assert not any(ch in "\x1b‮ " for line in lines for ch in line)
    assert "  - tests/test_x.py::test_\\u001b[31mred: fails on the new code" in lines
    assert "      AssertionError: \\u202egnp.exe" in lines
    assert "      caught only by the old test(s): tests/test_x.py::test_a\\u000aprotection losses: 0\\u2028x" in lines
    assert "protection losses: 1" in lines and "protection losses: 0" not in lines


# ---- runs ----------------------------------------------------------------------------------------------------------

def test_a_weakened_assertion_is_a_protection_loss(tmp_path):
    repo, base, head = _weakened(tmp_path)
    report = _prove(repo, base, head, max_mutants=2)

    assert report["status"] == "done"
    assert report["compared"] is True
    assert report["experimental"] is True
    assert report["catalogue"] == {"version": CATALOGUE_VERSION, "generator_sha256": CATALOGUE_SHA256}
    assert report["tests"] == {"old": ["tests/test_calc.py"], "new": ["tests/test_calc.py"]}
    assert report["needs_clarification"] == []
    # Twelve catalogue mutants on `return x * x`, two run under the cap: `*`
    # to `+` gives 6 (the old test catches it) and 4 (the new one does not);
    # `*` to `-` gives 0, which both catch.
    assert report["mutation"]["generated"] == 12
    assert report["mutation"]["run"] == 2
    assert report["mutation"]["skipped_over_cap"] == 10
    assert report["mutation"]["categories"] == {"loss": 1, "both": 1}
    (loss,) = report["protection_losses"]
    assert loss["id"] == "pkg/calc.py:2:11:AORB:1"
    assert (loss["path"], loss["line"], loss["before"], loss["after"]) == ("pkg/calc.py", 2, "x * x", "((x) + (x))")
    assert loss["old_caught_by"] == ["tests/test_calc.py::test_square"]
    assert loss["replay"].startswith(f"checkwash-prove {base}..{head} --repo ")
    assert loss["replay"].endswith(" --only pkg/calc.py:2:11:AORB:1")
    (row,) = [r for r in report["mutation"]["rows"] if r["category"] == "loss"]
    assert row["confirmation"] == "repeated"
    assert report["budgets"]["old"] >= 30


def test_only_replays_one_mutant_through_the_cli(tmp_path, capsys):
    repo, base, head = _weakened(tmp_path)
    code = main([f"{base}..{head}", "--repo", str(repo), "--format", "json", "--workers", "1",
                 "--only", "pkg/calc.py:2:11:AORB:6", "--fail-on-loss"])
    out = json.loads(capsys.readouterr().out)

    assert code == 1
    assert out["mutation"]["run"] == 1
    (loss,) = out["protection_losses"]
    assert (loss["id"], loss["after"]) == ("pkg/calc.py:2:11:AORB:6", "((x) ** (x))")


def test_the_terminal_report_and_the_out_file(tmp_path, capsys):
    repo, base, head = _weakened(tmp_path)
    out_file = tmp_path / "report.json"
    code = main([f"{base}..{head}", "--repo", str(repo), "--workers", "1", "--import-root", "./",
                 "--only", "pkg/calc.py:2:11:AORB:1", "--out", str(out_file)])
    text = capsys.readouterr().out

    assert code == 0
    assert "protection losses: 1" in text
    assert "caught only by the old test(s): tests/test_calc.py::test_square" in text
    assert "--import-root . --only pkg/calc.py:2:11:AORB:1" in text
    assert _loss_ids(json.loads(out_file.read_text(encoding="utf-8"))) == ["pkg/calc.py:2:11:AORB:1"]


def test_json_on_a_legacy_console_is_still_utf8_json(tmp_path, monkeypatch):
    # A cp1252 pipe once turned non-ASCII into \\xNN escapes: invalid JSON.
    test = "from pkg.calc import square\n\n\ndef test_平方():\n    assert square(3) == 9\n".encode("utf-8")
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": test},
        {"tests/test_calc.py": test.replace(b"square(3) == 9", b"square(2) == 4")},
    )
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    code = main([f"{base}..{head}", "--repo", str(repo), "--format", "json", "--workers", "1",
                 "--only", "pkg/calc.py:2:11:AORB:1"])
    sys.stdout.flush()

    assert code == 0
    (loss,) = json.loads(raw.getvalue().decode("utf-8"))["protection_losses"]
    assert loss["old_caught_by"] == ["tests/test_calc.py::test_平方"]


def test_an_unknown_mutant_id_is_an_error_not_an_empty_result(tmp_path, capsys):
    repo, base, head = _weakened(tmp_path)
    assert main([f"{base}..{head}", "--repo", str(repo), "--only", "pkg/calc.py:2:11:AORB:99"]) == 2
    assert "no mutant has id 'pkg/calc.py:2:11:AORB:99'" in capsys.readouterr().err


def test_list_names_every_mutant_in_run_order_and_runs_none(tmp_path):
    repo, base, head = _weakened(tmp_path)
    report = _prove(repo, base, head, list_only=True, max_mutants=2)

    assert report["status"] == "listed: no mutant was run"
    listed = report["mutation"]["candidates"]
    assert [c["id"] for c in listed[:2]] == ["pkg/calc.py:2:11:AORB:1", "pkg/calc.py:2:11:AORB:2"]
    assert len(listed) == report["mutation"]["generated"] == 12
    assert [c["within_cap"] for c in listed].count(True) == 2
    assert report["mutation"]["run"] == 0
    assert report["protection_losses"] == []


def test_a_mutant_id_does_not_depend_on_which_lines_ran(tmp_path):
    # An id names the operator and the position, line:column, so it is the
    # same whichever other lines a run traced (here `cube`, which none runs).
    calc = b"def cube(x):\n    return x * x\n\n\ndef square(x):\n    return x * x\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": calc, "tests/test_calc.py": STRONG},
        {"tests/test_calc.py": WEAK},
    )
    listed = _prove(repo, base, head, list_only=True)["mutation"]["candidates"]

    assert listed[0]["id"] == "pkg/calc.py:6:11:AORB:1"
    assert {c["line"] for c in listed} == {6}


def test_files_of_one_rank_take_turns_so_a_cap_reaches_each(tmp_path):
    both = b"from pkg.a import f\nfrom pkg.b import g\n\n\ndef test_fg():\n    assert f(3) == 9\n    assert g(3) == 9\n"
    # Both product lines are rewritten (same behaviour), so all their mutants rank 1.
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/a.py": b"def f(x):\n    return x*x\n",
         "pkg/b.py": b"def g(x):\n    return x*x\n", "tests/test_fg.py": both},
        {"pkg/a.py": b"def f(x):\n    return x * x\n", "pkg/b.py": b"def g(x):\n    return x * x\n",
         "tests/test_fg.py": both.replace(b"(3) == 9", b"(2) == 4")},
    )
    listed = _prove(repo, base, head, list_only=True)["mutation"]["candidates"]

    assert [c["id"] for c in listed[:4]] == ["pkg/a.py:2:11:AORB:1", "pkg/b.py:2:11:AORB:1",
                                             "pkg/a.py:2:11:AORB:2", "pkg/b.py:2:11:AORB:2"]
    assert {c["rank"] for c in listed} == {1}


def test_lines_only_failing_old_tests_run_are_mutated_last(tmp_path):
    # test_bump fails on the new code and cannot catch a mutant, so the line
    # only it runs comes last instead of crowding out the rest. It is not
    # dropped: a fixture or an import a failing test happens to run first can
    # serve the passing tests after it.
    calc = SQUARE + b"\n\ndef bump(x):\n    return x + 1\n"
    old = STRONG + b"\n\ndef test_bump():\n    from pkg.calc import bump\n    assert bump(1) == 3\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": calc, "tests/test_calc.py": old},
        {"tests/test_calc.py": WEAK},
    )
    report = _prove(repo, base, head, list_only=True)

    assert [c["test"] for c in report["needs_clarification"]] == ["tests/test_calc.py::test_bump"]
    ranks = [(c["line"], c["rank"]) for c in report["mutation"]["candidates"]]
    assert {rank for line, rank in ranks if line == 6} == {4}
    assert 4 not in {rank for line, rank in ranks if line == 2}
    assert [line for line, _ in ranks] == sorted(line for line, _ in ranks)


def test_a_fixture_built_during_a_failing_test_still_counts(tmp_path):
    # The module fixture is built in test_first's setup, and test_first fails;
    # test_three reuses the cached table. Its lines are still mutated.
    calc = SQUARE + b"\n\ndef table():\n    return {n: square(n) for n in range(5)}\n"
    test = (b"import pytest\n\nfrom pkg.calc import table\n\n\n@pytest.fixture(scope='module')\ndef tab():\n"
            b"    return table()\n\n\ndef test_first(tab):\n    assert tab[0] == 1\n\n\n"
            b"def test_three(tab):\n    assert tab[3] == 9\n")
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": calc, "tests/test_calc.py": test},
        {"tests/test_calc.py": test.replace(b"tab[3] == 9", b"tab[3] > 0")},
    )
    report = _prove(repo, base, head)

    assert "pkg/calc.py:2:11:AORB:1" in _loss_ids(report)


def test_a_test_that_cannot_run_under_a_tracer_is_compared_untraced(tmp_path):
    # Clean and mutant runs are untraced alike; only separate runs trace. A
    # test that fails under a tracer (timing, coverage) is no "needs
    # clarification" and still catches.
    guard = b"import sys\n\nfrom pkg.calc import square\n\n\ndef test_square():\n"
    check = b"    assert sys.gettrace() is None\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE,
         "tests/test_calc.py": guard + b"    value = square(3)\n" + check + b"    assert value == 9\n"},
        {"tests/test_calc.py": guard + b"    value = square(2)\n" + check + b"    assert value == 4\n"},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert report["needs_clarification"] == []
    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]
    # Passing is decided by the untraced run: the line is no "only failing tests ran it".
    assert report["mutation"]["rows"][0]["rank"] != 4


def test_a_slow_old_test_is_given_time_not_counted_as_a_catch(tmp_path):
    # The old test sleeps; a run against a mutant gets three times the clean
    # run's time plus 10 seconds, so the sleep is no timeout and no "catch".
    slow = WEAK.replace(b"def test_square():\n", b"import time\n\n\ndef test_square():\n    time.sleep(1)\n")
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": slow},
        {"tests/test_calc.py": WEAK},
    )
    report = _prove(repo, base, head, max_mutants=1, timeout=0.1)

    assert report["budgets"]["old"] >= 13
    assert report["mutation"]["categories"] == {"neither": 1}
    assert report["protection_losses"] == []


def test_a_file_a_test_leaves_behind_does_not_reach_the_next_run(tmp_path):
    # Every run starts from the pristine tree: the second run's mkdir succeeds.
    messy = WEAK.replace(b"def test_square():\n", b"import os\n\n\ndef test_square():\n    os.mkdir('out')\n")
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": messy},
        {"tests/test_calc.py": WEAK},
    )
    report = _prove(repo, base, head, max_mutants=2)

    assert report["mutation"]["categories"] == {"neither": 1, "both": 1}
    assert report["protection_losses"] == []


def test_an_interrupted_clean_run_stops_the_comparison(tmp_path):
    stop = b"import pytest\n\n\ndef test_square():\n    pytest.exit('database not available')\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": stop},
        {"tests/test_calc.py": WEAK},
    )
    report = _prove(repo, base, head)

    assert report["status"].startswith("old tests do not run on the new code (pytest exit 2)")
    assert report["compared"] is False
    assert exit_code(report, True) == 2


def test_a_mutant_the_whole_new_suite_catches_is_no_loss(tmp_path):
    other = b"from pkg.calc import square\n\n\ndef test_square_again():\n    assert square(3) == 9\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG, "tests/test_other.py": other},
        {"tests/test_calc.py": WEAK},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    (row,) = report["mutation"]["rows"]
    assert row["category"] == "old only"
    assert row["suite_caught_by"] == ["tests.test_other::test_square_again"]
    assert report["protection_losses"] == []


def test_a_branch_behind_main_is_compared_from_the_merge_base(tmp_path):
    # main strengthened the test after the branch left; the branch changed no test.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": WEAK})
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, {"README.md": b"notes\n"})
    _git(repo, "checkout", "-q", "main")
    _write(repo, {"tests/test_calc.py": STRONG})

    report = prove(str(repo), "main...feature", _opts())
    assert report["from_merge_base"] is True
    assert report["status"] == "nothing to compare: the range changes no test file"

    direct = prove(str(repo), "main..feature", _opts(list_only=True))
    assert any("is not an ancestor of feature" in note for note in direct["notes"])


def test_the_old_tests_run_under_the_old_pytest_configuration(tmp_path):
    # HEAD drops the strong test and deselects it in pytest.ini. Under HEAD's
    # configuration the old test would never run, and the loss would hide.
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE,
         "tests/test_calc.py": STRONG.replace(b"def test_square", b"def test_nine")},
        {"tests/test_calc.py": WEAK,
         "pytest.ini": b"[pytest]\naddopts = --deselect tests/test_calc.py::test_nine\n"},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert "pytest.ini" in report["test_suite_files_changed"]
    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


def test_a_change_to_a_fixture_alone_is_compared_through_the_modules_that_use_it(tmp_path):
    test = b"from pkg.calc import square\n\n\ndef test_square(expected):\n    assert square(3) == expected\n"
    fixture = b"import pytest\n\n\n@pytest.fixture\ndef expected():\n    return 9\n"
    # The new fixture computes the expectation with the code under test.
    tautology = (b"import pytest\n\nfrom pkg.calc import square\n\n\n@pytest.fixture\ndef expected():\n"
                 b"    return square(3)\n")
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": test, "tests/conftest.py": fixture},
        {"tests/conftest.py": tautology},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert report["tests"] == {"old": ["tests/test_calc.py"], "new": ["tests/test_calc.py"]}
    assert any(note.startswith("no test module that existed before and that the old suite runs changed; the "
                               "changed test files (tests/conftest.py) are compared through the test modules "
                               "under tests")
               for note in report["notes"])
    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


def test_a_src_layout_is_found_and_a_typed_import_root_is_normalised(tmp_path):
    repo, base, head = _repo(
        tmp_path,
        {"src/pkg/__init__.py": b"", "src/pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG},
        {"tests/test_calc.py": WEAK},
    )
    found = _prove(repo, base, head, only="src/pkg/calc.py:2:11:AORB:1")
    typed = _prove(repo, base, head, only="src/pkg/calc.py:2:11:AORB:1", import_root="./src/")

    assert found["import_root"] == typed["import_root"] == "src"
    assert _loss_ids(found) == _loss_ids(typed) == ["src/pkg/calc.py:2:11:AORB:1"]


def test_an_old_test_the_new_code_fails_needs_clarification(tmp_path):
    # The product changed on purpose and the test followed it. Whether the old
    # expectation still mattered is a person's call, so it is never a loss.
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": WEAK},
        {"pkg/calc.py": SQUARE.replace(b"x * x", b"x * x + 1"),
         "tests/test_calc.py": WEAK.replace(b"== 4", b"== 5")},
    )
    report = _prove(repo, base, head)

    (item,) = report["needs_clarification"]
    assert item["test"] == "tests/test_calc.py::test_square"
    assert item["reason"] == "this old test fails on the new code: the behaviour changed"
    assert item["also_fails_in_new"] is False
    assert "5 == 4" in item["detail"]
    assert report["protection_losses"] == []
    assert report["status"] == "no old test passes on the new code; nothing to mutate"


def test_an_old_test_whose_new_version_fails_too_is_marked(tmp_path):
    # Both versions of the test fail on the new code: a broken environment or
    # a defect is likelier than a deliberate change, and the report says so.
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG},
        {"pkg/calc.py": SQUARE.replace(b"x * x", b"x * x + 1"),
         "tests/test_calc.py": STRONG + b"\n\ndef test_zero():\n    assert square(0) == 1\n"},
    )
    report = _prove(repo, base, head)

    (item,) = report["needs_clarification"]
    assert item["test"] == "tests/test_calc.py::test_square"
    assert item["reason"] == "this old test fails on the new code, and so does its new version"
    assert item["also_fails_in_new"] is True


def test_an_old_module_that_no_longer_imports_does_not_stop_the_others(tmp_path):
    # pytest stops a whole run on one collection error unless told to go on.
    # Here the change removed `cube` and the test file that used it: that file
    # needs clarification, and the other old test is still compared.
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE + b"\n\ndef cube(x):\n    return x * x * x\n",
         "tests/test_calc.py": STRONG,
         "tests/test_cube.py": b"from pkg.calc import cube\n\n\ndef test_cube():\n    assert cube(2) == 8\n"},
        {"pkg/calc.py": SQUARE, "tests/test_calc.py": WEAK, "tests/test_cube.py": None},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert report["tests"] == {"old": ["tests/test_calc.py", "tests/test_cube.py"], "new": ["tests/test_calc.py"]}
    (item,) = report["needs_clarification"]
    assert item["test"] == "tests/test_cube.py"
    assert item["reason"] == "this old test module cannot be collected on the new code"
    assert "cannot import name 'cube'" in item["detail"]
    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


def test_no_old_test_files_is_said_not_passed(tmp_path):
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE},
        {"tests/test_calc.py": STRONG},
    )
    report = _prove(repo, base, head)

    assert report["tests"] == {"old": [], "new": ["tests/test_calc.py"]}
    assert report["status"] == "nothing to compare: no test module that existed before the change is affected"
    assert report["nothing_to_compare"] is True
    assert report["mutation"] == {}


def test_no_mutants_is_said_not_reported_as_done(tmp_path):
    # The old test executes no product line, so nothing can be compared.
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE,
         "tests/test_calc.py": b"def test_arithmetic():\n    assert 3 * 3 == 9\n"},
        {"tests/test_calc.py": b"def test_arithmetic():\n    assert 2 * 2 == 4\n"},
    )
    report = _prove(repo, base, head)

    assert report["status"].startswith("no mutants: ")
    assert report["compared"] is False
    assert report["mutation"] == {"generated": 0, "run": 0, "skipped_over_cap": 0}


def test_prove_reads_revisions_not_the_working_tree(tmp_path):
    # An uncommitted edit is not part of BASE..HEAD.
    repo, base, head = _weakened(tmp_path)
    (repo / "tests" / "test_calc.py").write_bytes(STRONG)
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


def test_a_bad_revision_is_a_prove_error(tmp_path):
    repo, base, _head = _weakened(tmp_path)
    with pytest.raises(ProveError, match="rev-parse"):
        prove(str(repo), f"{base}..no-such-revision", _opts())


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_a_temporary_directory_behind_a_symlink_is_the_same_tree(tmp_path, monkeypatch):
    # macOS's temporary directory is reached through /var -> /private/var.
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "link"))
    repo, base, head = _weakened(tmp_path)

    assert _loss_ids(_prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")) == ["pkg/calc.py:2:11:AORB:1"]


def test_a_helper_in_a_subdirectory_is_compared_through_its_test_directory(tmp_path):
    # tests/helpers/ holds no test module; the helper's test directory, tests/, does.
    # tests/data/test_sample.py is data the project never collects: it is no old test.
    test = (b"from pkg.calc import square\nfrom tests.helpers.expect import expected\n\n\n"
            b"def test_square():\n    assert square(3) == expected(3)\n")
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/__init__.py": b"", "tests/test_calc.py": test,
         "tests/conftest.py": b"collect_ignore = ['data']\n",
         "tests/data/test_sample.py": b"raise SystemExit('sample data, not a test')\n",
         "tests/helpers/__init__.py": b"", "tests/helpers/expect.py": b"def expected(x):\n    return {3: 9}[x]\n"},
        # The new helper computes the expectation with the code under test.
        {"tests/helpers/expect.py": b"from pkg.calc import square\n\n\ndef expected(x):\n    return square(x)\n"},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert report["tests"] == {"old": ["tests/test_calc.py"], "new": ["tests/test_calc.py"]}
    assert report["needs_clarification"] == []
    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


def test_a_change_that_only_stops_collecting_a_test_is_a_loss(tmp_path):
    # HEAD's conftest excludes test_calc.py from collection: CI no longer runs
    # it, though prove could still name it on the command line.
    other = b"def test_nothing():\n    pass\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG, "tests/test_other.py": other,
         "tests/conftest.py": b"\n"},
        {"tests/conftest.py": b"collect_ignore = ['test_calc.py']\n"},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert report["tests"] == {"old": ["tests/test_calc.py", "tests/test_other.py"], "new": ["tests/test_other.py"]}
    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


def test_a_test_the_new_configuration_ignores_protects_nothing(tmp_path):
    # The module changed and is still there, but HEAD's addopts ignore it. Named
    # on the command line it still runs and catches; CI's run never collects it.
    other = b"def test_nothing():\n    pass\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG, "tests/test_other.py": other},
        {"tests/test_calc.py": STRONG + b"\n", "pytest.ini": b"[pytest]\naddopts = --ignore=tests/test_calc.py\n"},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    (row,) = report["mutation"]["rows"]
    assert row["new_caught_by"] is None
    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


def test_an_old_test_the_old_configuration_excluded_protected_nothing(tmp_path):
    # BASE never collected test_legacy.py, so losing it loses nothing: its
    # catch, made only because prove named it, is not counted.
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/conftest.py": b"collect_ignore = ['test_legacy.py']\n",
         "tests/test_legacy.py": STRONG, "tests/test_calc.py": WEAK.replace(b"def test_square", b"def test_small")},
        {"tests/test_legacy.py": WEAK},
    )
    report = _prove(repo, base, head)

    assert report["status"].startswith("nothing to compare: the old suite")
    assert report["protection_losses"] == []


def test_tests_under_a_configuration_of_their_own_are_said_not_compared(tmp_path, monkeypatch):
    # proj/sub/pytest.ini collects check_* functions; a pytest run at the
    # repository root, which prove takes as the suite, does not. The long-list
    # path still finds that configuration for the files themselves.
    monkeypatch.setattr("checkwash_prove.engine.ARGS_LIMIT", 1)
    check = b"from pkg.calc import square\n\n\ndef check_square():\n    assert square(3) == 9\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "proj/sub/pytest.ini": b"[pytest]\npython_functions = check_*\n",
         "proj/sub/test_calc.py": check},
        {"proj/sub/test_calc.py": check.replace(b"(3) == 9", b"(2) == 4")},
    )
    report = _prove(repo, base, head)

    assert report["status"].startswith("not compared: pytest runs the changed old tests under a configuration file")
    assert exit_code(report, True) == 2


def test_cache_options_in_the_projects_addopts_still_parse(tmp_path):
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG,
         "pytest.ini": b"[pytest]\naddopts = --ff\n"},
        {"tests/test_calc.py": WEAK},
    )
    assert _loss_ids(_prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")) == ["pkg/calc.py:2:11:AORB:1"]


def test_configuration_is_found_from_the_common_ancestor_as_pytest_does(tmp_path):
    # The root pytest.ini collects check_* functions; aaa/pytest.ini is nearer
    # to one of the two changed modules but not to their common ancestor.
    check = b"from pkg.calc import square\n\n\ndef check_square():\n    assert square(3) == 9\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "pytest.ini": b"[pytest]\npython_functions = check_*\n",
         "aaa/pytest.ini": b"[pytest]\n", "aaa/test_other.py": check.replace(b"check_square", b"check_other"),
         "tests/test_calc.py": check},
        {"aaa/test_other.py": check.replace(b"check_square", b"check_other").replace(b"(3) == 9", b"(2) == 4"),
         "tests/test_calc.py": check.replace(b"(3) == 9", b"(2) == 4")},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert report["needs_clarification"] == []
    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


def test_files_above_the_tree_are_never_pytest_configuration(tmp_path, monkeypatch):
    # The temporary directory's parent is shared (/tmp on Linux): a conftest.py
    # or pytest.ini planted there must not run or steer the tests.
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "conftest.py").write_text("raise RuntimeError('planted conftest ran')\n", encoding="utf-8")
    (shared / "pytest.ini").write_text("[pytest]\naddopts = --deselect tests/test_calc.py::test_square\n",
                                       encoding="utf-8")
    monkeypatch.setattr("tempfile.tempdir", str(shared))
    repo, base, head = _weakened(tmp_path)

    assert _loss_ids(_prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")) == ["pkg/calc.py:2:11:AORB:1"]


def test_options_in_the_callers_environment_do_not_reach_the_tests(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "--deselect tests/test_calc.py::test_square")
    repo, base, head = _weakened(tmp_path)

    assert _loss_ids(_prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")) == ["pkg/calc.py:2:11:AORB:1"]


def test_a_project_that_turns_xdist_off_still_runs(tmp_path):
    # -n 0 is passed only when the project's own addopts distribute the run.
    ini = {"pytest.ini": b"[pytest]\naddopts = -p no:xdist\n"}
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG, **ini},
        {"tests/test_calc.py": WEAK},
    )
    assert _loss_ids(_prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")) == ["pkg/calc.py:2:11:AORB:1"]


def _alive(pid):
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True).stdout
        return str(pid).encode() in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_processes_a_test_leaves_running_end_with_its_run(tmp_path, monkeypatch):
    # Each run's test starts a process that holds a file in the tree and
    # sleeps. It must end with the run: on Windows a held file would stop the
    # next reset, and anywhere it would outlive prove.
    pids = tmp_path / "pids.txt"
    monkeypatch.setenv("PROVE_TEST_PIDS", str(pids))
    spawn = (b"import os\nimport subprocess\nimport sys\n\nfrom pkg.calc import square\n\n\ndef test_square():\n"
             b"    child = subprocess.Popen([sys.executable, '-c', \"import time; f = open('held.log', 'w'); "
             b"time.sleep(60)\"])\n    with open(os.environ['PROVE_TEST_PIDS'], 'a') as fh:\n"
             b"        fh.write(f'{child.pid}\\n')\n")
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": spawn + b"    assert square(3) == 9\n"},
        {"tests/test_calc.py": spawn + b"    assert square(2) == 4\n"},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]
    started = [int(line) for line in pids.read_text(encoding="utf-8").split()]
    assert len(started) >= 6
    assert [pid for pid in started if _alive(pid)] == []


def test_a_submodule_is_left_out_with_a_note(tmp_path):
    repo, base, head = _weakened(tmp_path)
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},tests/vendored")
    _git(repo, "commit", "-q", "-m", "add a submodule pointer")
    report = prove(str(repo), f"{base}..HEAD", _opts(only="pkg/calc.py:2:11:AORB:1"))

    assert "submodules are not compared: tests/vendored" in report["notes"]
    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


def test_a_long_list_of_test_files_still_runs_exactly_those_files(tmp_path, monkeypatch):
    # Past the command-line limit the files go through an allow-list. They are
    # collected as if named on the command line: a directory pytest would not
    # recurse into by default (build) is no reason to skip one.
    monkeypatch.setattr("checkwash_prove.engine.ARGS_LIMIT", 1)
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/build/test_calc.py": STRONG},
        {"tests/build/test_calc.py": WEAK},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    # The old module ran (no "old tests do not run"); but a pytest run at the
    # root never collects build/, so it protected nothing in the suite.
    assert report["needs_clarification"] == []
    assert report["status"].startswith("nothing to compare: the old suite")
    assert report["protection_losses"] == []


def test_a_bare_repository_works(tmp_path):
    repo, base, head = _weakened(tmp_path)
    bare = tmp_path / "bare.git"
    _git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))

    assert _loss_ids(prove(str(bare), f"{base}..{head}", _opts(only="pkg/calc.py:2:11:AORB:1"))) == [
        "pkg/calc.py:2:11:AORB:1"]


@pytest.mark.skipif(os.name != "nt", reason="needs a case-insensitive file system")
def test_an_import_root_typed_in_another_case_is_the_directory(tmp_path):
    repo, base, head = _repo(
        tmp_path,
        {"src/pkg/__init__.py": b"", "src/pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG},
        {"tests/test_calc.py": WEAK},
    )
    report = _prove(repo, base, head, only="src/pkg/calc.py:2:11:AORB:1", import_root="SRC")

    assert report["import_root"] == "src"
    assert _loss_ids(report) == ["src/pkg/calc.py:2:11:AORB:1"]


def test_side_by_side_runs_carry_a_note(tmp_path):
    repo, base, head = _weakened(tmp_path)
    ran = _prove(repo, base, head, max_mutants=2, workers=2)
    listed = _prove(repo, base, head, list_only=True, workers=2)

    assert any(note.startswith("mutants ran side by side (--workers 2)") for note in ran["notes"])
    assert not any(note.startswith("mutants ran side by side") for note in listed["notes"])


class _ScriptedRunner:
    def __init__(self, results):
        self.results = list(results)

    def pytest(self, *args, **kwargs):
        return self.results.pop(0)


class _StillTree:
    path = pathlib.Path(".")

    def reset(self, mutant=None):
        pass


CAUGHT = {("tests.test_calc", "test_square"): "failed"}
BOTH_PASSED = {("tests.test_calc", "test_square"): "passed", ("tests.test_calc", "test_other"): "passed"}
OTHER_CAUGHT = {("tests.test_calc", "test_square"): "passed", ("tests.test_calc", "test_other"): "failed"}


@pytest.mark.parametrize(("runs", "category", "confirmation"), [
    # Clean old, new and whole-suite references, then the mutant's three runs.
    ([PASSED, PASSED, PASSED, CAUGHT, PASSED, PASSED], "loss", "repeated"),
    ([PASSED, PASSED, PASSED, PASSED], "unconfirmed loss", "the old tests did not catch it again"),
    ([PASSED, PASSED, PASSED, CAUGHT, CAUGHT], "unconfirmed loss", "the new tests caught it when run alone"),
    ([PASSED, PASSED, PASSED, CAUGHT, PASSED, CAUGHT], "unconfirmed loss",
     "the whole new suite caught it when run alone"),
    # Another old test catches it the second time: a flaky pair, not the same catch.
    ([BOTH_PASSED, PASSED, PASSED, OTHER_CAUGHT], "unconfirmed loss",
     "other old tests caught it the second time: tests.test_calc::test_other"),
])
def test_a_loss_is_kept_only_when_it_repeats_alone(runs, category, confirmation):
    from checkwash_prove.engine import _confirm

    results = [RunResult(0 if outcomes is PASSED else 1, outcomes, {}) for outcomes in runs]
    row = {"category": "loss", "old_caught_by": ["tests.test_calc::test_square"]}
    _confirm(_ScriptedRunner(results), [row], [object()], _StillTree(), _StillTree(), ["t"], ["t"], _opts(),
             {"old": 1, "new": 1, "suite": 1})

    assert (row["category"], row["confirmation"]) == (category, confirmation)


# ---- review round 1 of the satellite: what counts as caught ---------------------------------------------------------

# A loop the old tests run: SDL on `i += 1` makes it hang.
LOOP = (b"def square(x):\n    return x * x\n\n\ndef total(n):\n    i = 0\n    s = 0\n    while i < n:\n        s += i\n"
        b"        i += 1\n    return s\n")
HANG = "pkg/calc.py:10:8:SDL:1"
TOTAL = b"from pkg.calc import total\n\n\ndef test_total():\n    assert total(4) == 6\n"


@pytest.fixture
def short_budgets(monkeypatch):
    # A hang ends at its budget: three times the clean run plus the margin. A short margin keeps these tests quick.
    monkeypatch.setattr("checkwash_prove.engine.TIMEOUT_MARGIN", 2.0)


def test_a_module_the_old_suite_excludes_is_not_run_at_all(tmp_path):
    # BASE's own run never collected test_legacy.py, so it protected nothing in CI. Named on the command line it would
    # run, and its hang on the loop's mutant read as a catch; it is left out, with its lines and its mutants.
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": LOOP, "tests/conftest.py": b"collect_ignore = ['test_legacy.py']\n",
         "tests/test_legacy.py": TOTAL, "tests/test_calc.py": STRONG},
        {"tests/test_legacy.py": None, "tests/test_calc.py": WEAK},
    )
    report = _prove(repo, base, head, list_only=True)

    assert report["tests"] == {"old": ["tests/test_calc.py"], "new": ["tests/test_calc.py"]}
    assert any("not run, since they protect nothing in CI: tests/test_legacy.py" in n for n in report["notes"])
    assert HANG not in [c["id"] for c in report["mutation"]["candidates"]]


def test_a_conftest_of_an_excluded_directory_never_loads(tmp_path):
    # tests/legacy is excluded; its conftest.py runs the product at import. Had prove named the legacy module, every
    # mutant that breaks that import would have been a catch the project's CI never makes.
    legacy_conftest = b"from pkg.calc import square\n\nassert square(3) == 9\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/conftest.py": b"collect_ignore = ['legacy']\n",
         "tests/legacy/conftest.py": legacy_conftest, "tests/legacy/test_legacy.py": b"def test_old():\n    pass\n",
         "tests/test_calc.py": STRONG.replace(b"square(3) == 9", b"square(1) == 1")},
        {"tests/legacy/test_legacy.py": None, "tests/test_calc.py": STRONG.replace(b"square(3) == 9", b"square(1) > 0")},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:2")

    (row,) = report["mutation"]["rows"]
    assert row["old_caught_by"] == ["tests.test_calc::test_square"]
    assert report["tests"]["old"] == ["tests/test_calc.py"]


def test_a_hang_in_an_old_test_that_fails_on_the_new_code_is_no_catch(tmp_path, short_budgets):
    # test_total fails on the new code (needs clarification): its hanging on a mutant catches nothing.
    old = STRONG + b"\n\ndef test_total():\n    from pkg.calc import total\n    assert total(4) == 6\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": LOOP, "tests/test_calc.py": old},
        {"pkg/calc.py": LOOP.replace(b"while i < n", b"while i <= n"), "tests/test_calc.py": STRONG},
    )
    report = _prove(repo, base, head, only=HANG, timeout=1)

    assert [c["test"] for c in report["needs_clarification"]] == ["tests/test_calc.py::test_total"]
    (row,) = report["mutation"]["rows"]
    assert row["old_caught_by"] is None
    assert report["protection_losses"] == []


def test_a_hang_in_a_test_the_new_suite_ignores_does_not_hide_a_loss(tmp_path, short_budgets):
    # HEAD's addopts ignore tests/test_calc.py. Named on the command line it still runs and hangs; CI's run never
    # collects it, so the new suite does not catch the mutant.
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": LOOP, "tests/test_calc.py": TOTAL,
         "tests/test_other.py": b"def test_nothing():\n    pass\n"},
        {"tests/test_calc.py": TOTAL + b"\n", "pytest.ini": b"[pytest]\naddopts = --ignore=tests/test_calc.py\n"},
    )
    report = _prove(repo, base, head, only=HANG, timeout=1)

    (row,) = report["mutation"]["rows"]
    assert row["old_caught_by"] == ["tests.test_calc::test_total (timeout)"]
    assert row["new_caught_by"] is None
    assert _loss_ids(report) == [HANG]


def test_a_failure_under_a_parameter_id_the_mutant_changed_still_catches(tmp_path):
    # The parameter comes from the product code, so the mutant renames the test: test_limit[6] becomes
    # test_limit[5]. The clean run passed its one instance, so the failure counts.
    product = b"def limit(base):\n    return base * 3\n\n\nLIMIT = limit(2)\n"
    test = (b"import pytest\n\nfrom pkg.calc import LIMIT\n\n\n@pytest.mark.parametrize('n', [LIMIT])\n"
            b"def test_limit(n):\n    assert n == 6\n")
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": product, "tests/test_calc.py": test},
        {"tests/test_calc.py": test.replace(b"n == 6", b"n > 0")},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    (loss,) = report["protection_losses"]
    assert loss["old_caught_by"] == ["tests/test_calc.py::test_limit[5]"]


def test_exitfirst_in_the_projects_options_does_not_hide_later_catches(tmp_path):
    # With -x the old run would stop at test_label, which fails on the new code, before test_square catches.
    product = b"def double(x):\n    return x * 2\n\n\ndef label():\n    return 'old'\n\n\ndef square(x):\n    return x * x\n"
    test = (b"from pkg.calc import double, label, square\n\n\ndef test_double():\n    assert double(2) == 4\n\n\n"
            b"def test_label():\n    assert label() == 'old'\n\n\ndef test_square():\n    assert square(3) == 9\n")
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": product, "tests/test_calc.py": test,
         "pytest.ini": b"[pytest]\naddopts = -x\n"},
        {"pkg/calc.py": product.replace(b"'old'", b"'new'"),
         "tests/test_calc.py": test.replace(b"square(3) == 9", b"square(2) == 4").replace(b"'old'", b"'new'")},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:10:11:AORB:1")

    assert [c["test"] for c in report["needs_clarification"]] == ["tests/test_calc.py::test_label"]
    assert _loss_ids(report) == ["pkg/calc.py:10:11:AORB:1"]


def test_a_junit_prefix_in_the_projects_options_changes_nothing(tmp_path):
    ini = b"[pytest]\naddopts = --junit-prefix=px\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG, "pytest.ini": ini},
        {"tests/test_calc.py": WEAK},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert report["status"] == "done"
    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


@pytest.mark.parametrize("option", ["--sw", "--stepwise-skip", "--sw-reset"])
def test_stepwise_in_the_projects_options_is_refused(tmp_path, option):
    # --stepwise-skip and --sw-reset turn stepwise on as well.
    ini = f"[pytest]\naddopts = {option}\n".encode()
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG, "pytest.ini": ini},
        {"tests/test_calc.py": WEAK},
    )
    with pytest.raises(ProveError, match="--stepwise"):
        _prove(repo, base, head)


@pytest.mark.parametrize(("options", "found"), [
    ("-n 4", True), ("-n4", True), ("-nauto", True), ("-nlogical", True), ("-n auto", True),
    ("--numprocesses=2", True), ("--dist loadfile", True), ("--tx 2*popen", True), ("-q --strict-markers", False),
    ("-vn2", True), ("-qn auto", True), ("-xn2", True),
    ("-p no:xdist", False), ("--no-header", False), ("-knot_slow", False), ("-m network", False),
])
def test_every_spelling_of_xdists_options_is_seen(options, found):
    assert (_XDIST_OPTION.search(options) is not None) is found


def test_test_files_are_the_ones_the_projects_python_files_names(tmp_path):
    # pytest-django's style: test modules are tests.py.
    ini = b"[pytest]\npython_files = tests.py test_*.py\n"
    test = STRONG.replace(b"pkg.calc", b"myapp.calc")
    repo, base, head = _repo(
        tmp_path,
        {"myapp/__init__.py": b"", "myapp/calc.py": SQUARE, "myapp/tests.py": test, "pytest.ini": ini},
        {"myapp/tests.py": test.replace(b"square(3) == 9", b"square(2) == 4")},
    )
    report = _prove(repo, base, head, only="myapp/calc.py:2:11:AORB:1")

    assert report["test_file_naming"] == [{"python_files": ["tests.py", "test_*.py"], "testpaths": []}]
    assert report["tests"] == {"old": ["myapp/tests.py"], "new": ["myapp/tests.py"]}
    assert _loss_ids(report) == ["myapp/calc.py:2:11:AORB:1"]


def test_a_broad_python_files_reaches_only_the_testpaths(tmp_path):
    # `python_files = *.py` would name the product modules too; pytest's own run looks only under testpaths.
    ini = b"[pytest]\npython_files = *.py\ntestpaths = tests\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/check_calc.py": STRONG, "pytest.ini": ini},
        {"tests/check_calc.py": WEAK, "pkg/calc.py": SQUARE + b"\n"},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert report["product_files_changed"] == ["pkg/calc.py"]
    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


def test_python_files_set_at_one_revision_add_to_the_default_of_the_other(tmp_path):
    repo, base, head = _repo(tmp_path, {"a.txt": b"a"},
                             {"pytest.ini": b"[pytest]\npython_files = tests.py\ntestpaths = app\n",
                              "app/tests.py": b""})
    assert naming_rules(str(repo), (base, head)) == (Naming(), Naming(("tests.py",), ("app",)))
    assert naming_rules(str(repo), (base,)) == (Naming(),)
    rules = naming_rules(str(repo), (base, head))
    assert is_test_module("app/tests.py", rules) and is_test_module("lib/test_x.py", rules)
    assert not is_test_module("lib/tests.py", rules)


def test_a_helper_change_is_compared_even_when_a_test_module_is_added(tmp_path):
    # The weakened helper serves the unchanged test_calc.py; adding test_new.py must not turn that into "nothing to
    # compare".
    test = b"from helpers import expected\nfrom pkg.calc import square\n\n\ndef test_square():\n    assert square(3) == expected()\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": test,
         "tests/helpers.py": b"def expected():\n    return 9\n"},
        {"tests/helpers.py": b"def expected():\n    from pkg.calc import square\n    return square(3)\n",
         "tests/test_new.py": b"def test_new():\n    pass\n"},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert report["tests"] == {"old": ["tests/test_calc.py"], "new": ["tests/test_calc.py", "tests/test_new.py"]}
    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


def test_a_package_with_a_pyproject_of_its_own_is_compared(tmp_path):
    # pytest runs packages/foo's tests with packages/foo as rootdir (its pyproject.toml), and the root run with the
    # repository root: the same tests, named from two places.
    test = STRONG.replace(b"pkg.calc", b"foo.calc")
    repo, base, head = _repo(
        tmp_path,
        {"packages/foo/pyproject.toml": b"[project]\nname = 'foo'\n", "packages/foo/src/foo/__init__.py": b"",
         "packages/foo/src/foo/calc.py": SQUARE, "packages/foo/tests/test_calc.py": test},
        {"packages/foo/tests/test_calc.py": test.replace(b"square(3) == 9", b"square(2) == 4")},
    )
    report = _prove(repo, base, head, import_root="packages/foo/src",
                    only="packages/foo/src/foo/calc.py:2:11:AORB:1")

    assert report["status"] == "done"
    assert _loss_ids(report) == ["packages/foo/src/foo/calc.py:2:11:AORB:1"]


# ---- review round 1 of the satellite: the trees, the command and its output -------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_a_link_a_test_leaves_is_removed_without_touching_what_it_points_at(tmp_path):
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "dir" / "keep.txt").write_text("keep")
    (outside / "file.sh").write_text("echo keep")
    os.chmod(outside / "dir", 0o755)
    os.chmod(outside / "file.sh", 0o755)
    linker = (f"import os\n\n\ndef test_links():\n    if not os.path.lexists('dir_link'):\n"
              f"        os.symlink({str(outside / 'dir')!r}, 'dir_link')\n"
              f"        os.symlink({str(outside / 'file.sh')!r}, 'file_link')\n").encode()
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG, "tests/test_links.py": linker},
        {"tests/test_calc.py": WEAK},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]
    assert oct(os.stat(outside / "dir").st_mode & 0o777) == oct(0o755)
    assert oct(os.stat(outside / "file.sh").st_mode & 0o777) == oct(0o755)
    assert (outside / "dir" / "keep.txt").read_text() == "keep"


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows'")
def test_a_junction_a_test_leaves_is_removed_without_emptying_its_target(tmp_path):
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "a.txt").write_text("keep")
    (outside / "sub" / "b.txt").write_text("keep")
    linker = (f"import _winapi\nimport os\n\n\ndef test_junction():\n    if not os.path.exists('junc'):\n"
              f"        _winapi.CreateJunction({str(outside)!r}, 'junc')\n").encode()
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG, "tests/test_junction.py": linker},
        {"tests/test_calc.py": WEAK},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]
    assert (outside / "a.txt").read_text() == "keep"
    assert (outside / "sub" / "b.txt").read_text() == "keep"


def test_the_repository_root_is_found_wherever_git_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", tmp_path.as_posix())
    repo, base, head = _weakened(tmp_path)
    assert repository_root(str(repo)) == (str(repo), "")
    top, prefix = repository_root(str(repo / "tests"))
    assert prefix == "tests/" and os.path.samefile(top, repo)
    assert repository_root(str(repo / ".git")) == (str(repo / ".git"), "")
    bare = tmp_path / "bare.git"
    _git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    assert repository_root(str(bare)) == (str(bare), "")
    (tmp_path / "plain").mkdir()
    with pytest.raises(GitError):
        repository_root(str(tmp_path / "plain"))


def test_an_error_before_git_is_asked_still_exits_2(monkeypatch, capsys):
    # The engine's import, a damaged catalogue's included, happens inside the guard.
    def broken(spec):
        raise ImportError("damaged")

    monkeypatch.setattr("checkwash_prove.cli.range_usage_error", broken)
    assert main(["HEAD~1..HEAD", "--fail-on-loss"]) == 2
    assert "checkwash-prove engine error: ImportError: damaged" in capsys.readouterr().err


def test_the_catalogue_is_not_imported_until_its_hash_is_checked():
    code = "import sys, checkwash_prove.cli, checkwash_prove.engine\nprint('checkwash_prove.mutops_v05m2' in sys.modules)\n"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "False"


def test_standard_error_lines_never_reach_standard_output(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stderr", None)
    assert main(["HEAD"]) == 2
    assert exit_code({"protection_losses": [], "status": "no mutants: x"}, True) == 2
    assert capsys.readouterr().out == ""


def test_standard_error_escapes_what_the_repository_controls(capsys):
    status = "old tests do not run on the new code (pytest exit 2): \x1b]0;TITLE\x07\nE   boom"
    assert exit_code({"protection_losses": [], "status": status}, True) == 2
    err = capsys.readouterr().err
    assert "\x1b" not in err and "\x07" not in err
    assert "\\u001b]0;TITLE\\u0007 | E   boom" in err


def test_the_text_report_keeps_joiners_and_marks_continuation_lines():
    report = {
        "catalogue": {"version": CATALOGUE_VERSION}, "base": "a" * 40, "head": "b" * 40, "from_merge_base": False,
        "tests": {"old": [], "new": []}, "status": "old tests do not run on the new code (pytest exit 2): E   one\nE   two",
        "needs_clarification": [{"test": "tests/test_x.py::test_\U0001f468\u200d\U0001f469", "reason": "r",
                                 "detail": ""}],
        "protection_losses": [], "unconfirmed_losses": [], "mutation": {}, "notes": ["n\U000e0041"],
    }
    lines = render(report).splitlines()

    assert "status: old tests do not run on the new code (pytest exit 2): E   one" in lines
    assert "  | E   two" in lines
    assert "  - tests/test_x.py::test_\U0001f468\u200d\U0001f469: r" in lines
    assert "note: n\\U000e0041" in lines


def test_python_dash_m_of_either_module_returns_mains_exit_code():
    for module in ("checkwash_prove", "checkwash_prove.cli"):
        proc = subprocess.run([sys.executable, "-m", module, "HEAD"], capture_output=True, text=True)
        assert proc.returncode == 2, module
        assert "needs a range BASE..HEAD or BASE...HEAD" in proc.stderr


def test_no_module_of_the_package_imports_checkwash():
    # Imports inside functions too, which loading the modules would not show.
    root = pathlib.Path(checkwash_prove.__file__).parent
    found = []
    for path in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names = [node.value] if re.fullmatch(r"checkwash(\.[\w.]+)?", node.value) else []  # import_module()
            else:
                names = []
            found += [f"{path.name}: {n}" for n in names if n == "checkwash" or n.startswith("checkwash.")]
    assert found == []


def test_a_subdirectory_of_a_src_layout_runs_from_the_root(tmp_path, capsys):
    # Left in the subdirectory, ls-tree would see no src/ and the import root, the test files and the changed lines
    # would all be wrong.
    test = STRONG
    repo, base, head = _repo(
        tmp_path,
        {"src/pkg/__init__.py": b"", "src/pkg/calc.py": SQUARE, "tests/test_calc.py": test},
        {"tests/test_calc.py": WEAK},
    )
    code = main([f"{base}..{head}", "--repo", str(repo / "src" / "pkg"), "--format", "json",
                 "--only", "src/pkg/calc.py:2:11:AORB:1", "--fail-on-loss"])
    captured = capsys.readouterr()

    assert code == 1
    (loss,) = json.loads(captured.out)["protection_losses"]
    assert loss["id"] == "src/pkg/calc.py:2:11:AORB:1"
    assert f"--repo {_quote_path(os.path.realpath(repo))} " in loss["replay"]
    assert "not its subdirectory src/pkg/" in captured.err


def _quote_path(path):
    return subprocess.list2cmdline([path]) if os.name == "nt" else shlex.quote(path)


# ---- review round 2 of the satellite ---------------------------------------------------------------------------

def test_test_files_one_run_cannot_name_together_are_not_compared(tmp_path):
    # packages/foo's pyproject.toml makes it pytest's rootdir for a run that names its test with tests/test_b.py, and
    # pytest then names test_b's tests by their names alone: nothing of them could be matched.
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_b.py": STRONG,
         "packages/foo/pyproject.toml": b"[project]\nname = 'foo'\n",
         "packages/foo/tests/test_a.py": b"def test_a():\n    pass\n"},
        {"tests/test_b.py": WEAK, "packages/foo/tests/test_a.py": b"def test_a():\n    assert True\n"},
    )
    report = _prove(repo, base, head)

    assert report["status"].startswith("not compared: pytest would run the changed old test files from packages/foo")
    assert exit_code(report, True) == 2


def test_a_module_skipped_as_a_whole_in_a_run_of_some_files_is_no_catch(tmp_path):
    # The mutant makes enabled() false, so the old module skips at module level and its run exits 5. The project's
    # whole suite runs test_other.py too and passes: no catch, so no loss.
    product = b"LEVEL = 3\n\n\ndef enabled():\n    return LEVEL >= 2\n"
    old = (b"import pytest\n\nfrom pkg import calc\n\nif not calc.enabled():\n"
           b"    pytest.skip('disabled', allow_module_level=True)\n\n\ndef test_level():\n    assert calc.LEVEL == 3\n")
    new = b"from pkg import calc\n\n\ndef test_level():\n    assert calc.LEVEL == 3\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": product, "tests/test_calc.py": old,
         "tests/test_other.py": b"def test_other():\n    pass\n"},
        {"tests/test_calc.py": new},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:5:11:ROR:2")

    (row,) = report["mutation"]["rows"]
    assert row["old_caught_by"] is None
    assert report["protection_losses"] == []


STOP = b"def square(x):\n    return x * x\n\n\ndef stop(n):\n    i = 0\n    while i < n:\n        i += 1\n    return i\n"
SERVER = (b"import pytest\n\nfrom pkg.calc import stop\n\n\n@pytest.fixture(scope='session')\ndef server():\n"
          b"    yield\n    stop(3)\n")


def test_a_hang_in_a_session_fixtures_teardown_is_no_loss(tmp_path, short_budgets):
    # A hang in the session fixture's teardown belongs to no one test. For the old tests it is the last test's
    # (test_square, which passes on the clean code: a catch). The new test_square does not use the fixture; in the
    # whole new suite the last test is test_known_bug, which already fails, and there the hang is the run's: the suite
    # catches the mutant, so there is no loss.
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": STOP, "tests/conftest.py": SERVER,
         "tests/test_calc.py": STRONG.replace(b"test_square()", b"test_square(server)"),
         "tests/test_zzz.py": b"def test_known_bug(server):\n    assert False\n"},
        {"tests/test_calc.py": WEAK},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:8:8:SDL:1", timeout=1)

    (row,) = report["mutation"]["rows"]
    assert row["old_caught_by"] == ["tests.test_calc::test_square (timeout)"]
    assert row["new_caught_by"] is None
    assert row["suite_caught_by"] == ["(timeout)"]
    assert report["protection_losses"] == []


def test_a_long_list_of_test_files_goes_through_the_allow_list(tmp_path, monkeypatch):
    # Past ARGS_LIMIT the files go to the plugin, which collects exactly them; the comparison is the same.
    monkeypatch.setattr("checkwash_prove.engine.ARGS_LIMIT", 1)
    repo, base, head = _weakened(tmp_path)
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


def test_a_catch_is_shown_as_a_node_id_with_what_happened(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("")
    assert _shown(tmp_path, None, "tests.test_calc::test_total (timeout)") == "tests/test_calc.py::test_total (timeout)"
    assert _shown(tmp_path, None, "tests.test_calc (pytest exit 4)") == "tests/test_calc.py (pytest exit 4)"
    assert _shown(tmp_path, None, "tests.test_calc::test_x[a (b)]") == "tests/test_calc.py::test_x[a (b)]"
    assert _shown(tmp_path, None, "(timeout)") == "(timeout)"


def test_a_damaged_escaping_module_still_gives_exit_2(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "checkwash_prove._text", None)
    assert main(["HEAD"]) == 2
    assert "needs a range BASE..HEAD" in capsys.readouterr().err


def test_a_standard_error_that_cannot_be_written_is_dropped(monkeypatch):
    class Broken(io.StringIO):
        def write(self, text):
            raise BrokenPipeError

    monkeypatch.setattr(sys, "stderr", Broken())
    assert main(["HEAD"]) == 2
    assert sys.stderr is None


def _pristine(tmp_path):
    pristine = tmp_path / "pristine"
    (pristine / "data").mkdir(parents=True)
    (pristine / "data" / "a.txt").write_text("inside")
    (pristine / "conf.txt").write_text("inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("outside")
    return pristine, outside


def test_a_reset_puts_a_files_mode_back(tmp_path):
    pristine, _ = _pristine(tmp_path)
    tree = Tree(pristine, tmp_path / "tree")
    os.chmod(tree.path / "conf.txt", 0o444)
    tree.reset()

    assert os.access(tree.path / "conf.txt", os.W_OK)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_a_reset_never_writes_through_a_link_in_an_unwritable_directory(tmp_path):
    pristine, outside = _pristine(tmp_path)
    tree = Tree(pristine, tmp_path / "tree")
    shutil.rmtree(tree.path / "data")
    os.symlink(outside, tree.path / "data")
    os.chmod(tree.path, 0o555)
    try:
        tree.reset()
    finally:
        os.chmod(tree.path, 0o755)

    assert (outside / "a.txt").read_text() == "outside"
    assert sorted(p.name for p in outside.iterdir()) == ["a.txt"]
    assert not os.path.islink(tree.path / "data") and (tree.path / "data" / "a.txt").read_text() == "inside"


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows'")
def test_a_reset_never_writes_through_a_read_only_junction(tmp_path):
    import _winapi
    import stat as stat_

    pristine, outside = _pristine(tmp_path)
    tree = Tree(pristine, tmp_path / "tree")
    shutil.rmtree(tree.path / "data")
    _winapi.CreateJunction(str(outside), str(tree.path / "data"))
    os.chmod(tree.path / "data", stat_.S_IREAD)
    tree.reset()

    assert (outside / "a.txt").read_text() == "outside"
    assert sorted(p.name for p in outside.iterdir()) == ["a.txt"]
    assert (tree.path / "data" / "a.txt").read_text() == "inside"


# ---- review round 3, before the first release: what counts, and what reaches outside -------------------------------

def test_an_old_directory_whose_conftest_no_longer_imports_is_said_not_to_run(tmp_path):
    # BASE's tests/unit/conftest.py imports a name HEAD renamed. That directory is the suite's all the same: the old
    # tests do not run on the new code, which is said as such, never "protected nothing".
    calc = SQUARE + b"\n\ndef old_name():\n    return 1\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": calc, "tests/unit/conftest.py": b"from pkg.calc import old_name\n",
         "tests/unit/test_calc.py": STRONG},
        {"pkg/calc.py": calc.replace(b"old_name", b"new_name"),
         "tests/unit/conftest.py": b"from pkg.calc import new_name\n", "tests/unit/test_calc.py": WEAK},
    )
    report = _prove(repo, base, head)

    assert report["tests"]["old"] == ["tests/unit/test_calc.py"]
    assert report["status"].startswith("old tests do not run on the new code")
    assert not report["nothing_to_compare"] and report["protection_losses"] == []
    assert exit_code(report, True) == 2


def test_a_new_directory_whose_conftest_does_not_import_makes_no_loss(tmp_path):
    # HEAD's tests/unit/conftest.py imports a name HEAD lacks, so the new tests do not run. Counted as "not collected",
    # every mutant the old tests catch would have been a loss.
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/unit/conftest.py": b"from pkg.calc import square\n",
         "tests/unit/test_calc.py": STRONG},
        {"tests/unit/conftest.py": b"from pkg.calc import missing\n", "tests/unit/test_calc.py": STRONG + b"\n"},
    )
    report = _prove(repo, base, head)

    assert report["status"].startswith("new tests do not run on the new code")
    assert report["protection_losses"] == []
    assert exit_code(report, True) == 2


def test_a_test_file_that_skips_itself_while_collected_is_part_of_the_suite(tmp_path):
    skip = b"import pytest\n\npytest.skip('not here', allow_module_level=True)\n\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": skip + STRONG},
        {"tests/test_calc.py": skip + WEAK},
    )
    report = _prove(repo, base, head)

    assert report["tests"]["old"] == ["tests/test_calc.py"]
    assert not report["nothing_to_compare"]
    assert exit_code(report, True) == 2


OWN_TEARDOWN = (b"import pytest\n\nfrom pkg.calc import square, stop\n\n\n@pytest.fixture{scope}\ndef res():\n"
                b"    yield\n    stop(3)\n\n\ndef test_square():\n    assert square(3) == 9\n\n\n"
                b"def test_old_behaviour(res):\n    assert square(2) == 5\n")


@pytest.mark.parametrize("scope", [b"", b"(scope='module')"])
def test_a_hang_in_a_teardown_after_an_old_test_that_fails_is_no_catch(tmp_path, short_budgets, scope):
    # test_old_behaviour fails on the new code; its fixture's teardown hangs on the mutant. Function-scoped, the hang is
    # that test's; module-scoped, it is the last test's for the old tests. Either way it catches nothing.
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": STOP, "tests/test_calc.py": OWN_TEARDOWN.replace(b"{scope}", scope)},
        {"tests/test_calc.py": STRONG},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:8:8:SDL:1", timeout=1)

    assert [c["test"] for c in report["needs_clarification"]] == ["tests/test_calc.py::test_old_behaviour"]
    (row,) = report["mutation"]["rows"]
    assert row["old_caught_by"] is None
    assert report["protection_losses"] == []


LABEL = SQUARE + b"\n\ndef label(v):\n    return 'n%d' % v\n"


def test_a_test_class_whose_collection_the_mutant_breaks_is_a_catch(tmp_path):
    # The old class takes its parameter ids from the product code; the mutant makes them fail, so the class does not
    # collect and CI fails. The new class does not use them.
    old = (b"import pytest\n\nfrom pkg.calc import label, square\n\n\nclass TestSquare:\n"
           b"    @pytest.mark.parametrize('v', [3], ids=label)\n    def test_square(self, v):\n"
           b"        assert square(v) == 9\n")
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": LABEL, "tests/test_calc.py": old},
        {"tests/test_calc.py": old.replace(b", ids=label", b"").replace(b"== 9", b"> 0")},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:6:11:AORB:1")

    (row,) = report["mutation"]["rows"]
    assert row["old_caught_by"] == ["tests.test_calc::TestSquare"]
    assert _loss_ids(report) == ["pkg/calc.py:6:11:AORB:1"]


def test_testpaths_that_match_nothing_leave_the_tests_found(tmp_path):
    # pytest warns and searches from the root when no testpaths entry matches: the tests under tests/ still run.
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG,
         "pytest.ini": b"[pytest]\ntestpaths = test\n"},
        {"tests/test_calc.py": WEAK},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert report["tests"]["old"] == ["tests/test_calc.py"]
    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


FIXTURE_CONFTEST = (b"import pytest\n\nfrom pkg.calc import square\n\n{extra}\n\n@pytest.fixture\ndef expected():\n"
                    b"    return 9\n")
USES_EXPECTED = b"from pkg.calc import square\n\n\ndef test_square(expected):\n    assert square(3) == expected\n"


def test_a_helper_is_compared_through_even_beside_an_edit_the_suite_never_runs(tmp_path):
    # The range weakens the fixture and edits a module the suite excludes: that edit changes no module of the suite,
    # so the fixture is still compared through the modules that use it.
    conftest = FIXTURE_CONFTEST.replace(b"{extra}", b"collect_ignore = ['legacy']\n")
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/conftest.py": conftest, "tests/test_calc.py": USES_EXPECTED,
         "tests/legacy/test_old.py": b"def test_old():\n    pass\n"},
        {"tests/conftest.py": conftest.replace(b"return 9", b"return square(3)"),
         "tests/legacy/test_old.py": b"def test_old():\n    assert True\n"},
    )
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert report["tests"] == {"old": ["tests/test_calc.py"], "new": ["tests/test_calc.py"]}
    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


def test_unchanged_modules_are_left_out_when_an_existing_module_changed(tmp_path):
    # tests/test_b.py changed and is compared; tests/test_a.py, which only the weakened fixture serves, is not.
    conftest = FIXTURE_CONFTEST.replace(b"{extra}", b"")
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/conftest.py": conftest, "tests/test_a.py": USES_EXPECTED,
         "tests/test_b.py": STRONG},
        {"tests/conftest.py": conftest.replace(b"return 9", b"return square(3)"), "tests/test_b.py": WEAK},
    )
    report = _prove(repo, base, head, list_only=True)

    assert report["tests"] == {"old": ["tests/test_b.py"], "new": ["tests/test_b.py"]}
    assert any(n.startswith("changed test helpers, data or configuration (tests/conftest.py)") for n in report["notes"])


def test_unchanged_modules_that_import_a_changed_test_module_are_noted(tmp_path):
    shared = (b"from pkg.calc import square\n\n\nclass SquareChecks:\n    def test_square(self):\n"
              b"        assert square(3) == 9\n\n\ndef test_sanity():\n    assert square(0) == 0\n")
    impl = b"from tests.test_base import SquareChecks\n\n\nclass TestImpl(SquareChecks):\n    pass\n"
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/__init__.py": b"", "tests/test_base.py": shared,
         "tests/test_impl.py": impl},
        {"tests/test_base.py": shared.replace(b"square(3) == 9", b"square(2) == 4")},
    )
    report = _prove(repo, base, head, list_only=True)

    assert any("tests/test_impl.py (imports tests/test_base.py)" in n for n in report["notes"])


def test_doctests_the_project_collects_are_noted_as_not_compared(tmp_path):
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG,
         "pytest.ini": b"[pytest]\naddopts = --doctest-modules\n"},
        {"tests/test_calc.py": WEAK},
    )
    report = _prove(repo, base, head, list_only=True)

    assert any(n.startswith("the project's addopts collect doctests") for n in report["notes"])


def test_a_hooks_git_variables_reach_neither_prove_nor_the_tests(tmp_path, monkeypatch):
    # A pre-commit hook sets GIT_INDEX_FILE, others GIT_DIR. The tests make a repository of their own and stage a
    # file: with the hook's variables they would have written the caller's index.
    own_repo = (b"\n\ndef test_own_repository(tmp_path):\n    import subprocess\n\n"
                b"    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)\n"
                b"    (tmp_path / 'f.txt').write_text('x')\n"
                b"    subprocess.run(['git', '-C', str(tmp_path), 'add', 'f.txt'], check=True)\n")
    repo, base, head = _repo(
        tmp_path,
        {"pkg/__init__.py": b"", "pkg/calc.py": SQUARE, "tests/test_calc.py": STRONG + own_repo},
        {"tests/test_calc.py": WEAK + own_repo},
    )
    decoy = tmp_path / "callers-index"
    decoy.write_bytes(b"not an index")
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "nowhere"))
    report = _prove(repo, base, head, only="pkg/calc.py:2:11:AORB:1")

    assert decoy.read_bytes() == b"not an index"
    assert report["needs_clarification"] == []
    assert _loss_ids(report) == ["pkg/calc.py:2:11:AORB:1"]


def test_a_file_a_test_hard_linked_elsewhere_is_replaced_not_written_into(tmp_path):
    from checkwash_prove.engine import Candidate

    pristine, _ = _pristine(tmp_path)
    tree = Tree(pristine, tmp_path / "tree")
    outside = tmp_path / "outside-name.txt"
    os.link(tree.path / "conf.txt", outside)
    tree.reset(Candidate("conf.txt:1:0:X:1", "conf.txt", "X", 1, "inside", "INSIDE", "w", 0, 6, b"INSIDE", 0))

    assert (tree.path / "conf.txt").read_text() == "INSIDE"
    assert outside.read_text() == "inside"


def test_the_work_directory_goes_whatever_the_last_run_left(tmp_path):
    from checkwash_prove.engine import remove_workdir

    work = tmp_path / "work"
    (work / "a" / "b").mkdir(parents=True)
    (work / "a" / "b" / "f.txt").write_text("x")
    os.chmod(work / "a" / "b" / "f.txt", 0o444)
    if os.name != "nt":
        os.chmod(work / "a" / "b", 0o500)
    remove_workdir(work)

    assert not work.exists()


@pytest.mark.parametrize(("version", "missing"), [
    ((9, 2), set()),
    ((8, 4), {"pytest.toml", ".pytest.toml"}),
    ((7, 0), {"pytest.toml", ".pytest.toml", ".pytest.ini"}),
    (None, set()),
])
def test_configuration_files_follow_the_tests_pytest(version, missing):
    from checkwash_prove.engine import CONFIG_NAMES, config_names_for

    assert set(CONFIG_NAMES) - set(config_names_for(version)) == missing


def test_a_pytest_before_9_never_takes_pytest_toml(tmp_path):
    from checkwash_prove.engine import config_names_for, locate_config

    (tmp_path / "pytest.toml").write_text("[pytest]\npython_files = ['check_*.py']\n")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\npython_files = ['check_*.py']\n")
    assert locate_config(tmp_path, []).name == "pytest.toml"
    assert locate_config(tmp_path, [], config_names_for((8, 4))).name == "pyproject.toml"


def test_ini_comment_lines_are_no_settings():
    from checkwash_prove.engine import _setting

    text = "[pytest]\npython_files =\n    test_*.py\n    # *.py\n; addopts = --sw\n"
    assert _setting("pytest.ini", text, "python_files").split() == ["test_*.py"]
    assert _setting("pytest.ini", text, "addopts") is None


def test_an_import_root_takes_the_trees_spelling(tmp_path):
    from checkwash_prove.engine import _tree_spelling

    repo, _, head = _repo(tmp_path, {"Src/Pkg/__init__.py": b""}, {"Src/Pkg/calc.py": SQUARE})
    assert _tree_spelling(str(repo), head, "src/pkg") == "Src/Pkg"
    assert _tree_spelling(str(repo), head, "Src/Pkg") == "Src/Pkg"
    assert _tree_spelling(str(repo), head, "lib") == "lib"


def test_exit_5_is_no_catch_only_in_a_run_of_part_of_the_suite():
    from checkwash_prove.engine import Collected, _partial

    whole = Collected({"tests/test_a.py"}, set(), set(), set())
    assert not _partial(whole, ["tests/test_a.py"])
    assert _partial(Collected({"tests/test_a.py", "tests/test_b.py"}, set(), set(), set()), ["tests/test_a.py"])
    assert _partial(Collected({"tests/test_a.py"}, set(), set(), set(), {"tests/unit"}), ["tests/test_a.py"])
    assert _partial(None, ["tests/test_a.py"])


def test_the_main_module_does_not_run_on_import():
    proc = subprocess.run([sys.executable, "-c", "import checkwash_prove.__main__"], capture_output=True, text=True)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_keep_says_where_the_trees_are(tmp_path, capsys):
    repo, base, head = _weakened(tmp_path)
    report = _prove(repo, base, head, list_only=True, keep=True)
    try:
        assert f"kept the temporary trees in {report['workdir']}" in capsys.readouterr().err
    finally:
        shutil.rmtree(report["workdir"], ignore_errors=True)


@pytest.mark.parametrize("option", [["--timeout", "nan"], ["--timeout", "0"], ["--suite-timeout", "-5"],
                                    ["--workers", "0"], ["--max-mutants", "-1"]])
def test_impossible_numbers_are_usage_errors(option):
    with pytest.raises(SystemExit) as raised:
        main(["HEAD~1..HEAD", *option])
    assert raised.value.code == 2


@pytest.mark.parametrize("setup", [
    "import os\nos.close(2)\n",                                   # a standard error that cannot be written
    "import sys\nsys.modules['checkwash_prove._text'] = None\n",  # an escaping module that does not import
])
def test_a_usage_error_exits_2_in_a_process_of_its_own(setup):
    code = setup + "from checkwash_prove.cli import main\nraise SystemExit(main(['HEAD']))\n"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True)
    assert proc.returncode == 2
