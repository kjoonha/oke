#!/usr/bin/env python3
"""OKe helper: fixed procedures for the orchestrator / PM skills.

Python 3.6 compatible, standard library only.
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

PHASES = ("intake", "planning", "awaiting_approval", "working", "reviewing",
          "awaiting_merge", "merging", "done", "failed")
EMPTY_STATE = {"orchestrator": {}, "repos": {}, "jobs": {}}


def fail(message):
    sys.stderr.write(message + "\n")
    return 1


# ---------------------------------------------------------------- files

def native_path(path, windows=None):
    """Turn a Git Bash style /c/x path into c:/x so Windows APIs accept it."""
    if windows is None:
        windows = os.name == "nt"
    if windows:
        m = re.match(r"^/([a-zA-Z])(/.*)?$", path)
        if m:
            return m.group(1) + ":" + (m.group(2) or "/")
    return path


def normalize_path(path, windows=None):
    if windows is None:
        windows = os.name == "nt"
    p = native_path(path.replace("\\", "/"), windows)
    if windows:
        p = p.lower()
    return p.rstrip("/") or "/"


def oke_home():
    home = os.environ.get("OKE_HOME") or os.path.join(os.path.expanduser("~"), ".oke")
    home = os.path.abspath(native_path(home))
    if not os.path.isdir(home):
        os.makedirs(home)
    return home


def load_config():
    home = oke_home()
    config = {
        "autonomy": "balanced",
        "wiki_path": os.path.join(home, "wiki"),
        "models": {"pm": "opus", "worker": "sonnet", "reviewer": "sonnet"},
    }
    try:
        with io.open(os.path.join(home, "config.json"), encoding="utf-8") as f:
            user = json.load(f)
    except (IOError, OSError, ValueError):
        user = {}
    if isinstance(user, dict):
        models = user.pop("models", None)
        config.update(user)
        if isinstance(models, dict):
            config["models"].update(models)
    config["wiki_path"] = os.path.abspath(native_path(os.path.expanduser(config["wiki_path"])))
    return config


def read_header(path):
    """Return the `key: value` lines above the first `---`, or None."""
    try:
        with io.open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (IOError, OSError, UnicodeDecodeError):
        return None
    header = {}
    for line in lines:
        if line.strip() == "---":
            if "seq" in header:
                try:
                    header["seq"] = int(header["seq"])
                except ValueError:
                    pass
            return header
        if ":" in line:
            key, value = line.split(":", 1)
            header[key.strip()] = value.strip()
    return None


def write_json_atomic(path, obj):
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = "%s.%d.tmp" % (path, os.getpid())  # two processes may write the same file
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))
        f.write(u"\n")
    replace_file(tmp, path)


def replace_file(src, dst):
    for attempt in range(20):
        try:
            os.replace(src, dst)
            return
        except OSError:  # Windows: another process has the file open for a moment
            if attempt == 19:
                raise
            time.sleep(0.05)


def read_json(path, default):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return default


def state_path():
    return os.path.join(oke_home(), "state.json")


class StateError(Exception):
    pass


def load_state(recover=False):
    """Only `oke status` (run by the orchestrator, the single writer) passes
    recover=True and may set a broken state.json aside."""
    path = state_path()
    state = json.loads(json.dumps(EMPTY_STATE))
    if not os.path.exists(path):
        return state
    try:
        with io.open(path, encoding="utf-8") as f:
            text = f.read()
    except (IOError, OSError) as e:
        raise StateError("cannot read state.json right now: %s" % e)
    try:
        loaded = json.loads(text)
        if not isinstance(loaded, dict):
            raise ValueError("not an object")
    except ValueError:
        if not recover:
            raise StateError("state.json is unreadable: run `oke status` in the orchestrator")
        os.replace(path, path + ".bad")
        state["_recovered"] = True
        return state
    for key in EMPTY_STATE:
        if isinstance(loaded.get(key), dict):
            state[key] = loaded[key]
    return state


def save_state(state):
    write_json_atomic(state_path(), dict((k, v) for k, v in state.items() if k != "_recovered"))


# A lock older than this is left over from a crashed holder. A revive takes about a
# minute; this is not a rule about whether an agent is alive.
LOCK_STALE = 300


def lock_path(name):
    return os.path.join(oke_home(), "locks", name)


def try_lock(name):
    path = lock_path(name)
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except OSError:
            try:
                if time.time() - os.path.getmtime(path) <= LOCK_STALE:
                    return False
                os.remove(path)
            except OSError:
                pass  # the holder just released it: try again
    return False


def unlock(name):
    try:
        os.remove(lock_path(name))
    except OSError:
        pass


class locked(object):
    """Hold a lock around a read-modify-write of state.json or children.json: the
    mods' ticks, waits, spawn and the orchestrator all write them now."""

    def __init__(self, name="records", timeout=30):
        self.name, self.timeout = name, timeout

    def __enter__(self):
        deadline = time.time() + self.timeout
        while not try_lock(self.name):
            if time.time() > deadline:
                raise StateError("lock %s is held by another OKe process" % self.name)
            time.sleep(0.05)

    def __exit__(self, *exc):
        unlock(self.name)


def events_path():
    return os.path.join(oke_home(), "events.log")


def log_event(text):
    with io.open(events_path(), "a", encoding="utf-8", newline="\n") as f:
        f.write(u"%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), text))


def read_events(offset):
    """New complete lines after byte `offset` and the next offset. No offset: none, from the end."""
    try:
        size = os.path.getsize(events_path())
    except OSError:
        return [], 0
    if offset is None:
        return [], size
    if offset > size:  # the log was replaced
        offset = 0
    with io.open(events_path(), "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1
    return data[:end].decode("utf-8", "replace").splitlines(), offset + end


def repo_key(path):
    """Normalized git common dir: the same value from a checkout and its worktrees."""
    cwd = native_path(path)
    try:
        p = subprocess.Popen(["git", "rev-parse", "--git-common-dir"], cwd=cwd,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = p.communicate()
    except (OSError, ValueError):
        raise ValueError("not a git repository: %s" % path)
    if p.returncode != 0:
        raise ValueError("not a git repository: %s" % path)
    common = native_path(out.decode("utf-8", "replace").strip())
    if not os.path.isabs(common):
        common = os.path.join(cwd, common)
    return normalize_path(os.path.realpath(common))


def slug(text, limit=24):
    s = re.sub(r"[^a-z0-9-]", "-", text.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    if not s or not s[0].isalpha():
        s = "x" + s
    return s[:limit].rstrip("-")


# ---------------------------------------------------------------- herdr

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


def run_herdr(args):
    """Return (exit code, stdout, stderr). Raises HerdrError when herdr cannot run."""
    try:
        p = subprocess.Popen(herdr_argv() + list(args), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
    except (OSError, ValueError) as e:
        raise HerdrError("herdr_unavailable", str(e))
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


# ---------------------------------------------------------------- spawn

AGENT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TRUST_MARKER = "trust this folder"
DIALOG_TRIES = 5
DIALOG_POLL = 1.0
START_TIMEOUT_MS = 90000
FIRST_PROMPT = ("Use the oke:{role} skill. Your assignment file is {path}. "
                "Read it first, then follow it.")
REVIEWER_TOOLS = ["Read", "Grep", "Glob", "Skill", "Bash(git diff:*)", "Bash(git log:*)",
                  "Bash(git show:*)", "Bash(git status:*)"]


def plugin_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def edit_rules(path, windows=None):
    """Permission rules that let a session write under `path` only (see docs/probe-results.md).
    A symlinked path also gets rules for its resolved form: which one Claude Code matches is unverified."""
    if windows is None:
        windows = os.name == "nt"
    rules = []
    resolved = [os.path.realpath(path)] if os.path.exists(path) else []
    for one in [path] + resolved:
        p = native_path(one.replace("\\", "/"), windows).rstrip("/")
        if windows and re.match(r"^[a-zA-Z]:/", p):
            drive, rest = p[0], p[2:]
            new = ["Edit(//%s%s/**)" % (drive.lower(), rest), "Edit(//%s:%s/**)" % (drive.upper(), rest)]
        else:
            new = ["Edit(/%s/**)" % p]
        rules += [r for r in new if r not in rules]
    return rules


def launch_args(role, config, home, plugin_dir, resume=None):
    args = ["--model", config["models"].get(role, "sonnet"),
            "--add-dir", home, "--add-dir", config["wiki_path"],
            "--plugin-dir", plugin_dir]
    if resume:
        args += ["--resume", resume]
    if role == "reviewer":
        # dontAsk denies everything not listed without stopping at a dialog.
        args += ["--permission-mode", "dontAsk", "--allowedTools"] + REVIEWER_TOOLS
        args += edit_rules(home) + edit_rules(config["wiki_path"])
    elif config.get("autonomy") == "conservative":
        # Ask for everything except the agent's own OKe files; without this every
        # report and board write would be a dialog.
        args += ["--permission-mode", "manual", "--allowedTools", "Skill"]
        args += edit_rules(home) + edit_rules(config["wiki_path"])
        if role == "pm":
            # shortcut: the PM's coordination commands are allowed wholesale (python
            # included, for oke.py); narrow to exact commands if a PM ever misuses them.
            args += ["Bash(herdr:*)", "Bash(git:*)", "Bash(python3:*)", "Bash(python:*)"]
    else:
        args += ["--permission-mode", "bypassPermissions"]
    return args


def repo_name(key):
    """Folder and agent-name part for a repository: readable name plus a hash of
    the key, so two repositories called `proj` (or two non-ASCII names) differ."""
    name = os.path.basename(key)
    if name == ".git":
        name = os.path.basename(os.path.dirname(key))
    if name.endswith(".git"):
        name = name[:-4]
    return "%s-%s" % (slug(name, 18), hashlib.sha1(key.encode("utf-8")).hexdigest()[:6])


def pm_dir(key):
    return os.path.join(oke_home(), "repos", repo_name(key), "pm")


def pm_job_dir(job):
    """The orchestrator-PM channel of one job: assignment.md and report.md."""
    return os.path.join(oke_home(), "jobs", job, "pm")


def children_path(key):
    return os.path.join(pm_dir(key), "children.json")


def read_children(key):
    """{agent: record}. Only spawn writes this file, but a PM once rewrote it as a list of records."""
    data = read_json(children_path(key), {})
    if isinstance(data, list):
        data = dict((r["agent"], r) for r in data if isinstance(r, dict) and r.get("agent"))
    return data if isinstance(data, dict) else {}


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
        # The dialog can still be drawing when herdr reports the agent blocked.
        for _ in range(DIALOG_TRIES):
            screen = herdr_text("agent", "read", agent, "--source", "visible", "--lines", "40")
            if TRUST_MARKER in screen.lower():
                break
            time.sleep(DIALOG_POLL)
        if TRUST_MARKER not in screen.lower() or not trusted():
            raise DialogError(screen)
        herdr("agent", "send-keys", agent, "down", "enter")
        herdr("agent", "wait", agent, "--until", "idle", "--timeout", "30000")
    registered()

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
    return info


def cmd_spawn(args):
    path = os.path.abspath(native_path(args.assignment)).replace("\\", "/")
    header = read_header(path)
    if header is None:
        return fail("cannot read assignment header: %s" % path)
    for field in ("agent", "role", "cwd", "seq"):
        if field not in header:
            return fail("assignment is missing %r: %s" % (field, path))
    agent, role, cwd = header["agent"], header["role"], native_path(header["cwd"])
    if not AGENT_NAME.match(agent):
        return fail("bad agent name %r: must match %s" % (agent, AGENT_NAME.pattern))
    if role not in ("pm", "worker", "reviewer"):
        return fail("bad role %r" % role)

    state = load_state()
    known_keys = set(state["repos"]) | set(j.get("repo") for j in state["jobs"].values())
    if role == "pm":
        if "job" in header:
            # a pre-slice-3 PM folder held one job's instruction here
            return fail("%s is an old per-job PM assignment; rewrite it as the standing assignment" % path)
        try:
            key = repo_key(cwd)
        except ValueError as e:
            return fail(str(e))
    else:
        job = state["jobs"].get(header.get("job"))
        if job is None:
            return fail("unknown job %r in assignment" % header.get("job"))
        if job["phase"] not in ("working", "reviewing", "merging"):
            return fail("plan not approved: job %s is in phase %s" % (header["job"], job["phase"]))
        key = job["repo"]

    if any(a.get("name") == agent for a in list_agents()):
        return fail("agent name in use: %s" % agent)

    # A report left by an earlier run would satisfy the first `oke wait` at once.
    stale = os.path.join(os.path.dirname(path), "report.md")
    if os.path.exists(stale):
        os.remove(stale)

    pane = args.pane
    if not pane:
        pane = herdr("pane", "split", "--pane", args.split_from, "--direction", "right",
                     "--cwd", cwd, "--no-focus")["pane"]["pane_id"]

    record = {"agent": agent, "pane": pane, "session": None,
              "seq": header["seq"], "role": role, "job": header.get("job")}

    def save_record():
        with locked():
            if role == "pm":
                current = load_state()
                current["repos"].setdefault(key, {})["pm"] = record
                save_state(current)
            else:
                children = read_children(key)
                children[agent] = record
                write_json_atomic(children_path(key), children)

    def trusted_folder():
        """The trust dialog is answered only for a repository OKe already works on,
        and only when the pane really sits in it."""
        if key not in known_keys:
            return False
        try:
            if repo_key(cwd) != key:
                return False
            if args.pane:
                where = herdr("pane", "get", pane).get("pane", {}).get("cwd") or ""
                return repo_key(where) == key
        except (ValueError, HerdrError):
            return False
        return True

    claude_args = launch_args(role, load_config(), oke_home(), plugin_root(), args.resume)
    try:
        info = start_agent(agent, pane, claude_args, FIRST_PROMPT.format(role=role, path=path),
                           trusted_folder, save_record)
    except DialogError as e:
        return fail("agent %s is stopped at a dialog in pane %s:\n%s" % (agent, pane, e))
    except HerdrError as e:
        if e.code == "herdr_unavailable":
            raise
        return fail("spawn of %s failed in pane %s: %s" % (agent, pane, e))
    record["pane"] = info.get("pane_id", pane)
    record["session"] = session_of(info)
    record["terminal"] = terminal_of(record["pane"])  # lets a revive close this pane safely
    save_record()
    print(json.dumps({"agent": agent, "pane": record["pane"], "session": record["session"]}))
    return 0


# ---------------------------------------------------------------- wait

FINAL_TYPES = ("done", "question", "blocked", "failed")
REPORT_TYPES = FINAL_TYPES + ("waiting",)
IDLE_CONFIRMATIONS = 3
BLOCKED_GRACE = 10
HERDR_RETRIES = 30  # consecutive herdr_unavailable polls (about a minute) before a wait gives up  # seconds


def check_once(agent, report, seq):
    """One look at a child. Returns a result line, None to keep waiting, or a
    '?'-prefixed provisional verdict that only counts once the agent stays idle."""
    header = read_header(report)
    current = header is not None and header.get("seq") == seq
    if current and header.get("type") in FINAL_TYPES:
        return "report %s" % header["type"]
    look = look_at(agent, os.path.dirname(report))
    if look != "idle":
        return look
    if current and header.get("type") == "waiting":
        return None
    return "?" + (report_problem(report, header) or "idle_no_report")


def look_at(agent, folder):
    """herdr's view of an agent: 'gone', 'blocked\\n<screen>', 'idle' when it has
    nothing to do, '?deliver' when idle with mail, or None while it works."""
    try:
        info = herdr("agent", "get", agent).get("agent", {})
    except HerdrError as e:
        if e.code in ("agent_not_found", "pane_not_found"):
            return "gone"
        raise
    status = info.get("agent_status")
    if status == "blocked":
        try:
            return "blocked\n" + herdr_text("agent", "read", agent, "--source", "visible", "--lines", "40")
        except HerdrError as e:
            if e.code in ("agent_not_found", "pane_not_found"):
                return "gone"
            raise
    if status not in ("idle", "done"):
        return None
    if retry_pending(os.path.join(folder, "api-retry.json")):
        return None  # its mod resubmits the turn after an API error
    if inbox_messages(agent):
        return "?deliver"  # idle with mail: deliver it, never call that "no report"
    return "idle"


def report_problem(report, header):
    """Why an existing report cannot be read, or None (also when there is none)."""
    if not os.path.exists(report):
        return None
    if header is None:
        return "bad_report no header (expected key: value lines, then ---)"
    if "type" not in header or "seq" not in header:
        return "bad_report header needs type and seq"
    if header["type"] not in REPORT_TYPES:
        return "bad_report unknown type %r" % header["type"]
    if not isinstance(header["seq"], int):
        return "bad_report seq is not a number"
    return None


SILENT_AFTER = 2  # warnings for one job and seq before the wait stops raising it


def warn_job(result):
    """Count an idle_no_report/bad_report of a job. The second one says "again"; after
    that the shared PM wait skips the job, and `pending` lists it as silent."""
    name = result.split(" ")[1]
    with locked():
        state = load_state()
        job = state["jobs"].get(name)
        if job is None:
            return result
        seq = pm_job_report(name)[0]
        count = job["warned"][1] + 1 if (job.get("warned") or [None])[0] == seq else 1
        job["warned"] = [seq, count]
        save_state(state)
    if count >= SILENT_AFTER:
        word, rest = result.split(" ", 1)
        return "%s again %s" % (word, rest)
    return result


def is_silent(job, seq):
    warned = job.get("warned") or [None, 0]
    return warned[0] == seq and warned[1] >= SILENT_AFTER


def seen_mark(header):
    """A blocked report may follow a question at the same seq: both must come through."""
    return "%s:%s" % (header.get("type"), header.get("seq"))


def check_pm_once(agent, key):
    """One look at a PM and every open job of its repository (read fresh each time,
    so a job added later counts). A final report is returned once: `seen` remembers it."""
    state = load_state()
    if not open_jobs(state, key) and not inbox_messages(agent):
        return "no_jobs"  # nothing left to watch or deliver (a cancel goes in first)
    jobs = []
    for name in sorted(open_jobs(state, key)):
        seq, header, mtime = pm_job_report(name)
        if seq is not None:
            jobs.append((name, seq, header, mtime))
    fresh = [j for j in jobs if j[2].get("seq") == j[1] and j[2].get("type") in FINAL_TYPES
             and state["jobs"][j[0]].get("seen") != seen_mark(j[2])]
    if fresh:
        name, seq, header, _ = min(fresh, key=lambda j: j[3] or 0)
        with locked():
            current = load_state()
            if name not in current["jobs"] or current["jobs"][name].get("seen") == seen_mark(header):
                return None  # another wait returned it a moment ago
            current["jobs"][name]["seen"] = seen_mark(header)
            save_state(current)
        return "report %s %s" % (header["type"], name)
    look = look_at(agent, pm_dir(key))
    if look != "idle":
        return look
    for name, seq, header, _ in jobs:
        if state["jobs"][name].get("sent_seq") != seq:
            continue  # the orchestrator is still handing it over: the message is not sent yet
        if is_silent(state["jobs"][name], seq):
            continue  # the user was told twice; `pending` keeps it in view
        if not (header.get("seq") == seq and header.get("type") in REPORT_TYPES):
            report = os.path.join(pm_job_dir(name), "report.md")
            problem = report_problem(report, read_header(report))
            if problem:
                word, why = problem.split(" ", 1)
                return "?%s %s %s" % (word, name, why)
            return "?idle_no_report %s" % name
    return None


DELIVER_TRIES = 3  # failed deliveries in a row before the wait says deliver_failed
_claims = [0]


def wait_mark(agent):
    return os.path.join(oke_home(), "waits", agent)


def claim_wait(agent):
    """The newest wait on an agent is the only one: two would report one dialog twice
    and deliver one message twice. Older waits see a foreign token and stop."""
    _claims[0] += 1
    token = "%.6f-%d-%d" % (time.time(), os.getpid(), _claims[0])
    write_json_atomic(wait_mark(agent), {"token": token})
    return token


def superseded(agent, token):
    mark = read_json(wait_mark(agent), None)
    return isinstance(mark, dict) and mark.get("token") not in (None, token)


RETRY_MARKER_GRACE = 120  # seconds past the marker's own delay


def retry_pending(marker):
    """A marker its mod failed to clear stops counting once its delay is long past."""
    try:
        age = time.time() - os.path.getmtime(marker)
    except OSError:
        return False
    delay = (read_json(marker, {}) or {}).get("delay") or 300
    return age < delay + RETRY_MARKER_GRACE


def wait_loop(agent, report, seq, poll=2, max_polls=None, after_blocked=False, jobs_key=None):
    """No time limit: a five hour simulation is a normal wait. A herdr state read
    right after input can be stale and a report can be mid-write, so an idle
    verdict needs IDLE_CONFIRMATIONS consecutive polls."""
    idle = 0
    polls = 0
    outage = 0
    # after_blocked: a key was just sent to a dialog the caller already reported. Do not
    # report that dialog again while herdr still shows the old state. Only a short grace:
    # the agent may already be at its next dialog when this wait starts.
    armed = not after_blocked
    started = time.time()
    failed_deliveries = 0
    token = claim_wait(agent)
    while max_polls is None or polls < max_polls:
        polls += 1
        if superseded(agent, token):
            return "superseded"
        try:
            if jobs_key is None:
                result = check_once(agent, report, seq)
            else:
                result = check_pm_once(agent, jobs_key)
            outage = 0
        except HerdrError as e:
            outage += 1
            if e.code != "herdr_unavailable" or outage > HERDR_RETRIES:
                raise
            result = None
        except (OSError, StateError):
            # Windows holds a file open for a moment, or state.json is locked a while:
            # the next poll reads it again. Only a lasting failure ends the wait.
            outage += 1
            if outage > HERDR_RETRIES:
                raise
            result = None
        if result is not None and result.startswith("blocked"):
            if not armed and time.time() - started < BLOCKED_GRACE:
                result = None
        else:
            armed = True
        if result == "gone":
            try:
                if revive(agent) in ("revived", "alive", "busy", "failed"):
                    result = None  # failed counts toward the revive limit and is tried again
            except (OSError, StateError):
                result = None  # state.json busy for a moment: look again next poll
        if result is None:
            idle = 0
        elif result.startswith("?"):
            idle += 1
            if idle >= IDLE_CONFIRMATIONS:
                if result != "?deliver":
                    if superseded(agent, token):
                        return "superseded"
                    if jobs_key is not None:
                        return warn_job(result[1:])
                    return result[1:]
                try:
                    if deliver(agent) is False:  # the mail stays; the next round tries again
                        failed_deliveries += 1
                    else:
                        failed_deliveries = 0
                except (HerdrError, OSError):
                    pass  # a passing outage: the next check_once counts it
                if failed_deliveries >= DELIVER_TRIES:
                    return "deliver_failed"  # never loop silently on mail that cannot go in
                idle = 0
        else:
            # A newer wait may have started while this one looked. A --jobs report is
            # already marked seen, so the newer wait would never return it: keep it.
            if jobs_key is not None and result.startswith("report "):
                return result
            return "superseded" if superseded(agent, token) else result
        if poll:
            time.sleep(poll)
    return None


def cmd_wait(args):
    if args.jobs:
        state = load_state()
        keys = [k for k, r in state["repos"].items() if (r.get("pm") or {}).get("agent") == args.agent]
        if not keys:
            return fail("--jobs needs a PM agent: %s is not one" % args.agent)
        print(wait_loop(args.agent, None, None, poll=args.poll,
                        after_blocked=args.after_blocked, jobs_key=keys[0]))
        return 0
    if args.report is None or args.seq is None:
        return fail("wait needs --report and --seq, or --jobs for a PM")
    print(wait_loop(args.agent, native_path(args.report), args.seq, poll=args.poll,
                    after_blocked=args.after_blocked))
    return 0


# ---------------------------------------------------------------- revive

DONE_PHASES = ("done", "failed")
REVIVE_LIMIT = 3  # consecutive deaths with no new report in between
ORCH_NAME = "oke-orch"
REVIVE_PROMPT = ("You were restarted after a crash. Use the oke:{role} skill. Re-read your "
                 "assignment file {path} and your board.md, then continue.")


def pm_jobs_note(state, key):
    """The job assignments a restarted or compacted PM has to re-read."""
    paths = [os.path.join(pm_job_dir(j), "assignment.md").replace("\\", "/")
             for j in sorted(open_jobs(state, key))]
    paths = [p for p in paths if os.path.exists(p)]
    return (" Then re-read the assignment of each open job: %s." % ", ".join(paths)) if paths else ""


def open_jobs(state, key=None):
    return [n for n, j in state["jobs"].items()
            if j.get("phase") not in DONE_PHASES and (key is None or j.get("repo") == key)]


def all_keys(state):
    return sorted(set(state["repos"]) | set(j.get("repo") for j in state["jobs"].values() if j.get("repo")))


def records(state):
    """Every recorded OKe agent as (role, name, key, record)."""
    out = [("orchestrator", "orchestrator", None, state["orchestrator"])]
    for key in all_keys(state):
        pm = (state["repos"].get(key) or {}).get("pm")
        if pm and pm.get("agent"):
            out.append(("pm", pm["agent"], key, pm))
        for name, rec in sorted(read_children(key).items()):
            out.append((rec.get("role") or "worker", name, key, rec))
    return out


def find_target(state, name):
    for role, n, key, rec in records(state):
        if n == name or (role == "orchestrator" and name == rec.get("agent")):
            return role, n, key, rec
    return None


def agent_folder(role, name, key, record):
    if role == "pm":
        return pm_dir(key)
    if role in ("worker", "reviewer") and record.get("job"):
        return os.path.join(oke_home(), "jobs", record["job"], name)
    return None


def should_be_alive(state, role, key, name, record):
    """Only agents with work left are revived: never a worker closed after merge."""
    if role == "orchestrator":
        return record.get("argv") is not None and bool(open_jobs(state))
    if role == "pm":
        return bool(open_jobs(state, key))
    job = state["jobs"].get(record.get("job"))
    if not job or job.get("phase") in DONE_PHASES:
        return False
    folder = agent_folder(role, name, key, record)
    assignment = read_header(os.path.join(folder, "assignment.md")) or {}
    report = read_header(os.path.join(folder, "report.md")) or {}
    return not (report.get("type") in FINAL_TYPES and "seq" in assignment
                and report.get("seq") == assignment["seq"])


def same_agent(record, a):
    """Pane ids come back after a herdr restart and herdr may not know a fresh
    session yet, so only the OKe name or an equal known session identify an agent."""
    if record.get("agent") and a.get("name") == record["agent"]:
        return True
    mine = record.get("session")
    return bool(mine and session_of(a) == mine)


def live_agent(record, agents):
    for a in agents:
        if same_agent(record, a):
            return a
    return None


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


def strip_resume(argv):
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            if not a.startswith("-"):
                continue
        if a in ("--resume", "-r"):
            skip = True
        elif a not in ("--continue", "-c") and not a.startswith("--resume="):
            out.append(a)
    return out


def update_record(role, key, name, fields):
    with locked():
        if role == "orchestrator":
            state = load_state()
            state["orchestrator"].update(fields)
            save_state(state)
        elif role == "pm":
            state = load_state()
            state["repos"].setdefault(key, {}).setdefault("pm", {}).update(fields)
            save_state(state)
        else:
            children = read_children(key)
            children.setdefault(name, {}).update(fields)
            write_json_atomic(children_path(key), children)


def report_mtime(folder):
    try:
        return os.path.getmtime(os.path.join(folder, "report.md"))
    except OSError:
        return None


def new_pane(state, role, key, cwd):
    """A pane next to the agent's parent; a new workspace when no parent pane is left."""
    if role == "orchestrator":
        parents = [(r.get("pm") or {}).get("pane") for r in state["repos"].values()]
    elif role == "pm":
        parents = [state["orchestrator"].get("pane")]
    else:
        parents = [((state["repos"].get(key) or {}).get("pm") or {}).get("pane")]
    for parent in [p for p in parents if p]:
        try:
            return herdr("pane", "split", "--pane", parent, "--direction", "right",
                         "--cwd", cwd, "--no-focus")["pane"]["pane_id"]
        except HerdrError as e:
            if e.code == "herdr_unavailable":
                raise
    return herdr("workspace", "create", "--cwd", cwd, "--label", "oke", "--no-focus")["root_pane"]["pane_id"]


def relaunch(state, role, key, name, record, folder):
    if role == "orchestrator":
        agent, prompt, cwd = ORCH_NAME, "/oke:start", native_path(record.get("cwd") or oke_home())
        args = strip_resume(record["argv"])
    else:
        path = os.path.join(folder, "assignment.md").replace("\\", "/")
        cwd = native_path((read_header(path) or {}).get("cwd") or oke_home())
        agent, prompt = name, REVIVE_PROMPT.format(role=role, path=path)
        if role == "pm":
            prompt += pm_jobs_note(state, key)
        args = launch_args(role, load_config(), oke_home(), plugin_root())
    if record.get("session"):
        args = args + ["--resume", record["session"]]
    # Never start in the old pane: a force-killed claude leaves terminal modes set, and
    # the start command herdr types into that shell arrives garbled (seen live).
    # Close it only while it is still that agent's terminal: after a herdr restart the
    # same pane id can be a user's shell. The user's own orchestrator pane stays.
    old = record.get("pane")
    if old and (role != "orchestrator" or record.get("agent") == ORCH_NAME):
        if record.get("terminal") and terminal_of(old) == record["terminal"]:
            try:
                herdr("pane", "close", old)
            except HerdrError as e:
                if e.code == "herdr_unavailable":
                    raise  # a worktree group root refuses to close: leave it
    pane = new_pane(state, role, key, cwd)
    # The folder was trusted when the agent first started; a trust dialog now is unexpected.
    try:
        return start_agent(agent, pane, args, prompt, lambda: False)
    except (HerdrError, DialogError):
        # herdr keeps the name of a start that failed; with the pane closed it is
        # gone again, so the next watcher round does not read it as alive.
        try:
            herdr("pane", "close", pane)
        except HerdrError:
            pass
        raise


def revive(name):
    """Bring a dead OKe agent back in its own session. Returns revived, alive, busy
    (another process is reviving it), not_needed, gave_up, failed or unknown."""
    state = load_state()
    found = find_target(state, name)
    if found is None:
        return "unknown"
    lock = "revive-" + found[1]
    if not try_lock(lock):
        return "busy"
    try:
        return revive_locked(found[1])
    finally:
        unlock(lock)


def revive_locked(name):
    state = load_state()  # again, under the lock: another watcher may have revived it
    found = find_target(state, name)
    if found is None:
        return "unknown"
    role, name, key, record = found
    if not should_be_alive(state, role, key, name, record):
        return "not_needed"
    if live_agent(record, list_agents()):
        return "alive"
    folder = agent_folder(role, name, key, record)
    if role == "pm":  # a PM reports per job, in each job's pm folder
        mtime = max([m for m in (report_mtime(pm_job_dir(j)) for j in open_jobs(state, key))
                     if m is not None] or [None])
    else:
        mtime = report_mtime(folder) if folder else None
    revives = record.get("revives", 0)
    if folder and mtime != record.get("report_mtime"):
        revives = 0  # it reported since the last revive: not a crash loop
    if revives >= REVIVE_LIMIT:
        if not record.get("gave_up"):
            log_event("gave_up %s after %d restarts" % (name, revives))
            update_record(role, key, name, {"gave_up": True})
        return "gave_up"
    if folder:
        try:
            os.remove(os.path.join(folder, "api-retry.json"))  # the dead process's retry
        except OSError:
            pass
    # Counted before the start: a watcher killed during a slow start must not leave
    # the count unchanged and retry forever.
    update_record(role, key, name, {"revives": revives + 1, "report_mtime": mtime, "gave_up": False})
    fields = {}
    try:
        info = relaunch(state, role, key, name, record, folder)
    except (HerdrError, DialogError) as e:
        if isinstance(e, HerdrError) and e.code == "herdr_unavailable":
            raise
        log_event("revive_failed %s: %s" % (name, (str(e).strip().splitlines() or [""])[0]))
        return "failed"
    fields.update(pane=info.get("pane_id"), session=session_of(info) or record.get("session"))
    fields["terminal"] = terminal_of(fields["pane"]) if fields["pane"] else None
    if role == "orchestrator":
        fields["agent"] = ORCH_NAME
    update_record(role, key, name, fields)
    log_event("revived %s pane %s" % (name, fields["pane"]))
    return "revived"


def cmd_revive(args):
    if args.reset:  # the user chose to resume after OKe gave up
        found = find_target(load_state(), args.agent)
        if found:
            update_record(found[0], found[2], found[1], {"revives": 0, "gave_up": False})
    print(revive(args.agent))
    return 0


def cmd_resume_all(args):
    """After a reboot: the orchestrator brings back each PM with work left; the PMs'
    own watchers bring back their children."""
    state = load_state()
    result = dict((name, revive(name)) for role, name, key, rec in records(state) if role == "pm")
    print(json.dumps(result, ensure_ascii=False))
    return 0


def whoami(state, pane):
    """(role, name, key, record) of the OKe agent in `pane`, or None. Pane ids come
    back after a herdr restart, so a recorded session has to match as well."""
    mine = [r for r in records(state) if r[3].get("pane") == pane]
    if not mine:
        return None, []
    agents = list_agents()
    here = [a for a in agents if a.get("pane_id") == pane]
    if not here:
        return None, agents
    for r in mine:
        if same_agent(r[3], here[0]):
            return r, agents
    return None, agents


def board_node(role, name, key, record, agents):
    folder = agent_folder(role, name, key, record)
    a = live_agent(record, agents)
    # a PM reports per job; an old pm/report.md from before is not its state
    report = read_header(os.path.join(folder, "report.md")) if folder and role != "pm" else None
    try:
        with io.open(os.path.join(folder, "board.md"), encoding="utf-8") as f:
            board = f.read()
    except (IOError, OSError, TypeError, UnicodeDecodeError):
        board = ""
    return {"agent": name, "role": role, "status": a.get("agent_status") if a else "dead",
            "pane": (a or {}).get("pane_id") or record.get("pane"),
            "report": (report or {}).get("type"), "board": board,
            "revives": record.get("revives", 0)}


def board_tree(state, agents, only_key=None):
    tree = []
    for key in all_keys(state):
        if only_key and key != only_key:
            continue
        pm = (state["repos"].get(key) or {}).get("pm")
        children = read_children(key)
        jobs = []
        for job in sorted(n for n, j in state["jobs"].items() if j.get("repo") == key):
            nodes = [board_node(rec.get("role") or "worker", name, key, rec, agents)
                     for name, rec in sorted(children.items()) if rec.get("job") == job]
            jobs.append({"job": job, "phase": state["jobs"][job].get("phase"), "agents": nodes})
        tree.append({"repo": repo_name(key), "key": key, "jobs": jobs,
                     "pm": board_node("pm", pm["agent"], key, pm, agents) if pm and pm.get("agent") else None})
    return tree


def cmd_tick(args):
    """One watcher round for a mod: who am I, revive what died, new events, the board."""
    state = load_state()
    events, offset = read_events(args.events_from)
    out = {"role": None, "agent": None, "revived": {}, "events": events,
           "events_offset": offset, "board": None}
    me, agents = whoami(state, args.pane)
    if me is not None:
        role, name, key, record = me
        out["role"], out["agent"] = role, name
        if role == "orchestrator":
            targets = [r for r in records(state) if r[0] == "pm"]
        elif role == "pm":
            targets = [r for r in records(state) if r[0] == "orchestrator"
                       or (r[2] == key and r[0] in ("worker", "reviewer"))]
        else:
            targets = []
        for t_role, t_name, t_key, t_rec in targets:
            a = live_agent(t_rec, agents)
            if a:
                if not t_rec.get("session") and session_of(a):  # spawn could not see it yet
                    update_record(t_role, t_key, t_name, {"session": session_of(a)})
            elif not out["revived"] and should_be_alive(state, t_role, t_key, t_name, t_rec):
                # one per tick: a revive can take two minutes and the mod's call has a timeout
                out["revived"][t_name] = revive(t_name)
        if args.board and role in ("orchestrator", "pm"):
            out["board"] = board_tree(state, agents, key if role == "pm" else None)
    print(json.dumps(out, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- api retry

RETRYABLE_ERRORS = ("rate_limit", "overloaded", "server_error", "unknown", "")
API_RETRY_LIMIT = 20


def cmd_api_retry(args):
    """The mod reports an API-error turn (or a good turn with --clear); OKe decides
    whether to retry and after how long, and gives up with a failed report."""
    state = load_state()
    me, _ = whoami(state, args.pane)
    if me is None:
        print(json.dumps({"action": "none", "role": None}))
        return 0
    role, name, key, record = me
    folder = agent_folder(role, name, key, record)
    marker = os.path.join(folder, "api-retry.json") if folder else None
    limit = int(load_config().get("api_retry_limit", API_RETRY_LIMIT))
    out = {"role": role, "agent": name, "limit": limit}
    kind = args.error or ""
    if args.clear:
        out["action"] = "cleared"
    elif kind in RETRYABLE_ERRORS and args.attempt <= limit:
        out["action"], out["delay"] = "retry", 30 * 2 ** min(max(args.attempt, 1) - 1, 4)
        out["delay"] = min(out["delay"], 300)
        if marker:
            write_json_atomic(marker, {"attempt": args.attempt, "error": kind, "delay": out["delay"]})
        print(json.dumps(out))
        return 0
    else:
        out["action"] = "give_up"
        log_event("api_gave_up %s: %s after %d attempts" % (name, kind or "error", args.attempt))
        if role == "pm":
            # A PM reports per job. A final report of the current seq still waits on
            # the user (a plan to approve): leave it, fail only the jobs owing one.
            folders = []
            for job in sorted(open_jobs(state, key)):
                seq, header, _ = pm_job_report(job)
                if seq is not None and not (header.get("seq") == seq and header.get("type") in FINAL_TYPES):
                    folders.append(pm_job_dir(job))
        else:
            folders = [folder] if folder else []
        for f_dir in folders:
            seq = (read_header(os.path.join(f_dir, "assignment.md")) or {}).get("seq")
            with io.open(os.path.join(f_dir, "report.md"), "w", encoding="utf-8", newline="\n") as f:
                f.write(u"type: failed\nseq: %s\n---\n## 요약\nAPI error %s: automatic retry stopped "
                        u"after %d attempts.\n" % (seq, kind or "unknown", args.attempt))
    if marker and os.path.exists(marker):
        os.remove(marker)
    print(json.dumps(out))
    return 0


# ---------------------------------------------------------------- status

def find_agent(record, agents, key):
    """Find a recorded agent among live ones: by name, then pane, then session.
    A match whose cwd is not in the repository `key` is someone else's work."""
    tests = (
        lambda a: record.get("agent") and a.get("name") == record.get("agent"),
        lambda a: record.get("pane") and a.get("pane_id") == record.get("pane"),
        lambda a: record.get("session") and session_of(a) == record.get("session"),
    )
    for test in tests:
        for a in agents:
            if test(a):
                try:
                    if repo_key(a.get("cwd") or "") == key:
                        return a
                except ValueError:
                    pass
                return None
    return None


def cmd_status(args):
    with locked():
        return status_locked(args)


def own_launch(pane, agents):
    """How the orchestrator in `pane` was started, so a watcher can start it again."""
    found = {}
    try:
        info = herdr("pane", "process-info", "--pane", pane).get("process_info", {})
    except HerdrError:
        info = {}  # without it the orchestrator just cannot be revived; the claim still stands
    for p in info.get("foreground_processes") or []:
        if "claude" in (p.get("name") or "").lower() and p.get("argv"):
            argv = p["argv"][1:]
            # shortcut: a node-run claude has its script as the next argument; other launchers unhandled
            if os.path.basename(p["argv"][0]).lower().startswith("node") and argv:
                argv = argv[1:]
            found["argv"], found["cwd"] = strip_resume(argv), p.get("cwd")
            break
    for a in agents:
        if a.get("pane_id") == pane and session_of(a):
            found["session"] = session_of(a)
    return found


def status_locked(args):
    state = load_state(recover=True)
    home = oke_home()
    result = {"herdr": {"inside": os.environ.get("HERDR_ENV") == "1", "missing": []},
              "state_recovered": bool(state.get("_recovered")),
              "orchestrator_conflict": None, "repos": {}, "jobs": {}, "orphan_job_dirs": []}
    jobs_dir = os.path.join(home, "jobs")
    if os.path.isdir(jobs_dir):
        result["orphan_job_dirs"] = sorted(d for d in os.listdir(jobs_dir) if d not in state["jobs"])
    if not result["herdr"]["inside"]:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    result["herdr"]["missing"] = missing_commands()
    agents = list_agents()
    names = set(a.get("name") for a in agents if a.get("name"))
    changed = False

    for key, repo in state["repos"].items():
        record = repo.get("pm") or {}
        found = find_agent(record, agents, key)
        if found:
            name, pane = found.get("name") or record.get("agent"), found.get("pane_id")
            if (name, pane) != (record.get("agent"), record.get("pane")):
                record["agent"], record["pane"] = name, pane
                changed = True
            # the session id does not exist yet when spawn records a fresh agent
            if session_of(found) and session_of(found) != record.get("session"):
                record["session"] = session_of(found)
                changed = True
        result["repos"][key] = {"pm": "alive" if found else "dead",
                                "agent": record.get("agent"), "pane": record.get("pane"),
                                "session": record.get("session"),
                                "pm_dir": pm_dir(key).replace("\\", "/")}

    for name, job in state["jobs"].items():
        key = job.get("repo")
        children = read_children(key) if key else {}
        job_agents = dict((a, "alive" if a in names else "dead")
                          for a, rec in children.items() if rec.get("job") == name)
        if job.get("phase") in ("awaiting_approval", "awaiting_merge"):
            cls = "awaiting_user"
        elif result["repos"].get(key, {}).get("pm") == "alive":
            cls = "alive"
        else:
            cls = "disconnected"
        result["jobs"][name] = {"phase": job.get("phase"), "class": cls, "agents": job_agents,
                                "pm_seq": pm_job_report(name)[0],
                                "pm_job_dir": pm_job_dir(name).replace("\\", "/")}

    here = os.environ.get("HERDR_PANE_ID")
    recorded = state["orchestrator"].get("pane")
    if recorded and recorded != here and any(a.get("pane_id") == recorded for a in agents):
        result["orchestrator_conflict"] = recorded

    code = 0
    if args.claim:
        if result["orchestrator_conflict"]:
            code = fail("another orchestrator is alive in pane %s" % recorded)
        elif here:
            state["orchestrator"]["pane"] = here
            state["orchestrator"].update(own_launch(here, agents))
            if not any(a.get("pane_id") == here and a.get("name") == ORCH_NAME for a in agents):
                state["orchestrator"].update(revives=0, gave_up=False, agent=None)
            changed = True
    if changed or state.get("_recovered"):
        save_state(state)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


# ---------------------------------------------------------------- compact hook

def compact_message():
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return ""
    home = oke_home()
    # Read-only on purpose: a hook must not rename or rewrite state files.
    state = read_json(state_path(), None)
    if not isinstance(state, dict):
        return ""
    if (state.get("orchestrator") or {}).get("pane") == pane:
        return ("OKe: context was compacted. Re-read %s and run oke status before continuing."
                % state_path().replace("\\", "/"))
    folder = None
    note = ""
    repos = state.get("repos") or {}
    keys = set(repos) | set(j.get("repo") for j in (state.get("jobs") or {}).values() if j.get("repo"))
    for key in sorted(keys):
        if ((repos.get(key) or {}).get("pm") or {}).get("pane") == pane:
            folder = pm_dir(key)
            note = pm_jobs_note({"jobs": state.get("jobs") or {}}, key)
            break
        for name, rec in read_children(key).items():
            if rec.get("pane") == pane and rec.get("job"):
                folder = os.path.join(home, "jobs", rec["job"], name)
                break
        if folder:
            break
    if not folder:
        return ""
    folder = folder.replace("\\", "/")
    return ("OKe: context was compacted. Re-read %s/assignment.md and %s/board.md before continuing.%s"
            % (folder, folder, note))


def cmd_compact_hook(args):
    try:
        message = compact_message()
    except Exception:  # a hook runs in every session; it must never break one
        message = ""
    if message:
        print(message)
    return 0


# ---------------------------------------------------------------- job

def cmd_job(args):
    with locked():
        return job_locked(args)


def job_locked(args):
    state = load_state()
    home = oke_home()
    if args.action == "archive" and args.slug not in state["jobs"]:
        # a directory left behind with no state entry (after a recovered state.json)
        if args.slug != slug(args.slug) or not os.path.isdir(os.path.join(home, "jobs", args.slug)):
            return fail("unknown job: %s" % args.slug)
        state["jobs"][args.slug] = {}
    if args.action == "new":
        if args.slug != slug(args.slug):
            return fail("bad job name %r: use %r" % (args.slug, slug(args.slug)))
        job_dir = os.path.join(home, "jobs", args.slug)
        if args.slug in state["jobs"] or os.path.exists(job_dir):
            return fail("job already exists: %s" % args.slug)
        try:
            key = repo_key(args.repo)
        except ValueError as e:
            return fail(str(e))
        branch = "refs/heads/oke/" + args.slug
        if subprocess.call(["git", "-C", native_path(args.repo), "show-ref", "--verify", "--quiet", branch]) == 0:
            return fail("branch oke/%s already exists (an earlier job): pick another job name" % args.slug)
        os.makedirs(job_dir)
        state["jobs"][args.slug] = {"repo": key, "phase": "intake", "worktree": None,
                                    "phase_at": time.time()}
        save_state(state)
        print(json.dumps({"job_dir": job_dir.replace("\\", "/"), "repo": key,
                          "pm_job_dir": pm_job_dir(args.slug).replace("\\", "/"),
                          "pm_dir": pm_dir(key).replace("\\", "/"),
                          "pm_agent": "pm-" + repo_name(key),
                          "wiki": os.path.join(load_config()["wiki_path"], repo_name(key)).replace("\\", "/")},
                         ensure_ascii=False))
        return 0
    if args.slug not in state["jobs"]:
        return fail("unknown job: %s" % args.slug)
    if args.action == "phase":
        if args.phase not in PHASES:
            return fail("bad phase %r: one of %s" % (args.phase, ", ".join(PHASES)))
        state["jobs"][args.slug].update(phase=args.phase, phase_at=time.time())
        save_state(state)
        return 0
    if args.action == "defer":
        item = pending_item(args.slug, state["jobs"][args.slug])
        if item is None:
            return fail("job %s has nothing waiting for the user" % args.slug)
        state["jobs"][args.slug]["deferred"] = item["key"]
        save_state(state)
        return 0
    if args.action == "archive":
        src = os.path.join(home, "jobs", args.slug)
        dst = os.path.join(home, "archive", "%s-%s" % (args.slug, time.strftime("%Y%m%d-%H%M%S")))
        if os.path.isdir(src):
            if not os.path.isdir(os.path.dirname(dst)):
                os.makedirs(os.path.dirname(dst))
            shutil.move(src, dst)
        del state["jobs"][args.slug]
        save_state(state)
        print(dst)
        return 0
    return fail("unknown action")


# ---------------------------------------------------------------- inbox

_sent = [0]


def inbox_dir(agent):
    return os.path.join(oke_home(), "inbox", agent)


def inbox_messages(agent):
    """[(path, text)] oldest first. Names start with a fixed-width time, so they sort."""
    d = inbox_dir(agent)
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".txt"))
    except OSError:
        return []
    out = []
    for n in names:
        try:
            with io.open(os.path.join(d, n), encoding="utf-8") as f:
                out.append((os.path.join(d, n), f.read().strip()))
        except (IOError, OSError, UnicodeDecodeError):
            pass  # moved away by a delivery in another process
    return out


def cmd_send(args):
    text = " ".join(" ".join(args.text).split())
    if not text:
        return fail("empty message")
    if text.startswith("/"):
        return fail("a message must not start with '/': Git Bash rewrites it into a Windows path")
    state = load_state()
    found = find_target(state, args.agent)
    if found is None:
        return fail("unknown agent: %s" % args.agent)
    if found[0] == "orchestrator":
        return fail("the orchestrator has no inbox: a PM reports to it instead")
    sender = "user"
    pane = os.environ.get("HERDR_PANE_ID")
    if pane:
        try:
            me, _ = whoami(state, pane)
        except HerdrError:
            me = None
        if me:
            sender = me[1]
    if args.job:
        # From now on the PM owes this job a report for this seq (see check_pm_once).
        # Recorded before the message: a crash in between leaves an idle_no_report
        # nudge, never a job nobody waits for.
        with locked():
            current = load_state()
            if args.job not in current["jobs"]:
                return fail("unknown job: %s" % args.job)
            seq = pm_job_report(args.job)[0]
            if current["jobs"][args.job].get("sent_seq") != seq:
                current["jobs"][args.job].pop("warned", None)  # a nudge keeps the count
            current["jobs"][args.job]["sent_seq"] = seq
            save_state(current)
    d = inbox_dir(found[1])
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    _sent[0] += 1
    path = os.path.join(d, "%.6f-%d-%d.txt" % (time.time(), os.getpid(), _sent[0]))
    with io.open(path + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        f.write(u"%s: %s\n" % (sender, text))
    os.replace(path + ".tmp", path)
    print(json.dumps({"agent": found[1], "queued": True}))
    return 0


def deliver(agent):
    """Type the queued messages into `agent`, which the caller saw idle. They leave the
    inbox only after herdr took them, so a failed prompt loses nothing."""
    if not inbox_messages(agent):
        return None
    lock = "deliver-" + agent
    if not try_lock(lock):
        return None  # another wait delivers right now
    try:
        msgs = inbox_messages(agent)
        if not msgs:
            return None
        text = "OKe inbox: " + " | ".join(t for _, t in msgs)
        try:
            herdr("agent", "prompt", agent, text, "--wait", "--until", "working",
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
                status = herdr("agent", "get", agent).get("agent", {}).get("agent_status")
            except HerdrError:
                status = None
            if status not in ("working", "blocked"):
                return False
        done = os.path.join(inbox_dir(agent), "read")
        if not os.path.isdir(done):
            os.makedirs(done, exist_ok=True)
        for path, _ in msgs:
            replace_file(path, os.path.join(done, os.path.basename(path)))
        return True
    finally:
        unlock(lock)


# ---------------------------------------------------------------- pending

PENDING_RANKS = {"unsent": 0, "report_done": 0, "silent": 1, "question": 1, "blocked": 1, "failed": 1, "plan": 2, "merge": 3}
ASKING_PHASES = ("planning", "working", "reviewing", "merging")


def pm_job_report(job):
    """(assignment seq, report header, report mtime) of a job's PM channel."""
    folder = pm_job_dir(job)
    seq = (read_header(os.path.join(folder, "assignment.md")) or {}).get("seq")
    report = os.path.join(folder, "report.md")
    try:
        mtime = os.path.getmtime(report)
    except OSError:
        mtime = None
    return seq, read_header(report) or {}, mtime


def pending_item(name, job):
    """What job `name` waits on the user (or the orchestrator) for, or None. The key
    names this very item, so a deferral ends by itself when seq or phase moves on."""
    phase = job.get("phase")
    if phase in DONE_PHASES:
        return None
    seq, report, mtime = pm_job_report(name)
    rtype = report.get("type") if seq is not None and report.get("seq") == seq else None
    if seq is not None and job.get("sent_seq") != seq:
        kind, since, mark = "unsent", job.get("phase_at"), seq  # written, never sent
    elif rtype in ("question", "blocked", "failed"):
        kind, since, mark = rtype, mtime, seq
    elif rtype not in REPORT_TYPES and is_silent(job, seq):
        kind, since, mark = "silent", mtime, seq
    elif rtype == "done" and phase in ASKING_PHASES:
        kind, since, mark = "report_done", mtime, seq
    elif phase == "awaiting_approval":
        kind, since, mark = "plan", job.get("phase_at"), job.get("phase_at")
    elif phase == "awaiting_merge":
        kind, since, mark = "merge", job.get("phase_at"), job.get("phase_at")
    else:
        return None
    key = "%s:%r" % (kind, mark)
    return {"job": name, "kind": kind, "rank": PENDING_RANKS[kind], "since": since or 0,
            "key": key, "deferred": job.get("deferred") == key}


def cmd_pending(args):
    """The user queue, computed from state and files, so compaction loses nothing."""
    state = load_state()
    items = [i for i in (pending_item(n, j) for n, j in state["jobs"].items()) if i]
    items.sort(key=lambda i: (i["rank"], i["since"], i["job"]))
    for i in items:
        i["pm_job_dir"] = pm_job_dir(i["job"]).replace("\\", "/")
    print(json.dumps({"items": items}, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- main

def build_parser():
    parser = argparse.ArgumentParser(prog="oke")
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    job = sub.add_parser("job")
    job_sub = job.add_subparsers(dest="action")
    job_sub.required = True
    new = job_sub.add_parser("new")
    new.add_argument("slug")
    new.add_argument("--repo", required=True)
    phase = job_sub.add_parser("phase")
    phase.add_argument("slug")
    phase.add_argument("phase")
    archive = job_sub.add_parser("archive")
    archive.add_argument("slug")
    defer = job_sub.add_parser("defer")
    defer.add_argument("slug")
    job.set_defaults(func=cmd_job)

    spawn = sub.add_parser("spawn")
    spawn.add_argument("assignment")
    where = spawn.add_mutually_exclusive_group(required=True)
    where.add_argument("--pane")
    where.add_argument("--split-from")
    spawn.add_argument("--resume")
    spawn.set_defaults(func=cmd_spawn)

    wait = sub.add_parser("wait")
    wait.add_argument("agent")
    wait.add_argument("--report")
    wait.add_argument("--seq", type=int)
    wait.add_argument("--jobs", action="store_true")
    wait.add_argument("--poll", type=float, default=2)
    wait.add_argument("--after-blocked", action="store_true")
    wait.set_defaults(func=cmd_wait)

    status = sub.add_parser("status")
    status.add_argument("--claim", action="store_true")
    status.set_defaults(func=cmd_status)

    revive_p = sub.add_parser("revive")
    revive_p.add_argument("agent")
    revive_p.add_argument("--reset", action="store_true")
    revive_p.set_defaults(func=cmd_revive)

    resume_all = sub.add_parser("resume-all")
    resume_all.set_defaults(func=cmd_resume_all)

    tick = sub.add_parser("tick")
    tick.add_argument("--pane", required=True)
    tick.add_argument("--board", action="store_true")
    tick.add_argument("--events-from", type=int)
    tick.set_defaults(func=cmd_tick)

    retry = sub.add_parser("api-retry")
    retry.add_argument("--pane", required=True)
    retry.add_argument("--attempt", type=int, default=1)
    retry.add_argument("--error", default="")
    retry.add_argument("--clear", action="store_true")
    retry.set_defaults(func=cmd_api_retry)

    pending = sub.add_parser("pending")
    pending.set_defaults(func=cmd_pending)

    send = sub.add_parser("send")
    send.add_argument("agent")
    send.add_argument("text", nargs="+")
    send.add_argument("--job")
    send.set_defaults(func=cmd_send)

    hook = sub.add_parser("compact-hook")
    hook.set_defaults(func=cmd_compact_hook)
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return 1 if e.code else 0
    try:
        return args.func(args)
    except StateError as e:
        return fail(str(e))
    except HerdrError as e:
        if e.code != "herdr_unavailable":
            return fail("herdr error: %s" % e)
        # exit 2: herdr itself is unreachable. Callers must not read this as "agent gone".
        sys.stderr.write("herdr unavailable: %s\n" % e.message)
        return 2


if __name__ == "__main__":
    # Screen text contains characters a legacy locale (cp949, C) cannot encode.
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(code)
