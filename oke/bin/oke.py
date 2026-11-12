#!/usr/bin/env python3
"""OKe helper: fixed procedures for the orchestrator / PM skills.

Python 3.6 compatible, standard library only.
"""
import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

PHASES = ("intake", "planning", "awaiting_approval", "working", "reviewing",
          "awaiting_merge", "done", "failed")
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
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))
        f.write(u"\n")
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return default


def state_path():
    return os.path.join(oke_home(), "state.json")


def load_state():
    path = state_path()
    state = json.loads(json.dumps(EMPTY_STATE))
    if not os.path.exists(path):
        return state
    try:
        with io.open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("state.json is not an object")
    except (IOError, OSError, ValueError):
        os.replace(path, path + ".bad")
        state["_recovered"] = True
        return state
    for key in EMPTY_STATE:
        if isinstance(loaded.get(key), dict):
            state[key] = loaded[key]
    return state


def save_state(state):
    write_json_atomic(state_path(), dict((k, v) for k, v in state.items() if k != "_recovered"))


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
    return normalize_path(os.path.abspath(common))


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


def herdr(*args):
    code, out, err = run_herdr(args)
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        lines = (err.strip() or out.strip() or "no output (exit %d)" % code).splitlines()
        raise HerdrError("herdr_unavailable", lines[0])
    if "error" in data:
        e = data["error"] if isinstance(data["error"], dict) else {}
        raise HerdrError(e.get("code", "unknown"), e.get("message", ""))
    return data.get("result", {})


def herdr_text(*args):
    code, out, err = run_herdr(args)
    if code != 0:
        try:
            e = json.loads(out).get("error", {})
            raise HerdrError(e.get("code", "unknown"), e.get("message", ""))
        except (ValueError, AttributeError):
            raise HerdrError("herdr_unavailable", (err.strip() or out.strip() or "exit %d" % code).splitlines()[0])
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
FIRST_PROMPT = ("Use the oke:{role} skill. Your assignment file is {path}. "
                "Read it first, then follow it.")
REVIEWER_TOOLS = ["Read", "Grep", "Glob", "Skill", "Bash(git diff:*)", "Bash(git log:*)",
                  "Bash(git show:*)", "Bash(git status:*)"]


def plugin_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def edit_rules(path, windows=None):
    """Permission rules that let a session write under `path` only (see docs/probe-results.md)."""
    if windows is None:
        windows = os.name == "nt"
    p = native_path(path.replace("\\", "/"), windows).rstrip("/")
    if windows and re.match(r"^[a-zA-Z]:/", p):
        drive, rest = p[0], p[2:]
        return ["Edit(//%s%s/**)" % (drive.lower(), rest), "Edit(//%s:%s/**)" % (drive.upper(), rest)]
    return ["Edit(/%s/**)" % p]


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
        args += ["--permission-mode", "manual"]
    else:
        args += ["--permission-mode", "bypassPermissions"]
    return args


def repo_name(key):
    name = os.path.basename(key)
    if name == ".git":
        name = os.path.basename(os.path.dirname(key))
    if name.endswith(".git"):
        name = name[:-4]
    return slug(name)


def pm_dir(key):
    return os.path.join(oke_home(), "repos", repo_name(key), "pm")


def children_path(key):
    return os.path.join(pm_dir(key), "children.json")


def session_of(agent):
    session = agent.get("agent_session")
    return session.get("value") if isinstance(session, dict) else None


def list_agents():
    return herdr("agent", "list").get("agents", [])


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
    if role == "pm":
        try:
            key = repo_key(cwd)
        except ValueError as e:
            return fail(str(e))
        known = True
    else:
        job = state["jobs"].get(header.get("job"))
        if job is None:
            return fail("unknown job %r in assignment" % header.get("job"))
        if job["phase"] not in ("working", "reviewing"):
            return fail("plan not approved: job %s is in phase %s" % (header["job"], job["phase"]))
        key = job["repo"]
        try:
            known = repo_key(cwd) == key
        except ValueError:
            known = False

    if any(a.get("name") == agent for a in list_agents()):
        return fail("agent name in use: %s" % agent)

    pane = args.pane
    if not pane:
        pane = herdr("pane", "split", "--pane", args.split_from, "--direction", "right",
                     "--cwd", cwd, "--no-focus")["pane"]["pane_id"]

    claude_args = launch_args(role, load_config(), oke_home(), plugin_root(), args.resume)
    try:
        herdr("agent", "start", agent, "--kind", "claude", "--pane", pane, "--", *claude_args)
    except HerdrError as e:
        if e.code != "agent_not_ready":
            return fail("agent start failed: %s" % e)
        screen = herdr_text("agent", "read", agent, "--source", "visible", "--lines", "40")
        if TRUST_MARKER not in screen.lower() or not known:
            return fail("agent %s is stopped at a dialog in pane %s:\n%s" % (agent, pane, screen))
        herdr("agent", "send-keys", agent, "down", "enter")
        herdr("agent", "wait", agent, "--until", "idle", "--timeout", "30000")

    herdr("agent", "prompt", agent, FIRST_PROMPT.format(role=role, path=path))
    info = herdr("agent", "get", agent).get("agent", {})
    record = {"agent": agent, "pane": info.get("pane_id", pane), "session": session_of(info),
              "seq": header["seq"], "role": role, "job": header.get("job")}
    if role == "pm":
        state = load_state()
        state["repos"].setdefault(key, {})["pm"] = record
        save_state(state)
    else:
        children = read_json(children_path(key), {})
        children[agent] = record
        write_json_atomic(children_path(key), children)
    print(json.dumps({"agent": agent, "pane": record["pane"], "session": record["session"]}))
    return 0


# ---------------------------------------------------------------- wait

FINAL_TYPES = ("done", "question", "blocked", "failed")
REPORT_TYPES = FINAL_TYPES + ("waiting",)
IDLE_CONFIRMATIONS = 3


def check_once(agent, report, seq):
    """One look at a child. Returns a result line, None to keep waiting, or a
    '?'-prefixed provisional verdict that only counts once the agent stays idle."""
    header = read_header(report)
    current = header is not None and header.get("seq") == seq
    if current and header.get("type") in FINAL_TYPES:
        return "report %s" % header["type"]
    try:
        info = herdr("agent", "get", agent).get("agent", {})
    except HerdrError as e:
        if e.code in ("agent_not_found", "pane_not_found"):
            return "gone"
        raise
    status = info.get("agent_status")
    if status == "blocked":
        return "blocked\n" + herdr_text("agent", "read", agent, "--source", "visible", "--lines", "40")
    if status not in ("idle", "done"):
        return None
    if current and header.get("type") == "waiting":
        return None
    if os.path.exists(report):
        if header is None:
            return "?bad_report no header (expected key: value lines, then ---)"
        if "type" not in header or "seq" not in header:
            return "?bad_report header needs type and seq"
        if header["type"] not in REPORT_TYPES:
            return "?bad_report unknown type %r" % header["type"]
        if not isinstance(header["seq"], int):
            return "?bad_report seq is not a number"
    return "?idle_no_report"


def wait_loop(agent, report, seq, poll=2, max_polls=None):
    """No time limit: a five hour simulation is a normal wait. A herdr state read
    right after input can be stale and a report can be mid-write, so an idle
    verdict needs IDLE_CONFIRMATIONS consecutive polls."""
    idle = 0
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        result = check_once(agent, report, seq)
        if result is None:
            idle = 0
        elif result.startswith("?"):
            idle += 1
            if idle >= IDLE_CONFIRMATIONS:
                return result[1:]
        else:
            return result
        if poll:
            time.sleep(poll)
    return None


def cmd_wait(args):
    print(wait_loop(args.agent, native_path(args.report), args.seq, poll=args.poll))
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
    state = load_state()
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
        result["repos"][key] = {"pm": "alive" if found else "dead",
                                "agent": record.get("agent"), "pane": record.get("pane")}

    for name, job in state["jobs"].items():
        key = job.get("repo")
        children = read_json(children_path(key), {}) if key else {}
        job_agents = dict((a, "alive" if a in names else "dead")
                          for a, rec in children.items() if rec.get("job") == name)
        if job.get("phase") in ("awaiting_approval", "awaiting_merge"):
            cls = "awaiting_user"
        elif result["repos"].get(key, {}).get("pm") == "alive":
            cls = "alive"
        else:
            cls = "disconnected"
        result["jobs"][name] = {"phase": job.get("phase"), "class": cls, "agents": job_agents}

    here = os.environ.get("HERDR_PANE_ID")
    recorded = state["orchestrator"].get("pane")
    if recorded and recorded != here and any(a.get("pane_id") == recorded for a in agents):
        result["orchestrator_conflict"] = recorded

    code = 0
    if args.claim:
        if result["orchestrator_conflict"]:
            code = fail("another orchestrator is alive in pane %s" % recorded)
        elif here and recorded != here:
            state["orchestrator"]["pane"] = here
            changed = True
    if changed or state.get("_recovered"):
        save_state(state)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


# ---------------------------------------------------------------- job

def cmd_job(args):
    state = load_state()
    home = oke_home()
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
        os.makedirs(job_dir)
        state["jobs"][args.slug] = {"repo": key, "phase": "intake", "worktree": None}
        save_state(state)
        print(job_dir)
        return 0
    if args.slug not in state["jobs"]:
        return fail("unknown job: %s" % args.slug)
    if args.action == "phase":
        if args.phase not in PHASES:
            return fail("bad phase %r: one of %s" % (args.phase, ", ".join(PHASES)))
        state["jobs"][args.slug]["phase"] = args.phase
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
    wait.add_argument("--report", required=True)
    wait.add_argument("--seq", type=int, required=True)
    wait.add_argument("--poll", type=float, default=2)
    wait.set_defaults(func=cmd_wait)

    status = sub.add_parser("status")
    status.add_argument("--claim", action="store_true")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return 1 if e.code else 0
    try:
        return args.func(args)
    except HerdrError as e:
        if e.code != "herdr_unavailable":
            return fail("herdr error: %s" % e)
        # exit 2: herdr itself is unreachable. Callers must not read this as "agent gone".
        sys.stderr.write("herdr unavailable: %s\n" % e.message)
        return 2


if __name__ == "__main__":
    sys.exit(main())
