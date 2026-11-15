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
import subprocess
import time

from runtime_herdr import HerdrError

CLAUDE_TIMEOUT = 120  # seconds
START_POLLS = 30      # seconds a new session gets to show up in `claude agents`
# bg state -> herdr agent_status. failed, stopped and unknown states count as gone.
STATUS = {"working": "working", "blocked": "blocked", "done": "idle"}
# Settings every bg child needs: no self-isolation into its own worktree (OKe already
# picked the folder), and messages from the PM or orchestrator taken without asking.
BG_SETTINGS = {"worktree": {"bgIsolation": "none"}, "crossSessionInbound": "accept"}


def claude_argv():
    value = os.environ.get("OKE_CLAUDE", "claude")
    if value.lstrip().startswith("["):
        return json.loads(value)
    return value.split()


def child_env():
    """The caller's environment without the variables that make claude think it runs
    nested inside the caller's session (or inside the caller's host)."""
    # shortcut: drops by prefix (measured with an Orca-hosted parent); keep-list grows if a needed one shows up
    keep = ("CLAUDE_CONFIG_DIR",)
    return dict((k, v) for k, v in os.environ.items()
                if k in keep or not k.startswith(("CLAUDECODE", "CLAUDE_CODE_", "ORCA_", "AI_AGENT")))


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


def as_agent(a):
    """One `claude agents --json` row in herdr's agent shape."""
    return {"name": a.get("name"), "pane_id": a.get("id"), "terminal_id": a.get("id"),
            "agent_status": STATUS.get(a.get("state"), "gone"), "cwd": a.get("cwd"),
            "agent_session": {"value": a.get("sessionId")} if a.get("sessionId") else None}


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
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(settings, ensure_ascii=False))
        argv[i] = path
        return argv
    return argv + ["--settings", json.dumps(BG_SETTINGS)]


class BgRuntime(object):
    name = "bg"
    Error = HerdrError

    def inside(self):
        return True  # any terminal or host where claude runs

    def missing(self):
        return []

    def rows(self):
        data = json.loads(run_claude(["agents", "--json", "--all"]) or "[]")
        return data if isinstance(data, list) else []

    def agents(self):
        return [as_agent(a) for a in self.rows() if STATUS.get(a.get("state"))]

    def agent(self, name):
        for a in self.agents():
            if a.get("name") == name:
                return a
        raise HerdrError("agent_not_found", name)

    def screen(self, name):
        lines = run_claude(["logs", self.agent(name)["pane_id"]]).splitlines()
        return "\n".join(lines[-40:]) + "\n"

    def start(self, name, place, argv, prompt, trusted, registered=lambda: None, kind="claude"):
        """Start `claude --bg` in folder `place`. A bg session cannot answer a dialog:
        an untrusted folder fails here, and `trusted` is not asked."""
        if kind != "claude":
            raise HerdrError("unsupported", "only claude agents run as background sessions")
        folder = place
        settings = argv[argv.index("--settings") + 1] if "--settings" in argv else None
        if settings and os.path.isfile(settings):
            folder = os.path.dirname(settings)  # OKe's agent folder
        # The prompt goes first: --allowedTools takes every word after it (seen live).
        run_claude([prompt, "--bg", "--name", name] + with_bg_settings(argv, folder), cwd=place)
        registered()
        for _ in range(START_POLLS):
            try:
                return self.agent(name)
            except HerdrError as e:
                if e.code != "agent_not_found":
                    raise
            time.sleep(1)
        raise HerdrError("agent_not_ready", "%s did not show up in claude agents" % name)

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
        return any(a.get("id") == place and STATUS.get(a.get("state")) for a in self.rows())

    def has_claude(self, place):
        return self.place_exists(place)

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

    def worktree_create(self, repo, branch, label):
        """A worktree inside the repository: a folder in a trusted folder is trusted,
        so `claude --bg` starts there (measured)."""
        path = os.path.join(repo, ".claude", "worktrees", "oke-" + label)
        exists = subprocess.call(["git", "-C", repo, "rev-parse", "--verify", "--quiet", branch],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
        args = ["git", "-C", repo, "worktree", "add", path] + ([branch] if exists else ["-b", branch])
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        if p.returncode != 0:
            raise HerdrError("worktree_failed", err.decode("utf-8", "replace").strip())
        return {"id": path, "path": path, "place": path}

    def worktree_remove(self, wid):
        # Run from the main checkout: on Windows git cannot delete the folder it runs in.
        common = subprocess.check_output(["git", "-C", wid, "rev-parse", "--git-common-dir"])
        common = os.path.join(wid, common.decode("utf-8").strip())  # may be relative to wid
        # Without --force, git refuses a worktree with changes, like herdr does.
        p = subprocess.Popen(["git", "-C", os.path.dirname(common), "worktree", "remove", wid],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        if p.returncode != 0:
            raise HerdrError("worktree_failed", err.decode("utf-8", "replace").strip())
