#!/usr/bin/env python3
"""OKe helper: fixed procedures for the orchestrator / PM skills.

Python 3.6 compatible, standard library only.
"""
import argparse
import glob
import binascii
import errno
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

import automate
import runtime_bg
import runtime_herdr
import wiki
from runtime_herdr import DialogError, HerdrError, herdr, herdr_text, replace_file, session_of, write_private  # noqa: F401 (tests)

PHASES = ("intake", "planning", "awaiting_approval", "working", "reviewing",
          "awaiting_merge", "merging", "done", "failed")
EMPTY_STATE = {"orchestrator": {}, "pms": {}, "jobs": {}}


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
    p = p.rstrip("/") or "/"
    if windows and re.match(r"^[a-z]:$", p):
        p += "/"  # "c:" alone is the current folder on drive c, not its root
    return p


def oke_home():
    home = os.environ.get("OKE_HOME") or os.path.join(os.path.expanduser("~"), ".oke")
    home = os.path.abspath(native_path(home))
    if not os.path.isdir(home):
        os.makedirs(home, 0o700)  # agents' settings copies hold the user's env tokens
    return home


def load_config():
    home = oke_home()
    config = {
        "autonomy": "balanced",
        "wiki_path": os.path.join(home, "wiki"),
        "models": {"pm": "opus", "worker": "opus", "reviewer": "opus"},
        "lean_launch": True,  # children skip the user's plugins, hooks and MCP servers
        "max_review_rounds": 10,  # a job's reviews stop there and the PM asks the user; null: no limit
        "merge_ignore_untracked": [".omc/"],  # other tools' untracked folders never block a merge
        # effortLevel per role ("low" .. "max"); a lean child of a role left out uses the default
        "effort": {},
        "theme": "disco",  # the band's characters: disco (default), slime, mushroom, cute-dog, cute-cat, ... (README lists all)
        # a PM, worker or reviewer compacts at a task boundary only from this much context used
        "compact_min_percent": 30,
    }
    try:
        with io.open(os.path.join(home, "config.json"), encoding="utf-8") as f:
            user = json.load(f)
    except (IOError, OSError, ValueError):
        user = {}
    if isinstance(user, dict):
        models = user.pop("models", None)
        effort = user.pop("effort", None)
        config.update(user)
        if isinstance(effort, dict):
            config["effort"] = effort
        if isinstance(models, dict):
            config["models"].update(models)
    config["wiki_path"] = os.path.abspath(native_path(os.path.expanduser(config["wiki_path"])))
    return config


def read_header(path):
    """Return the `key: value` lines above the first `---`, or None."""
    try:
        with io.open(path, encoding="utf-8-sig") as f:
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
        f.flush()
        os.fsync(f.fileno())  # a crash must not leave state.json empty
    replace_file(tmp, path)


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
            raise StateError("state.json is unreadable: run `$OKE status` in the orchestrator")
        os.replace(path, path + ".bad")
        state["_recovered"] = True
        return state
    for key in EMPTY_STATE:
        if isinstance(loaded.get(key), dict):
            state[key] = loaded[key]
    for job in state["jobs"].values():
        if isinstance(job, dict) and "lane" in job:
            lane = job.pop("lane")
            if lane in OLD_LANES:
                job["size"] = OLD_LANES[lane]
    if loaded.get("runtime") in RUNTIMES:
        state["runtime"] = loaded["runtime"]
    # an OKe with one PM per repository wrote jobs with "repo" and no "pm": its finished
    # jobs stay, its PMs ("repos") go, its open jobs it must finish itself
    old = sorted(n for n, j in state["jobs"].items()
                 if isinstance(j, dict) and "repo" in j and not j.get("pm")
                 and j.get("phase") not in ("done", "failed"))
    if old:
        raise StateError("state.json is from an OKe with one PM per repository and still has open "
                         "jobs: %s. Finish or cancel them with that OKe, or move %s aside"
                         % (", ".join(old), path.replace("\\", "/")))
    for job in state["jobs"].values():  # finished jobs of that OKe: their key is "repo"
        if isinstance(job, dict) and "repo" in job and "git_key" not in job:
            job["git_key"] = job.pop("repo")
    return state


def save_state(state):
    write_json_atomic(state_path(), dict((k, v) for k, v in state.items() if k != "_recovered"))


def lock_path(name):
    return os.path.join(oke_home(), "locks", name)


_lock_fds = {}  # path -> fd of each lock this process holds


def try_lock(name):
    """An OS lock on $OKE_HOME/locks/<name>: the OS frees it when the holder dies,
    however long it held it. False when held, by another process or by this one."""
    path = lock_path(name)
    if path in _lock_fds:
        return False  # no nesting
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # byte 0 (fd is at offset 0); past EOF is allowed
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError) as e:
        os.close(fd)
        if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK):
            return False  # held by another process (msvcrt says EACCES or EDEADLK)
        raise StateError("cannot lock %s (%s): OKE_HOME needs a file system with locks "
                         "(an NFS home without lockd fails here)" % (path, e))
    _lock_fds[path] = fd
    return True


def unlock(name):
    """Only our own lock; the file stays."""
    fd = _lock_fds.pop(lock_path(name), None)
    if fd is not None:
        if os.name == "nt":
            import msvcrt
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # closing frees it too, but not at a set time
            except OSError:
                pass
        os.close(fd)


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
    """Normalized git common dir: the same value from a checkout and its worktrees.
    A folder outside git gets `<folder>/.git`, where `git init` would put it: a direct job."""
    cwd = native_path(path)
    try:
        p = subprocess.Popen(["git", "rev-parse", "--git-common-dir"], cwd=cwd,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
    except (OSError, ValueError):
        raise ValueError("not a folder: %s" % path)  # git missing, or no such folder
    if p.returncode != 0:
        if not os.path.isdir(cwd):
            raise ValueError("not a folder: %s" % path)
        up = os.path.realpath(cwd)
        while True:  # a repository git refuses (dubious ownership, a broken .git) is no plain folder
            if os.path.exists(os.path.join(up, ".git")):
                raise ValueError("git refuses %s: %s" % (path, err.decode("utf-8", "replace").strip()))
            if os.path.dirname(up) == up:
                break
            up = os.path.dirname(up)
        return normalize_path(os.path.join(os.path.realpath(cwd), ".git"))
    common = native_path(out.decode("utf-8", "replace").strip())
    if not os.path.isabs(common):
        common = os.path.join(cwd, common)
    return normalize_path(os.path.realpath(common))


def repo_dir(key):
    """The working folder of the repository whose git common dir is `key`; None for a bare one."""
    code, top = git_out(key, "rev-parse", "--show-toplevel")  # a submodule's git dir names it in core.worktree
    if code == 0 and top:  # git before 2.25 exits 0 with no output inside a .git folder
        return normalize_path(top)
    if git_out(key, "rev-parse", "--is-bare-repository")[1] == "true":
        return None
    return os.path.dirname(key)  # <checkout>/.git, or a direct job's folder


def slug(text, limit=24):
    s = re.sub(r"[^a-z0-9-]", "-", text.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    if not s or not s[0].isalpha():
        s = "x" + s
    return s[:limit].rstrip("-")


# ---------------------------------------------------------------- runtime

RUNTIMES = {"herdr": runtime_herdr.HerdrRuntime, "bg": runtime_bg.BgRuntime}


def default_runtime():
    """herdr inside a herdr pane, else bg."""
    return "herdr" if os.environ.get("HERDR_ENV") == "1" else "bg"
_runtimes = {}


def runtime():
    """The runtime that runs OKe's agents: $OKE_RUNTIME, else the one state.json names,
    else default_runtime() (see docs/superpowers/specs/2026-11-15-oke-runtime-layer-design.md)."""
    state = read_json(state_path(), {})
    # before the first claim (seen live: a plain `oke status` outside herdr said "not inside")
    default = default_runtime()
    name = os.environ.get("OKE_RUNTIME") or (state.get("runtime") if isinstance(state, dict) else None) or default
    if name not in RUNTIMES:
        raise StateError("unknown runtime %r: use %s" % (name, " or ".join(sorted(RUNTIMES))))
    if name not in _runtimes:
        _runtimes[name] = RUNTIMES[name]()
    return _runtimes[name]


def list_agents():
    return runtime().agents()


# ---------------------------------------------------------------- spawn

AGENT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
FIRST_PROMPT = ("Use the oke:{role} skill. Your assignment file is {path}. "
                "Read it first, then follow it.")


def folder_cost(folder):
    """What the agents that ran in `folder` cost: this session's figure (its mod rewrites
    usage.json after each turn) plus what earlier sessions there banked."""
    total = 0.0
    for name in ("usage.json", "usage-prev.json"):
        usage = read_json(os.path.join(folder, name), {})
        usd = usage.get("usd") if isinstance(usage, dict) else None
        if isinstance(usd, (int, float)):
            total += usd
    return round(total, 4)


def bank_usage(folder):
    """Before a new session starts in `folder`: its mod counts from zero again, so keep
    what the last one cost."""
    if not folder or not os.path.exists(os.path.join(folder, "usage.json")):
        return
    try:
        write_json_atomic(os.path.join(folder, "usage-prev.json"), {"usd": folder_cost(folder)})
        os.remove(os.path.join(folder, "usage.json"))
    except (IOError, OSError):
        pass  # the cost just reads lower


def agent_command(role, config, home, plugin_dir, folder, resume=None):
    """The claude arguments for one agent. A PM also reads the whole file system."""
    bank_usage(folder)
    header = read_header(os.path.join(folder, "assignment.md")) if folder else None
    extra = []
    if role == "pm" and header and header.get("agent"):
        extra = fs_roots()
    return launch_args(role, config, home, plugin_dir, resume, folder, header, extra)
REVIEWER_TOOLS = ["Read", "Grep", "Glob", "Skill", "Bash(git diff:*)", "Bash(git log:*)",
                  "Bash(git show:*)", "Bash(git status:*)"]
# shortcut: relies on `*` matching anywhere in a Bash rule (not probed live, see
# docs/probe-results.md); the PM's `git status` check after a review is the backstop.
REVIEWER_DENIED = ["Bash(git *--output*)"]
# The mod's GIT_DENIED as permission rules, which Claude Code enforces itself (the mod's
# check reads the command text only). shortcut: prefix rules miss `git -C <dir> push` and
# git run from a script; the mod's regex still catches the first.
GIT_DENY_RULES = ["Bash(git push:*)", "Bash(git reset --hard:*)", "Bash(git reset --merge:*)",
                  "Bash(git clean -f:*)", "Bash(git clean --force:*)", "Bash(git branch -D:*)",
                  "Bash(git checkout -f:*)", "Bash(git checkout --force:*)", "Bash(git stash drop:*)",
                  "Bash(git stash clear:*)", "Bash(git update-ref -d:*)", "Bash(git filter-branch:*)",
                  "Bash(git filter-repo:*)", "Bash(git worktree remove --force:*)"]


def worker_deny_rules(folder):
    """Edit rules a worker may not have: the job's files the merge gate trusts (plan,
    ledger, the reviewer's and the PM's folders). Its own folder stays writable.
    shortcut: Bash can still write them (`echo > ...`); the mod's workerDenied and the
    user's merge approval are the other layers."""
    job_dir = os.path.dirname(folder)
    if os.path.basename(os.path.dirname(job_dir)) != "jobs":
        return []
    rules = []
    # reviewer folders are <job>-rv[N]: a bare *-rv* would also match `fix-rv-w1`
    for tail in ("/*.md", "/*.tsv", "/*.head", "/*.json", "/*.log", "/pm/**", "/%s-rv*/**" % os.path.basename(job_dir)):
        rules += edit_rules(job_dir, tail=tail)
    return rules


def key_deny_rules(home):
    """No agent reads or changes the ledger key: with it, any row could be signed."""
    rules = edit_rules(home, tail="/" + LEDGER_KEY) + edit_rules(home, tail="/" + LEDGER_KEY + ".*")
    return rules + [r.replace("Edit(", "Read(", 1) for r in rules] + ["Bash(*%s*)" % LEDGER_KEY]


def plugin_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def edit_rules(path, windows=None, tail="/**"):
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
            new = ["Edit(//%s%s%s)" % (drive.lower(), rest, tail), "Edit(//%s:%s%s)" % (drive.upper(), rest, tail)]
        else:
            new = ["Edit(/%s%s)" % (p, tail)]
        rules += [r for r in new if r not in rules]
    return rules


def claude_config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def user_json(path):
    data = read_json(path, {})
    return data if isinstance(data, dict) else {}


def user_mcp_servers():
    """MCP servers the user configured (user scope), by name."""
    path = os.path.join(os.path.dirname(claude_config_dir()), ".claude.json")         if not os.environ.get("CLAUDE_CONFIG_DIR") else os.path.join(claude_config_dir(), ".claude.json")
    servers = user_json(path).get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def user_plugins():
    """Plugins the user has enabled, as name@marketplace."""
    enabled = user_json(os.path.join(claude_config_dir(), "settings.json")).get("enabledPlugins") or {}
    return sorted(k for k, v in enabled.items() if v) if isinstance(enabled, dict) else []


def header_list(header, field):
    return [x.strip() for x in str((header or {}).get(field) or "").split(",") if x.strip()]


def write_lean_settings(folder, plugins=(), dirs=()):
    """Settings for a lean child: only the user's herdr hooks (herdr learns the session
    id from them, revive needs it), PreToolUse guards, deny rules, env, and the plugins
    its assignment asks for.
    Returns the file path, or None if unwritable."""
    user = user_json(os.path.join(claude_config_dir(), "settings.json"))
    hooks = {}
    for event, groups in (user.get("hooks") or {}).items():
        kept = [g for g in groups if isinstance(g, dict) and any(
            "herdr" in str(h.get("command", "")).lower() for h in g.get("hooks") or [] if isinstance(h, dict))]
        if kept:
            hooks[event] = kept
    env = dict(user.get("env") or {}, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
    env.pop("CLAUDE_CODE_PLUGIN_DIRS", None)  # the user's own mods would load in every child
    env["OKE_HOME"] = oke_home()  # the skills say $OKE_HOME; a herdr pane does not inherit it
    settings = {"hooks": hooks, "env": env}
    # the user's compaction setting holds in children too; effort does not (a user's max
    # would run every child at max): config `effort` sets it per role
    if "autoCompactWindow" in user:
        settings["autoCompactWindow"] = user["autoCompactWindow"]
    if plugins:
        settings["enabledPlugins"] = dict((p, True) for p in plugins)
    # the user's deny rules and PreToolUse guards hold in a lean child too; `ask` rules
    # are left out: under dontAsk they would refuse silently what the user only wanted to see
    perms = user.get("permissions") if isinstance(user.get("permissions"), dict) else {}
    deny = [r for r in perms.get("deny") or [] if isinstance(r, str)]
    guards = [g for g in (user.get("hooks") or {}).get("PreToolUse") or [] if isinstance(g, dict)]
    if guards:
        hooks["PreToolUse"] = guards  # the herdr groups of this event are among them
    if deny or dirs:
        settings["permissions"] = {}
    if deny:
        settings["permissions"]["deny"] = deny
    if dirs:
        # `--add-dir` stops every new session at a trust dialog for a folder never
        # trusted (seen live with a fresh OKe home); this settings key does not
        settings["permissions"]["additionalDirectories"] = list(dirs)
    path = os.path.join(folder, "lean-settings.json")
    try:
        write_private(path, json.dumps(settings, indent=2, ensure_ascii=False))
    except (IOError, OSError):
        return None
    return path


# Built-in tools a lean child gets; an assignment's `tools:` adds more. Measured: the
# full built-in set is about 28k tokens of a 41k start, this set about 3k.
ROLE_TOOLS = {
    # SendMessage: in bg mode the PM hands its children their mail (a `deliver` wait result)
    "pm": ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "Skill", "Agent", "TaskStop",
           "TaskCreate", "TaskUpdate", "TaskList", "WebFetch", "WebSearch", "SendMessage"],
    "worker": ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "Skill", "Agent", "TaskStop",
               "TaskCreate", "TaskUpdate", "TaskList", "WebFetch", "WebSearch"],
    # Edit and Write so a pane reviewer can write its report.md and board.md (seen live: it
    # could not); its permission rules still allow writes only under the OKe folders.
    # shortcut: a repo's own .claude settings allowing Edit would merge in (project,local
    # sources); the mod's reviewerDenied is then the guard. Add a deny rule for the repo
    # path to launch_args if a repo ever does that.
    "reviewer": ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "Skill", "TaskCreate", "TaskUpdate",
                 "TaskList", "WebFetch", "WebSearch"],
}
# Always on, whatever the PM writes: built-in tools only (user's choice). MCP servers
# and plugins come only from the assignment header.


USAGE_FIELDS = ("input", "output", "cache_read", "cache_write")


def usage_totals(home):
    """Token totals per place from every usage.csv under `home` (the mod adds a line per
    turn): {place: {"turns", "input", "output", "cache_read", "cache_write", "models"}}.
    A place is a job (jobs/<job>, archive/<job>-<time>), a PM (pms/<name>), `old PMs` (an
    OKe with one PM per repository kept them under repos/) or the orchestrator."""
    out = {}
    for path in glob.glob(os.path.join(glob.escape(home), "**", "usage.csv"), recursive=True):
        parts = os.path.relpath(path, home).replace("\\", "/").split("/")
        if parts[0] in ("jobs", "archive", "pms"):
            place = "/".join(parts[:2])
        elif parts[0] == "repos":
            place = "old PMs"  # never the orchestrator's
        else:
            place = "orchestrator"
        row = out.setdefault(place, dict([("turns", 0), ("models", [])] + [(f, 0) for f in USAGE_FIELDS]))
        try:
            with io.open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (IOError, OSError):
            continue
        for line in lines[1:]:  # the first line names the columns
            cells = line.split(",")
            if len(cells) != 2 + len(USAGE_FIELDS):
                continue
            try:
                numbers = [int(c) for c in cells[2:]]
            except ValueError:
                continue
            row["turns"] += 1
            for name, n in zip(USAGE_FIELDS, numbers):
                row[name] += n
            if cells[1] and cells[1] not in row["models"]:
                row["models"].append(cells[1])
    return out


def cmd_cost(args):
    """Tokens per job, PM and orchestrator, from the per-turn usage.csv files."""
    totals = usage_totals(oke_home())
    if args.job:
        totals = dict((p, r) for p, r in totals.items() if p == "jobs/" + args.job
                      or re.match(r"archive/%s-\d{8}-\d{6}$" % re.escape(args.job), p))
    print(json.dumps(totals, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_extras(args):
    """What a PM may switch on for one child (`tools:`, `mcp:`, `plugins:` header lines)."""
    print(json.dumps({"default_tools": ROLE_TOOLS, "mcp": sorted(user_mcp_servers()),
                      "plugins": user_plugins()}, ensure_ascii=False))
    return 0


def cmd_rt(args):
    """Runtime actions the skills need, so they never call herdr (or claude) themselves."""
    rt = runtime()
    if args.rt == "agents":
        out = sorted(a.get("name") for a in rt.agents() if a.get("name"))
    elif args.rt == "read":
        sys.stdout.write(rt.screen(args.agent))
        return 0
    elif args.rt == "keys":
        rt.keys(args.agent, args.keys)
        out = {"agent": args.agent, "sent": args.keys}
    elif args.rt == "attach":
        hint = rt.attach(args.agent)
        # herdr shows the pane itself; a bg session needs the user to run the hint
        out = {"agent": args.agent, "attached": hint is None, "hint": hint}
    elif args.rt == "close":
        if len(args.place) == 1:
            rt.close(args.place[0])
            out = {"closed": args.place[0]}
        else:
            # a PM names the worker and the reviewer in one call (seen live): try each one,
            # so an "already closed" first one does not hide a second that stayed open
            closed, failed = [], {}
            for place in args.place:
                try:
                    rt.close(place)
                    closed.append(place)
                except HerdrError as e:
                    if e.code == "herdr_unavailable":
                        # say what closed before the outage: a retry would call it not found
                        print(json.dumps({"closed": closed, "failed": failed, "unavailable": e.message},
                                         ensure_ascii=False))
                        return 2
                    failed[place] = str(e)
            print(json.dumps({"closed": closed, "failed": failed}, ensure_ascii=False))
            return 1 if failed else 0
    elif args.rt == "place":
        if args.pm not in load_state()["pms"]:
            return fail("no PM %s: `$OKE pm add %s` first" % (args.pm, args.pm))
        folder = pm_dir(args.pm)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        out = {"place": rt.place_for_repo(folder, args.pm)}
    elif args.rt == "worktree":
        if args.remove:
            # herdr reuses workspace ids after a restart: never another job's (--job), or
            # without --job (an older PM) one a job still works in
            owner = [n for n, j in load_state()["jobs"].items()
                     if (j.get("worktree") or {}).get("id") == args.remove
                     and (n != args.remove_job if args.remove_job
                          else j.get("phase") not in DONE_PHASES + ("merging",))]
            if owner:
                return fail("worktree %s is job %s's now, and that job still works in it: not removed"
                            % (args.remove, owner[0]))
            try:
                rt.worktree_remove(args.remove)
            except HerdrError as e:
                # herdr: closing a worktree's last pane (a hold, a cancel) closes its workspace
                # too, and the id goes with it; the checkout stays: remove it by its path
                state = load_state()
                found = [(n, j) for n, j in state["jobs"].items()
                         if (j.get("worktree") or {}).get("id") == args.remove]
                if e.code != "workspace_not_found" or len(found) != 1:  # never a guess between jobs
                    raise
                job, info = found[0]
                if not info.get("git_key"):
                    raise
                # revived children run outside the workspace, in the checkout: on Windows git
                # cannot delete a folder a shell sits in (half deleted, then "Permission denied")
                close_children(job, info.get("pm"))
                runtime_bg.git_worktree_remove(native_path(info["worktree"]["path"]),
                                               native_path(repo_dir(info["git_key"])))
            out = {"removed": args.remove}
        else:
            if not args.job:
                return fail("worktree needs <job> (or --remove <id>)")
            info = load_state()["jobs"].get(args.job)
            if info is None:
                return fail("unknown job: %s" % args.job)
            if no_path(args.job, info):
                return fail(no_path(args.job, info))
            if info.get("direct"):
                return fail("job %s changes a folder outside git: no worktree" % args.job)
            # shortcut: `oke job merge` merges only oke/<job>, so the branch name is fixed here
            out = rt.worktree_create(native_path(info["path"]), "oke/" + args.job, args.job)
            with locked():
                state = load_state()
                if args.job in state["jobs"]:  # for --remove once its id is gone
                    for j in state["jobs"].values():  # herdr reuses workspace ids after a restart
                        if (j.get("worktree") or {}).get("id") == out["id"]:
                            j.pop("worktree")
                    state["jobs"][args.job]["worktree"] = {"id": out["id"], "path": out["path"]}
                    save_state(state)
    print(json.dumps(out, ensure_ascii=False))
    return 0


EFFORTS = ("low", "medium", "high", "xhigh", "max")  # `claude --effort`


def launch_args(role, config, home, plugin_dir, resume=None, folder=None, header=None, extra_dirs=()):
    """`header` is the agent's assignment header: its `tools:`, `mcp:` and `plugins:`
    (comma separated) are what the PM switched on for this job."""
    model = (header or {}).get("model") or config["models"].get(role, "opus")
    args = ["--model", model, "--plugin-dir", plugin_dir]
    # Measured: the user's plugins, hooks and MCP servers add about 36k tokens to every
    # child's context. Without a settings file, start as before so the herdr hook stays.
    folder = folder or home
    dirs = [home, config["wiki_path"]] + [d for d in extra_dirs if d not in (home, config["wiki_path"])]
    effort = (config.get("effort") or {}).get(role)
    if effort in EFFORTS:
        args += ["--effort", effort]  # config.json `effort`; a role left out keeps the user's setting
    settings = config.get("lean_launch", True) and write_lean_settings(folder, header_list(header, "plugins"), dirs)
    if not settings:
        for d in dirs:
            args += ["--add-dir", d]
    if settings:
        args += ["--setting-sources", "project,local", "--strict-mcp-config", "--settings", settings]
        tools = ROLE_TOOLS.get(role, ROLE_TOOLS["worker"])
        tools = tools + [t for t in header_list(header, "tools") if t not in tools]
        args += ["--tools", ",".join(tools)]
        known = user_mcp_servers()
        servers = dict((n, known[n]) for n in header_list(header, "mcp") if n in known)
        if servers:
            path = os.path.join(folder, "mcp.json")
            try:
                write_private(path, json.dumps({"mcpServers": servers}, indent=2, ensure_ascii=False))
                args += ["--mcp-config", path]
            except (IOError, OSError):
                pass  # the child starts without them
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
    elif settings:
        # A lean child does not read the user's bypass acceptance, so bypassPermissions
        # stops at its warning dialog on every start (seen live). dontAsk with the
        # agent's own tools allowed runs the same without it; --allowedTools goes last.
        args += ["--permission-mode", "dontAsk", "--allowedTools"] + tools
    else:
        args += ["--permission-mode", "bypassPermissions"]
    # deny rules beat allow rules and bypass mode; `git diff/log/show --output=<file>` writes a file
    denied = GIT_DENY_RULES + (REVIEWER_DENIED if role == "reviewer" else []) + key_deny_rules(home)
    if role == "worker" and folder and folder != home:
        denied = denied + worker_deny_rules(folder)
    args += ["--disallowedTools"] + denied
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


def set_old_wiki_aside(name):
    """A wiki folder `name` left by an OKe with one PM per repository (its `.oke-repo`
    marker, or no log.md a PM wrote) is moved to `<name>.old-<time>`, unread. Returns
    the new place, or None."""
    path = os.path.join(load_config()["wiki_path"], name)
    if not os.path.isdir(path):
        return None
    if not os.path.exists(os.path.join(path, ".oke-repo")) and (
            os.path.exists(os.path.join(path, "log.md")) or not os.listdir(path)):
        return None
    aside = "%s.old-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    os.rename(path, aside)
    return aside


def wiki_dir(name):
    """The PM's wiki folder (one per PM), made if missing."""
    path = os.path.join(load_config()["wiki_path"], name)
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def pm_dir(name):
    """A PM's own folder, also its session's working folder."""
    return os.path.join(oke_home(), "pms", name)


PM_NAME = re.compile(r"^[a-z][a-z0-9-]{0,23}$")


def pm_name_problem(name):
    """Why `name` cannot name a PM, or None."""
    if not PM_NAME.match(name or ""):
        return "bad PM name %r: lowercase letters, digits and -, at most 24, starting with a letter" % name
    if name in ("orchestrator", ORCH_NAME) or re.search(r"-(w\d+|rv\d*)$", name):
        return "PM name %r is taken by OKe (the orchestrator, or a worker or reviewer name)" % name
    return None


def under(path, folder):
    path, folder = path.rstrip("/"), folder.rstrip("/")  # a drive root c:/ compares as c:
    return path == folder or path.startswith(folder + "/")


def real_dir(path):
    """`path` normalized with its links resolved, as git resolves a checkout's top, and on
    Windows its 8.3 short names (KNOWLE~1) made long."""
    return normalize_path(long_path(os.path.realpath(os.path.abspath(native_path(path)))))


def long_path(p):
    """On Windows, `p` with the short names of its longest existing part made long
    (Python 3.6's realpath leaves them); elsewhere `p`."""
    if os.name != "nt":
        return p
    head, tail = p, []
    while head and not os.path.exists(head):
        head, part = os.path.split(head)
        if not part:
            return p
        tail.insert(0, part)
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        buf = ctypes.create_unicode_buffer(32768)
        # the final path resolves junctions and links too (Python 3.6's realpath does not)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        # no access needed, any sharing, FILE_FLAG_BACKUP_SEMANTICS so a folder opens too
        h = k32.CreateFileW(head, 0, 7, None, 3, 0x02000000, None)
        if h and h != wintypes.HANDLE(-1).value:
            try:
                n = k32.GetFinalPathNameByHandleW(h, buf, 32768, 0)
            finally:
                k32.CloseHandle(h)
            if 0 < n < 32768:
                final = buf.value
                if final.startswith("\\\\?\\UNC\\"):
                    final = "\\\\" + final[8:]
                elif final.startswith("\\\\?\\"):
                    final = final[4:]
                return os.path.join(final, *tail) if tail else final
        n = k32.GetLongPathNameW(head, buf, 32768)
        if 0 < n < 32768:
            head = buf.value
    except (ImportError, AttributeError, OSError, ValueError):
        pass
    return os.path.join(head, *tail) if tail else head


SPLIT_PHASES = ("intake", "planning", "awaiting_approval")  # a job splits only before it works


def pm_job_dir(job):
    """The orchestrator-PM channel of one job: assignment.md and report.md."""
    return os.path.join(oke_home(), "jobs", job, "pm")


def path_facts(path):
    """(git_key, folder, direct) for a job path. git_key is OKe's private lock and git
    key: the git common dir, or `<folder>/.git` for a folder outside git. A file stands
    for its folder. Raises ValueError."""
    p = native_path(path)
    if os.path.isfile(p):
        p = os.path.dirname(os.path.abspath(p))
    key = repo_key(p)
    if os.path.exists(key):
        folder = repo_dir(key)
        if folder is None:
            raise ValueError("%s is a bare repository (no working folder): give a checkout's folder" % path)
        return key, normalize_path(folder), False
    return key, normalize_path(os.path.dirname(key)), True


def set_job_path(state, name, path, branch_taken=None):
    """Give job `name` its one path (state is not saved): git or direct, one open direct
    job per folder, the PM's places. Returns the job. Raises ValueError; `branch_taken`
    is the advice when branch oke/<name> exists already (default: pick another name)."""
    job = state["jobs"][name]
    key, folder, direct = path_facts(path)
    if direct and job.get("size") != "research":
        # research changes nothing: never in the way. A folder inside another's, or holding
        # it, is the same files
        busy = sorted(n for n, j in state["jobs"].items()
                      if n != name and j.get("direct") and j.get("path") and j.get("size") != "research"
                      and j.get("phase") not in DONE_PHASES
                      and (under(folder, j["path"]) or under(j["path"], folder)))
        if busy:
            raise ValueError("job %s already changes %s in place (no git, no undo), which overlaps %s: "
                             "finish or archive it first" % (busy[0], state["jobs"][busy[0]]["path"], folder))
    if not direct and job.get("size") != "research" and subprocess.call(["git", "-C", native_path(folder), "show-ref", "--verify", "--quiet",
                                       "refs/heads/oke/" + name]) == 0:
        raise ValueError(branch_taken % {"job": name, "checkout": folder} if branch_taken else
                         "branch oke/%s already exists in %s (an earlier job): pick another job name"
                         % (name, folder))
    # the branch the main checkout is on now: `job merge` merges only into it
    base = None if direct else git_out(native_path(folder), "symbolic-ref", "-q", "--short", "HEAD")[1]
    job.update(path=folder, git_key=key, base=base or None)
    if direct:
        job["direct"] = True
    else:
        job.pop("direct", None)
    places = state["pms"][job["pm"]].setdefault("places", [])
    if folder in places:
        places.remove(folder)
    places.insert(0, folder)
    return job


def pm_fields(pm, job):
    """What a job's PM assignment header needs, as `job new` prints it."""
    return {"pm_agent": pm, "pm_dir": pm_dir(pm).replace("\\", "/"),
            "pm_job_dir": pm_job_dir(job).replace("\\", "/"),
            "wiki": wiki_dir(pm).replace("\\", "/"),
            "shared_wiki": shared_wiki().replace("\\", "/")}


def no_path(name, info):
    """The refusal for a job that has no path yet, else None."""
    if info.get("path") and info.get("git_key"):
        return None
    return ("job %s has no path yet: its PM sets it while planning (`$OKE job path %s <path>`)" % (name, name))


def fs_roots():
    """The file system's roots: `/`, or on Windows every drive that exists. A PM's session
    reads all of them (it finds each job's path itself); writes stay with the guard."""
    if os.name != "nt":
        return ["/"]
    return [d + ":/" for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(d + ":/")]


def write_areas(state, role, pm, job=None):
    """Where an agent's Edit and Write may land in its jobs: [{"path", "direct"}]. A git
    job: its worktree, else its main checkout. A direct job: its folder."""
    names = [n for n, j in state["jobs"].items() if j.get("pm") == pm] if role == "pm" else [job]
    out = []
    for n in names:
        j = state["jobs"].get(n) or {}
        if j.get("phase") in DONE_PHASES or not j.get("path") or j.get("size") == "research" or j.get("held"):
            continue  # research changes nothing, a held job is not worked on: no write area
        if j.get("direct"):
            out.append({"path": j["path"], "direct": True})
        else:
            out.append({"path": normalize_path((j.get("worktree") or {}).get("path") or j["path"]), "direct": False})
    return out


def memory_dir(cwd):
    """The Claude Code memory folder of a session started in `cwd`."""
    folder = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(native_path(cwd)))
    return os.path.join(os.path.abspath(native_path(claude_config_dir())), "projects", folder, "memory")


def memory_dirs(cwd):
    """Every memory folder a session in `cwd` may use: the one named after `cwd`, and, in
    git, the one Claude Code shares across a repository's worktrees and subfolders, named
    after the main checkout's root (the folder that holds the git common dir)."""
    out = [memory_dir(cwd)]
    code, common = git_out(native_path(cwd), "rev-parse", "--git-common-dir")
    if code == 0 and common:
        common = os.path.normpath(os.path.join(native_path(cwd), native_path(common)))
        if os.path.basename(common) == ".git":  # a bare repository has no main checkout
            main = memory_dir(os.path.dirname(common))
            if main not in out:
                out.append(main)
    return out


def agent_cwd(me):
    folder = agent_folder(*me)
    return (read_header(os.path.join(folder, "assignment.md")) or {}).get("cwd") if folder else None


# files under $OKE_HOME only OKe writes, also inside a folder an agent may write: the merge
# gate, the interview, the peer talk, a PM's children and every agent's launch settings
GATE_FILE = re.compile(r"^(ledger\.(tsv|head)|baseline\.(json|log)|interview\.json|peers\.(json|md)|intake\.md|"
                       r"children\.json|(lean-|bg-)?settings\.json|mcp\.json|(check|verify)-\d+\.log)$")


def home_areas(state, me):
    """The folders under $OKE_HOME agent `me` may Edit or Write: its own folder, a PM's
    open jobs' folders (not held ones), and tmp/. Never state.json, config.json, another PM's folder,
    automations/ or the wikis."""
    role, name, pm, record = me
    out = [agent_folder(role, name, pm, record), os.path.join(oke_home(), "tmp")]
    if role == "pm":
        out += [os.path.join(oke_home(), "jobs", n) for n in open_jobs(state, pm)]  # not held ones
    return [real_dir(d) for d in out if d]


def next_step(me):
    """What an agent does about a write it may not make."""
    return {"pm": "If a job must change it: while the job plans, set its path with `$OKE job path`, or split "
                  "a new job for that folder with `$OKE job split`; once it works, report a question for it.",
            "worker": "If the job needs it changed, report a question to your PM.",
            "reviewer": "Reviewers change nothing: name it in your review."}.get(me[0], "")


def may_write(state, me):
    """The agent's write areas, for a refusal."""
    cwd = agent_cwd(me)
    places = [d.replace("\\", "/") for d in home_areas(state, me)]
    if cwd:
        places += [m.replace("\\", "/") for m in memory_dirs(cwd)]
    places += [a["path"] for a in write_areas(state, me[0], me[2], me[3].get("job"))]
    return ", ".join(places)


def write_problem(state, me, target):
    """Why the agent `me` may not Edit or Write `target`, or None. Every check uses the
    path with links and short names resolved, so neither leads past it."""
    role, name, pm, record = me
    raw = normalize_path(os.path.abspath(native_path(target)))
    p = real_dir(target)
    wikis = real_dir(load_config()["wiki_path"])
    if under(p, wikis) or under(raw, wikis):
        return "%s is in a wiki: change wiki pages with `$OKE wiki` (read it, then write it --from a file)" % target
    home = real_dir(oke_home())
    if under(p, home) or under(raw, home):
        if not any(under(p, d) for d in home_areas(state, me)):
            return ("%s is OKe's own file, not this agent's to write. It may write in: %s. %s"
                    % (target, may_write(state, me), next_step(me)))
        if GATE_FILE.match(os.path.basename(p)):
            return ("%s is written only by OKe (`oke` commands keep it; the merge gate trusts it): leave it. %s"
                    % (target, next_step(me)))
        return None
    cwd = agent_cwd(me)
    if cwd and any(under(p, real_dir(m)) for m in memory_dirs(cwd)):
        return None
    # allowed when any area allows it; else the most specific area's reason
    areas = sorted((a for a in write_areas(state, role, pm, record.get("job"))
                    if under(raw, a["path"]) or under(p, real_dir(a["path"]))), key=lambda a: -len(a["path"]))
    reasons = [area_problem(p, target, a) for a in areas]
    if areas:
        return None if None in reasons else reasons[0]
    return ("%s is outside what this agent may write. It may write in: %s. %s"
            % (target, may_write(state, me), next_step(me)))


def area_problem(p, target, area):
    """Why `target` (resolved: `p`) under `area` is not that job's to write, or None."""
    top = real_dir(area["path"])
    if not under(p, top):
        return ("%s leads outside the job's folder %s (a link to %s): not this job's to change; change it "
                "as its own job at %s" % (target, area["path"], p, p))
    if not area["direct"]:
        return None
    folder = os.path.dirname(p)
    while folder != top and under(folder, top):  # the resolved parents: a junction cannot skip one
        if os.path.exists(os.path.join(native_path(folder), ".git")):
            return ("%s is inside the git repository %s, which is not this job's folder %s: change it as "
                    "its own job at %s" % (target, folder, area["path"], folder))
        folder = os.path.dirname(folder)
    return None


def write_pm_assignment(state, name):
    """The PM's standing assignment, which OKe keeps: who it is and its wiki."""
    folder = pm_dir(name)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    path = os.path.join(folder, "assignment.md")
    old = (read_header(path) or {}).get("seq")
    lines = [u"agent: %s" % name, u"role: pm", u"parent: orchestrator",
             u"cwd: %s" % folder.replace("\\", "/"),
             u"wiki: %s" % wiki_dir(name).replace("\\", "/"),
             u"shared_wiki: %s" % shared_wiki().replace("\\", "/"),
             u"seq: %d" % ((old + 1) if isinstance(old, int) else 1), u"---", u"## Goal",
             u"You are PM %s. Each job has one path, which you find while planning (the paths of your" % name,
             u"jobs so far are your places: `$OKE pm list`). Each job has its own assignment at",
             u"$OKE_HOME/jobs/<job>/pm/assignment.md and its own report at $OKE_HOME/jobs/<job>/pm/report.md.",
             u'New instructions arrive as messages that start with "OKe inbox:".']
    with io.open(path + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        f.write(u"\n".join(lines) + u"\n")
    replace_file(path + ".tmp", path)
    return path


def pm_alive(state, name):
    record = (state["pms"].get(name) or {}).get("pm") or {}
    try:
        return bool(record.get("agent") and live_agent(record, list_agents()))
    except HerdrError:
        return False


def stop_pm(state, name):
    """Close a PM's session (best effort). Returns its pane when it may still run (the runtime
    did not answer or did not close it), else None."""
    record = (state["pms"].get(name) or {}).get("pm") or {}
    if not record.get("pane") or not record.get("agent"):
        return None
    try:
        if not live_agent(record, list_agents()):
            return None
    except HerdrError:
        return record["pane"]  # the runtime did not say: it may still run
    try:
        runtime().close(record["pane"])
    except HerdrError as e:
        if e.code == "herdr_unavailable":
            raise
        return record["pane"]
    return None


def cancel_for_pm(name, jobs):
    """Cancel `jobs` of PM `name` (the user confirmed). Returns ([the jobs that could not be],
    [panes of the cancelled jobs the runtime did not close])."""
    refused, panes = [], []
    for job in jobs:
        out = io.StringIO()
        old, sys.stderr = sys.stderr, out
        try:
            with locked():
                code = job_locked(argparse.Namespace(action="cancel", slug=job))
        finally:
            sys.stderr = old
        if code:
            refused.append("%s (%s)" % (job, out.getvalue().strip()))
            continue
        try:
            _, left, kept = close_children(job, name)
            panes.extend(left + kept)
        except Exception:  # noqa: BLE001 - an outage: the cancel stands, the panes are named
            panes.append("the panes of %s" % job)
        drop_job_mail(job, name)
    return refused, panes


def cancel_problem(state, job):
    """Why `job cancel` refuses `job`, or None."""
    info = state["jobs"][job]
    merged = merge_state(job)[0]
    if info.get("phase") == "done" or merged in ("unverified", "verified"):
        return ("job %s is %s: its merge is in main or under way. Let it finish (verify, or "
                "roll it back), not cancel" % (job, info.get("phase") if merged == "none" else
                                               "merged (%s)" % merged))
    return None


def move_folders(pairs):
    """Rename each (old, new) folder; an `old` already gone was moved by an earlier try.
    All or nothing: on a failure the ones moved here go back, and OSError names the folder."""
    done = []
    try:
        for old, new in pairs:
            if not os.path.isdir(old):
                continue
            if os.path.isdir(new) and not os.listdir(new):
                os.rmdir(new)
            if os.path.exists(new):
                raise OSError("%s exists already" % new.replace("\\", "/"))
            if not os.path.isdir(os.path.dirname(new)):
                os.makedirs(os.path.dirname(new))
            os.rename(old, new)
            done.append((old, new))
    except OSError as e:
        failed = old
        for back, moved in reversed(done):
            try:
                os.rename(moved, back)
            except OSError:
                pass
        raise OSError("could not move %s: %s" % (failed.replace("\\", "/"), e))


def pm_jobs(state, name):
    """Open and held jobs of PM `name`."""
    return sorted(n for n, j in state["jobs"].items() if j.get("pm") == name and j.get("phase") not in DONE_PHASES)


def cmd_pm(args):
    if args.action == "list":
        state = load_state()
        want = real_dir(args.path) if args.path else None  # resolved, as places are
        out, best = [], (0, None)
        for name in sorted(state["pms"]):
            places = state["pms"][name].get("places") or []
            entry = {"name": name, "places": places, "alive": pm_alive(state, name),
                     "open_jobs": sorted(open_jobs(state, name)), "held_jobs": held_jobs(state, name),
                     "pm_dir": pm_dir(name).replace("\\", "/")}
            for place in places if want else []:
                if under(want, place) and len(place) > best[0]:
                    best = (len(place), name)
            out.append(entry)
        if want:  # the PM whose places hold the path the user said, most specific first
            for entry in out:
                entry["recommended"] = entry["name"] == best[1]
        print(json.dumps(out, ensure_ascii=False))
        return 0
    with locked():
        state = load_state()
        if args.action == "add":
            problem = pm_name_problem(args.name)
            if problem:
                return fail(problem)
            if args.name in state["pms"]:
                return fail("PM %s exists already" % args.name)
            try:
                aside = set_old_wiki_aside(args.name)
            except OSError as e:
                return fail("an old wiki named %s is in the way and could not be moved aside: %s. Close what "
                            "holds it and run this again" % (args.name, e))
            state["pms"][args.name] = {"pm": {}, "places": []}
            save_state(state)
            write_pm_assignment(state, args.name)
            out = {"name": args.name, "pm_dir": pm_dir(args.name).replace("\\", "/"),
                   "wiki": wiki_dir(args.name).replace("\\", "/")}
            if aside:
                out["old_wiki"] = aside.replace("\\", "/")  # an older OKe's wiki of this name, set aside
            print(json.dumps(out, ensure_ascii=False))
            return 0
        if args.name not in state["pms"]:
            return fail("no PM %s" % args.name)
        if args.action == "rename":
            problem = pm_name_problem(args.new)
            if problem:
                return fail(problem)
            # a folder of the new name left by an earlier try is fine while the old one is gone
            if args.new in state["pms"] or (os.path.exists(pm_dir(args.new)) and os.path.exists(pm_dir(args.name))):
                return fail("PM %s exists already" % args.new)
            busy = pm_jobs(state, args.name)
            if busy:
                return fail("PM %s has open or held jobs: %s. Rename it when they are done or cancelled"
                            % (args.name, ", ".join(busy)))
            wikis = load_config()["wiki_path"]
            try:
                aside = set_old_wiki_aside(args.new)
            except OSError as e:
                return fail("an old wiki named %s is in the way and could not be moved aside: %s. Close what "
                            "holds it and run this again" % (args.new, e))
            if os.path.exists(os.path.join(wikis, args.new)) and os.path.exists(os.path.join(wikis, args.name)) \
                    and os.listdir(os.path.join(wikis, args.new)):
                return fail("a wiki named %s exists already" % args.new)
            left = stop_pm(state, args.name)
            try:
                move_folders([(pm_dir(args.name), pm_dir(args.new)), (inbox_dir(args.name), inbox_dir(args.new)),
                              (os.path.join(wikis, args.name), os.path.join(wikis, args.new))])
            except OSError as e:
                return fail("PM %s was not renamed, nothing changed: %s. Close what holds it and run this "
                            "again" % (args.name, e))
            entry = state["pms"].pop(args.name)
            entry["pm"] = {}  # a new name starts a new session
            state["pms"][args.new] = entry
            for j in state["jobs"].values():
                if j.get("pm") == args.name:
                    j["pm"] = args.new
            save_state(state)
            write_pm_assignment(state, args.new)
            out = {"name": args.new, "pm_dir": pm_dir(args.new).replace("\\", "/")}
            if aside:
                out["old_wiki"] = aside.replace("\\", "/")
            if left:
                out["not_closed"] = [left]  # the old session may still run: the user closes it
            print(json.dumps(out, ensure_ascii=False))
            return 0
        if args.action != "remove":
            return fail("unknown action")
        busy = pm_jobs(state, args.name)
        if busy and not args.cancel:
            return fail("PM %s has open or held jobs: %s. Ask the user; with their yes, run this again "
                        "with --cancel (cancels them)" % (args.name, ", ".join(busy)))
        # all or nothing: a job that cannot be cancelled stops the remove before any cancel
        refused = [p for p in (cancel_problem(state, j) for j in busy) if p]
        if refused:
            return fail("cannot cancel every job of PM %s, so nothing was cancelled or removed: %s"
                        % (args.name, "; ".join(refused)))
    # cancels run outside this lock (they take it themselves)
    refused, panes = cancel_for_pm(args.name, busy)
    if refused:
        return fail("could not cancel: %s. Nothing was removed; jobs cancelled so far stay cancelled "
                    "(`$OKE pm remove` lists them under `archive` once it succeeds)%s"
                    % ("; ".join(refused), "; not closed: " + ", ".join(panes) if panes else ""))
    with locked():
        state = load_state()
        new = sorted(set(pm_jobs(state, args.name)) - set(busy))
        if new:  # the PM made them while its jobs were cancelled (a split)
            return fail("PM %s got new jobs meanwhile: %s. Nothing was removed; jobs cancelled so far stay "
                        "cancelled. Ask the user again, then run this again" % (args.name, ", ".join(new)))
        left = stop_pm(state, args.name)
        if left:
            panes.append(left)
        # a later PM of this name starts clean and gets none of this one's mail; the
        # folders move first, so a failed move leaves the PM as it was (run it again)
        gone = pm_dir(args.name) + ".removed-" + time.strftime("%Y%m%d-%H%M%S")
        try:
            wiki_old = os.path.join(load_config()["wiki_path"], args.name)
            move_folders([(pm_dir(args.name), gone), (inbox_dir(args.name), os.path.join(gone, "inbox")),
                          (wiki_old, wiki_old + gone[len(pm_dir(args.name)):])])  # <name>.removed-<time>
        except OSError as e:
            return fail("PM %s was not removed (its cancelled jobs stay cancelled): %s. Close what holds "
                        "it and run this again; it lists them under `archive` once it succeeds" % (args.name, e))
        # every job of this PM left unarchived as failed: this run's cancels and an earlier,
        # failed run's alike, so none is left behind
        archive = sorted(j for j, i in state["jobs"].items()
                         if i.get("pm") == args.name and i.get("phase") == "failed")
        state["pms"].pop(args.name, None)
        save_state(state)
    out = {"removed": args.name, "cancelled": busy, "archive": archive}
    if panes:
        out["not_closed"] = panes  # the runtime did not close them: the user closes them
    print(json.dumps(out))
    return 0


def cmd_path_check(args):
    """May the agent in `pane` Edit or Write `path`? See write_problem."""
    # the mod asks only for a PM, worker or reviewer: whatever goes wrong refuses
    try:
        state = load_state()
        me = whoami(state, args.pane)[0]
        if not me or me[0] not in ("pm", "worker", "reviewer"):
            problem = cannot_check("pane %s is no OKe PM, worker or reviewer OKe knows" % args.pane)
        elif me[2] not in state["pms"]:
            problem = cannot_check("PM %s is not in state.json" % me[2])
        else:
            problem = write_problem(state, me, args.path)
    except Exception as e:  # noqa: BLE001 - refuse, never allow by accident
        problem = cannot_check("%s: %s" % (type(e).__name__, e))
    print(json.dumps({"allow": problem is None, "reason": problem}, ensure_ascii=False))
    return 0


def cannot_check(reason):
    return ("OKe could not check this write (%s): try again; if it keeps failing, report a question"
            % reason)


def children_path(key):
    return os.path.join(pm_dir(key), "children.json") if key else os.path.join(oke_home(), "pms", ".none")


def read_children(key):
    """{agent: record}. Only spawn writes this file, but a PM once rewrote it as a list of records."""
    data = read_json(children_path(key), {}) if key else {}
    if isinstance(data, list):
        data = dict((r["agent"], r) for r in data if isinstance(r, dict) and r.get("agent"))
    return data if isinstance(data, dict) else {}


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
    config = load_config()

    state = load_state()
    git_key = None
    if role == "pm":
        if "job" in header:
            # a pre-slice-3 PM folder held one job's instruction here
            return fail("%s is an old per-job PM assignment; rewrite it as the standing assignment" % path)
        if agent not in state["pms"]:
            return fail("no PM %s: `$OKE pm add %s` first" % (agent, agent))
        key = agent
    else:
        job = state["jobs"].get(header.get("job"))
        if job is None:
            return fail("unknown job %r in assignment" % header.get("job"))
        if job.get("held"):
            return fail("job %s is on hold: start none of its agents" % header["job"])
        if job["phase"] not in ("working", "reviewing", "merging"):
            return fail("plan not approved: job %s is in phase %s" % (header["job"], job["phase"]))
        over = role == "reviewer" and over_rounds(header["job"], job, config, int(header["seq"]))
        if over:
            return fail(over)
        key, git_key = job.get("pm"), job.get("git_key")
        if not key:
            return fail("job %s has no PM" % header["job"])
        if no_path(header["job"], job):
            return fail(no_path(header["job"], job))

    if any(a.get("name") == agent for a in list_agents()):
        return fail("agent name in use: %s" % agent)

    # A report left by an earlier run would satisfy the first `oke wait` at once.
    stale = os.path.join(os.path.dirname(path), "report.md")
    if os.path.exists(stale):
        os.remove(stale)

    pane = args.pane
    if not pane:
        pane = runtime().split(args.split_from, cwd)

    record = {"agent": agent, "pane": pane, "session": None,
              "seq": header["seq"], "role": role, "job": header.get("job")}

    def save_record():
        """Save the record; False when its job went on hold or was cancelled meanwhile:
        that close_children may have run before this record existed."""
        with locked():
            current = load_state()
            if role == "pm":
                if key not in current["pms"]:
                    return False  # removed meanwhile
                current["pms"][key]["pm"] = record
                save_state(current)
                return True
            children = read_children(key)
            children[agent] = record
            write_json_atomic(children_path(key), children)
            now = current["jobs"].get(record["job"]) or {}
            return not now.get("held") and now.get("phase") in ("working", "reviewing", "merging")

    def gone_pm(place):
        """The PM was removed while it started: close its new pane (best effort)."""
        try:
            runtime().close(place)
        except HerdrError:
            return fail("PM %s was removed while it started; close pane %s" % (agent, place))
        return fail("PM %s was removed while it started: closed it" % agent)

    def stopped():
        close_children(record["job"], key)
        return fail("job %s went on hold or was cancelled while %s started: closed it"
                    % (record["job"], agent))

    def trusted_folder():
        """The trust dialog is answered only for a PM's own folder, or a child's job's
        folder (its checkout or worktree), and only when the pane really sits in it."""
        try:
            if role == "pm":
                if real_dir(cwd) != real_dir(pm_dir(key)):
                    return False
                return not args.pane or real_dir(runtime().place_cwd(pane)) == real_dir(pm_dir(key))
            if repo_key(cwd) != git_key:
                return False
            if args.pane:
                return repo_key(runtime().place_cwd(pane)) == git_key
        except (ValueError, HerdrError, TypeError):
            return False
        return True

    cli_args = agent_command(role, config, oke_home(), plugin_root(), os.path.dirname(path), args.resume)
    try:
        prompt = FIRST_PROMPT.format(role=role, path=path)
        held = held_jobs(state, key) if role == "pm" else []
        if held:  # a fresh PM never saw the hold notices of an earlier one
            prompt += (" The user put these jobs on hold: %s. Do not work on them until a message "
                       "says one is back." % ", ".join(held))
        info = runtime().start(agent, pane, cli_args, prompt, trusted_folder, save_record)
    except DialogError as e:
        # The record stays (the user may answer the dialog); a revive closes the old
        # pane only when it knows this terminal.
        record["terminal"] = runtime().place_id(pane)
        if not save_record():
            return stopped() if role != "pm" else gone_pm(pane)
        return fail("agent %s is stopped at a dialog in pane %s:\n%s" % (agent, pane, e))
    except HerdrError as e:
        if e.code == "herdr_unavailable":
            raise
        return fail("spawn of %s failed in pane %s: %s" % (agent, pane, e))
    record["pane"] = info.get("pane_id", pane)
    record["session"] = session_of(info)
    record["terminal"] = runtime().place_id(record["pane"])  # lets a revive close this pane safely
    if not save_record():
        if role == "pm":
            return gone_pm(record["pane"])
        return stopped()
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
    job_dir = os.path.dirname(os.path.dirname(report))
    job = load_state()["jobs"].get(os.path.basename(job_dir), {})
    archived = not job and (normalize_path(os.path.dirname(job_dir))
                            == normalize_path(os.path.join(oke_home(), "jobs")))
    if archived or job.get("cancelled"):
        return "cancelled"  # the user gave the job up: nothing about its children matters
    header = read_header(report)
    current = header is not None and header.get("seq") == seq
    look = None
    if not (current and header.get("type") in FINAL_TYPES):
        look = look_at(agent, os.path.dirname(report))
        if look and look.startswith("blocked"):
            # a report written while we looked beats the blocked read before it (check_pm_once)
            header = read_header(report)
            current = header is not None and header.get("seq") == seq
    if current and header.get("type") in FINAL_TYPES:
        return "report %s" % header["type"]
    if look != "idle":
        return look
    if current and header.get("type") == "waiting":
        return None
    return "?" + (report_problem(report, header) or "idle_no_report")


_sessions = {}


def remember_session(agent, info):
    """Record a child's session id when the runtime shows a new one (it can appear only
    after spawn, or change), so a revive resumes the session it really has."""
    session = session_of(info)
    if not session or _sessions.get(agent) == session:
        return
    found = find_target(load_state(), agent)
    if not found or found[0] not in ("worker", "reviewer"):
        return  # no record yet (spawn still starting): look again next time
    if found[3].get("session") != session:
        update_record(found[0], found[2], found[1], {"session": session})
    _sessions[agent] = session


def look_at(agent, folder):
    """herdr's view of an agent: 'gone', 'blocked\\n<screen>', 'idle' when it has
    nothing to do, '?deliver' when idle with mail, or None while it works."""
    try:
        info = runtime().agent(agent)
    except HerdrError as e:
        if e.code not in ("agent_not_found", "pane_not_found"):
            raise
        try:
            found = find_target(load_state(), agent)
            info = live_agent(found[3], list_agents()) if found else None
        except HerdrError:
            info = None  # the name lookup already said gone; the confirmations decide
        if not info:
            return "gone"
        # the runtime lost its name but its session still runs: judge that agent
        lost = True
    else:
        lost = False
    remember_session(agent, info)
    status = info.get("agent_status")
    if status == "blocked":
        try:
            return "blocked\n" + runtime().screen(agent)
        except HerdrError as e:
            if e.code in ("agent_not_found", "pane_not_found"):
                return "blocked\n(screen unreadable: open its pane)" if lost else "gone"
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
    def reports():
        jobs = []
        for name in sorted(open_jobs(state, key)):
            seq, header, mtime = pm_job_report(name)
            if seq is not None:
                jobs.append((name, seq, header, mtime))
        return jobs, [j for j in jobs if j[2].get("seq") == j[1] and j[2].get("type") in FINAL_TYPES
                      and state["jobs"][j[0]].get("seen") != seen_mark(j[2])]
    jobs, fresh = reports()
    look = None
    if not fresh:
        look = look_at(agent, pm_dir(key))
        if look and look.startswith("blocked"):
            # a report written while we looked beats the blocked read before it: read the
            # reports again
            jobs, fresh = reports()
    if fresh:
        name, seq, header, _ = min(fresh, key=lambda j: j[3] or 0)
        with locked():
            current = load_state()
            if name not in current["jobs"] or current["jobs"][name].get("seen") == seen_mark(header):
                return None  # another wait returned it a moment ago
            current["jobs"][name]["seen"] = seen_mark(header)
            save_state(current)
            if header["type"] in ("done", "failed"):
                end_peer_talk(name)  # the plan (or the job) is settled: nobody waits for answers now
        return "report %s %s" % (header["type"], name)
    if look != "idle":
        return look
    for name, seq, header, _ in jobs:
        if state["jobs"][name].get("sent_seq") != seq:
            continue  # the orchestrator is still handing it over: the message is not sent yet
        if is_silent(state["jobs"][name], seq):
            continue  # the user was told twice; `pending` keeps it in view
        if peers_hold(state, name):
            continue  # it waits for other PMs' answers (oke peer ask): not stopped short
        if not (header.get("seq") == seq and header.get("type") in REPORT_TYPES):
            report = os.path.join(pm_job_dir(name), "report.md")
            problem = report_problem(report, read_header(report))
            if problem:
                word, why = problem.split(" ", 1)
                return "?%s %s %s" % (word, name, why)
            return "?idle_no_report %s" % name
    owed = owed_answers(state, agent)
    if owed:
        # idle with an empty inbox: the question went in, and no answer came
        return "?peer_idle %s" % owed[0][0]
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
    idle = gone = 0
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
        gone = gone + 1 if result == "gone" else 0
        if result == "gone" and gone < GONE_CONFIRMATIONS:
            result = None
        try:
            no_jobs = result == "gone" and jobs_key is not None and not open_jobs(load_state(), jobs_key)
        except (OSError, StateError):
            no_jobs = False  # read again on the next poll, as above
        if no_jobs:
            # A dead PM without open jobs: nobody revives it, and the next PM of this
            # repository must not get its mail (a cancel notice).
            done = os.path.join(inbox_dir(agent), "read")
            os.makedirs(done, exist_ok=True)
            for path, _ in inbox_messages(agent):
                try:
                    os.replace(path, os.path.join(done, os.path.basename(path)))
                except OSError:
                    pass
            return "no_jobs"
        if result is None:
            idle = 0
        elif result.startswith("?"):
            idle += 1
            if idle >= IDLE_CONFIRMATIONS:
                if result.startswith("?peer_idle "):
                    if superseded(agent, token):
                        return "superseded"
                    job = result.split(" ", 1)[1]
                    nudged = peer_nudge(agent, job)
                    if nudged is not False:
                        idle = 0  # reminded: the next look delivers it. None: nothing owed now
                        if poll:
                            time.sleep(poll)
                        continue
                    return "peer_no_answer %s %s" % (job, agent)
                if result != "?deliver":
                    if superseded(agent, token):
                        return "superseded"
                    if jobs_key is not None:
                        return warn_job(result[1:])
                    return result[1:]
                try:
                    sent = deliver(agent)
                    if sent is False:  # the mail stays; the next round tries again
                        failed_deliveries += 1
                    else:
                        failed_deliveries = 0
                except HerdrError as e:
                    sent = None  # a passing outage: the next check_once counts it
                    if e.code != "herdr_unavailable":
                        failed_deliveries += 1  # refused (a lost name): never loop on it
                        try:  # the reason, for whoever reads deliver_failed later
                            log_event("deliver_refused %s: %s" % (agent, (str(e).strip().splitlines() or [""])[0]))
                        except (IOError, OSError):
                            pass
                except OSError:
                    sent = None
                if isinstance(sent, str):
                    # the caller sends it (SendMessage) and starts this wait again
                    return json.dumps({"deliver": {"to": agent, "text": sent}}, ensure_ascii=False)
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
    if args.poll is None:  # a bg look runs a node program: not every 2 s
        args.poll = 2 if runtime().name == "herdr" else 10
    if args.jobs:
        state = load_state()
        keys = [k for k, r in state["pms"].items() if (r.get("pm") or {}).get("agent") == args.agent]
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
GONE_CONFIRMATIONS = 3  # one failed look (a CLI hiccup) is not a death
ORCH_NAME = "oke-orch"
REVIVE_PROMPT = ("You were restarted after a crash. Use the oke:{role} skill. Re-read your "
                 "assignment file {path} and your board.md, then continue.")


def pm_jobs_note(state, key):
    """The job assignments a restarted or compacted PM has to re-read."""
    paths = [os.path.join(pm_job_dir(j), "assignment.md").replace("\\", "/")
             for j in sorted(open_jobs(state, key))]
    paths = [p for p in paths if os.path.exists(p)]
    note = (" Then re-read the assignment of each open job: %s." % ", ".join(paths)) if paths else ""
    held = held_jobs(state, key)
    if held:
        note += (" The user put these jobs on hold: %s. Do not work on them or start their agents "
                 "until a message says one is back." % ", ".join(held))
    return note


def open_jobs(state, key=None):
    """Jobs with work left. A job the user put on hold (`job hold`) is not one: nothing
    revives or watches agents for it until `job unhold`."""
    return [n for n, j in state["jobs"].items()
            if j.get("phase") not in DONE_PHASES and not j.get("held") and (key is None or j.get("pm") == key)]


def held_jobs(state, key=None):
    return sorted(n for n, j in state["jobs"].items()
                  if j.get("held") and j.get("phase") not in DONE_PHASES and (key is None or j.get("pm") == key))


def all_keys(state):
    """Every PM name: those that exist, and those a job still names."""
    return sorted(set(state["pms"]) | set(j.get("pm") for j in state["jobs"].values() if j.get("pm")))


def records(state):
    """Every recorded OKe agent as (role, name, key, record)."""
    out = [("orchestrator", "orchestrator", None, state["orchestrator"])]
    for key in all_keys(state):
        pm = (state["pms"].get(key) or {}).get("pm")
        if pm and pm.get("agent"):
            out.append(("pm", pm["agent"], key, pm))
        for name, rec in sorted(read_children(key).items()):
            out.append((rec.get("role") or "worker", name, key, rec))
    return out


def review_round(job):
    """The review round a job is in: the highest seq among its reviewers' assignments
    (each new review, by a pane or a subagent, is a new seq)."""
    d = os.path.join(oke_home(), "jobs", job)
    seqs = [0]
    for name in os.listdir(d) if os.path.isdir(d) else []:
        if re.search(r"-rv\d*$", name):
            header = read_header(os.path.join(d, name, "assignment.md")) or {}
            if str(header.get("seq", "")).isdigit():
                seqs.append(int(header["seq"]))
    return max(seqs)


def over_rounds(job, info, config, seq):
    """The refusal when review round `seq` is past the job's limit, else None."""
    limit = info.get("review_rounds", config.get("max_review_rounds"))
    if limit is None or seq <= limit:
        return None
    return ("job %s: review round %d is past its limit of %d: report a question with the open "
            "findings; the user may allow more (`$OKE job rounds %s <n>`)" % (job, seq, limit, job))


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
    if not job or job.get("phase") in DONE_PHASES or job.get("held"):
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
            if key in state["pms"]:  # a PM removed meanwhile stays removed
                state["pms"][key].setdefault("pm", {}).update(fields)
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
        parents = [(r.get("pm") or {}).get("pane") for r in state["pms"].values()]
    elif role == "pm":
        # its own place, labelled with its name, as `oke place` gives it: never beside the orchestrator
        return runtime().place_for_repo(cwd, key)
    else:
        parents = [((state["pms"].get(key) or {}).get("pm") or {}).get("pane")]
    return runtime().new_place(parents, cwd)


def relaunch(state, role, key, name, record, folder):
    """Start a dead agent again. It gets the mail queued while it was down with its start,
    under deliver's lock (a wait delivering at the same time would mail it twice); if a
    deliver holds the lock, the mail is left to that deliver."""
    lock = "deliver-" + name
    held = role != "orchestrator" and try_lock(lock)
    try:
        return start_again(state, role, key, name, record, folder, held)
    finally:
        if held:
            unlock(lock)


def start_again(state, role, key, name, record, folder, with_mail):
    mail = []
    if role == "orchestrator":
        agent, prompt, cwd = ORCH_NAME, "/oke:start", native_path(record.get("cwd") or oke_home())
        args = strip_resume(record["argv"])
    else:
        path = os.path.join(folder, "assignment.md").replace("\\", "/")
        cwd = native_path((read_header(path) or {}).get("cwd") or oke_home())
        agent = name
        prompt = REVIVE_PROMPT.format(role=role, path=path)
        if role == "pm":
            prompt += pm_jobs_note(state, key)
        mail = fit_mail(inbox_messages(name)) if with_mail else []
        if mail:
            prompt += "\n\nOKe inbox: " + " | ".join(t for _, t in mail)
        args = agent_command(role, load_config(), oke_home(), plugin_root(), folder, record.get("session"))
    if role == "orchestrator" and record.get("session"):
        args = args + ["--resume", record["session"]]
    # Never start in the old pane: a force-killed claude leaves terminal modes set, and
    # the start command herdr types into that shell arrives garbled (seen live).
    # Close it only while it is still that agent's terminal: after a herdr restart the
    # same pane id can be a user's shell. The user's own orchestrator pane stays.
    old = record.get("pane")
    if old and (role != "orchestrator" or record.get("agent") == ORCH_NAME):
        if record.get("terminal") and runtime().place_id(old) == record["terminal"]:
            try:
                runtime().close(old)
            except HerdrError as e:
                if e.code == "herdr_unavailable":
                    raise  # a worktree group root refuses to close: leave it
    pane = new_pane(state, role, key, cwd)
    # The folder was trusted when the agent first started; a trust dialog now is unexpected.
    try:
        info = runtime().start(agent, pane, args, prompt, lambda: False)
    except (HerdrError, DialogError):
        # herdr keeps the name of a start that failed; with the pane closed it is
        # gone again, so the next watcher round does not read it as alive.
        try:
            runtime().close(pane)
        except HerdrError:
            pass
        raise
    mark_read(agent, mail)
    return info


def revive(name, auto=False):
    """Bring a dead OKe agent back in its own session. Returns revived, alive, busy
    (another process is reviving it), not_needed, failed, unknown, or ask: `auto` (an
    orchestrator or PM resuming it unasked, autonomy full) after an automatic resume of
    it that it never reported since. Only a request revives: nothing in OKe restarts
    a dead agent by itself."""
    state = load_state()
    found = find_target(state, name)
    if found is None:
        return "unknown"
    lock = "revive-" + found[1]
    if not try_lock(lock):
        return "busy"
    try:
        return revive_locked(found[1], auto)
    finally:
        unlock(lock)


def revive_locked(name, auto=False):
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
        revives = 0  # it reported since the last revive (the board shows the count)
    elif auto and record.get("auto_revived"):
        return "ask"  # its last automatic resume did not hold: never twice in a row, the user decides
    if folder:
        try:
            os.remove(os.path.join(folder, "api-retry.json"))  # the dead process's retry
        except OSError:
            pass
    # Counted before the start: a watcher killed during a slow start must not leave
    # the count unchanged and retry forever.
    update_record(role, key, name, {"revives": revives + 1, "report_mtime": mtime, "gave_up": False,
                                    "auto_revived": auto})
    fields = {}
    try:
        info = relaunch(state, role, key, name, record, folder)
    except (HerdrError, DialogError) as e:
        if isinstance(e, HerdrError) and e.code == "herdr_unavailable":
            # herdr was down, the agent did not crash again: do not count this try
            update_record(role, key, name, {"revives": revives, "auto_revived": record.get("auto_revived", False)})
            raise
        log_event("revive_failed %s: %s" % (name, (str(e).strip().splitlines() or [""])[0]))
        return "failed"
    fields.update(pane=info.get("pane_id"), session=session_of(info) or record.get("session"))
    fields["terminal"] = runtime().place_id(fields["pane"]) if fields["pane"] else None
    if role == "orchestrator":
        fields["agent"] = ORCH_NAME
    update_record(role, key, name, fields)
    if role in ("worker", "reviewer") and not should_be_alive(load_state(), role, key, name, record):
        # held or cancelled during the start: its close_children ran before this pane was recorded
        close_children(record.get("job"), key)
        log_event("revive_stopped %s: its job went on hold or ended" % name)
        return "not_needed"
    log_event("revived %s pane %s" % (name, fields["pane"]))
    return "revived"


def cmd_revive(args):
    if args.reset:  # a fresh start of the restart count the board shows
        found = find_target(load_state(), args.agent)
        if found:
            update_record(found[0], found[2], found[1], {"revives": 0, "gave_up": False})
    print(revive(args.agent, args.auto))
    return 0


def cmd_resume_all(args):
    """After a reboot: the orchestrator brings back each PM with work left; each PM
    restarts its own children when it comes back."""
    state = load_state()
    result = dict((name, revive(name)) for role, name, key, rec in records(state) if role == "pm")
    print(json.dumps(result, ensure_ascii=False))
    return 0


def whoami(state, pane):
    """(role, name, key, record) of the OKe agent in `pane`, or None. Pane ids come
    back after a herdr restart, so a recorded session has to match as well."""
    if pane and pane.startswith("session:"):
        # a session id is never reused: no need to ask the runtime who is there
        sid = pane[len("session:"):]
        mine = [r for r in records(state) if r[3].get("pane") == pane or r[3].get("session") == sid]
        return (mine[0] if mine else None), list_agents()
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


def duration_seconds(text):
    """`2h`, `30m`, `90s` or `1h30m` in seconds; None when it is none of these."""
    m = re.match(r"^\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\s*$", str(text or ""), re.I)
    if not m or not any(m.groups()):
        return None
    h, mins, s = (int(g or 0) for g in m.groups())
    return h * 3600 + mins * 60 + s


OVERDUE_FACTOR = 1.5  # a wait past 1.5x its `expect:` is shown to the user; nothing is stopped


def overdue(report_path, header):
    """The `expect:` of a `waiting` report whose wait has run past OVERDUE_FACTOR times it."""
    expect = duration_seconds((header or {}).get("expect"))
    if (header or {}).get("type") != "waiting" or not expect:
        return None
    try:
        waited = time.time() - os.path.getmtime(report_path)
    except OSError:
        return None
    return header["expect"] if waited > OVERDUE_FACTOR * expect else None


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
    # the agent's own task list, written by its OKe mod
    todos = read_json(os.path.join(folder, "todos.json"), []) if folder else []
    todos = [t for t in todos if isinstance(t, dict)] if isinstance(todos, list) else []
    # what the agent's session has cost so far, written by its OKe mod after each turn
    cost = folder_cost(folder) if folder and (os.path.exists(os.path.join(folder, "usage.json"))
                                              or os.path.exists(os.path.join(folder, "usage-prev.json"))) else None
    return {"agent": name, "role": role, "status": a.get("agent_status") if a else "dead",
            "pane": (a or {}).get("pane_id") or record.get("pane"),
            "report": (report or {}).get("type"), "board": board,
            "overdue": overdue(os.path.join(folder, "report.md"), report) if report else None,
            "revives": record.get("revives", 0),
            "cost": cost if isinstance(cost, (int, float)) else None,
            "todos": [{"content": str(t.get("content", "")), "status": str(t.get("status", "pending"))}
                      for t in todos]}


def board_tree(state, agents, only_key=None):
    tree = []
    for key in all_keys(state):
        if only_key and key != only_key:
            continue
        if key not in state["pms"] and not any(j.get("pm") == key and j.get("phase") not in DONE_PHASES
                                               for j in state["jobs"].values()):
            continue  # a removed PM's closed jobs: no line for it
        entry = state["pms"].get(key) or {}
        pm = entry.get("pm")
        children = read_children(key)
        jobs = []
        for job in sorted(n for n, j in state["jobs"].items() if j.get("pm") == key):
            nodes = [board_node(rec.get("role") or "worker", name, key, rec, agents)
                     for name, rec in sorted(children.items()) if rec.get("job") == job]
            info = state["jobs"][job]
            for n in nodes:
                # the hold closed it, like the PM's "off": not hurt. Its cleared pane says so
                # after the unhold too, until the PM revives it
                if n["status"] == "dead" and (info.get("held") or not n["pane"]):
                    n["status"] = "off"
            waiting = pending_item(job, info)
            pm_report = read_header(os.path.join(pm_job_dir(job), "report.md")) or {}
            subagent = pm_report.get("type") == "waiting" and "subagent" in str(pm_report.get("on") or "")
            try:
                merge = merge_state(job)[0]
                after = [o for o in after_jobs(job) if after_block(state, o)] \
                    if info.get("phase") in ("awaiting_merge", "merging") else \
                    start_after(state, job) if gated(info) else []
            except StateError:  # the ledger key is missing or damaged: the board still shows the rest
                merge, after = "unreadable", []
            jobs.append({"job": job, "phase": info.get("phase"), "agents": nodes,
                         "size": info.get("size"), "direct": bool(info.get("direct")), "output": job_output(info),
                         "merge": merge, "held": bool(info.get("held")), "subagent": subagent,
                         "pending": waiting["kind"] if waiting and not waiting["deferred"] else None,
                         # where it works: its worktree, or the path itself
                         "path": native_path((info.get("worktree") or {}).get("path") or info["path"])
                         if info.get("path") else "path?",
                         "peer_wait": sorted(peer_waiting(state, job)),
                         "after_wait": after})
        node = board_node("pm", pm["agent"], key, pm, agents) if pm and pm.get("agent") else None
        if node and node["status"] == "dead" and not open_jobs(state, key):
            node["status"] = "off"  # nothing for it to do (its jobs are done or on hold): not stuck
        tree.append({"name": key, "key": key, "jobs": jobs, "pm": node})
    return tree


TODO_MARKS = {"completed": "v", "in_progress": ">", "pending": "o"}


def board_lines(tree):
    """The board as (depth, text) lines."""
    out = []

    def agent(n, depth):
        out.append((depth, "%s %s: %s%s" % (n["role"], n["agent"], n["status"],
                                            ", report " + n["report"] if n.get("report") else "")
                                         + (", waiting past its expected %s" % n["overdue"] if n.get("overdue") else "")))
        for t in n.get("todos") or []:
            out.append((depth + 1, "%s %s" % (TODO_MARKS.get(t["status"], "o"), t["content"])))

    for repo in tree:
        out.append((0, "PM %s" % repo["name"]))
        if repo["pm"]:
            agent(repo["pm"], 1)
        for job in repo["jobs"]:
            if job["phase"] in DONE_PHASES:
                continue
            out.append((1, "job %s: %s%s%s%s%s" % (
                job["job"], job["phase"], ", on hold" if job.get("held") else "",
                ", waiting for you (%s)" % job["pending"] if job["pending"] else "",
                ", waiting for PM %s" % ", ".join(job["peer_wait"]) if job.get("peer_wait") else "",
                ", waiting for after-job %s" % ", ".join(job["after_wait"]) if job.get("after_wait") else "")))
            out.append((2, "at %s" % (job.get("path") or "path?")))
            for n in job["agents"]:
                agent(n, 2)
    return out or [(0, "no OKe agents yet")]


def cmd_board(args):
    """The board for hosts that cannot draw the mod's pane (SDK hosts)."""
    lines = board_lines(board_tree(load_state(), list_agents()))
    print("\n".join("  " * d + t for d, t in lines))
    return 0


def cmd_tick(args):
    """One watcher round for a mod: who am I, revive what died, new events, the board."""
    state = load_state()
    events, offset = read_events(args.events_from)
    out = {"role": None, "agent": None, "events": events,
           "events_offset": offset, "board": None}
    me, agents = whoami(state, args.pane)
    if me is not None:
        role, name, key, record = me
        out["role"], out["agent"] = role, name
        out["folder"] = agent_folder(role, name, key, record)
        # the orchestrator writes only in OKe's home outside the wikis, and its own memory
        out["home"] = oke_home()
        config = load_config()
        out["wiki"], out["theme"] = config["wiki_path"], config["theme"]
        out["claude_dir"] = os.path.abspath(native_path(claude_config_dir()))
        if role in ("pm", "worker", "reviewer"):
            # what the mod allows without asking `oke path-check`: home (not the wikis), the
            # memory folder, the git write areas; under a direct one it asks
            out["write_areas"] = write_areas(state, role, key, record.get("job"))
            cwd = agent_cwd(me)
            # its cwd's memory folder and, in git, the repository's shared one (all its worktrees use it)
            out["memories"] = [m.replace("\\", "/") for m in memory_dirs(cwd)] if cwd else []
        if role in ("worker", "reviewer"):  # for the status line
            out["job"] = record.get("job")
            out["seq"] = (read_header(os.path.join(out["folder"], "assignment.md")) or {}).get("seq")                 if out["folder"] else None
        if role == "orchestrator":
            targets = [r for r in records(state) if r[0] == "pm"]
        elif role == "pm":
            targets = [r for r in records(state)
                       if (r[0] == "orchestrator" and runtime().owns_orchestrator)
                       or (r[2] == key and r[0] in ("worker", "reviewer"))]
        else:
            targets = []
        # A dead agent is not revived here: the user decides (`oke revive`), as in OMC.
        for t_role, t_name, t_key, t_rec in targets:
            a = live_agent(t_rec, agents)
            if a and not t_rec.get("session") and session_of(a):  # spawn could not see it yet
                update_record(t_role, t_key, t_name, {"session": session_of(a)})
        if args.board and role in ("orchestrator", "pm"):
            out["board"] = board_tree(state, agents, key if role == "pm" else None)
    print(json.dumps(out, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- task boundary

def answered(folder):
    """The assignment seq the report in `folder` answers, or None while it owes one. A
    `waiting` report (a subagent or a wait still runs) has not answered it yet."""
    seq = (read_header(os.path.join(folder, "assignment.md")) or {}).get("seq")
    report = read_header(os.path.join(folder, "report.md")) or {}
    if seq is None or report.get("seq") != seq or report.get("type") not in FINAL_TYPES:
        return None
    return seq


def assignment_goal(folder):
    """The first line of an assignment's body (past its header and headings), shortened."""
    try:
        with io.open(os.path.join(folder, "assignment.md"), encoding="utf-8-sig") as f:
            text = f.read()
    except (IOError, OSError, UnicodeDecodeError):
        return ""
    body = text.split("\n---\n", 1)[1] if "\n---\n" in text else text
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:160]
    return ""


def next_work(role, name, record, open_jobs):
    """What the agent will do after the task it just answered, in words, for the
    compaction at that boundary: its unread mail, a PM's jobs still open."""
    parts = []
    mail = [t for _, t in inbox_messages(name)][:3]
    if mail:
        parts.append("mail waiting: " + " | ".join(t[:300] for t in mail))
    if role == "pm":
        for job, phase, folder in open_jobs:
            goal = assignment_goal(folder)
            parts.append("job %s is open (%s)%s" % (job, phase, ": " + goal if goal else ""))
    elif record.get("job"):
        parts.append("the PM may come back on job %s: %s" % (record["job"], (
            "a new review round of the same assignment" if role == "reviewer"
            else "review findings or a new seq of the same assignment")))
    return "; ".join(parts)


def cmd_boundary(args):
    """The assignments this PM, worker or reviewer has answered, as `job:seq`. A new
    one since the mod last looked is a task boundary, where the mod compacts when the
    context is at least `min_percent` full, keeping what `next` needs."""
    state = load_state()
    me, _ = whoami(state, args.pane)
    out = {"role": None, "answered": [], "next": "", "min_percent": load_config().get("compact_min_percent", 30)}
    if me is not None:
        role, name, key, record = me
        out["role"] = role
        if role == "pm":
            jobs = sorted(n for n, j in state["jobs"].items() if j.get("pm") == key)
            channels = [(job, pm_job_dir(job)) for job in jobs]
        elif role in ("worker", "reviewer") and record.get("job"):
            channels = [(record["job"], agent_folder(role, name, key, record))]
        else:
            channels = []
        open_jobs = []
        for job, folder in channels:
            seq = answered(folder)
            if seq is not None:
                out["answered"].append("%s:%s" % (job, seq))
            info = state["jobs"].get(job, {})
            if info.get("phase") not in (None, "done", "failed") and not info.get("held"):
                # answered and still open: the next step of that job comes back to the PM
                open_jobs.append((job, info["phase"], folder))
        if role in ("pm", "worker", "reviewer"):
            out["next"] = next_work(role, name, record, open_jobs)
    print(json.dumps(out, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- api retry

RETRYABLE_ERRORS = ("rate_limit", "overloaded", "server_error", "unknown", "")
API_RETRY_LIMIT = 20
UNKNOWN_RETRY_LIMIT = 2  # an error with no kind may be permanent: do not resubmit it for an hour


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
    elif kind in RETRYABLE_ERRORS and args.attempt <= (
            min(limit, UNKNOWN_RETRY_LIMIT) if kind in ("unknown", "") else limit):
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
            # a final report the child already wrote stands: the error came after its work
            seq = (read_header(os.path.join(folder, "assignment.md")) or {}).get("seq") if folder else None
            header = (read_header(os.path.join(folder, "report.md")) or {}) if folder else {}
            done = seq is not None and header.get("seq") == seq and header.get("type") in FINAL_TYPES
            folders = [folder] if folder and not done else []
        for f_dir in folders:
            seq = (read_header(os.path.join(f_dir, "assignment.md")) or {}).get("seq")
            report = os.path.join(f_dir, "report.md")
            with io.open(report + ".tmp", "w", encoding="utf-8", newline="\n") as f:  # a wait reads it
                f.write(u"type: failed\nseq: %s\n---\n## Summary\nAPI error %s: automatic retry stopped "
                        u"after %d attempts.\n" % (seq, kind or "unknown", args.attempt))
            replace_file(report + ".tmp", report)
    if marker and os.path.exists(marker):
        os.remove(marker)
    print(json.dumps(out))
    return 0


# ---------------------------------------------------------------- status

def find_agent(record, agents, folder):
    """Find a recorded PM among live ones: by name, then pane, then session.
    A match whose cwd is not the PM's folder is someone else's work."""
    tests = (
        lambda a: record.get("agent") and a.get("name") == record.get("agent"),
        lambda a: record.get("pane") and a.get("pane_id") == record.get("pane"),
        lambda a: record.get("session") and session_of(a) == record.get("session"),
    )
    for test in tests:
        for a in agents:
            if test(a):
                if a.get("cwd") and real_dir(a["cwd"]) == real_dir(folder):
                    return a
                return None
    return None


def cmd_status(args):
    # herdr calls are slow (seconds each): make them before taking the lock, so
    # spawn and the waits do not time out on it. The agent list may then be a few
    # seconds old: an agent that just started can show as dead until the next status.
    if args.claim and not os.environ.get("OKE_RUNTIME"):
        # one runtime per OKe home, chosen by where the orchestrator starts
        want = default_runtime()
        with locked():
            current = load_state()
            if current.get("runtime") != want:
                current["runtime"] = want
                save_state(current)
    if runtime().inside():
        herdr_info = (runtime().missing(), list_agents())
    else:
        # outside the runtime's terminals nothing can be claimed or started, but the
        # state still answers (as `oke board` does); liveness unknown if unreachable
        try:
            herdr_info = ([], list_agents())
        except HerdrError:
            herdr_info = ([], None)
    with locked():
        return status_locked(args, herdr_info)


def own_launch(pane, agents):
    """How the orchestrator in `pane` was started, so a watcher can start it again."""
    found = {}
    # without them the orchestrator just cannot be revived; the claim still stands
    for p in runtime().processes(pane):
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


def status_locked(args, herdr_info):
    state = load_state(recover=True)
    home = oke_home()
    result = {"herdr": {"inside": runtime().inside(), "missing": []},
              "state_recovered": bool(state.get("_recovered")),
              "orchestrator_conflict": None, "pms": {}, "jobs": {}, "orphan_job_dirs": []}
    jobs_dir = os.path.join(home, "jobs")
    if os.path.isdir(jobs_dir):
        result["orphan_job_dirs"] = sorted(d for d in os.listdir(jobs_dir) if d not in state["jobs"])

    result["herdr"]["missing"], agents = herdr_info
    dead = "dead" if agents is not None else "unknown"
    agents = agents or []
    names = set(a.get("name") for a in agents if a.get("name"))
    changed = False

    for key, repo in state["pms"].items():
        record = repo.setdefault("pm", {})
        found = find_agent(record, agents, pm_dir(key)) if record else None
        if found:
            name, pane = found.get("name") or record.get("agent"), found.get("pane_id")
            if (name, pane) != (record.get("agent"), record.get("pane")):
                record["agent"], record["pane"] = name, pane
                changed = True
            # the session id does not exist yet when spawn records a fresh agent
            if session_of(found) and session_of(found) != record.get("session"):
                record["session"] = session_of(found)
                changed = True
        result["pms"][key] = {"pm": "alive" if found else (dead if record.get("agent") else "never"),
                              "agent": record.get("agent") or key, "pane": record.get("pane"),
                              "session": record.get("session"),
                              "pm_dir": pm_dir(key).replace("\\", "/"),
                              "places": repo.get("places") or []}

    for name, job in state["jobs"].items():
        key = job.get("pm")
        children = read_children(key) if key else {}
        job_agents = dict((a, "alive" if a in names else dead)
                          for a, rec in children.items() if rec.get("job") == name)
        if job.get("phase") in DONE_PHASES and (not key or key not in state["pms"]):
            cls = "closed"  # nothing to resume: its PM is gone (or it is an old OKe's job)
        elif job.get("held") and job.get("phase") not in DONE_PHASES:
            cls = "held"
        elif job.get("phase") in ("awaiting_approval", "awaiting_merge"):
            cls = "awaiting_user"
        elif result["pms"].get(key, {}).get("pm") == "alive":
            cls = "alive"
        else:
            cls = "disconnected" if dead == "dead" else "unknown"
        result["jobs"][name] = {"phase": job.get("phase"), "class": cls, "agents": job_agents, "pm": key,
                                "path": job.get("path"),
                                "pm_seq": pm_job_report(name)[0],
                                "pm_job_dir": pm_job_dir(name).replace("\\", "/"),
                                "size": job.get("size"), "direct": bool(job.get("direct")), "output": job_output(job),
                                "held": job.get("held"), "base": job.get("base"),
                                "split_from": job.get("split_from")}

    here = runtime().whoami()
    recorded = state["orchestrator"].get("pane")
    there = [a for a in agents if a.get("pane_id") == recorded]
    orch = state["orchestrator"]
    # pane ids come back after a herdr restart: with a known name or session, the
    # agent now in that pane must be the recorded orchestrator to count as a conflict
    # (herdr may not know a fresh session yet: an agent without one there still counts)
    if recorded and recorded != here and there and (
            not (orch.get("agent") or orch.get("session")) or same_agent(orch, there[0])
            or (not orch.get("agent") and not session_of(there[0]))):
        result["orchestrator_conflict"] = recorded

    code = 0
    if args.claim:
        if result["orchestrator_conflict"]:
            code = fail("another orchestrator is alive in pane %s" % recorded)
        elif here and result["herdr"]["inside"]:
            state["orchestrator"]["pane"] = here
            state["orchestrator"].update(own_launch(here, agents))
            if not any(a.get("pane_id") == here and a.get("name") == ORCH_NAME for a in agents):
                state["orchestrator"].update(revives=0, gave_up=False, agent=None)
            changed = True
    if changed or state.get("_recovered"):
        save_state(state)
    kept = kept_worktrees()
    if kept:
        result["kept_worktrees"] = kept
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


# ---------------------------------------------------------------- compact hook

def compact_message():
    pane = runtime().whoami()
    if not pane:
        return ""
    sid = pane[len("session:"):] if pane.startswith("session:") else None

    def mine(rec):
        # a bg child's place is its short id; its session id is what whoami knows
        return rec.get("pane") == pane or bool(sid and rec.get("session") == sid)

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
    repos = state.get("pms") or {}
    keys = set(repos) | set(j.get("pm") for j in (state.get("jobs") or {}).values() if j.get("pm"))
    for key in sorted(keys):
        if mine((repos.get(key) or {}).get("pm") or {}):
            folder = pm_dir(key)
            note = pm_jobs_note({"jobs": state.get("jobs") or {}}, key)
            break
        for name, rec in read_children(key).items():
            if mine(rec) and rec.get("job"):
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

BRIEF_SECTIONS = ("Workspace", "Approach", "Steps", "Acceptance criteria", "Check command",
                  "Impact elsewhere", "Questions for the user")


def job_brief(job):
    """The plan as the user reads it before approving: the job's absolute path first, then
    the plan's main sections, then the plan file."""
    info = load_state()["jobs"].get(job)
    if info is None:
        raise StateError("no job " + job)
    lines = ["job: %s" % job, "path: %s" % (native_path(info["path"]) if info.get("path") else "path?"), ""]
    for name in BRIEF_SECTIONS:
        text = plan_section(job, name)
        if not text or (name in ("Impact elsewhere", "Questions for the user")
                        and text.lower().startswith("none")):
            continue
        lines += ["## " + name, text, ""]
    lines.append("plan: %s" % native_path(os.path.join(oke_home(), "jobs", job, "plan.md")))
    return "\n".join(lines)


def cmd_job(args):
    if args.action == "brief":
        if args.json:  # the mod: every job awaiting approval, for the approval popup's preview
            jobs = load_state()["jobs"]
            print(json.dumps({"briefs": [{"job": j, "path": native_path(info["path"]) if info.get("path") else "",
                                     "brief": job_brief(j)}
                                    for j, info in sorted(jobs.items())
                                    if info.get("phase") == "awaiting_approval"]}))
            return 0
        if not args.slug:
            return fail("name the job")
        try:
            text = job_brief(args.slug)
        except StateError as e:
            return fail(str(e))
        out = getattr(sys.stdout, "buffer", None)
        if out is not None:
            out.write((text + "\n").encode("utf-8"))
        else:
            print(text)
        return 0
    if args.action == "phase":
        # only the orchestrator moves a job: a PM's report does (seen live: a PM set its own job
        # done and never reported it, so nobody archived it)
        pane = runtime().whoami()
        if pane:
            try:
                me = whoami(load_state(), pane)[0]
            except HerdrError:
                me = None
            if me and me[0] != "orchestrator":
                return fail("only the orchestrator sets a job's phase: write your report (`type: done`) "
                            "and the orchestrator moves the job")
    if args.action == "merge":
        # git can take a while: not under the state lock, but one merge per git key at a time
        key = (load_state()["jobs"].get(args.slug) or {}).get("git_key")
        with locked("merge-" + repo_name(key) if key else "merge", timeout=120):
            return merge_job(args.slug)
    if args.action == "tree":
        info = load_state()["jobs"].get(args.slug)
        if not info:
            return fail("unknown job: %s" % args.slug)
        if no_path(args.slug, info):
            return fail(no_path(args.slug, info))
        tree = pending_tree(native_path(repo_dir(info["git_key"])))
        if not tree:
            return fail("git could not read the main checkout of %s" % args.slug)
        print(json.dumps({"job": args.slug, "tree": tree}))
        return 0
    if args.action == "cancel":
        with locked():
            code = job_locked(args)
        if code:
            return code
        return cancel_children(args.slug, load_state()["jobs"][args.slug].get("pm"))
    if args.action == "hold":
        with locked():
            code = job_locked(args)
        if code:
            return code
        info = load_state()["jobs"][args.slug]
        # closes its agents, drops its queued mail but not the hold notice job_locked queued
        return cancel_children(args.slug, info.get("pm"))
    if args.action == "verify":
        if len([x for x in (args.command, args.accept, args.skip, args.run) if x]) > 1:
            return fail("give at most one of: the Check command, --accept, --skip, --run")
        if args.skip is not None and not args.skip.strip():
            return fail("--skip needs the reason")
        return verify_job(args.slug, args.command, args.accept, args.skip, args.run)  # can take hours
    key = left = None
    if args.action in ("archive", "unhold"):
        key = (load_state()["jobs"].get(args.slug) or {}).get("pm")
    if args.action == "archive":
        open_phase = archive_refusal(args.slug, load_state())
        if open_phase:
            return fail(open_phase)
    if args.action == "archive" and key:
        # a PM can forget to close a finished job's worker (seen live): archive does it,
        # before the folder moves (its next write would bring the folder back) and outside
        # the state lock, like the mail drop. Best effort: the archive happens anyway.
        try:
            _, left, refused = close_children(args.slug, key)
            left = left + refused
        except Exception:  # noqa: BLE001 - cleanup only
            left = None
        drop_job_mail(args.slug, key)
    if args.action == "clean":  # runtime and git, and only an archived job: no state lock
        return clean_job(args.slug)
    with locked():
        code = job_locked(args)
    if code == 0 and getattr(args, "drop_worktree", None):
        # a job ended with no change: nothing in its worktree to keep (`job output --no-change` checked)
        dst, info_was = args.drop_worktree
        wt = info_was["worktree"]["path"]
        # checked again now (nothing may have changed since `--no-change`): clean means only
        # byproducts (caches, logs) are left, which a plain `git worktree remove` would refuse
        clean = os.path.isdir(native_path(wt)) and job_changes(args.slug, info_was) is None
        removed, branch_left = remove_kept_worktree(args.slug, info_was, force=clean)
        if removed:
            info_was["worktree_removed"] = True
            write_json_atomic(os.path.join(dst, "job.json"), info_was)
        else:
            print("kept_worktree: %s (could not remove it: remove it with `$OKE job clean %s` when the "
                  "user says so)" % (wt, args.slug))
        if branch_left:
            print("branch_left: %s" % branch_left)
    if code == 0 and getattr(args, "drop_branch", None):
        # -d only: git keeps a branch that is not merged, or that a worktree still has checked out
        subprocess.call(["git", "-C", native_path(repo_dir(args.drop_branch["git_key"])), "branch", "-q", "-d",
                         "oke/" + args.slug], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if code == 0 and key and args.action == "unhold":
        drop_job_mail(args.slug, key, HOLD_NOTICE.split(":")[0] % args.slug)  # if the PM has not read it
    if code == 0 and left:
        print("not_closed: %s (the runtime did not close them: tell the user to close them)" % ", ".join(left))
    return code


def latest_report(job_dir, pattern):
    """(mtime, path) of the newest report.md in the job's folders matching pattern."""
    found = []
    for name in os.listdir(job_dir):
        path = os.path.join(job_dir, name, "report.md")
        if re.search(pattern, name) and os.path.isfile(path):
            found.append((os.path.getmtime(path), path))
    return max(found) if found else None


def review_verdict(path):
    """The verdict on the first non-empty line after the header's `---`, or None.
    Only that line counts: a finding in the body may quote `verdict: pass`."""
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except (IOError, OSError, UnicodeDecodeError):
        return None
    if "---" not in [l.strip() for l in lines]:
        return None
    body = lines[[l.strip() for l in lines].index("---") + 1:]
    first = next((l.strip() for l in body if l.strip()), "")
    m = re.match(r"^verdict:\s*(pass|changes)\b", first)
    return m.group(1) if m else None


def git_out(cwd, *args):
    """(exit code, stripped stdout) of one git command."""
    p = subprocess.run(["git", "-C", cwd] + list(args), stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL)
    return p.returncode, p.stdout.decode("utf-8", "replace").strip()


def after_jobs(job):
    """The jobs that must be finished before `job` merges (or, for a job that never merges, starts):
    its plan's `after:` and its split's `--after`."""
    header = read_header(os.path.join(oke_home(), "jobs", job, "plan.md")) or {}
    names = [n.strip() for n in str(header.get("after") or "").split(",") if n.strip()]
    split = (load_state()["jobs"].get(job) or {}).get("after")  # `job split --after`
    if split and split not in names:
        names.append(split)
    return [n for n in names if n != job]  # a job never waits for itself


def after_block(state, other):
    """Why `other` still holds back a job that must merge after it, or None."""
    info = state["jobs"].get(other)
    if info is not None:
        if info.get("phase") == "failed":
            return "after-job %s was cancelled" % other
        if never_merges(info):
            # it never merges: done is all it does
            return None if info.get("phase") == "done" else "waiting for %s to be done" % other
        if merge_state(other)[0] != "verified":
            return "waiting for %s to merge" % other
        return None
    archived = sorted(glob.glob(os.path.join(oke_home(), "archive", glob.escape(other) + "-*")))
    archived = [d for d in archived if re.match(re.escape(other) + r"-\d{8}-\d{6}$", os.path.basename(d))]
    if not archived:
        return "after-job %s is no known job" % other
    kept = read_json(os.path.join(archived[-1], "job.json"), {})  # its state entry, kept at the archive
    if isinstance(kept, dict) and never_merges(kept) and kept.get("phase") == "done":
        return None
    if merge_state(other, os.path.join(archived[-1], "ledger.tsv"))[0] == "verified":
        return None
    return "after-job %s was archived without a verified merge" % other


AFTER_FINISHED = "is finished"


GATE_PHASES = ("intake", "planning", "awaiting_approval")  # a job held at its start is in one


def gated(job):
    """True for a job the start gate refused that has not started since."""
    return bool(job.get("start_after")) and job.get("phase") in GATE_PHASES


def after_blocks(state, job):
    """(waits, broken) for `job`'s after-jobs: [(after-job, reason)] of those it still plainly
    waits for, and of those that can never clear by themselves (cancelled, unknown,
    archived without a verified merge), which the user decides."""
    waits, broken = [], []
    for other in after_jobs(job):
        why = after_block(state, other)
        if why and why.startswith("waiting for ") and waits_on(other, job):
            why = "after-job %s itself waits for %s (a cycle)" % (other, job)
        if why:
            (waits if why.startswith("waiting for ") else broken).append((other, why))
    return waits, broken


def waits_on(job, target):
    """Does `job` wait, through its after-jobs and theirs, for `target`?"""
    seen, todo = set(), [job]
    while todo:
        cur = todo.pop()
        for other in after_jobs(cur):
            if other == target:
                return True
            if other not in seen:
                seen.add(other)
                todo.append(other)
    return False


def start_after(state, job):
    """The after-jobs a folder job still waits for at its start: [] when none."""
    info = state["jobs"].get(job) or {}
    if not never_merges(info) or info.get("size") == "research":
        return []
    return [o for o, _ in after_blocks(state, job)[0] + after_blocks(state, job)[1]]


def tell_after_waiters(state, job, news, phases=None):
    """Queue `news` about `job` to the PM of every open job whose plan says `after: <job>`
    (only jobs in `phases`, when given). A job held at its start gate needs no mail: its
    `after_ready` item shows up in `oke pending` once nothing holds it."""
    for other in sorted(open_jobs(state)):
        if phases and state["jobs"][other].get("phase") not in phases:
            continue
        if other != job and job in after_jobs(other):
            pm = (state["pms"].get(state["jobs"][other].get("pm")) or {}).get("pm") or {}
            if pm.get("agent"):
                queue_mail(pm["agent"], "oke", "Job %s: after-job %s %s" % (other, job, news))


def quality_block(job, review_asg, target):
    """Why the review or the check does not count for the current plan, or None. The review
    must name the current criteria_rev; OKe's last `oke check` row for the reviewed commit
    (or tree:<sha>) must be a pass under it."""
    rev = criteria_rev(job)
    if review_asg.get("criteria_rev") != rev:
        return (u"the criteria or the Check command of %s changed since the review (or the review assignment "
                u"names no criteria_rev): give the reviewer a new instruction with `criteria_rev: %s`"
                % (job, rev))
    rows = [r for r in ledger_rows(job) if r["check"] == "check" and r["commit"] == target]
    if not rows or rows[-1]["result"] != "pass" or rows[-1]["detail"].split()[:1] != [rev]:
        return "no passing `oke check` on %s with the current criteria: run `$OKE check %s`" % (target, job)
    return None


def merge_job(job):
    """Merge oke/<job> into the main checkout, only if the user approved the merge and
    the last review passed on the very commit the branch points at. A worker that
    reported again (a rebase, a fix) needs a new review first (seen live: a PM skipped it).
    The main checkout must be clean, on the job's base branch and not mid-merge: the
    abort after a conflict must only ever undo OKe's own merge."""
    state = load_state()
    info = state["jobs"].get(job)
    if info and info.get("held"):
        return fail("job %s is on hold: no merge until it is back" % job)
    if not info:
        return fail("unknown job: %s" % job)
    if no_path(job, info) and info.get("size") != "research":
        return fail(no_path(job, info))
    if never_merges(info):
        return fail("job %s: %s" % (job, NO_MERGE))
    if info.get("phase") != "merging":
        return fail("job %s is in phase %s: set phase merging after the user approves the merge"
                    % (job, info.get("phase")))
    waits, broken = after_blocks(state, job)
    if broken:
        return fail("job %s: %s: the orchestrator asks the user; keep the job waiting" % (job, broken[0][1]))
    if waits:
        return fail("job %s: %s" % (job, waits[0][1]))
    already = merge_state(job)[0]
    if already in ("unverified", "verified"):
        return fail("job %s is already merged (%s): run `$OKE job verify %s`, not a second merge"
                    % (job, already, job))
    # every job of this checkout, a failed or cancelled one too: its merge is still in main
    for other in [n for n, j in state["jobs"].items() if j.get("git_key") == info["git_key"]]:
        other_state, row = merge_state(other)
        if other != job and other_state == "unverified":
            return fail("the merge of %s is not verified yet: run `$OKE job verify %s` first"
                        % (other, other))
        if other != job and other_state == "rolled_back" and "already-in" in row["detail"].split():
            # its failing code is still in main (nothing could be undone): a check here would fail on it
            return fail("main still holds %s's code, whose check failed: no other merge until %s merges "
                        "again or the user decides" % (other, other))
    job_dir = os.path.join(oke_home(), "jobs", job)
    worker = latest_report(job_dir, r"-w\d+$")
    review = latest_report(job_dir, r"-rv\d*$")
    if not review:
        return fail("job %s has no review report: have the reviewer review the branch first" % job)
    if review_verdict(review[1]) != "pass":
        return fail("the last review of %s did not pass: %s" % (job, review[1].replace("\\", "/")))
    if worker and worker[0] > review[0]:
        return fail("the worker reported after the last review (a rebase or a fix): give the reviewer "
                    "a new instruction to review it, then merge")
    main = native_path(repo_dir(info["git_key"]))
    # The review counts only for the commit its assignment named, and only once the
    # report answers that assignment: commits added after it were never reviewed.
    review_asg = read_header(os.path.join(os.path.dirname(review[1]), "assignment.md")) or {}
    review_seq = (read_header(review[1]) or {}).get("seq")
    if review_seq != review_asg.get("seq"):
        return fail("the review report of %s does not answer its current assignment (seq %s): wait for "
                    "the reviewer" % (job, review_asg.get("seq")))
    code, tip = git_out(main, "rev-parse", "-q", "--verify", "refs/heads/oke/%s^{commit}" % job)
    if code != 0 and review_asg.get("tree"):
        # no worktree: the review covered the uncommitted changes in the main checkout
        why = quality_block(job, review_asg, "tree:" + review_asg["tree"])
        if why:
            return fail(why)
        return commit_direct(job, info, main, review[1], review_asg["tree"])
    reviewed = review_asg.get("commit")
    if code != 0:
        return fail("branch oke/%s does not exist" % job)
    if not reviewed or not re.match(r"^[0-9a-f]{7,40}$", reviewed):
        # a branch name would resolve to the moving tip and pass any check
        return fail("the review assignment of %s names no commit sha in `commit:`: give the reviewer a new "
                    "instruction with `commit: %s`, then merge" % (job, tip))
    code, reviewed_full = git_out(main, "rev-parse", "-q", "--verify", reviewed + "^{commit}")
    if code != 0 or reviewed_full != tip:
        return fail("oke/%s is at %s but the review covered %s: give the reviewer a new instruction "
                    "with `commit: %s`, then merge" % (job, tip[:12], reviewed, tip))
    why = quality_block(job, review_asg, reviewed_full)
    if why:
        return fail(why)
    if git_out(main, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0:
        return fail("a merge is already in progress in %s: leave it to the user" % main)
    code, dirty = main_changes(main)
    if code != 0 or dirty:
        return fail("the main checkout %s is not clean:\n%s" % (main, dirty))
    base = info.get("base")
    current = git_out(main, "symbolic-ref", "-q", "--short", "HEAD")[1]
    if not current or (base and current != base):
        return fail("the main checkout is on %r, not on the job's base branch %r: ask the user"
                    % (current or "a detached HEAD", base))
    if left_merging(job):
        return 1
    pre = git_out(main, "rev-parse", "HEAD")[1]
    if git_out(main, "merge-base", "--is-ancestor", tip, "HEAD")[0] == 0:
        # already in main (the change was there, or a merge whose record was lost): git would
        # only say "Already up to date". Recorded as merged at HEAD, so verify and done work.
        ledger_add(job, os.path.basename(os.path.dirname(review[1])), tip, "review", "pass",
                   review[1].replace("\\", "/"))
        ledger_add(job, "oke", pre, "merge", "ok", "pre=%s already-in" % pre)
        print(json.dumps({"result": "merged", "job": job, "commit": pre, "already_in": True}))
        return 0
    result = subprocess.run(["git", "-C", main, "merge", "--no-ff", "--no-edit",
                             "-m", "Merge branch 'oke/%s'" % job, tip],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode == 0:
        merged = git_out(main, "rev-parse", "HEAD")[1]
        ledger_add(job, os.path.basename(os.path.dirname(review[1])), tip, "review", "pass",
                   review[1].replace("\\", "/"))
        ledger_add(job, "oke", merged, "merge", "ok", "pre=%s" % pre)
        print(json.dumps({"result": "merged", "job": job, "commit": merged}))
        return 0
    files = git_out(main, "diff", "--name-only", "--diff-filter=U")[1].split()
    if git_out(main, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0:
        if subprocess.call(["git", "-C", main, "merge", "--abort"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) != 0:
            return fail("git merge of oke/%s conflicted and `git merge --abort` failed: %s is "
                        "mid-merge, ask the user" % (job, main))
    if not files:
        return fail("git merge failed: %s" % result.stdout.decode("utf-8", "replace").strip())
    print(json.dumps({"result": "conflict", "job": job, "files": files}))
    return 2


# An untracked byproduct line of `git status --porcelain`: Python caches only.
BYPRODUCT = re.compile(r"^\?\? (.*/)?(__pycache__/|\.pytest_cache/|[^/]*\.py[co])$")

# Tracked changes always go in; a new file only when the PM staged it (`git add`): only
# the PM can tell the job's new files from what its checks left (seen live: `add -A`
# committed __pycache__).
ADD = ["add", "-u"]


def byproduct(line):
    """An untracked line of `git status --porcelain` that never blocks a merge: Python
    caches, or a folder config `merge_ignore_untracked` names (another tool's state)."""
    if not line.startswith("?? "):
        return False
    path = line[3:].strip('"')
    return bool(BYPRODUCT.match(line)) or any(
        path == p or path.startswith(p.rstrip("/") + "/") for p in load_config().get("merge_ignore_untracked") or [])


def main_changes(main):
    """(code, porcelain lines) of the main checkout, untracked byproducts left out: every
    verify run leaves Python caches (seen live), and they never block a merge."""
    code, dirty = git_out(main, "status", "--porcelain")
    return code, "\n".join(l for l in dirty.splitlines() if not byproduct(l))


def pending_tree(main):
    """Tree sha of the main checkout as `oke job merge` would commit it: what the PM
    staged plus every tracked change. The real index is untouched. The review of a job without a worktree names this tree."""
    idx = os.path.join(oke_home(), "tree-%d.index" % os.getpid())
    env = dict(os.environ, GIT_INDEX_FILE=idx)
    code, real = git_out(main, "rev-parse", "--git-path", "index")
    if code != 0:
        return None
    real = os.path.join(main, real)
    try:
        steps = [ADD, ["write-tree"]]
        if os.path.exists(real):
            shutil.copyfile(real, idx)  # what the PM staged
        else:
            steps.insert(0, ["read-tree", "HEAD"])
        for argv in steps:
            result = subprocess.run(["git", "-C", main] + argv, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if result.returncode != 0:
                return None
        return result.stdout.decode().strip()
    finally:
        try:
            os.remove(idx)
        except OSError:
            pass


def left_merging(job):
    """A cancel since merge_job's first checks: merge nothing."""
    with locked():
        if load_state()["jobs"].get(job, {}).get("phase") == "merging":
            return False
    fail("job %s left phase merging (cancelled?): not merged" % job)
    return True


def commit_direct(job, info, main, review, reviewed):
    """A job without a worktree: commit its reviewed, uncommitted changes in the main
    checkout and record that commit as the job's merge, so it is verified like one.
    Only the exact tree the review named: anything changed or added since is refused."""
    if git_out(main, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0:
        return fail("a merge is already in progress in %s: leave it to the user" % main)
    base = info.get("base")
    current = git_out(main, "symbolic-ref", "-q", "--short", "HEAD")[1]
    if not current or (base and current != base):
        return fail("the main checkout is on %r, not on the job's base branch %r: ask the user"
                    % (current or "a detached HEAD", base))
    code, dirty = git_out(main, "status", "--porcelain", "-uno")  # untracked files are never committed
    if code == 0 and not dirty and git_out(main, "rev-parse", "HEAD^{tree}")[1] == reviewed:
        # the review passed on main as it is (the change was already there): recorded like
        # merge_job's already-in, so verify and done work
        new = [l[3:] for l in git_out(main, "status", "--porcelain")[1].splitlines()
               if l.startswith("?? ") and not byproduct(l)]
        if new:  # the job's new files, unstaged, would never be committed
            return fail("untracked files %s: if they are the job's, `git add` them, get a new tree "
                        "review, then merge" % ", ".join(new))
        if left_merging(job):
            return 1
        pre = git_out(main, "rev-parse", "HEAD")[1]
        ledger_add(job, os.path.basename(os.path.dirname(review)), pre, "review", "pass",
                   review.replace("\\", "/"))
        ledger_add(job, "oke", pre, "merge", "ok", "pre=%s direct already-in" % pre)
        print(json.dumps({"result": "merged", "job": job, "commit": pre, "direct": True, "already_in": True}))
        return 0
    if code != 0 or not dirty:
        return fail("job %s has no branch oke/%s and no changes in %s to commit" % (job, job, main))
    tree = pending_tree(main)
    if tree != reviewed:
        return fail("the changes in %s are not the ones the review covered (tree %s, reviewed %s): give "
                    "the reviewer a new instruction with `tree: %s`, then merge" % (main, tree, reviewed, tree))
    if left_merging(job):
        return 1
    pre = git_out(main, "rev-parse", "HEAD")[1]
    for argv in (ADD, ["commit", "-q", "-m", "oke: %s" % job]):
        result = subprocess.run(["git", "-C", main] + argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            subprocess.call(["git", "-C", main, "reset", "-q"])  # unstage; the files stay as they are
            return fail("git %s failed: %s" % (argv[0], result.stdout.decode("utf-8", "replace").strip()))
    if git_out(main, "rev-parse", "HEAD^{tree}")[1] != reviewed:  # changed between the check and the commit
        subprocess.call(["git", "-C", main, "reset", "-q", "--soft", pre])
        subprocess.call(["git", "-C", main, "reset", "-q"])
        return fail("the changes in %s moved during the commit: run `$OKE job merge %s` again" % (main, job))
    merged = git_out(main, "rev-parse", "HEAD")[1]
    ledger_add(job, os.path.basename(os.path.dirname(review)), merged, "review", "pass",
               review.replace("\\", "/"))
    ledger_add(job, "oke", merged, "merge", "ok", "pre=%s direct" % pre)
    left = git_out(main, "ls-files", "--others", "--exclude-standard")[1].splitlines()
    print(json.dumps({"result": "merged", "job": job, "commit": merged, "direct": True,
                      "left_untracked": [f for f in left if f]}))
    return 0


# ---------------------------------------------------------------- ledger

LEDGER_FIELDS = ("time", "by", "commit", "check", "result", "detail")


def ledger_path(job):
    return os.path.join(oke_home(), "jobs", job, "ledger.tsv")


# Every row carries an HMAC of the job, the row before it and itself under a key only OKe
# reads: a row an agent appended by hand, or copied from elsewhere, is not a row at all.
# shortcut: the agents run as the same OS user, so an agent bent on it can still find and
# read the key or import oke.py and call ledger_add (deny rules and the mod refuse the
# obvious ways); putting back an older head with the rows cut to it also goes unseen.
# OS users would close both.
LEDGER_KEY = "ledger.key"


KEY_HEADS = ".heads"  # ledger.key.heads: every ledger has its signed head (agents are denied it)


def read_key(path, marked=False):
    """The key; with marked, (key, whether the heads are written)."""
    try:
        with io.open(path, encoding="ascii") as f:
            text = f.read().strip()
    except (IOError, OSError, UnicodeDecodeError):
        text = ""
    key = binascii.unhexlify(text) if re.match(r"^[0-9a-f]{64}$", text) else None
    return (key, os.path.exists(path + KEY_HEADS)) if marked else key


def write_key(path, key):
    """The key file (hex only, as every OKe version reads it) and its heads mark."""
    write_private(path + ".tmp", binascii.hexlify(key).decode("ascii"))
    replace_file(path + ".tmp", path)
    final = path[:-len(".new")] if path.endswith(".new") else path
    write_private(final + KEY_HEADS, "every ledger has its head\n")


def add_heads(key):
    """Heads for ledgers signed before heads existed: once, as the rows stand."""
    for job, ledger in all_ledgers():
        if os.path.exists(head_path(ledger)):
            continue
        try:
            with io.open(ledger, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except (IOError, OSError, UnicodeDecodeError):
            continue
        sigs = chain_sigs(key, job, lines)[0]
        write_head(key, job, ledger, len(sigs), sigs[-1] if sigs else "")


def ledger_key():
    path = os.path.join(oke_home(), LEDGER_KEY)
    key, heads = read_key(path, marked=True)
    if key and heads:
        return key
    with locked("ledger-key", timeout=120):
        key, heads = read_key(path, marked=True)
        if key and not heads:  # the key the agents cannot change says the heads are not written yet
            add_heads(key)
            write_key(path, key)
        if key:
            return key
        if os.path.exists(path):
            raise StateError("the ledger key %s is damaged: ask the user" % path)
        new = path + ".new"  # a first signing that stopped part way resumes with its key
        key = read_key(new)
        if not key:
            if signed_ledgers():
                # a new key would sign whatever was appended meanwhile: never by itself
                raise StateError("the ledger key %s is missing while signed ledgers exist: ask the user. "
                                 "Restore the key file; or, to trust every ledger line as it stands now "
                                 "(hand-written ones too), the user writes a new key to %s.new: "
                                 "python -c \"import os;open(r'%s.new','w').write(os.urandom(32).hex())\""
                                 % (path, path, path))
            if not os.path.isdir(oke_home()):
                os.makedirs(oke_home(), exist_ok=True)
            write_private(new, binascii.hexlify(os.urandom(32)).decode("ascii"))
            key = read_key(new)
        sign_ledgers(key)
        write_key(new, key)  # marked: sign_ledgers wrote every head
        replace_file(new, path)  # last: until then every reader waits on the lock above
    return key


def all_ledgers():
    """[(job, ledger path)] of open and archived jobs (`archive/<job>-YYYYmmdd-HHMMSS`)."""
    out = [(os.path.basename(os.path.dirname(l)), l)
           for l in glob.glob(os.path.join(oke_home(), "jobs", "*", "ledger.tsv"))]
    for l in glob.glob(os.path.join(oke_home(), "archive", "*", "ledger.tsv")):
        out.append((re.sub(r"-\d{8}-\d{6}$", "", os.path.basename(os.path.dirname(l))), l))
    return out


def signed_ledgers():
    for _, ledger in all_ledgers():
        try:
            with io.open(ledger, encoding="utf-8", errors="replace") as f:
                if any(l.count("\t") == 6 for l in f.read().splitlines()):
                    return True
        except (IOError, OSError):
            pass
    return False


def sign_ledgers(key):
    """Sign every ledger as it stands: the rows from before signing, and again (same result)
    the rows a stopped run already signed with this key."""
    for job, ledger in all_ledgers():
        try:
            with io.open(ledger, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (IOError, OSError, UnicodeDecodeError):
            continue
        prev, text = "", u""
        for l in lines:
            line = l.rpartition("\t")[0] if l.count("\t") == 6 else l
            if line.count("\t") != 5:
                continue  # a torn line
            prev = row_sig(key, job, prev, line)
            text += line + u"\t" + prev + u"\n"
        with io.open(ledger + ".tmp", "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        replace_file(ledger + ".tmp", ledger)
        write_head(key, job, ledger, text.count(u"\n"), prev)


def row_sig(key, job, prev, line):
    return hmac.new(key, (u"%s\n%s\n%s" % (job, prev, line)).encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def head_path(ledger):
    return os.path.join(os.path.dirname(ledger), "ledger.head")


def head_sig(key, job, n, last):
    return row_sig(key, job, "head", "%d\t%s" % (n, last))


def write_head(key, job, ledger, n, last):
    """The number of rows and the last signature, signed: rows cut from the end show."""
    path = head_path(ledger)
    with io.open(path + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        f.write(u"%d\t%s\t%s\n" % (n, last, head_sig(key, job, n, last)))
    replace_file(path + ".tmp", path)


def chain_sigs(key, job, lines):
    """(signatures, rows, untrusted line count) of the lines that chain from the start."""
    prev, rows, sigs, bad = "", [], [], 0
    for l in lines:
        line, _, sig = l.rpartition("\t")
        if line.count("\t") == 5 and hmac.compare_digest(sig.encode("utf-8"), row_sig(key, job, prev, line).encode("ascii")):
            rows.append(dict(zip(LEDGER_FIELDS, line.split("\t"))))
            sigs.append(sig)
            prev = sig
        elif l.strip():
            bad += 1
    return sigs, rows, bad


def ledger_chain(job, path=None):
    """(trusted rows, number of lines that were not), and the last trusted row's signature.
    Raises StateError when rows were cut from the end (the signed head counts more)."""
    key = ledger_key()  # first: making it re-signs the ledgers written before signing
    ledger = path or ledger_path(job)
    # the head before the rows: a writer puts its row down first, so this head never counts more
    head = n = last = None
    for attempt in range(20):
        try:
            with io.open(head_path(ledger), encoding="utf-8") as f:
                n, last, mac = f.read().strip().split("\t")
            n = int(n)
            head = hmac.compare_digest(mac.encode("utf-8"), head_sig(key, job, n, last).encode("ascii"))
            break
        except (IOError, OSError) as e:
            if e.errno == errno.ENOENT:
                break  # no head
            if attempt == 19:  # Windows: a writer renaming over it holds it for a moment
                raise StateError("cannot read %s: %s" % (head_path(ledger), e))
            time.sleep(0.05)
        except (UnicodeDecodeError, ValueError):
            head = False  # not a head OKe wrote
            break
    lines = None
    for attempt in range(20):
        try:
            with io.open(ledger, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            break
        except (IOError, OSError) as e:
            if e.errno == errno.ENOENT:
                break  # no ledger yet
            if attempt == 19:
                raise StateError("cannot read %s: %s" % (ledger, e))
            time.sleep(0.05)
    sigs, rows, bad = chain_sigs(key, job, lines or [])
    prev = sigs[-1] if sigs else ""
    # a crash between a row and its head leaves one row more than the head: fine. Fewer, a
    # head that is not OKe's, or a lost head over more than the one row it lags: rows were cut
    lost = (head is False or (head and (len(sigs) < n or (n and sigs[n - 1] != last)))
            or (head is None and len(sigs) > 1))
    if lost:
        raise StateError("the ledger of job %s lost rows or its head (%s): ask the user. If an older "
                         "OKe wrote this job (it kept no heads), deleting %s makes OKe write the "
                         "missing heads once, as the rows stand" % (job, head_path(ledger),
                                                                  os.path.join(oke_home(), LEDGER_KEY + KEY_HEADS)))
    if lines is None:
        return [], 0, ""
    return rows, bad, prev


def ledger_add(job, by, commit, check, result, detail=""):
    """Append one row. Rows are never rewritten: a later row supersedes an earlier one."""
    values = [time.strftime("%Y-%m-%dT%H:%M:%S"), by, commit, check, result, detail]
    line = "\t".join(" ".join(str(v or "-").split()) for v in values)
    path = ledger_path(job)
    with locked("ledger-" + job, timeout=150):  # the chain needs the last row; the key lock waits 120 s
        key = ledger_key()  # before the file is open: making the key re-signs older ledgers
        rows, _, prev = ledger_chain(job)
        sig = row_sig(key, job, prev, line)
        try:
            with io.open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                torn = f.read(1) != b"\n"  # a writer died mid-line: do not glue this row onto it
        except (IOError, OSError):
            torn = False
        with io.open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write((u"\n" if torn else u"") + line + u"\t" + sig + u"\n")
        write_head(key, job, path, len(rows) + 1, sig)


def ledger_rows(job, path=None):
    """The rows OKe wrote, in order; lines without a valid signature are left out."""
    return ledger_chain(job, path)[0]


# Rows only OKe itself writes; `oke ledger add` refuses them so nobody forges a verdict.
OKE_CHECKS = ("review", "merge", "verify", "rollback", "check")


def merge_state(job, path=None):
    """(state, last merge row): state is none, unverified, verified or rolled_back.
    `path`: a ledger.tsv elsewhere (an archived job's)."""
    rows = ledger_rows(job, path)
    merges = [i for i, r in enumerate(rows) if r["check"] == "merge"]
    if not merges:
        return "none", None
    after = rows[merges[-1] + 1:]
    if any(r["check"] == "rollback" for r in after):
        return "rolled_back", rows[merges[-1]]
    if any(r["check"] == "verify" and r["result"] == "pass" for r in after):
        return "verified", rows[merges[-1]]
    return "unverified", rows[merges[-1]]


def find_bash():
    """bash for the plan's Check command. On Windows only Git Bash: the `bash` on PATH may be
    the WSL launcher (System32, WindowsApps), which has no Windows python or tools."""
    if os.name != "nt":
        return shutil.which("bash")
    code, path = git_out(oke_home(), "--exec-path")  # <git>/mingw64/libexec/git-core
    if code == 0:
        bash = os.path.normpath(os.path.join(native_path(path), "..", "..", "..", "bin", "bash.exe"))
        if os.path.isfile(bash):
            return bash
    bash = shutil.which("bash")
    if bash and not re.search(r"system32|windowsapps", bash, re.I):
        return bash
    return None


def run_check(command, cwd, log):
    """Exit code of `command` in bash (else the platform shell), output to `log`. No time limit."""
    bash = find_bash()
    argv = [bash, "-c", command] if bash else command
    with io.open(log, "wb") as f:
        return subprocess.call(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=f,
                               stderr=subprocess.STDOUT, shell=bash is None)


# Failure signatures: a port of oh-my-claudecode's feedback baseline (signatureLines,
# diffAgainstBaseline). Lines of a failing run minus known noise; new lines are new failures.
MAX_SIGNATURES = 200
FAILURE_WORD = re.compile(u"\\bfail(?:ed|ure|ures|ing)?\\b|\\w*(?:Error|Exception)s?\\b|✗|×", re.I)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
VOLATILE = [  # in this order, each part becomes <v>
    (re.compile(r"(\bin \d+(?:\.\d+)?s\s)\(\d+:\d{2}(?::\d{2})?(?:\.\d+)?\)"), r"\1<v>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds)\b", re.I), "<v>"),
    (re.compile(r"\bduration(?:_ms)?\s*:?\s*\d+(?:\.\d+)?\b", re.I), "<v>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"), "<v>"),
    (re.compile(r"[A-Za-z]:\\[^\s]*\\Temp\\[^\s]*", re.I), "<v>"),
    (re.compile(r"/tmp/\S*"), "<v>"),
    (re.compile(r"(?<=\(node:)\d+(?=\) (?:\[[^\]]+\] )?\w+:)"), "<v>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<v>"),
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<v>"),
]
# shortcut: macOS /var/folders temp paths are not normalized; port OMC's normalizer for a macOS user
NOISE = [re.compile(p, re.I) for p in (
    r"^(?:npm|yarn|pnpm)\s+warn\b", r"^>\s+\S+@\S+",
    r"^>\s+(?:tsc|vite|vitest|jest|node|npm|yarn|pnpm|eslint|tsx|ts-node)\b",
    r"^(?:RUN\s+v?\d|Duration\s)", r"^\d+\|\s", r"^\|\s*\^", u"^(?:✓|√)",
    r"^[.sxX]+(?:\s*\[\s*\d+%\]\s*)?$",
    r"^\d+ passed(?:, \d+ (?:warnings?|skipped|xfailed|xpassed|deselected))* in <v>(?: <v>)*(?: \([^)]*\))?$",
    r"^Start at \d{2}:\d{2}:\d{2}$", r"^#\s*(?:pass|tests?|skipped|cancelled|todo)\s+\d+\s*$",
    r"^#\s*fail\s+0\s*$", r"^#\s*<v>\s*(<v>\s*)*$", r"^\d+\.\.\d+\s*$")]
NOISE_UNLESS_FAILURE = [re.compile(p, re.I) for p in (
    r"^(?:\[[^\]]*\]\s*)*(?:<v>\s+)?(?:TRACE|DEBUG|INFO|NOTICE)\b", r"^\[(?:TRACE|DEBUG|INFO|NOTICE)\]",
    r"^(?:Test Files|Tests)\b", r"^(running|collecting|compiling|building|passing|tap\s+version|%|\s*at\s)")]
PASSING_TAP = re.compile(r"^ok\s+\d+\s+", re.I)
# Numbers that move when a job adds code or tests, not when a failure changes: source line numbers
# and summary counts. Each failing test still has its own line, so a new failure stays visible.
COUNTS = [
    (re.compile(r"\bline \d+"), "line <n>"),
    (re.compile(r"(\.[A-Za-z]\w*):\d+(?::\d+)?\b"), r"\1:<n>"),
    (re.compile(r"\b\d+(?= (?:tests?|passed|failed|errors?|failures?|skipped|warnings?|deselected"
                r"|xfailed|xpassed|todo|pending|total|items?|passing|failing)\b)", re.I), "<n>"),
    (re.compile(r"\b((?:failures|errors|skipped|expected failures|unexpected successes)=)\d+", re.I), r"\1<n>"),
    (re.compile(r"^(Tests?\b.*)\(\d+\)$"), r"\1(<n>)"),  # vitest "Tests  1 failed | 5 passed (6)"
    # TAP numbers move when tests are added; an unnamed `not ok 3` keeps its number to stay apart
    (re.compile(r"^((?:not )?ok) \d+(?=\s+(?:-\s*)?[^\s-])", re.I), r"\1 <n>"),
    (re.compile(r"^(>?\s*)\d+(\s*\|)"), r"\1<n>\2"),  # a code frame kept for its failure word
    # pytest -v progress percentages and xdist worker ids move with added tests and scheduling
    (re.compile(r"\s*\[\s*\d+%\]\s*$"), ""),
    (re.compile(r"^\[gw\d+\]\s*(?:\[\s*\d+%\]\s*)?"), ""),
    (re.compile(r"\b(gw\d+) \[\d+\]"), r"\1 [<n>]"),
]
# shortcut: pytest --tb=line with -rN prints no per-test line, so two failures with the same message in
# one file share a signature; the default short summary (FAILED file::test) keeps them apart.
# the file part has no ":" or " - ", so a failure line (`FAILED a.py::t - ... x.py .`) never matches
PROGRESS = re.compile(r"^(?:(?:(?! - )[^:])+\.py\s+)?[.sxXFEi]+\s*(?:\[\s*\d+%\]|\d+/\d+)?$")  # failures are also listed by name
CODE_FRAME = re.compile(r"^>?\s*\d+\s*\|")  # jest/babel source excerpts: line numbers move
# lines a passing test prints, which an added test adds: cargo/unittest -v, mocha, jest, go test
PASSING = re.compile(u"\\.\\.\\. (?:ok$|skipped\\b)|^✔|^PASS\\s|^ok\\s+\\S+\\s+(?:<v>|\\(cached\\))"
                     u"|^--- (?:PASS|SKIP):|^=== (?:RUN|PAUSE|CONT|NAME)\\b|^\\?\\s+\\S+\\s+\\[no test files\\]"
                     u"|^(?:\\[gw\\d+\\]\\s*)?(?:\\[\\s*\\d+%\\]\\s*)?(?:PASSED|SKIPPED|XFAIL|XPASS)\\s",
                     re.I)
FAIL_STATUS = re.compile(r"\bFAIL(?:ED|URE)?\b")  # a status word, not a test named TestParseError


def cwd_pattern(cwd):
    """The run folder in the forms an output may print it: both slashes, resolved, Git Bash /c/..."""
    forms = set()
    for base in {cwd, os.path.realpath(cwd)}:
        base = base.rstrip("/\\")
        forms.update({base, base.replace("\\", "/"), base.replace("/", "\\")})
        m = re.match(r"([A-Za-z]):[\\/](.*)", base)
        if m:
            forms.add("/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/"))
    forms = [f for f in forms if len(f) >= 3]  # "/" or "c:" would match inside ordinary words
    if not forms:
        return None
    flags = re.I if os.name == "nt" or re.match(r"[A-Za-z]:", cwd) else 0
    return re.compile("(?:%s)(?![\\w-]|\\.\\w)" % "|".join(  # not a longer name: wt2, wt.bak
        re.escape(f) for f in sorted(forms, key=len, reverse=True)), flags)


def signature_lines(output, cwd=None):
    """The failure signatures of one failing run's output: sorted, de-duplicated, capped.
    cwd: the folder the command ran in; its path becomes <cwd>, so runs in other checkouts compare."""
    here = cwd_pattern(cwd) if cwd else None
    if here:
        output = here.sub("<cwd>", output)
    seen = []
    for raw in ANSI.sub("", output).splitlines():
        line = raw.strip()
        for pattern, repl in VOLATILE:
            line = pattern.sub(repl, line)
        line = " ".join(line.split())
        failing = FAILURE_WORD.search(line)
        if not line or len(line) > 500 or any(p.search(line) for p in NOISE) \
                or PROGRESS.match(line) or (CODE_FRAME.match(line) and not failing) \
                or (PASSING.search(line) and not FAIL_STATUS.search(line)):
            continue
        if not FAILURE_WORD.search(line) and any(p.search(line) for p in NOISE_UNLESS_FAILURE):
            continue
        if PASSING_TAP.search(line) and "not ok" not in line:
            continue
        for pattern, repl in COUNTS:
            line = pattern.sub(repl, line)
        if line not in seen:
            seen.append(line)
            if len(seen) >= MAX_SIGNATURES:
                break
    return sorted(seen)


def diff_baseline(before, now):
    """(new failures, resolved signatures). No baseline counts as a passing one."""
    before = before or {"exit": 0, "unrunnable": False, "signatures": []}
    if now["unrunnable"] and not before.get("unrunnable"):
        return ["<command became unrunnable>"], []
    new = []
    if before.get("exit") == 0 and now["exit"] != 0:
        new.append("<exited with code %s>" % now["exit"])
    old = set(before.get("signatures") or [])
    new += [s for s in now["signatures"] if s not in old]
    return new, sorted(old - set(now["signatures"]))


def run_signatures(command, cwd, log):
    """Run the command (no time limit) and reduce its run to exit code and signatures."""
    code = run_check(command, cwd, log)
    unrunnable = code in (126, 127)
    if code == 0 or unrunnable:
        return {"exit": code, "unrunnable": unrunnable, "signatures": []}
    with io.open(log, encoding="utf-8", errors="replace") as f:
        return {"exit": code, "unrunnable": False, "signatures": signature_lines(f.read(), cwd)}


BASELINE_HISTORY = 20
BASELINE_VERSION = 2  # bump whenever signature_lines changes what a failure line reduces to


def plan_section(job, name):
    try:
        with io.open(os.path.join(oke_home(), "jobs", job, "plan.md"), encoding="utf-8") as f:
            text = f.read()
    except (IOError, OSError, ValueError):
        return ""
    m = re.search(r"^## " + re.escape(name) + r"[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def plan_command(job):
    """The Check command: the first backticked span of plan.md's section."""
    m = re.search(r"`([^`]+)`", plan_section(job, u"Check command"))
    return m.group(1).strip() if m else None


def criteria_rev(job):
    """Changes whenever the acceptance criteria or the Check command change."""
    text = plan_section(job, u"Acceptance criteria") + "\n" + (plan_command(job) or "")
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_dir(info):
    """(cwd, what is checked): the worktree and its HEAD, or the main checkout and
    tree:<sha> of its pending changes (what a review of a job without a worktree names)."""
    wt = (info.get("worktree") or {}).get("path")
    if wt:
        cwd = native_path(wt)
        code, dirty = git_out(cwd, "status", "--porcelain", "-uno")
        if code == 0 and dirty:  # uncommitted work: HEAD is not what ran
            tree = pending_tree(cwd)
            return cwd, "tree:" + tree if tree else None
        code, head = git_out(cwd, "rev-parse", "HEAD")
        return cwd, head if code == 0 else None
    main = native_path(repo_dir(info["git_key"]))
    tree = pending_tree(main)
    return main, "tree:" + tree if tree else None


def quality_target(job):
    """(info, command, cwd, target) or an exit code."""
    info = load_state()["jobs"].get(job)
    if not info:
        return fail("unknown job: %s" % job)
    if never_merges(info):
        return fail("job %s has nothing to merge (its output is %s): no baseline or check"
                    % (job, job_output(info)))
    if no_path(job, info):
        return fail(no_path(job, info))
    command = plan_command(job)
    if not command:
        fail(u"plan.md of %s names no Check command: its section's first line is the command in backticks" % job)
        return 2
    cwd, target = check_dir(info)
    if not target:
        fail("cannot read the commit or tree of %s in %s" % (job, cwd))
        return 2
    return info, command, cwd, target


def untracked_work(cwd):
    code, out = git_out(cwd, "status", "--porcelain")
    return [l[3:] for l in out.splitlines() if l.startswith("?? ") and not byproduct(l)] if code == 0 else []


def base_point(info, cwd):
    """The commit the job's work starts from: in a worktree, where its branch leaves the
    base branch; without one, the main checkout's HEAD."""
    main = native_path(repo_dir(info["git_key"]))
    code, head = git_out(main, "rev-parse", "HEAD")
    if not (info.get("worktree") or {}).get("path"):
        return head if code == 0 else None
    code, point = git_out(cwd, "merge-base", "HEAD", info.get("base") or head)
    return point if code == 0 else None


def untouched(info, cwd, point):
    code, dirty = git_out(cwd, "status", "--porcelain", "-uno")
    if code != 0 or dirty:
        return False
    if (info.get("worktree") or {}).get("path"):
        return not untracked_work(cwd) and git_out(cwd, "rev-parse", "HEAD")[1] == point
    return True


def run_at(cwd, commit, where, run):
    """run(where) in a temporary detached checkout of `commit`, removed afterwards; None
    when git cannot make it."""
    def drop():
        subprocess.call(["git", "-C", cwd, "worktree", "remove", "--force", where],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(where, ignore_errors=True)
        subprocess.call(["git", "-C", cwd, "worktree", "prune"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    drop()  # a run that died left it behind
    if subprocess.call(["git", "-C", cwd, "worktree", "add", "-q", "--detach", where, commit],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        return None
    try:
        return run(where)
    finally:
        drop()


def read_baseline(job, any_version=False):
    """The job's baseline; None without one, or (unless any_version) recorded by another version."""
    try:
        with io.open(os.path.join(oke_home(), "jobs", job, "baseline.json"), encoding="utf-8") as f:
            base = json.load(f)
    except (IOError, OSError, ValueError):
        return None
    return base if any_version or base.get("version") == BASELINE_VERSION else None


def reread_baseline(job, info, old):
    """An older baseline with its signatures read from its baseline.log the current way;
    None without the log. The run was in the job's checkout or a baseline checkout."""
    log = os.path.join(oke_home(), "jobs", job, "baseline.log")
    try:
        recorded = time.mktime(time.strptime(old.get("recorded_at", ""), "%Y-%m-%dT%H:%M:%S"))
        if os.path.getmtime(log) > recorded + 60:
            return None  # written by a later run that saved no baseline: not this one's output
        with io.open(log, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except (IOError, OSError, ValueError, OverflowError):
        return None
    folders = [(info.get("worktree") or {}).get("path"), repo_dir(info["git_key"]),
               os.path.join(oke_home(), "jobs", job, "baseline-checkout")]
    sigs = []
    for where in [f for f in folders if f]:
        sigs += [l for l in signature_lines(text, where) if l not in sigs]
    return dict(old, signatures=sigs)


def cmd_baseline(args):
    found = quality_target(args.job)
    if isinstance(found, int):
        return found
    if merge_state(args.job)[0] in ("unverified", "verified"):
        # after the merge the starting point is the job's own tip: its failures would read as old
        return fail("job %s is merged already: a baseline now would run on its own code" % args.job)
    info, command, cwd, target = found
    job_dir = os.path.join(oke_home(), "jobs", args.job)
    point = base_point(info, cwd)
    if not point:
        fail("cannot find the commit job %s started from in %s" % (args.job, cwd))
        return 2
    lock = "check-" + args.job
    while not try_lock(lock):
        time.sleep(5)
    try:
        log = os.path.join(job_dir, "baseline.log")
        if untouched(info, cwd, point):
            now = run_signatures(command, cwd, log)
        else:
            # the work has begun: run on the code it started from, so a re-run never
            # takes the job's own failures for old ones
            now = run_at(cwd, point, os.path.join(job_dir, "baseline-checkout"),
                         lambda where: run_signatures(command, where, log))
            if now is None:
                fail("cannot check out %s for the baseline of %s" % (point[:12], args.job))
                return 2
        if now["unrunnable"]:  # the previous baseline stays
            fail(u"the Check command could not run (exit %d, see %s): fix the command in plan.md, then "
                 u"`$OKE baseline %s` again" % (now["exit"], log.replace("\\", "/"), args.job))
            return 2
        target = point
        path = os.path.join(job_dir, "baseline.json")
        old = read_baseline(args.job)
        if read_baseline(args.job, any_version=True) is not None:
            history = os.path.join(job_dir, "baseline.history")
            if not os.path.isdir(history):
                os.makedirs(history)
            stamp = time.strftime("%Y%m%dT%H%M%S")
            name, n = stamp + ".json", 1
            while os.path.exists(os.path.join(history, name)):  # same second: "_" sorts after "."
                name, n = "%s_%03d.json" % (stamp, n), n + 1
            os.rename(path, os.path.join(history, name))
            for stale in sorted(os.listdir(history))[:-BASELINE_HISTORY]:
                os.remove(os.path.join(history, stale))
        base = dict(now, version=BASELINE_VERSION, recorded_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                    criteria_rev=criteria_rev(args.job), commit=target)
        write_json_atomic(path, base)
    finally:
        unlock(lock)
    before = set((old or {}).get("signatures") or [])
    print(json.dumps({"result": "recorded", "exit": now["exit"], "signatures": len(now["signatures"]),
                      "added": len(set(now["signatures"]) - before),
                      "removed": len(before - set(now["signatures"])),
                      "log": log.replace("\\", "/")}))
    return 0


def cmd_check(args):
    """Run the Check command where the job's work is and judge it against the baseline:
    0 no new failure, 1 new failures, 2 cannot judge. Written to the ledger by OKe."""
    found = quality_target(args.job)
    if isinstance(found, int):
        return found
    info, command, cwd, target = found
    loose = untracked_work(cwd) if (info.get("worktree") or {}).get("path") else []
    if loose:
        fail("untracked files in %s: %s. `git add` the ones that are part of the work (the merge "
             "carries only tracked files), delete the rest, then check again" % (cwd, ", ".join(loose)))
        return 2
    rev = criteria_rev(args.job)
    base = read_baseline(args.job)
    if base is None:
        if read_baseline(args.job, any_version=True) is not None:
            merged = merge_state(args.job)[0] in ("unverified", "verified")
            fail("the baseline of %s was recorded by an older OKe, which read failures differently: %s"
                 % (args.job, "`$OKE job verify %s` reads its log again" % args.job if merged
                    else "run `$OKE baseline %s` again" % args.job))
        else:
            fail("job %s has no baseline: run `$OKE baseline %s` before the work starts" % (args.job, args.job))
        return 2
    if base.get("criteria_rev") != rev:
        fail(u"the Check command or the criteria of %s changed since the baseline: run `$OKE baseline %s` again"
             % (args.job, args.job))
        return 2
    lock = "check-" + args.job
    while not try_lock(lock):
        time.sleep(5)
    try:
        n = len([r for r in ledger_rows(args.job) if r["check"] == "check"]) + 1
        log = os.path.join(oke_home(), "jobs", args.job, "check-%d.log" % n)
        now = run_signatures(command, cwd, log)
        new, resolved = diff_baseline(base, now)
        log = log.replace("\\", "/")
        if now["unrunnable"]:
            fail(u"the Check command could not run (exit %d, see %s)" % (now["exit"], log))
            return 2
        result = "fail" if new else "pass"
        ledger_add(args.job, "oke", target, "check", result,
                   "%s new=%d resolved=%d log=%s" % (rev, len(new), len(resolved), log))
    finally:
        unlock(lock)
    print(json.dumps({"result": result, "criteria_rev": rev, "commit": target, "new": new,
                      "resolved": len(resolved), "baseline_failures": len(base.get("signatures") or []),
                      "log": log}, ensure_ascii=False))
    return 1 if new else 0


def same_tree_check(job, main, merged, rev):
    """The target of a passing `oke check` under `rev` whose tree is the merged commit's, or None."""
    code, tree = git_out(main, "rev-parse", merged + "^{tree}")
    if code != 0:
        return None
    last = {}
    for r in ledger_rows(job):
        if r["check"] == "check":
            last[r["commit"]] = r  # a later run on the same target supersedes an earlier one
    for target, r in last.items():
        if r["result"] != "pass" or r["detail"].split()[:1] != [rev]:
            continue
        if target.startswith("tree:"):
            checked = target[len("tree:"):]
        else:
            ok, checked = git_out(main, "rev-parse", target + "^{tree}")
            if ok != 0:
                continue
        if checked == tree:
            return target
    return None


def changed_since_check(job, main, merged, rev):
    """(target, tree, files) of the last passing `oke check` under `rev`, with the files the merged
    commit changes against its tree: what other merges brought in meanwhile. None without one."""
    last = {}  # target -> (position, its latest check row)
    reviewed = None
    for i, r in enumerate(ledger_rows(job)):
        if r["check"] == "check":
            last[r["commit"]] = (i, r)
        elif r["check"] == "review" and r["result"] == "pass":
            reviewed = r["commit"]
    passing = sorted((i, t) for t, (i, r) in last.items()
                     if r["result"] == "pass" and r["detail"].split()[:1] == [rev])
    if not passing:
        return None
    # the reviewed commit is what was merged; a later check of other edits would list the job's own files
    target = reviewed if reviewed in [t for _, t in passing] else passing[-1][1]
    tree = target[len("tree:"):] if target.startswith("tree:") else git_out(main, "rev-parse", target + "^{tree}")[1]
    code, out = git_out(main, "diff", "--name-only", tree, merged + "^{tree}")
    if code != 0 or not tree:
        return None
    return target, tree, [f for f in out.splitlines() if f]


def verify_job(job, command, accept=None, skip=None, run=False):
    """Run the Check command on the merged main checkout. A failure undoes the merge, but never
    anybody else's work: only while HEAD is still the merge, and with `reset --keep`,
    which keeps local edits and refuses when they would be lost.
    No time limit: a long simulation is a normal check. Run it in the background.
    A second run (a restarted PM) waits for the first and then sees its result."""
    info = load_state()["jobs"].get(job)
    if not info:
        return fail("unknown job: %s" % job)
    lock = "verify-" + job
    while not try_lock(lock):
        time.sleep(5)
    try:
        return verify_locked(job, command, accept, info, skip, run)
    finally:
        unlock(lock)


def verify_locked(job, command, accept, info, skip=None, run=False):
    status, row = merge_state(job)
    if status == "verified":
        print(json.dumps({"result": "verified", "job": job, "commit": row["commit"], "already": True}))
        return 0
    if status != "unverified":
        return fail("job %s has no merge to verify (%s): run `$OKE job merge %s` first" % (job, status, job))
    if accept:
        # the user's call, e.g. after the main branch moved on: recorded as such
        ledger_add(job, "user", row["commit"], "verify", "pass", "accepted: " + accept)
        tell_after_waiters(load_state(), job, AFTER_FINISHED, ("merging", "awaiting_merge"))
        print(json.dumps({"result": "verified", "job": job, "commit": row["commit"], "accepted": True}))
        return 0
    main = native_path(repo_dir(info["git_key"]))
    if git_out(main, "rev-parse", "HEAD")[1] != row["commit"]:
        return fail("the main checkout moved past the merge of %s (%s): ask the user; if they accept "
                    "the merge as it is, `$OKE job verify %s --accept \"<why>\"`" % (job, row["commit"][:12], job))
    planned = plan_command(job)
    if command and planned and command != planned:
        return fail(u"the Check command given (%s) differs from plan.md's (%s): run `$OKE job verify %s` "
                    u"without a command" % (command, planned, job))
    command = planned or command
    if not command:
        return fail(u"give the Check command (plan.md names none), or --accept \"<why>\" when the user "
                    u"accepts the merge as it is")
    rev = criteria_rev(job)
    same = same_tree_check(job, main, row["commit"], rev)
    if same:
        # the merged code is exactly what `oke check` passed on: running it again proves nothing new
        ledger_add(job, "oke", row["commit"], "verify", "pass", "same tree as " + same)
        tell_after_waiters(load_state(), job, AFTER_FINISHED, ("merging", "awaiting_merge"))
        print(json.dumps({"result": "verified", "job": job, "commit": row["commit"], "skipped": True,
                          "same_tree_as": same}))
        return 0
    since = None if run else changed_since_check(job, main, row["commit"], rev)
    if skip and not since:
        return fail("nothing to skip: no passing `oke check` under the current plan; run `$OKE job verify %s --run`"
                    % job)
    if since and skip:
        # the PM judged that what other merges brought in cannot change the check's result
        target, _, files = since
        shown = ", ".join(files[:20]) + (" (+%d more)" % (len(files) - 20) if len(files) > 20 else "")
        ledger_add(job, "pm", row["commit"], "verify", "pass",
                   "skipped by PM: %s (checked %s; changed: %s)" % (" ".join(skip.split()), target, shown))
        tell_after_waiters(load_state(), job, AFTER_FINISHED, ("merging", "awaiting_merge"))
        print(json.dumps({"result": "verified", "job": job, "commit": row["commit"], "skipped": True,
                          "skipped_by": "pm", "checked": target, "changed": files}))
        return 0
    if since:
        # the code differs from what `oke check` passed on only by other merges: the PM decides
        target, tree, files = since
        code, stat = git_out(main, "diff", "--shortstat", tree, row["commit"] + "^{tree}")
        print(json.dumps({"result": "changed_since_check", "job": job, "commit": row["commit"],
                          "checked": target, "changed": files, "stat": stat}, ensure_ascii=False))
        return 4
    n = len([r for r in ledger_rows(job) if r["check"] == "verify"]) + 1
    log = os.path.join(oke_home(), "jobs", job, "verify-%d.log" % n)
    base = read_baseline(job)
    old = read_baseline(job, any_version=True)
    if not base and old and old.get("criteria_rev") == rev:
        # recorded by an older OKe, which read failures differently: read its log again the
        # current way (a new baseline now would run on the job's own merged code)
        base = reread_baseline(job, info, old)
        if not base:
            return fail("the baseline of %s was recorded by an older OKe and its log is gone: ask the user; "
                        "if they accept the merge as it is, `$OKE job verify %s --accept \"<why>\"`" % (job, job))
    if (base or {}).get("criteria_rev") != rev:
        base = None  # another command's failures say nothing about this one's
    now = run_signatures(command, main, log)
    code = now["exit"]
    new, _ = diff_baseline(base, now)
    with io.open(log, encoding="utf-8", errors="replace") as f:
        tail = f.read().splitlines()[-40:]
    log = log.replace("\\", "/")
    if now["unrunnable"]:  # the shell could not run the command: not a verdict on the merge
        return fail("the Check command could not run (exit %d, see %s): fix the command; the merge stays"
                    % (code, log))
    if not new:
        ledger_add(job, "oke", row["commit"], "verify", "pass", command)
        tell_after_waiters(load_state(), job, AFTER_FINISHED, ("merging", "awaiting_merge"))
        print(json.dumps({"result": "verified", "job": job, "commit": row["commit"], "log": log}))
        return 0
    ledger_add(job, "oke", row["commit"], "verify", "fail", "exit %d: %s" % (code, command))
    words = row["detail"].split()
    if "already-in" in words:
        # main held the branch before OKe's merge: nothing to undo, but the job goes back
        # to the worker like any failed merge (rolled_back lets it merge again)
        ledger_add(job, "oke", row["commit"], "rollback", "ok", "already-in: nothing undone")
        print(json.dumps({"result": "verify_failed", "job": job, "exit": code, "log": log,
                          "rolled_back_to": row["commit"], "already_in": True, "uncommitted": False,
                          "tail": tail, "new": new}))
        return 3
    pre = words[0][len("pre="):] if words and words[0].startswith("pre=") else None
    # a direct commit (no worktree) goes back to uncommitted changes for the worker to fix
    mode = "--mixed" if "direct" in words else "--keep"
    head = git_out(main, "rev-parse", "HEAD")[1]
    if not pre or head != row["commit"] or subprocess.call(
            ["git", "-C", main, "reset", "-q", mode, pre],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        return fail("the Check command failed on the merge of %s (log %s), and the merge was NOT undone: "
                    "the main checkout moved on or has local edits in the merged files. Ask the user."
                    % (job, log))
    ledger_add(job, "oke", pre, "rollback", "ok", "after verify fail")
    print(json.dumps({"result": "verify_failed", "job": job, "exit": code, "log": log,
                      "rolled_back_to": pre, "uncommitted": mode == "--mixed", "tail": tail, "new": new}))
    return 3


def cmd_ledger(args):
    if args.job != slug(args.job):  # a name like ../x would write outside the jobs folder
        return fail("bad job name %r" % args.job)
    if args.action == "add":
        if not os.path.isdir(os.path.dirname(ledger_path(args.job))):
            return fail("unknown job: %s" % args.job)
        if args.check in OKE_CHECKS:
            return fail("check %r is written by OKe itself; use a name like worker-verify" % args.check)
        ledger_add(args.job, args.by, args.commit, args.check, args.result, args.detail)
        return 0
    status, _ = merge_state(args.job)
    rows, bad, _ = ledger_chain(args.job)
    print(json.dumps({"job": args.job, "merge": status, "rows": rows, "ignored_lines": bad}, indent=2))
    return 0


# Phases that pass a user gate may only be entered from the phase before that gate
# (or stay). Not proof of a user's approval, but a PM or an orchestrator slip cannot
# skip the approval step. Phases not listed here are free.
# Job sizes, which the PM decides while planning. small: a subagent does the work,
# one approval covers plan and merge; normal: a worker pane, a review subagent;
# large: worker and reviewer panes
SIZES = ("small", "normal", "large")
# state.json from before the rename kept a "lane" with the old names
OLD_LANES = {"trivial": "small", "small": "normal", "normal": "large", "research": "research"}
# What a job hands over (its output), apart from where it works:
#   code    a git job's change, accepted by its verified merge
#   files   a direct job's folder (outside git), changed in place: no branch, no merge
#   result  an answer or a run's outputs: research (the PM looks and reports, no worker, no plan
#           approval), or an experiment in a worktree whose edits never reach the main branch
OUTPUTS = ("code", "result")  # the ones `job output` sets: files comes from the path
NO_MERGE = "the job has nothing to merge (its output is not code): set phase done after its report"


def job_output(info):
    """code, files or result. Jobs saved before outputs: research -> result, direct -> files."""
    if info.get("size") == "research":
        return "result"
    if info.get("direct"):
        return "files"
    return info.get("output") or "code"


def never_merges(info):
    return job_output(info) != "code"


def experiment(info):
    """A result job with a worker: it works in a worktree, its edits are thrown away (not a
    job the user ended with no change: it made nothing to keep)."""
    return info.get("size") != "research" and job_output(info) == "result" and not info.get("no_change")


def plan_worktree(job):
    """Does the job's plan give it a worktree (its `## Workspace` section names its branch)?"""
    section = plan_place(job)
    if section is None:
        return False
    # the Takes over line names files, which may contain oke/<job> too
    lines = [l for l in section.splitlines() if not TAKEN_LINE.match(l)]
    return ("oke/" + job) in "\n".join(lines)


# a plan's work-place section
PLACE_SECTION = re.compile(r"^## Workspace[^\n]*\n(.*?)(?=^## |\Z)", re.M | re.S)
# its line naming the main checkout's uncommitted changes the job takes over
TAKEN_LINE = re.compile(r"^\s*Takes over\s*:\s*(.*)$", re.M | re.I)


def plan_place(job):
    """The text of the job's `## Workspace` section, or None."""
    try:
        with io.open(os.path.join(oke_home(), "jobs", job, "plan.md"), encoding="utf-8") as f:
            section = PLACE_SECTION.search(f.read())
    except (OSError, ValueError):
        return None
    return section.group(1) if section else None


def uncommitted_tracked(cwd):
    """The checkout's uncommitted changes to tracked files, each as the list of its paths
    (a rename or copy: the new path, then the old one), exactly as git stores them, or None
    when git fails."""
    try:
        p = subprocess.run(["git", "-C", cwd, "status", "--porcelain", "-z", "-uno"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    if p.returncode != 0:
        return None
    fields, out, i = p.stdout.decode("utf-8", "replace").split("\0"), [], 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        paths = [entry[3:]]
        if "R" in entry[:2] or "C" in entry[:2]:
            paths.append(fields[i] if i < len(fields) else "")
            i += 1
        out.append(paths)
    return out


def plan_uncommitted(job):
    """The main checkout's uncommitted files the plan takes over as the job's own change
    (its `## Workspace` section's `Takes over: <file>, <file>` line), or [] (`Takes over: none`,
    or no such line)."""
    section = plan_place(job)
    line = TAKEN_LINE.search(section) if section is not None else None
    if not line or line.group(1).strip().lower() in ("none", ""):
        return []
    names = [p.strip().strip("`\"'").strip() for p in line.group(1).split(",")]
    return [n.replace("\\", "/") for n in names if n]


PHASE_FROM = {
    "planning": ("intake", "planning", "awaiting_approval", "working", "reviewing", "awaiting_merge"),
    "working": ("awaiting_approval", "working", "reviewing", "awaiting_merge", "merging"),
    "merging": ("awaiting_merge", "merging"),
    "done": ("merging", "done"),
}


# ---------------------------------------------------------------- interview

DIMENSIONS = (("goal", 0.40), ("constraints", 0.30), ("criteria", 0.30))
INTERVIEW_THRESHOLD = 0.2  # ambiguity at or below this ends the interview
INTERVIEW_MAX_ROUNDS = 10
STALL_ROUNDS, STALL_BAND = 3, 0.05
CHALLENGES = {3: "contrarian", 5: "simplifier"}  # round -> one-time challenge question


def interview_path(job):
    return os.path.join(oke_home(), "jobs", job, "interview.json")


def ambiguity(scores):
    return round(1 - sum(scores[name] * weight for name, weight in DIMENSIONS), 3)


def interview_problem(job):
    """Why job `job` may not start planning yet, or None."""
    data = read_json(interview_path(job), None)
    if not isinstance(data, dict) or not data.get("done"):
        return ("job %s has no finished interview: run `$OKE interview %s --goal .. --constraints .. "
                "--criteria ..` until it says done" % (job, job))
    return None


def cmd_interview(args):
    """One round of the intake interview: record the orchestrator's clarity scores
    (and the question just answered), compute ambiguity, say what to ask next."""
    path = interview_path(args.job)
    if args.job != slug(args.job) or not os.path.isdir(os.path.dirname(path)):
        return fail("unknown job: %s (run `$OKE job new` first)" % args.job)
    scores = {}
    for name, _ in DIMENSIONS:
        value = getattr(args, name)
        if not 0.0 <= value <= 1.0:
            return fail("--%s must be between 0 and 1" % name)
        scores[name] = value
    config = load_config()
    try:
        threshold = float(config.get("interview_threshold", INTERVIEW_THRESHOLD))
        max_rounds = int(config.get("interview_max_rounds", INTERVIEW_MAX_ROUNDS))
    except (TypeError, ValueError):
        return fail("interview_threshold / interview_max_rounds in config.json must be numbers")
    data = read_json(path, None)
    if not isinstance(data, dict):
        data = {"rounds": [], "done": False}
    if data.get("done"):
        return fail("the interview of %s is already done (%s)" % (args.job, data.get("reason")))
    number = len(data["rounds"])  # round 0 scores the user's first request, no question
    amb = ambiguity(scores)
    data["rounds"].append({"round": number, "scores": scores, "ambiguity": amb,
                           "question": args.question, "answer": args.answer})
    if amb <= threshold:
        reason = "clear"
    elif args.stop:
        reason = "user_stop"
    elif number >= max_rounds:
        reason = "round_cap"
    else:
        reason = None
    data.update(done=reason is not None, reason=reason, threshold=threshold)
    write_json_atomic(path, data)
    last = [r["ambiguity"] for r in data["rounds"][-STALL_ROUNDS:]]
    out = {"job": args.job, "round": number, "ambiguity": amb, "threshold": threshold,
           "scores": scores, "done": data["done"], "reason": reason,
           # the weakest dimension weighs most: it is what the next question is about
           "weakest": min(DIMENSIONS, key=lambda d: (scores[d[0]] - 1) * d[1])[0],
           "stalled": len(last) == STALL_ROUNDS and max(last) - min(last) <= STALL_BAND,
           "challenge": None if reason else CHALLENGES.get(number)}
    print(json.dumps(out, ensure_ascii=False))
    return 0


def archived_dir(job):
    """The newest archive folder of `job`, or None."""
    found = sorted(d for d in glob.glob(os.path.join(oke_home(), "archive", glob.escape(job) + "-*"))
                   if re.match(re.escape(job) + r"-\d{8}-\d{6}$", os.path.basename(d)))
    return found[-1] if found else None


def kept_worktree(kept):
    """The worktree an archived job left for the user (an experiment's outputs, or one a
    no-change end could not remove), or None."""
    wt = (kept.get("worktree") or {}).get("path") if isinstance(kept, dict) else None
    if wt and (experiment(kept) or kept.get("no_change")) and not kept.get("worktree_removed") \
            and os.path.isdir(native_path(wt)):
        return wt
    return None


def kept_worktrees():
    """[{job, path}] of archived jobs whose worktree is still there for the user."""
    out = []
    for path in sorted(glob.glob(os.path.join(oke_home(), "archive", "*", "job.json"))):
        wt = kept_worktree(read_json(path, {}))
        if wt:
            out.append({"job": re.sub(r"-\d{8}-\d{6}$", "", os.path.basename(os.path.dirname(path))),
                        "path": wt})
    return out


def remove_kept_worktree(job, kept, force):
    """Remove an archived job's worktree and its branch. With force (no change was checked, or the
    user said to throw it away) the branch goes even when it holds commits a revert undid; otherwise
    only with -d. Returns (removed, branch_left)."""
    wt = native_path(kept["worktree"]["path"])
    main = native_path(repo_dir(kept["git_key"]))
    if os.path.isdir(wt):
        # through the runtime first: herdr closes the worktree's workspace and the panes in it
        # (on Windows git cannot delete a folder a shell still sits in). Found by its path, never
        # by the stored id: herdr reuses ids, and that id may be another job's workspace now
        try:
            rt = runtime()
            wid = rt.worktree_workspace(main, wt)
            if wid:
                rt.worktree_remove(wid, force=force)
        except HerdrError:
            pass  # no workspace open for it (its last pane went): git removes the folder
    if os.path.isdir(wt):
        try:
            runtime_bg.git_worktree_remove(wt, main, force=force)
        except HerdrError:
            pass
    subprocess.call(["git", "-C", main, "worktree", "prune"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.path.isdir(wt):
        return False, None
    subprocess.call(["git", "-C", main, "branch", "-q", "-D" if force else "-d", "oke/" + job],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    left = git_out(main, "rev-parse", "-q", "--verify", "refs/heads/oke/" + job)[0] == 0
    return True, ("oke/" + job) if left else None


def clean_job(job):
    """`job clean <job>`: remove an archived job's kept worktree (the user said so)."""
    found = sorted(d for d in glob.glob(os.path.join(oke_home(), "archive", glob.escape(job) + "-*"))
                   if re.match(re.escape(job) + r"-\d{8}-\d{6}$", os.path.basename(d))
                   and kept_worktree(read_json(os.path.join(d, "job.json"), {})))
    if not found:
        return fail("job %s has no archived worktree left to remove" % job)
    folder = found[-1]
    kept = read_json(os.path.join(folder, "job.json"), {})
    wt = kept["worktree"]["path"]
    # --force: its edits and outputs are what the user said to throw away
    removed, branch_left = remove_kept_worktree(job, kept, force=True)
    if not removed:
        return fail("could not remove %s: close what still runs in it, then run this again" % wt)
    kept["worktree_removed"] = True
    write_json_atomic(os.path.join(folder, "job.json"), kept)
    out = {"job": job, "removed": wt}
    if branch_left:
        out["branch_left"] = branch_left
    print(json.dumps(out, ensure_ascii=False))
    return 0


def archive_refusal(job, state):
    """Why `job archive` must not take this job yet, or None: only a finished job moves to the
    archive (an open one would lose its state; seen live after a refused `job phase done`)."""
    info = state["jobs"].get(job)
    if info is None or not info:
        return None  # unknown (refused later) or a folder left behind with no state entry
    if info.get("phase") in ("done", "failed"):
        return None
    return ("job %s is %s, not done or failed: archive only a finished job (`job phase %s done` first, "
            "or cancel it)" % (job, info.get("phase"), job))


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
        if any(re.match(re.escape(args.slug) + r"-\d{8}-\d{6}$", os.path.basename(d))
               for d in glob.glob(os.path.join(home, "archive", glob.escape(args.slug) + "-*"))):
            # never a name again: an archived ledger copied in would carry valid signatures
            return fail("job %s was archived before: pick another job name" % args.slug)
        if args.pm not in state["pms"]:
            return fail("no PM %s: pick one of %s, or `$OKE pm add` a new one"
                        % (args.pm, ", ".join(sorted(state["pms"])) or "(none yet)"))
        os.makedirs(job_dir)
        state["jobs"][args.slug] = {"pm": args.pm, "path": None, "phase": "intake", "worktree": None,
                                    "phase_at": time.time(), "created": time.time(), "base": None}
        if args.research:
            state["jobs"][args.slug]["size"] = "research"
        if args.path:
            try:
                set_job_path(state, args.slug, args.path)
            except ValueError as e:
                os.rmdir(job_dir)
                return fail(str(e))
        save_state(state)
        info = state["jobs"][args.slug]
        print(json.dumps({"job_dir": job_dir.replace("\\", "/"), "path": info.get("path"),
                          "size": "research" if args.research else None,
                          "direct": bool(info.get("direct")) if info.get("path") else None,
                          "pm_job_dir": pm_job_dir(args.slug).replace("\\", "/"),
                          "pm_dir": pm_dir(args.pm).replace("\\", "/"),
                          "pm_agent": args.pm,
                          "wiki": wiki_dir(args.pm).replace("\\", "/"),
                          "shared_wiki": os.path.join(load_config()["wiki_path"], "_shared").replace("\\", "/")},
                         ensure_ascii=False))
        return 0
    if args.slug not in state["jobs"]:
        return fail("unknown job: %s" % args.slug)
    if args.action == "path":
        info = state["jobs"][args.slug]
        try:
            folder = path_facts(args.path)[1]
        except ValueError as e:
            return fail(str(e))
        if info.get("path") and normalize_path(folder) == normalize_path(info["path"]):
            # the same folder (case, slashes): nothing to do, and safe to call at any time
            print(json.dumps({"job": args.slug, "path": info["path"], "direct": bool(info.get("direct")),
                              "unchanged": True}, ensure_ascii=False))
            return 0
        research = info.get("size") == "research" and not info.get("path") \
            and info.get("phase") not in DONE_PHASES  # no plan: it finds its path while it works
        started = info.get("worked") or info.get("worktree") or info.get("approved")
        if (info.get("phase") not in ("intake", "planning", "awaiting_approval") or started) and not research:
            if info.get("size") == "research":
                return fail("job %s is research and has its path %s: a research job keeps its path; report a "
                            "question" % (args.slug, info.get("path")))
            if info.get("phase") in SPLIT_PHASES:
                return fail("job %s has worked in %s (or was approved): its path stays. For work in %s split a "
                            "new job: `$OKE job split %s <new job> --path %s`"
                            % (args.slug, info.get("path"), folder, args.slug, args.path))
            return fail("job %s has worked in %s and is past planning: its path stays, and it is too late to "
                        "split. Report a question: the user decides on work in %s"
                        % (args.slug, info.get("path"), folder))
        if info.get("held"):
            return fail("job %s is on hold: leave it until a message says it is back" % args.slug)
        if not info.get("pm") or info["pm"] not in state["pms"]:
            return fail("job %s has no PM" % args.slug)
        try:
            set_job_path(state, args.slug, args.path, branch_taken=(
                "report a question: branch oke/%(job)s already exists in %(checkout)s (an earlier job); the user "
                "decides: delete that branch, or cancel this job and start it under another name"))
        except ValueError as e:
            return fail(str(e))
        save_state(state)
        print(json.dumps({"job": args.slug, "path": info["path"], "direct": bool(info.get("direct"))},
                         ensure_ascii=False))
        return 0
    if args.action == "split":
        info = state["jobs"][args.slug]
        if info.get("size") == "research":
            return fail("job %s is research: research jobs are not split (it changes nothing)" % args.slug)
        new = args.new
        if new != slug(new):
            return fail("bad job name %r: use %r" % (new, slug(new)))
        new_dir = os.path.join(home, "jobs", new)
        if new in state["jobs"] or os.path.exists(new_dir) or any(
                re.match(re.escape(new) + r"-\d{8}-\d{6}$", os.path.basename(d))
                for d in glob.glob(os.path.join(home, "archive", glob.escape(new) + "-*"))):
            return fail("job %s exists already (or was archived): pick another name" % new)
        if info.get("phase") not in SPLIT_PHASES or info.get("held"):
            return fail("job %s is past planning (%s): split it only while planning" % (args.slug, info.get("phase")))
        if args.after and args.after not in state["jobs"]:
            return fail("unknown job for --after: %s" % args.after)
        try:
            folder = path_facts(args.path)[1]
        except ValueError as e:
            return fail(str(e))
        twins = sorted(n for n, j in state["jobs"].items() if j.get("split_from") == args.slug
                       and j.get("path") == folder and j.get("phase") not in DONE_PHASES)
        if twins:  # a revived or re-planning PM splits again: reuse the job it made
            print(json.dumps({"existing": twins[0], "path": folder}, ensure_ascii=False))
            return fail("job %s already split %s off as job %s: use that job" % (args.slug, folder, twins[0]))
        state["jobs"][new] = {"pm": info.get("pm"), "path": None, "phase": "intake", "worktree": None,
                              "phase_at": time.time(), "created": time.time(), "base": None,
                              "split_from": args.slug}
        try:
            set_job_path(state, new, args.path)
        except ValueError as e:
            return fail(str(e))
        os.makedirs(new_dir)
        src = os.path.join(home, "jobs", args.slug, "intake.md")
        intake = io.open(src, encoding="utf-8").read() if os.path.exists(src) else u""
        starts_late = state["jobs"][new].get("direct")
        intake += u"\n## Split\nSplit from job %s by its PM: this job changes only %s.%s\n" % (
            args.slug, state["jobs"][new]["path"],
            (u" It starts its work only after job %s (plan `after: %s`)." if starts_late else
             u" It merges only after job %s (plan `after: %s`).") % (args.after, args.after) if args.after else u"")
        with io.open(os.path.join(new_dir, "intake.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(intake)
        if os.path.exists(interview_path(args.slug)):
            shutil.copyfile(interview_path(args.slug), interview_path(new))
        if args.after:
            state["jobs"][new]["after"] = args.after
        save_state(state)
        out = {"job": new, "split_from": args.slug, "path": state["jobs"][new]["path"],
               "direct": bool(state["jobs"][new].get("direct")), "after": args.after}
        out.update(pm_fields(info.get("pm"), new))
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if args.action == "phase":
        if state["jobs"][args.slug].get("held"):
            return fail("job %s is on hold: leave it until a message says it is back" % args.slug)
        if args.phase not in PHASES:
            return fail("bad phase %r: one of %s" % (args.phase, ", ".join(PHASES)))
        now = state["jobs"][args.slug].get("phase")
        allowed = PHASE_FROM.get(args.phase)
        info = state["jobs"][args.slug]
        no_merge = never_merges(info)
        if no_merge and args.phase in ("awaiting_merge", "merging"):
            return fail("job %s: %s" % (args.slug, NO_MERGE))
        if no_merge and args.phase == "done":
            allowed = allowed + ("working", "reviewing")
            if info.get("no_change"):  # the user ended it before it worked
                allowed = allowed + GATE_PHASES
        if info.get("size") == "research" and args.phase == "working":
            allowed = allowed + ("intake", "planning")  # no plan to approve
        if allowed is not None and now not in allowed:
            return fail("job %s cannot go from %s to %s: allowed only from %s"
                        % (args.slug, now, args.phase, ", ".join(allowed)))
        if args.phase == "working" and info.get("size") != "research" and no_path(args.slug, info):
            return fail(no_path(args.slug, info))
        if args.phase == "working" and experiment(info) and not plan_worktree(args.slug):
            return fail("job %s is an experiment (output result): its plan's `## Workspace` must name branch "
                        "oke/%s, so its edits never reach the main checkout" % (args.slug, args.slug))
        if args.phase == "working" and info.get("git_key") and not info.get("direct") \
                and info.get("size") != "research" and not plan_worktree(args.slug) and not info.get("worked_in_main") \
                and not ("worked_in_main" not in info and info.get("worked")):
            # it starts in the main checkout: every uncommitted change there becomes the job's,
            # so the plan must name each one it takes in (the user approved that)
            main = native_path(repo_dir(info["git_key"]))
            named = set(plan_uncommitted(args.slug))
            unnamed = [" <- ".join(paths) for paths in (uncommitted_tracked(main) or [])
                       if not named.intersection(paths)]
            if unnamed:
                return fail(u"the main checkout %s has uncommitted changes the plan of job %s does not take in "
                            u"(%s): working there, the job would commit them. Ask the user, then give the PM "
                            u"`kind: plan`: a worktree, or `Takes over: <files>` naming them as the job's"
                            % (main, args.slug, ", ".join(unnamed[:5])))
        if args.phase == "working" and never_merges(info) and info.get("size") != "research" \
                and now in GATE_PHASES:
            # it never merges, so `after:` holds its start instead of its merge (a move back
            # from reviewing has started already: no gate)
            for other in after_jobs(args.slug):
                block = after_block(state, other)
                if block:
                    info["start_after"] = True  # `oke pending` names it as after_ready once free
                    if now == "awaiting_approval":
                        info["approved"] = True  # the user approved the plan; it only waits
                    save_state(state)
                    return fail("job %s: %s (it starts its work only after job %s). Write no assignment: "
                                "`$OKE pending` shows it as after_ready when it may start, or after_broken if "
                                "the after-job can never finish"
                                % (args.slug, block, other))
        if args.phase == "done" and experiment(info) and not info.get("no_change"):
            job_dir = os.path.join(oke_home(), "jobs", args.slug)
            review = latest_report(job_dir, r"-rv\d*$")
            worker = latest_report(job_dir, r"-w\d+$")
            if not review or review_verdict(review[1]) != "pass" or (worker and worker[0] > review[0]):
                return fail("job %s is an experiment: done needs the reviewer's `verdict: pass` on its latest "
                            "result" % args.slug)
        if args.phase == "done" and not no_merge and merge_state(args.slug)[0] != "verified":
            # done tells the user the work is in: only after OKe's merge and its verify
            return fail("job %s: its merge is %s: `$OKE job merge`, then `$OKE job verify`, before done"
                        % (args.slug, merge_state(args.slug)[0]))
        if args.phase == "done" and info.get("pm") and not wiki.logged(wiki_dir(info["pm"]), args.slug,
                                                                         info.get("created")):
            return fail("job %s: the PM has not recorded what it taught. It writes the lessons with "
                        "`$OKE wiki write --job %s`, or `$OKE wiki log --job %s --note none`"
                        % (args.slug, args.slug, args.slug))
        if args.phase in ("planning", "working") and now == "intake":  # a re-plan needs no new interview
            problem = interview_problem(args.slug)
            if problem:
                return fail(problem)
        state["jobs"][args.slug].update(phase=args.phase, phase_at=time.time())
        if args.phase == "working" and now not in ("working", "reviewing"):
            # set before its execute (curate) assignment is written: until a newer seq is,
            # whatever the PM reports is about the plan (or an answer to it), not the work.
            # Set after it (a research start, a misordered approval): no mark.
            seq = pm_job_report(args.slug)[0]
            if assignment_kind(args.slug) not in WORK_KINDS:
                state["jobs"][args.slug]["start_seq"] = seq
            else:
                state["jobs"][args.slug].pop("start_seq", None)
        info.pop("start_after", None)
        if args.phase == "working":
            info["worked"] = True  # a later re-plan does not undo the edits already made
            if not plan_worktree(args.slug):
                info["worked_in_main"] = True  # its edits may sit in the main checkout, whatever a re-plan says
            else:
                info.setdefault("worked_in_main", False)  # new state: never read as an old job's
        if args.phase not in ("intake", "planning", "awaiting_approval", "failed"):
            info["approved"] = True  # past its approval once: its path stays
        save_state(state)
        if args.phase == "done" and no_merge:  # it never merges: done finishes it for `after:`
            tell_after_waiters(state, args.slug, AFTER_FINISHED, ("merging", "awaiting_merge"))
        return 0
    if args.action == "after":
        info = state["jobs"][args.slug]
        if info.get("phase") in DONE_PHASES:
            return fail("job %s is finished" % args.slug)
        if args.drop not in after_jobs(args.slug):
            return fail("job %s does not wait for %s (after: %s)"
                        % (args.slug, args.drop, ", ".join(after_jobs(args.slug)) or "none"))
        plan = os.path.join(home, "jobs", args.slug, "plan.md")
        try:
            with io.open(plan, encoding="utf-8") as f:
                text = f.read()
        except (IOError, OSError):
            text = None
        if text is not None:
            head, sep, body = text.partition("\n---\n")
            lines = []
            for line in head.split("\n"):
                m = re.match(r"^after:(.*)$", line)
                if m:
                    left = [n.strip() for n in m.group(1).split(",") if n.strip() and n.strip() != args.drop]
                    if not left:
                        continue
                    line = "after: " + ", ".join(left)
                lines.append(line)
            tmp = "%s.%d.tmp" % (plan, os.getpid())
            with io.open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write("\n".join(lines) + sep + body)
            replace_file(tmp, plan)
        if info.get("after") == args.drop:
            info.pop("after")
        save_state(state)
        print(json.dumps({"job": args.slug, "after": after_jobs(args.slug)}, ensure_ascii=False))
        return 0
    if args.action == "rounds":
        info = state["jobs"][args.slug]
        if args.n is not None:
            if args.n < 1:
                return fail("a review round limit is 1 or more")
            info["review_rounds"] = args.n
            save_state(state)
        print(json.dumps({"job": args.slug, "round": review_round(args.slug),
                          "limit": info.get("review_rounds", load_config().get("max_review_rounds"))}))
        return 0
    if args.action == "output":
        return set_output(state, args)
    if args.action == "size":
        info = state["jobs"][args.slug]
        if info.get("size") == "research":
            return fail("job %s is research: it has no size" % args.slug)
        if args.size == "small" and experiment(info):
            return fail("job %s is an experiment: it is normal or large (only a worker pane waits for a long "
                        "run)" % args.slug)
        grows = SIZES.index(args.size) > SIZES.index(info.get("size") or "large")
        if info.get("phase") not in ("intake", "planning", "awaiting_approval") and not (
                grows and info.get("phase") in ("working", "reviewing")):
            return fail("job %s: in phase %s a size can only grow, before the merge"
                        % (args.slug, info.get("phase")))
        state["jobs"][args.slug]["size"] = args.size
        save_state(state)
        return 0
    if args.action == "defer":
        item = pending_item(args.slug, state["jobs"][args.slug])
        if item is None:
            return fail("job %s has nothing waiting for the user" % args.slug)
        state["jobs"][args.slug]["deferred"] = item["key"]
        save_state(state)
        return 0
    if args.action == "hold":
        info = state["jobs"][args.slug]
        if info.get("held"):
            return 0  # held already: cmd_job only retries the closes; no second notice, same held_at
        if (info.get("phase") in DONE_PHASES or info.get("phase") == "merging"
                or merge_state(args.slug)[0] in ("unverified", "verified")):
            return fail("job %s is %s: only a job before its merge can go on hold"
                        % (args.slug, info.get("phase")))
        merged, row = merge_state(args.slug)
        if merged == "rolled_back" and "already-in" in row["detail"].split():
            return fail("job %s blocks the merges into its git checkout (its failing code is in main): fix "
                        "it, or cancel and archive it, not hold it" % args.slug)
        early = info.get("size") == "research" or (
            info.get("phase") in ("intake", "planning", "awaiting_approval") and not info.get("worked"))
        if info.get("direct") and not early:
            return fail("job %s changes its folder in place (no git): cancel it, or let it finish, not hold it"
                        % args.slug)
        main = native_path(os.path.dirname(info.get("git_key") or ""))
        # a plan with a worktree names its branch: main's changes are the user's, not this job's
        uses_worktree = plan_worktree(args.slug)
        if not early and not uses_worktree and info.get("git_key") \
                and git_out(main, "rev-parse", "-q", "--verify", "refs/heads/oke/" + args.slug)[0] != 0:
            # no branch: its edits (a worker's or the PM's own) sit uncommitted in the main
            # checkout, in every other job's way. Not judged by main's state: a PM subagent the
            # hold cannot close may still write there after it
            return fail("job %s works in the main checkout (no branch oke/%s): cancel it, or let it "
                        "finish, not hold it" % (args.slug, args.slug))
        pm = (state["pms"].get(info.get("pm")) or {}).get("pm") or {}
        if pm.get("agent"):
            # queued before held is saved: a PM wait that sees the job gone sees this mail
            # and does not end with no_jobs. Not "Job X:", so the mail drop keeps it.
            queue_mail(pm["agent"], "oke", HOLD_NOTICE % args.slug)
        info.update(held=args.reason or "the user", held_at=time.time())
        save_state(state)
        return 0
    if args.action == "unhold":
        info = state["jobs"][args.slug]
        if not info.get("held"):
            return fail("job %s is not on hold" % args.slug)
        seq, header, mtime = pm_job_report(args.slug)
        if mtime and mtime > info.get("held_at", 0) and header.get("type") in ("question", "blocked", "failed"):
            # written while held (a child the hold closed looked "gone" to the PM): stale,
            # so neither a PM wait nor `pending` asks the user about it after the unhold
            report = os.path.join(pm_job_dir(args.slug), "report.md")
            replace_file(report, os.path.join(pm_job_dir(args.slug), "report-while-held.md"))
        info.pop("held", None)
        info.pop("held_at", None)
        info.pop("deferred", None)  # what it waits on the user for is asked again
        save_state(state)
        pm = (state["pms"].get(info.get("pm")) or {}).get("pm") or {}
        print(json.dumps({"job": args.slug, "pm": pm.get("agent"),
                          "pm_job_dir": pm_job_dir(args.slug).replace("\\", "/")}))
        return 0
    if args.action == "cancel":
        info = state["jobs"][args.slug]
        if cancel_problem(state, args.slug):
            return fail(cancel_problem(state, args.slug))
        pm = (state["pms"].get(info.get("pm")) or {}).get("pm") or {}
        if pm.get("agent"):
            # queued before the phase changes: the PM's wait delivers it before it can say no_jobs
            queue_mail(pm["agent"], "oke", "Job %s was cancelled by the user. Forget it, close what is "
                       "left of it, and write no report for it." % args.slug)
        # only a job at its merge waits on it now (after_broken asks the user); one still
        # working sees it at its merge, and a folder job past its start never waits
        tell_after_waiters(state, args.slug, "was cancelled: the orchestrator asks the user; keep the job waiting",
                           ("awaiting_merge", "merging"))
        info.update(phase="failed", phase_at=time.time(), cancelled=True)
        save_state(state)  # nothing may start for this job from here on
        return 0
    if args.action == "archive" and archive_refusal(args.slug, state):
        return fail(archive_refusal(args.slug, state))
    if args.action == "archive":
        src = os.path.join(home, "jobs", args.slug)
        dst = os.path.join(home, "archive", "%s-%s" % (args.slug, time.strftime("%Y%m%d-%H%M%S")))
        key = state["jobs"][args.slug].get("pm")
        # a removed PM has no wiki to count in or curate: none is made again for it
        hint = record_wiki_use(src, key) if key in state["pms"] and os.path.isdir(src) else None
        if os.path.isdir(src):
            if not os.path.isdir(os.path.dirname(dst)):
                os.makedirs(os.path.dirname(dst))
            shutil.move(src, dst)
            # the children's settings copies hold the user's env tokens: no need to keep them
            for name in ("lean-settings.json", "bg-settings.json", "mcp.json"):
                for path in glob.glob(os.path.join(glob.escape(dst), "**", name), recursive=True):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            try:  # what it was (direct, research, done): a job after it still asks
                write_json_atomic(os.path.join(dst, "job.json"), state["jobs"][args.slug])
            except (IOError, OSError):
                pass
        info_was = state["jobs"].pop(args.slug)
        save_state(state)
        if key:  # a later job of the same name must not inherit its agents
            children = read_children(key)
            kept = dict((n, r) for n, r in children.items() if r.get("job") != args.slug)
            if kept != children:
                write_json_atomic(children_path(key), kept)
        print(dst)
        wt = (info_was.get("worktree") or {}).get("path")
        if info_was.get("no_change") and wt and info_was.get("git_key"):
            args.drop_worktree = (dst, info_was)  # removed after the state lock: runtime and git are slow
        elif job_output(info_was) == "code" and info_was.get("git_key") and info_was.get("phase") == "done":
            args.drop_branch = info_was  # merged: its branch goes too, after the lock
        if experiment(info_was) and wt:
            print("kept_worktree: %s (the experiment's outputs: remove it with `$OKE job clean %s` when the user "
                  "says so)" % (wt, args.slug))
        if hint:
            print(hint)
        return 0
    return fail("unknown action")


CURATE_EVERY = 10  # jobs logged in a wiki since its last curation


def cancel_children(job, key):
    """After `job cancel` closed the job: close its children's panes and drop its mail,
    outside the state lock (runtime calls and the delivery lock can be slow)."""
    closed, left, refused = close_children(job, key)
    print(json.dumps({"job": job, "closed": closed, "not_closed": left, "refused": refused,
                      "dropped_messages": drop_job_mail(job, key) if key else 0}))
    return 0


def close_children(job, key):
    """Close the panes of `job`'s children that are still theirs: ([closed], [not closed:
    the runtime did not answer], [refused to close: a retry does not help])."""
    closed, left, refused = [], [], []
    for name, rec in sorted(read_children(key).items() if key else []):
        if rec.get("job") != job or not rec.get("pane"):
            continue
        try:
            # a pane id comes back after a herdr restart: close it only while it is still this child's
            terminal = runtime().place_id(rec["pane"]) if rec.get("terminal") else None
            if terminal and terminal == rec["terminal"]:
                try:
                    runtime().close(rec["pane"])
                except HerdrError as e:
                    if e.code == "registry_locked":
                        left.append(name)  # a passing lock: a retry closes it
                        continue
                    if e.code in ("herdr_unavailable", "agent_not_found", "pane_not_found"):
                        raise  # an outage, or it went away meanwhile: handled below
                    # it is still there and refused (a worktree group root): keep its record
                    refused.append(name)
                    continue
                closed.append(name)
            elif rec.get("terminal") and not terminal:
                # herdr's place_id answers None in an outage too: ask once more, so an
                # outage keeps the record (place_exists raises it) and a closed pane drops it
                runtime().place_exists(rec["pane"])
        except HerdrError as e:
            if e.code == "herdr_unavailable":
                left.append(name)  # keep its record: `job cancel` again closes it later
                continue
        except OSError:
            pass  # already gone
        update_record(rec.get("role") or "worker", key, name, {"pane": None, "terminal": None})
    return closed, left, refused


HOLD_NOTICE = ("Job %s is on hold: the user put it on hold. Stop working on it, start none of its "
               "agents, leave its worktree, and write no report for it until a message says it is back.")


def drop_job_mail(job, key, also=None):
    """Move the queued messages about `job` out of its PM's and children's inboxes, so a
    later agent never acts on a closed job (OMC deletes a finished team's mailboxes)."""
    names = [key] + [n for n, r in read_children(key).items() if r.get("job") == job]
    mark = ("Job %s:" % job,) + ((also,) if also else ())
    dropped = 0
    for agent in names:
        lock = "deliver-" + agent
        for _ in range(50):
            if try_lock(lock):
                break
            time.sleep(0.1)
        else:
            continue  # a delivery holds it: what it delivers is gone from the inbox anyway
        try:
            for path, text in inbox_messages(agent):
                if text.split(": ", 1)[-1].startswith(mark) or agent != names[0]:
                    done = os.path.join(inbox_dir(agent), "read")
                    os.makedirs(done, exist_ok=True)
                    try:
                        os.replace(path, os.path.join(done, os.path.basename(path)))
                        dropped += 1
                    except OSError:
                        pass  # delivered a moment ago
        finally:
            unlock(lock)
    return dropped


def shared_wiki():
    path = os.path.join(load_config()["wiki_path"], "_shared")
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def record_wiki_use(job_dir, key):
    """Count what the job's reports said about wiki pages (`## wiki`); a page is the
    PM's if its wiki has one by that name, else the shared wiki's. Says when the PM's
    wiki is due for a curation."""
    entries = []
    for name in sorted(os.listdir(job_dir)):
        try:
            with io.open(os.path.join(job_dir, name, "report.md"), encoding="utf-8") as f:
                entries += wiki.report_entries(f.read())
        except (IOError, OSError, UnicodeDecodeError):
            pass
    repo, shared = wiki_dir(key), shared_wiki()
    wiki.record(repo, [e for e in entries if os.path.isfile(os.path.join(repo, e[0]))])
    wiki.record(shared, [e for e in entries if not os.path.isfile(os.path.join(repo, e[0]))])
    try:
        with io.open(os.path.join(repo, "log.md"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (IOError, OSError, UnicodeDecodeError):
        return None
    jobs = set()
    for line in reversed(lines):
        m = re.match(r"^\S+ (\S+): (curated)?", line)
        if not m:
            continue
        if m.group(1) == "curate" or m.group(2):  # a curation job logs "curated: .."
            break
        jobs.add(m.group(1))
    if len(jobs) >= CURATE_EVERY:
        return ("curate: the wiki of PM %s took %d jobs since its last curation: give it `kind: curate`"
                % (key, len(jobs)))
    return None


def set_output(state, args):
    """`job output <job> code|result [--no-change]` (state is under the lock)."""
    info = state["jobs"][args.slug]
    if info.get("held"):
        return fail("job %s is on hold: leave it until a message says it is back" % args.slug)
    if info.get("size") == "research":
        return fail("job %s is research: its output is its answer (result)" % args.slug)
    if no_path(args.slug, info) and not args.no_change:
        return fail(no_path(args.slug, info))
    if info.get("direct") and not args.no_change:
        return fail("job %s changes a folder outside git: its output is the files" % args.slug)
    phase = info.get("phase")
    if not args.no_change:
        if args.output == job_output(info):  # the same output again (every plan records it)
            out = {"job": args.slug, "output": args.output}
            if args.output == "result" and info.get("size") == "small":
                info["size"] = out["size"] = "normal"  # an experiment's run needs a worker pane
                save_state(state)
            print(json.dumps(out))
            return 0
        # a replan may change it while nothing was made yet that the other output cannot use
        if phase not in GATE_PHASES or merge_state(args.slug)[0] != "none" \
                or ((info.get("worked") or info.get("worktree")) and job_changes(args.slug, info)):
            return fail("job %s has worked on its plan (%s): its output stays %s. Report a question: the user "
                        "decides (cancel it and start a new job)" % (args.slug, phase, job_output(info)))
        info["output"] = args.output
        out = {"job": args.slug, "output": args.output}
        if args.output == "result" and info.get("size") == "small":
            info["size"] = out["size"] = "normal"  # an experiment's run needs a worker pane
        save_state(state)
        print(json.dumps(out))
        return 0
    # the user ends a job that changed nothing: only the orchestrator, on the user's answer
    pane = runtime().whoami()
    if pane:
        try:
            me = whoami(state, pane)[0]
        except HerdrError:
            me = None
        if me and me[0] != "orchestrator":
            return fail("only the orchestrator ends a job with no change, on the user's answer: report a "
                        "question")
    if args.output != "result":
        return fail("--no-change ends the job with output result")
    if phase in DONE_PHASES or phase in ("awaiting_merge", "merging") \
            or merge_state(args.slug)[0] != "none":
        return fail("job %s is %s: too late to end it with no change" % (args.slug, phase))
    # a folder outside git has nothing to compare with: the user's word is the check
    changed = None if info.get("direct") or not info.get("git_key") else job_changes(args.slug, info)
    if changed:
        return fail("job %s changed something (%s): it does not end with no change; merge it, or cancel it"
                    % (args.slug, changed))
    if info.get("pm") in state["pms"] and not wiki.logged(wiki_dir(info["pm"]), args.slug, info.get("created")):
        # the user ended it: `job phase done` needs a wiki line, and a job that changed nothing
        # rarely taught one. Written here, so done never has to go back to the PM
        wiki.log(wiki_dir(info["pm"]), args.slug, "ended with no change")
    info["no_change"] = True
    if not info.get("direct"):  # a git job, or one that never found its path
        info["output"] = "result"
    pm = (state["pms"].get(info.get("pm")) or {}).get("pm") or {}
    if pm.get("agent"):
        queue_mail(pm["agent"], "oke", "Job %s ended with no change (the user agreed): it is closed. Close what "
                   "is left of it, drop it from your board, and write no report for it." % args.slug)
    save_state(state)
    ledger_add(args.slug, "orchestrator", "-", "no-change", "accept", "the user ended it with no change")
    print(json.dumps({"job": args.slug, "output": job_output(info), "no_change": True}))
    return 0


def job_changes(job, info):
    """What a git job changed so far, in words, or None: commits on its branch beyond the
    point it started from, tracked changes, new files that look like work."""
    wt = (info.get("worktree") or {}).get("path")
    main = native_path(repo_dir(info["git_key"]))
    # the main checkout counts only when the job worked there (a job from before the flag:
    # it worked without a worktree in its plan); otherwise its changes are the user's or another job's
    in_main = info.get("worked_in_main") or (
        "worked_in_main" not in info and info.get("worked") and not wt and not plan_worktree(job))
    if not wt and not in_main:
        return None  # it never worked, or its work belongs in a worktree it does not have yet
    cwd = native_path(wt) if wt else main
    for where in ([cwd, main] if wt and in_main else [cwd]):
        code, dirty = git_out(where, "status", "--porcelain", "-uno")
        if code != 0:
            return "git status failed in %s" % where
        if where == main:  # the main checkout: the user's edits from before the job are not its change
            before = set(info.get("main_before") or [])
            dirty = "\n".join(l for l in dirty.splitlines() if l.strip() not in before)
        if dirty:
            # git_out strips the output: the first line lost its leading space
            return "changed files%s: " % (" in the main checkout" if where != cwd else "") + \
                ", ".join(l[2:].strip() for l in dirty.splitlines()[:5])
    if wt:
        loose = untracked_work(cwd)
        if loose:
            return "new files: " + ", ".join(loose[:5])
    branch = "refs/heads/oke/" + job
    point = base_point(info, cwd)
    tips = [git_out(cwd, "rev-parse", "HEAD")[1]] if wt else []
    if branch and git_out(main, "rev-parse", "-q", "--verify", branch)[0] == 0:
        tips.append(git_out(main, "rev-parse", branch)[1])
    for tip in tips:
        # the tree counts, not the commits: a reverted change leaves commits but no change
        if subprocess.call(["git", "-C", main, "diff", "--quiet", point or "HEAD", tip],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
            return "commits on its branch change files"
    return None


def wiki_verified(job, info):
    """Lessons count only from work that was checked (Hermes: learning from unchecked
    sessions taught wrong rules)."""
    if info.get("size") == "research" or info.get("no_change"):
        return True
    if never_merges(info):
        review = latest_report(os.path.join(oke_home(), "jobs", job), r"-rv\d*$")
        return bool(review) and review_verdict(review[1]) == "pass"
    return merge_state(job)[0] == "verified"


def cmd_wiki(args):
    if args.action == "read":
        return wiki_locked(args)
    with locked():  # .usage.json is read-modify-write, and `job archive` writes it too
        return wiki_locked(args)


def wiki_locked(args):
    state = load_state()
    label = "curate"
    if args.job:
        if args.job not in state["jobs"]:
            return fail("unknown job: %s" % args.job)
        label = args.job
    if args.shared:
        where = shared_wiki()
    elif args.job:
        if not state["jobs"][args.job].get("pm"):
            return fail("job %s has no PM" % args.job)
        where = wiki_dir(state["jobs"][args.job]["pm"])
    elif args.pm:
        if args.pm not in state["pms"]:
            return fail("no PM %s" % args.pm)
        where = wiki_dir(args.pm)
    else:
        return fail("name the wiki: --job <job>, --pm <name> or --shared")
    try:
        if args.action == "read":
            out = wiki.read(where, args.page)
            text = out.pop("text")
            print(json.dumps(out))
            if text:
                sys.stdout.write(text if text.endswith("\n") else text + "\n")
            return 0
        if args.action == "write":
            if args.job and args.page != "index.md" and not wiki_verified(args.job, state["jobs"][args.job]):
                return fail("job %s is not verified yet (merge verified, the review of a job that never "
                            "merges passed, research, or an end with no change): write its lessons after that"
                            % args.job)
            with io.open(args.source, encoding="utf-8-sig") as f:
                text = f.read()
            if args.shared:
                low = text.replace("\\", "/").lower()
                for checkout in set(j["path"].lower() for j in state["jobs"].values()
                                    if j.get("path") and j.get("phase") not in DONE_PHASES):
                    bash = re.sub(r"^([a-z]):/", r"/\1/", checkout)  # c:/x as Git Bash writes it: /c/x
                    if len(checkout) > 3 and (checkout in low or bash in low):
                        return fail("the shared wiki holds what is true beyond one PM: drop the "
                                    "path %s" % checkout)
            print(json.dumps(wiki.write(where, args.page, text, args.base, label, args.note, args.dup_ok,
                                        merging=args.merging or ())))
            return 0
        if args.action == "log":
            wiki.log(where, label, args.note)
            return 0
        if args.action == "status":
            print(json.dumps(wiki.status(where), ensure_ascii=False, indent=1))
            return 0
        if args.action == "prune":
            print(json.dumps({"archived" if args.apply else "due": wiki.prune(where, args.apply)}))
            return 0
        if args.action == "archive":
            wiki.archive(where, args.page, args.reason, label)
            return 0
        if args.action in ("pin", "unpin"):
            wiki.pin(where, args.page, args.action == "pin")
            return 0
    except (wiki.WikiError, IOError, OSError) as e:
        return fail(str(e))
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


def queue_mail(agent, sender, text):
    d = inbox_dir(agent)
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    _sent[0] += 1
    path = os.path.join(d, "%.6f-%d-%d.txt" % (time.time(), os.getpid(), _sent[0]))
    with io.open(path + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        f.write(u"%s: %s\n" % (sender, text))
    os.replace(path + ".tmp", path)
    set_mail_flag(agent, True)


# The agent's mod looks only for this file (no python every tick): it exists while the
# inbox may hold mail. A stale one costs one `take-mail` call, which removes it.
MAIL_FLAG = ".new"


def lower_mail_flag(agent):
    """Off when the inbox is empty. Off first, then look: a send in between (it writes
    its message before the flag) raises it again, or this look sees its message."""
    if inbox_messages(agent):
        return
    set_mail_flag(agent, False)
    if inbox_messages(agent):
        set_mail_flag(agent, True)


def set_mail_flag(agent, on):
    flag = os.path.join(inbox_dir(agent), MAIL_FLAG)
    try:
        if on:
            io.open(flag, "a").close()
        elif os.path.exists(flag):
            os.remove(flag)
    except (IOError, OSError):
        pass  # on: the waits still deliver; off: one more take-mail look


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
    child_job = state["jobs"].get(found[3].get("job")) if found[0] == "reviewer" else None
    if child_job is not None:
        header = read_header(os.path.join(agent_folder(*found), "assignment.md")) or {}
        seq = int(header["seq"]) if str(header.get("seq", "")).isdigit() else 0
        over = over_rounds(found[3]["job"], child_job, load_config(), seq)
        if over:
            return fail(over)
    sender = "user"
    pane = runtime().whoami()
    if pane:
        try:
            me, _ = whoami(state, pane)
        except HerdrError:
            me = None
        # a pane OKe does not know is not proof of the user: say so, never "user"
        sender = me[1] if me else "unverified (pane %s)" % pane
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
    queue_mail(found[1], sender, text)
    print(json.dumps({"agent": found[1], "queued": True}))
    return 0


# ---------------------------------------------------------------- peers

ANSWER_HINT = "oke peer answer %s <no impact | impact: ...>"


def peers_path(job):
    return os.path.join(oke_home(), "jobs", job, "peers.json")


def read_peers(job):
    data = read_json(peers_path(job), {})
    return data if isinstance(data, dict) else {}


def peer_waiting(state, job):
    """The PMs that still owe `job` an answer. Left out: a PM that has no open
    job left (done, archived, cancelled or held: nothing revives or watches it), and one
    the user was asked about already (nudged 2: its late answer still counts)."""
    waiting = read_peers(job).get("waiting") or {}
    return dict((pm, e) for pm, e in waiting.items()
                if isinstance(e, dict) and e.get("nudged", 0) < 2 and not e.get("closed")
                and open_jobs(state, e.get("pm")))


def peer_ruling(state, job):
    """The asked PMs the user must rule on for `job`: they never answered, and the job's PM
    has had no new assignment since (ruled at its current seq). `pending` lists them."""
    seq = state["jobs"].get(job, {}).get("sent_seq")
    return sorted(pm for pm, e in (read_peers(job).get("waiting") or {}).items()
                  if isinstance(e, dict) and not e.get("closed") and "ruled_seq" in e and e["ruled_seq"] == seq
                  and open_jobs(state, e.get("pm")))


def peers_hold(state, job):
    """The asking PM is waiting, not stopped short: an answer is still owed, or the user is
    being asked about a missing one."""
    return bool(peer_waiting(state, job) or peer_ruling(state, job))


def end_peer_talk(job):
    """Nobody waits for these answers any more; a late one is still logged and mailed."""
    data = read_peers(job)
    if data.get("waiting"):
        for e in data["waiting"].values():
            if isinstance(e, dict):
                e["closed"] = True
        write_json_atomic(peers_path(job), data)


def owed_answers(state, agent):
    """[(job, round)] of every open job that waits on an answer from `agent`."""
    return [(job, read_peers(job).get("round", 1)) for job in sorted(open_jobs(state))
            if agent in peer_waiting(state, job)]


def peer_nudge(agent, job):
    """True: `agent` was reminded of the answer it owes `job` (first time). False: it was
    reminded already, so the user is asked (once: nudged 2 takes it off `owed_answers`).
    None: nothing is owed any more (it answered, or a new round replaced the list)."""
    with locked():
        data = read_peers(job)
        entry = (data.get("waiting") or {}).get(agent)
        if not isinstance(entry, dict) or entry.get("nudged", 0) >= 2:
            return None
        first = not entry.get("nudged")
        entry["nudged"] = 1 if first else 2
        if not first:
            entry["ruled_seq"] = (load_state()["jobs"].get(job) or {}).get("sent_seq")
        write_json_atomic(peers_path(job), data)
        if first:
            queue_mail(agent, "oke", "Answer the peer ask of job %s (round %d): %s"
                       % (job, data.get("round", 1), ANSWER_HINT % job))
    return first


def peer_log(job, line):
    with io.open(os.path.join(oke_home(), "jobs", job, "peers.md"), "a", encoding="utf-8", newline="\n") as f:
        f.write(line + u"\n")


def cmd_peer(args):
    text = " ".join(" ".join(args.text).split())
    if not text and args.action != "status":
        return fail("empty message")
    if text.startswith("/"):
        return fail("a message must not start with '/': Git Bash rewrites it into a Windows path")
    state = load_state()
    info = state["jobs"].get(args.job)
    if info is None:
        return fail("unknown job: %s" % args.job)
    if args.action == "status":
        raw = read_peers(args.job).get("waiting") or {}
        waiting = peer_waiting(state, args.job)
        ruling = set(peer_ruling(state, args.job))  # the user has not decided yet
        ruled = set(pm for pm, e in raw.items()
                    if isinstance(e, dict) and (e.get("nudged", 0) >= 2 or e.get("closed"))) - ruling
        print(json.dumps({"round": read_peers(args.job).get("round", 0), "waiting": sorted(waiting),
                          "ruling": sorted(ruling), "dropped": sorted(set(raw) - set(waiting) - ruled - ruling),
                          "no_answer": sorted(ruled)}))
        return 0
    if info.get("phase") in DONE_PHASES:
        return fail("job %s is closed (%s): nothing to ask or answer" % (args.job, info.get("phase")))
    pane = runtime().whoami()
    me = whoami(state, pane)[0] if pane else None
    if not me or me[0] != "pm":
        return fail("only a PM can use oke peer")
    name = me[1]
    with locked():
        state = load_state()
        if args.action == "ask":
            if me[2] != info.get("pm"):
                return fail("job %s belongs to another PM" % args.job)
            n = read_peers(args.job).get("round", 0) + 1
            waiting = {}
            for key in all_keys(state):
                pm = (state["pms"].get(key) or {}).get("pm") or {}
                if key != me[2] and pm.get("agent") and open_jobs(state, key):
                    waiting[pm["agent"]] = {"pm": key, "nudged": 0}
            write_json_atomic(peers_path(args.job), {"round": n, "asker": name, "waiting": waiting})
            peer_log(args.job, u"## round %d (%s)" % (n, time.strftime("%Y-%m-%d %H:%M")))
            peer_log(args.job, u"- ask (%s): %s" % (name, text))
            for pm in sorted(waiting):
                queue_mail(pm, name, "peer ask (job %s, round %d): %s. Answer with: %s"
                           % (args.job, n, text, ANSWER_HINT % args.job))
            print(json.dumps({"round": n, "asked": sorted(waiting)}))
            return 0
        if not isinstance((read_peers(args.job).get("waiting") or {}).get(name), dict):
            return fail("%s was not asked about job %s (or answered already)" % (name, args.job))
        data = read_peers(args.job)
        data["waiting"].pop(name, None)
        write_json_atomic(peers_path(args.job), data)
        peer_log(args.job, u"- answer (%s): %s" % (name, text))
        asker = data.get("asker") or ((state["pms"].get(info.get("pm")) or {}).get("pm") or {}).get("agent")
        if asker:
            queue_mail(asker, name, "Job %s: peer answer from %s (round %d): %s"
                       % (args.job, name, data.get("round", 1), text))
        print(json.dumps({"waiting": sorted(peer_waiting(state, args.job))}))
        return 0


def deliver(agent):
    """Type the queued messages into `agent`, which the caller saw idle. They leave the
    inbox only after herdr took them, so a failed prompt loses nothing. A runtime that
    cannot type (bg) answers "ask_caller": the text is returned for the waiting Claude
    session to send with SendMessage."""
    if not inbox_messages(agent):
        return None
    lock = "deliver-" + agent
    if not try_lock(lock):
        return None  # another wait delivers right now
    try:
        msgs = inbox_messages(agent)
        if not msgs:
            return None
        msgs = fit_mail(msgs) or msgs[:1]  # the rest goes on the next delivery
        text = "OKe inbox: " + " | ".join(t for _, t in msgs)
        took = runtime().prompt(agent, text)
        if not took:
            return False
        # shortcut: with ask_caller the mail counts as read once handed out; a caller
        # compacted before its SendMessage loses it (the file stays in read/)
        mark_read(agent, msgs)
        return text if took == "ask_caller" else True
    finally:
        unlock(lock)


MAIL_CHARS = 16000  # one delivery's text: a command line (Windows: 32k) must hold it


def fit_mail(msgs):
    """The oldest messages whose text fits in one delivery (none when the first is too long)."""
    out, size = [], 0
    for m in msgs:
        size += len(m[1]) + 3
        if size > MAIL_CHARS:
            break
        out.append(m)
    return out


def mark_read(agent, msgs):
    """Move delivered inbox messages to read/ (kept a week, for a look back)."""
    if not msgs:
        return
    done = os.path.join(inbox_dir(agent), "read")
    os.makedirs(done, exist_ok=True)
    for path, _ in msgs:
        try:
            if os.path.exists(path):
                replace_file(path, os.path.join(done, os.path.basename(path)))
        except OSError:
            pass  # moved already (a job cancel drops its mail): the delivery still happened
    lower_mail_flag(agent)
    for old in os.listdir(done):
        try:
            if os.path.getmtime(os.path.join(done, old)) < time.time() - 7 * 86400:
                os.remove(os.path.join(done, old))
        except OSError:
            pass


def cmd_take_mail(args):
    """The agent's own mod takes its inbox after a turn ended, while the runtime may
    still call the agent working (a background subagent or task runs). Prints
    {"text": ..., "names": [...]} or {"text": null}. --give-back puts taken mail back
    when the mod could not submit it."""
    state = load_state()
    found = find_target(state, args.agent)
    rec = found[3] if found else {}
    if not found or found[0] == "orchestrator" or not args.pane or \
            args.pane not in (rec.get("pane"), "session:%s" % rec.get("session")):
        print(json.dumps({"text": None, "why": "not this pane's agent"}))
        return 0
    if args.give_back:
        done = os.path.join(inbox_dir(args.agent), "read")
        for name in args.give_back:
            if os.path.basename(name) != name or not name.endswith(".txt"):
                continue
            try:
                replace_file(os.path.join(done, name), os.path.join(inbox_dir(args.agent), name))
            except OSError:
                pass
        set_mail_flag(args.agent, True)
        print(json.dumps({"text": None, "given_back": len(args.give_back)}))
        return 0
    if retry_pending(os.path.join(agent_folder(*found), "api-retry.json")):
        print(json.dumps({"text": None, "why": "api retry pending"}))
        return 0
    lock = "deliver-" + args.agent
    if not try_lock(lock):
        print(json.dumps({"text": None, "why": "another delivery runs"}))
        return 0
    try:
        msgs = inbox_messages(args.agent)
        if not msgs:
            lower_mail_flag(args.agent)
            print(json.dumps({"text": None}))
            return 0
        msgs = fit_mail(msgs) or msgs[:1]  # the rest on the next look
        mark_read(args.agent, msgs)  # clears the flag when nothing is left
        print(json.dumps({"text": "OKe inbox: " + " | ".join(t for _, t in msgs),
                          "names": [os.path.basename(p) for p, _ in msgs]}, ensure_ascii=False))
        return 0
    finally:
        unlock(lock)


# ---------------------------------------------------------------- pending

PENDING_RANKS = {"unsent": 0, "unassigned": 0, "split": 0, "after_ready": 0, "after_broken": 1, "report_done": 0, "no_change": 0, "silent": 1, "question": 1, "blocked": 1, "failed": 1,
                 "peer": 1, "plan": 2, "merge": 3}
ASKING_PHASES = ("planning", "working", "reviewing", "merging")


WORK_KINDS = ("execute", "research", "curate")  # assignments that start a job's work


def assignment_kind(job):
    """The `kind:` of a job's current PM assignment (its first line under `## Instruction`), or None."""
    try:
        with io.open(os.path.join(pm_job_dir(job), "assignment.md"), encoding="utf-8") as f:
            text = f.read()
    except (IOError, OSError, ValueError):
        return None
    body = text.split("\n---", 1)[1] if "\n---" in text else ""
    m = re.search(r"^kind:\s*([a-z]+)", body, re.M)
    return m.group(1) if m else None


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
    if phase in DONE_PHASES or job.get("held"):
        return None  # a held job asks nothing until `job unhold`
    seq, report, mtime = pm_job_report(name)
    rtype = report.get("type") if seq is not None and report.get("seq") == seq else None
    ruling = peer_ruling(load_state(), name) if os.path.exists(peers_path(name)) else []
    at_gate = gated(job)
    waits, broken = after_blocks(load_state(), name) \
        if at_gate or phase in ("awaiting_merge", "merging") else ([], [])
    if job.get("no_change"):
        # the user ended it with no change and a compaction came before `job phase done`
        kind, since, mark = "no_change", job.get("phase_at"), "end"
    elif at_gate:
        # held at its start: what an agent reports or a peer asks still shows; the gate
        # itself shows only once it can move, or never will by itself
        if seq is not None and job.get("sent_seq") != seq:
            kind, since, mark = "unsent", job.get("phase_at"), seq
        elif rtype in ("question", "blocked", "failed"):
            kind, since, mark = rtype, mtime, seq
        elif ruling:
            kind, since, mark = "peer", os.path.getmtime(peers_path(name)), "%s:%s" % (
                job.get("sent_seq"), ",".join(ruling))
        elif broken:
            kind, since, mark = "after_broken", job.get("phase_at"), broken[0][0]
        elif waits:
            return None  # approved, waiting for an after-job: nothing to ask yet
        else:
            kind, since, mark = "after_ready", job.get("phase_at"), "start"
    elif phase == "working" and "start_seq" in job and job["start_seq"] == seq:
        # phase set first (approval, after_ready, a curate start), its assignment never
        # written: the report is not the work's, and this job must not look done
        kind, since, mark = "unassigned", job.get("phase_at"), seq
    elif seq is None and phase == "intake" and job.get("split_from"):
        kind, since, mark = "split", job.get("phase_at"), job.get("split_from")  # a PM split it off
    elif seq is not None and job.get("sent_seq") != seq:
        kind, since, mark = "unsent", job.get("phase_at"), seq  # written, never sent
    elif rtype in ("question", "blocked", "failed"):
        kind, since, mark = rtype, mtime, seq
    elif rtype not in REPORT_TYPES and is_silent(job, seq):
        kind, since, mark = "silent", mtime, seq
    elif ruling:
        # the PMs are in the mark: a later missing answer is a new item, even after a deferral
        kind, since, mark = "peer", os.path.getmtime(peers_path(name)), "%s:%s" % (job.get("sent_seq"), ",".join(ruling))
    elif broken:  # at its merge: an after-job that will never finish is the user's call
        kind, since, mark = "after_broken", job.get("phase_at"), broken[0][0]
    elif rtype == "done" and phase in ASKING_PHASES:
        kind, since, mark = "report_done", mtime, seq
    elif phase == "awaiting_approval":
        kind, since, mark = "plan", job.get("phase_at"), job.get("phase_at")
    elif phase == "awaiting_merge":
        kind, since, mark = "merge", job.get("phase_at"), job.get("phase_at")
    else:
        return None
    key = "%s:%r" % (kind, mark)
    item = {"job": name, "kind": kind, "rank": PENDING_RANKS[kind], "since": since or 0,
            "key": key, "deferred": job.get("deferred") == key}
    if ruling and kind in ("peer", "question", "blocked", "failed"):
        item["peers"] = ruling  # the answer that settles this item must settle these too
    if kind == "after_ready":
        left = after_jobs(name)
        item["text"] = ("job %s may start: after-job %s %s" % (name, ", ".join(left), AFTER_FINISHED) if left
                        else "job %s may start: its after-jobs were dropped" % name)
    if kind == "after_broken":
        other, why = broken[0]
        item["text"] = "job %s cannot %s: %s; ask the user: drop it (`$OKE job after %s --drop %s`) or cancel %s" % (
            name, "start" if at_gate else "merge", why, name, other, name)
        item["after"], item["phase"] = other, phase  # merging: the PM's merge waits, tell it after a drop
    if kind == "no_change":
        item["text"] = ("job %s ended with no change (the user agreed): do not ask again; finish it as the "
                        "Flow row says (wiki, `job phase done`, archive)" % name)
    if kind == "unassigned":
        item["text"] = ("job %s is in phase working but its assignment for that (execute, research or curate) "
                        "was never written: write and send it now" % name)
    if kind == "split":
        item["text"] = "PM %s split job %s: new job %s (%s)" % (job.get("pm"), job["split_from"], name,
                                                                job.get("path"))
        item.update(pm_fields(job.get("pm"), name))  # for the new job's PM assignment header
    return item


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
    new.add_argument("--pm", required=True, help="the PM the user picked")
    new.add_argument("--path", help="the folder the job changes, only when the user said it")
    new.add_argument("--research", action="store_true", help="look and report: no change, no merge")
    jb = job_sub.add_parser("brief", help="the plan as the user reads it before approving")
    jb.add_argument("slug", nargs="?")
    jb.add_argument("--json", action="store_true", help="every job awaiting approval, as JSON")
    jp = job_sub.add_parser("path", help="the PM sets the job's one path while planning")
    jp.add_argument("slug")
    jp.add_argument("path")
    sp = job_sub.add_parser("split", help="the PM splits work on another path into a new job")
    sp.add_argument("slug")
    sp.add_argument("new")
    sp.add_argument("--path", required=True)
    sp.add_argument("--after", help="the new job starts (folder) or merges (git) only after this job")
    phase = job_sub.add_parser("phase")
    phase.add_argument("slug")
    phase.add_argument("phase")
    job_sub.add_parser("cancel", help="the user gave the job up: close it, its children and its mail").add_argument("slug")
    hold = job_sub.add_parser("hold", help="the user sets the job aside: its agents close, nothing revives them")
    hold.add_argument("slug")
    hold.add_argument("--reason")
    job_sub.add_parser("unhold", help="bring a held job back").add_argument("slug")
    archive = job_sub.add_parser("archive")
    archive.add_argument("slug")
    defer = job_sub.add_parser("defer")
    defer.add_argument("slug")
    af = job_sub.add_parser("after", help="drop a job from a job's after: (the user's call)")
    af.add_argument("slug")
    af.add_argument("--drop", required=True)
    rounds = job_sub.add_parser("rounds", help="show a job's review round and limit, or set the limit")
    rounds.add_argument("slug")
    rounds.add_argument("n", nargs="?", type=int)
    output = job_sub.add_parser("output", help="what the job hands over: code (a merge) or result "
                                "(an answer or a run's outputs, no merge)")
    output.add_argument("slug")
    output.add_argument("output", choices=OUTPUTS)
    output.add_argument("--no-change", action="store_true",
                        help="the orchestrator, on the user's answer: end a job that changed nothing")
    job_sub.add_parser("clean", help="remove an archived experiment's kept worktree").add_argument("slug")
    size = job_sub.add_parser("size", help="the job's size, which the PM decides while planning")
    size.add_argument("slug")
    size.add_argument("size", choices=SIZES)
    merge = job_sub.add_parser("merge")
    merge.add_argument("slug")
    job_sub.add_parser("tree", help="the tree a review of a job without a worktree names").add_argument("slug")
    verify = job_sub.add_parser("verify")
    verify.add_argument("slug")
    verify.add_argument("command", nargs="?", help="the plan's Check command, as one quoted string")
    verify.add_argument("--accept", help="the user accepts the merge without (another) check: why")
    verify.add_argument("--skip", help="the PM judged that the files other merges changed since the "
                                       "check cannot change its result: why")
    verify.add_argument("--run", action="store_true", help="run the Check command even though other merges "
                                                          "changed the code since the check")
    job.set_defaults(func=cmd_job)

    wk = sub.add_parser("wiki", help="read and write a wiki: capped pages, read before write, a log line per job")
    wk_sub = wk.add_subparsers(dest="action")
    wk_sub.required = True
    for name in ("read", "write", "log", "status", "prune", "archive", "pin", "unpin"):
        p = wk_sub.add_parser(name)
        if name in ("read", "write", "archive", "pin", "unpin"):
            p.add_argument("page")
        p.add_argument("--job", help="the wiki of the job's PM; with write and log, the job the lines are for")
        p.add_argument("--pm", help="the wiki of this PM (curation)")
        p.add_argument("--shared", action="store_true", help="the shared wiki (only the orchestrator writes it)")
        if name == "write":
            p.add_argument("--from", dest="source", required=True, help="a file holding the whole new page")
            p.add_argument("--base", required=True, help="the base `oke wiki read` printed (new for a new page)")
            p.add_argument("--note", required=True, help="what changed, for log.md")
            p.add_argument("--dup-ok", action="store_true", help="lines that only look like another page's")
            p.add_argument("--merging", action="append", help="a page merged into this one (archived next)")
        if name == "log":
            p.add_argument("--note", required=True)
        if name == "prune":
            p.add_argument("--apply", action="store_true", help="move them; without it only list them")
        if name == "archive":
            p.add_argument("--reason", required=True)
    wk.set_defaults(func=cmd_wiki)

    bl = sub.add_parser("baseline", help="record the plan's Check command failures before the work starts")
    bl.add_argument("job")
    bl.set_defaults(func=cmd_baseline)
    ck = sub.add_parser("check", help="run the Check command on the job's work and judge it against the baseline")
    ck.add_argument("job")
    ck.set_defaults(func=cmd_check)
    ledger = sub.add_parser("ledger")
    ledger_sub = ledger.add_subparsers(dest="action")
    ledger_sub.required = True
    show = ledger_sub.add_parser("show")
    show.add_argument("job")
    add = ledger_sub.add_parser("add")
    add.add_argument("job")
    add.add_argument("--by", required=True)
    add.add_argument("--commit", required=True)
    add.add_argument("--check", required=True)
    add.add_argument("--result", required=True, choices=("pass", "fail"))
    add.add_argument("--detail", default="")
    ledger.set_defaults(func=cmd_ledger)

    spawn = sub.add_parser("spawn")
    spawn.add_argument("assignment")
    where = spawn.add_mutually_exclusive_group()  # neither: next to the caller
    where.add_argument("--pane")
    where.add_argument("--split-from")
    spawn.add_argument("--resume")
    spawn.set_defaults(func=cmd_spawn)

    wait = sub.add_parser("wait")
    wait.add_argument("agent")
    wait.add_argument("--report")
    wait.add_argument("--seq", type=int)
    wait.add_argument("--jobs", action="store_true")
    wait.add_argument("--poll", type=float, default=None)
    wait.add_argument("--after-blocked", action="store_true")
    wait.set_defaults(func=cmd_wait)

    status = sub.add_parser("status")
    status.add_argument("--claim", action="store_true")
    status.set_defaults(func=cmd_status)

    revive_p = sub.add_parser("revive")
    revive_p.add_argument("agent")
    revive_p.add_argument("--reset", action="store_true")
    revive_p.add_argument("--auto", action="store_true")  # autonomy full: refuses a second one in a row
    revive_p.set_defaults(func=cmd_revive)

    resume_all = sub.add_parser("resume-all")
    resume_all.set_defaults(func=cmd_resume_all)

    tick = sub.add_parser("tick")
    tick.add_argument("--pane", required=True)
    tick.add_argument("--board", action="store_true")
    tick.add_argument("--events-from", type=int)
    tick.set_defaults(func=cmd_tick)

    take = sub.add_parser("take-mail", help="the agent's mod takes its inbox after a turn (mod)")
    take.add_argument("--agent", required=True)
    take.add_argument("--pane", required=True)
    take.add_argument("--give-back", nargs="+", metavar="NAME")
    take.set_defaults(func=cmd_take_mail)

    boundary = sub.add_parser("boundary", help="the assignments this agent has answered (mod)")
    boundary.add_argument("--pane", required=True)
    boundary.set_defaults(func=cmd_boundary)
    retry = sub.add_parser("api-retry")
    retry.add_argument("--pane", required=True)
    retry.add_argument("--attempt", type=int, default=1)
    retry.add_argument("--error", default="")
    retry.add_argument("--clear", action="store_true")
    retry.set_defaults(func=cmd_api_retry)

    interview = sub.add_parser("interview")
    interview.add_argument("job")
    for name, _ in DIMENSIONS:
        interview.add_argument("--" + name, type=float, required=True)
    interview.add_argument("--question")
    interview.add_argument("--answer")
    interview.add_argument("--stop", action="store_true", help="the user chose to stop asking")
    interview.set_defaults(func=cmd_interview)

    pending = sub.add_parser("pending")
    pending.set_defaults(func=cmd_pending)

    sub.add_parser("extras", help="what a child's assignment may switch on").set_defaults(func=cmd_extras)
    cost = sub.add_parser("cost", help="tokens per job, PM and orchestrator, from the usage.csv files")
    cost.add_argument("job", nargs="?")
    cost.set_defaults(func=cmd_cost)

    for name, helptext in (("agents", "names of the running agents"),
                           ("read", "the agent's screen"), ("keys", "press keys in the agent"),
                           ("attach", "show the agent to the user"), ("close", "close a place"),
                           ("place", "a place for a PM that never ran"),
                           ("worktree", "create or remove a job's worktree")):
        p = sub.add_parser(name, help=helptext)
        p.set_defaults(func=cmd_rt, rt=name)
        if name in ("read", "keys", "attach"):
            p.add_argument("agent")
        if name == "keys":
            p.add_argument("keys", nargs="+")
        if name == "close":
            p.add_argument("place", nargs="+")
        if name == "place":
            p.add_argument("pm", help="the PM's name")
        if name == "worktree":
            p.add_argument("job", nargs="?")
            p.add_argument("--remove", metavar="ID")
            p.add_argument("--job", dest="remove_job", help="with --remove: the job the worktree is for")

    pm = sub.add_parser("pm", help="the PMs: named roles, each with its own wiki")
    pm_sub = pm.add_subparsers(dest="action")
    pm_sub.required = True
    pm_sub.add_parser("add").add_argument("name")
    pl = pm_sub.add_parser("list")
    pl.add_argument("--path", help="mark the PM whose places hold this path as recommended")
    p = pm_sub.add_parser("rename")
    p.add_argument("name")
    p.add_argument("new")
    p = pm_sub.add_parser("remove")
    p.add_argument("name")
    p.add_argument("--cancel", action="store_true", help="the user agreed: cancel its open and held jobs")
    pm.set_defaults(func=cmd_pm)

    pc = sub.add_parser("path-check", help="may this agent write the file (mod)")
    pc.add_argument("--pane", required=True)
    pc.add_argument("--path", required=True)
    pc.set_defaults(func=cmd_path_check)

    board = sub.add_parser("board", help="the board as text, for hosts without the mod pane")
    board.set_defaults(func=cmd_board)

    send = sub.add_parser("send")
    send.add_argument("agent")
    send.add_argument("text", nargs="+")
    send.add_argument("--job")
    send.set_defaults(func=cmd_send)

    peer = sub.add_parser("peer", help="PMs ask each other about the impact of a change")
    peer.add_argument("action", choices=["ask", "answer", "status"])
    peer.add_argument("job")
    peer.add_argument("text", nargs="*")
    peer.set_defaults(func=cmd_peer)

    hook = sub.add_parser("compact-hook")
    hook.set_defaults(func=cmd_compact_hook)

    automate.add_parser(sub, lambda args: automate.cmd(args, oke_home(), native_path))
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
            try:
                who = runtime().name
            except Exception:  # noqa: BLE001 - only the message's first word
                who = "runtime"
            return fail("%s error: %s" % (who, e))
        # exit 2: the runtime itself is unreachable. Callers must not read this as "agent gone".
        try:
            who = runtime().name
        except Exception:  # noqa: BLE001 - only the message's first word
            who = "runtime"
        sys.stderr.write("%s unavailable: %s\n" % (who, e.message))
        return 2


if __name__ == "__main__":
    # Screen text contains characters a legacy locale (cp949, C) cannot encode.
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(code)
