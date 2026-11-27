"""Shared helpers that keep the tests fast: on Windows every git or python process start
costs ~80 ms, so fixtures are built once per process and copied, and the fake herdr runs
in-process. Set OKE_TEST_SUBPROCESS=1 to run the fake herdr as a real process again."""
import atexit
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_HERDR = os.path.join(HERE, "fake", "herdr.py")

_templates = {}
_root = []


def _template_root():
    if not _root:
        d = tempfile.mkdtemp(prefix="oke-test-templates-")
        atexit.register(shutil.rmtree, d, True)
        _root.append(d)
    return _root[0]


def copy_template(key, build, dest):
    """Copy the fixture `key` to `dest` (which must not exist), building it first with
    `build(path)` the first time this process asks for it. Only for fixtures without linked
    worktrees: a copied .git must hold no absolute paths."""
    src = _templates.get(key)
    if src is None:
        src = os.path.join(_template_root(), "t%d" % len(_templates))
        os.makedirs(src)
        build(src)
        _templates[key] = src
    parent = os.path.dirname(dest)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    shutil.copytree(src, dest)
    return dest


def _git(cwd, *args):
    return subprocess.check_output(["git", "-c", "user.name=t", "-c", "user.email=t@t"] + list(args),
                                   cwd=cwd, stderr=subprocess.DEVNULL).decode().strip()


def plain_repo(dest):
    """A repository at `dest` with one empty commit (built once per process, then copied)."""
    def build(p):
        _git(p, "init", "-q")
        _git(p, "commit", "-q", "--allow-empty", "-m", "init")
        hooks = os.path.join(p, ".git", "hooks")
        for n in os.listdir(hooks) if os.path.isdir(hooks) else ():
            if n.endswith(".sample"):  # inactive examples: fewer files to copy per test
                os.remove(os.path.join(hooks, n))
    return copy_template("plain_repo", build, dest)


def _objects(repo):
    root = os.path.join(repo, ".git", "objects")
    found = set()
    for d, _, names in os.walk(root):
        for n in names:
            found.add(os.path.relpath(os.path.join(d, n), root))
    return found


def _head_commit(repo):
    """The commit HEAD points at, read from the files (no git process); None when unsure."""
    git = os.path.join(repo, ".git")
    try:
        with open(os.path.join(git, "HEAD")) as f:
            head = f.read().strip()
        if not head.startswith("ref: "):
            return head
        with open(os.path.join(git, head[5:])) as f:
            return f.read().strip()
    except (IOError, OSError):
        return None


_branch_commits = {}


def add_branch_commit(test, repo, branch, files, msg):
    """New branch `branch` in `repo` (a Base.git_repo copy, HEAD on its first commit): one
    commit with `files` ({name: text}) on top of HEAD, the checkout left as it was. Does
    what `checkout -b`, write, `add -A`, `commit`, `checkout -` does, but builds the commit
    once per process and copies its objects in."""
    key = (branch, tuple(sorted(files.items())), msg)
    made = _branch_commits.get(key)
    if made is None:
        tpl = plain_repo(os.path.join(_template_root(), "b%d" % len(_branch_commits)))
        before = _objects(tpl)
        _git(tpl, "checkout", "-q", "-b", branch)
        for name, text in files.items():
            with io.open(os.path.join(tpl, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        _git(tpl, "add", "-A")
        _git(tpl, "commit", "-q", "-m", msg)
        made = {"commit": _git(tpl, "rev-parse", "HEAD"), "dir": tpl, "objects": sorted(_objects(tpl) - before)}
        _git(tpl, "checkout", "-q", "-")
        made["base"] = _head_commit(tpl)
        _branch_commits[key] = made
    plain = (_head_commit(repo) == made["base"] and os.listdir(repo) == [".git"]
             and not os.path.exists(os.path.join(repo, ".git", "refs", "heads", branch)))
    if not plain:  # not an untouched copy (a file `add -A` would take, another first commit): the long way
        test.git(repo, "checkout", "-q", "-b", branch)
        for name, text in files.items():
            with io.open(os.path.join(repo, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        test.git(repo, "add", "-A")
        test.git(repo, "commit", "-q", "-m", msg)
        test.git(repo, "checkout", "-q", "-")
        return
    src, dst = os.path.join(made["dir"], ".git", "objects"), os.path.join(repo, ".git", "objects")
    for rel in made["objects"]:
        target = os.path.join(dst, rel)
        if not os.path.exists(target):
            if not os.path.isdir(os.path.dirname(target)):
                os.makedirs(os.path.dirname(target))
            shutil.copy2(os.path.join(src, rel), target)
    ref = os.path.join(repo, ".git", "refs", "heads", *branch.split("/"))
    if not os.path.isdir(os.path.dirname(ref)):
        os.makedirs(os.path.dirname(ref))
    with open(ref, "w") as f:
        f.write(made["commit"] + "\n")


def built_once(build_parser):
    """oke.build_parser, built on the first call only: its parser holds no state from a parse,
    and building it is ~10% of a test's time (a real `oke` process builds it once too)."""
    made = []

    def once():
        if not made:
            made.append(build_parser())
        return made[0]
    return once


_fake = []


def _fake_module():
    if not _fake:
        spec = importlib.util.spec_from_file_location("oke_fake_herdr", FAKE_HERDR)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _fake.append(mod)
    return _fake[0]


class _Out(object):
    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, text):
        self.buffer.write(text.encode("utf-8"))

    def flush(self):
        pass


def in_process_herdr(original, herdr_argv):
    """A runtime_herdr.run_herdr that runs fake/herdr.py's main() in this process when
    OKE_HERDR names it (as Base.fake sets it), and the real run_herdr otherwise."""
    def run_herdr(args):
        if os.environ.get("OKE_TEST_SUBPROCESS") or herdr_argv() != [sys.executable, FAKE_HERDR]:
            return original(args)
        fake = _fake_module()
        saved = sys.argv, sys.stdout, sys.stderr
        out, err = _Out(), _Out()
        sys.argv, sys.stdout, sys.stderr = ["herdr"] + [str(a) for a in args], out, err
        try:
            try:
                code = fake.main()
            except SystemExit as e:
                code = e.code
        finally:
            sys.argv, sys.stdout, sys.stderr = saved
        code = 0 if code is None else code
        return (code,
                out.buffer.getvalue().decode("utf-8", "replace").replace("\r\n", "\n"),
                err.buffer.getvalue().decode("utf-8", "replace").replace("\r\n", "\n"))
    return run_herdr
