"""Background-session runtime: OKe's agents as `claude --bg` sessions, for a terminal
or host without herdr. Same methods as runtime_herdr.HerdrRuntime, and agent records
in herdr's shape, so the domain does not know which runtime runs.

Place: before a start it is the folder the agent starts in; after it, the session's
short id (claude agents --json: "id"). Errors reuse HerdrError and its codes
(agent_not_found, herdr_unavailable) so the domain's handling stays one path.
"""
import io
import json
import os
import re
import shutil
import subprocess
import time

from runtime_herdr import HerdrError, write_private

# terminal escapes in `claude logs`: CSI, OSC (ended by BEL or ST), and two-byte ones
ANSI = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[@-Z\\-_])")
CLAUDE_TIMEOUT = 120  # seconds
START_POLLS = 30      # seconds a new session gets to show up in `claude agents`
# bg state -> herdr agent_status. failed, stopped and unknown states count as gone.
STATUS = {"working": "working", "blocked": "blocked", "done": "idle"}


def status_of(row):
    """herdr's agent_status for a `claude agents` row, or None when it is gone. A live
    session's own `status` wins over `state`: a waiting PM showed state working, and a
    turn that ended with a question showed state blocked, both with status idle at an
    empty prompt (seen live). Only a non-idle blocked session is at a dialog."""
    state = STATUS.get(row.get("state"))
    if state is None:
        return None
    if row.get("status") == "idle":
        return "idle"
    if state == "blocked":
        return "blocked"
    return {"busy": "working"}.get(row.get("status"), state)


# Settings every bg child needs: no self-isolation into its own worktree (OKe already
# picked the folder), and messages from the PM or orchestrator taken without asking.
BG_SETTINGS = {"worktree": {"bgIsolation": "none"}, "crossSessionInbound": "accept"}


def find_claude(name="claude"):
    """The program to start for `name`: a full path, or `name` when not found. An npm
    install on Windows is only claude.cmd, which Popen finds by full path only and runs
    through cmd.exe (which re-parses & % ^ | in arguments and survives a kill): start the
    claude.exe the shim names instead."""
    found = shutil.which(name)
    if not found or not found.lower().endswith(".cmd"):
        return found or name
    try:
        with io.open(found, encoding="utf-8", errors="replace") as f:
            m = re.search(r'"%dp0%\\([^"]*claude\.exe)"', f.read(), re.I)
    except (IOError, OSError):
        m = None
    # the shim writes Windows separators: split on them, so the path holds on any system
    exe = os.path.join(os.path.dirname(found), *m.group(1).split("\\")) if m else None
    return exe if exe and os.path.isfile(exe) else found


def claude_argv():
    value = os.environ.get("OKE_CLAUDE")
    if value is None:
        return [find_claude()]
    if value.lstrip().startswith("["):
        return json.loads(value)
    return value.split()


def child_env():
    """The caller's environment without the variables that make claude think it runs
    nested inside the caller's session (or inside the caller's host)."""
    # the user's own settings (provider, login, shell) stay; the rest of the prefix goes
    keep = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_GIT_BASH_PATH",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS")
    keep_prefix = ("CLAUDE_CODE_USE_", "CLAUDE_CODE_SKIP_", "CLAUDE_CODE_DISABLE_")
    return dict((k, v) for k, v in os.environ.items()
                if k in keep or k.startswith(keep_prefix)
                or not k.startswith(("CLAUDECODE", "CLAUDE_CODE_", "AI_AGENT")))


def run_claude(args, cwd=None):
    try:
        p = subprocess.Popen(claude_argv() + list(args), stdin=subprocess.DEVNULL, cwd=cwd,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env())
    except (OSError, ValueError) as e:
        raise HerdrError("herdr_unavailable", "claude: %s" % e)
    try:
        out, err = p.communicate(timeout=CLAUDE_TIMEOUT)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise HerdrError("herdr_unavailable", "claude %s did not answer in %d s"
                         % (" ".join(args[:1]), CLAUDE_TIMEOUT))
    out = out.decode("utf-8", "replace").replace("\r\n", "\n")
    err = err.decode("utf-8", "replace").replace("\r\n", "\n")
    if p.returncode != 0:
        line = (err.strip() or out.strip() or "exit %d" % p.returncode).splitlines()[0]
        raise HerdrError("claude_failed", line)
    return out


def trusted_by_claude(folder):
    """Has the user accepted Claude Code's trust dialog for `folder` or a folder above it
    (the home folder itself does not count)? None when ~/.claude.json cannot be read."""
    conf = os.environ.get("CLAUDE_CONFIG_DIR")
    path = os.path.join(conf, ".claude.json") if conf else os.path.join(os.path.expanduser("~"), ".claude.json")
    try:
        with io.open(path, encoding="utf-8") as f:
            projects = json.load(f).get("projects") or {}
    except (IOError, OSError, ValueError, AttributeError):
        return None

    def norm(p):
        p = os.path.normcase(os.path.abspath(p)).replace("\\", "/")
        return p.rstrip("/") or "/"
    trusted = set(norm(k) for k, v in projects.items() if isinstance(v, dict) and v.get("hasTrustDialogAccepted"))
    home, here = norm(os.path.expanduser("~")), os.path.abspath(folder)
    while True:
        if norm(here) in trusted and norm(here) != home:
            return True
        up = os.path.dirname(here)
        if up == here:  # a drive or filesystem root
            return False
        here = up


def as_agent(a):
    """One `claude agents --json` row in herdr's agent shape."""
    return {"name": a.get("name"), "pane_id": a.get("id"), "terminal_id": a.get("id"),
            "agent_status": status_of(a) or "gone", "cwd": a.get("cwd"),
            "agent_session": {"value": a.get("sessionId")} if a.get("sessionId") else None}


def without_bypass(argv):
    """bypassPermissions -> dontAsk with the agent's own tools allowed. `--bg` takes
    bypass only with the user's disclaimer acceptance from user settings, which a lean
    child does not read (seen live), and a bg session cannot answer a dialog anyway:
    dontAsk denies anything else instead of asking."""
    argv = list(argv)
    if "bypassPermissions" not in argv:
        return argv
    argv[argv.index("bypassPermissions")] = "dontAsk"
    tools = argv[argv.index("--tools") + 1].split(",") if "--tools" in argv else ["Bash", "Read", "Edit", "Write"]
    # --allowedTools takes every word after it: it goes last
    return argv + ["--allowedTools"] + tools


def with_bg_settings(argv, folder):
    """argv with BG_SETTINGS added: into OKe's own --settings file when there is one
    (two --settings flags do not merge), else as a new --settings."""
    argv = list(argv)
    if "--settings" in argv:
        i = argv.index("--settings") + 1
        with io.open(argv[i], encoding="utf-8") as f:
            settings = json.load(f)
        settings.update(BG_SETTINGS)
        path = os.path.join(folder, "bg-settings.json")
        write_private(path, json.dumps(settings, ensure_ascii=False))
        argv[i] = path
        return argv
    return argv + ["--settings", json.dumps(BG_SETTINGS)]


class BgRuntime(object):
    name = "bg"
    owns_orchestrator = False  # the orchestrator is the user's own session: never revived

    def inside(self):
        return True  # any terminal or host where claude runs

    def whoami(self):
        """The calling session's place: "session:<id>". A bg child's record holds that
        session id; the orchestrator's record holds this place itself."""
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
        return "session:" + sid if sid else None

    def missing(self):
        return []

    def rows(self):
        data = json.loads(run_claude(["agents", "--json", "--all"]) or "[]")
        return data if isinstance(data, list) else []

    def agents(self):
        return [as_agent(a) for a in self.rows() if status_of(a)]

    def agent(self, name):
        for a in self.agents():
            if a.get("name") == name:
                return a
        raise HerdrError("agent_not_found", name)

    def screen(self, name):
        """The end of `claude logs`, which is raw terminal output: escapes removed and
        repeated redraws of the same line folded."""
        lines = []
        for line in ANSI.sub("", run_claude(["logs", self.agent(name)["pane_id"]])).splitlines():
            line = line.rstrip()
            if line and (not lines or lines[-1] != line):
                lines.append(line)
        return "\n".join(lines[-40:]) + "\n"

    def start(self, name, place, argv, prompt, trusted, registered=lambda: None):
        """Start `claude --bg` in folder `place`. A bg session cannot answer a dialog:
        an untrusted folder fails here, and `trusted` is not asked."""
        folder = place
        settings = argv[argv.index("--settings") + 1] if "--settings" in argv else None
        if settings and os.path.isfile(settings):
            folder = os.path.dirname(settings)  # OKe's agent folder
        # The prompt goes first: --allowedTools takes every word after it (seen live).
        argv = without_bypass(with_bg_settings(argv, folder))
        try:
            run_claude([prompt, "--bg", "--name", name] + argv, cwd=place)
            registered()
            for _ in range(START_POLLS):
                try:
                    return self.agent(name)
                except HerdrError as e:
                    if e.code != "agent_not_found":
                        raise
                time.sleep(1)
            raise HerdrError("agent_not_ready", "%s did not show up in claude agents" % name)
        except HerdrError as e:
            if e.code in ("agent_not_ready", "claude_failed") and trusted_by_claude(place) is False:
                raise HerdrError(e.code, "%s. Claude Code has not trusted %s, and a background session cannot "
                                 "answer the trust dialog: open `claude` in that folder (or the OKe home "
                                 "above it) once, accept, and try again" % (e.message, place))
            raise

    def prompt(self, name, text):
        """No documented way for a script to type into a bg session: the Claude session
        that waits on `name` sends the text itself (SendMessage)."""
        self.agent(name)  # gone: agent_not_found, like herdr
        return "ask_caller"

    def keys(self, name, keys):
        raise HerdrError("unsupported", "a background session takes no keys: claude attach %s"
                         % self.agent(name)["pane_id"])

    def attach(self, name):
        """A person attaches with `claude attach <id>`; a script cannot do it for them."""
        return "claude attach %s" % self.agent(name)["pane_id"]

    def split(self, near, cwd):
        return cwd

    def new_place(self, near, cwd):
        return cwd

    def place_for_repo(self, repo, label):
        return repo

    def place_cwd(self, place):
        if os.path.isdir(place):
            return place
        for a in self.rows():
            if a.get("id") == place:
                return a.get("cwd") or ""
        return ""

    def place_id(self, place):
        """The session id while `place` is a session OKe can still close, dead ones included."""
        return place if any(a.get("id") == place for a in self.rows()) else None

    def place_exists(self, place):
        return any(a.get("id") == place and status_of(a) for a in self.rows())

    def processes(self, place):
        return []

    def close(self, place):
        if os.path.isdir(place):
            return  # never started: nothing to close
        try:
            run_claude(["stop", place])
        except HerdrError as e:
            if e.code != "claude_failed":
                raise  # already stopped: rm still applies
        run_claude(["rm", place])

    def worktree_create(self, repo, branch, label, start=None, near=None):
        """A worktree inside the repository: a folder in a trusted folder is trusted,
        so `claude --bg` starts there (measured). A new branch starts at `start` (else HEAD).
        `near` is for the herdr runtime: here there are no tabs to place."""
        path = os.path.join(repo, ".claude", "worktrees", "oke-" + label)
        exists = subprocess.call(["git", "-C", repo, "rev-parse", "--verify", "--quiet", branch],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
        args = ["git", "-C", repo, "worktree", "add"] + (
            [path, branch] if exists else ["-b", branch, path] + ([start] if start else []))
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        if p.returncode != 0:
            raise HerdrError("worktree_failed", err.decode("utf-8", "replace").strip())
        # main's `git status` must not list the worktree: it is not the user's change
        exclude = subprocess.check_output(["git", "-C", repo, "rev-parse", "--git-path", "info/exclude"])
        exclude = os.path.join(repo, exclude.decode("utf-8").strip())
        line = "/.claude/worktrees/"
        try:
            with io.open(exclude, encoding="utf-8") as f:
                known = line in f.read().split()
        except (IOError, OSError):
            known = False
        if not known:
            if not os.path.isdir(os.path.dirname(exclude)):
                os.makedirs(os.path.dirname(exclude))
            with io.open(exclude, "a", encoding="utf-8", newline="\n") as f:
                f.write(u"\n%s\n" % line)
        return {"id": path, "path": path, "place": path}

    def tab_ids(self):
        return None  # no tabs: the folder is all there is

    def worktree_places(self, repo, path, own=()):
        return []  # no tabs or workspaces: the folder is all there is

    def worktree_remove(self, wid=None, force=False, path=None, repo=None):
        """The worktree's id is its path: there is nothing to close."""
        git_worktree_remove(path or wid, repo, force=force)


def git_worktree_remove(wid, main=None, force=False):
    """Remove the git worktree at path `wid`; its branch stays (as herdr does). `main`: the
    main checkout, when known; a folder already gone is then pruned from git's list."""
    if main is None:
        common = subprocess.check_output(["git", "-C", wid, "rev-parse", "--git-common-dir"])
        main = os.path.dirname(os.path.join(wid, common.decode("utf-8").strip()))  # may be relative to wid
    # Run from the main checkout: on Windows git cannot delete the folder it runs in.
    # Without --force, git refuses a worktree with changes, like herdr does.
    argv = (["worktree", "remove"] + (["--force"] if force else []) + [wid]) if os.path.isdir(wid) \
        else ["worktree", "prune"]
    p = subprocess.Popen(["git", "-C", main] + argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    if p.returncode != 0:
        raise HerdrError("worktree_failed", err.decode("utf-8", "replace").strip())
