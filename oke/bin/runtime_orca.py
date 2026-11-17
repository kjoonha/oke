"""Orca runtime: OKe's agents as Orca supervised workers (`orca orchestration
worker-start`), for a user who runs Claude Code inside Orca. Same methods as
runtime_herdr.HerdrRuntime, and agent records in herdr's shape.

Orca starts the agent itself: OKe cannot pass claude flags, so there is no lean
launch, no --plugin-dir (OKe must be installed as a Claude Code plugin) and no
--resume (a revived agent gets the dead one's transcript instead). Each worker's
caller is the Orca terminal that ran `oke` (ORCA_TERMINAL_HANDLE), so the orchestrator
coordinates its PMs and each PM its workers.

Place: before a start it is the folder the agent starts in; after it, the worker's
terminal handle. Errors reuse HerdrError and its codes.
"""
import glob
import io
import json
import os
import re
import subprocess

from runtime_herdr import HerdrError

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
    value = os.environ.get("OKE_ORCA") or os.environ.get("ORCA_CLI_COMMAND") or "orca"
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
    with io.open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(path + ".tmp", path)


def status_of(show):
    """herdr's agent_status for a `worker-show` result, or None when the worker is gone."""
    if show.get("liveness") in ("exited", "lost"):
        return None
    if show.get("terminalState") in ("released", "closed"):
        return None
    # A settled task waits for the next one; Orca then often reports liveness
    # "unverifiable" and no activity for a chat worker (seen live).
    if show.get("stage") == "settled" and show.get("activity") in (None, "idle"):
        return "idle"
    return STATUS.get(show.get("activity"), "working")


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


class OrcaRuntime(object):
    name = "orca"
    Error = HerdrError

    owns_orchestrator = False  # the orchestrator is the user's own Orca session: never revived

    def inside(self):
        return bool(os.environ.get("ORCA_TERMINAL_HANDLE"))

    def whoami(self):
        """The calling session's place: its Orca terminal (or chat) handle."""
        return os.environ.get("ORCA_TERMINAL_HANDLE")

    def missing(self):
        return []

    def show(self, entry):
        try:
            return run_orca(["orchestration", "worker-show", "--dispatch", entry["dispatch"]])
        except HerdrError as e:
            if e.code == "herdr_unavailable":
                raise
            return {}

    def as_agent(self, name, entry, show):
        return {"name": name, "pane_id": entry["handle"], "terminal_id": entry["handle"],
                "agent_status": status_of(show) or "gone", "cwd": entry.get("cwd"),
                # the dispatch stands for the session: a revive finds its transcript by it
                "agent_session": {"value": entry["dispatch"]}}

    def agents(self):
        out = []
        for name, entry in sorted(load_registry().items()):
            show = self.show(entry)
            if status_of(show):
                out.append(self.as_agent(name, entry, show))
        return out

    def agent(self, name):
        entry = load_registry().get(name)
        show = self.show(entry) if entry else {}
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
        """Start a supervised Orca worker in folder `place`. Only --model and --resume of
        `argv` mean anything here (see the module doc)."""
        argv = list(argv)
        spec = TASK_NOTE + prompt
        if "--resume" in argv:
            old = argv[argv.index("--resume") + 1]
            path = transcript_of(old, place)
            if path:
                spec += ("\n\nThe session you replace died. Its transcript is %s: read its end "
                         "to see where it was." % path.replace("\\", "/"))
        args = ["orchestration", "worker-start", "--spec", spec, "--agent", kind,
                "--worktree", "path:" + place.replace("\\", "/"), "--keep-open", "--task-title", name]
        if "--model" in argv:
            args += ["--model", argv[argv.index("--model") + 1]]
        result = run_orca(args)
        entry = {"dispatch": result["dispatchId"], "handle": result["terminal"], "cwd": place}
        registry = load_registry()
        registry[name] = entry
        save_registry(registry)
        registered()
        return self.as_agent(name, entry, {"activity": "working", "liveness": "live"})

    def prompt(self, name, text):
        """Orca mail to the worker: a working one gets it when its turn ends, a finished
        one as a follow-up task. Never typed into its input."""
        entry = load_registry().get(name)
        if not entry:
            raise HerdrError("agent_not_found", name)
        run_orca(["orchestration", "send", "--to", "dispatch:" + entry["dispatch"],
                  "--subject", "OKe", "--body", text])
        return True

    def keys(self, name, keys):
        raise HerdrError("unsupported", "an Orca worker takes no keys: answer it in Orca (%s)" % name)

    def attach(self, name):
        entry = load_registry().get(name)
        if not entry:
            raise HerdrError("agent_not_found", name)
        return "open %s in Orca (task %s)" % (entry["handle"], name)

    def split(self, near, cwd):
        return cwd

    def new_place(self, near, cwd):
        return cwd

    def place_for_repo(self, repo, label):
        run_orca(["repo", "add", "--path", repo.replace("\\", "/")])  # worker-start needs Orca to know it
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
        entry = self.entry_of(place)[1]
        return bool(entry and status_of(self.show(entry)))

    def has_claude(self, place):
        return self.place_exists(place)

    def processes(self, place):
        return []

    def close(self, place):
        name, entry = self.entry_of(place)
        if not entry:
            return  # a folder: never started
        try:
            run_orca(["orchestration", "worker-stop", "--dispatch", entry["dispatch"]])
        except HerdrError as e:
            if e.code == "herdr_unavailable":
                raise  # already settled or stopped: forget it anyway
        registry = load_registry()
        registry.pop(name, None)
        save_registry(registry)

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
