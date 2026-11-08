# checkwash-prove (experimental)

[checkwash](https://github.com/taipei49314/checkwash)'s `checkwash check`
reads a diff and never runs your code. `checkwash-prove` asks the same
question the opposite way: it **runs your tests** to show, with a concrete
fault, what protection a change to them dropped.

It is a separate package with its own command, kept outside checkwash as
checkwash's [satellite rule](https://github.com/taipei49314/checkwash/blob/main/docs/satellite-execution.md)
asks: it does not import checkwash, checkwash does not import it, and nothing
in checkwash (`check`, the GitHub Action, the hooks) starts it. It runs only
when you type `checkwash-prove`.

It is experimental. Its options, output and exit codes may change in any
version.

## Run it only on code you would run anyway

checkwash-prove executes the repository's tests and product code, at both
revisions, many times, with your privileges and no sandbox. Use it on your
own checkout, on code you trust. Never point it at an untrusted pull request
in CI: that is running the pull request's code. Do not make it a required
check either: its result is a lead for a reviewer, not a merge verdict.
[THREATMODEL.md](https://github.com/taipei49314/checkwash-prove/blob/main/THREATMODEL.md)
says what it does and does not protect.

## Install

```
pip install checkwash-prove
```

It needs Python 3.11 or later and Git 2.30 or later on `PATH`. The
interpreter that runs the tests (`--python`, by default the one running
checkwash-prove) needs the project's test dependencies and pytest 7 or
later. The catalogue reads product files with the Python that runs
checkwash-prove, so use one at least as new as the project's syntax; a file
it cannot parse is noted and not mutated.

## What it does

For a range `BASE..HEAD`, or `BASE...HEAD` to compare from the merge base as
`checkwash check` does (use this one for a branch, or changes made on the
base branch since will read as the branch's):

1. It builds two trees: HEAD, and HEAD with the test-suite files the range
   changed put back to BASE. Those are test modules (named as the
   repository root's pytest configuration sets `python_files`, at either
   revision, pytest's default `test_*.py` and `*_test.py` when it does not,
   and under its `testpaths` when it sets them), `conftest.py` files, any
   file under a `test`, `tests` or `testing` directory (helpers, data), and
   pytest configuration (`pytest.toml`, `.pytest.toml`, `pytest.ini`,
   `.pytest.ini`, `pyproject.toml`, `tox.ini`, `setup.cfg`) when its pytest
   settings changed. Test files the range added are left out of the old
   tree.
2. It runs the **old** test modules against the **new** product code. An old
   test that fails there is listed under **needs clarification**: the
   behaviour changed, and only a person can say whether on purpose. It is
   never counted as a loss. An old test module that no longer imports is
   listed there too, and the other old tests still run. When the new version
   of the same test fails as well, the report says so: a broken environment
   or a defect is then likelier than a deliberate change. When helpers,
   fixtures, data or configuration changed and no changed test module that
   existed before is in the old suite (only modules added, or ones the suite
   does not run, changed with them), the test modules they can affect are
   compared: those under the outermost test directory above the changed
   file, or under its own directory, that each side's own run collects.
3. In separate traced runs (the runs it compares are not traced, so a test
   that measures its own time is not disturbed), it records the product
   lines the old tests execute and generates mutants on those lines from the
   frozen catalogue v0.5-m2 (below). Each mutant is one small, ordinary
   fault, such as `*` turned into `+` or `-=` into `>>=`.
4. It runs the old and the new test modules against every mutant. For a
   mutant only the old tests catch, it runs the **whole** new suite. When no
   new test catches it either, it runs all three again, one run at a time,
   against fresh clean runs. Only if that agrees is the mutant a
   **protection loss**: a concrete fault the old tests caught and the new
   suite lets through. One that does not repeat is listed apart as an
   unconfirmed loss (a flaky test, or tests that interfere when run side by
   side).

What counts as caught is what CI would fail: a test that passed on the clean
code (or was skipped) and fails, a module that stops importing, a run that
dies, or a timeout. A skip is not. A test that failed on the clean code, or
a module that did not import there, catches nothing. checkwash-prove names
test modules on the command line, so it runs only the changed modules each
side's own whole-suite collection holds: a module the project excludes
(`collect_ignore`, `--ignore`, outside `testpaths`) protected nothing in CI,
and its directory's `conftest.py` and fixtures never load there; the report
notes it. A test file the collection fails on, or skips as a whole, is part
of the suite all the same, and so is every file under a directory whose
`conftest.py` or `__init__.py` does not import: its run then fails, and the
result says the tests do not run. Within a run, only a test that collection
holds counts (a deselected one does not). A run that hangs or dies is
pinned to the test it was in, its own fixtures' teardown included, or to
the test file it was collecting, and counts only when that one does; a run
that was in none failed as a whole, as CI's would. One that hangs in the
teardown of fixtures that several tests share (class, module, package or
session scope) failed as a whole for the new tests and the whole suite, and
for the old tests in the last test of that class, module, package or
session, the one whose teardown ends them. In a run of some of the suite's files, exit 5 (no test ran, every
module skipped) is no catch: the whole suite runs other tests and passes. A
test the mutant renamed (a parameter id computed from the product code)
counts when the clean run passed its other instances, and a test class
whose collection the mutant broke counts when the clean run passed or
skipped any of its tests; with no clean result to go by, a test counts for the new tests
and not for the old, so an unclear result never makes a loss, though it can
hide one. A loss is confirmed only when the same old test, or the old run
as a whole, catches the mutant again. Every run starts from a pristine copy
of its tree, checked after it is put back, so a file one test run leaves
behind never reaches the next; a link or junction it leaves is removed
without following it.

Every loss comes with the command that replays it alone:

```
checkwash-prove 1a2b3c..4d5e6f --repo /path/to/repo --only src/pkg/calc.py:2:11:AORB:1
```

## The catalogue: what counts as evidence

A mutant is evidence because a frozen generator produced it, not because
someone wrote a fault after looking at the tests. The catalogue is muJava's
method-level operators (Ma and Offutt, description updated July 2016) mapped
to Python: AORS, AOIS and AODS, which need `++` and `--`, have no Python
form, and PY-ASRS takes ASRS's place, replacing a compound assignment
operator with any other in the set `+= -= *= @= /= //= %= **= <<= >>= &=
^= |=`. As the method level says, only code inside function and method
bodies is mutated. `src/checkwash_prove/mutops_v05m2.py` is that generator
byte for byte, and it is the catalogue's definition (the catalogue files its
docstring names are not published here); checkwash-prove refuses to run
when its SHA-256 differs from the frozen one, and imports it only after that
check. The report names the catalogue
version, and the JSON report also the hash it checked. A mutant id
names its file, position and operator, `path:line:column:OP:n` (`n` counts
the mutants one operator makes at one place), so it does not depend on which
lines a run traced; unless the path has unusual characters, it needs no
quoting in a shell.

## Usage

```
checkwash-prove BASE..HEAD [--repo DIR] [--format term|json] [--out FILE]
                [--python PATH] [--import-root DIR] [--workers N]
                [--timeout S] [--suite-timeout S] [--max-mutants N]
                [--only ID] [--list] [--strict-warnings] [--fail-on-loss]
                [--keep]
```

`python -m checkwash_prove` is the same command.

- `--repo`: any directory of the Git repository. It runs from the
  repository root, and says so on standard error when `--repo` names a
  subdirectory.
- `--list`: do everything up to the mutants (the old and new tests' clean
  and traced runs, both trees' collections), then list every mutant with its
  id in run order, without running any. Use an id with `--only`.
- `--python`: the interpreter that runs the tests, with the project's test
  dependencies installed. The default is the one running checkwash-prove.
  It puts the trees it builds first on `PYTHONPATH` and stops when a product
  package (a directory with an `__init__.py` directly under the import root,
  the first 32 by name) still imports from somewhere else; single-file
  modules and namespace packages are not checked.
- `--import-root`: the directory that holds the product packages, `src` when
  it exists and the repository root otherwise, spelt as HEAD's tree spells
  it.
- `--keep`: keep the temporary trees, and print their directory on standard
  error.
- `--timeout` and `--suite-timeout`: a clean run gets at most
  `--suite-timeout` seconds (default 900); a run against a mutant gets three
  times its clean run's time plus 10 seconds, and never less than
  `--timeout` (default 30); a traced run gets ten times its clean run's time
  plus 60 seconds, and never less than `--suite-timeout`. A timeout counts as
  caught when the test it hung in counts: CI fails a run that hangs.
- `--max-mutants` (default 200): the cap on mutants run.
- Run order: mutants on lines only the old tests execute run first, then
  those on lines the range added or replaced, then the rest of the product
  files it changed, then everything else, and last those on lines that only
  old tests which do not pass on the new code ran (such a test cannot catch
  a mutant, but a fixture or an import it happened to run first can serve
  the passing tests). Within each of these ranks the files take turns, so a
  cap reaches every file. The report says how many were left over the cap.
- `--workers`: mutants run one at a time by default. With more, tests that
  share ports, files outside the repository or other machine-wide state can
  interfere, and a catch that interference causes can hide a loss; the
  report says so. Losses are always run again alone.
- `--strict-warnings`: by default the old tests run with
  `-W ignore::DeprecationWarning`, so a name the change deprecated but kept
  working does not read as a failure. This flag keeps the project's own
  warning settings.
- `--format json` (or `--out FILE`) gives the full report, including every
  mutant's outcome. JSON on standard output is UTF-8, whatever the console's
  code page. The text report escapes control, format and line-separator
  characters, since paths, test ids and test output come from the
  repository.

The tests run under the project's own pytest configuration, found as the
pytest in `--python` finds it (`pytest.toml` and `.pytest.toml` only from
pytest 9, `.pytest.ini` from pytest 7.2) but only inside the repository,
never above it (with `--confcutdir` at the tree, so no `conftest.py` above
it loads either). Git's variables that name a repository, an index or a
work tree (`GIT_DIR`, `GIT_INDEX_FILE` and the like, which a Git hook sets)
are removed from its own Git commands and from the tests' environment.
They run in one process, in order, without coverage and past every failure:
checkwash-prove passes `-n 0` when the project's addopts distribute the run
with pytest-xdist, `--no-cov` when they turn pytest-cov on, `-p no:randomly`,
`--maxfail=0` (over `-x` or `--maxfail` in addopts) and an empty
`--junit-prefix`; it refuses to run when the addopts turn on `--stepwise`
(`--sw`, `--sw-skip`, `--sw-reset`), which nothing turns off. It gives each run a pytest cache of its own outside
the tree, and clears `PYTEST_ADDOPTS`. A timeout, and the end of every run,
ends what the run started: on Windows everything in the run's job object, on
POSIX everything in its process group. A process a test moves to a session
or process group of its own (`setsid`, `setpgid`) is out of reach on POSIX;
if it changes the tree, the next reset puts the tree back, and stops the run
when it cannot within about five seconds.

Exit codes: `0` when the report was produced. With `--fail-on-loss`: `1` when
there is a loss; `2` when the comparison did not happen (old or new tests that
do not run, no mutants, tests pytest runs under a configuration of their own
that the root run does not collect, test files one pytest run cannot name
together) or some of it could not be decided (a
whole-suite result that could not be had, a loss that did not repeat alone);
`0` when every mutant run was decided without a loss, when the mutants were
only listed, and for the three statuses that begin "nothing to compare": the
range changes no test file, no test module that existed before the change is
affected, or the old suite does not collect the changed old tests (they
protected nothing). `2` also for a usage error or anything that stopped the
run, a Git error or a crash included: never `1`.

## What the result does not say

- No loss is not proof that nothing was lost. Only lines the old tests
  executed are mutated, only inside function and method bodies (never
  module- or class-level statements, decorators, default values or
  annotations), only with this catalogue, and only up to `--max-mutants`; a
  traced run sees only the lines a test reaches under the tracer.
- Doctests are not compared: only named test modules run, so a doctest in a
  product module or a `--doctest-glob` file is in no run. The report notes
  addopts that collect them.
- "Nothing to compare", "no mutants" and the other statuses are said as
  such: they mean nothing was tested, not that the change is safe.
- A loss is a fact about the tests, not a verdict on the change: the change
  may have dropped that protection on purpose.
- The code under test can tell it is being run by checkwash-prove, so a
  change written to defeat it can hide a loss. See
  [THREATMODEL.md](https://github.com/taipei49314/checkwash-prove/blob/main/THREATMODEL.md).
- It takes a pytest run at the repository root as the whole suite and
  counts only the tests that run collects. Changed test modules that pytest
  runs under another configuration file are noted; when the root run
  collects none of the changed old tests and some are such, the result is
  "not compared".
- When test modules that existed before (and that the old suite runs)
  changed, only those are compared: an unchanged module that uses a changed
  helper or fixture is not (under a changed `conftest.py`, comparing them
  would run the whole suite against every mutant), nor one that imports a
  changed test module (a base class, shared tests); the report notes both.
  With no such change, unchanged modules are compared only through a
  changed file under the same test directory; one that imports a changed
  helper from elsewhere is not.
- Test modules are found by the root configuration's `python_files` and
  `testpaths` (testpaths that match nothing are left out, as pytest then
  searches from the root); one set in a configuration file below the root
  is not read. A pattern that also matches product modules makes them test
  files, never mutated.
- Helpers and data count as test files only under a `test`, `tests` or
  `testing` directory. Elsewhere (a `testdata/` directory, a helper beside
  the tests in a `testpaths` directory of another name) they count as
  product code: they are not put back to BASE, so weakening one is not
  compared.
- One pytest run names every test file of a side. When pytest would run them
  from a package's own rootdir (its configuration or `pyproject.toml`) and
  some lie outside it, the result is "not compared": compare the parts in
  separate ranges.
- A test that behaves differently from run to run can still mislead it; a
  loss that does not repeat alone is listed apart as unconfirmed.
- Files under a directory named `test`, `tests` or `testing` count as test
  files, so a product package with such a name (`numpy.testing`,
  `django.test`) is treated as tests: put back to BASE and never mutated.
- Submodules are not compared.
- On Windows, a test suite that reads files in UTF-8 may need
  `PYTHONUTF8=1`.

## Working on it

```
pip install -e ".[dev]"
pytest
```

The tests build small Git repositories and run the real procedure on them,
so they need `git` on `PATH` and take minutes, not seconds. Some are for one
platform only (symlinks on POSIX, junctions and case-insensitive paths on
Windows). CI runs them on Linux, Windows and macOS with every supported
Python version. Changes are listed in
[CHANGELOG.md](https://github.com/taipei49314/checkwash-prove/blob/main/CHANGELOG.md);
[RELEASING.md](https://github.com/taipei49314/checkwash-prove/blob/main/RELEASING.md)
says how a version reaches PyPI.

## Reporting a vulnerability

See [SECURITY.md](https://github.com/taipei49314/checkwash-prove/blob/main/SECURITY.md).

## License

Apache-2.0, as checkwash.
