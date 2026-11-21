"""Orca runtime: OKe's agents as Orca supervised workers (`orca orchestration
worker-start`), for a user who runs Claude Code inside Orca. Same methods as
runtime_herdr.HerdrRuntime, and agent records in herdr's shape.

Orca starts the agent itself: OKe cannot pass claude flags, so there is no lean
launch, no --plugin-dir (OKe must be installed as a Claude Code plugin) and no
--resume (a revived agent gets the dead one's transcript instead). Each worker's
caller is the Orca terminal that ran `oke` (ORCA_TERMINAL_HANDLE), so the orchestrator
coordinates its PMs and each PM its workers.

Place: before a start it is the folder the agent starts in; after it, the worker's
first terminal handle. A follow-up may run in a new terminal (`terminal` in the
registry): the first handle stays the agent's place, as OKe's state records it. Errors reuse HerdrError and its codes.
"""
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

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
    (Orca: "may still be starting"), and takes mail: it stays working (measured)."""
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


def mark_live(name, ours, live):
    """Save `live` on registry row `name` while its dispatch is one of `ours` (best effort)."""
    try:
        if name:
            update_registry(lambda r: (r.get(name) or {}).get("dispatch") in ours and r[name].update(live=live))
    except HerdrError as e:
        if e.code != "registry_locked":
            raise  # else best effort: the next look saves it


def sent_live(name, dispatch, result):
    """After a mail that started a follow-up: Orca answers worker state `ready` only once it
    saw claude's turn start, its first hook (measured: `outcome_unknown` when it never came),
    as worker-start does. Mark the follow-up live so a death right after reads gone, also
    when no look caught it live."""
    nxt = (result or {}).get("dispatchId") if isinstance(result, dict) else None
    if nxt and nxt != dispatch and result.get("state") == "ready":
        mark_live(name, (dispatch, nxt), nxt)


def transcript_of(dispatch, cwd):
    """The Claude Code transcript of the session that ran `dispatch`: Orca's preamble
    names the dispatch id in its first message. None when not found."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd or "")
    for folder in [os.path.join(base, "projects", slug)] + glob.glob(os.path.join(base, "projects", "*")):
        for path in sorted(glob.glob(os.path.join(folder, "*.jsonl")), key=os.path.getmtime, reverse=True):
            try:
                with io.open(path, encoding="utf-8", errors="replace") as f:
                    if dispatch in f.read(20000):
                        return path
            except (IOError, OSError):
                continue
    return None


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
        nxt = self.follow_up(show, entry, name) if show and show.get("stage") == "settled" else None
        if not nxt:
            return show
        entry.setdefault("origin", entry["dispatch"])
        entry.update(nxt)
        try:
            # plain run_orca: a status read must never rebind this session's Run (run-use
            # keeps only the Run it names); a fenced keep-open is caught below
            run_orca(["orchestration", "worker-keep-open", "--dispatch", nxt["dispatch"]])
        except HerdrError as e:
            if e.code in FATAL:
                raise  # else it settled and closed already: the next mail reopens it
        if name:
            update_registry(lambda r: r.get(name) and r[name].update(origin=entry["origin"], **nxt))
        return self.show(entry, name)

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
                # the first dispatch stands for the session: Orca's preamble names it at the
                # start of the transcript, where a revive looks for it
                "agent_session": {"value": entry.get("origin") or entry["dispatch"]}}

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
        """The end of the worker's transcript (Orca has no screen read for a chat worker)."""
        entry = load_registry().get(name)
        if not entry:
            raise HerdrError("agent_not_found", name)
        result = run_orca(["orchestration", "worker-read", "--dispatch", entry["dispatch"]])
        lines = []
        for m in ((result.get("transcript") or {}).get("messages") or [])[-10:]:
            for b in m.get("blocks") or []:
                text = b.get("text") or (b.get("name") and "[%s]" % b["name"]) or ""
                if text:
                    lines.append("%s: %s" % (m.get("role", "?"), text.strip()[:400]))
        return "\n".join(lines[-40:]) + "\n"

    def start(self, name, place, argv, prompt, trusted, registered=lambda: None, kind="claude"):
        """Start a supervised Orca worker in folder `place`. Only --model and the resumed
        session of `argv` mean anything here (see the module doc)."""
        argv = list(argv)
        spec = TASK_NOTE + prompt
        # the replaced session: claude --resume, codex resume, opencode --session
        old = next((argv[argv.index(f) + 1] for f in ("--resume", "resume", "--session")
                    if f in argv[:-1]), None)
        if old and kind == "claude":  # codex and opencode keep no Claude transcript
            path = transcript_of(old, place)
            if path:
                spec += ("\n\nThe session you replace died. Its transcript is %s: read its end "
                         "to see where it was." % path.replace("\\", "/"))
        args = ["orchestration", "worker-start", "--spec", spec, "--agent", kind,
                "--worktree", "path:" + place.replace("\\", "/"), "--keep-open", "--task-title", name]
        if "--model" in argv:
            args += ["--model", argv[argv.index("--model") + 1]]
        try:
            result = run_orca(args)
        except HerdrError as e:
            # a revive from a new session: the dead agent's Run may fence the start. Its
            # origin dispatch (--resume) names that Run; relaunch already dropped the
            # registry row, and its follow-ups share the origin's Run.
            if e.code != "consumer_fenced" or not old:
                raise
            try:
                show = run_orca(["orchestration", "worker-show", "--dispatch", old])
            except HerdrError as e2:
                if e2.code in FATAL:
                    raise  # an outage is no crash: the revive try is not spent
                show = None
            rebind(e, show)
            result = run_orca(args)
        entry = {"dispatch": result["dispatchId"], "handle": result["terminal"], "cwd": place, "kind": kind,
                 # worker-start returns only after the agent's first hook (measured): it ran live
                 "live": result["dispatchId"]}
        try:  # Orca titles every tab worker-task_<id>: say who it is (seen live)
            run_orca(["terminal", "rename", "--terminal", entry["handle"], "--title", title_of(name)])
        except HerdrError:
            pass  # only the tab's name
        update_registry(lambda r: r.__setitem__(name, entry))
        registered()
        return self.as_agent(name, entry, {"activity": "working", "liveness": "live"})

    def prompt(self, name, text):
        """Orca mail to the worker: a working one gets it when its turn ends, a finished
        one as a follow-up task. Never typed into its input."""
        entry = load_registry().get(name)
        if not entry:
            raise HerdrError("agent_not_found", name)
        show = self.show(entry, name)  # mail the current dispatch, not one a follow-up replaced
        try:
            sent_live(name, entry["dispatch"], mutate(["orchestration", "send", "--to", "dispatch:" + entry["dispatch"],
                                                       "--subject", "OKe", "--body", text], show))
        except HerdrError as e:
            if NO_WORKER not in e.message and START_FAILED not in e.message:
                raise
            old = entry["dispatch"]
            show = self.show(entry, name)
            if entry["dispatch"] != old and status_of(show):  # a follow-up started meanwhile: mail that one
                sent_live(name, entry["dispatch"], mutate(["orchestration", "send", "--to", "dispatch:" + entry["dispatch"],
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
        show = self.show(entry, name)  # stop the follow-up that runs now, not the dispatch it replaced
        try:
            stopped = mutate(["orchestration", "worker-stop", "--dispatch", entry["dispatch"]], show)
            if stopped.get("alreadySettled"):
                # a settled worker's stop changes nothing and its kept-open terminal stays
                # (measured): release closes it
                mutate(["orchestration", "worker-release", "--dispatch", entry["dispatch"]], show)
        except HerdrError as e:
            if e.code in FATAL:
                raise  # already stopped or released: forget it anyway
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
