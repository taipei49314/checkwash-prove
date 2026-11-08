# checkwash-prove threat model

checkwash-prove executes the repository's code: product code, tests,
`conftest.py` files and pytest configuration, at two revisions, many times,
with your privileges. That is its job. checkwash's claims (local,
byte-identical, no code under review runs) do not carry over to it; this
page replaces them for this tool. It covers version 0.1.0.dev0, which is
experimental and unreleased.

## What it promises

- **It runs only when asked.** Only `checkwash-prove` (or
  `python -m checkwash_prove`) starts it. checkwash never imports or starts
  it, and it never imports checkwash; a test checks the second half.
- **It reads the repository and leaves it alone.** Its own Git commands
  only read (`rev-parse`, `diff`, `ls-tree`, `merge-base`, `cat-file`) or
  check files out into its own temporary directory through a temporary index
  (`read-tree`, `checkout-index`). It does not write the repository's working
  tree, index or refs, and makes no network request of its own, with two
  exceptions Git makes: in a partial clone (`--filter`), reading an object
  the clone lacks fetches it from the remote, through your credential
  helper, and stores it under `.git`; and a checkout filter such as Git
  LFS's may fetch from its server (below). The code it runs is another
  matter.
- **A loss can be checked by anyone.** Every protection loss names the
  catalogue mutant and comes with the command that replays that one mutant.
  A loss is reported only after the old tests caught the mutant, the new
  tests and the whole new suite did not, and that repeated in a second
  round, one run at a time, against fresh clean runs.
- **The fault list is fixed in advance.** Mutants come from the frozen
  catalogue generator v0.5-m2, whose SHA-256 is checked before every run and
  before the generator is imported.
- **A failure never reads as a loss.** A usage error, a Git error or a
  crash exits 2; with `--fail-on-loss`, so does a run that could not be
  decided. None of them exits 1, the loss code. An interrupted run exits as
  Python does on an interrupt.

## What it does not promise

### No sandbox

The code it runs can do anything you can: read and write your files
(outside the temporary trees too, the real repository included, through an
absolute path), read your environment and credentials, use the network,
and start processes. The run hygiene below exists so that runs do not
disturb each other's results. It is not a security boundary:

- every run works in a copy of a pristine tree, put back and checked after
  the run. Putting it back removes a symbolic link or junction a test left
  without following it (on Windows, clearing the link's own read-only
  attribute first if it has to), and changes a file's mode only on Windows,
  where a read-only file cannot be deleted, and only when no hard link shares
  the file with one elsewhere. On POSIX it gives the tree's own directories,
  never a link's target, their owner's permissions back. A link it still
  cannot remove is left alone, with nothing written through it, and the run
  stops;
- each run gets its own pytest cache outside the tree, `PYTEST_ADDOPTS`
  cleared, `--confcutdir` at the tree, and `-p no:randomly`;
- every run has a time budget; at its end, or at a timeout, everything in
  the run's job object (Windows) or process group (POSIX) is ended. The
  probe that imports the product packages runs the same way, within 300
  seconds.

A process a test moves to a session or process group of its own (POSIX
`setsid`, `setpgid`), or one started outside the run's job object or
process group by other means (a service, a scheduler), can outlive the run.

### Results the code under test can steer

The code under test can tell that checkwash-prove is running it: the
`CHECKWASH_PROVE_*` environment variables, its pytest plugin
(`_checkwash_prove_plugin`), the tracer during traced runs, product files
that differ from the commit, and the temporary path. A change written to
defeat it can therefore hide a loss, for example with a `conftest.py` that
fails whenever the product files differ from HEAD; it can also make the
suite look weaker than it is in CI. So:

- a reported loss is a lead: replay it with `--only` and read the mutant and
  the tests before acting on it;
- no loss is no evidence about a change that may have been written to pass
  this tool, and is never a pass.

The same holds for the base revision: both revisions run.

### Determinism

Test runs depend on time, randomness, order, the network and shared machine
state. Losses are repeated alone against fresh clean runs, and the ones that
do not repeat are listed as unconfirmed; mutants run one at a time unless
`--workers` asks for more, which the report flags. Reports are not
byte-identical between runs or machines: timings, budgets and temporary
paths differ, and a flaky suite can change the outcome.

### Resources

A run makes up to `--max-mutants` mutant runs of the old and new tests,
whole-suite runs and confirmation runs on top, each within its time budget,
plus copies of the tree in the temporary directory. A test can use CPU,
memory and disk freely until its budget ends. Lower `--max-mutants` and the
timeouts to bound the cost; the two traced runs get ten times their clean
run plus 60 seconds, and never less than `--suite-timeout`.

### What Git and the interpreter bring in

- Checking files out applies the repository's configured checkout filters
  and its `.gitattributes` line-ending rules, as `git checkout` would (with
  `core.autocrlf` off): a Git LFS filter, for one, may fetch from its
  server. `diff` runs with `--no-ext-diff` and `--no-textconv`; other Git
  configuration is the repository's and yours.
- The interpreter given with `--python`, and every package installed in it,
  is trusted: pytest plugins installed there load in every run. To check
  where the product packages import from, it imports them once in a probe
  process, which runs their import-time code too.

### Output

- File names, test ids and test output come from the repository. The text
  report and every line on standard error escape control, format and
  line-separator characters (joiners excepted); output pytest printed over
  several lines shows as continuation lines marked `  | `. The JSON report
  escapes only what JSON requires (control characters below U+0020, quotes,
  backslashes): a program that prints its fields to a terminal must escape
  the rest itself.
- The replay command is quoted with `shlex` on POSIX and with Windows'
  `list2cmdline` there. `list2cmdline` quotes for program argument parsing,
  not for cmd.exe or PowerShell: read a replay command whose paths hold
  anything but letters, digits and `. _ - / \ :` before pasting it into
  either.
- Temporary trees, JUnit files and trace files live in directories made by
  Python's `tempfile`, readable only by you on POSIX and under your own
  temporary folder on Windows, and are removed at the end unless `--keep`
  is given.

### The catalogue check

The SHA-256 check catches an edited or damaged copy of the generator. Anyone
who can change the installed package can change the check too, so it is no
defence against someone with write access to your environment.

## Where to run it, and where not

- On your own checkout, on changes you would run anyway: your branch, an
  agent's work in your own workspace.
- In a throwaway container or virtual machine without credentials, when the
  code is not yours.
- Not in CI on pull requests from forks, not under `pull_request_target`,
  and never in a job that holds secrets or a write token: that runs the pull
  request's code with them. `actions/checkout` keeps its token where Git
  in the checkout can use it unless told `persist-credentials: false`, so
  the tests, and a partial clone's fetches, can use it too.
- Not as a required check. A merge gate that needs a test run is a test
  runner; this tool's result is a lead for a reviewer, not a verdict.
