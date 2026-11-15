"""herdr runtime: the only place OKe runs the herdr CLI.

The domain (oke.py) reaches herdr through HerdrRuntime's methods; the plain functions
below are the plumbing they share. Agent records keep herdr's shape: name, pane_id,
agent_status, agent_session, terminal_id.
"""
import json
import os
import subprocess
import time

REQUIRED_COMMANDS = ("agent start", "agent prompt", "agent wait", "agent read",
                     "agent send-keys", "pane split", "worktree create")


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
START_TIMEOUT_MS = 90000
READY_MARKERS = {"codex": "for shortcuts"}  # the input line's hint: no dialog left
READY_TRIES = 10  # orchestrator and PM are always claude
# Startup screens OKe answers by itself: (text on screen, kind or None for all, keys).
SAFE_DIALOGS = [
    ("update now", "codex", ["esc"]),  # "Update now / Skip": esc skips
    # codex's hook review panel: esc closes it without trusting, so the hook stays off
    ("hook needs review", "codex", ["esc"]),
    # "Review hooks / Trust all / Continue without trusting": esc skips, hooks stay off.
    # herdr shows this screen as idle, so a first prompt's Enter would pick "Review".
    ("continue without trusting", "codex", ["esc"]),
]


def session_of(agent):
    session = agent.get("agent_session")
    return session.get("value") if isinstance(session, dict) else None


def list_agents():
    return herdr("agent", "list").get("agents", [])


class DialogError(Exception):
    """The new agent stopped at a dialog OKe may not answer. The message is the screen."""


def start_agent(agent, pane, claude_args, prompt, trusted, registered=lambda: None, kind="claude"):
    """Start claude in `pane`, answer the trust dialog only when `trusted()` allows it,
    send the first prompt and make sure the first turn started. `registered` runs once
    herdr knows the name. Returns herdr's agent info."""
    try:
        # herdr's 30 s default was too short live for a claude resuming a long session
        herdr("agent", "start", agent, "--kind", kind, "--pane", pane, "--timeout", str(START_TIMEOUT_MS),
              "--", *claude_args)
    except HerdrError as e:
        if e.code != "agent_not_ready":
            raise
        registered()  # herdr keeps the name even though the agent is not ready
        answer_startup_dialogs(agent, kind, trusted)
    registered()
    if kind != "claude":
        # codex can show a panel after herdr already calls it ready; a prompt typed
        # then is lost (seen live). Close such panels first.
        close_startup_panels(agent, kind, READY_MARKERS.get(kind))

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
        if kind != "claude":
            # codex asks about untrusted hooks only when the first prompt is submitted;
            # herdr shows that as idle. Skip it; the prompt text stays in the input.
            close_startup_panels(agent, kind, ready=None)
        # Seen live: the prompt text sat in the input box with its Enter lost.
        # Enter on an empty input does nothing, so one more is safe.
        herdr("agent", "send-keys", agent, "enter")
        try:
            herdr("agent", "wait", agent, "--until", "working", "--until", "blocked", "--timeout", "20000")
        except HerdrError as e:
            if e.code != "timeout":
                raise
        info = herdr("agent", "get", agent).get("agent", {})
    return info


def startup_keys(screen, kind, trusted):
    """Keys for a startup screen OKe may answer, or None."""
    low = screen.lower()
    if TRUST_MARKER in low:
        if not trusted():
            return None
        # claude preselects "No, exit"; codex preselects "Trust and continue"
        return ["down", "enter"] if kind == "claude" else ["enter"]
    for text, only, keys in SAFE_DIALOGS:
        if text in low and only in (None, kind):
            return keys
    return None


def close_startup_panels(agent, kind, ready):
    """Skip the safe screens (update offer, hook trust) shown while herdr already
    calls the agent ready or idle. With a `ready` marker, look until the input line
    shows it: the next screen can take a moment to draw."""
    for _ in range(READY_TRIES if ready else 3):
        try:
            screen = herdr_text("agent", "read", agent, "--source", "visible", "--lines", "40")
        except HerdrError:
            return  # cannot look: the prompt reports a real outage
        keys = startup_keys(screen, kind, lambda: False)
        if keys:
            herdr("agent", "send-keys", agent, *keys)
        elif not ready or ready in screen.lower():
            return
        time.sleep(DIALOG_POLL)


def answer_startup_dialogs(agent, kind, trusted):
    """Answer the startup screens one by one (codex may offer an update, then ask for
    trust). Anything else is the user's call: DialogError with the screen."""
    for _ in range(3):
        # The dialog can still be drawing when herdr reports the agent blocked.
        for _ in range(DIALOG_TRIES):
            screen = herdr_text("agent", "read", agent, "--source", "visible", "--lines", "40")
            keys = startup_keys(screen, kind, trusted)
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


def pane_has_claude(pane):
    try:
        info = herdr("pane", "process-info", "--pane", pane).get("process_info", {})
    except HerdrError as e:
        if e.code == "herdr_unavailable":
            raise
        return False
    return any("claude" in (p.get("name") or "").lower() for p in info.get("foreground_processes") or [])


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


class HerdrRuntime(object):
    """What the OKe domain needs from a runtime, done with herdr panes. A place is a
    herdr pane id. Errors are HerdrError with herdr's own codes."""

    name = "herdr"
    Error = HerdrError

    def inside(self):
        return os.environ.get("HERDR_ENV") == "1"

    def missing(self):
        return missing_commands()

    def agents(self):
        return list_agents()

    def agent(self, name):
        return herdr("agent", "get", name).get("agent", {})

    def screen(self, name):
        return herdr_text("agent", "read", name, "--source", "visible", "--lines", "40")

    def start(self, name, place, argv, prompt, trusted, registered=lambda: None, kind="claude"):
        return start_agent(name, place, argv, prompt, trusted, registered, kind)

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

    def has_claude(self, place):
        return pane_has_claude(place)

    def processes(self, place):
        """Foreground processes in a place (name, argv, cwd); [] when unknown."""
        try:
            info = herdr("pane", "process-info", "--pane", place).get("process_info", {})
        except HerdrError:
            return []
        return info.get("foreground_processes") or []

    def close(self, place):
        herdr("pane", "close", place)
