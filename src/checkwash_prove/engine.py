"""checkwash-prove (experimental): run tests to show what protection a change dropped.

For a range BASE..HEAD (or BASE...HEAD, compared from the merge base) this module
1. builds two pristine trees: the HEAD tree, and the same tree with the test-suite files the range changed (test
   modules, conftest files, files under test directories, pytest configuration) put back to BASE. No test runs in a
   pristine tree: every run happens in a copy that is put back to its pristine state before the run;
2. runs the old test modules on the new code. An old test that fails there is "needs clarification": the behaviour
   changed, and only a person can say whether on purpose;
3. records, in separate traced runs, the product lines the old tests execute, and generates mutants on those lines
   from the frozen catalogue v0.5-m2;
4. runs the old and the new test modules against each mutant, and the whole new suite for every mutant only the old
   tests catch. A mutant nothing new catches is run again, one run at a time, against fresh clean runs; only when that
   agrees is it a "protection loss": a concrete fault the old tests would have caught and the new suite lets through.

Nothing here is an opinion: every finding comes from test runs and can be replayed with --only.
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib.resources
import json
import os
import pathlib
import posixpath
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait

from checkwash_prove import mutops_v05m2 as mutops

CATALOGUE_VERSION = "v0.5-m2"
# SHA-256 of mutops_v05m2.py with CRLF normalised to LF: the frozen generator, byte for byte.
CATALOGUE_SHA256 = "67096f7340a281585aaeaf801ae0ac29c4eb1a777c358ee973f02058cc7f4e14"
TEST_DIRS = {"test", "tests", "testing"}
# pytest's own search order for configuration files (pytest 9).
CONFIG_NAMES = ("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
PLUGIN = "_checkwash_prove_plugin"
# A mutant run gets this many times the clean run's time, plus the margin, and never less than --timeout.
TIMEOUT_FACTOR, TIMEOUT_MARGIN = 3.0, 10.0
# Longer test-file lists go through an allow-list file: Windows refuses command lines past 32767 characters.
ARGS_LIMIT = 20000
# Rank of a mutant on a line only old tests that do not pass on the new code ran: last.
RANK_ONLY_FAILING = 4
PLUGIN_SOURCE = '''\
"""pytest plugin written by checkwash-prove; loaded with -p on every run it makes.

With CHECKWASH_PROVE_TRACE_OUT set it records the lines executed under CHECKWASH_PROVE_TRACE_ROOT: those run outside
any test (imports, collection, session hooks), and those run by each test, by node id. With CHECKWASH_PROVE_ONLY set
it collects exactly the test files listed in that file, as if they were named on the command line. With
CHECKWASH_PROVE_COLLECT_OUT set it writes the node ids the run collected.
"""
import json
import os
import sys
import threading

import pytest

_OUT = os.environ.get("CHECKWASH_PROVE_TRACE_OUT")
_ONLY = os.environ.get("CHECKWASH_PROVE_ONLY")
_COLLECT = os.environ.get("CHECKWASH_PROVE_COLLECT_OUT")
_ROOT = os.path.normcase(os.path.realpath(os.environ.get("CHECKWASH_PROVE_TRACE_ROOT") or os.getcwd())) + os.sep
_outside = {}
_tests = {}
_current = None
_wanted = {}
_allowed = None
_allowed_dirs = None


def _is_wanted(name):
    hit = _wanted.get(name)
    if hit is None:
        # "<frozen importlib._bootstrap>", "<string>": no file, though realpath would place it in the working directory.
        if not name or name.startswith("<"):
            hit = False
        else:
            try:
                hit = os.path.normcase(os.path.realpath(name)).startswith(_ROOT)
            except (OSError, ValueError):
                hit = False
        _wanted[name] = hit
    return hit


def _record(name, line):
    target = _current if _current is not None else _outside
    target.setdefault(name, set()).add(line)


def _local(frame, event, arg):
    if event == "line":
        _record(frame.f_code.co_filename, frame.f_lineno)
    return _local


def _global(frame, event, arg):
    if event == "call" and _is_wanted(frame.f_code.co_filename):
        _record(frame.f_code.co_filename, frame.f_lineno)
        return _local
    return None


if _OUT:
    # Set at import: -p loads this module before the initial conftest files, whose imports must be seen too.
    threading.settrace(_global)
    sys.settrace(_global)


def pytest_configure(config):
    global _allowed, _allowed_dirs
    if _ONLY:
        with open(_ONLY, encoding="utf-8") as fh:
            _allowed = {os.path.normcase(line.strip()) for line in fh if line.strip()}
        _allowed_dirs = set()
        for path in _allowed:
            parent = os.path.dirname(path)
            while parent and parent not in _allowed_dirs:
                _allowed_dirs.add(parent)
                if os.path.dirname(parent) == parent:
                    break
                parent = os.path.dirname(parent)


@pytest.hookimpl(tryfirst=True)
def pytest_ignore_collect(collection_path, config):
    # Named files and the directories that lead to them are collected whatever norecursedirs or --ignore say, as
    # files named on the command line are; nothing else is.
    if _allowed is None:
        return None
    path = os.path.normcase(os.path.realpath(collection_path))
    return False if path in _allowed or path in _allowed_dirs else True


def pytest_collection_finish(session):
    if _COLLECT:
        with open(_COLLECT, "w", encoding="utf-8") as fh:
            json.dump([item.nodeid for item in session.items], fh)


def pytest_runtest_logstart(nodeid, location):
    global _current
    _current = _tests.setdefault(nodeid, {})


def pytest_runtest_logfinish(nodeid, location):
    global _current
    _current = None


def pytest_unconfigure(config):
    if not _OUT:
        return
    sys.settrace(None)
    threading.settrace(None)

    def plain(part):
        return {name: sorted(lines) for name, lines in part.items()}

    with open(_OUT, "w", encoding="utf-8") as fh:
        json.dump({"outside": plain(_outside), "tests": {nodeid: plain(part) for nodeid, part in _tests.items()}}, fh)
'''
PROBE_SOURCE = '''\
import importlib, importlib.util, json, sys
out = {"packages": {}, "plugins": {}}
for name in sys.argv[2:]:
    try:
        out["packages"][name] = getattr(importlib.import_module(name), "__file__", None)
    except BaseException as exc:
        out["packages"][name] = "error: %s: %s" % (type(exc).__name__, exc)
for plugin in ("xdist", "pytest_cov"):
    try:
        out["plugins"][plugin] = importlib.util.find_spec(plugin) is not None
    except Exception:
        out["plugins"][plugin] = False
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(out, fh)
'''


class ProveError(Exception):
    """A precondition failed; the message says what to do."""


class Stopped(Exception):
    """The run was interrupted."""


@dataclasses.dataclass
class Options:
    python: str
    workers: int = 1                         # side-by-side runs can interfere: one at a time unless asked
    timeout: float = 30.0                    # the least time a mutant run gets
    suite_timeout: float = 900.0             # the most time a clean run gets
    max_mutants: int = 200
    import_root: str | None = None
    strict_warnings: bool = False
    only: str | None = None
    keep: bool = False
    list_only: bool = False
    replay: list[str] = dataclasses.field(default_factory=list)   # options the replay command repeats


def verify_catalogue() -> str:
    data = importlib.resources.files("checkwash_prove").joinpath("mutops_v05m2.py").read_bytes()
    digest = hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()
    if digest != CATALOGUE_SHA256:
        raise ProveError(f"the vendored catalogue generator differs from frozen {CATALOGUE_VERSION} "
                         f"(sha256 {digest}, expected {CATALOGUE_SHA256})")
    return digest


# ---- which files are which -----------------------------------------------------------------------------------------

def is_test_path(path: str) -> bool:
    """A file of the test suite: any file under a test directory, a test module, or a conftest.py."""
    parts = path.split("/")
    name = parts[-1]
    if any(p.lower() in TEST_DIRS for p in parts[:-1]):
        return True
    return name.endswith(".py") and (name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py"))


def is_test_module(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def is_config_name(path: str) -> bool:
    return path.rsplit("/", 1)[-1] in CONFIG_NAMES


def scope_dir(path: str) -> str:
    """The directory whose test modules a changed helper, data or configuration file can affect: the outermost test
    directory above it, or else its own directory ("" for the repository root)."""
    parts = path.split("/")[:-1]
    for i, part in enumerate(parts):
        if part.lower() in TEST_DIRS:
            return "/".join(parts[:i + 1])
    return "/".join(parts)


_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _ini_section(text: str, section: str) -> str | None:
    lines, inside, found = [], False, False
    for line in text.splitlines():
        head = _SECTION.match(line)
        if head:
            inside = head.group(1).strip() == section
            found = found or inside
            continue
        if inside:
            lines.append(line.rstrip())
    return "\n".join(lines).strip() if found else None


def pytest_settings(name: str, data: bytes | None) -> str | None:
    """What a configuration file says to pytest, as comparable text; None when it says nothing."""
    if data is None:
        return None
    text = data.decode("utf-8", "replace")
    if name in ("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini"):
        return text
    if name == "pyproject.toml":
        try:
            table = tomllib.loads(text).get("tool", {}).get("pytest")
        except tomllib.TOMLDecodeError:
            return text
        return None if table is None else json.dumps(table, sort_keys=True, default=str)
    return _ini_section(text, "pytest" if name == "tox.ini" else "tool:pytest")


def addopts(config: pathlib.Path | None) -> str:
    """The addopts a configuration file gives pytest, as one string."""
    if config is None:
        return ""
    try:
        text = config.read_text(encoding="utf-8")
        if config.name in ("pytest.toml", ".pytest.toml"):
            value = tomllib.loads(text).get("pytest", {}).get("addopts", "")
        elif config.name == "pyproject.toml":
            table = tomllib.loads(text).get("tool", {}).get("pytest", {})
            value = table.get("ini_options", {}).get("addopts", table.get("addopts", ""))
        else:
            body = _ini_section(text, "tool:pytest" if config.name == "setup.cfg" else "pytest") or ""
            found = re.search(r"(?ms)^addopts\s*[=:]\s*(.*?)(?=^\S|\Z)", body)
            value = found.group(1) if found else ""
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return ""
    return " ".join(map(str, value)) if isinstance(value, list) else str(value)


def _walk_for_config(start: pathlib.Path, tree: pathlib.Path) -> tuple[pathlib.Path | None, pathlib.Path | None]:
    """(configuration, first pyproject.toml seen) from start up to the tree's root, never above it."""
    pyproject, base = None, start
    while True:
        for name in CONFIG_NAMES:
            path = base / name
            if path.is_file():
                if name == "pyproject.toml" and pyproject is None:
                    pyproject = path
                if pytest_settings(name, path.read_bytes()) is not None:
                    return path, pyproject
        if base == tree or base.parent == base:
            return None, pyproject
        base = base.parent


def locate_config(tree: pathlib.Path, args: list[str]) -> pathlib.Path | None:
    """The configuration file pytest would use for these arguments, as pytest 9 looks for it, but only inside the
    tree: from the arguments' common ancestor up; failing that, and with no setup.py there, from each argument."""
    dirs = []
    for arg in args:
        path = tree / arg
        if path.exists():
            dirs.append(path if path.is_dir() else path.parent)
    ancestor = pathlib.Path(os.path.commonpath([str(d) for d in dirs])) if dirs else tree
    found, pyproject = _walk_for_config(ancestor, tree)
    if found:
        return found
    if pyproject is None and dirs and dirs != [ancestor]:
        base = ancestor
        while not (base / "setup.py").is_file():
            if base == tree or base.parent == base:
                for start in dirs:
                    found, seen = _walk_for_config(start, tree)
                    if found:
                        return found
                    pyproject = pyproject or seen
                break
            base = base.parent
    return pyproject


# ---- git -----------------------------------------------------------------------------------------------------------

def _git(repo: str, *args: str, env: dict[str, str] | None = None, input: bytes | None = None,
         ok: tuple[int, ...] = (0,)) -> bytes:
    proc = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.quotepath=false", *args], cwd=repo,
                          capture_output=True, env=env, input=input)
    if proc.returncode not in ok:
        raise ProveError(f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def _git_code(repo: str, *args: str) -> int:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True).returncode


def parse_range(spec: str) -> tuple[str, str, bool] | None:
    """(base, head, three_dot) for BASE..HEAD or BASE...HEAD; None for anything else."""
    three = "..." in spec
    base, dots, head = spec.partition("..." if three else "..")
    if not dots or not base or not head or head.startswith("."):
        return None
    return base, head, three


def resolve(repo: str, rev: str) -> str:
    return _git(repo, "rev-parse", "--verify", "--end-of-options", rev + "^{commit}").decode().strip()


@dataclasses.dataclass
class Change:
    status: str        # A, D, M or T
    path: str
    gitlink: bool      # a submodule pointer on either side


def changed_paths(repo: str, base: str, head: str) -> list[Change]:
    tokens = _git(repo, "diff", "--raw", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", base, head
                  ).decode("utf-8", "replace").split("\0")
    out, i = [], 0
    while i + 1 < len(tokens):
        meta, path = tokens[i], tokens[i + 1]
        i += 2
        if not meta.startswith(":"):
            continue
        fields = meta[1:].split()
        out.append(Change(fields[4][:1], path, "160000" in (fields[0], fields[1])))
    return out


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(repo: str, base: str, head: str, paths: list[str]) -> dict[str, set[int]]:
    """For each path, the HEAD-side lines the range added or replaced (an empty set: it only lost lines)."""
    out: dict[str, set[int]] = {}
    for path in paths:
        # The configured diff options must not move a line: no textconv, no external diff, no merged hunks.
        text = _git(repo, "diff", "-U0", "--inter-hunk-context=0", "--no-renames", "--no-color", "--no-ext-diff",
                    "--no-textconv", base, head, "--", f":(literal){path}").decode("utf-8", "replace")
        lines: set[int] = set()
        for line in text.splitlines():
            hunk = _HUNK.match(line)
            if hunk:
                start, count = int(hunk.group(1)), int(hunk.group(2) or "1")
                lines.update(range(start, start + count))
        out[path] = lines
    return out


def tree_files(repo: str, rev: str, dirs: list[str]) -> list[str]:
    specs = [] if "" in dirs else ["--", *(f":(literal){d}" for d in dirs)]
    names = _git(repo, "ls-tree", "-r", "-z", "--name-only", rev, *specs).decode("utf-8", "replace").split("\0")
    return [n for n in names if n]


def blob(repo: str, rev: str, path: str) -> bytes | None:
    proc = subprocess.run(["git", "cat-file", "blob", f"{rev}:{path}"], cwd=repo, capture_output=True)
    return proc.stdout if proc.returncode == 0 else None


def checkout(repo: str, rev: str, dest: pathlib.Path, paths: list[str] | None = None) -> None:
    """Files of rev as `git checkout` writes them (filters and line endings applied), under dest."""
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="checkwash-prove-index-") as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(tmp, "index"))
        # --work-tree lets a bare repository check out too; --prefix decides where the files go.
        _git(repo, f"--work-tree={tmp}", "read-tree", rev, env=env)
        if paths is None:
            _git(repo, f"--work-tree={tmp}", "checkout-index", "-a", "-f", f"--prefix={dest.as_posix()}/", env=env)
        elif paths:
            _git(repo, f"--work-tree={tmp}", "checkout-index", "-f", "-z", "--stdin", f"--prefix={dest.as_posix()}/",
                 env=env, input=b"\0".join(p.encode("utf-8") for p in paths))


# ---- run trees -----------------------------------------------------------------------------------------------------

def _scan(root: pathlib.Path) -> tuple[dict[str, tuple[int, int]], set[str]]:
    files: dict[str, tuple[int, int]] = {}
    dirs: set[str] = set()
    for current, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(current, root)
        keep = []
        for name in dirnames:
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if os.path.islink(os.path.join(current, name)):
                st = os.lstat(os.path.join(current, name))
                files[rel] = (st.st_size, st.st_mtime_ns)
            else:
                dirs.add(rel)
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            st = os.lstat(os.path.join(current, name))
            files[os.path.normpath(os.path.join(rel_dir, name))] = (st.st_size, st.st_mtime_ns)
    return files, dirs


def _remove(path: pathlib.Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
        return
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    path.unlink(missing_ok=True)


class Tree:
    """A copy of a pristine tree, put back to that pristine state before every run in it."""

    def __init__(self, pristine: pathlib.Path, path: pathlib.Path):
        shutil.copytree(pristine, path, symlinks=True)
        self.pristine, self.path = pristine, path
        self.files, self.dirs = _scan(path)

    def _put_back(self) -> None:
        files, dirs = _scan(self.path)
        for rel, sig in files.items():
            if self.files.get(rel) != sig:
                _remove(self.path / rel)
        for rel in sorted(dirs - self.dirs, key=lambda r: r.count(os.sep), reverse=True):
            shutil.rmtree(self.path / rel, ignore_errors=True)
        for rel, sig in self.files.items():
            if files.get(rel) != sig:
                target = self.path / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    _remove(target)
                shutil.copy2(self.pristine / rel, target, follow_symlinks=False)

    def reset(self, mutant: Candidate | None = None) -> None:
        # Checked, not assumed: a process a test left behind can hold a file or write one back.
        for attempt in range(20):
            try:
                self._put_back()
            except OSError:
                pass
            if _scan(self.path) == (self.files, self.dirs):
                break
            time.sleep(0.25)
        else:
            files, dirs = _scan(self.path)
            stray = sorted({rel for rel in files if self.files.get(rel) != files[rel]} | (dirs - self.dirs)
                           | {rel for rel in self.files if rel not in files})
            raise ProveError(f"could not put {self.path} back to its pristine state ({', '.join(stray[:5])}): a process "
                             "the tests started may still be running")
        if mutant is not None:
            (self.path / mutant.path).write_bytes(mutant.apply((self.pristine / mutant.path).read_bytes()))


def junit_key(nodeid: str) -> tuple[str, str]:
    """The (classname, name) pytest's JUnit report gives a node id."""
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


# ---- processes -----------------------------------------------------------------------------------------------------

class _Job:
    """A Windows job object: the run's process is started suspended and put in it before it can start anything, so
    every process it starts, however deep, can be ended with it."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateJobObjectW.restype = wintypes.HANDLE
        self._k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._k32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._k32.TerminateJobObject.restype = wintypes.BOOL
        self._k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._k32.QueryInformationJobObject.restype = wintypes.BOOL
        self._k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                        wintypes.DWORD, ctypes.c_void_p]
        self._k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._ntdll = ctypes.WinDLL("ntdll")
        self._ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self.handle = self._k32.CreateJobObjectW(None, None)
        self.adopted = False

    def adopt_and_resume(self, proc: subprocess.Popen) -> None:
        handle = int(proc._handle)    # noqa: SLF001 - Popen keeps the process handle; the thread handle it closed
        if self.handle:
            self.adopted = bool(self._k32.AssignProcessToJobObject(self.handle, handle))
        self._ntdll.NtResumeProcess(handle)

    def kill(self) -> None:
        """End every process in the job, and wait (up to 10 s) until none is left: termination is asynchronous."""
        if not (self.handle and self.adopted):
            return
        import ctypes

        self._k32.TerminateJobObject(self.handle, 1)
        info = (ctypes.c_uint32 * 12)()     # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: 4 LARGE_INTEGERs, 4 DWORDs
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not self._k32.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                return
            if info[10] == 0:               # ActiveProcesses
                return
            time.sleep(0.05)

    def close(self) -> None:
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None


def _start(cmd: list[str], **kwargs) -> tuple[subprocess.Popen, _Job | None]:
    """Start a run in its own process group (POSIX: its own session; Windows: a new process group in a job)."""
    if os.name != "nt":
        return subprocess.Popen(cmd, start_new_session=True, **kwargs), None
    create_suspended = 0x00000004
    proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | create_suspended, **kwargs)
    job = None
    try:
        job = _Job()
        job.adopt_and_resume(proc)
    except (OSError, AttributeError, ValueError):
        # Without a job the run must still start; a timeout then falls back to taskkill.
        import ctypes

        ctypes.WinDLL("ntdll").NtResumeProcess(int(proc._handle))   # noqa: SLF001
    return proc, job


def _end(proc: subprocess.Popen, job: _Job | None) -> None:
    """End the run's process and everything it started."""
    if os.name == "nt":
        if job is not None and job.adopted:
            job.kill()
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _wait(proc: subprocess.Popen, budget: float) -> int:
    """proc.wait(budget) in short slices, so Ctrl-C reaches Python on Windows too."""
    deadline = time.monotonic() + budget
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, budget)
        try:
            return proc.wait(timeout=min(remaining, 0.5))
        except subprocess.TimeoutExpired:
            continue


# ---- pytest runs ---------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class RunResult:
    code: int | str          # pytest exit code, or "timeout"
    outcomes: dict           # (classname, name) -> "passed" | "failed" | "skipped"
    messages: dict           # (classname, name) -> the line that says why it failed
    tail: str = ""
    seconds: float = 0.0
    rootdir: pathlib.Path | None = None

    @property
    def usable(self) -> bool:
        """A clean run to compare against: pytest finished its tests (exit 0 or 1) and wrote results."""
        return self.code in (0, 1) and bool(self.outcomes)


def _junit(path: pathlib.Path) -> tuple[dict, dict]:
    outcomes, messages = {}, {}
    if not path.exists():
        return outcomes, messages
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return outcomes, messages
    for case in root.iter("testcase"):
        key = (case.get("classname") or "", case.get("name") or "")
        state, message = "passed", ""
        for child in case:
            if child.tag in ("failure", "error"):
                state = "failed"
                message = _failure_line(child.get("message") or "", child.text or "")
            elif child.tag == "skipped" and state == "passed":
                state = "skipped"
        outcomes[key], messages[key] = state, message
    return outcomes, messages


def _failure_line(message: str, text: str) -> str:
    """The line that says why: the message, or for a collection error the exception line of its traceback."""
    first = message.strip().splitlines()[:1]
    if first and first[0] != "collection failure":
        return first[0][:300]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    errors = [ln[1:].strip() for ln in lines if ln.startswith("E ")]
    return (errors[-1] if errors else lines[-1] if lines else (first[0] if first else ""))[:300]


_XDIST_OPTION = re.compile(r"(?<![\w-])(-n|--numprocesses|--dist)(?![\w-])|(?<![\w-])-n\d")
_COV_OPTION = re.compile(r"(?<![\w-])--cov(?![\w-])")


class Runner:
    def __init__(self, opts: Options, import_root: str, work: pathlib.Path):
        self.opts, self.import_root = opts, import_root
        self.plugin_dir, self.scratch = work / "plugin", work / "scratch"
        self.plugin_dir.mkdir()
        self.scratch.mkdir()
        (self.plugin_dir / f"{PLUGIN}.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
        self.empty_ini = self.plugin_dir / "pytest.ini"
        self.empty_ini.write_text("[pytest]\n", encoding="utf-8")
        self.plugins: dict = {}
        self._n = 0
        self._lock = threading.Lock()
        self._live: dict[subprocess.Popen, _Job | None] = {}
        self.stopping = threading.Event()

    def _next(self) -> int:
        with self._lock:
            self._n += 1
            return self._n

    def env(self, tree: pathlib.Path, trace_out: pathlib.Path | None = None, only: pathlib.Path | None = None,
            collect_out: pathlib.Path | None = None) -> dict[str, str]:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        # The project's own configuration decides how its tests run, not options from the caller's shell.
        env.pop("PYTEST_ADDOPTS", None)
        for key in ("CHECKWASH_PROVE_TRACE_OUT", "CHECKWASH_PROVE_TRACE_ROOT", "CHECKWASH_PROVE_ONLY",
                    "CHECKWASH_PROVE_COLLECT_OUT"):
            env.pop(key, None)
        roots = [str(tree / self.import_root), str(self.plugin_dir)]
        if os.environ.get("PYTHONPATH"):
            roots.append(os.environ["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(roots)
        if trace_out is not None:
            env["CHECKWASH_PROVE_TRACE_ROOT"] = str(tree)
            env["CHECKWASH_PROVE_TRACE_OUT"] = str(trace_out)
        if only is not None:
            env["CHECKWASH_PROVE_ONLY"] = str(only)
        if collect_out is not None:
            env["CHECKWASH_PROVE_COLLECT_OUT"] = str(collect_out)
        return env

    def pytest(self, tree: pathlib.Path, files: list[str], budget: float, *, old_side: bool,
               trace_out: pathlib.Path | None = None, collect_out: pathlib.Path | None = None) -> RunResult:
        if self.stopping.is_set():
            raise Stopped()
        n = self._next()
        junit, log = self.scratch / f"run-{n}.xml", self.scratch / f"run-{n}.log"
        args, only = list(files), None
        # The configuration is the one pytest would use for the files themselves, however they are passed.
        config = locate_config(tree, args)
        if sum(len(a) + 1 for a in args) > ARGS_LIMIT:
            only = self.scratch / f"only-{n}.txt"
            only.write_text("".join(os.path.realpath(tree / a) + "\n" for a in args), encoding="utf-8")
            args = sorted({a.split("/", 1)[0] for a in args})
        # A cache of the run's own, so --lf, --ff or --sw in the project's addopts still parse, and nothing is
        # written into the tree.
        cmd = [self.opts.python, "-m", "pytest", "-p", PLUGIN, "-o", f"cache_dir={self.scratch / f'cache-{n}'}",
               "-p", "no:randomly", "-q", "--continue-on-collection-errors", f"--junitxml={junit}",
               f"--confcutdir={tree}"]
        if only is not None:
            # pytest 7 enforces norecursedirs apart from pytest_ignore_collect; the plugin decides instead.
            cmd += ["-o", "norecursedirs="]
        if collect_out is not None:
            cmd += ["--collect-only"]
        # Never a configuration file or conftest.py from above the tree: that is the temporary directory's parent.
        cmd += ["-c", str(config)] if config else ["-c", str(self.empty_ini), f"--rootdir={tree}"]
        # In-process and uncovered, where the project asks otherwise: a distributed run hides executed lines, and so
        # does a coverage tracer.
        options = addopts(config)
        if self.plugins.get("xdist") and _XDIST_OPTION.search(options):
            cmd += ["-n", "0"]
        if self.plugins.get("pytest_cov") and _COV_OPTION.search(options):
            cmd += ["--no-cov"]
        if old_side and not self.opts.strict_warnings:
            cmd += ["-W", "ignore::DeprecationWarning"]
        started = time.monotonic()
        with open(log, "wb") as out:
            proc, job = _start(cmd + args, cwd=tree, env=self.env(tree, trace_out, only, collect_out), stdout=out,
                               stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            with self._lock:
                self._live[proc] = job
            try:
                code: int | str = _wait(proc, budget)
            except subprocess.TimeoutExpired:
                code = "timeout"
            except BaseException:
                _end(proc, job)
                proc.wait()
                raise
            finally:
                # Whatever the tests started and left running ends with the run.
                _end(proc, job)
                proc.wait()
                if job is not None:
                    job.close()
                with self._lock:
                    self._live.pop(proc, None)
        seconds = time.monotonic() - started
        outcomes, messages = ({}, {}) if code == "timeout" else _junit(junit)
        tail = log.read_bytes().decode("utf-8", "replace").strip().splitlines()[-15:]
        return RunResult(code, outcomes, messages, "\n".join(tail), seconds, config.parent if config else tree)

    def stop(self) -> None:
        self.stopping.set()
        with self._lock:
            live = list(self._live.items())
        for proc, job in live:
            _end(proc, job)


def budget(clean: RunResult, floor: float) -> float:
    return round(max(floor, TIMEOUT_FACTOR * clean.seconds + TIMEOUT_MARGIN), 1)


def _killed(clean: RunResult, run: RunResult) -> list[str] | None:
    """What shows that a run caught the mutant, as CI would see it; None when it did not.

    A result that fails or errors now and did not on the clean code is a catch: a test that passed, a test module
    that no longer collects, a test that was skipped. So is a timeout, given a budget well over the clean run's time.
    A skip is not: CI passes a skipped test. A test module that did not collect on the clean code catches nothing:
    that part of the suite was red already, and a mutant that lets it import only trades one failure for another.
    Without a catch so far, a run that wrote no results where the clean run had passes, or that ends with a nonzero
    exit the clean run did not have, is a catch too: CI fails it, though no single test is to blame.
    """
    if run.code == "timeout":
        return ["(timeout)"]
    red = {name for (cls, name), state in clean.outcomes.items() if not cls and state == "failed"}

    def in_red_module(cls: str) -> bool:
        return any(cls == module or cls.startswith(module + ".") for module in red)

    fresh = sorted(k for k, v in run.outcomes.items()
                   if v == "failed" and clean.outcomes.get(k) != "failed" and not (k[0] and in_red_module(k[0])))
    if fresh:
        return [f"{c}::{n}" if c else n for c, n in fresh]
    if not run.outcomes and any(v == "passed" for v in clean.outcomes.values()):
        return [f"(pytest exit {run.code}, no results)"]
    if run.code != 0 and run.code != clean.code:
        return [f"(pytest exit {run.code})"]
    return None


def _executed(trace_out: pathlib.Path, tree: pathlib.Path,
              clean: RunResult) -> tuple[dict[str, set[int]], dict[str, set[int]]]:
    """(good, all): lines run outside tests or by tests that passed on the clean, untraced run; and every line run.
    A test that fails only under the tracer (timing, coverage) still counts as passing."""
    if not trace_out.exists():
        return {}, {}
    real_tree = os.path.realpath(tree)
    root = os.path.normcase(real_tree) + os.sep
    data = json.loads(trace_out.read_text(encoding="utf-8"))
    places: dict[str, str | None] = {}

    def add(target: dict[str, set[int]], part: dict) -> None:
        for name, lines in part.items():
            if name not in places:
                full = os.path.realpath(name)
                places[name] = (os.path.relpath(full, real_tree).replace(os.sep, "/")
                                if os.path.normcase(full).startswith(root) else None)
            if places[name]:
                target.setdefault(places[name], set()).update(lines)

    good: dict[str, set[int]] = {}
    every: dict[str, set[int]] = {}
    add(good, data.get("outside", {}))
    add(every, data.get("outside", {}))
    for nodeid, part in data.get("tests", {}).items():
        add(every, part)
        if clean.outcomes.get(junit_key(nodeid)) == "passed":
            add(good, part)
    return good, every


@dataclasses.dataclass
class Collected:
    """What a tree's own whole-suite collection holds: test files (relative to the tree), JUnit keys, modules."""
    files: set[str]
    keys: set[tuple[str, str]]
    modules: set[str]


def _collect(runner: Runner, tree: Tree, opts: Options, *, old_side: bool) -> Collected | None:
    out = runner.scratch / f"collect-{'old' if old_side else 'new'}.json"
    tree.reset()
    result = runner.pytest(tree.path, [], opts.suite_timeout, old_side=old_side, collect_out=out)
    if not out.exists():
        return None
    nodeids = json.loads(out.read_text(encoding="utf-8"))
    prefix = os.path.relpath(result.rootdir or tree.path, tree.path).replace(os.sep, "/")
    prefix = "" if prefix == "." else prefix + "/"
    return Collected({prefix + n.split("::")[0] for n in nodeids}, {junit_key(n) for n in nodeids},
                     {re.sub(r"\.py$", "", n.split("::")[0].replace("/", ".")) for n in nodeids})


def _in_suite(caught: list[str] | None, collected: Collected | None) -> list[str] | None:
    """Only a test the project's own collection holds can catch for it: a test module it excludes (collect_ignore,
    --ignore, a deselection) protects nothing in CI, though prove named it on the command line."""
    if caught is None or collected is None:
        return caught
    kept = []
    for test in caught:
        cls, name = _split(test)
        if test.startswith("(") or (not cls and name in collected.modules) or (cls, name) in collected.keys:
            kept.append(test)
    return kept or None


def node_id(tree: pathlib.Path, rootdir: pathlib.Path | None, classname: str, name: str) -> str:
    """Best-effort pytest node id, relative to the tree, for a JUnit (classname, name) pair; a module's own entry
    (a collection error) has no classname and the module's dotted name."""
    base = rootdir or tree
    prefix = os.path.relpath(base, tree).replace(os.sep, "/")
    prefix = "" if prefix == "." else prefix + "/"
    if not classname:
        path = name.replace(".", "/") + ".py"
        return prefix + path if (base / path).is_file() else name
    parts = classname.split(".")
    for i in range(len(parts), 0, -1):
        path = "/".join(parts[:i]) + ".py"
        if (base / path).is_file():
            return "::".join([prefix + path, *parts[i:], name])
    return f"{classname}::{name}"


def _split(test: str) -> tuple[str, str]:
    """Undo _killed's "classname::name" (a dotted classname never holds "::"; a parametrized name may)."""
    classname, sep, name = test.partition("::")
    return (classname, name) if sep else ("", test)


# ---- mutants -------------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class Candidate:
    mid: str
    path: str
    op: str
    line: int
    before: str
    after: str
    what: str
    start: int           # the mutant is the file with bytes start..end replaced by `replacement`
    end: int
    replacement: bytes
    priority: int

    def apply(self, data: bytes) -> bytes:
        return data[:self.start] + self.replacement + data[self.end:]


def _common_prefix(a: bytes, b: bytes) -> int:
    lo, hi = 0, min(len(a), len(b))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _splice(data: bytes, mutated: bytes) -> tuple[int, int, bytes]:
    start = _common_prefix(data, mutated)
    lo, hi = 0, min(len(data), len(mutated)) - start
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if data[len(data) - mid:] == mutated[len(mutated) - mid:]:
            lo = mid
        else:
            hi = mid - 1
    end = len(data) - lo
    replacement = mutated[start:len(mutated) - lo]
    assert data[:start] + replacement + data[end:] == mutated
    return start, end, replacement


def file_mutants(data: bytes, lines: set[int]) -> list[dict]:
    """The catalogue's mutants on the given lines of a file. A mutant's id names its position and operator,
    `line:column:OP:n` (n counts the mutants one operator makes at one node), so it does not depend on which other
    lines a run traced; only a splice of each is kept, not a copy of the file."""
    muts, _ = mutops.generate(data, lines)
    seen: dict[tuple[str, int, int], int] = {}
    out = []
    for m in muts:
        key = (m["op"], m["line"], m["col"])
        seen[key] = seen.get(key, 0) + 1
        start, end, replacement = _splice(data, m.pop("source"))
        out.append({**m, "id": f"{m['line']}:{m['col']}:{m['op']}:{seen[key]}", "start": start, "end": end,
                    "replacement": replacement})
    return out


def _candidates(tree, import_root, old_exec, new_exec, changed, report) -> list[Candidate]:
    """Catalogue mutants on the product lines the old tests executed, most telling first: lines only the old tests
    executed, then lines the range added or replaced, then the rest of the files it changed, then everything else, and
    last the lines that only old tests which do not pass on the new code ran; within each rank, one mutant from each
    file in turn."""
    old_good, old_all = old_exec
    new_all = new_exec[1]
    prefix = "" if import_root == "." else import_root + "/"
    out: list[Candidate] = []
    for rel in sorted(old_all):
        if not rel.startswith(prefix) or not rel.endswith(".py") or is_test_path(rel) or is_config_name(rel):
            continue
        try:
            muts = file_mutants((tree / rel).read_bytes(), old_all[rel])
        except (SyntaxError, ValueError, OSError) as exc:
            report["notes"].append(f"{rel}: not mutated ({exc})")
            continue
        good = old_good.get(rel, set())
        only_old = good - new_all.get(rel, set())
        touched = changed.get(rel)
        for m in muts:
            line = m["line"]
            priority = (RANK_ONLY_FAILING if line not in good else 0 if line in only_old
                        else 1 if touched and line in touched else 2 if touched is not None else 3)
            out.append(Candidate(f"{rel}:{m['id']}", rel, m["op"], line, m["before"], m["after"], m["what"],
                                 m["start"], m["end"], m["replacement"], priority))
    out.sort(key=lambda c: (c.priority, c.path, c.line))     # stable: on a line, the catalogue's operator order
    turns: dict[tuple[int, str], int] = {}
    keyed = []
    for c in out:
        turn = turns[(c.priority, c.path)] = turns.get((c.priority, c.path), 0) + 1
        keyed.append(((c.priority, turn, c.path), c))
    keyed.sort(key=lambda pair: pair[0])
    return [c for _, c in keyed]


# ---- the procedure -------------------------------------------------------------------------------------------------

def _probe(runner: Runner, tree: pathlib.Path, import_root: str) -> tuple[list[str], list[str], dict]:
    """(problems, notes, plugins): a product package that imports from outside the tree is a problem; one that does
    not import at all is a note, since every old test that needs it will then fail for want of a dependency."""
    root = tree / import_root
    names = sorted(p.name for p in root.iterdir()
                   if p.is_dir() and (p / "__init__.py").is_file() and p.name.lower() not in TEST_DIRS)[:32]
    out = runner.scratch / "probe.json"
    try:
        subprocess.run([runner.opts.python, "-c", PROBE_SOURCE, str(out), *names], cwd=tree, env=runner.env(tree),
                       capture_output=True, timeout=300, stdin=subprocess.DEVNULL)
        found = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return [f"could not run {runner.opts.python} to check the product imports ({exc})"], [], {}
    problems, notes = [], []
    prefix = os.path.normcase(os.path.realpath(tree)) + os.sep
    for name, path in found["packages"].items():
        if path and str(path).startswith("error:"):
            notes.append(f"package {name!r} does not import with this interpreter ({str(path)[7:].strip()[:200]}); "
                         "tests that need it fail for that reason, not because of the change")
        elif path and not os.path.normcase(os.path.realpath(path)).startswith(prefix):
            problems.append(f"package {name!r} imports from {path}, not from the checked-out tree")
    return problems, notes, found.get("plugins", {})


def prove(repo: str, spec: str, opts: Options) -> dict:
    digest = verify_catalogue()
    parsed = parse_range(spec)
    if parsed is None:
        raise ProveError(f"prove needs a range BASE..HEAD or BASE...HEAD, got {spec!r}")
    base_rev, head_rev, three = parsed
    head = resolve(repo, head_rev)
    if three:
        found = _git(repo, "merge-base", resolve(repo, base_rev), head, ok=(0, 1)).decode().split()
        if not found:
            raise ProveError(f"{base_rev} and {head_rev} have no merge base")
        base = found[0]
    else:
        base = resolve(repo, base_rev)
    changes = changed_paths(repo, base, head)
    report: dict = {
        "tool": "checkwash-prove", "experimental": True,
        "catalogue": {"version": CATALOGUE_VERSION, "generator_sha256": digest},
        "range": spec, "base": base, "head": head, "from_merge_base": three, "python": opts.python,
        "options": {k: v for k, v in dataclasses.asdict(opts).items() if k not in ("python", "replay")},
        "needs_clarification": [], "protection_losses": [], "unconfirmed_losses": [], "notes": [], "mutation": {},
        "compared": False, "nothing_to_compare": False,
    }
    # Exit 1 is "not an ancestor"; any other failure (history missing in a shallow clone) proves nothing either way.
    if not three and _git_code(repo, "merge-base", "--is-ancestor", base, head) == 1:
        report["notes"].append(f"{base_rev} is not an ancestor of {head_rev}, so the range also undoes what changed "
                               f"on {base_rev}'s side since they diverged; for a branch, {base_rev}...{head_rev} "
                               "compares from the merge base, as checkwash check does")
    submodules = sorted(c.path for c in changes if c.gitlink)
    if submodules:
        report["notes"].append("submodules are not compared: " + ", ".join(submodules))
    suite = _suite_changes(repo, base, head, [c for c in changes if not c.gitlink])
    modules = [c for c in suite if is_test_module(c.path)]
    others = [c for c in suite if not is_test_module(c.path)]
    old_tests = sorted(c.path for c in modules if c.status != "A")
    new_tests = sorted(c.path for c in modules if c.status != "D")
    scoped = not modules and bool(others)
    if scoped:
        dirs = sorted({scope_dir(c.path) for c in others})
        old_tests = sorted(p for p in tree_files(repo, base, dirs) if is_test_module(p))
        new_tests = sorted(p for p in tree_files(repo, head, dirs) if is_test_module(p))
        report["notes"].append("no test module changed; the changed test files ("
                               + ", ".join(c.path for c in others) + ") are compared through the test modules under "
                               + ", ".join(d or "the repository root" for d in dirs))
    elif others:
        report["notes"].append("changed test helpers, data or configuration (" + ", ".join(c.path for c in others)
                               + "): test modules the range did not change are not compared, though some may use "
                                 "them")
    product = sorted(c.path for c in changes if not c.gitlink and c.status != "D" and c.path.endswith(".py")
                     and not is_test_path(c.path))
    report.update({"tests": {"old": old_tests, "new": new_tests}, "test_suite_files_changed": [c.path for c in suite],
                   "product_files_changed": product})
    if not suite:
        report.update(status="nothing to compare: the range changes no test file", nothing_to_compare=True)
        return report
    if not old_tests:
        report.update(status="nothing to compare: no test module that existed before the change is affected",
                      nothing_to_compare=True)
        return report

    work = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix="checkwash-prove-")))
    report["workdir"] = str(work) if opts.keep else None
    runner = None
    try:
        runner = Runner(opts, _import_root(opts, repo, head), work)
        return _prove_in(work, runner, repo, base, head, opts, suite, old_tests, new_tests, product, report,
                         scoped)
    except KeyboardInterrupt:
        if runner is not None:
            runner.stop()
        raise
    finally:
        if not opts.keep:
            shutil.rmtree(work, ignore_errors=True)
            if work.exists():
                sys.stderr.write(f"checkwash-prove: could not remove {work}\n")


def _suite_changes(repo: str, base: str, head: str, changes: list[Change]) -> list[Change]:
    """The changed files of the test suite: test files, and configuration files whose pytest settings changed."""
    out = []
    for c in changes:
        if is_test_path(c.path):
            out.append(c)
        elif is_config_name(c.path):
            name = c.path.rsplit("/", 1)[-1]
            if pytest_settings(name, blob(repo, base, c.path)) != pytest_settings(name, blob(repo, head, c.path)):
                out.append(c)
    return out


def normalise_root(root: str) -> str:
    """`src`, `./src/` and `src\\` all name the same directory."""
    root = root.replace("\\", "/").strip()
    while root.startswith("./"):
        root = root[2:]
    return root.strip("/") or "."


def _import_root(opts: Options, repo: str, head: str) -> str:
    if opts.import_root:
        return normalise_root(opts.import_root)
    return "src" if tree_files(repo, head, ["src"]) else "."


def _prove_in(work, runner, repo, base, head, opts, suite, old_tests, new_tests, product, report, scoped=False):
    started = time.monotonic()
    pristine_new, pristine_old = work / "pristine" / "new", work / "pristine" / "old"
    checkout(repo, head, pristine_new)
    shutil.copytree(pristine_new, pristine_old, symlinks=True)
    for c in suite:
        if c.status == "A":                   # only in HEAD: not part of the old suite
            _remove(pristine_old / c.path)
    checkout(repo, base, pristine_old, [c.path for c in suite if c.status != "A"])
    if not (pristine_new / runner.import_root).is_dir():
        raise ProveError(f"the import root {runner.import_root!r} is not a directory of {head[:12]}; "
                         "pass --import-root")
    # The directory's own spelling: `SRC` names `src` on Windows, and the traced paths spell it `src`.
    runner.import_root = os.path.relpath(os.path.realpath(pristine_new / runner.import_root),
                                         os.path.realpath(pristine_new)).replace(os.sep, "/")
    import_root = report["import_root"] = runner.import_root
    clean_old, clean_new = Tree(pristine_old, work / "clean" / "old"), Tree(pristine_new, work / "clean" / "new")

    problems, notes, runner.plugins = _probe(runner, clean_new.path, import_root)
    report["notes"].extend(notes)
    if problems:
        raise ProveError("; ".join(problems) + ". Something in this interpreter imports another copy first; use an "
                         "environment without it (--python), or pass --import-root.")

    # What each side's own whole-suite collection holds: prove names test modules on the command line, which would
    # otherwise count modules the project excludes (collect_ignore, --ignore, a deselection) as protection.
    collected_old = _collect(runner, clean_old, opts, old_side=True)
    collected_new = _collect(runner, clean_new, opts, old_side=False)
    for side, collected in (("old", collected_old), ("new", collected_new)):
        if collected is None:
            report["notes"].append(f"the {side} tree's own test collection could not be read, so test modules it "
                                   "may exclude count as protection")
    if scoped:
        # Through a helper, data or configuration file: only the modules each side's own run collects.
        if collected_old is not None:
            old_tests = [t for t in old_tests if t in collected_old.files]
        if collected_new is not None:
            new_tests = [t for t in new_tests if t in collected_new.files]
        report["tests"] = {"old": old_tests, "new": new_tests}
        if not old_tests:
            report.update(status="nothing to compare: no test module that existed before the change is affected",
                          nothing_to_compare=True)
            return report

    # Clean runs to compare against, untraced as the mutant runs are; tracing slows tests that measure time.
    clean_old.reset()
    old_clean = runner.pytest(clean_old.path, old_tests, opts.suite_timeout, old_side=True)
    if new_tests:
        clean_new.reset()
        new_clean = runner.pytest(clean_new.path, new_tests, opts.suite_timeout, old_side=False)
    else:
        new_clean = RunResult(0, {}, {})
    if not opts.strict_warnings:
        report["notes"].append("old tests ran with -W ignore::DeprecationWarning (a deprecated name still works); "
                               "use --strict-warnings to keep the project's warning settings")
    if not old_clean.usable:
        report["status"] = f"old tests do not run on the new code (pytest exit {old_clean.code}): {old_clean.tail[-300:]}"
        return report
    if new_tests and not new_clean.usable:
        report["status"] = f"new tests do not run on the new code (pytest exit {new_clean.code}): {new_clean.tail[-300:]}"
        return report
    for key, state in sorted(old_clean.outcomes.items()):
        if state == "failed":
            also_new = new_clean.outcomes.get(key) == "failed"
            if not key[0]:
                reason = "this old test module cannot be collected on the new code"
            elif also_new:
                # The same test fails in its new version too: more likely the environment or a defect than a
                # deliberate change of behaviour, but still a person's call.
                reason = "this old test fails on the new code, and so does its new version"
            else:
                reason = "this old test fails on the new code: the behaviour changed"
            report["needs_clarification"].append({
                "test": node_id(clean_old.path, old_clean.rootdir, *key), "reason": reason,
                "also_fails_in_new": also_new, "detail": old_clean.messages.get(key, "")})
    new_failing = [node_id(clean_new.path, new_clean.rootdir, *k) for k, v in new_clean.outcomes.items() if v == "failed"]
    if new_failing:
        report["notes"].append(f"{len(new_failing)} new test(s) already fail on the new code and are not compared: "
                               + ", ".join(new_failing[:5]))
    if not any(v == "passed" for v in old_clean.outcomes.values()):
        report["status"] = "no old test passes on the new code; nothing to mutate"
        return report
    # prove takes a pytest run at the repository root as the project's suite. Old tests that run does not collect
    # protected nothing there, unless the project runs them some other way (a configuration of their own).
    root_config = locate_config(clean_old.path, [])
    own = [t for t in old_tests if locate_config(clean_old.path, [t]) != root_config]
    if own:
        report["notes"].append("test modules with a pytest configuration of their own (" + ", ".join(own[:5])
                               + "): prove takes a pytest run at the repository root as the suite, and counts only "
                                 "the tests that run collects")
    if collected_old is not None and not any(k in collected_old.keys for k in old_clean.outcomes if k[0]):
        if own:
            report["status"] = ("not compared: the changed old tests have a pytest configuration of their own, and "
                                "a pytest run at the repository root, which prove takes as the suite, does not "
                                "collect them")
        else:
            report.update(status="nothing to compare: the old suite (a pytest run at the repository root) does not "
                                 "collect the changed old tests, so they protected nothing", nothing_to_compare=True)
        return report

    # Traced runs, only to learn which lines the tests execute. Tracing is slow: they get ten times the clean run.
    old_trace, new_trace = runner.scratch / "old-trace.json", runner.scratch / "new-trace.json"
    traced = [("old", clean_old, old_tests, old_clean, old_trace, True)]
    if new_tests:
        traced.append(("new", clean_new, new_tests, new_clean, new_trace, False))
    for side, tree, tests, clean, trace_out, old_side in traced:
        tree.reset()
        result = runner.pytest(tree.path, tests, max(opts.suite_timeout, 10 * clean.seconds + 60), old_side=old_side,
                               trace_out=trace_out)
        if result.code == "timeout" or not trace_out.exists():
            report["status"] = (f"the traced run of the {side} tests did not finish (pytest exit {result.code}), so "
                                "no product line is known; raise --suite-timeout if it timed out")
            return report
    old_exec = _executed(old_trace, clean_old.path, old_clean)
    new_exec = _executed(new_trace, clean_new.path, new_clean)
    changed = changed_lines(repo, base, head, product)
    candidates = _candidates(pristine_new, import_root, old_exec, new_exec, changed, report)
    if not candidates:
        # Zero mutants is not zero losses: nothing was tested.
        report["mutation"] = {"generated": 0, "run": 0, "skipped_over_cap": 0}
        report["status"] = ("no mutants: the old tests executed no product line under the import root that the "
                            "catalogue mutates, so nothing was compared")
        return report
    if opts.only:
        candidates = [c for c in candidates if c.mid == opts.only]
        if not candidates:
            raise ProveError(f"no mutant has id {opts.only!r} for this range")
    generated, cap = len(candidates), max(0, opts.max_mutants)
    if opts.list_only:
        report["mutation"] = {"generated": generated, "run": 0, "candidates": [
            {"id": c.mid, "rank": c.priority, "within_cap": i < cap, "path": c.path, "line": c.line, "op": c.op,
             "before": c.before, "after": c.after, "what": c.what} for i, c in enumerate(candidates)]}
        report["status"] = "listed: no mutant was run"
        return report
    candidates = candidates[:cap]
    report["mutation"] = {"generated": generated, "run": len(candidates), "skipped_over_cap": generated - len(candidates)}
    if not candidates:
        report["status"] = "no mutant was run (--max-mutants 0)"
        return report
    if opts.workers > 1 and len(candidates) > 1:
        report["notes"].append(f"mutants ran side by side (--workers {opts.workers}): tests that share ports, files "
                               "outside the repository or other machine-wide state can interfere, and a catch that "
                               "interference causes can hide a loss; losses are always run again alone, and "
                               "--workers 1 rules interference out")

    budgets = {"old": budget(old_clean, opts.timeout), "new": budget(new_clean, opts.timeout)}
    report["budgets"] = budgets
    suite_lock = threading.Lock()
    suite_clean: list[RunResult] = []

    def baseline() -> RunResult:
        with suite_lock:
            if not suite_clean:
                clean_new.reset()
                suite_clean.append(runner.pytest(clean_new.path, [], opts.suite_timeout, old_side=False))
                budgets["suite"] = budget(suite_clean[0], opts.timeout)
            return suite_clean[0]

    workers = max(1, min(opts.workers, len(candidates)))
    free = [(Tree(pristine_old, work / f"w{i}" / "old"), Tree(pristine_new, work / f"w{i}" / "new"))
            for i in range(workers)]
    free_lock = threading.Lock()

    def run_one(c: Candidate) -> dict:
        with free_lock:
            o, n = free.pop()
        try:
            return _compare(runner, c, o, n, old_tests, new_tests, old_clean, new_clean, baseline, budgets,
                            (collected_old, collected_new))
        finally:
            with free_lock:
                free.append((o, n))

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(run_one, c) for c in candidates]
        pending = set(futures)
        while pending:      # a short wait keeps Ctrl-C responsive on every platform
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_EXCEPTION)
            for f in done:
                f.result()
        rows = [f.result() for f in futures]
    except BaseException:
        runner.stop()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    _confirm(runner, rows, candidates, clean_old, clean_new, old_tests, new_tests, opts, budgets,
             (collected_old, collected_new))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    report["mutation"]["categories"] = counts
    report["mutation"]["rows"] = rows
    if counts.get("undecided"):
        report["notes"].append(f"{counts['undecided']} mutant(s) only the old tests catch are undecided: the whole "
                               f"new suite gave no usable clean run (pytest exit {baseline().code}; for a timeout, "
                               "raise --suite-timeout)")
    replay = _replay_prefix(opts, repo, base, head)
    for row in rows:
        if row["category"] in ("loss", "unconfirmed loss"):
            entry = {**{k: row[k] for k in ("id", "op", "path", "line", "before", "after", "what")},
                     "old_caught_by": [node_id(clean_old.path, old_clean.rootdir, *_split(t))
                                       for t in row["old_caught_by"]],
                     "replay": f"{replay} --only {_quote([row['id']])}"}
            if row["category"] == "loss":
                report["protection_losses"].append(entry)
            else:
                report["unconfirmed_losses"].append({**entry, "why": row["confirmation"]})
    if report["unconfirmed_losses"]:
        report["notes"].append(f"{len(report['unconfirmed_losses'])} loss(es) did not repeat when run again one at a "
                               "time, so they are not reported as losses (a flaky test, or tests that interfere "
                               "when run side by side); each has its replay command")
    report["status"] = "done"
    report["compared"] = True
    report["seconds"] = round(time.monotonic() - started, 1)
    return report


def _compare(runner, c, o, n, old_tests, new_tests, old_clean, new_clean, baseline, budgets,
             collected=(None, None)) -> dict:
    o.reset(c)
    old_k = _in_suite(_killed(old_clean, runner.pytest(o.path, old_tests, budgets["old"], old_side=True)),
                      collected[0])
    new_k = None
    if new_tests:
        n.reset(c)
        new_k = _in_suite(_killed(new_clean, runner.pytest(n.path, new_tests, budgets["new"], old_side=False)),
                          collected[1])
    row = {"id": c.mid, "op": c.op, "path": c.path, "line": c.line, "what": c.what, "before": c.before,
           "after": c.after, "rank": c.priority, "old_caught_by": old_k, "new_caught_by": new_k}
    row["category"] = {(True, True): "both", (True, False): "old only", (False, True): "new only",
                       (False, False): "neither"}[(old_k is not None, new_k is not None)]
    if old_k is not None and new_k is None:
        clean_suite = baseline()
        if not clean_suite.usable:
            # Without a usable clean whole-suite run there is nothing to compare against: not a loss, not a catch.
            row["suite_caught_by"] = None
            row["category"] = "undecided"
        else:
            n.reset(c)
            caught = _killed(clean_suite, runner.pytest(n.path, [], budgets["suite"], old_side=False))
            row["suite_caught_by"] = caught
            row["category"] = "old only" if caught else "loss"
    return row


def _confirm(runner, rows, candidates, clean_old, clean_new, old_tests, new_tests, opts, budgets,
             collected=(None, None)) -> None:
    """Run every loss again, one run at a time, against clean runs made the same way; keep the losses that repeat."""
    losses = [(row, c) for row, c in zip(rows, candidates) if row["category"] == "loss"]
    if not losses:
        return
    clean_old.reset()
    old_ref = runner.pytest(clean_old.path, old_tests, opts.suite_timeout, old_side=True)
    new_ref = None
    if new_tests:
        clean_new.reset()
        new_ref = runner.pytest(clean_new.path, new_tests, opts.suite_timeout, old_side=False)
    clean_new.reset()
    suite_ref = runner.pytest(clean_new.path, [], opts.suite_timeout, old_side=False)
    for row, c in losses:
        why = None
        if not old_ref.usable:
            why = f"the clean old run made to confirm it gave no usable result (pytest exit {old_ref.code})"
        else:
            clean_old.reset(c)
            run = runner.pytest(clean_old.path, old_tests, budgets["old"], old_side=True)
            if _in_suite(_killed(old_ref, run), collected[0]) is None:
                why = "the old tests did not catch it again"
        if why is None and new_ref is not None:
            if not new_ref.usable:
                why = f"the clean new run made to confirm it gave no usable result (pytest exit {new_ref.code})"
            else:
                clean_new.reset(c)
                run = runner.pytest(clean_new.path, new_tests, budgets["new"], old_side=False)
                if _in_suite(_killed(new_ref, run), collected[1]):
                    why = "the new tests caught it when run alone"
        if why is None:
            if not suite_ref.usable:
                why = f"the clean whole-suite run made to confirm it gave no usable result (pytest exit {suite_ref.code})"
            else:
                clean_new.reset(c)
                if _killed(suite_ref, runner.pytest(clean_new.path, [], budgets["suite"], old_side=False)):
                    why = "the whole new suite caught it when run alone"
        row["confirmation"] = why or "repeated"
        if why:
            row["category"] = "unconfirmed loss"
    clean_old.reset()
    clean_new.reset()


def _quote(parts: list[str]) -> str:
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def _replay_prefix(opts: Options, repo: str, base: str, head: str) -> str:
    return _quote(["checkwash-prove", f"{base}..{head}", "--repo", os.path.realpath(repo), *opts.replay])


def render(report: dict) -> str:
    lines = [f"checkwash-prove (experimental), catalogue {report['catalogue']['version']}",
             f"range: {report['base'][:12]}..{report['head'][:12]}"
             + (" (from the merge base)" if report.get("from_merge_base") else ""),
             f"changed test files: {len(report['tests']['old'])} old, {len(report['tests']['new'])} new"]
    if report.get("status") and report["status"] != "done":
        lines.append(f"status: {report['status']}")
    clar = report["needs_clarification"]
    lines.append(f"needs clarification: {len(clar)}")
    for item in clar:
        lines.append(f"  - {item['test']}: {item['reason']}")
        if item.get("detail"):
            lines.append(f"      {item['detail'].splitlines()[0][:200]}")
    losses = report["protection_losses"]
    lines.append(f"protection losses: {len(losses)}")
    for loss in losses:
        lines.append(f"  - {loss['path']}:{loss['line']}  {loss['op']}  {loss['before']!r} -> {loss['after']!r}")
        lines.append(f"      caught only by the old test(s): {', '.join(loss['old_caught_by'])}")
        lines.append("      no test of the new suite that passes on the clean code fails with this fault "
                     "(run twice, the second time alone)")
        lines.append(f"      replay: {loss['replay']}")
    unconfirmed = report.get("unconfirmed_losses") or []
    if unconfirmed:
        lines.append(f"unconfirmed losses (did not repeat alone): {len(unconfirmed)}")
        for loss in unconfirmed:
            lines.append(f"  - {loss['path']}:{loss['line']}  {loss['op']}  {loss['before']!r} -> {loss['after']!r}"
                         f"  ({loss['why']})")
            lines.append(f"      replay: {loss['replay']}")
    m = report.get("mutation") or {}
    if "candidates" in m:
        lines.append(f"mutants: {m['generated']} would be generated; in run order (rank 0 = lines only the old tests "
                     "execute, 1 = lines the range changed, 2 = rest of a changed file, 3 = other files, 4 = lines "
                     "only old tests that do not pass ran):")
        for c in m["candidates"]:
            mark = "" if c["within_cap"] else "  (over the cap)"
            lines.append(f"  {c['id']}  rank {c['rank']}  {c['before']!r} -> {c['after']!r}{mark}")
    elif m:
        lines.append(f"mutants: {m.get('generated', 0)} generated, {m.get('run', 0)} run, "
                     f"{m.get('skipped_over_cap', 0)} over the cap; categories {m.get('categories', {})}")
    for note in report["notes"]:
        lines.append(f"note: {note}")
    return "".join(_visible(line) + "\n" for line in lines)


def _visible(line: str) -> str:
    """A report line with control, format and line-separator characters escaped. Paths, test ids and test output
    come from the repository: a terminal would act on an escape sequence or a bidirectional override in them, and a
    line break in one would start a line that reads as the report's own."""
    return "".join(f"\\u{ord(c):04x}" if unicodedata.category(c) in {"Cc", "Cf", "Zl", "Zp"} else c for c in line)
