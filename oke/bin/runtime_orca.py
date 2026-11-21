"""Orca runtime: OKe's agents as Orca supervised workers in Orca terminals, for a user
who runs Claude Code inside Orca. Same methods as runtime_herdr.HerdrRuntime, and agent
records in herdr's shape.

OKe runs only in Orca terminals, never in an Orca chat (no screen, no mod), whatever the
user's Orca default for new agent tabs: OKe creates each agent's terminal itself with the
same claude command line the herdr runtime starts (`terminal create --command`), then
dispatches the task onto it (`orchestration worker-start --terminal`). Each worker's
caller is the Orca terminal that ran `oke` (ORCA_TERMINAL_HANDLE), so the orchestrator
coordinates its PMs and each PM its workers.

Place: before a start it is the folder the agent starts in; after it, the worker's
first terminal handle. A follow-up may run in a new terminal (`terminal` in the
registry): the first handle stays the agent's place, as OKe's state records it. Errors reuse HerdrError and its codes.
"""
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

from runtime_bg import claude_argv
from runtime_herdr import HerdrError, replace_file

# errors no lookup may read as "agent gone": the caller sees them
FATAL = ("herdr_unavailable", "orca_cli_unknown")
ORCA_TIMEOUT = 300  # seconds: worker-start waits for the agent to take its first prompt
# Orca's worker activity -> herdr agent_status. "waiting" is a permission prompt or a
# question to the user (Orca's claude hooks); an unknown activity of a live worker
# counts as working: silence never proves it stopped.
STATUS = {"working": "working", "idle": "idle", "waiting": "blocked", "blocked": "blocked"}

# Orca puts a "you are a dispatched worker" preamble before this; it forbids subagents
# and helpers unless the task asks, and wants exactly one worker_done per task.
TASK_NOTE = ("This task is an OKe role. It asks you to use subagents and to start helper "
             "agents only through the `oke` commands its skill names. The OKe skill decides "
             "what you do; your report is the report.md file it describes. Each time you "
             "write a report.md, also send worker_done once, with the report's type as "
             "subject.\n\n")


CHAT_REFUSAL = ("OKe runs in an Orca terminal, not a chat: open a terminal tab in Orca and run "
                "/oke:start there.")
# `terminal wait --for tui-idle` names this while claude asks whether to trust the folder (measured)
TRUST_BLOCK = "agent-trust-workspace"
READY_MS = 60000  # one `terminal wait` for a starting agent


def in_chat():
    """True in an Orca chat: Orca sets ORCA_CHAT_SCRATCH_DIR there, and a chat worker's
    handle is a structured one (structworker_...). It has no ORCA_TERMINAL_HANDLE when
    opened from the UI, so without this check OKe would pick the bg runtime there."""
    return bool(os.environ.get("ORCA_CHAT_SCRATCH_DIR")) or         (os.environ.get("ORCA_TERMINAL_HANDLE") or "").startswith("struct")


def shell_line(words, env):
    """`words` as one line of a launch script (launch_command), with the variables `env`
    set for it: PowerShell on Windows, sh elsewhere."""
    if os.name != "nt":
        return " ".join(["exec", "env"] + [shlex.quote("%s=%s" % kv) for kv in sorted(env.items())] +
                        [shlex.quote(w) for w in words])
    q = lambda w: "'%s'" % w.replace("'", "''")  # noqa: E731
    sets = "".join("$env:%s=%s; " % (k, q(v)) for k, v in sorted(env.items()))
    # a bare `claude` goes through the profile's claude function (Orca's account choice)
    first = words[0] if re.match(r"^[\w.-]+$", words[0]) else "& " + q(words[0])
    return sets + " ".join([first] + [q(w) for w in words[1:]])


def launch_command(name, words, env):
    """The short command typed into the agent's new terminal. The launch line itself goes
    into a file under OKe's home: typed in full (about 2.5k characters) PowerShell took
    over a minute to start it (measured). Windows: OKe makes the terminal PowerShell, which
    reads the file with Invoke-Expression (no execution policy applies to it). Elsewhere
    Orca always starts the user's login shell (`--shell` is refused there), which may be
    tcsh: `sh <file>` runs the same in every shell.
    Orca adds its status hooks (`--plugin-dir <ORCA_CLAUDE_AGENT_VIEW_PLUGIN_DIR>`) to a
    startup command only when claude stands first in it (Orca's withClaudeAgentViewPlugin):
    here it never does, so launch() passes that plugin dir itself.
    Returns (command, file): one file per launch (a revive racing a reopen of the same
    agent must not overwrite the other's), written whole before any shell can read it;
    launch() removes it once the agent started."""
    folder = os.path.join(env["OKE_HOME"], "orca-launch")
    os.makedirs(folder, exist_ok=True)
    nt = os.name == "nt"
    path = os.path.join(folder, "%s-%s%s" % (name, uuid.uuid4().hex[:12], ".ps1" if nt else ".sh"))
    with io.open(path + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        f.write(shell_line(words, env) + "\n")
    os.replace(path + ".tmp", path)
    if nt:
        return "iex (Get-Content -Raw -Encoding UTF8 -LiteralPath '%s')" % path.replace("'", "''"), path
    return "sh " + shlex.quote(path), path  # shortcut: tcsh expands `!` even in quotes; a path with one breaks


def child_env():
    """Variables an OKe child needs that its Orca terminal (Orca's own environment) lacks:
    OKe's home and settings, and the caller's Claude config folder (its Orca account)."""
    env = dict((k, v) for k, v in os.environ.items() if k.startswith("OKE_") and k != "OKE_RUNTIME")
    env["OKE_HOME"] = os.environ.get("OKE_HOME") or os.path.join(os.path.expanduser("~"), ".oke")
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_CONFIG_DIR"]
    return env


def orca_argv():
    """The Orca CLI, chosen the way Orca's own orchestration skill does. On Linux a bare
    `orca` is usually the GNOME screen reader (/usr/bin/orca), which takes our flags,
    prints nothing and starts speech: refuse it loudly instead of failing silently."""
    value = os.environ.get("OKE_ORCA") or os.environ.get("ORCA_CLI_COMMAND")
    if not value:
        if os.environ.get("ORCA_DEV_REPO_ROOT"):
            value = "orca-dev"
        elif sys.platform.startswith("linux"):
            value = shutil.which("orca-ide") or "orca"
            if value == "orca" and os.path.realpath(shutil.which("orca") or "") == "/usr/bin/orca":
                raise HerdrError("orca_cli_unknown", "`orca` here is the GNOME screen reader and "
                                 "orca-ide is not on PATH: set OKE_ORCA to the Orca CLI path")
        else:
            value = "orca"
    if value.lstrip().startswith("["):
        return json.loads(value)
    return [value] if os.path.isfile(value) else value.split()


def run_orca(args):
    """Run orca with --json and return its `result`; raise HerdrError with Orca's code."""
    try:
        p = subprocess.Popen(orca_argv() + list(args) + ["--json"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, ValueError) as e:
        raise HerdrError("herdr_unavailable", "orca: %s" % e)
    try:
        out, err = p.communicate(timeout=ORCA_TIMEOUT)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise HerdrError("herdr_unavailable", "orca %s did not answer in %d s" % (" ".join(args[:2]), ORCA_TIMEOUT))
    text = out.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = {}
    if isinstance(data, dict) and data.get("ok"):
        return data.get("result") or {}
    error = data.get("error") if isinstance(data, dict) else None
    error = error if isinstance(error, dict) else {}
    code = error.get("code") or "orca_failed"
    message = error.get("message") or (err.decode("utf-8", "replace").strip() or text or "exit %d" % p.returncode)
    if code == "runtime_unavailable":
        code = "herdr_unavailable"  # Orca is down or restarting: the domain waits, it does not revive
    raise HerdrError(code, message.splitlines()[0] if message else code)


def mutate(args, show):
    """run_orca for a command that changes a worker. Orca binds a Run to the terminal that
    started it, so a new session in the same role (an orchestrator after /oke:start, a
    revived PM) is fenced from the agents it inherited: resume coordinating their Run
    (`run-use`) and try once more (consumer_fenced seen live on a company PC).
    shortcut: run-use keeps only the Run it names, so a session mailing agents of two Runs
    in turn rebinds on each switch (two extra Orca calls, still correct); pass --run to
    worker-start if that ever costs too much."""
    try:
        return run_orca(args)
    except HerdrError as e:
        rebind(e, show)
    return run_orca(args)


def rebind(e, show):
    """Bind this session to the Run of `show` after the fence `e`, or raise `e`."""
    run = (show or {}).get("runId")
    if e.code != "consumer_fenced" or not run:
        raise e
    try:
        run_orca(["orchestration", "run-use", "--id", run])
    except HerdrError as e2:
        if e2.code in FATAL:
            raise  # an outage stays an outage: callers wait instead of counting a refusal
        raise e  # Orca refused the bind too: the fence is what the caller should see


def registry_path():
    home = os.environ.get("OKE_HOME") or os.path.join(os.path.expanduser("~"), ".oke")
    return os.path.join(home, "orca-agents.json")


def load_registry():
    try:
        with io.open(registry_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (IOError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_registry(data):
    path = registry_path()
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False))
    replace_file(tmp, path)


def update_registry(change):
    """Load, `change(registry)`, save, under a lock: PMs start workers at the same time."""
    lock = registry_path() + ".lock"
    for _ in range(100):
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL))
            break
        except OSError:
            try:
                if time.time() - os.path.getmtime(lock) > 30:
                    os.remove(lock)  # left by a killed process
            except OSError:
                pass  # released meanwhile
            time.sleep(0.1)
    else:
        raise HerdrError("registry_locked", "%s is locked" % lock)
    try:
        registry = load_registry()
        change(registry)
        save_registry(registry)
    finally:
        os.remove(lock)


# Orca's refusal to mail a dispatch whose worker it no longer runs
NO_WORKER = "has no worker Orca started that it can continue"
# Orca's refusal to start the follow-up a mail needs (seen live after an Orca restart: "error:
# start_failed: terminal_create: The worker's conversation is open somewhere Orca cannot
# reach; nothing was sent"): the same end, nothing can continue the dispatch
START_FAILED = "start_failed"
# dispatch states nothing continues; "failed" seen live: that follow-up, stage terminal_create.
# A worker that settled with `worker_done --outcome failed` is failed too, but settled and idle.
ENDED = ("abandoned", "failed", "cancelled", "canceled", "stopped")


def status_of(show):
    """herdr's agent_status for a `worker-show` result, or None when the worker is gone
    (no result: Orca no longer knows the dispatch)."""
    if not show:
        return None
    # seen live after the Orca app was killed: "The assigned worker terminal is no longer
    # live after orchestration recovery". Nothing runs the dispatch any more.
    if (show.get("state") in ENDED and show.get("stage") != "settled") or show.get("stage") == "terminal_missing":
        return None
    # A settled task waits for the next one: Orca mail starts it as a follow-up dispatch,
    # in the same kept terminal or, when Orca closed that one, in a new terminal on the
    # same session ("send reopens a closed worker"). Liveness then reads "exited" or
    # "unverifiable" and the terminal may be released (all seen live): still idle.
    if show.get("stage") == "settled" and show.get("activity") in (None, "idle") \
            and show.get("liveness") != "lost":
        return "idle"
    if show.get("terminalState") in ("released", "closed"):
        return None
    if show.get("liveness") in ("exited", "lost"):
        return None
    return STATUS.get(show.get("activity"), "working")


# dispatches whose agent read ended in this process: a dead process never comes back, so
# a dead entry costs one Orca call per look until it is revived, not two
ENDED_SEEN = set()


def agent_ended(dispatch):
    """True when Orca holds no agent status at all for the dispatch: its claude hooks
    recorded the agent's process (pid + start time) and Orca's presence probe dropped the
    row once that process was gone. Measured 2026-11-21 (Orca 1.4.261): taskkill of a
    worker's claude mid-turn leaves the summary at ready/input_accepted/unverifiable for
    good, and `--full` reads liveness reason missing_status with no evidence, 5 s after the
    kill; a live worker reads live/agent_status, one silent for 30 min reads stale_status
    (evidence kept), so neither matches. A starting agent reads missing_status too (a
    follow-up for ~15 s, before its first hook): callers ask only after it read live, or
    Orca said it saw the turn start (worker-start, a mail answered `ready`). One killed
    before its first hook reads start_unknown/turn_start_unobserved, like a slow start
    (Orca: "may still be starting"), and takes mail: show() reads it blocked, never gone."""
    if dispatch in ENDED_SEEN:
        return True
    try:
        result = run_orca(["orchestration", "worker-show", "--dispatch", dispatch, "--full"])
    except HerdrError as e:
        if e.code in FATAL:
            raise
        return False  # unknown is never dead
    p = result.get("projection") if isinstance(result, dict) else None
    liveness = p.get("liveness") if isinstance(p, dict) else None
    evidence = p.get("evidence") if isinstance(p, dict) else None
    if isinstance(liveness, dict) and liveness.get("reason") == "missing_status" \
            and isinstance(evidence, dict) and evidence.get("lastObservedAt") is None:
        ENDED_SEEN.add(dispatch)
        return True
    return False


def start_unknown(show):
    """Orca's own verdict after its 30 s wait for a (follow-up) start: it typed the task but
    never saw claude's turn start. The claude may have died or may still be starting
    (measured both ways look the same; Orca moves it to ready if the turn comes)."""
    return show.get("state") == "start_unknown" and show.get("stage") == "turn_start_unobserved"


STARTUP_NOTE = ("OKe: {name} may have failed to start. Orca typed its task but never saw claude's "
                "turn start (Orca: start_unknown / turn_start_unobserved): its claude may have died, "
                "or may still be starting. This is no dialog: send it no key. To revive it: "
                "`$OKE close {handle}`, then `$OKE revive {name} --reset`. To wait: leave it; it "
                "turns back to working once Orca sees its turn.\n")


def mark_live(name, ours, live):
    """Save `live` on registry row `name` while its dispatch is one of `ours` (best effort)."""
    try:
        if name:
            update_registry(lambda r: (r.get(name) or {}).get("dispatch") in ours and r[name].update(live=live))
    except HerdrError as e:
        if e.code != "registry_locked":
            raise  # else best effort: the next look saves it


def title_of(name):
    """The tab title a person reads: `OKe PM: <repo>`, `OKe worker: <job>`."""
    m = re.match(r"^pm-(.+)-[0-9a-f]{6}$", name)
    if m:
        return "OKe PM: " + m.group(1)
    m = re.match(r"^(.+)-(w\d+|rv\d*)$", name)
    if m:
        return "OKe %s: %s" % ("reviewer" if m.group(2).startswith("rv") else "worker " + m.group(2)[1:], m.group(1))
    return "OKe: " + name


class OrcaRuntime(object):
    name = "orca"
    owns_orchestrator = False  # the orchestrator is the user's own Orca session: never revived

    def inside(self):
        return bool(os.environ.get("ORCA_TERMINAL_HANDLE"))

    def whoami(self):
        """The calling session's place: its Orca terminal (or chat) handle."""
        return self.canonical(os.environ.get("ORCA_TERMINAL_HANDLE"))

    def canonical(self, place):
        """The agent's first handle for a terminal a follow-up moved it to, else `place`."""
        for entry in load_registry().values():
            if place and entry.get("terminal") == place:
                return entry.get("handle")
        return place

    def missing(self):
        return []

    def show(self, entry, name=None):
        """worker-show of the agent's current dispatch. Orca mail to a settled worker
        starts a follow-up dispatch with a new id, so a settled one moves the entry (and
        the registry row `name`) to its follow-up, and keeps that one open too: Orca
        closes a follow-up's terminal two minutes after it finishes otherwise."""
        try:
            show = run_orca(["orchestration", "worker-show", "--dispatch", entry["dispatch"]])
        except HerdrError as e:
            if e.code in FATAL:
                raise
            return None  # unknown dispatch: gone, so the watcher revives it
        dispatch = entry["dispatch"]
        # only an unsettled dispatch is ever checked: a settled one reading live (same terminal)
        # must not overwrite the mark prompt() left for its follow-up
        if isinstance(show, dict) and show.get("liveness") == "live" and entry.get("live") != dispatch \
                and show.get("stage") != "settled":
            entry["live"] = dispatch  # once per dispatch: it started, so missing status means it died
            mark_live(name, (dispatch,), dispatch)
        # claude only: Orca records the agent process from claude's hooks (an entry from
        # before `kind` was stored may be codex: never read dead on absence)
        if isinstance(show, dict) and entry.get("kind") == "claude" and entry.get("live") == dispatch \
                and show.get("stage") != "settled" and show.get("liveness") == "unverifiable" \
                and status_of(show) and agent_ended(dispatch):
            show = dict(show, liveness="exited")
        # in this order: a dispatch seen live (registry live = this very dispatch id, set
        # only from a live read of it or Orca's `ready` for it) proved its claude started, so
        # a dropped status row means that process is gone, even if Orca reads start_unknown
        # (agent_ended needs the row dropped: a claude still alive keeps it, so it is never
        # read gone). Only a start never seen live is left to the user (blocked).
        if isinstance(show, dict) and entry.get("kind") == "claude" and start_unknown(show) and status_of(show):
            show = dict(show, activity="waiting")  # blocked: the user decides, nothing reads it dead
        nxt = self.follow_up(show, entry, name) if show and show.get("stage") == "settled" else None
        if not nxt:
            return show
        self.adopt(entry, name, nxt)
        return self.show(entry, name)

    def adopt(self, entry, name, nxt):
        """Move the entry (and registry row `name`, unless a revive replaced it) to the
        follow-up `nxt` = {dispatch, terminal, ...} and keep its terminal open."""
        old = entry["dispatch"]
        entry.setdefault("origin", old)
        entry.update(nxt)
        # first: a failed keep-open below must not lose the move (or its live mark); a keep-open
        # lost to an outage is not retried: Orca may close the terminal later, the next mail reopens it
        if name:
            update_registry(lambda r: (r.get(name) or {}).get("dispatch") in (old, nxt["dispatch"])
                            and r[name].update(origin=entry["origin"], **nxt))
        try:
            # plain run_orca: a status read must never rebind this session's Run (run-use
            # keeps only the Run it names); a fenced keep-open is caught below
            run_orca(["orchestration", "worker-keep-open", "--dispatch", nxt["dispatch"]])
        except HerdrError as e:
            if e.code in FATAL:
                raise  # else it settled and closed already: the next mail reopens it

    def sent(self, entry, name, result):
        """After a mail: when it started a follow-up (a new `dispatchId`, in the same or a
        new terminal), the entry moves to it now, not on a later look (seen live: the row
        kept the old dispatch and terminal a long while). Orca answers state `ready` only
        once it saw claude's turn start, its first hook (measured: `outcome_unknown` when it
        never came), as worker-start does: mark it live, so a death right after reads gone
        also when no look caught it live."""
        nxt = result.get("dispatchId") if isinstance(result, dict) else None
        if not nxt or nxt == entry["dispatch"]:
            return
        ready = result.get("state") == "ready"
        try:  # the mail went in: nothing here may fail it (a retry would mail twice)
            try:
                full = run_orca(["orchestration", "worker-show", "--dispatch", nxt, "--full"])
            except HerdrError:
                full = None
            worker = (full.get("worker") if isinstance(full, dict) else None) or {}
            if not worker.get("agentTerminalHandle"):
                # its terminal unknown: leave the row, a later look moves it (follow_up finds
                # the new terminal); keep only the live mark, which that move carries over
                if ready:
                    mark_live(name, (entry["dispatch"], nxt), nxt)
                return
            moved = {"dispatch": nxt, "terminal": worker["agentTerminalHandle"], "seen": None}
            if ready:
                moved["live"] = nxt
            self.adopt(entry, name, moved)
        except (HerdrError, OSError):
            pass  # an outage or a locked registry: a later look finds the follow-up

    def follow_up(self, show, entry, name=None):
        """The follow-up dispatch that replaced the entry's, as {dispatch, terminal}, or None."""
        if not show.get("runId"):
            return None
        here = entry.get("terminal") or entry["handle"]
        try:
            rows = run_orca(["orchestration", "worker-list", "--run", show["runId"], "--limit", "100",
                             "--full"]).get("workers") or []
            for row in rows:  # one in the same terminal owns it now
                resource = row.get("resource") or {}
                owner = resource.get("ownerDispatchId")
                if resource.get("terminalHandle") == here and resource.get("releaseState") != "released"                         and owner and owner != entry["dispatch"]:
                    return {"dispatch": owner, "terminal": here}
                # only newer rows: in a terminal OKe opened every dispatch keeps its own
                # retained row on it (measured), so an older one would bounce the entry back
                if row.get("dispatchId") == entry["dispatch"]:
                    break
            # Orca closed the terminal: a newer dispatch (rows come newest first) names
            # this one as the dispatch it follows up. Each row is asked once: `seen` is
            # the newest row already asked, so a long rest costs one list per look.
            newest = rows[0].get("dispatchId") if rows else None
            for row in rows:
                if row.get("dispatchId") in (entry["dispatch"], entry.get("seen")):
                    break
                full = run_orca(["orchestration", "worker-show", "--dispatch", row["dispatchId"], "--full"])
                if (full.get("dispatch") or {}).get("followUpOf") == entry["dispatch"]:
                    terminal = (full.get("worker") or {}).get("agentTerminalHandle") or here
                    return {"dispatch": row["dispatchId"], "terminal": terminal, "seen": None}
            if newest and newest != entry.get("seen"):
                entry["seen"] = newest
                if name:
                    update_registry(lambda r: r.get(name) and r[name].update(seen=newest))
        except HerdrError as e:
            if e.code in FATAL:
                raise
        return None

    def as_agent(self, name, entry, show):
        return {"name": name, "pane_id": entry["handle"], "terminal_id": entry.get("terminal") or entry["handle"],
                "agent_status": status_of(show) or "gone", "cwd": entry.get("cwd"),
                # claude's own session id (OKe gave it --session-id), which a revive resumes
                "agent_session": {"value": entry.get("session")}}

    def agents(self):
        out = []
        for name, entry in sorted(load_registry().items()):
            show = self.show(entry, name)
            if status_of(show):
                out.append(self.as_agent(name, entry, show))
        return out

    def agent(self, name):
        entry = load_registry().get(name)
        show = self.show(entry, name) if entry else {}
        if not status_of(show):
            raise HerdrError("agent_not_found", name)
        return self.as_agent(name, entry, show)

    def screen(self, name):
        """The end of the worker's transcript (worker-read works also once its terminal is closed)."""
        entry = load_registry().get(name)
        if not entry:
            raise HerdrError("agent_not_found", name)
        if entry.get("kind") == "claude":
            try:
                show = run_orca(["orchestration", "worker-show", "--dispatch", entry["dispatch"]])
            except HerdrError as e:
                if e.code in FATAL:
                    raise
                show = {}
            if isinstance(show, dict) and start_unknown(show):
                return STARTUP_NOTE.format(name=name, handle=entry["handle"])
        result = run_orca(["orchestration", "worker-read", "--dispatch", entry["dispatch"]])
        lines = []
        for m in ((result.get("transcript") or {}).get("messages") or [])[-10:]:
            for b in m.get("blocks") or []:
                text = b.get("text") or (b.get("name") and "[%s]" % b["name"]) or ""
                if text:
                    lines.append("%s: %s" % (m.get("role", "?"), text.strip()[:400]))
        return "\n".join(lines[-40:]) + "\n"

    def start(self, name, place, argv, prompt, trusted, registered=lambda: None, kind="claude"):
        """Start the agent in a new Orca terminal in folder `place` and dispatch its first
        prompt onto it as a supervised worker."""
        argv = list(argv)
        session = argv[argv.index("--resume") + 1] if "--resume" in argv[:-1] else None
        if session and session.startswith("ctx_"):
            # shortcut: a record from before terminal mode holds an Orca dispatch id, which
            # claude cannot resume: start fresh. Drop once no such record is left.
            i = argv.index("--resume")
            del argv[i:i + 2]
            session = None
        bare = [w for i, w in enumerate(argv) if w != "--resume" and (i == 0 or argv[i - 1] != "--resume")]
        if kind == "claude" and not session:
            session = str(uuid.uuid4())
            argv += ["--session-id", session]
        handle = self.launch(name, place, argv, trusted, kind)
        result = self.dispatch(handle, place, TASK_NOTE + prompt, name)
        entry = {"dispatch": result["dispatchId"], "handle": handle, "cwd": place, "kind": kind,
                 # worker-start returns only after the agent's first hook (measured): it ran live
                 "live": result["dispatchId"],
                 # what reopen() starts again after its terminal was closed
                 "session": session if kind == "claude" else None, "argv": bare}
        update_registry(lambda r: r.__setitem__(name, entry))
        registered()
        return self.as_agent(name, entry, {"activity": "working", "liveness": "live"})

    def launch(self, name, place, argv, trusted, kind):
        """A new Orca terminal in `place` running the agent, ready for its first prompt.
        Answers claude's folder trust dialog only when `trusted()` allows it; any other
        stop closes the terminal again (OKe could not track it)."""
        program = claude_argv() if kind == "claude" else [kind]
        view = os.environ.get("ORCA_CLAUDE_AGENT_VIEW_PLUGIN_DIR")  # set in every Orca terminal (measured)
        if kind == "claude" and not view:
            raise HerdrError("orca_hooks_unknown", "ORCA_CLAUDE_AGENT_VIEW_PLUGIN_DIR is not set: run OKe "
                             "in an Orca terminal (Orca 1.4.261 or later)")
        hooks = ["--plugin-dir", view] if kind == "claude" else []
        args = ["terminal", "create", "--worktree", "path:" + place.replace("\\", "/"), "--title", title_of(name),
                "--command"]
        command, script = launch_command(name, program + hooks + list(argv), child_env())
        args.append(command)
        if os.name == "nt":
            args += ["--shell", "powershell.exe"]  # see shell_line
        try:
            return self.ready(name, run_orca(args)["terminal"]["handle"], trusted, kind)
        finally:
            try:  # the shell read it once the agent is up; a failed start closed the terminal
                os.remove(script)
            except OSError:
                pass

    def ready(self, name, handle, trusted, kind):
        """Waits for the agent in its new terminal `handle` (see launch)."""
        try:
            for _ in range(3):
                try:
                    wait = run_orca(["terminal", "wait", "--terminal", handle, "--for", "tui-idle",
                                     "--timeout-ms", str(READY_MS)]).get("wait") or {}
                except HerdrError as e:
                    if e.code != "timeout":  # seen live: a claude that never came up
                        raise
                    wait = {"status": "timeout"}
                if wait.get("satisfied"):
                    return handle
                if wait.get("blockedReason") != TRUST_BLOCK or wait.get("status") == "exited" or not trusted():
                    break
                # claude preselects "No, exit": Down, then Enter (codex preselects trust)
                if kind == "claude":
                    run_orca(["terminal", "send", "--terminal", handle, "--text", "\x1b[B"])
                run_orca(["terminal", "send", "--terminal", handle, "--text", "\r"])
            try:
                screen = "\n".join(run_orca(["terminal", "read", "--terminal", handle, "--screen"])
                                   .get("terminal", {}).get("tail") or [])
            except HerdrError:
                screen = ""
            raise HerdrError("agent_not_ready", "%s did not start (%s):\n%s" % (
                name, wait.get("blockedReason") or wait.get("status") or "not ready", screen.strip()[-1500:]))
        except BaseException:
            self.close_terminal(handle)
            raise

    def dispatch(self, handle, place, spec, name):
        """worker-start onto the agent in `handle`; closes the terminal when it fails.
        No consumer_fenced retry here, unlike mutate(): measured 2026-11-21, a coordinator
        whose Run another terminal took (`run-use`) gets a new Run from worker-start
        (runCreated), while its send to the old Run's workers is fenced."""
        args = ["orchestration", "worker-start", "--terminal", handle, "--worktree",
                "path:" + place.replace("\\", "/"), "--spec", spec, "--keep-open", "--task-title", name]
        try:
            for tries in range(5):
                try:
                    result = run_orca(args)
                    break
                except HerdrError as e:
                    # Orca refuses a terminal whose agent it does not recognise yet (measured
                    # right after the create): nothing was typed, so a retry is safe
                    if e.code != "agent_unconfigured" or tries == 4:
                        raise
                    time.sleep(2)
        except BaseException:
            self.close_terminal(handle)
            raise
        try:  # Orca titles every tab worker-task_<id>: say who it is (seen live)
            run_orca(["terminal", "rename", "--terminal", handle, "--title", title_of(name)])
        except HerdrError:
            pass  # only the tab's name
        return result

    def close_terminal(self, handle):
        try:
            run_orca(["terminal", "close", "--terminal", handle])
        except HerdrError as e:
            if e.code in FATAL:
                raise  # else closed already

    def terminal_gone(self, entry):
        """True when the agent's terminal was closed (measured: connected and writable
        false; an unknown handle, as after an Orca restart, is terminal_handle_stale).
        `orphaned` (true there too) is not read: what it means for an open terminal is
        not measured. Any
        other error raises: reading an open terminal as closed would start a second claude
        on the same session."""
        try:
            t = run_orca(["terminal", "show", "--terminal", entry.get("terminal") or entry["handle"]]).get("terminal") or {}
        except HerdrError as e:
            if e.code == "terminal_handle_stale":
                return True
            raise
        return t.get("connected") is False or t.get("writable") is False

    def reopen(self, entry, name, text):
        """Mail for a settled agent whose terminal was closed: start it again in a new
        terminal on its own session, with its own command line, and dispatch the mail
        onto it. Orca's own reopen (what `send` does here) runs a bare `claude --resume`
        without OKe's flags (measured: no allowed tools, settings or plugin dir). This can
        outlast deliver's lock (LOCK_STALE), so from launch to the move the row's
        `reopening` names the new terminal, for prompt() to check."""
        mine = entry["dispatch"]
        handle = self.launch(name, entry["cwd"], entry["argv"] + ["--resume", entry["session"]],
                             lambda: False, entry.get("kind") or "claude")
        # shortcut: a deliver killed during launch leaves an unmarked idle terminal; rare (seconds)
        try:
            update_registry(lambda r: (r.get(name) or {}).get("dispatch") == mine
                            and r[name].update(reopening={"terminal": handle}))
            result = self.dispatch(handle, entry["cwd"], TASK_NOTE + text, name)
        except BaseException:
            self.close_terminal(handle)  # nothing went in: the next deliver starts afresh
            update_registry(lambda r: (r.get(name) or {}).get("reopening") == {"terminal": handle}
                            and r[name].update(reopening=None))
            raise
        try:  # the mail went in: nothing here may fail it (a retry would start a second claude)
            self.adopt(entry, name, {"dispatch": result["dispatchId"], "terminal": handle, "seen": None,
                                     "live": result["dispatchId"], "reopening": None})
        except (HerdrError, OSError):
            pass  # the mark stays: the next deliver finds the dispatch on its terminal

    def dispatch_on(self, terminal):
        """The dispatch running in `terminal`, or None. Lists the Run bound to this session,
        which a reopen's worker-start used."""
        for row in run_orca(["orchestration", "worker-list", "--limit", "100", "--full"]).get("workers") or []:
            resource = row.get("resource") or {}
            if resource.get("terminalHandle") == terminal and resource.get("ownerDispatchId") \
                    and resource.get("releaseState") != "released":
                return resource["ownerDispatchId"]
        return None

    def prompt(self, name, text):
        """Orca mail to the worker: a working one gets it when its turn ends, a finished
        one as a follow-up task. Never typed into its input. None: another deliver is
        reopening the agent, the mail waits in the inbox (not a failed delivery)."""
        entry = load_registry().get(name)
        if not entry:
            raise HerdrError("agent_not_found", name)
        mark = entry.get("reopening")
        if mark:
            if not self.terminal_gone({"terminal": mark["terminal"]}):
                dispatch = self.dispatch_on(mark["terminal"])
                if not dispatch:
                    # shortcut: a deliver killed between launch and dispatch leaves this open
                    # terminal and its mark; closing that terminal lets the next mail reopen
                    return None  # its worker-start still runs: never start a second claude
                self.adopt(entry, name, {"dispatch": dispatch, "terminal": mark["terminal"], "seen": None,
                                         "live": dispatch, "reopening": None})
            else:  # that reopen failed or its claude ended: start afresh
                update_registry(lambda r: (r.get(name) or {}).get("reopening") == mark
                                and r[name].update(reopening=None))
                entry["reopening"] = None
        show = self.show(entry, name)  # mail the current dispatch, not one a follow-up replaced
        if entry.get("session") and entry.get("argv") is not None and (show or {}).get("stage") == "settled" \
                and self.terminal_gone(entry):
            # shortcut: the terminal may close between this look and the send; Orca then
            # reopens it without OKe's flags. Rare: OKe keeps every agent's terminal open.
            self.reopen(entry, name, text)
            return True
        try:
            self.sent(entry, name, mutate(["orchestration", "send", "--to", "dispatch:" + entry["dispatch"],
                                           "--subject", "OKe", "--body", text], show))
        except HerdrError as e:
            if NO_WORKER not in e.message and START_FAILED not in e.message:
                raise
            old = entry["dispatch"]
            show = self.show(entry, name)
            if entry["dispatch"] != old and status_of(show):  # a follow-up started meanwhile: mail that one
                self.sent(entry, name, mutate(["orchestration", "send", "--to", "dispatch:" + entry["dispatch"],
                                               "--subject", "OKe", "--body", text], show))
                return True
            # After the Orca app or the agent's claude died, Orca can no longer continue the
            # dispatch, though worker-show still reads it settled (seen live). The agent is
            # gone: forget it, so the wait reports `gone` and a revive may start it again.
            ours = (old, entry["dispatch"])  # the failed follow-up too, when show moved to it
            for dispatch in sorted(set(ours)):  # the old one holds the retained terminal
                try:
                    run_orca(["orchestration", "worker-release", "--dispatch", dispatch])
                except HerdrError as e2:
                    if e2.code in FATAL:
                        raise
            self.close_terminal(entry.get("terminal") or entry["handle"])  # OKe opened it: no tab left behind
            # only this dispatch's row: a revive may have registered a new one meanwhile
            update_registry(lambda r: r.pop(name) if (r.get(name) or {}).get("dispatch") in ours else None)
            raise HerdrError("agent_not_found", "%s is gone: Orca cannot continue its dispatch" % name)
        return True

    def keys(self, name, keys):
        raise HerdrError("unsupported", "an Orca worker takes no keys: answer it in Orca (%s)" % name)

    def attach(self, name):
        entry = load_registry().get(name)
        if not entry:
            raise HerdrError("agent_not_found", name)
        return "open %s in Orca (task %s)" % (entry.get("terminal") or entry["handle"], name)

    def split(self, near, cwd):
        return cwd

    def new_place(self, near, cwd):
        return cwd

    def place_for_repo(self, repo, label):
        # worker-start needs Orca to know it; Orca refuses a plain folder as kind git
        kind = "git" if subprocess.call(["git", "-C", repo, "rev-parse"], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL) == 0 else "folder"
        run_orca(["repo", "add", "--path", repo.replace("\\", "/"), "--kind", kind])
        return repo

    def entry_of(self, place):
        for name, entry in load_registry().items():
            if entry.get("handle") == place:
                return name, entry
        return None, None

    def place_cwd(self, place):
        if os.path.isdir(place):
            return place
        return (self.entry_of(place)[1] or {}).get("cwd") or ""

    def place_id(self, place):
        return place if self.entry_of(place)[1] else None

    def place_exists(self, place):
        name, entry = self.entry_of(place)
        return bool(entry and status_of(self.show(entry, name)))

    def processes(self, place):
        return []

    def close(self, place):
        name, entry = self.entry_of(place)
        if not entry:
            if os.path.isdir(place):
                return  # a folder: never started
            # a wrong place (an agent name, a follow-up's terminal) must not read as closed
            raise HerdrError("agent_not_found", "no open OKe agent in %s: already closed (job cancel "
                             "closes a job's children), or not a pane spawn printed" % place)
        self.show(entry, name)  # close the terminal a follow-up runs in now
        # worker-stop changes nothing in a terminal Orca did not start ("The worker terminal
        # is external", measured); closing the terminal ends the dispatch (failed / process_exited)
        self.close_terminal(entry.get("terminal") or entry["handle"])
        update_registry(lambda r: r.pop(name, None))

    def worktree_create(self, repo, branch, label):
        """An Orca worktree: worker-start only runs in a workspace Orca knows."""
        result = run_orca(["worktree", "create", "--repo", "path:" + repo.replace("\\", "/"), "--name", "oke-" + label,
                           "--branch", branch, "--no-parent", "--setup", "skip"])
        wt = result.get("worktree") or result
        path = wt.get("path")
        if not path:
            raise HerdrError("worktree_failed", "orca worktree create gave no path")
        return {"id": wt.get("id") or wt.get("worktreeId"), "path": path, "place": path}

    def worktree_remove(self, wid):
        run_orca(["worktree", "rm", "--worktree", "id:" + wid])
