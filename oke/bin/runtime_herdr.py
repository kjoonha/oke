"""herdr runtime: the only place OKe runs the herdr CLI.

The domain (oke.py) reaches herdr through HerdrRuntime's methods; the plain functions
below are the plumbing they share. Agent records keep herdr's shape: name, pane_id,
agent_status, agent_session, terminal_id.
"""
import json
import hashlib
import io
import os
import shutil
import stat
import subprocess
import time

REQUIRED_COMMANDS = ("agent start", "agent prompt", "agent wait", "agent read",
                     "agent send-keys", "pane split", "tab create")


def replace_file(src, dst):
    for attempt in range(20):
        try:
            os.replace(src, dst)
            return
        except OSError:  # Windows: another process has the file open for a moment
            if attempt == 19:
                raise
            time.sleep(0.05)


def write_private(path, text):
    """Write a file only its owner can read: it may hold the user's env tokens.
    Windows ignores the mode; the user's profile folder already guards it there."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)  # an older file kept its wider mode
    with io.open(fd, "w", encoding="utf-8") as f:
        f.write(text)


class HerdrError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, "%s: %s" % (code, message))
        self.code = code
        self.message = message


def herdr_argv():
    value = os.environ.get("OKE_HERDR", "herdr")
    if value.lstrip().startswith("["):
        return json.loads(value)
    return value.split()


HERDR_TIMEOUT = 180  # seconds; longer than any --timeout OKe passes (agent start: 90 s)


def run_herdr(args):
    """Return (exit code, stdout, stderr). Raises HerdrError when herdr cannot run
    or hangs: a hung herdr must not freeze a wait or hold a lock forever."""
    try:
        p = subprocess.Popen(herdr_argv() + list(args), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, ValueError) as e:
        raise HerdrError("herdr_unavailable", str(e))
    try:
        out, err = p.communicate(timeout=HERDR_TIMEOUT)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.communicate(timeout=5)  # a child that inherited the pipes may keep them open
        except subprocess.TimeoutExpired:
            pass
        raise HerdrError("herdr_unavailable", "herdr %s did not answer in %d s"
                         % (" ".join(args[:2]), HERDR_TIMEOUT))
    return (p.returncode,
            out.decode("utf-8", "replace").replace("\r\n", "\n"),
            err.decode("utf-8", "replace").replace("\r\n", "\n"))


def parse_json(text):
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def raise_herdr_error(code, out, err):
    """herdr prints error JSON on stderr; anything else means herdr itself is unreachable."""
    for text in (err, out):
        data = parse_json(text)
        if data and isinstance(data.get("error"), dict):
            raise HerdrError(data["error"].get("code", "unknown"), data["error"].get("message", ""))
    lines = (err.strip() or out.strip() or "no output (exit %d)" % code).splitlines()
    raise HerdrError("herdr_unavailable", lines[0])


def herdr(*args):
    code, out, err = run_herdr(args)
    data = parse_json(out)
    if data is None or "error" in data:
        raise_herdr_error(code, out, err)
    return data.get("result", {})


def herdr_text(*args):
    code, out, err = run_herdr(args)
    if code != 0:
        raise_herdr_error(code, out, err)
    return out


def missing_commands():
    missing = []
    for command in REQUIRED_COMMANDS:
        code, _, _ = run_herdr(command.split() + ["--help"])
        if code != 0:
            missing.append(command)
    return missing



TRUST_MARKER = "trust this folder"
DIALOG_TRIES = 5
DIALOG_POLL = 1.0
PROMPT_TRIES = 3  # looks for the first prompt on screen, typing it again after each miss but the last
PROMPT_POLL = 3.0  # seconds claude gets to finish starting before the next look
START_TIMEOUT_MS = 90000


def session_of(agent):
    session = agent.get("agent_session")
    return session.get("value") if isinstance(session, dict) else None


def list_agents():
    return herdr("agent", "list").get("agents", [])


class DialogError(Exception):
    """The new agent stopped at a dialog OKe may not answer. The message is the screen."""


def start_agent(agent, pane, claude_args, prompt, trusted, registered=lambda: None):
    """Start claude in `pane`, answer the trust dialog only when `trusted()` allows it,
    send the first prompt and make sure the first turn started. `registered` runs once
    herdr knows the name. Returns herdr's agent info."""
    try:
        # herdr's 30 s default was too short live for a claude resuming a long session
        herdr("agent", "start", agent, "--kind", "claude", "--pane", pane, "--timeout", str(START_TIMEOUT_MS),
              "--", *claude_args)
    except HerdrError as e:
        if e.code != "agent_not_ready":
            raise
        registered()  # herdr keeps the name even though the agent is not ready
        answer_startup_dialogs(agent, trusted)
    registered()

    # a resumed session shows its earlier prompts: only a new copy counts as this one
    shown = prompt_count(agent, prompt) or 0

    # Wait until the first turn starts: for a few seconds after a prompt herdr still
    # says idle, and `oke wait` would read that as "idle with no report".
    try:
        herdr("agent", "prompt", agent, prompt,
              "--wait", "--until", "working", "--until", "blocked", "--timeout", "20000")
    except HerdrError as e:
        if e.code not in ("agent_prompt_stalled", "timeout"):
            raise
    info = herdr("agent", "get", agent).get("agent", {})
    if info.get("agent_status") in ("idle", "done"):
        # Seen live: the prompt text sat in the input box with its Enter lost.
        # Enter on an empty input does nothing, so one more is safe.
        herdr("agent", "send-keys", agent, "enter")
        try:
            herdr("agent", "wait", agent, "--until", "working", "--until", "blocked", "--timeout", "20000")
        except HerdrError as e:
            if e.code != "timeout":
                raise
        info = herdr("agent", "get", agent).get("agent", {})
    # Seen live: a prompt typed while claude was still starting vanished, while herdr
    # already said working (the startup itself), so the session never had a first turn.
    # Give claude time to finish starting, then type it again.
    for attempt in range(PROMPT_TRIES):
        if prompt_seen(agent, prompt, shown):
            break
        time.sleep(PROMPT_POLL)
        if attempt == PROMPT_TRIES - 1 or prompt_seen(agent, prompt, shown):
            break  # it showed up late, or the last look: `oke wait` reports a silent child
        info = herdr("agent", "get", agent).get("agent", {})
        if info.get("agent_status") not in ("idle", "done"):
            continue  # still starting, or at a dialog: herdr refuses a prompt now; look again
        try:
            herdr("agent", "prompt", agent, prompt,
                  "--wait", "--until", "working", "--until", "blocked", "--timeout", "20000")
        except HerdrError as e:
            if e.code not in ("agent_prompt_stalled", "timeout", "agent_not_idle", "agent_blocked"):
                raise
        info = herdr("agent", "get", agent).get("agent", {})
    return info


def prompt_count(agent, prompt):
    """How often the start of `prompt` is on the agent's screen (typed or taken), or None
    when the screen cannot be read."""
    try:
        screen = herdr_text("agent", "read", agent, "--source", "recent-unwrapped", "--lines", "400")
    except HerdrError:
        return None
    return "".join(screen.split()).count("".join(prompt.split())[:40])


def prompt_seen(agent, prompt, shown=0):
    """Whether the screen shows `prompt` more often than the `shown` copies it had before
    it was typed. Unsure (the read failed) counts as seen: a second copy of the prompt is
    worse than a retry missed."""
    count = prompt_count(agent, prompt)
    return count is None or count > shown


def startup_keys(screen, trusted):
    """Keys for a startup screen OKe may answer, or None."""
    if TRUST_MARKER in screen.lower() and trusted():
        return ["down", "enter"]  # claude preselects "No, exit"
    return None


def answer_startup_dialogs(agent, trusted):
    """Answer the trust dialog when `trusted()` allows it. Anything else is the user's
    call: DialogError with the screen."""
    for _ in range(3):
        # The dialog can still be drawing when herdr reports the agent blocked.
        for _ in range(DIALOG_TRIES):
            screen = herdr_text("agent", "read", agent, "--source", "visible", "--lines", "40")
            keys = startup_keys(screen, trusted)
            if keys:
                break
            time.sleep(DIALOG_POLL)
        if not keys:
            raise DialogError(screen)
        herdr("agent", "send-keys", agent, *keys)
        try:
            herdr("agent", "wait", agent, "--until", "idle", "--timeout", "30000")
            return
        except HerdrError as e:
            if e.code != "timeout":
                raise  # still blocked: the next screen is another dialog
    raise DialogError(screen)


def terminal_of(pane):
    """herdr's id of the terminal in `pane`; unlike the pane id it is never reused."""
    try:
        return herdr("pane", "get", pane).get("pane", {}).get("terminal_id")
    except HerdrError:
        return None


def pane_exists(pane):
    try:
        herdr("pane", "get", pane)
        return True
    except HerdrError as e:
        if e.code == "herdr_unavailable":
            raise
        return False


WORKTREES_ROOT = os.path.join(os.path.expanduser("~"), ".herdr", "worktrees")


CLOSE_WAIT = 10.0  # seconds a closed tab's processes get to leave the worktree folder
REMOVE_TRIES = 5  # git worktree remove, again while Windows still locks the folder
REMOVE_POLL = 0.5


def norm_path(p):
    """A path to compare: herdr may answer with a long-path prefix, git with forward
    slashes, and either may name a symlinked folder by its target."""
    def bare(q):
        return q[4:] if q.startswith("\\\\?\\") else q
    p = bare(os.path.realpath(bare(p)))
    return os.path.normcase(os.path.abspath(p)).replace("\\", "/").rstrip("/")


def path_inside(cwd, want):
    """Whether `cwd` is `want` (a norm_path) or a folder below it."""
    cwd = norm_path(cwd) if cwd else None
    return cwd is not None and (cwd == want or cwd.startswith(want + "/"))


def worktree_folder(repo, branch):
    """~/.herdr/worktrees/<repository folder name>-<hash of its path>/<branch, "/" as "-">:
    two repositories of one name never share a folder."""
    name = os.path.basename(os.path.normpath(os.path.abspath(repo))) or "root"  # a drive root has no name
    tag = hashlib.sha1(norm_path(repo).encode("utf-8")).hexdigest()[:8]
    return os.path.join(WORKTREES_ROOT, "%s-%s" % (name, tag), branch.replace("/", "-"))


def registered(repo, path):
    """Whether git lists `path` as a worktree of `repo`."""
    if not repo:
        return False
    p = subprocess.Popen(["git", "-C", repo, "worktree", "list", "--porcelain"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = p.communicate()[0].decode("utf-8", "replace")
    want = norm_path(path)
    return any(norm_path(line[len("worktree "):]) == want
               for line in out.splitlines() if line.startswith("worktree "))


def checkout_of(repo, branch):
    """The folder of a linked worktree of `repo` that has `branch` checked out (wherever it
    is: an older OKe made them elsewhere), or None. The main checkout never counts."""
    p = subprocess.Popen(["git", "-C", repo, "worktree", "list", "--porcelain"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = p.communicate()[0].decode("utf-8", "replace")
    here, first = None, True
    for line in out.splitlines():
        if line.startswith("worktree "):
            here, first = line[len("worktree "):], here is None
        elif line == "branch refs/heads/" + branch and not first and os.path.isdir(here):
            return os.path.normpath(here)
    return None


def toplevel(path):
    """The top folder of the git checkout `path` is in (a job's path may be a subfolder)."""
    p = subprocess.Popen(["git", "-C", path, "rev-parse", "--show-toplevel"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = p.communicate()[0].decode("utf-8", "replace").strip()
    return os.path.normpath(out) if p.returncode == 0 and out else path


def git_worktree_add(repo, path, branch, start=None):
    """Check out `branch` at `path`; a new branch starts at `start` (else HEAD). A failed
    add leaves no new branch behind: a retry would take it, at HEAD, without `start`."""
    if os.path.exists(path) and (not os.path.isdir(path) or os.listdir(path)):
        raise HerdrError("worktree_failed", "folder %s is already there and is not the worktree of %s: "
                         "move it away first" % (path, branch))
    exists = subprocess.call(["git", "-C", repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    # a folder deleted by hand stays in git's list and blocks the add
    subprocess.call(["git", "-C", repo, "worktree", "prune"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    args = ["git", "-C", repo, "worktree", "add"] + (
        [path, branch] if exists else ["-b", branch, path] + ([start] if start else []))
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err = p.communicate()[1]
    if p.returncode != 0:
        if not exists:  # git may have made the branch before the checkout failed
            subprocess.call(["git", "-C", repo, "worktree", "prune"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.call(["git", "-C", repo, "branch", "-q", "-D", branch],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        raise HerdrError("worktree_failed", err.decode("utf-8", "replace").strip())


def wait_closed(wid):
    """Wait (up to CLOSE_WAIT seconds) until herdr lists no pane of the closed tab or
    workspace `wid`: a close returns before its shells have let go of their folder."""
    deadline = time.time() + CLOSE_WAIT
    while True:
        try:
            panes = herdr("pane", "list").get("panes", [])
        except HerdrError:
            return
        if not any(wid in (p.get("tab_id"), p.get("workspace_id")) for p in panes) or time.time() >= deadline:
            return
        time.sleep(0.25)


LOCKED = ("permission denied", "resource busy", "being used by another process",
          "directory not empty", "invalid argument")


def ours_to_delete(path, repo, was_registered):
    """Whether OKe may delete what is left of `path` itself: a folder where OKe makes
    worktrees, or one git listed as a worktree of `repo` when the removal began."""
    p = norm_path(path)
    roots = [norm_path(WORKTREES_ROOT)] + ([norm_path(os.path.join(repo, ".claude", "worktrees"))] if repo else [])
    return was_registered or any(p.startswith(r + "/") for r in roots)


def writable_again(func, path, _exc):
    """shutil.rmtree's onerror: Windows will not delete a read-only file (git makes its
    object files read-only); clear the bit and try once more. A file still locked stays."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_checkout(path, repo, force):
    """`git worktree remove` of `path`, again for a while when Windows still locks the folder.
    git deletes the checkout's files and its own record of it even when some file stays
    locked; from then on git knows no worktree there ("is not a working tree"), and what is
    left is deleted directly, until it is gone or the tries run out."""
    import runtime_bg  # it imports this module
    was_registered = registered(repo, path)
    by_hand = False
    for attempt in range(REMOVE_TRIES):
        if attempt:
            time.sleep(REMOVE_POLL)
        if not os.path.isdir(path):
            if repo:
                runtime_bg.git_worktree_remove(path, repo, force=force)  # prunes git's list
            return
        if not os.path.exists(os.path.join(path, ".git")):
            if not (attempt or force or was_registered):
                # not git's leftover from a try here: files nobody said to throw away
                raise HerdrError("worktree_failed", "%s is no git checkout: not deleted without --force" % path)
            by_hand = True  # git started and left the rest; it would not touch it again
        if not by_hand:
            try:
                runtime_bg.git_worktree_remove(path, repo, force=force)
                return
            except HerdrError as e:
                text = e.message.lower()
                if "not a working tree" in text and was_registered:
                    by_hand = True
                elif not any(m in text for m in LOCKED):
                    raise
                continue
        if not ours_to_delete(path, repo, was_registered):
            raise HerdrError("worktree_failed", "%s is no worktree OKe made: not deleted" % path)
        shutil.rmtree(path, onerror=writable_again)
        if repo:
            subprocess.call(["git", "-C", repo, "worktree", "prune"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not os.path.isdir(path):
            return
    raise HerdrError("worktree_failed", "could not delete %s: a file in it is still in use" % path)


class HerdrRuntime(object):
    """What the OKe domain needs from a runtime, done with herdr panes. A place is a
    herdr pane id. Errors are HerdrError with herdr's own codes."""

    name = "herdr"
    owns_orchestrator = True  # a PM may revive a dead orchestrator in its pane

    def inside(self):
        return os.environ.get("HERDR_ENV") == "1"

    def whoami(self):
        """The calling session's place: its herdr pane."""
        return os.environ.get("HERDR_PANE_ID")

    def missing(self):
        return missing_commands()

    def agents(self):
        return list_agents()

    def agent(self, name):
        return herdr("agent", "get", name).get("agent", {})

    def screen(self, name):
        return herdr_text("agent", "read", name, "--source", "visible", "--lines", "40")

    def start(self, name, place, argv, prompt, trusted, registered=lambda: None):
        return start_agent(name, place, argv, prompt, trusted, registered)

    def prompt(self, name, text):
        """Type `text` into an idle agent. True when it took it, False when it surely
        did not; raises when herdr is unreachable."""
        try:
            herdr("agent", "prompt", name, text, "--wait", "--until", "working",
                  "--until", "blocked", "--timeout", "20000")
        except HerdrError as e:
            if e.code == "herdr_unavailable":
                raise
            # After a stall or timeout the text may be in although the wait for
            # "working" failed: typing it again would give the same instruction twice.
            # Any other error (a refusal) typed nothing.
            if e.code not in ("agent_prompt_stalled", "timeout"):
                return False
            try:
                status = self.agent(name).get("agent_status")
            except HerdrError:
                status = None
            if status not in ("working", "blocked"):
                return False
        return True

    def split(self, near, cwd):
        """A new pane right of `near`; without `near`, right of the caller's own pane."""
        near = near or os.environ.get("HERDR_PANE_ID")
        if not near:
            raise HerdrError("no_place", "not inside a herdr pane: give --pane or --split-from")
        return herdr("pane", "split", "--pane", near, "--direction", "right",
                     "--cwd", cwd, "--no-focus")["pane"]["pane_id"]

    def new_place(self, near, cwd):
        """A pane next to the first of `near` that still exists, else a new workspace."""
        for parent in [p for p in near if p]:
            try:
                return self.split(parent, cwd)
            except HerdrError as e:
                if e.code == "herdr_unavailable":
                    raise
        return herdr("workspace", "create", "--cwd", cwd, "--label", "oke", "--no-focus")["root_pane"]["pane_id"]

    def place_cwd(self, place):
        return herdr("pane", "get", place).get("pane", {}).get("cwd") or ""

    def place_exists(self, place):
        return pane_exists(place)

    def place_id(self, place):
        return terminal_of(place)

    def processes(self, place):
        """Foreground processes in a place (name, argv, cwd); [] when unknown."""
        try:
            info = herdr("pane", "process-info", "--pane", place).get("process_info", {})
        except HerdrError:
            return []
        return info.get("foreground_processes") or []

    def close(self, place):
        herdr("pane", "close", place)

    def keys(self, name, keys):
        herdr("agent", "send-keys", name, *keys)

    def attach(self, name):
        """Show the agent to the user: herdr focuses its pane."""
        herdr("agent", "focus", name)

    def place_for_repo(self, repo, label):
        """A place for a new PM: next to a pane already in `repo`, else a new workspace."""
        want = os.path.normcase(os.path.abspath(repo))
        for p in herdr("pane", "list").get("panes", []):
            if p.get("cwd") and os.path.normcase(os.path.abspath(p["cwd"])) == want:
                return self.split(p["pane_id"], repo)
        return herdr("workspace", "create", "--cwd", repo, "--label", label,
                     "--no-focus")["root_pane"]["pane_id"]

    def worktree_create(self, repo, branch, label, start=None, near=None):
        """Check out `branch` in a worktree under ~/.herdr/worktrees/<repo>/ (a new branch
        starts at `start`, else HEAD; a worktree already there on `branch` is reused) and
        open a tab for it in the workspace of pane `near` (the PM's), else a new workspace.
        Returns id (the tab, or the workspace: to close it), path, place (its first pane)."""
        path = checkout_of(repo, branch)  # reused wherever it is
        if path is None:
            path = worktree_folder(toplevel(repo), branch)
            git_worktree_add(repo, path, branch, start)
        ws = None
        if near:
            try:
                ws = herdr("pane", "get", near).get("pane", {}).get("workspace_id")
            except HerdrError as e:
                if e.code == "herdr_unavailable":
                    raise
        if ws:
            made = herdr("tab", "create", "--workspace", ws, "--cwd", path, "--label", label, "--no-focus")
            wid = made["tab"]["tab_id"]
        else:
            made = herdr("workspace", "create", "--cwd", path, "--label", label, "--no-focus")
            wid = made["workspace"]["workspace_id"]
        return {"id": wid, "path": path, "place": made["root_pane"]["pane_id"]}

    def worktree_places(self, repo, path, own=()):
        """What to close for the worktree at `path`: the ids in `own` (the tabs and the
        fallback workspace OKe opened for the job) of the open tabs and workspaces whose panes
        all sit at or under `path`. Both must hold: a tab the user opened in the worktree is not
        OKe's, and herdr reuses ids after a restart, so a stored id alone may be another's now.
        Never a tab with a pane elsewhere: the PM's tab may hold a reviewer split in the
        worktree."""
        want = norm_path(path)
        panes = herdr("pane", "list").get("panes", [])

        def groups(key):
            out = {}
            for p in panes:
                if p.get(key):
                    out.setdefault(p[key], []).append(p)
            return out
        found = []
        for wid, members in sorted(groups("workspace_id").items()):
            if wid in own and all(path_inside(p.get("cwd"), want) for p in members):
                found.append(wid)
        for tid, members in sorted(groups("tab_id").items()):
            if tid in own and members[0].get("workspace_id") not in found \
                    and all(path_inside(p.get("cwd"), want) for p in members):
                found.append(tid)
        return found

    def worktree_remove(self, wid=None, force=False, path=None, repo=None):
        """Close tab or workspace `wid` (a tab id has ":t"; one already gone counts as
        closed), then remove the checkout at `path` (its branch stays). Pass only an id that
        worktree_places found: herdr closes any id it is given."""
        if wid:
            try:
                herdr("tab" if ":t" in wid else "workspace", "close", wid)
                wait_closed(wid)
            except HerdrError as e:
                if e.code not in ("tab_not_found", "workspace_not_found"):
                    raise
        if path and (os.path.isdir(path) or repo):
            remove_checkout(path, repo, force)
