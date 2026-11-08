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
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys

import pytest

import checkwash_prove
from checkwash_prove.cli import exit_code, interpreter, main, range_usage_error
from checkwash_prove.engine import (
    CATALOGUE_SHA256,
    CATALOGUE_VERSION,
    Options,
    ProveError,
    RunResult,
    _killed,
    changed_lines,
    is_test_module,
    is_test_path,
    normalise_root,
    parse_range,
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


@pytest.mark.parametrize("spec", ["HEAD", "..HEAD", "main..", "main....HEAD", "...HEAD", ""])
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
    assert any(note.startswith("no test module changed") for note in report["notes"])
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

    assert report["status"].startswith("not compared: the changed old tests have a pytest configuration")
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


@pytest.mark.parametrize(("runs", "category", "confirmation"), [
    # Clean old, new and whole-suite references, then the mutant's three runs.
    ([PASSED, PASSED, PASSED, CAUGHT, PASSED, PASSED], "loss", "repeated"),
    ([PASSED, PASSED, PASSED, PASSED], "unconfirmed loss", "the old tests did not catch it again"),
    ([PASSED, PASSED, PASSED, CAUGHT, CAUGHT], "unconfirmed loss", "the new tests caught it when run alone"),
    ([PASSED, PASSED, PASSED, CAUGHT, PASSED, CAUGHT], "unconfirmed loss",
     "the whole new suite caught it when run alone"),
])
def test_a_loss_is_kept_only_when_it_repeats_alone(runs, category, confirmation):
    from checkwash_prove.engine import _confirm

    results = [RunResult(0 if outcomes is PASSED else 1, outcomes, {}) for outcomes in runs]
    row = {"category": "loss"}
    _confirm(_ScriptedRunner(results), [row], [object()], _StillTree(), _StillTree(), ["t"], ["t"], _opts(),
             {"old": 1, "new": 1, "suite": 1})

    assert (row["category"], row["confirmation"]) == (category, confirmation)
