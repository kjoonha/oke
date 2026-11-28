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
import signal
import stat
import subprocess
import sys
import tempfile
import time

import automate
import runtime_bg
import runtime_herdr
import wiki
from runtime_herdr import DialogError, HerdrError, herdr, herdr_text, replace_file, session_of, write_private  # noqa: F401 (tests)

PHASES = ("intake", "planning", "awaiting_approval", "working", "reviewing", "done", "failed")
OLD_PHASES = {"awaiting_merge": "reviewing", "merging": "reviewing"}  # state.json of an OKe that merged
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
        "max_review_rounds": 3,  # a job's reviews stop there and the PM asks the user; null: no limit
        "merge_ignore_untracked": [".omc/"],  # other tools' untracked folders: never the job's work
        # effortLevel per role ("low" .. "max"); a lean child of a role left out uses the default
        "effort": {},
        "theme": "disco",  # the band's characters: disco (default), slime, mushroom, cute-dog, cute-cat, ... (README lists all)
        # a PM, worker or reviewer compacts at a task boundary only from this much context used
        "compact_min_percent": 30,
        "check_timeout": 30,  # minutes one run of a plan's Check command may take (also its run on the base)
        # the checkout's ignored dependency folders linked into the base run's temporary worktree
        "base_links": ["node_modules", ".venv", "venv"],
        "allowed_models": [],  # the models an assignment's `model:` may pick; empty: any
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
        if isinstance(job, dict) and job.get("phase") in OLD_PHASES:
            job["phase"] = OLD_PHASES[job["phase"]]
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
        try:
            out, err = p.communicate(timeout=GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            raise ValueError("git did not answer about %s" % path)
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
# "Call the Skill tool" outright: seen live, a model told only "use the skill" read files and did the job itself
FIRST_PROMPT = ("Call the Skill tool with the oke:{role} skill. Do that first: read or change nothing before it. "
                "Your assignment file is {path}: then read it and follow it as the skill says.")


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
                  "Bash(git branch --delete --force:*)", "Bash(git branch --force --delete:*)",
                  "Bash(git branch -d -f:*)",
                  "Bash(git checkout -f:*)", "Bash(git checkout --force:*)", "Bash(git stash drop:*)",
                  "Bash(git stash clear:*)", "Bash(git update-ref -d:*)", "Bash(git filter-branch:*)",
                  "Bash(git filter-repo:*)", "Bash(git worktree remove --force:*)", "Bash(git switch -f:*)",
                  "Bash(git switch --force:*)", "Bash(git switch --discard-changes:*)"]
# Workers and reviewers also may not throw away or hide uncommitted work (the user's files sit in
# the same checkout): no stash at all, no checkout/restore of the whole tree. The mod's
# AGENT_GIT_DENIED catches the forms these exact rules miss (`-C <dir>`, `./`, extra spaces).
AGENT_GIT_DENY_RULES = ["Bash(git stash:*)", "Bash(git checkout -- .)", "Bash(git checkout .)",
                        "Bash(git restore .)", "Bash(git restore -- .)",
                        "Bash(git checkout HEAD .)", "Bash(git checkout HEAD -- .)", "Bash(git checkout -- '*')",
                        "Bash(git checkout -- \"*\")", "Bash(git checkout -- :/)", "Bash(git checkout :/)",
                        "Bash(git restore -s HEAD .)", "Bash(git restore --source HEAD .)",
                        "Bash(git restore --source=HEAD .)", "Bash(git restore :/)", "Bash(git rm -r .)",
                        "Bash(git rm -rf .)", "Bash(git rm -r -- .)"]
# A worker of a job in place leaves its work uncommitted: only `oke job deliver` commits there.
COMMIT_DENY_RULES = ["Bash(git commit:*)", "Bash(git revert:*)", "Bash(git cherry-pick:*)", "Bash(git merge:*)",
                     "Bash(git am:*)", "Bash(git rebase:*)"]


def worker_deny_rules(folder):
    """Edit rules a worker may not have: the job's files the delivery gate trusts (plan,
    ledger, the reviewer's and the PM's folders). Its own folder stays writable.
    shortcut: Bash can still write them (`echo > ...`); the mod's workerDenied and the
    reviewer's look at the branch are the other layers."""
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
            # herdr reuses tab and workspace ids after a restart: never another job's (--job), or
            # without --job (an older PM) one a job still works in
            owner = [n for n, j in load_state()["jobs"].items()
                     if (j.get("worktree") or {}).get("id") == args.remove
                     and (n != args.remove_job if args.remove_job
                          else j.get("phase") not in DONE_PHASES)]
            if owner:
                return fail("worktree %s is job %s's now, and that job still works in it: not removed"
                            % (args.remove, owner[0]))
            jobs = load_state()["jobs"]
            found = [(n, j) for n, j in jobs.items() if (j.get("worktree") or {}).get("id") == args.remove]
            if len(found) == 1 and found[0][1].get("git_key") and found[0][1]["worktree"].get("path"):
                # one job names it (never a guess between jobs): close what is open in its
                # checkout, found by its path (the stored id may be another's after a restart;
                # closing its last pane closed it already), then remove the checkout
                job, info = found[0]
                path = native_path(info["worktree"]["path"])
                repo = checkout_dir(info)
                # children may run outside the tab, in the checkout: on Windows git cannot
                # delete a folder a shell sits in (half deleted, then "Permission denied")
                close_children(job, info.get("pm"))
                close_worktree_places(rt, repo, path, own=own_places(info))
                rt.worktree_remove(path=path, repo=repo)
                left = path
            elif rt.name == "bg":
                rt.worktree_remove(args.remove)  # its id is its path: never another's
                left = args.remove
            else:
                # no single job names it: close it only when it is open in the worktree of the
                # job --job names, or without --job of a finished job; never in a job's that
                # still works (herdr reuses ids: w1:t4 may be another job's working tab now)
                mine = [jobs.get(args.remove_job) or {}] if args.remove_job else \
                    [j for j in jobs.values() if j.get("phase") in DONE_PHASES]
                known = [j for j in mine
                         if j.get("git_key") and (j.get("worktree") or {}).get("path")
                         and args.remove in rt.worktree_places(native_path(repo_dir(j["git_key"])),
                                                               native_path(j["worktree"]["path"]),
                                                               own=own_places(j))]
                gone = (jobs.get(args.remove_job) or {}).get("worktree") or {}
                if not known and gone.get("path") and not os.path.isdir(native_path(gone["path"])):
                    # its tab closed with its last pane, and its checkout is gone: done already
                    print(json.dumps({"removed": args.remove, "already": True}, ensure_ascii=False))
                    return 0
                if not known:
                    return fail("worktree %s is open in no worktree of %s (herdr reuses ids): nothing "
                                "closed or removed" % (args.remove, ("job " + args.remove_job) if args.remove_job
                                                       else "a finished job"))
                # one id, no single job naming it: close that tab only, never a guess at a folder
                rt.worktree_remove(args.remove)
                paths = [native_path(j["worktree"]["path"]) for j in known]
                left = next((p for p in paths if os.path.isdir(p)), paths[0])
            if os.path.isdir(native_path(left)):
                # its tab closed, its checkout still there: not removed (`job archive` removes a
                # finished job's checkout; git may refuse one with changes)
                print(json.dumps({"closed": args.remove, "checkout_left": left}, ensure_ascii=False))
                return 1
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
            if is_answer(info):
                return fail("job %s is an answer: it changes nothing, so it gets no worktree" % args.job)
            if info.get("phase") in SPLIT_PHASES and not (info.get("approved") or info.get("worked")):
                return fail("job %s is %s and its plan was never approved: its worktree comes with the work, "
                            "after the approval" % (args.job, info.get("phase")))
            other = plan_continues(args.job)
            start = None
            if other:  # its own branch oke/<job>, made at the tip of oke/<other>
                start = continue_start(args.job, info, other)
                if start is None:
                    return 1
            # shortcut: `oke job deliver` delivers only oke/<job>, so the branch name is fixed here
            # its tab opens in the PM's workspace: next to the PM's pane, else the caller's
            near = worktree_near(rt, info)
            out = rt.worktree_create(native_path(info["path"]), "oke/" + args.job, args.job,
                                     start=start["commit"] if start else None, near=near)
            with locked():
                state = load_state()
                if args.job in state["jobs"]:  # for --remove once its id is gone
                    for j in state["jobs"].values():  # herdr reuses tab ids after a restart
                        if (j.get("worktree") or {}).get("id") == out["id"]:
                            j["worktree"] = dict(j["worktree"], id=None)  # its folder stays its own
                        if out["id"] in ((j.get("worktree") or {}).get("tabs") or []):
                            j["worktree"]["tabs"] = [t for t in j["worktree"]["tabs"] if t != out["id"]]
                    mine = state["jobs"][args.job]
                    before = mine.get("worktree") or {}
                    same = normalize_path(before.get("path") or "") == normalize_path(out["path"])
                    # every tab OKe opened there (a second `worktree <job>` opens a second): only
                    # these close with the job, never a tab the user opened in the checkout
                    tabs = []
                    for t in ((before.get("tabs") or []) + [before.get("id")] if same else []) + [out["id"]]:
                        if t and t not in tabs:
                            tabs.append(t)
                    mine["worktree"] = {"id": out["id"], "path": out["path"], "tabs": tabs}
                    if start:
                        mine.update(continues=other, start_commit=start["commit"], base=start["base"])
                    save_state(state)
            if start:
                out.update(continues=other, review_base=start["commit"], base=start["base"])
    print(json.dumps(out, ensure_ascii=False))
    return 0


EFFORTS = ("low", "medium", "high", "xhigh", "max")  # `claude --effort`


def launch_args(role, config, home, plugin_dir, resume=None, folder=None, header=None, extra_dirs=()):
    """`header` is the agent's assignment header: its `tools:`, `mcp:` and `plugins:`
    (comma separated) are what the PM switched on for this job."""
    model = (header or {}).get("model") or config["models"].get(role, "opus")
    allowed = [m for m in config.get("allowed_models") or [] if isinstance(m, str)]
    if allowed and model not in allowed:
        model = config["models"].get(role, "opus")  # a header's model outside the list: the role's own
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
    if role in ("worker", "reviewer"):
        denied = denied + AGENT_GIT_DENY_RULES
    if role == "worker" and folder and folder != home:
        denied = denied + worker_deny_rules(folder)
    if role == "worker":
        try:
            job = load_state()["jobs"].get((header or {}).get("job") or "") or {}
        except (OSError, StateError):
            job = {}
        if in_place(job):
            denied = denied + COMMIT_DENY_RULES
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
        # a linked worktree (`git worktree add`) the user named is the job's checkout, on its own branch
        code, top = git_out(p, "rev-parse", "--show-toplevel")
        git_dir = git_out(p, "rev-parse", "--absolute-git-dir")[1]
        if code == 0 and top and git_dir and normalize_path(os.path.realpath(native_path(git_dir))) != key:
            folder = top
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
    if changes_now(job):
        problem = path_clash(state, name, folder, direct, branch_taken)
        if problem:
            raise ValueError(problem)
    # the branch the main checkout is on now: `job deliver` brings the job's branch onto it
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


def changes_now(job):
    """A job that may change its path: not an answer, and approved (or past planning) once. A job
    still investigating only reads, so it never clashes with another job's folder or branch; the
    clash is checked when its plan goes to the user (`job phase awaiting_approval`)."""
    if is_answer(job):
        return False
    return not (job.get("phase") in GATE_PHASES and not (job.get("approved") or job.get("worked")))


def holds_path(job):
    """A job whose folder another job may not change too: it may change it now, or its plan is with
    the user (an approval would let it)."""
    return changes_now(job) or job.get("phase") == "awaiting_approval" and not is_answer(job)


def path_clash(state, name, folder, direct, branch_taken=None):
    """Why job `name` cannot change `folder`: another open job changes the same files in place
    (no git, no undo), or branch oke/<name> exists already. None when it can."""
    if direct:
        # a folder inside another's, or holding it, is the same files
        busy = sorted(n for n, j in state["jobs"].items()
                      if n != name and j.get("direct") and j.get("path") and holds_path(j)
                      and j.get("phase") not in DONE_PHASES
                      and (under(folder, j["path"]) or under(j["path"], folder)))
        if busy:
            return ("job %s already changes %s in place (no git, no undo), which overlaps %s: "
                    "finish or archive it first" % (busy[0], state["jobs"][busy[0]]["path"], folder))
        return None
    if call_timed(["git", "-C", native_path(folder), "show-ref", "--verify", "--quiet",
                   "refs/heads/oke/" + name]) == 0:
        return (branch_taken % {"job": name, "checkout": folder} if branch_taken else
                "branch oke/%s already exists in %s (an earlier job): pick another job name" % (name, folder))
    return None


def checkout_busy(state, name):
    """The open job that has changed, or may change, job `name`'s checkout in place, or None.
    It counts from its start (it worked there, or its snapshot of the user's files exists)
    until it is done or failed, also back in planning or approval after a change request.
    A delivered job counts again once it is put back to work after its last delivery.
    A job in a worktree of its own never counts, nor one that waits for another itself (queued)."""
    path = normalize_path(state["jobs"][name].get("path") or "")
    if not path:
        return None
    for n, j in sorted(state["jobs"].items()):
        if n == name or j.get("phase") in DONE_PHASES or is_answer(j) or j.get("direct") \
                or normalize_path(j.get("path") or "") != path:
            continue
        if j.get("queued") and j.get("phase") in ("planning", "awaiting_approval"):
            continue  # it waits itself: two jobs back from a delivery must not wait for each other
        if (j.get("worktree") or {}).get("path"):
            continue
        started = j.get("worked_in_main") or j.get("phase") in ("working", "reviewing") \
            or os.path.exists(snapshot_path(n))
        if not started or (not j.get("worked_in_main") and plan_worktree(n)):
            continue
        try:
            done, row = deliver_state(n)
        except StateError:
            return n  # its ledger cannot be read: count it busy rather than mix two jobs' files
        if done == "delivered":
            at = time.mktime(time.strptime(row["time"], "%Y-%m-%dT%H:%M:%S")) if row.get("time") else 0
            if (j.get("phase_at") or 0) <= at + 1:
                continue  # delivered, not put back to work since
        return n
    return None


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
    names = [n for n, j in state["jobs"].items() if pm_owns(j, pm)] if role == "pm" else [job]
    out = []
    for n in names:
        j = state["jobs"].get(n) or {}
        if j.get("phase") in DONE_PHASES or not j.get("path") or is_answer(j) or j.get("held"):
            continue  # an answer changes nothing, a held job is not worked on: no write area
        if j.get("phase") in SPLIT_PHASES and not (j.get("approved") or j.get("worked")):
            continue  # nothing changes before the user approved the plan: investigating only reads
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


# files under $OKE_HOME only OKe writes, also inside a folder an agent may write: the delivery
# gate, the interview, the peer talk, a PM's children and every agent's launch settings
GATE_FILE = re.compile(r"^(ledger\.(tsv|head)|baseline\.(json|log)|dirty-before\.json|marks\.json|interview\.json|"
                       r"peers\.(json|md)|intake\.md|children\.json|(lean-|bg-)?settings\.json|mcp\.json|"
                       r"(check|verify)-\d+\.log)$")
# the copies of the user's files from a job's start (`job restore-user` brings them back)
GATE_DIR = re.compile(r"[\\/]jobs[\\/][^\\/]+[\\/]dirty-before(?:\.new-\d+)?(?:[\\/]|$)", re.I)


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
    # seen live: a PM refused an Edit rewrote the file with a script and committed it itself
    return {"pm": "A PM never changes a repository itself, by any tool (no Bash, script or git commit either): "
                  "while the job plans, write its plan.md and report done; a worker makes the change once the "
                  "user approved it. If the job must change another folder: while it plans, set its path with "
                  "`$OKE job path`, or split a new job for that folder with `$OKE job split`; once it works, "
                  "report a question for it.",
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
        if GATE_FILE.match(os.path.basename(p)) or GATE_DIR.search(p[len(home):]):
            return ("%s is written only by OKe (`oke` commands keep it; the delivery gate trusts it): leave it. %s"
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
    if info.get("phase") == "done":
        return "job %s is done: its work is delivered, nothing to cancel" % job
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
    return sorted(n for n, j in state["jobs"].items() if pm_owns(j, name) and j.get("phase") not in DONE_PHASES)


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
            fits = [e["name"] for e in out if any(under(want, p) for p in e["places"])]
            for entry in out:
                entry["recommended"] = entry["name"] == best[1]
                # the only PM that worked there: the orchestrator gives it the job without asking
                entry["only_fit"] = fits == [entry["name"]]
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
        me = whoami(state, args.pane, args.session)[0]
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
        if job["phase"] not in ("working", "reviewing"):
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
            return not now.get("held") and now.get("phase") in ("working", "reviewing")

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


def report_body(path):
    """The text below a report's first `---` line, or ""."""
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except (IOError, OSError, UnicodeDecodeError):
        return ""
    strip = [l.strip() for l in lines]
    return "\n".join(lines[strip.index("---") + 1:]) if "---" in strip else ""


def text_section(text, name):
    """The body of a `## <name>` section of a markdown text (to the next `## `), or None."""
    m = re.search(r"^## " + re.escape(name) + r"[^\n]*\n?(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else None


QUESTION_FORM = ("write each question as these lines: `job: <the user's own words for the job, short>` / "
                 "`decide: <what the user decides, one sentence>` / `options:` / then 2 to 4 lines "
                 "`- <option>: <what happens if the user picks it>`, in the user's language (`decide:` and each "
                 "option at most 200 characters; `(Recommended)` after the option you advise). The one exception: a "
                 "Questions section that starts with `no-change:` (the question to end a job with no change)")
# a line's lead-in a PM may add: a list mark (`-`, `*`, `1.`, `1)`), then bold or backticks
LEAD = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*|__|`)?")
KEY_LINE = re.compile(r"^(job|decide|options|asks)(?:\*\*|__|`)?\s*:(?:\*\*|__|`)?\s*(.*)$", re.I)
OPTION_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*(?:\*\*|`)?\s*:\s+(\S.*)$")


def question_key(line):
    """(key, value) of a `job:`, `decide:`, `options:` or `asks:` line, however it is marked up, or None."""
    m = KEY_LINE.match(LEAD.sub("", line, count=1))
    return (m.group(1).lower(), m.group(2).strip().strip("*`").strip()) if m else None


def parse_questions(text):
    """The questions of a `## Questions` section as [{"job", "decide", "options": [(option, effect)],
    "asks"}]; each starts at a `job:` line. `asks:` (one of QUESTION_ASKS: user_files, review_cap,
    check_accept, head_moved) names a question only the user answers, even for a job on `job auto`. Options are the list lines after its `options:` line only: a
    bullet of context elsewhere is not one."""
    out = []
    listing = False
    for line in (text or "").splitlines():
        key = question_key(line)
        if key:
            listing = key[0] == "options"
        if key and key[0] == "job":
            out.append({"job": key[1], "decide": "", "options": [], "asks": None})
        elif not out:
            continue
        elif key and key[0] == "decide":
            out[-1]["decide"] = key[1]
        elif key and key[0] == "asks":
            out[-1]["asks"] = key[1].lower()
        elif key:
            continue  # `options:`
        elif listing and OPTION_LINE.match(line):
            option, effect = OPTION_LINE.match(line).groups()
            out[-1]["options"].append((option.strip("*` "), effect.strip()))
    return out


def no_change_question(text):
    """A Questions section that asks to end the job with no change (`no-change:` first)."""
    return LEAD.sub("", (text or "").strip(), count=1).lower().startswith("no-change")


QUESTION_ASKS = ("user_files", "review_cap", "check_accept", "head_moved")  # `asks:` values: questions `job auto` never answers
QUESTION_CHARS = 200  # a `decide:` line, and each option with what happens, fit in a popup
RECOMMENDED = re.compile(r"\(recommended\)", re.I)
# what is not prose when the language of a text is judged: code, paths, file names, identifiers
NOT_PROSE = re.compile(r"`[^`]*`|\S*[\\/._:](?=[^\s.,:;!?)\]'\"])\S*|\S*\d\S*|\b[a-z]+[A-Z]\w*")
# (a word that only ends in `.` or `:` is prose: `합니다.`, `옵션:`)


def is_hangul(c):
    return u"가" <= c <= u"힣" or u"ᄀ" <= c <= u"ᇿ" or u"㄰" <= c <= u"㆏"


KOREAN_QUESTION = 0.3  # a question is in Korean when this part of its words hold Hangul


def hangul_share(text):
    """The part of a text's words (its prose: code and paths left out) that hold Hangul, or None when
    it has no words. A word is counted once: `commit합니다` is a Hangul word."""
    words = [w for w in NOT_PROSE.sub(" ", text or "").split() if any(c.isalpha() for c in w)]
    if not words:
        return None
    return len([w for w in words if any(is_hangul(c) for c in w)]) / float(len(words))


def recommended_picks(job):
    """The option marked `(Recommended)` of each question of a job's PM report, when every question has
    exactly one; else None. A job the user set to `job auto` takes them without asking."""
    text = text_section(report_body(os.path.join(pm_job_dir(job), "report.md")), "Questions")
    if no_change_question(text):
        return None
    picks = []
    for q in parse_questions(text):
        if q["asks"]:  # the user's own files, or a review past its cap: never answered for the user
            return None
        marked = [o for o, _ in q["options"] if RECOMMENDED.search(o)]
        if len(marked) != 1:
            return None
        picks.append(marked[0])
    return picks or None


def question_problem(report, words=None):
    """Why a PM's `question` report does not ask the user in the required form, or None. A
    Questions section that starts with `no-change:` asks whether to end the job with no change:
    the orchestrator words that one itself. `words`: the user's words for the job; when they are
    mostly Hangul, each question must be too."""
    text = text_section(report_body(report), "Questions")
    if no_change_question(text):
        return None
    korean = (hangul_share(words) or 0) > 0.5
    questions = parse_questions(text)
    if not questions:
        return "bad_report its Questions section holds no question: %s" % QUESTION_FORM
    for n, q in enumerate(questions, 1):
        missing = [what for what, ok in (("the user's words after `job:`", q["job"]),
                                         ("a `decide:` line", q["decide"]),
                                         ("2 or more options", len(q["options"]) >= 2)) if not ok]
        if missing:
            return "bad_report question %d lacks %s: %s" % (n, " and ".join(missing), QUESTION_FORM)
        if q["asks"] is not None and q["asks"] not in QUESTION_ASKS:
            return "bad_report question %d: `asks:` is %s, not %r" % (n, " or ".join(QUESTION_ASKS), q["asks"])
        if len(q["options"]) > 4:
            return "bad_report question %d has more than 4 options (the user picks from at most 4): %s" % (
                n, QUESTION_FORM)
        if len(q["decide"]) > QUESTION_CHARS:
            return ("bad_report question %d: its `decide:` line has %d characters; say it in at most %d"
                    % (n, len(q["decide"]), QUESTION_CHARS))
        long_ = [o for o, e in q["options"] if len(o) + len(e) > QUESTION_CHARS]
        if long_:
            return ("bad_report question %d: option %r and what happens with it are longer than %d characters: "
                    "say it shorter" % (n, long_[0][:40], QUESTION_CHARS))
        options = " ".join("%s %s" % (RECOMMENDED.sub("", o), e) for o, e in q["options"])  # the mark is frame
        if korean and any((hangul_share(t) or 0) < KOREAN_QUESTION for t in (q["decide"], options)):
            return ("bad_report question %d is not in the user's language: the user wrote in Korean, so write "
                    "its `decide:` line and its options in Korean" % n)
    return None


def pm_question_problem(job):
    return question_problem(os.path.join(pm_job_dir(job), "report.md"),
                            users_words(os.path.join(oke_home(), "jobs", job)))


# what a PM's `waiting` report may wait on (`on:`), besides one of the job's own agents (<job>-w1)
WAIT_FORMS = re.compile(r"^(?:(?:planner|work|review|research|explore) subagent|baseline|check|peer answers?"
                        r"|job [a-z][a-z0-9-]*)(?![a-z0-9-])")
WAIT_HINT = ("`on:` names what the job waits for: one of its agents (<job>-w1, <job>-rv), `planner`, `work`, "
             "`review`, `research` or `explore subagent <id>`, `baseline`, `check`, `peer answers` or "
             "`job <other job>`. A decision from the user is a `question` report, never `waiting`")
STUCK_GRACE = 60  # seconds a wait on something finished must last before the PM counts as stuck


def stuck_path():
    """When each job's wait was first seen stuck: kept on disk, so a restarted `oke wait` does not
    start the grace again."""
    return os.path.join(oke_home(), "waits", "stuck.json")


def wait_target(header):
    return (header.get("on") or "").strip().strip("`").strip().lower()


def wait_child(job, on):
    """The agent of job `job` a `waiting` report's `on:` names, or None."""
    m = re.match(re.escape(job) + r"-[a-z0-9]+(?:-[a-z0-9]+)*(?![a-z0-9-])", on)
    return m.group(0) if m else None


def waiting_problem(job, header):
    """Why a PM's `waiting` report for `job` does not say what it waits on, or None."""
    on = wait_target(header)
    if not on:
        return "bad_report it is waiting with no `on:`. %s" % WAIT_HINT
    if WAIT_FORMS.match(on) or wait_child(job, on):
        return None
    return "bad_report it is waiting on %r, which is none of these. %s" % (header.get("on"), WAIT_HINT)


def stuck_reason(state, key, job, header):
    """What a PM's `waiting` report for `job` waits on that will not come (one of the job's agents
    that finished or died, peer answers nobody owes, a job that closed), or None. A subagent, the
    baseline or a check cannot be seen from outside: never stuck here (`expect:` covers long ones)."""
    on = wait_target(header)
    if re.match(r"^peer answers?(?![a-z0-9-])", on):
        return None if peers_hold(state, job) else "peer answers, but no PM owes this job one now"
    m = re.match(r"^job ([a-z][a-z0-9-]*)", on)
    if m:
        other = state["jobs"].get(m.group(1))
        if other is None or other.get("phase") in DONE_PHASES:
            return "job %s, which is no longer open" % m.group(1)
        return None
    child = wait_child(job, on)
    if not child:
        return None
    record = read_children(key).get(child)
    if not record or record.get("job") != job:
        return "%s, which is not an agent of this job" % child
    folder = agent_folder(record.get("role") or "worker", child, key, record)
    seq = (read_header(os.path.join(folder, "assignment.md")) or {}).get("seq")
    report = read_header(os.path.join(folder, "report.md")) or {}
    if seq is not None and report.get("seq") == seq and report.get("type") in FINAL_TYPES:
        return "%s, which already reported %s for seq %s" % (child, report["type"], seq)
    if look_at(child, folder) == "gone":
        return "%s, which is gone" % child
    return None


def stuck_wait(state, key, job, seq, header):
    """The `?stuck_wait` verdict for a PM idle on a wait that will not end, once it has lasted
    STUCK_GRACE (the PM's own wait may be about to wake it), or None."""
    reason = stuck_reason(state, key, job, header)
    # the grace starts once per wait: a report rewritten with the same seq, `on:` and reason keeps
    # it (a PM rewriting a stuck wait every minute is still stuck)
    mark = [seq, header.get("on"), reason]
    with locked("stuck"):  # every PM's wait writes this file
        seen = read_json(stuck_path(), {})
        seen = seen if isinstance(seen, dict) else {}
        kept = dict((name, entry) for name, entry in seen.items()  # jobs still open only
                    if name in state.get("jobs", {}) and state["jobs"][name].get("phase") not in DONE_PHASES)
        if not reason:
            kept.pop(job, None)
            entry = None
        else:
            entry = kept.get(job)
            if not isinstance(entry, dict) or entry.get("mark") != mark or \
                    not isinstance(entry.get("since"), (int, float)):
                entry = kept[job] = {"mark": mark, "since": time.time()}
        if kept != seen:
            write_json_atomic(stuck_path(), kept)
    if entry is None or time.time() - entry["since"] < STUCK_GRACE:
        return None
    return "?stuck_wait %s %s" % (job, reason)


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
        seq, header, mtime = pm_job_report(name)
        warned = job.get("warned") or [None, 0]
        count = warned[1] + 1 if warned[0] == seq and not rewritten_valid(name, warned, seq, header, mtime) else 1
        job["warned"] = [seq, count, mtime]  # the report it was warned about
        save_state(state)
    if count >= SILENT_AFTER:
        word, rest = result.split(" ", 1)
        return "%s again %s" % (word, rest)
    return result


def is_silent(name, job, seq):
    """The PM was warned twice about this seq, and has not written a valid report since."""
    warned = job.get("warned") or [None, 0]
    if not (warned[0] == seq and warned[1] >= SILENT_AFTER):
        return False
    _, header, mtime = pm_job_report(name)
    return not rewritten_valid(name, warned, seq, header, mtime)


def spend_warnings(name, job, seq, header, mtime):
    """The wait saw the PM recover (a valid report rewritten since its warnings): the count ends
    here, so a wait that gets stuck later is counted from one."""
    warned = job.get("warned") or [None, 0]
    if warned[0] != seq or not warned[1] or not rewritten_valid(name, warned, seq, header, mtime):
        return
    with locked():
        state = load_state()
        current = state["jobs"].get(name)
        if current is not None and current.get("warned") == warned:
            current["warned"] = [seq, 0, mtime]
            save_state(state)
    job["warned"] = [seq, 0, mtime]


def rewritten_valid(name, warned, seq, header, mtime):
    """The PM rewrote its report since its last warning, and the report is now valid for this seq
    (a `waiting` with a right `on:` on something that can still come, or a final report in the
    required form): the warnings are spent."""
    if len(warned) < 3 or warned[2] == mtime or header.get("seq") != seq:
        return False
    kind = header.get("type")
    if kind == "waiting":
        if waiting_problem(name, header):
            return False
        state = load_state()
        info = state["jobs"].get(name) or {}
        # a wait on what will not come is no recovery: the same stuck wait rewritten counts on
        return not stuck_reason(state, info.get("pm"), name, header)
    if kind == "question":
        return not pm_question_problem(name)
    return kind in FINAL_TYPES


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
        # a question the user could not read goes back to the PM first (below), never to the user
        return jobs, [j for j in jobs if j[2].get("seq") == j[1] and j[2].get("type") in FINAL_TYPES
                      and state["jobs"][j[0]].get("seen") != seen_mark(j[2])
                      and not (j[2].get("type") == "question" and pm_question_problem(j[0]))]
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
    for name, seq, header, mtime in jobs:
        if state["jobs"][name].get("sent_seq") != seq:
            continue  # the orchestrator is still handing it over: the message is not sent yet
        if is_silent(name, state["jobs"][name], seq):
            continue  # the user was told twice; `pending` keeps it in view
        spend_warnings(name, state["jobs"][name], seq, header, mtime)
        if header.get("seq") == seq and header.get("type") == "question":
            # a question out of form goes back to the PM even while peers still owe answers:
            # it never reaches the user as it is
            problem = pm_question_problem(name)
            if problem:
                word, why = problem.split(" ", 1)
                return "?%s %s %s" % (word, name, why)
        if peers_hold(state, name):
            continue  # it waits for other PMs' answers (oke peer ask): not stopped short
        if not (header.get("seq") == seq and header.get("type") in REPORT_TYPES):
            report = os.path.join(pm_job_dir(name), "report.md")
            problem = report_problem(report, read_header(report))
            if problem:
                word, why = problem.split(" ", 1)
                return "?%s %s %s" % (word, name, why)
            return "?idle_no_report %s" % name
        problem = None
        if header.get("type") == "waiting":
            problem = waiting_problem(name, header)
            if not problem:
                stuck = stuck_wait(state, key, name, seq, header)
                if stuck:
                    return stuck
        if problem:
            word, why = problem.split(" ", 1)
            return "?%s %s %s" % (word, name, why)
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
            return fail(never_started(state, args.agent) or "--jobs needs a PM agent: %s is not one" % args.agent)
        return wait_said(args.agent, wait_loop(args.agent, None, None, poll=args.poll,
                                               after_blocked=args.after_blocked, jobs_key=keys[0]))
    if args.report is None or args.seq is None:
        return fail("wait needs --report and --seq, or --jobs for a PM")
    return wait_said(args.agent, wait_loop(args.agent, native_path(args.report), args.seq, poll=args.poll,
                                           after_blocked=args.after_blocked))


WAIT_SUPERSEDED = 0  # a newer wait on the same agent replaced this one: nothing to do, not an error


def wait_said(agent, result):
    """Print a wait's result; a superseded wait says only that, in one line."""
    if result == "superseded":
        print("superseded: a newer wait on %s replaced this one; nothing to do" % agent)
        return WAIT_SUPERSEDED
    print(result)
    return 0


# ---------------------------------------------------------------- revive

DONE_PHASES = ("done", "failed")
GONE_CONFIRMATIONS = 3  # one failed look (a CLI hiccup) is not a death
ORCH_NAME = "oke-orch"
REVIVE_PROMPT = ("You were restarted after a crash. Call the Skill tool with the oke:{role} skill. Do that "
                 "first, then re-read your assignment file {path} and your board.md, and continue.")


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


def pm_owns(job, key):
    """Job record `job` is PM `key`'s to work on. The orchestrator's own job (the shared wiki's
    curation, `job new --shared-wiki`) names a PM only for its wiki log: no PM works on it, waits
    for it, or sees it as its job."""
    return job.get("pm") == key and not job.get("orchestrator_job")


def open_jobs(state, key=None):
    """Jobs with work left. A job the user put on hold (`job hold`) is not one: nothing
    revives or watches agents for it until `job unhold`."""
    return [n for n, j in state["jobs"].items()
            if j.get("phase") not in DONE_PHASES and not j.get("held") and (key is None or pm_owns(j, key))]


def held_jobs(state, key=None):
    return sorted(n for n, j in state["jobs"].items()
                  if j.get("held") and j.get("phase") not in DONE_PHASES and (key is None or pm_owns(j, key)))


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
    """Only agents with work left are revived: never a worker closed after its job is done."""
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
        if runtime().inside():
            forget_gone_tabs(runtime())  # a dead agent: herdr may have restarted
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


def whoami(state, pane, session=None):
    """(role, name, key, record) of the OKe agent in `pane`, or None. Pane ids come
    back after a herdr restart, so a recorded session has to match as well: herdr's, or
    `session`, the one the mod runs in (herdr knows none without its Claude hook)."""
    if pane and pane.startswith("session:"):
        # a session id is never reused: no need to ask the runtime who is there
        sid = pane[len("session:"):]
        mine = [r for r in records(state) if r[3].get("pane") == pane or r[3].get("session") == sid]
        return (mine[0] if mine else None), list_agents()
    mine = [r for r in records(state) if r[3].get("pane") == pane]
    if not mine:
        return None, []
    agents = list_agents()
    for r in mine:
        if session and r[3].get("session") == session:
            return r, agents
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


def users_words(job_dir):
    """The `## User's words` of a job's intake.md, on one line ("" when there are none)."""
    try:
        with io.open(os.path.join(job_dir, "intake.md"), encoding="utf-8-sig") as f:
            text = f.read()
    except (IOError, OSError, UnicodeDecodeError):
        return ""
    words = text_section(text, u"User's words") or ""
    return " ".join(words.split())


def first_summary_line(report):
    """The first line of a report's `## Summary`, or ""."""
    for line in (text_section(report_body(report), "Summary") or "").splitlines():
        if line.strip():
            return line.strip().lstrip("-* ").strip()
    return ""


# the plain steps of a job, as the board shows them, and what each leaves to come
STEP_LEFT = {
    "taking the request": ["planning", "an answer, or a plan for your approval"],
    "planning": ["an answer, or a plan for your approval"],
    "replanning": ["your approval", "work", "review", "delivery"],
    "awaiting approval": ["your approval", "work", "review", "delivery"],
    "working": ["review", "delivery"],
    "reviewing": ["delivery"],
    "delivering": ["your merge"],
    "answering": [],
    "done": [],
    "stopped": [],
}


def waits_for(state, name):
    """The job a queued job `name` waits for now (the one first in its way may have changed), or None."""
    queued = state["jobs"][name].get("queued")
    if not queued or state["jobs"][name].get("phase") != "awaiting_approval":
        return None
    return checkout_busy(state, name) or queued.get("for")


def job_step(job, info, pm_report, delivered, waiting_for=None):
    """(where the job is, in plain words, [what is left]) for the board."""
    phase = info.get("phase")
    if phase == "done":
        step = "done"
    elif phase == "failed":
        step = "stopped"
    elif is_answer(info) or info.get("no_change"):
        step = "answering"
    elif phase == "intake":
        step = "taking the request"
    elif phase == "planning":
        step = "planning"  # the planner subagent, or the PM itself for a small job
    elif phase == "awaiting_approval" and info.get("queued"):
        # approved; another job changes the same checkout first
        return "waiting for %s to finish" % (waiting_for or info["queued"].get("for")), ["work", "review", "delivery"]
    elif phase == "awaiting_approval":
        step = "awaiting approval"
    elif delivered == "delivered":
        step = "delivering"
    else:
        on = wait_target(pm_report) if pm_report.get("type") == "waiting" else ""
        reviewing = phase == "reviewing" or on.startswith("review subagent") or bool(
            re.match(re.escape(job) + r"-rv", on))
        step = "reviewing" if reviewing else "working"
    replan = step == "planning" and os.path.exists(os.path.join(oke_home(), "jobs", job, "plan.md"))
    left = list(STEP_LEFT["replanning" if replan else step])  # a plan was made: an answer is past
    output = job_output(info)
    if output == "result":  # an experiment hands over its outputs, never a branch
        left = ["the result" if s in ("delivery", "your merge") else s for s in left]
    elif output == "files":  # changed in place: nothing to merge
        left = [s for s in left if s not in ("delivery", "your merge")]
    return step, left


RECENT_DONE = 5  # finished jobs the board lists


def recent_entry(name, info, d, at):
    """One finished job as the board's Done list shows it."""
    code = job_output(info) == "code" and info.get("phase") == "done"
    branch = job_branch(name, info) if code and info.get("git_key") and (info.get("worktree") or {}).get("path") \
        else None
    if branch and git_out(info["git_key"], "show-ref", "--verify", "--quiet", "refs/heads/" + branch)[0] != 0:
        branch = None  # merged and deleted, or `job clean` removed it: nothing left to merge
    return {"job": name, "pm": None if info.get("orchestrator_job") else info.get("pm"), "at": at, "words": users_words(d), "phase": info.get("phase"),
            "answer": is_answer(info) or bool(info.get("no_change")),
            "result": first_summary_line(os.path.join(d, "pm", "report.md")), "branch": branch}


def recent_index_path():
    return os.path.join(oke_home(), "recent-index.json")


def archive_index():
    """{archive folder name: its Done entry}. An archived job never changes, so each folder's
    files are read once, the first time a board sees it; the index keeps the entries."""
    root = os.path.join(oke_home(), "archive")
    try:
        names = [n for n in os.listdir(root) if re.match(r"^(.+)-(\d{8}-\d{6})$", n)]
    except OSError:
        names = []
    index = read_json(recent_index_path(), {})
    index = index if isinstance(index, dict) else {}
    kept = dict((n, e) for n, e in index.items() if n in names and isinstance(e, dict))
    for n in names:
        d = os.path.join(root, n)
        try:
            stamp = os.path.getmtime(os.path.join(d, "job.json"))
        except OSError:
            kept.pop(n, None)
            continue  # being moved in, or not a job: looked at again next time
        if n in kept and kept[n].get("mtime") == stamp:
            continue  # `job clean` rewrites job.json: then it is read again
        info = read_json(os.path.join(d, "job.json"), None)
        if not isinstance(info, dict) or not info.get("phase"):
            kept.pop(n, None)
            continue
        m = re.match(r"^(.+)-(\d{8}-\d{6})$", n)
        try:
            at = time.mktime(time.strptime(m.group(2), "%Y%m%d-%H%M%S"))
        except ValueError:
            at = 0
        kept[n] = dict(recent_entry(m.group(1), info, d, at), mtime=stamp)
    if kept != index:
        try:
            write_json_atomic(recent_index_path(), kept)
        except OSError:
            pass  # read again next time
    return kept


def recent_jobs(state, only_key=None):
    """The newest finished jobs of each PM (RECENT_DONE each), newest first: still in state (done
    or failed) or archived. `only_key`: that PM's only. Each entry names its PM (`pm`)."""
    found = []
    for name, info in state["jobs"].items():
        if info.get("phase") in DONE_PHASES:
            found.append(recent_entry(name, info, os.path.join(oke_home(), "jobs", name), info.get("phase_at") or 0))
    found.extend(e for e in archive_index().values() if e.get("job") and e.get("phase"))
    per_pm, out = {}, []
    for entry in sorted(found, key=lambda e: -(e.get("at") or 0)):
        if only_key and entry.get("pm") != only_key:
            continue
        per_pm[entry.get("pm")] = per_pm.get(entry.get("pm"), 0) + 1
        if per_pm[entry.get("pm")] <= RECENT_DONE:
            out.append(dict((k, v) for k, v in entry.items() if k not in ("at", "mtime")))
    return out


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
        if key not in state["pms"] and not any(pm_owns(j, key) and j.get("phase") not in DONE_PHASES
                                               for j in state["jobs"].values()):
            continue  # a removed PM's closed jobs: no line for it
        entry = state["pms"].get(key) or {}
        pm = entry.get("pm")
        children = read_children(key)
        jobs = []
        for job in sorted(n for n, j in state["jobs"].items() if pm_owns(j, key)):
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
                delivered = deliver_state(job)[0]
            except StateError:  # the ledger key is missing or damaged: the board still shows the rest
                delivered = "unreadable"
            step, left = job_step(job, info, pm_report, delivered, waits_for(state, job))
            seq, _, report_at = pm_job_report(job)
            # when this step began: the PM's `waiting` report for it (planner, worker, review), else the phase
            since = report_at if pm_report.get("type") == "waiting" and pm_report.get("seq") == seq \
                and report_at else info.get("phase_at")
            jobs.append({"since": since, "auto": bool(info.get("auto")),
                         "job": job, "phase": info.get("phase"), "agents": nodes,
                         "words": users_words(os.path.join(oke_home(), "jobs", job)), "step": step, "left": left,
                         "size": info.get("size"), "direct": bool(info.get("direct")), "output": job_output(info),
                         "deliver": delivered, "held": bool(info.get("held")), "subagent": subagent,
                         "pending": waiting["kind"] if waiting and not waiting["deferred"] else None,
                         # `job auto` answers it without the user: not counted as waiting for them
                         "auto_pending": bool(waiting and waiting.get("auto")),
                         "oke_rule": bool(waiting and waiting.get("oke_rule")),
                         # where it works: its worktree, or the path itself
                         "path": native_path((info.get("worktree") or {}).get("path") or info["path"])
                         if info.get("path") else "path?",
                         "peer_wait": sorted(peer_waiting(state, job))})
        node = board_node("pm", pm["agent"], key, pm, agents) if pm and pm.get("agent") else None
        if node and node["status"] == "dead" and not open_jobs(state, key):
            node["status"] = "off"  # nothing for it to do (its jobs are done or on hold): not stuck
        tree.append({"name": key, "key": key, "jobs": jobs, "pm": node})
    return tree


TODO_MARKS = {"completed": "v", "in_progress": ">", "pending": "o"}


# `oke pending` kinds that wait on the user, as the board names them; the rest the orchestrator
# handles itself
APPROVAL_KINDS = ("plan",)
QUESTION_KINDS = ("question", "blocked", "failed", "peer", "silent")
WAITING_WORDS = {"plan": "your approval of the plan", "question": "your answer to a question",
                 "blocked": "your answer to a permission prompt", "failed": "your decision on a failure",
                 "peer": "your decision: another PM did not answer", "silent": "your decision: the PM is silent"}
WORDS_SHORT = 100  # characters of the user's words a folded job shows


SAID = "You said: "  # the frame's label for the user's own words
FULL_HINT = "(oke board --full shows everything)"


def step_elapsed(seconds):
    """How long a step runs, as the board says it: `12 min`, `2 h 5 min`."""
    m = max(0, int(seconds // 60))
    return "%d min" % m if m < 60 else "%d h %d min" % (m // 60, m % 60)


def short_words(words, job):
    words = words or "(job %s)" % job
    return words if len(words) <= WORDS_SHORT else words[:WORDS_SHORT].rstrip() + u"…"


def board_lines(tree, recent=None, full=False):
    """The board as (depth, text) lines: what waits for the user, then per PM each job in the
    user's words, where it is and what is left (`full`: also its PM, agents, cost and tasks), and
    the PM's last jobs done. Jobs done of a PM no longer on the board come last."""
    out = []
    recent = recent or []
    cut = []  # some words were cut: one hint at the end names --full

    def said(words, job):
        """The user's words with their label (`full`: whole, else cut at WORDS_SHORT)."""
        if not words:
            return "(job %s)" % job
        shown = words if full else short_words(words, job)
        if shown != words:
            cut.append(job)
        return SAID + shown

    def done(entries, depth):
        out.append((depth, "Done"))
        for r in entries:
            mark = u"✗" if r.get("phase") == "failed" else u"✓"
            what = "stopped" if r.get("phase") == "failed" else "answered" if r.get("answer") else "done"
            tail = [t for t in (r.get("result"), "branch %s" % r["branch"] if r.get("branch") else "") if t]
            out.append((depth + 1, u"%s %s  (%s, %s)%s" % (mark, said(r.get("words"), r.get("job") or "?"),
                                                          r.get("job") or "?",
                                                          what, ": " + " - ".join(tail) if tail else "")))

    def agent(n, depth):
        out.append((depth, "%s %s: %s%s%s%s" % (
            n["role"], n["agent"], n["status"], ", report " + n["report"] if n.get("report") else "",
            ", waiting past its expected %s" % n["overdue"] if n.get("overdue") else "",
            ", $%.2f" % n["cost"] if n.get("cost") else "")))
        for t in n.get("todos") or []:
            out.append((depth + 1, "%s %s" % (TODO_MARKS.get(t["status"], "o"), t["content"])))

    jobs = [j for repo in tree for j in repo["jobs"] if j["phase"] not in DONE_PHASES]
    asks = [j for j in jobs if not j.get("auto_pending")]  # `job auto` answers the rest without the user
    approvals = len([j for j in asks if j.get("pending") in APPROVAL_KINDS])
    questions = len([j for j in asks if j.get("pending") in QUESTION_KINDS])
    out.append((0, u"To do for you: %d approval%s · %d question%s" % (
        approvals, "" if approvals == 1 else "s", questions, "" if questions == 1 else "s")))
    for repo in tree:
        open_ = [j for j in repo["jobs"] if j["phase"] not in DONE_PHASES]
        finished = [r for r in recent if r.get("pm") == repo.get("key")]
        if not open_ and not finished and not full:
            continue
        out.append((0, "PM %s" % repo["name"]))
        if full and repo["pm"]:
            agent(repo["pm"], 1)
        for job in open_:
            if not job.get("words"):
                out.append((1, "job %s" % job["job"]))  # no intake yet: its name is all there is
            else:
                out.append((1, u"%s  (%s)" % (said(job["words"], job["job"]), job["job"])))
            now = job.get("step") or job["phase"]
            if job.get("since"):
                now += u" · %s" % step_elapsed(time.time() - job["since"])
            if job.get("auto"):
                now += ", approves without asking"
            if job.get("held"):
                now += ", on hold"
            if job.get("pending") == "blocked" and job.get("oke_rule"):
                now += ", blocked by an OKe rule"  # not a dialog: the orchestrator reports it
            elif job.get("pending") in WAITING_WORDS and not job.get("auto_pending"):
                now += ", waiting for %s" % WAITING_WORDS[job["pending"]]
            if job.get("peer_wait"):
                now += ", waiting for PM %s" % ", ".join(job["peer_wait"])
            out.append((2, "now: %s" % now))
            out.append((2, "left: %s" % (", ".join(job.get("left") or []) or "nothing")))
            if full:
                out.append((2, "at %s" % (job.get("path") or "path?")))
                for n in job["agents"]:
                    agent(n, 2)
        if finished:
            done(finished, 1)
    keys = set(repo.get("key") for repo in tree)
    others = [r for r in recent if r.get("pm") not in keys]
    if others:
        done(others, 0)
    if not jobs and not recent and not any(repo["pm"] for repo in tree):
        return [(0, "no OKe agents yet")]
    if cut:
        out.append((0, FULL_HINT))
    return out


def cmd_board(args):
    """The board for hosts that cannot draw the mod's pane (SDK hosts)."""
    state = load_state()
    lines = board_lines(board_tree(state, list_agents()), recent_jobs(state), full=args.full)
    dim = sys.stdout.isatty()  # the hint is dim on a terminal, plain when a program reads it
    text = "\n".join("  " * d + ("\x1b[2m%s\x1b[0m" % t if dim and t == FULL_HINT else t)
                     for d, t in lines) + "\n"
    out = getattr(sys.stdout, "buffer", None)
    if out is not None:  # the user's words may be in any language: never the console's code page
        out.write(text.encode("utf-8"))
        out.flush()
    else:
        sys.stdout.write(text)
    return 0


def cmd_tick(args):
    """One watcher round for a mod: who am I, revive what died, new events, the board."""
    state = load_state()
    events, offset = read_events(args.events_from)
    out = {"role": None, "agent": None, "events": events,
           "events_offset": offset, "board": None}
    me, agents = whoami(state, args.pane, args.session)
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
            out["seq"] = (read_header(os.path.join(out["folder"], "assignment.md")) or {}).get("seq") \
                if out["folder"] else None
        if role == "worker":  # in the user's checkout only `oke job deliver` commits: the mod refuses git commit
            out["in_place"] = in_place(state["jobs"].get(record.get("job")) or {})
        if role == "orchestrator":
            targets = [r for r in records(state) if r[0] == "pm"]
            if not (state.get("orchestrator") or {}).get("ask_gate"):
                # a mod runs here: it counts popup answers, so user-only decisions need one
                with locked():
                    s = load_state()
                    s.setdefault("orchestrator", {})["ask_gate"] = True
                    save_state(s)
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
            out["recent"] = recent_jobs(state, key if role == "pm" else None)
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
    me, _ = whoami(state, args.pane, args.session)
    out = {"role": None, "answered": [], "next": "", "min_percent": load_config().get("compact_min_percent", 30)}
    if me is not None:
        role, name, key, record = me
        out["role"] = role
        if role == "pm":
            jobs = sorted(n for n, j in state["jobs"].items() if pm_owns(j, key))
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
    me, _ = whoami(state, args.pane, args.session)
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
        forget_gone_tabs(runtime())
    else:
        # outside the runtime's terminals nothing can be claimed or started, but the
        # state still answers (as `oke board` does); liveness unknown if unreachable
        try:
            herdr_info = ([], list_agents())
        except HerdrError:
            herdr_info = ([], None)
    with locked():
        return status_locked(args, herdr_info)


def forget_gone_tabs(rt):
    """Drop the recorded worktree tabs herdr no longer has (it restarted, or the tab closed): herdr
    reuses ids, so a later tab with one of them, maybe the user's, is never taken for OKe's. Run
    at `oke status` (the orchestrator's start) and when a PM is revived."""
    try:
        jobs = load_state()["jobs"]
    except StateError:
        return  # unreadable: `oke status` recovers it, and tabs are checked at the next start
    if not any((j.get("worktree") or {}).get("tabs") or ":t" in ((j.get("worktree") or {}).get("id") or "")
               for j in jobs.values() if j.get("phase") not in DONE_PHASES):
        return  # no tab recorded: nothing to ask herdr
    try:
        present = rt.tab_ids()
    except HerdrError:
        return
    if present is None:
        return  # no tabs in this runtime
    with locked():
        state = load_state()
        changed = False
        for j in state["jobs"].values():
            wt = j.get("worktree") or {}
            tabs = wt.get("tabs")
            if tabs is None and wt.get("id") and ":t" in wt["id"]:
                tabs = [wt["id"]]  # an older record: its id is its tab
            if tabs is None:
                continue
            kept = [t for t in tabs if t in present or ":t" not in t]  # a workspace id is not a tab
            if kept != wt.get("tabs"):
                j["worktree"]["tabs"] = kept
                changed = True
        if changed:
            save_state(state)


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
              "orchestrator_conflict": None, "pms": {}, "jobs": {}, "orphan_job_dirs": [], "kept_worktrees": []}
    # every key above (and in each job below) is always there: empty, false or null when it has nothing
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
        elif job.get("orchestrator_job"):
            cls = "orchestrator"  # the orchestrator's own (shared-wiki curation): no PM to resume or restart
            job_agents = {}
        elif job.get("held") and job.get("phase") not in DONE_PHASES:
            cls = "held"
        elif job.get("phase") == "awaiting_approval":
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
        # `job triage`'s size from the user's words (the PM decides `size`), and the user's "do not ask again"
        result["jobs"][name].update(size_hint=job.get("size_hint"), auto=bool(job.get("auto")))
        # its branch starts at another job's branch: its own work is after that
        result["jobs"][name].update(continues=job.get("continues"),
                                    review_base=review_base(name, job) if job.get("continues") else None,
                                    waiting_for=waits_for(state, name))

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
            if os.environ.get("CLAUDE_CODE_SESSION_ID"):  # the caller's own session, which herdr may not know yet
                state["orchestrator"]["session"] = os.environ["CLAUDE_CODE_SESSION_ID"]
            if not any(a.get("pane_id") == here and a.get("name") == ORCH_NAME for a in agents):
                state["orchestrator"].update(revives=0, gave_up=False, agent=None)
            changed = True
    if changed or state.get("_recovered"):
        save_state(state)
    result["kept_worktrees"] = kept_worktrees() or []
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

# what the user decides on first, the steps last: a long brief is cut short in the popup
BRIEF_SECTIONS = ("Workspace", "Approach", "Acceptance criteria", "Check command",
                  "Impact elsewhere", "Questions for the user", "Steps")


def job_brief(job):
    """The plan as the user reads it before approving: the job's absolute path and plan file
    first, then the plan's main sections."""
    info = load_state()["jobs"].get(job)
    if info is None:
        raise StateError("no job " + job)
    kind, value = check_spec(job)
    checked = ("checked by: `%s`" % value if kind == "command" else
               "checked by review only (no command): %s. The reviewer grades each acceptance criterion; "
               "no test runs" % value if kind == "none" else
               "checked by: ? (the plan names no Check command)")
    lines = ["job: %s" % job, "path: %s" % (native_path(info["path"]) if info.get("path") else "path?"),
             "plan: %s" % native_path(os.path.join(oke_home(), "jobs", job, "plan.md")), checked]
    if experiment(info) and not plan_worktree(job) and info.get("git_key"):
        lines.append("result in your checkout: the run's edits stay uncommitted there after the job is done "
                     "(nothing is committed)")
    left = sorted(info.get("byproducts") or {})
    if left:
        lines.append("left by the check (not committed): " + ", ".join(left[:10]) +
                     (" and %d more" % (len(left) - 10) if len(left) > 10 else ""))
    lines.append("")
    for name in BRIEF_SECTIONS:
        text = plan_section(job, name)
        if not text or (name in ("Impact elsewhere", "Questions for the user")
                        and text.lower().startswith("none")):
            continue
        lines += ["## " + name] + brief_lines(text) + [""]
    return "\n".join(lines).rstrip("\n")


BRIEF_LINES = 40  # lines of one plan section the brief shows
BRIEF_CHARS = 300  # characters of one line, and of one line's list of names


def brief_lines(text):
    """A plan section cut to what a person reads in a popup: at most BRIEF_LINES lines, each at
    most BRIEF_CHARS characters (a long comma list says how many more it names)."""
    out = []
    lines = text.splitlines()
    for line in lines[:BRIEF_LINES]:
        if len(line) > BRIEF_CHARS:
            names = line.split(",")
            kept = ""
            for n, part in enumerate(names):
                if len(kept) + len(part) > BRIEF_CHARS:
                    if not kept:  # one long name or sentence
                        line = part[:BRIEF_CHARS] + " ..." + (" and %d more" % (len(names) - 1) if n + 1 < len(names) else "")
                    else:
                        line = kept + " ... and %d more" % (len(names) - n)
                    break
                kept += ("," if n else "") + part
        out.append(line)
    if len(lines) > BRIEF_LINES:
        out.append("... %d more lines in plan.md" % (len(lines) - BRIEF_LINES))
    return out


def caller_is_orchestrator(state, save=True):
    """Is the caller the orchestrator OKe recorded: its Claude Code session, or the runtime's place of
    its record? A new session in the recorded pane (Claude Code restarted there, or /clear) is the
    orchestrator again when no other OKe agent is there: its record takes the new session (saved
    under the lock when `save`, else only in `state`, which the caller saves). Fails closed: an
    unknown caller, or a runtime that does not answer, is not."""
    orch = state.get("orchestrator") or {}
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid and (orch.get("session") == sid or orch.get("pane") == "session:" + sid):
        return True
    pane = runtime().whoami()
    if not pane:
        return False
    try:
        me, agents = whoami(state, pane)
    except HerdrError:
        return False
    if me:
        return me[0] == "orchestrator"
    if pane.startswith("session:") or pane != orch.get("pane"):
        return False
    here = [a for a in agents if a.get("pane_id") == pane]
    others = [r for r in records(state) if r[0] != "orchestrator"]
    if sid and any(r[3].get("session") == sid or r[3].get("pane") == "session:" + sid for r in others):
        return False
    if here and (any(same_agent(r[3], here[0]) for r in others)
                 or here[0].get("name") not in (None, ORCH_NAME, orch.get("agent"))):
        return False  # another agent in a pane id that came back after a herdr restart
    fresh = sid or (session_of(here[0]) if here else None)
    if fresh and fresh != orch.get("session"):
        state.setdefault("orchestrator", {})["session"] = fresh
        if save:
            with locked():
                s = load_state()
                if s["orchestrator"].get("pane") == pane:
                    s["orchestrator"]["session"] = fresh
                    save_state(s)
    return True


ORCHESTRATOR_ACTIONS = {
    "phase": "only the orchestrator sets a job's phase: write your report (`type: done`) and the orchestrator "
             "moves the job",
    "auto": "only the orchestrator turns `job auto` on or off, when the user asks for it",
    "files": "only the orchestrator records the user's answer about their files: report a question "
             "(`asks: user_files`)",
    "accept-check": "only the orchestrator accepts a failing check (the base could not run, or base_has_it), on "
                    "the user's answer: report a question (`asks: check_accept`)",
    "accept-head": "only the orchestrator accepts commits made in the checkout during the job, on the user's "
                   "answer: report a question (`asks: head_moved`)",
}


def user_decision(state, args):
    """The user-only decision this `oke job` call records, or None: plan approval (`phase working`
    from awaiting_approval, unless the job is auto, autonomy is full or the user approved it
    already and it waited), `auto` on, `files`, and `accept-check`/`accept-head --by-user`."""
    info = state["jobs"].get(getattr(args, "slug", None) or "") or {}
    if args.action == "phase":
        if args.phase != "working" or info.get("phase") != "awaiting_approval" or info.get("auto") \
                or info.get("user_approved") or load_config().get("autonomy") == "full":
            return None
        return "approve"
    if args.action == "auto":
        return None if args.off else "auto"
    if args.action == "files":
        return "files"
    if args.action in ("accept-check", "accept-head"):
        return args.action if args.by_user else None
    return None


def user_answer_missing(state, slug, decision):
    """The refusal when the orchestrator records a user's decision with no popup answer for it, or
    None. Seen live: an orchestrator approved plans and put the user's uncommitted files into a job's
    commit without asking. The mod counts AskUserQuestion answers (`oke answered`); without a mod
    (`ask_gate` never set) nothing is checked. One answer covers the calls of one kind for one job."""
    orch = state.get("orchestrator") or {}
    if not orch.get("ask_gate"):
        return None
    seq, spent = orch.get("answers", 0), orch.get("spent") or {}
    if seq > spent.get("seq", 0):
        return None
    if seq and spent.get("seq") == seq and spent.get("job") == slug and spent.get("what") == decision:
        return None
    return ("this records a decision only the user makes, and the user has answered no popup since OKe "
            "last took one: ask them with AskUserQuestion (a popup, not a chat question), then run this again "
            "with their answer. Never decide it for them")


def spend_user_answer(slug, decision, waited=False):
    """Mark the latest popup answer used for `decision` on `slug`; a plan approval that waited
    (exit 3) stays approved for when the job is released."""
    with locked():
        state = load_state()
        orch = state.setdefault("orchestrator", {})
        if orch.get("ask_gate"):
            orch["spent"] = {"seq": orch.get("answers", 0), "job": slug, "what": decision}
        info = state["jobs"].get(slug)
        if info is not None and decision == "approve":
            if waited:
                info["user_approved"] = True
            else:
                info.pop("user_approved", None)
        save_state(state)


def cmd_answered(args):
    """The mod: the orchestrator's user answered an AskUserQuestion popup."""
    with locked():
        state = load_state()
        me, _ = whoami(state, args.pane, args.session)
        if me is None or me[0] != "orchestrator":
            print(json.dumps({"counted": False}))
            return 0
        orch = state.setdefault("orchestrator", {})
        orch["ask_gate"] = True
        orch["answers"] = orch.get("answers", 0) + 1
        save_state(state)
    print(json.dumps({"counted": True}))
    return 0


def cmd_job(args):
    if args.action == "brief":
        if args.json:  # the mod: every job awaiting approval, for the approval popup's preview
            jobs = load_state()["jobs"]
            print(json.dumps({"briefs": [{"job": j, "path": native_path(info["path"]) if info.get("path") else "",
                                     "brief": job_brief(j)}
                                    for j, info in sorted(jobs.items())
                                    if info.get("phase") == "awaiting_approval"],
                              "jobs": sorted(jobs)}))
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
    if args.action in ORCHESTRATOR_ACTIONS:
        # only the orchestrator moves a job: a PM's report does (seen live: a PM set its own job
        # done and never reported it, so nobody archived it). The others record the user's answer.
        state = load_state()
        if not caller_is_orchestrator(state):
            return fail(ORCHESTRATOR_ACTIONS[args.action])
        decision = user_decision(state, args)
        missing = decision and user_answer_missing(state, args.slug, decision)
        if missing:
            return fail(missing)
        args.decision = decision
    if args.action == "triage":
        return cmd_triage(args.slug)
    if args.action == "restore-user":
        return restore_user(args.slug, args.file)
    if args.action in ("accept-check", "accept-head"):
        code = accept_check(args.slug, args.target, args.by_user) if args.action == "accept-check" \
            else accept_head(args.slug, args.by_user)
        if code == 0 and getattr(args, "decision", None):
            spend_user_answer(args.slug, args.decision)
        return code
    if args.action == "plan":  # the PM, after it wrote plan.md: is it short enough to show and check
        path = os.path.join(oke_home(), "jobs", args.slug, "plan.md")
        if not os.path.isfile(path):
            return fail("job %s has no plan.md yet" % args.slug)
        too = plan_too_big(args.slug)
        if too:
            return fail(too)
        print(json.dumps({"job": args.slug, "ok": True, "bytes": os.path.getsize(path)}))
        return 0
    if args.action == "deliver":  # git can take a while: not under the state lock
        return deliver_job(args.slug)
    if args.action == "pr":
        return pr_job(args.slug)
    if args.action == "base":
        return set_base(args.slug, args.branch)
    if args.action == "tree":
        info = load_state()["jobs"].get(args.slug)
        if not info:
            return fail("unknown job: %s" % args.slug)
        if no_path(args.slug, info):
            return fail(no_path(args.slug, info))
        if info.get("direct"):
            return fail("job %s changes a folder outside git: no tree" % args.slug)
        cwd = work_dir(info)
        tree, files = job_tree(args.slug, info, cwd)
        if not tree:
            return fail("git could not read the checkout of %s (%s)" % (args.slug, cwd))
        if nested_block(files, cwd):
            return fail(nested_block(files, cwd))
        head = git_out(cwd, "rev-parse", "HEAD")[1]
        # in place: from the checkout's HEAD when the job began, so a commit made meanwhile shows in the diff
        point = base_point(info, cwd, args.slug) or head
        out = {"job": args.slug, "tree": tree, "files": len(files["paths"]),
               # nothing uncommitted: the review names the commit; else this tree
               "commit": head if not files["paths"] and not in_place(info) else None,
               "diff": "git diff %s %s" % (point, tree), "user_files": files["shared"],
               "new_files": files["new"],  # untracked files the job made: the reviewer reads each whole
               "check_left": files["check_left"]}  # left by the check (not committed)
        moved = head_moved(args.slug, info)
        if moved:
            out.update(head_moved=moved["commits"], next=moved["next"])
        print(json.dumps(out, ensure_ascii=False))
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
    if args.action == "clean":  # runtime and git, and only a finished job: no state lock
        return clean_job(args.slug, args.force)
    if args.action == "archive":
        clean_base_leftovers(only=args.slug)  # a killed check's temporary base worktree
    if args.action == "phase" and args.phase == "working":
        # a job starting in place: the user's files are read (hashed, copied) before the lock
        info = load_state()["jobs"].get(args.slug) or {}
        if info.get("phase") == "awaiting_approval" and in_place(info) and not plan_worktree(args.slug) \
                and not os.path.exists(snapshot_path(args.slug)):
            args.snapshot = take_snapshot(args.slug, info)
    try:
        with locked():
            code = job_locked(args)
        if code in (0, 3) and getattr(args, "decision", None):
            spend_user_answer(args.slug, args.decision, waited=code == 3)
    finally:
        taken = getattr(args, "snapshot", None)
        if taken:
            safe_rmtree(taken[1])  # not kept (the job waits, or the phase was refused): gone
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
    if not m:
        return None
    if m.group(1) == "pass" and any(g not in PASS_GRADES for g in review_grades(path).values()):
        return "changes"  # a criterion PARTIAL, MISSING or fail is never a pass
    return "pass" if m.group(1) == "pass" else "changes"


# a review's `## Acceptance criteria` lines: `- <n>: VERIFIED | PARTIAL | MISSING — <evidence>`
# (an older review: `- <n>: pass | fail — <evidence>`)
# `- <n>: VERIFIED - ...`, also `AC<n>:` or `#<n>:` and bold or backticks around the number or the
# grade. The grade is the word right after the colon, in capitals (`pass`/`fail` of older reviews in
# lowercase): a restated criterion (`1. Verified users can log in - MISSING`) is never read as one.
GRADE_LINE = re.compile(r"^\s*(?:[-*+]\s+)?[*_`]*(?:AC|#)?(\d+)[*_`]*\s*:[*_`]*\s*[*_`]*"
                        r"(VERIFIED|PARTIAL|MISSING|pass|fail)(?![\w-])")
GRADE_WORD = re.compile(r"^[*_`]*(VERIFIED|PARTIAL|MISSING|pass|fail)[*_`]*$")
TABLE_NUMBER = re.compile(r"^[*_`]*(?:AC|#)?(\d+)[*_`]*$")
GRADE_FORM = "`- <n>: VERIFIED | PARTIAL | MISSING - <evidence>`, one line per criterion under `## Acceptance criteria`"
PASS_GRADES = ("verified", "pass")
GRADE_RANK = {"verified": 0, "pass": 0, "partial": 1, "missing": 2, "fail": 2}


def grade_of(line):
    """(criterion number, grade (lowercase)) of one line of a review's Acceptance criteria, or None.
    A table row (`| 1 | Login works | PARTIAL | ... |`) gives its number in the first cell and its
    grade in a cell holding only the grade word."""
    if line.strip().startswith("|"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        number = TABLE_NUMBER.match(cells[0]) if cells else None
        grades = [GRADE_WORD.match(c).group(1).lower() for c in cells[1:] if GRADE_WORD.match(c)]
        if not number or not grades:
            return None
        return int(number.group(1)), max(grades, key=GRADE_RANK.get)
    m = GRADE_LINE.match(line)
    return (int(m.group(1)), m.group(2).lower()) if m else None


def review_grades(path):
    """{criterion number: grade (lowercase)} from a review report's Acceptance criteria section; a
    criterion graded twice keeps its worst grade."""
    out = {}
    for line in (text_section(report_body(path), "Acceptance criteria") or "").splitlines():
        graded = grade_of(line)
        if graded:
            n, grade = graded
            if n not in out or GRADE_RANK[grade] > GRADE_RANK[out[n]]:
                out[n] = grade
    return out


def plan_criteria(job):
    """The numbers of plan.md's Acceptance criteria (`1. ...` or `1) ...`)."""
    return [int(n) for n in re.findall(r"^\s*(\d+)[.)]\s+\S", plan_section(job, u"Acceptance criteria"), re.M)]


def graded_block(job, review):
    """With no Check command (`none (<why>)`) the review is the check: every acceptance criterion
    of the plan must be graded VERIFIED, with its evidence. Why not, or None."""
    grades = review_grades(review)
    criteria = plan_criteria(job)
    if not criteria:
        return "plan.md of %s has no numbered Acceptance criteria for the review to grade" % job
    open_ = [n for n in criteria if grades.get(n) != "verified"]
    if open_:
        return ("job %s has no Check command, so the review is its check: criteria %s are not graded VERIFIED "
                "with evidence in %s: give the reviewer a new instruction to grade every criterion as %s"
                % (job, ", ".join(str(n) for n in open_), review.replace("\\", "/"), GRADE_FORM))
    return None


GIT_TIMEOUT = 300  # seconds for one git command OKe runs itself (a commit with its hooks: COMMIT_TIMEOUT)
COMMIT_TIMEOUT = 900
GH_TIMEOUT = 120  # one `gh` call
PUSH_TIMEOUT = 900
TIMED_OUT = 124  # the exit code OKe gives a command it stopped at its time limit


def run_timed(argv, timeout=GIT_TIMEOUT, input=None, **kw):
    """subprocess.run with a time limit: a command still running then is killed with everything it
    started and gets exit TIMED_OUT, with what it printed so far. Output goes through temporary
    files, not pipes: a process the command left running that holds them cannot stall OKe."""
    files = {}
    for name in ("stdout", "stderr"):
        if kw.get(name) == subprocess.PIPE:
            files[name] = kw[name] = tempfile.TemporaryFile()
    if input is not None:
        files["stdin"] = kw["stdin"] = tempfile.TemporaryFile()
        kw["stdin"].write(input)
        kw["stdin"].seek(0)
    if os.name != "nt":
        kw.setdefault("start_new_session", True)  # its own process group: kill_tree stops it, not OKe
    try:
        p = subprocess.Popen(argv, **kw)
        late = False
        try:
            code = p.wait(timeout)
        except subprocess.TimeoutExpired:
            kill_tree(p)
            code, late = TIMED_OUT, True
        out = {}
        for name in ("stdout", "stderr"):
            if name in files:
                files[name].seek(0)
                out[name] = files[name].read()
        if late and "stdout" in out:
            out["stdout"] += ("\n(OKe stopped it after %d s)" % timeout).encode("utf-8")
        return subprocess.CompletedProcess(argv, code, out.get("stdout"), out.get("stderr"))
    finally:
        for f in files.values():
            f.close()


def call_timed(argv, timeout=GIT_TIMEOUT, **kw):
    """subprocess.call with a time limit: TIMED_OUT when it ran over (it is killed)."""
    try:
        return subprocess.call(argv, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        return TIMED_OUT


def git_out(cwd, *args):
    """(exit code, stripped stdout) of one git command."""
    p = run_timed(["git", "-C", cwd] + list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.returncode, p.stdout.decode("utf-8", "replace").strip()


GATE_PHASES = ("intake", "planning", "awaiting_approval")  # before the job's work starts


def quality_block(job, review_asg, target, cwd=None):
    """Why the review or the check does not count for the current plan, or None. The review
    must name the current criteria_rev; OKe's last `oke check` row for the reviewed commit
    (or tree:<sha>; a check of a commit's tree counts for the commit) must be a pass under it."""
    why = criteria_block(job, review_asg)
    if why:
        return why
    if check_spec(job)[0] == "none":
        return None  # no command to run: the review's grades are the check (graded_block)
    rev = criteria_rev(job)
    same = set([target])
    if cwd and not target.startswith("tree:"):
        same.add("tree:" + git_out(cwd, "rev-parse", "-q", "--verify", target + "^{tree}")[1])
    rows = [r for r in ledger_rows(job) if r["check"] == "check" and r["commit"] in same]
    if not rows or rows[-1]["result"] != "pass" or rows[-1]["detail"].split()[:1] != [rev]:
        return ("no passing `oke check` on %s with the current criteria: run `$OKE check %s`. A failure it "
                "says the user may accept is a question with `asks: check_accept` (only the user answers it)"
                % (target, job))
    return None


def criteria_block(job, review_asg):
    rev = criteria_rev(job)
    if review_asg.get("criteria_rev") != rev:
        return (u"the criteria or the Check command of %s changed since the review (or the review assignment "
                u"names no criteria_rev): give the reviewer a new instruction with `criteria_rev: %s`"
                % (job, rev))
    return None


def job_branch(job, info):
    """The branch a code job works on and delivers: oke/<job>."""
    return "oke/" + job


def carried_tips(job, reviewed):
    """The commits a passing review of `reviewed` counts for: that commit, and each commit
    `job deliver` rebased it to with the same patch id (its `review` rows `carried from`)."""
    rows = [r for r in ledger_rows(job) if r["check"] == "review" and r["result"] == "pass"]
    tips, grew = set([reviewed]), True
    while grew:
        grew = False
        for r in rows:
            words = r["detail"].split()
            if words[:2] == ["carried", "from"] and len(words) > 2 and words[2] in tips and r["commit"] not in tips:
                tips.add(r["commit"])
                grew = True
    return tips


def patch_id(cwd, start, tip):
    """`git patch-id --stable` of the diff start..tip ("" for no change), or None when git fails."""
    diff = run_timed(["git", "-C", cwd, "-c", "core.quotepath=off", "diff", "--no-ext-diff",
                      "--no-textconv", start, tip], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if diff.returncode != 0:
        return None
    if not diff.stdout.strip():
        return ""
    p = run_timed(["git", "-C", cwd, "patch-id", "--stable"], input=diff.stdout,
                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if p.returncode != 0:
        return None
    out = p.stdout.decode("utf-8", "replace").split()
    return out[0] if out else ""


def left_work(job):
    """A cancel or a hold since deliver_job's first checks: deliver nothing."""
    with locked():
        info = load_state()["jobs"].get(job, {})
        if info.get("phase") in ("working", "reviewing") and not info.get("held"):
            return False
    fail("job %s was cancelled or put on hold meanwhile: nothing delivered" % job)
    return True


NOT_CODE = "the job has no branch to deliver (its output is not code): set phase done after its report"


def deliver_result(job, info):
    """`job deliver` of a result (output result, reviewed): it stays uncommitted where the job made
    it. Delivery needs the run `$OKE check` recorded on the edit as it is now (under the current
    criteria; its exit code is the reviewer's to grade); nothing is committed and the user's own
    uncommitted files are not touched. Prints where the result is."""
    cwd, target, files = check_dir(job, info)
    if not target:
        return fail("cannot read the tree of %s in %s" % (job, cwd))
    rev = criteria_rev(job)
    if not any(r["check"] == "check" and r["result"] in ("ran", "pass") and r["commit"] == target
               and r["detail"].split()[:1] == [rev] for r in ledger_rows(job)):
        return fail("job %s: no recorded run of its result as it is now: run `$OKE check %s`, then deliver"
                    % (job, job))
    print(json.dumps({"job": job, "result": "in_place", "path": cwd.replace("\\", "/"),
                      "results": plan_files(job, "Results") or
                      ((files or {}).get("paths") or []) + ((files or {}).get("check_left") or []),
                      "committed": False,
                      "next": "tell the user where the result is, then `$OKE job phase %s done`" % job},
                     ensure_ascii=False))
    return 0


def deliver_job(job):
    """`job deliver`: hand the reviewed work over, never into the main branch. A worktree job's
    branch is brought onto the latest base branch inside its worktree and must pass the check
    there; a job without a worktree gets its reviewed changes committed in the main checkout.
    Only the work the last passing review covered: a worker that reported again needs a new
    review first (seen live: a PM skipped it)."""
    info = load_state()["jobs"].get(job)
    if not info:
        return fail("unknown job: %s" % job)
    if info.get("held"):
        return fail("job %s is on hold: nothing is delivered until it is back" % job)
    if not_code(info) and not experiment(info):
        return fail("job %s: %s" % (job, NOT_CODE))
    if no_path(job, info):
        return fail(no_path(job, info))
    if info.get("phase") not in ("working", "reviewing"):
        return fail("job %s is in phase %s: only a job at work is delivered" % (job, info.get("phase")))
    job_dir = os.path.join(oke_home(), "jobs", job)
    worker = latest_report(job_dir, r"-w\d+$")
    review = latest_report(job_dir, r"-rv\d*$")
    if not review:
        return fail("job %s has no review report: have the reviewer review the branch first" % job)
    if review_verdict(review[1]) != "pass":
        return fail("the last review of %s did not pass: %s" % (job, review[1].replace("\\", "/")))
    if check_spec(job)[0] == "none" and graded_block(job, review[1]):
        return fail(graded_block(job, review[1]))
    if worker and worker[0] > review[0]:
        return fail("the worker reported after the last review (a rebase or a fix): give the reviewer "
                    "a new instruction to review it, then deliver")
    if not_code(info):
        return deliver_result(job, info)
    main = checkout_dir(info)
    # The review counts only for the commit its assignment named, and only once the
    # report answers that assignment: commits added after it were never reviewed.
    review_asg = read_header(os.path.join(os.path.dirname(review[1]), "assignment.md")) or {}
    review_seq = (read_header(review[1]) or {}).get("seq")
    if review_seq != review_asg.get("seq"):
        return fail("the review report of %s does not answer its current assignment (seq %s): wait for "
                    "the reviewer" % (job, review_asg.get("seq")))
    branch = job_branch(job, info)
    wt = (info.get("worktree") or {}).get("path")
    if not wt and review_asg.get("tree"):
        # in place: the review covered the job's uncommitted changes in its checkout
        why = quality_block(job, review_asg, "tree:" + review_asg["tree"])
        if why:
            return fail(why)
        with locked("deliver-" + job, timeout=120):
            return commit_direct(job, info, main, review[1], review_asg["tree"])
    if wt and review_asg.get("tree") and os.path.isdir(native_path(wt)):
        # its own worktree, reviewed as its commits plus what is left uncommitted: commit what is left
        why = criteria_block(job, review_asg)
        if why:
            return fail(why)
        with locked("deliver-" + job, timeout=120):
            if commit_leftover(job, info, native_path(wt), review[1], review_asg["tree"]) is None:
                return 1
    code, tip = git_out(main, "rev-parse", "-q", "--verify", "refs/heads/%s^{commit}" % branch)
    if code != 0:
        return fail("branch %s does not exist" % branch)
    if review_asg.get("tree"):
        reviewed = reviewed_full = "tree:" + review_asg["tree"]
    else:
        reviewed = review_asg.get("commit")
        if not reviewed or not re.match(r"^[0-9a-f]{7,40}$", reviewed):
            # a branch name would resolve to the moving tip and pass any check
            return fail("the review assignment of %s names no commit sha in `commit:`: give the reviewer a new "
                        "instruction with `commit: %s`, then deliver" % (job, tip))
        code, reviewed_full = git_out(main, "rev-parse", "-q", "--verify", reviewed + "^{commit}")
    if code != 0 or tip not in carried_tips(job, reviewed_full):
        return fail("%s is at %s but the review covered %s: give the reviewer a new instruction "
                    "with `commit: %s`, then deliver" % (branch, tip[:12], reviewed, tip))
    why = quality_block(job, review_asg, tip, main) if tip == reviewed_full or reviewed.startswith("tree:") \
        else criteria_block(job, review_asg)
    if why:
        return fail(why)
    base = info.get("base")
    if not base:
        return fail("job %s has no base branch (the main checkout was on a detached HEAD when it began): "
                    "report a question: the user names the branch it goes onto, and the orchestrator runs "
                    "`$OKE job base %s <branch>`" % (job, job))
    code, base_head = git_out(main, "rev-parse", "-q", "--verify", "refs/heads/%s^{commit}" % base)
    if code != 0:
        return fail("the base branch %s of job %s does not exist in %s: report a question" % (base, job, main))
    by = os.path.basename(os.path.dirname(review[1]))
    if info.get("continues"):  # a hand rebase (after a conflict) leaves start_commit behind
        start = review_base(job, info, tip)
        if start and start != info.get("start_commit"):
            with locked():
                state = load_state()
                if job in state["jobs"]:
                    state["jobs"][job]["start_commit"] = start
                    save_state(state)
            info = dict(info, start_commit=start)
    with locked("deliver-" + job, timeout=120):
        # the base branch holds it already (merged by hand, or an older OKe merged it): never rewritten
        already_in = git_out(main, "merge-base", "--is-ancestor", tip, base_head)[0] == 0
        if not already_in and git_out(main, "merge-base", "--is-ancestor", base_head, tip)[0] != 0:
            return rebase_onto(job, info, branch, tip, base, base_head, by)
        why = quality_block(job, review_asg, tip, main)  # a rebased tip is checked again (its tree may be)
        if why:
            return fail(why)
        if left_work(job):
            return 1
        rows = ledger_rows(job)
        if not any(r["check"] == "review" and r["result"] == "pass" and r["commit"] == tip for r in rows):
            ledger_add(job, by, tip, "review", "pass", review[1].replace("\\", "/"))
        last = deliver_state(job)[1]
        if not last or last["commit"] != tip:
            ledger_add(job, "oke", tip, "deliver", "ok", "branch=%s base=%s@%s%s"
                       % (branch, base, base_head, " already-in" if already_in else ""))
    wt = (info.get("worktree") or {}).get("path")
    out = {"result": "delivered", "job": job, "branch": branch, "base": base, "commit": tip,
           "worktree": native_path(wt) if wt else None, "main": main, "merge": "git merge %s" % branch}
    if already_in:
        out["already_in"] = True  # nothing left to merge: the base branch has it
    if info.get("continues"):
        out["contains"] = "oke/" + info["continues"]  # merging this branch brings that job's commits too
    print(json.dumps(out, ensure_ascii=False))
    return 0


def rebase_onto(job, info, branch, tip, base, base_head, by):
    """Bring the job's branch onto the base branch's head inside its worktree. Exit 4: rebased
    with the same change (the review still counts), the check must run on the new tip; exit 2:
    a conflict, undone; exit 1: refused, or a different change that needs a new review."""
    wt = (info.get("worktree") or {}).get("path")
    if not wt or not os.path.isdir(native_path(wt)):
        return fail("%s is behind %s, and job %s has no worktree to rebase it in: report a question"
                    % (branch, base, job))
    cwd = native_path(wt)
    for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD"):  # mid-rebase HEAD is detached: ask this first
        where = git_out(cwd, "rev-parse", "--git-path", name)[1]
        if where and os.path.exists(os.path.join(cwd, where)):
            return fail("a rebase or merge is in progress in %s: finish or abort it first" % cwd)
    if git_out(cwd, "symbolic-ref", "-q", "--short", "HEAD")[1] != branch:
        return fail("the worktree %s is not on %s: report a question" % (cwd, branch))
    code, dirty = git_out(cwd, "status", "--porcelain", "-uno")
    if code != 0 or dirty:
        return fail("the worktree %s has uncommitted changes (the reviewed work is committed):\n%s" % (cwd, dirty))
    fork = git_out(cwd, "merge-base", base_head, tip)[1]
    start = fork
    own = own_commits(cwd, info, tip)  # a continuing job: only the commits after the job it continues
    if own is not None:
        start = info["start_commit"]
        linear = git_out(cwd, "rev-list", "--merges", "%s..%s" % (start, tip))[1] == ""
        total = git_out(cwd, "rev-list", "--count", "%s..%s" % (fork, tip))[1] if fork else ""
    before = patch_id(cwd, start, tip) if start else None
    if before is None:
        return fail("git cannot read the change of %s in %s" % (branch, cwd))
    if left_work(job):
        return 1
    result = run_timed(["git", "-C", cwd, "rebase", "-q", base_head], COMMIT_TIMEOUT, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    if result.returncode != 0:
        files = git_out(cwd, "diff", "--name-only", "--diff-filter=U")[1].split()
        if call_timed(["git", "-C", cwd, "rebase", "--abort"], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL) != 0 and git_out(cwd, "rev-parse", "HEAD")[1] != tip:
            return fail("the rebase of %s onto %s stopped and `git rebase --abort` failed: %s is mid-rebase, "
                        "report a question" % (branch, base, cwd))
        if not files:
            return fail("git rebase failed: %s" % result.stdout.decode("utf-8", "replace").strip())
        print(json.dumps({"result": "conflict", "job": job, "branch": branch, "base": base, "files": files}))
        return 2
    new = git_out(cwd, "rev-parse", "HEAD")[1]
    new_start = base_head
    if own is not None:
        # its own commits are the last `own` of the rebased branch only when the rebase kept every
        # commit as one (no merges, nothing dropped) and they sit above the base; else the wider
        # range from the base: a wider review, never a narrower one
        moved = git_out(cwd, "rev-parse", "-q", "--verify", "%s~%d" % (new, own))[1] if own else new
        kept = git_out(cwd, "rev-list", "--count", "%s..%s" % (base_head, new))[1]
        if linear and moved and total and kept == total \
                and git_out(cwd, "rev-list", "--count", "%s..%s" % (moved, new))[1] == str(own) \
                and git_out(cwd, "merge-base", "--is-ancestor", base_head, moved)[0] == 0:
            new_start = moved
        with locked():
            state = load_state()
            if job in state["jobs"]:
                state["jobs"][job]["start_commit"] = new_start
                save_state(state)
    after = patch_id(cwd, new_start, new)
    if after != before or not after:
        return fail("%s was rebased onto %s (%s, was %s), and its change is not the reviewed one any more (another "
                    "patch id): give the reviewer a new instruction with `commit: %s`, run `$OKE check %s`, then "
                    "deliver again" % (branch, base, new[:12], tip[:12], new, job))
    ledger_add(job, by, new, "review", "pass", "carried from %s (rebased onto %s %s, same patch id)"
               % (tip, base, base_head))
    print(json.dumps({"result": "rebased", "job": job, "branch": branch, "base": base, "from": tip, "commit": new,
                      "next": "run `$OKE check %s` in the background (no time limit), then `$OKE job deliver %s` "
                              "again" % (job, job)}))
    return 4


def deliver_block(job, info):
    """Why a code job may not be done yet, or None: its last `deliver ok` row must name its
    branch's tip (a job without a worktree: be there at all)."""
    status, row = deliver_state(job)
    if status != "delivered":
        return "job %s is not delivered: `$OKE job deliver %s` after its review passed, before done" % (job, job)
    words = row["detail"].split()
    if words and words[0].startswith("branch=") and info.get("git_key"):
        branch = words[0][len("branch="):]
        code, tip = git_out(checkout_dir(info), "rev-parse", "-q", "--verify",
                            "refs/heads/%s^{commit}" % branch)
        if code == 0 and tip != row["commit"]:
            return ("job %s: %s moved to %s after its delivery at %s: `$OKE job deliver %s` again (with a new "
                    "review of a new change)" % (job, branch, tip[:12], row["commit"][:12], job))
    return None


def set_base(job, branch):
    """`job base`: the user names the branch a job goes onto (its main checkout was detached
    when it began, so it has none). Only before the job is delivered; a local branch."""
    with locked():
        state = load_state()
        info = state["jobs"].get(job)
        if info is None:
            return fail("unknown job: %s" % job)
        if info.get("phase") in DONE_PHASES:
            return fail("job %s is %s: its base branch stays" % (job, info.get("phase")))
        if not info.get("git_key") or not_code(info):
            return fail("job %s has no branch to deliver: no base branch" % job)
        if deliver_state(job)[0] == "delivered":
            return fail("job %s is delivered onto %s already: its base branch stays" % (job, info.get("base")))
        name = branch[len("refs/heads/"):] if branch.startswith("refs/heads/") else branch
        main = checkout_dir(info)
        if name.startswith("oke/") or git_out(main, "rev-parse", "-q", "--verify", "refs/heads/" + name)[0] != 0:
            return fail("%s is no local branch of %s (and never an oke/ branch): name one `git branch` lists"
                        % (branch, main))
        info["base"] = name
        save_state(state)
    print(json.dumps({"job": job, "base": name}))
    return 0


def gh_argv():
    """The GitHub CLI as an argv prefix, or None. OKE_GH (a JSON list) replaces it in tests."""
    if os.environ.get("OKE_GH"):
        return json.loads(os.environ["OKE_GH"])
    gh = shutil.which("gh")
    return [gh] if gh else None


def job_record(job):
    """(info, archive folder) of a job: its state entry (folder None), else the job.json of its
    newest archive; (None, None) when OKe knows no such job."""
    info = load_state()["jobs"].get(job)
    if info is not None:
        return info, None
    folder = archived_dir(job)
    kept = read_json(os.path.join(folder, "job.json"), None) if folder else None
    return (kept, folder) if isinstance(kept, dict) and kept else (None, None)


# hosts that are never GitHub: a self-hosted name may be GitHub Enterprise (gh decides, by its login)
NOT_GITHUB = re.compile(r"(?:^|\.)(?:gitlab\.com|bitbucket\.org|codeberg\.org|dev\.azure\.com|"
                        r"visualstudio\.com|sr\.ht)$", re.I)


def pr_job(job):
    """`job pr`: push the delivered branch to origin and open a pull request onto its base."""
    info, folder = job_record(job)
    if info is None:
        return fail("unknown job: %s" % job)
    if not_code(info) or not info.get("git_key"):
        return fail("job %s has no branch (its output is not code)" % job)
    status, row = deliver_state(job, os.path.join(folder, "ledger.tsv") if folder else None)
    if status != "delivered":
        return fail("job %s is not delivered yet: no pull request" % job)
    if not row["detail"].split()[:1] or not row["detail"].split()[0].startswith("branch="):
        return fail("job %s was committed in the main checkout (no branch): nothing for a pull request" % job)
    branch, base = job_branch(job, info), info.get("base")
    main = checkout_dir(info)
    if git_out(main, "rev-parse", "-q", "--verify", "refs/heads/" + branch)[0] != 0:
        return fail("branch %s is gone (merged or deleted)" % branch)
    code, origin = git_out(main, "remote", "get-url", "origin")
    if code != 0:
        return fail("%s has no remote `origin`: no pull request" % main)
    where = remote_repo(origin)
    if not where:
        return fail("cannot read host, owner and repository from remote `origin` of %s (%s): no pull request; "
                    "the user pushes %s themselves" % (main, origin, branch))
    host, owner, repo = where
    gh = gh_argv()
    if not gh:
        return fail("the GitHub CLI `gh` is not installed: install it (https://cli.github.com) and run "
                    "`gh auth login --hostname %s`, or push %s and open the pull request yourself" % (host, branch))
    env = dict(os.environ, GH_HOST=host)  # GitHub Enterprise too: gh talks to the remote's host

    def gh_run(argv):
        try:
            r = run_timed(gh + argv, GH_TIMEOUT, cwd=main, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)
        except OSError as e:
            return None, str(e)
        return r.returncode, r.stdout.decode("utf-8", "replace")
    code, text = gh_run(["auth", "status", "--hostname", host])
    if code is None:
        return fail("could not run gh: %s" % text)
    if code != 0 and "." not in host:
        # an SSH host alias (`git@work:owner/repo` with `Host work` in ~/.ssh/config): its real host name
        real = ssh_hostname(host)
        if not real or "." not in real:
            return fail("can't tell the GitHub host for remote %s of %s: %s is no host gh knows. The user pushes %s "
                        "and opens the pull request themselves" % (origin, main, host, branch))
        host = real
        env["GH_HOST"] = host
        code, text = gh_run(["auth", "status", "--hostname", host])
        if code is None:
            return fail("could not run gh: %s" % text)
    if code != 0 and NOT_GITHUB.search(host):
        return fail("%s is not a GitHub host (origin of %s): `job pr` opens GitHub pull requests only; the user "
                    "pushes %s and opens the request there" % (host, main, branch))
    if code != 0:
        return fail("gh is not logged in to %s: run `gh auth login --hostname %s`, then `$OKE job pr %s` again"
                    % (host, host, job))
    # OKe owns oke/* branches: a later rebase moves them, and the open pull request follows
    ref = "refs/heads/" + branch
    target = git_out(main, "remote", "get-url", "--push", "origin")[1] or "origin"  # where the push goes
    code, remote = git_out(main, "ls-remote", target, ref)
    if code != 0:
        return fail("git cannot reach origin of %s: %s" % (main, remote))
    seen = remote.split()[0] if remote.split() else ""  # "" : the branch must not be there yet
    push = run_timed(["git", "-C", main, "push", "-q", "--force-with-lease=%s:%s" % (ref, seen), "origin",
                      "%s:%s" % (ref, ref)], PUSH_TIMEOUT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if push.returncode != 0:
        return fail("git push of %s to origin failed: %s" % (branch, push.stdout.decode("utf-8", "replace").strip()))
    slug = "%s/%s" % (owner, repo)
    code, text = gh_run(["pr", "list", "--repo", slug, "--head", branch, "--state", "open", "--json", "url", "--jq", ".[0].url"])
    if code is None:
        return fail("could not run gh: %s" % text)
    urls = re.findall(r"https?://\S+", text) if code == 0 else []
    if urls:  # the branch has its pull request already: the push updated it
        print(json.dumps({"job": job, "branch": branch, "base": base, "url": urls[-1], "existing": True}))
        return 0
    code, text = gh_run(["pr", "create", "--repo", slug, "--base", base, "--head", branch, "--fill"])
    if code is None:
        return fail("could not run gh: %s" % text)
    urls = re.findall(r"https?://\S+", text)
    if code != 0 and not (urls and "already exists" in text):
        return fail("gh pr create failed: %s" % text.strip())
    if not urls:
        return fail("gh pr create printed no URL: %s" % text.strip())
    out = {"job": job, "branch": branch, "base": base, "url": urls[-1]}
    if code != 0:
        out["existing"] = True
    print(json.dumps(out))
    return 0


def ssh_hostname(alias):
    """The host name ssh connects to for `alias` (`ssh -G`, which reads ~/.ssh/config), or None."""
    try:
        r = run_timed(["ssh", "-G", alias], 15, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        if line.lower().startswith("hostname "):
            return line.split(None, 1)[1].strip().lower()
    return None


# a remote URL: https://host(:port)/owner/repo(.git), ssh://git@host(:port)/owner/repo, git@host:owner/repo
REMOTE_URL = re.compile(r"^(?:(?:https?|ssh|git)://(?:[^@/]+@)?(?P<h1>[^/:]+)(?::\d+)?/"
                        r"|(?:[^@/]+@)?(?P<h2>[^/:]+):)(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")


def remote_repo(url):
    """(host, owner, repo) of a remote URL on any host (github.com or a GitHub Enterprise server), or None."""
    m = REMOTE_URL.match(url.strip())
    if not m:
        return None
    return (m.group("h1") or m.group("h2")).lower(), m.group("owner"), m.group("repo")


# An untracked byproduct line of `git status --porcelain`: Python caches only.
# a cache folder, also each file in it (`git status -uall` lists files, not the folder)
BYPRODUCT = re.compile(r"^\?\? (.*/)?(?:(?:__pycache__|\.pytest_cache)(?:/.*)?|[^/]*\.py[co])$")

def byproduct(line):
    """An untracked line of `git status --porcelain` that is never the job's work: Python
    caches, or a folder config `merge_ignore_untracked` names (another tool's state)."""
    if not line.startswith("?? "):
        return False
    path = line[3:].strip('"')
    return bool(BYPRODUCT.match(line)) or any(
        path == p or path.startswith(p.rstrip("/") + "/") for p in load_config().get("merge_ignore_untracked") or [])


def head_moved(job, info):
    """In place: the commits made in the job's checkout since the job began that the user did not
    accept yet, as {"result": "head_moved", "commits", "next"}, else None. A worker never commits in
    place (the mod blocks it; a commit made another way, `bash -c` say, still shows here)."""
    snap = read_json(snapshot_path(job), None) if in_place(info) else None
    if not isinstance(snap, dict) or not snap.get("head"):
        return None
    main = checkout_dir(info)
    head = git_out(main, "rev-parse", "HEAD")[1]
    if not head or head == snap["head"]:
        return None
    commits = git_out(main, "log", "--format=%h %s", "-n", "20", "%s..HEAD" % snap["head"])[1].splitlines()
    return {"result": "head_moved", "job": job, "head_then": snap["head"][:12], "commits": commits,
            "next": "the checkout %s has %d commit(s) since job %s began (HEAD was %s): OKe does not know who "
                    "made them. Report a question with `asks: head_moved` naming them: did the user make them? "
                    "Options: yes, I made them (the orchestrator runs `$OKE job accept-head %s --by-user` and the "
                    "job goes on) / no (the job stops and the commits stay as they are: OKe never resets "
                    "commits). A worker never commits in place" % (main, len(commits), job, snap["head"][:12], job)}


def commit_direct(job, info, main, review, reviewed):
    """A job in place (the main checkout, or a linked worktree the user named): commit its reviewed
    changes there, on the branch it is on, one commit per `## Commits` group, and record them as its
    delivery. Only the job's own files: the user's other uncommitted files stay as they are. The
    check already passed on that very tree; anything changed since the review is refused."""
    if git_out(main, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0:
        return fail("a merge is in progress in %s: leave it to the user" % main)
    base = info.get("base")
    current = git_out(main, "symbolic-ref", "-q", "--short", "HEAD")[1]
    if not current or (base and current != base):
        return fail("the checkout %s is on %r, not on the job's base branch %r: ask the user"
                    % (main, current or "a detached HEAD", base))
    by = os.path.basename(os.path.dirname(review))
    snap = read_json(snapshot_path(job), None)
    if not isinstance(snap, dict) and not any(v == "include" for v in (info.get("user_files") or {}).values()):
        return fail("job %s has no record of the user's uncommitted files from its start, so OKe cannot tell "
                    "its files from the user's: report a question (`asks: user_files`) naming the changed files; "
                    "the orchestrator records the user's answer with `$OKE job files %s include <file>...`, and "
                    "only those are committed" % (job, job))
    moved = head_moved(job, info)
    if moved:
        return fail(moved["next"])
    tree, files = job_tree(job, info, main)
    if not tree:
        return fail("git could not read the changes of %s in %s" % (job, main))
    if nested_block(files, main):
        return fail(nested_block(files, main))
    if files["shared"]:
        # the job changed files that held the user's own uncommitted edits: committing them takes those too
        print(json.dumps({"result": "user_files", "job": job, "files": files["shared"],
                          "next": "report a question with `asks: user_files` (only the user answers it): for "
                                  "each file, include the user's edits in the job's commit, or leave the file "
                                  "out; the orchestrator records it (`$OKE job files %s include|leave <file>...`)"
                                  % job}, ensure_ascii=False))
        return 5
    if tree != reviewed:
        return fail("the changes in %s are not the ones the review covered (tree %s, reviewed %s): give "
                    "the reviewer a new instruction with `tree: %s`, then deliver" % (main, tree, reviewed, tree))
    pre = git_out(main, "rev-parse", "HEAD")[1]
    if not files["paths"]:
        if git_out(main, "rev-parse", "HEAD^{tree}")[1] != reviewed:
            return fail("job %s has no branch oke/%s and no changes in %s to commit" % (job, job, main))
        # the review passed on the checkout as it is (the change was already there): delivered at HEAD
        if left_work(job):
            return 1
        ledger_add(job, by, pre, "review", "pass", review.replace("\\", "/"))
        ledger_add(job, "oke", pre, "deliver", "ok", "main already-in")
        print(json.dumps({"result": "delivered", "job": job, "commit": pre, "direct": True, "main": main,
                          "already_in": True}, ensure_ascii=False))
        return 0
    if left_work(job):
        return 1
    made, why = commit_job(job, main, files["paths"], reviewed)
    if why:
        return fail(why)
    if isinstance(snap, dict):  # OKe's own commits: a later round of the job starts from them
        snap["head"] = made[-1]
        write_json_atomic(snapshot_path(job), snap)
    ledger_add(job, by, made[-1], "review", "pass", review.replace("\\", "/"))
    ledger_add(job, "oke", made[-1], "deliver", "ok", "main pre=%s" % pre)
    print(json.dumps({"result": "delivered", "job": job, "commit": made[-1], "commits": made, "direct": True,
                      "main": main, "branch_in": current, "left": files["left"], "user_files_kept": files["user"]},
                     ensure_ascii=False))
    return 0


def commit_leftover(job, info, cwd, review, reviewed):
    """A job in its own worktree whose review covered a tree: its commits plus what is left
    uncommitted. Commit what is left (one commit per `## Commits` group) and return the new tip, which
    the review counts for; the worker's commits stay as they are. None after printing why not."""
    if git_out(cwd, "symbolic-ref", "-q", "--short", "HEAD")[1] != "oke/" + job:
        fail("the worktree %s is not on oke/%s: report a question" % (cwd, job))
        return None
    tree, files = job_tree(job, info, cwd)
    if not tree:
        fail("git could not read the changes of %s in %s" % (job, cwd))
        return None
    if nested_block(files, cwd):
        fail(nested_block(files, cwd))
        return None
    head = git_out(cwd, "rev-parse", "HEAD")[1]
    if not files["paths"] and head in carried_tips(job, "tree:" + reviewed):
        return head  # committed by an earlier delivery (and maybe rebased since)
    if tree != reviewed:
        fail("the work in %s is not the one the review covered (tree %s, reviewed %s): give the reviewer a "
             "new instruction with `tree: %s`, then deliver" % (cwd, tree, reviewed, tree))
        return None
    if files["paths"]:
        if left_work(job):
            return None
        made, why = commit_job(job, cwd, files["paths"], reviewed)
        if why:
            fail(why)
            return None
        head = made[-1]
    if head not in carried_tips(job, "tree:" + reviewed):
        ledger_add(job, os.path.basename(os.path.dirname(review)), head, "review", "pass",
                   "carried from tree:%s (committed what was left)" % reviewed)
    return head


# ---------------------------------------------------------------- continue a finished job

def continue_start(job, info, other):
    """Where a job that continues finished job `other` starts: its own branch oke/<job> starts
    at the tip of oke/<other> (`other`'s worktree and branch are left alone), on `other`'s base.
    Returns {commit, base}, or None after printing why not."""
    if other == job:
        fail("job %s cannot continue itself: name another job in `from oke/<job>`" % job)
        return None
    was, _ = job_record(other)
    if was is None:
        fail("job %s continues job %s, which is no job OKe knows" % (job, other))
        return None
    if was.get("phase") != "done" or not_code(was):
        fail("job %s is %s: a job continues only a done code job (an open job takes the new request as new "
             "requirements)" % (other, was.get("phase") if was.get("phase") != "done" else "not a code job"))
        return None
    if was.get("git_key") != info.get("git_key"):
        fail("job %s worked in another checkout than job %s" % (other, job))
        return None
    main = checkout_dir(info)
    mine = "refs/heads/oke/" + job
    has_mine = git_out(main, "rev-parse", "-q", "--verify", mine)[0] == 0
    if has_mine and info.get("continues") == other and info.get("start_commit"):
        # made by an earlier run: oke/<other> may be merged and deleted since
        return {"commit": info["start_commit"], "base": info.get("base") or was.get("base")}
    code, tip = git_out(main, "rev-parse", "-q", "--verify", "refs/heads/oke/%s^{commit}" % other)
    if code != 0:
        fail("branch oke/%s of job %s is gone (merged and deleted?): plan job %s with its own worktree from "
             "the base branch, without `from oke/%s`" % (other, other, job, other))
        return None
    if has_mine:
        if git_out(main, "merge-base", "--is-ancestor", tip, mine)[0] != 0:
            fail("branch oke/%s exists and does not start at oke/%s: report a question" % (job, other))
            return None
    return {"commit": tip, "base": was.get("base")}


def review_base(job, info, tip=None):
    """Where a continuing job's own work starts below `tip` (default: its branch's tip): its
    start_commit while that is below the tip, else (a rebase by hand moved the branch) where the
    branch leaves its base branch. None for a job that continues nothing."""
    if not info.get("continues") or not info.get("git_key"):
        return None
    main = checkout_dir(info)
    tip = tip or git_out(main, "rev-parse", "-q", "--verify", "refs/heads/oke/%s^{commit}" % job)[1]
    start = info.get("start_commit")
    if not tip:
        return start
    if start and git_out(main, "merge-base", "--is-ancestor", start, tip)[0] == 0:
        return start
    if info.get("base"):
        code, fork = git_out(main, "merge-base", "refs/heads/" + info["base"], tip)
        if code == 0 and fork:
            return fork
    return start


def own_commits(cwd, info, tip):
    """How many commits of a continuing job's branch are its own (after its start_commit), or
    None for a job that continues nothing (or whose start is no longer below the tip)."""
    start = info.get("start_commit")
    if not info.get("continues") or not start:
        return None
    if git_out(cwd, "merge-base", "--is-ancestor", start, tip)[0] != 0:
        return None
    code, n = git_out(cwd, "rev-list", "--count", "%s..%s" % (start, tip))
    return int(n) if code == 0 and n.isdigit() else None


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
OKE_CHECKS = ("review", "deliver", "check")


def check_detail(detail):
    """A check row's detail as (its words before `log=`, the log path or None): the path is last and
    may hold spaces."""
    m = re.search(r"(?:^| )log=", detail or "")
    if not m:
        return (detail or "").split(), None
    return detail[:m.start()].split(), detail[m.end():]


def deliver_state(job, path=None):
    """(state, last delivery row): state is none or delivered. `path`: a ledger.tsv elsewhere
    (an archived job's)."""
    rows = [r for r in ledger_rows(job, path) if r["check"] == "deliver" and r["result"] == "ok"]
    return ("delivered", rows[-1]) if rows else ("none", None)


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


def check_timeout():
    """Seconds one run of the Check command may take (config `check_timeout`, minutes)."""
    try:
        minutes = float(load_config().get("check_timeout") or 30)
    except (TypeError, ValueError):
        minutes = 30.0
    return max(1.0, minutes * 60)


def kill_tree(p):
    """Stop a process and everything it started."""
    try:
        if os.name == "nt":
            call_timed(["taskkill", "/F", "/T", "/PID", str(p.pid)], 60, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        p.kill()
    except OSError:
        pass
    try:
        p.wait(60)
    except subprocess.TimeoutExpired:
        pass


def run_check(command, cwd, log):
    """Exit code of `command` in bash (else the platform shell), output to `log`. A run longer than
    check_timeout() is stopped, with everything it started: exit TIMED_OUT, and a last log line says so."""
    bash = find_bash()
    argv = [bash, "-c", command] if bash else command
    limit = check_timeout()
    extra = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with io.open(log, "wb") as f:
        p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                             shell=bash is None, **extra)
        try:
            return p.wait(limit)
        except subprocess.TimeoutExpired:
            kill_tree(p)
            f.write(("\n" + CHECK_TIMED_OUT % (limit / 60) + "\n").encode("utf-8"))
            return TIMED_OUT


CHECK_TIMED_OUT = "OKe stopped the Check command after %g min (check_timeout)"


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


def run_signatures(command, cwd, log):
    """Run the command (no time limit) and reduce its run to exit code and signatures."""
    code = run_check(command, cwd, log)
    unrunnable = code in (126, 127)
    if code == 0 or unrunnable:
        return {"exit": code, "unrunnable": unrunnable, "signatures": []}
    if code == TIMED_OUT and log_ends_timed_out(log):
        return {"exit": code, "unrunnable": False, "timed_out": True,
                "signatures": ["<%s>" % CHECK_TIMED_OUT % (check_timeout() / 60)]}
    with io.open(log, encoding="utf-8", errors="replace") as f:
        return {"exit": code, "unrunnable": False, "signatures": signature_lines(f.read(), cwd)}


def log_ends_timed_out(log):
    try:
        with io.open(log, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200))
            return b"(check_timeout)" in f.read()
    except (IOError, OSError):
        return False


BASELINE_VERSION = 3  # bump whenever signature_lines changes what a failure line reduces to


def plan_section(job, name):
    try:
        with io.open(os.path.join(oke_home(), "jobs", job, "plan.md"), encoding="utf-8") as f:
            text = f.read()
    except (IOError, OSError, ValueError):
        return ""
    m = re.search(r"^## " + re.escape(name) + r"[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


# `none (<why>)`, `` `none` (<why>) `` or `` `none (<why>)` ``: no command checks the job
NO_CHECK = re.compile(r"^`?none(?![\w-])", re.I)  # a Check command section whose first line starts so


def check_spec(job):
    """("command", <the command>) for the first backticked span of plan.md's Check command
    section; ("none", <why>) when its first line is `none (<why>)`: the review alone checks the
    job; (None, None) when it names neither."""
    section = plan_section(job, u"Check command")
    first = next((l.strip() for l in section.splitlines() if l.strip()), "")
    if NO_CHECK.match(first):  # never a command, whatever its reason holds
        return "none", none_reason(first)
    m = re.search(r"`([^`]+)`", section)
    return ("command", m.group(1).strip()) if m else (None, None)


def none_reason(line):
    """The <why> of a `none (<why>)` line: what its first parentheses hold (backticks and nested
    parentheses kept); text after them is left out. "" when it gives none."""
    start = line.find("(")
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(line)):
        depth += {"(": 1, ")": -1}.get(line[i], 0)
        if depth == 0:
            return line[start + 1:i].strip()
    return line[start + 1:].strip().rstrip("`").strip()


def plan_command(job):
    """The Check command, or None (none, or not named)."""
    kind, value = check_spec(job)
    return value if kind == "command" else None


def criteria_rev(job):
    """Changes whenever the acceptance criteria or the Check command change."""
    kind, value = check_spec(job)
    text = plan_section(job, u"Acceptance criteria") + "\n" + (
        value if kind == "command" else "none" if kind == "none" else "")
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_dir(job, info):
    """(cwd, what is checked, the job's files): its worktree and HEAD when nothing is left
    uncommitted there, else tree:<sha> of HEAD with the job's own changes (what `job tree` prints
    and a review of uncommitted work names)."""
    cwd = work_dir(info)
    tree, files = job_tree(job, info, cwd)
    if not in_place(info) and files is not None and not files["paths"]:
        code, head = git_out(cwd, "rev-parse", "HEAD")
        return cwd, head if code == 0 else None, files
    return cwd, "tree:" + tree if tree else None, files


def no_check(job):
    """`oke baseline` and `oke check` on a code job whose plan says `none (<why>)` for its Check
    command: nothing runs, exit 0; `job deliver` then takes the review's grade of every acceptance
    criterion as the check. None for any other job."""
    info = load_state()["jobs"].get(job)
    kind, why = check_spec(job)
    if not info or not_code(info) or kind != "none":
        return None
    print(json.dumps({"result": "none", "why": why, "criteria_rev": criteria_rev(job),
                      "note": "no Check command: nothing to run; the review must grade every acceptance "
                              "criterion VERIFIED"}, ensure_ascii=False))
    return 0


def quality_target(job):
    """(info, command, cwd, target) or an exit code."""
    info = load_state()["jobs"].get(job)
    if not info:
        return fail("unknown job: %s" % job)
    if not_code(info) and not experiment(info):
        return fail("job %s has no code to deliver (its output is %s): no check"
                    % (job, job_output(info)))
    if no_path(job, info):
        return fail(no_path(job, info))
    command = plan_command(job)
    if not command:
        fail(u"plan.md of %s names no Check command: its section's first line is the command in backticks, "
             u"or `none (<why>)`" % job)
        return 2
    cwd, target, files = check_dir(job, info)
    if not target:
        fail("cannot read the commit or tree of %s in %s" % (job, cwd))
        return 2
    return info, command, cwd, target, files


def untracked_work(cwd):
    code, out = git_out(cwd, "status", "--porcelain")
    return [l[3:] for l in out.splitlines() if l.startswith("?? ") and not byproduct(l)] if code == 0 else []


def base_point(info, cwd, job=None):
    """The commit the job's work starts from: in a worktree, where its branch leaves the
    base branch (a job that continues another: its start_commit, where that job ended); in place,
    the checkout's HEAD when the job began (its snapshot), else its HEAD now."""
    main = checkout_dir(info)
    code, head = git_out(main, "rev-parse", "HEAD")
    if not (info.get("worktree") or {}).get("path"):
        began = (read_json(snapshot_path(job), None) or {}).get("head") if job else None
        if began and git_out(main, "cat-file", "-e", began + "^{commit}")[0] == 0:
            return began
        return head if code == 0 else None
    start = info.get("start_commit")
    if start and git_out(cwd, "merge-base", "--is-ancestor", start, "HEAD")[0] == 0:
        return start
    code, point = git_out(cwd, "merge-base", "HEAD", info.get("base") or head)
    return point if code == 0 else None


def is_link(path):
    """A symbolic link, or on Windows a junction: never followed when OKe removes a folder."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def drop_link(path):
    """Remove a link itself, never what it points to."""
    try:
        os.rmdir(path)  # a junction or a folder link on Windows: rmdir removes only the link
    except OSError:
        os.unlink(path)


def safe_rmtree(top):
    """shutil.rmtree that never goes through a link or junction: a link inside is removed as a link."""
    if is_link(top):
        try:
            drop_link(top)
        except OSError:
            pass
        return
    if not os.path.isdir(top):
        return
    for name in os.listdir(top):
        path = os.path.join(top, name)
        try:
            if is_link(path):
                drop_link(path)
            elif os.path.isdir(path):
                safe_rmtree(path)
            else:
                try:
                    os.remove(path)
                except OSError:
                    os.chmod(path, 0o666)  # git's objects are read-only on Windows
                    os.remove(path)
        except OSError:
            pass
    try:
        os.rmdir(top)
    except OSError:
        pass


def base_dir(job):
    return os.path.join(oke_home(), "tmp", "base-" + job)


def worktree_admins(repo):
    """git's record folders of a repository's linked worktrees (<common dir>/worktrees/*)."""
    code, common = git_out(repo, "rev-parse", "--git-common-dir")
    if code != 0:
        return []
    common = native_path(common)
    if not os.path.isabs(common):
        common = os.path.join(repo, common)
    return glob.glob(os.path.join(common, "worktrees", "*"))


def forget_worktree(where, admins):
    """Remove git's record of the worktree at `where` once its folder is gone: only that one (never
    `git worktree prune`, which would forget the user's other worktrees on a drive not mounted now)."""
    if os.path.isdir(where):
        return
    mine = normalize_path(os.path.join(where, ".git"))
    for admin in admins:
        admin = native_path(admin)
        if os.path.basename(os.path.dirname(os.path.normpath(admin))) != "worktrees":
            continue
        try:
            with io.open(os.path.join(admin, "gitdir"), encoding="utf-8") as f:
                back = f.read().strip()
        except (IOError, OSError):
            continue
        if normalize_path(native_path(back)) == mine:
            safe_rmtree(admin)


def drop_base(where, repo=None):
    """Remove a temporary base worktree OKe made: its links first (never their targets), the folder,
    and git's record of it (only this one)."""
    admins = []
    try:
        with io.open(os.path.join(where, ".git"), encoding="utf-8") as f:
            line = f.read().strip()
        if line.startswith("gitdir:"):
            admins.append(line[len("gitdir:"):].strip())
    except (IOError, OSError):
        pass
    if repo:
        admins += worktree_admins(repo)
    safe_rmtree(where)
    forget_worktree(where, admins)


def clean_base_leftovers(holding=None, only=None):
    """Remove temporary base worktrees a killed run left behind (`holding`: the job whose check lock
    this process holds). A check running now keeps its own."""
    for where in glob.glob(os.path.join(oke_home(), "tmp", "base-*")):
        if not os.path.isdir(where):
            continue
        job = os.path.basename(where)[len("base-"):]
        if only and job != only:
            continue
        if job == holding:
            drop_base(where)
        elif try_lock("check-" + job):
            try:
                drop_base(where)
            finally:
                unlock("check-" + job)


BASE_COPY_FILE = 1000 * 1000  # a top-level ignored file copied into the base run's worktree: at most 1 MB
BASE_COPY_TOTAL = 20 * 1000 * 1000  # and at most 20 MB in all


def cmd_quoted(path):
    """`path` as one cmd.exe argument: quoted, so `&`, `|`, `<`, `>`, `(`, `)`, `^` and spaces are text,
    and each `%` outside the quotes as `^%`, so no variable expands."""
    return '"' + path.replace('%', '"^%"') + '"'


def make_junction(target, link):
    """A Windows junction `link` to the folder `target`, without a shell where Python can (no cmd.exe
    parses the paths). True when made."""
    try:
        import _winapi
        create = getattr(_winapi, "CreateJunction", None)
    except ImportError:
        create = None
    if create:
        try:
            create(target, link)
            return True
        except OSError:
            return False
    return call_timed("cmd /d /c mklink /J %s %s" % (cmd_quoted(link), cmd_quoted(target)), 60,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def link_ignored(cwd, where):
    """Prepare the temporary base worktree like the checkout: link the top-level ignored dependency
    folders config `base_links` names (node_modules, .venv, venv), so the Check command finds what the
    user installed, and copy small top-level ignored files (.env, local.settings.json). Never an
    output or cache folder: a run there would write into the user's. The links made."""
    try:
        p = run_timed(["git", "-C", cwd, "-c", "core.quotepath=off", "ls-files", "--others", "--ignored",
                       "--exclude-standard", "--directory", "-z"],
                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return []
    wanted = set(n.strip("/") for n in load_config().get("base_links") or [] if isinstance(n, str))
    links, total = [], 0
    for entry in p.stdout.decode("utf-8", "replace").split("\0"):
        name = entry.rstrip("/")
        if not name or "/" in name or name == ".git":
            continue
        target, link = os.path.normpath(os.path.join(cwd, name)), os.path.normpath(os.path.join(where, name))
        if os.path.lexists(link):
            continue
        if not entry.endswith("/"):
            try:
                size = os.path.getsize(target)
                if os.path.isfile(target) and not is_link(target) and size <= BASE_COPY_FILE and total + size <= BASE_COPY_TOTAL:
                    shutil.copyfile(target, link)
                    total += size
            except (IOError, OSError):
                pass
            continue
        if name not in wanted or not os.path.isdir(target):
            continue
        try:
            if os.name == "nt":
                if not make_junction(target, link):
                    continue
            else:
                os.symlink(target, link, target_is_directory=True)
            links.append(name)
        except OSError:
            pass
    return links


def overlay_user_files(job, info, where):
    """Put the user's uncommitted files from the job's start into the temporary base worktree: the
    base is the checkout as the job found it. Files OKe kept no copy of are taken as they are now
    when they did not change."""
    snap = read_json(snapshot_path(job), None)
    if not in_place(info) or not isinstance(snap, dict):
        return
    cwd, copied = checkout_dir(info), set(snap.get("copied") or [])
    for rel, mark in (snap.get("files") or {}).items():
        dst = os.path.join(where, *rel.split("/"))
        parts = rel.split("/")[:-1]
        if ".." in parts or any(is_link(os.path.join(where, *parts[:n])) for n in range(1, len(parts) + 1)):
            continue  # a committed link on the way: writing through it would reach outside the worktree
        try:
            if mark == "-":
                if os.path.isfile(dst) and not is_link(dst):
                    os.remove(dst)
                continue
            if rel in copied:
                src = os.path.join(oke_home(), "jobs", job, DIRTY_COPIES, *rel.split("/"))
            elif not mark.startswith(("dir:", "link:")) and file_mark(cwd, rel) == mark:
                src = os.path.join(cwd, *rel.split("/"))
            else:
                continue
            if is_link(dst) or os.path.isdir(dst):
                continue
            if not os.path.isdir(os.path.dirname(dst)):
                os.makedirs(os.path.dirname(dst))
            shutil.copyfile(src, dst)
        except (IOError, OSError):
            pass


def run_at(cwd, commit, where, run, prepare=None):
    """run(where) in a temporary detached checkout of `commit`, removed afterwards (links first, never
    through them); None when git cannot make it. `prepare(where)` runs before."""
    drop_base(where, cwd)  # a run that died left it behind
    if call_timed(["git", "-C", cwd, "worktree", "add", "-q", "-f", "--detach", where, commit], COMMIT_TIMEOUT,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        drop_base(where, cwd)
        return None
    try:
        if prepare:
            prepare(where)
        return run(where)
    finally:
        drop_base(where, cwd)


def base_run(job, info, cwd, command):
    """The Check command's run on the commit the job started from, in a temporary detached worktree
    under OKe's home (removed afterwards), with the user's uncommitted files from the job's start and
    links to the checkout's ignored folders (installed packages): {"exit", "unrunnable", "signatures",
    ...}, or a string saying why it could not run. Run once per commit and command (baseline.json)."""
    point = base_point(info, cwd, job)
    if not point:
        return "cannot find the commit job %s started from" % job
    job_dir = os.path.join(oke_home(), "jobs", job)
    path = os.path.join(job_dir, "baseline.json")
    kept = read_json(path, None)
    if isinstance(kept, dict) and kept.get("version") == BASELINE_VERSION and kept.get("commit") == point \
            and kept.get("command") == command:
        return kept
    log = os.path.join(job_dir, "baseline.log")
    where = base_dir(job)
    if not os.path.isdir(os.path.dirname(where)):
        os.makedirs(os.path.dirname(where))

    def prepare(w):
        overlay_user_files(job, info, w)
        link_ignored(cwd, w)
    now = run_at(cwd, point, where, lambda w: run_signatures(command, w, log), prepare)
    if now is None:
        return "git could not check out %s in a temporary worktree" % point[:12]
    if now["unrunnable"]:
        return "the Check command could not run there (exit %d, see %s)" % (now["exit"], log.replace("\\", "/"))
    if now.get("timed_out"):
        return "the Check command ran over its time limit there (%g min, `check_timeout`)" % (check_timeout() / 60)
    base = dict(now, version=BASELINE_VERSION, commit=point, command=command,
                recorded_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    write_json_atomic(path, base)
    return base


def names_file(line, paths):
    """Does a failure line name one of the files (relative path or file name)?"""
    for p in paths:
        for form in set([p, p.replace("/", "\\"), p.rsplit("/", 1)[-1]]):
            if form and re.search(r"(?<![\w.-])%s(?![\w-])" % re.escape(form), line):
                return True
    return False


def changed_files(info, cwd, files, job):
    """The files a job changed so far: its uncommitted ones, and in its own worktree its commits'."""
    paths = list(files["paths"]) if files else []
    if not in_place(info):
        point = base_point(info, cwd, job)
        if point:
            paths += [p for p in git_out(cwd, "-c", "core.quotepath=off", "diff", "--name-only", point,
                                         "HEAD")[1].splitlines() if p and p not in paths]
    return paths


PLAN_MAX_BYTES = 20000
PLAN_MAX_LINE = 2000


def plan_too_big(job):
    """Why plan.md is too big to show or check (over 20 KB, or a line over 2,000 characters), or None."""
    path = os.path.join(oke_home(), "jobs", job, "plan.md")
    try:
        size = os.path.getsize(path)
        with io.open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except (IOError, OSError):
        return None
    hint = "Shorten it: name folders or patterns instead of listing every file"
    if size > PLAN_MAX_BYTES:
        return "plan.md of %s is %d KB; a plan is at most %d KB. %s" % (job, size // 1000, PLAN_MAX_BYTES // 1000, hint)
    for n, line in enumerate(lines, 1):
        if len(line) > PLAN_MAX_LINE:
            return ("line %d of plan.md of %s has %d characters; a line is at most %d. %s"
                    % (n, job, len(line), PLAN_MAX_LINE, hint))
    return None


def cmd_baseline(args):
    """Kept for older skills: there is nothing to record before the work."""
    if no_check(args.job) is not None:  # a plan with no command: `"result": "none"`, as `oke check` says
        return 0
    print(json.dumps({"result": "not_needed", "note": "no baseline step: when `oke check` fails, OKe runs the "
                      "Check command on the job's base itself"}))
    return 0


def dirty_marks(cwd, marks, untracked=None):
    """{path: mark} of every changed or untracked file in a checkout (caches and nested
    repositories left out), or None when git fails. `untracked`: a set that gets the untracked ones."""
    entries = dirty_paths(cwd)
    if entries is None:
        return None
    out = {}
    for xy, rel in entries:
        full = os.path.join(cwd, *rel.split("/"))
        if xy == "??" and (byproduct("?? " + rel) or (os.path.isdir(full) and not os.path.islink(full))):
            continue
        out[rel] = marks.mark(cwd, rel)
        if xy == "??" and untracked is not None:
            untracked.add(rel)
    return out


def record_byproducts(job, left):
    """Untracked files the Check command's run made: never the job's change (they stay uncommitted)."""
    with locked():
        state = load_state()
        info = state["jobs"].get(job)
        if info is None:
            return None
        info.setdefault("byproducts", {}).update(left)
        save_state(state)
        return info


BASE_UNRUNNABLE = "base=unrunnable"  # a check row's detail word: the base run could not run
BASE_HAS_IT = "base_has_it"  # a check row's detail count: new failures the base has too (naming changed files)


def cmd_check(args):
    """Run the Check command where the job's work is. A pass is a pass. A failure is compared with
    a run on the job's base commit (OKe makes it itself): failure lines the base has too are
    pre-existing, unless they name a file the job changed. Untracked files the run makes are its
    byproducts: never the job's change (a tracked file it changes is). What is checked (a commit or tree:<sha>) is read after the run.
    A result (output result) is only recorded: the reviewer grades it. 0 no new failure, 1 new
    failures, 2 cannot judge. Written to the ledger by OKe."""
    if no_check(args.job) is not None:
        return 0
    found = quality_target(args.job)
    if isinstance(found, int):
        return found
    info, command, cwd, target, files = found
    too = plan_too_big(args.job)
    if too:
        fail(too)
        return 2
    if nested_block(files, cwd):
        fail(nested_block(files, cwd))
        return 2
    moved = head_moved(args.job, info)
    if moved:
        print(json.dumps(moved, ensure_ascii=False))
        return 2
    rev = criteria_rev(args.job)
    why = None
    lock = "check-" + args.job
    while not try_lock(lock):
        time.sleep(5)
    try:
        clean_base_leftovers(holding=args.job)
        n = len([r for r in ledger_rows(args.job) if r["check"] == "check"]) + 1
        log = os.path.join(oke_home(), "jobs", args.job, "check-%d.log" % n)
        marks = Marks(args.job)
        before = dirty_marks(cwd, marks) or {}
        mine = set(files["paths"]) if files else set()
        now = run_signatures(command, cwd, log)
        untracked, kept = set(), info.get("byproducts") or {}
        after = dirty_marks(cwd, marks, untracked) or {}
        # only untracked files the run made (or a byproduct an earlier run made): a tracked file the run
        # changed is the job's change like any other
        left = dict((r, m) for r, m in after.items() if r in untracked and r not in mine and before.get(r) != m
                    and (r not in before or r in kept))
        marks.save()
        if left:
            info = record_byproducts(args.job, left) or info
        cwd, target, files = check_dir(args.job, info)
        log = log.replace("\\", "/")
        if not target:
            fail("cannot read the commit or tree of %s in %s" % (args.job, cwd))
            return 2
        if now["unrunnable"]:
            fail(u"the Check command could not run (exit %d, see %s)" % (now["exit"], log))
            return 2
        new, old, base_failed, has = [], [], False, []
        if experiment(info):
            # a result: the run is the experiment. Recorded once, never compared with the base (that would
            # run the experiment again); the reviewer grades its outputs against the criteria
            result = "ran"
            ledger_add(args.job, "oke", target, "check", result,
                       "%s exit=%d log=%s" % (rev, now["exit"], log))
        else:
            if now.get("timed_out"):
                new = list(now["signatures"])  # never also on the base: that would wait as long again
            elif now["exit"] != 0:
                base = base_run(args.job, info, cwd, command)
                if isinstance(base, str):
                    why = "could not check the base: " + base
                    new = ["<%s>" % why]
                    base_failed = True
                elif base["exit"] == 0:
                    new = ["<exited with code %s>" % now["exit"]] + now["signatures"]
                else:
                    known, changed = set(base["signatures"]), changed_files(info, cwd, files, args.job)
                    users = [r for r in ((read_json(snapshot_path(args.job), None) or {}).get("files") or {})
                             if r not in changed] if in_place(info) else []
                    for s in now["signatures"]:
                        pre = (s in known or names_file(s, users)) and not names_file(s, changed)
                        (old if pre else new).append(s)
                    # the base fails the same way, but the line names a file the job changed
                    has = [s for s in new if s in known]
            result = "fail" if new else "pass"
            ledger_add(args.job, "oke", target, "check", result,
                       "%s new=%d pre_existing=%d%s%s log=%s" % (
                           rev, len(new), len(old), " " + BASE_UNRUNNABLE if base_failed else "",
                           " %s=%d" % (BASE_HAS_IT, len(has)) if has else "", log))
    finally:
        unlock(lock)
    out = {"result": result, "criteria_rev": rev, "commit": target, "new": new, "pre_existing": old, "log": log}
    if result == "ran":
        out.update(exit=now["exit"], note="the run is recorded; the reviewer grades its outputs")
    if now.get("timed_out"):
        out["timed_out"] = True  # stopped at check_timeout: a hang the job may cause, or a check that needs longer
    if files and files["shared"]:
        out["user_files"] = files["shared"]  # the user's uncommitted files the job changed too: ask before delivery
    if files and files["check_left"]:
        out["check_left"] = files["check_left"]  # untracked, made by the run: left uncommitted
    ask = ("report a question with `asks: check_accept` (only the user answers it): accept this failing "
           "check for this job? On yes the orchestrator runs `$OKE job accept-check %s %s --by-user`"
           % (args.job, target))
    if base_failed:
        out["base_unrunnable"] = True
        out["next"] = ("the Check command fails and OKe could not run it on the job's base to compare. Fix it "
                       "if the job caused it; else " + ask)
    elif has:
        out["base_has_it"] = has
        if len(has) == len(new):
            out["next"] = ("every new failure is on the job's base too, but names a file the job changed. Fix "
                           "it if the job caused it; else " + ask)
    print(json.dumps(out, ensure_ascii=False))
    if why:
        sys.stderr.write(why + "\n")
    return 1 if new else 0


NOT_AUTO = ("job %s is on `job auto`, which never answers this: ask the user (`asks: %s`), and after the "
            "user's yes run it again with --by-user")


def accept_check(job, target, by_user=False):
    """The user accepts a failing check for this job and this commit or tree: OKe records it as the
    check's pass. Only after a failure whose base could not run, or whose new failures the base has
    too (they name files the job changed), under the current criteria."""
    info = load_state()["jobs"].get(job)
    if not info:
        return fail("unknown job: %s" % job)
    if info.get("auto") and not by_user:
        return fail(NOT_AUTO % (job, "check_accept"))
    rev = criteria_rev(job)
    rows = [r for r in ledger_rows(job) if r["check"] == "check" and r["commit"] == target]
    last = rows[-1] if rows else None
    words, log = check_detail(last["detail"]) if last else ([], None)
    count = dict(w.split("=", 1) for w in words if "=" in w)
    on_base = count.get(BASE_HAS_IT) is not None and count.get(BASE_HAS_IT) == count.get("new")
    if not last or last["result"] != "fail" or not (BASE_UNRUNNABLE in words or on_base) or words[:1] != [rev]:
        return fail("job %s has no failing `oke check` on %s whose base could not run, or whose new failures "
                    "the base has too (under the current criteria): only such a check is accepted" % (job, target))
    ledger_add(job, "user", target, "check", "pass", "%s accepted_by_user new=0 pre_existing=0 log=%s" % (rev, log or "-"))
    print(json.dumps({"job": job, "commit": target, "result": "pass", "accepted_by_user": True}))
    return 0


def accept_head(job, by_user=False):
    """The user made the commits in the job's checkout since the job began: the job's changes count
    from the checkout's HEAD now."""
    with locked():
        state = load_state()
        info = state["jobs"].get(job)
        if not info:
            return fail("unknown job: %s" % job)
        if info.get("auto") and not by_user:
            return fail(NOT_AUTO % (job, "head_moved"))
        snap = read_json(snapshot_path(job), None)
        if not in_place(info) or not isinstance(snap, dict):
            return fail("job %s does not work in place with a record of its start: nothing to accept" % job)
        head = git_out(checkout_dir(info), "rev-parse", "HEAD")[1]
        snap.update(head=head, head_accepted=time.time())
        write_json_atomic(snapshot_path(job), snap)
    print(json.dumps({"job": job, "head": head}))
    return 0


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
    status, _ = deliver_state(args.job)
    rows, bad, _ = ledger_chain(args.job)
    print(json.dumps({"job": args.job, "deliver": status, "rows": rows, "ignored_lines": bad}, indent=2))
    return 0


# Phases that pass a user gate may only be entered from the phase before that gate
# (or stay). Not proof of a user's approval, but a PM or an orchestrator slip cannot
# skip the approval step. Phases not listed here are free.
# Job sizes, which the PM decides while planning. small: a subagent does the work;
# normal: a worker pane, a review subagent; large: worker and reviewer panes
SIZES = ("small", "normal", "large")
# state.json from before the rename kept a "lane" with the old names
OLD_LANES = {"trivial": "small", "small": "normal", "normal": "large", "research": "research"}
# What a job hands over (its output), apart from where it works:
#   code    a git job's change, delivered as its branch (or a commit in the main checkout)
#   files   a direct job's folder (outside git), changed in place: no branch
#   result  an answer or a run's outputs: an answer (the PM investigated and nothing needs to
#           change: `job answer`, no plan, approval or worker), or an experiment in a worktree
#           whose edits never reach the main branch
OUTPUTS = ("code", "result")  # the ones `job output` sets: files comes from the path


def is_answer(info):
    """A job that ends with the PM's answer and changes nothing: `job answer`, or a job an older OKe
    saved with size research. Such a job stops being one when its PM plans a change for it: `job size`
    then replaces `research` with the planned size (and `job answer --undo` drops it)."""
    return bool(info.get("answer")) or info.get("size") == "research"


def job_output(info):
    """code, files or result. An answer -> result; jobs saved before outputs: direct -> files."""
    if is_answer(info):
        return "result"
    if info.get("direct"):
        return "files"
    return info.get("output") or "code"


def not_code(info):
    return job_output(info) != "code"


def experiment(info):
    """A result job with a worker: its result stays uncommitted where its plan's `## Workspace`
    puts it (not a job the user ended with no change: it made nothing to keep)."""
    return not is_answer(info) and job_output(info) == "result" and not info.get("no_change")


def experiment_none(job, info):
    """Why an experiment's plan cannot go on: its Check command is `none` (the command is the
    experiment's run), or None. Checked at every phase its plan is approved, worked or ended on."""
    if experiment(info) and check_spec(job)[0] == "none":
        return ("job %s is an experiment: its Check command is the run, so it cannot be `none`: give the PM "
                "`kind: plan` to name the command that runs it" % job)
    return None


def place_lines(job):
    """The `## Workspace` section without its Continues and Results lines (they name files, which
    may contain oke/<job> too), or None."""
    section = plan_place(job)
    if section is None:
        return None
    return "\n".join(l for l in section.splitlines() if not FILE_LINE.match(l))


def plan_own_worktree(job):
    """Does the job's plan give it its own worktree (its `## Workspace` section names its branch
    by its whole name: oke/fix is not in oke/fix-a)?"""
    return bool(re.search(r"oke/%s(?![a-z0-9-])" % re.escape(job), place_lines(job) or ""))


def plan_continues(job):
    """The finished job whose branch the plan's worktree starts from (`from oke/<name>` in its
    `## Workspace` section), or None."""
    m = CONTINUES.search(place_lines(job) or "")
    return m.group(1).lower() if m else None


def plan_worktree(job):
    """Does the job's plan give it a worktree?"""
    return plan_own_worktree(job)


# a plan's worktree that starts from another job's branch: `new worktree (branch oke/b, from oke/a)`
CONTINUES = re.compile(r"\bfrom\s+`?oke/([a-z][a-z0-9-]{0,23})(?![a-z0-9-])`?", re.I)


# a plan's work-place section
PLACE_SECTION = re.compile(r"^## Workspace[^\n]*\n(.*?)(?=^## |\Z)", re.M | re.S)
# its lines naming files: the user's uncommitted work the job goes on with, the result files
FILE_LINE = re.compile(r"^\s*(Continues|Results)\s*:\s*(.*)$", re.M | re.I)


def plan_place(job):
    """The text of the job's `## Workspace` section, or None."""
    try:
        with io.open(os.path.join(oke_home(), "jobs", job, "plan.md"), encoding="utf-8") as f:
            section = PLACE_SECTION.search(f.read())
    except (OSError, ValueError):
        return None
    return section.group(1) if section else None


def plan_files(job, name):
    """The files or folders the plan's `## Workspace` line `<name>: <path>, <path>` names
    (`Continues` or `Results`), relative with forward slashes; [] for none or no such line."""
    for m in FILE_LINE.finditer(plan_place(job) or ""):
        if m.group(1).lower() == name.lower():
            value = m.group(2).strip()
            if value.lower().startswith("none") or not value:
                return []
            names = [rel_path(p.strip().strip("`\"'")) for p in value.split(",")]
            return [n for n in names if n]
    return []


def rel_path(p):
    """A path as OKe compares a job's files: forward slashes, no `./`, no slash at either end."""
    p = (p or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:].lstrip("/")
    return p.strip("/")


def path_key(p):
    """What two paths compare by: Windows file names do not differ by case."""
    return p.lower() if os.name == "nt" else p


def names_path(rel, names):
    """Is `rel` one of `names`, or inside one of them (a folder)?"""
    rel = path_key(rel)
    return any(rel == n or rel.startswith(n + "/") for n in (path_key(rel_path(n)) for n in names) if n)


def dirty_paths(cwd):
    """Every path `git status` shows changed in a checkout: tracked changes (both paths of a rename),
    each untracked file (ignored ones left out), as [(status, path)] relative with forward
    slashes; None when git fails."""
    try:
        p = run_timed(["git", "-C", cwd, "-c", "core.quotepath=off", "status", "--porcelain", "-z", "-uall"],
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
        out.append((entry[:2], entry[3:].rstrip("/")))
        if "R" in entry[:2] or "C" in entry[:2]:
            if i < len(fields) and fields[i]:
                out.append((entry[:2], fields[i].rstrip("/")))
            i += 1
    return out


DIRTY_FILE = "dirty-before.json"  # the user's uncommitted files when a job began work in place
DIRTY_COPIES = "dirty-before"  # copies of them, to restore one (`job restore-user`)
COPY_MAX_FILE = 2 * 1000 * 1000  # a bigger file keeps only its hash
COPY_MAX_TOTAL = 100 * 1000 * 1000  # copying stops there; hashes are always kept


BIG_FILE = 50 * 1000 * 1000  # a bigger file is told by its size and time, not hashed


def file_mark(cwd, rel):
    """What a file holds now, to compare: a hash of its bytes (a file over 50 MB: its size and
    modification time), `-` when it is gone."""
    p = os.path.join(cwd, *rel.split("/"))
    if os.path.islink(p):
        return "link:" + os.readlink(p)
    if os.path.isdir(p):  # a submodule: its commit
        return "dir:" + git_out(p, "rev-parse", "HEAD")[1]
    try:
        st = os.stat(p)
    except OSError:
        return "-"
    if st.st_size > BIG_FILE:
        return "stat:%d:%d" % (st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    h = hashlib.sha1()
    try:
        with io.open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except (IOError, OSError):
        return "-"
    return h.hexdigest()


def in_place(info):
    """A git job that works in its checkout (the main one, or a linked worktree the user named),
    not in a worktree of its own."""
    return bool(info.get("git_key")) and not info.get("direct") and not (info.get("worktree") or {}).get("path")


def snapshot_path(job):
    return os.path.join(oke_home(), "jobs", job, DIRTY_FILE)


class Marks(object):
    """file_mark with a cache by (size, mtime, inode, POSIX ctime) for one job, kept in its folder: a big
    checkout's files are hashed again only when they change."""
    def __init__(self, job):
        self.path = os.path.join(oke_home(), "jobs", job, "marks.json") if job else None
        data = read_json(self.path, None) if self.path else None
        self.data = data if isinstance(data, dict) and len(data) < 50000 else {}
        self.changed = False

    def mark(self, cwd, rel):
        p = os.path.join(cwd, *rel.split("/"))
        try:
            st = os.lstat(p)
        except OSError:
            return "-"
        if not stat.S_ISREG(st.st_mode):
            return file_mark(cwd, rel)
        key = normalize_path(p)
        stamp = [st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)), st.st_ino or 0,
                 # Windows' st_ctime is the creation time: only POSIX's (the inode's change) counts
                 getattr(st, "st_ctime_ns", int(st.st_ctime * 1e9)) if os.name != "nt" else 0]
        hit = self.data.get(key)
        # a file written within 2 s of its hashing may change again with the same mtime: hashed again
        if isinstance(hit, list) and len(hit) == 6 and hit[:4] == stamp and abs(hit[5] - st.st_mtime) >= 2:
            return hit[4]
        at = time.time()
        m = file_mark(cwd, rel)
        self.data[key] = stamp + [m, at]
        self.changed = True
        return m

    def save(self):
        if self.changed and self.path and os.path.isdir(os.path.dirname(self.path)):
            try:
                write_json_atomic(self.path, self.data)
            except (IOError, OSError):
                pass  # only a cache
            self.changed = False


def take_snapshot(job, info):
    """The user's uncommitted files in a job's checkout and what they hold, with a copy of each (up
    to 2 MB a file, 100 MB in all) in a folder of its own: (record, that folder), or None when git
    fails. Slow on a big checkout, so it runs before the state lock (`snapshot_dirty` keeps it)."""
    cwd = checkout_dir(info)
    entries = dirty_paths(cwd)
    if entries is None:
        return None
    copies = os.path.join(oke_home(), "jobs", job, "%s.new-%d" % (DIRTY_COPIES, os.getpid()))
    safe_rmtree(copies)
    files, copied, total = {}, [], 0
    for _, rel in entries:
        src = os.path.join(cwd, *rel.split("/"))
        size = os.path.getsize(src) if os.path.isfile(src) and not os.path.islink(src) else None
        if size is not None and size <= COPY_MAX_FILE and total + size <= COPY_MAX_TOTAL:
            dst = os.path.join(copies, *rel.split("/"))
            try:
                if not os.path.isdir(os.path.dirname(dst)):
                    os.makedirs(os.path.dirname(dst))
                shutil.copyfile(src, dst)
                files[rel] = file_mark(copies, rel)  # the copy's hash: what restore-user brings back
                copied.append(rel)
                total += size
                continue
            except (IOError, OSError):
                pass
        files[rel] = file_mark(cwd, rel)
    return {"head": git_out(cwd, "rev-parse", "HEAD")[1], "files": files, "copied": copied,
            "at": time.time()}, copies


def snapshot_dirty(job, info, taken=None):
    """When a job starts work in place (under the state lock): keep the record of the user's
    uncommitted files, so that only what changes after this counts as the job's. `taken`: what
    take_snapshot gave before the lock; else it is taken now. Taken once. False when git fails."""
    if os.path.exists(snapshot_path(job)):
        if taken:
            safe_rmtree(taken[1])
        return True
    taken = taken or take_snapshot(job, info)
    if not taken:
        return False
    record, copies = taken
    final = os.path.join(oke_home(), "jobs", job, DIRTY_COPIES)
    safe_rmtree(final)
    if os.path.isdir(copies):
        try:
            os.rename(copies, final)
        except OSError as e:  # a virus scanner or an open file holds the folder
            safe_rmtree(copies)
            raise StateError("could not keep the copies of the user's files for job %s (%s): run it again"
                             % (job, e))
    write_json_atomic(snapshot_path(job), record)
    return True


def restore_user(job, name):
    """`job restore-user`: bring one of the user's files back to what it held when the job began (a
    worker changed it by mistake). Never `git checkout` such a file: that loses the user's edits."""
    info = load_state()["jobs"].get(job)
    if not info:
        return fail("unknown job: %s" % job)
    snap = read_json(snapshot_path(job), None)
    if not isinstance(snap, dict) or not in_place(info):
        return fail("job %s has no record of the user's files (it does not work in place, or began before "
                    "OKe kept one): ask the user" % job)
    by_key = dict((path_key(r), r) for r in snap.get("files") or {})
    rel = by_key.get(path_key(rel_path(name)))
    if rel is None:
        return fail("%s was not one of the user's uncommitted files when job %s began: it is not restored this "
                    "way" % (name, job))
    cwd = checkout_dir(info)
    parts = rel.split("/")
    target = os.path.join(cwd, *parts)
    # never outside the checkout: no `..`, no absolute path, no folder link that leads out of it
    if os.path.isabs(rel) or ".." in parts or re.match(r"^[A-Za-z]:", rel) or \
            not under(real_dir(os.path.dirname(target)), real_dir(cwd)):
        return fail("%s is outside job %s's checkout %s: not restored" % (name, job, cwd))
    copy = os.path.join(oke_home(), "jobs", job, DIRTY_COPIES, *parts)
    removing, kept = snap["files"][rel] == "-", None  # "-": the user had deleted it
    if not removing and rel not in (snap.get("copied") or []):
        if file_mark(cwd, rel) != snap["files"][rel]:
            return fail("OKe kept no copy of %s (over 2 MB, or past 100 MB of copies), only its hash: ask the user"
                        % rel)
    else:
        if os.path.lexists(target):
            if os.path.isdir(target) and not os.path.islink(target):
                return fail("%s is a folder now: not restored, ask the user" % rel)
            # what the file holds now is kept first: a restore never loses anything
            kept = os.path.join(oke_home(), "jobs", job, "restored",
                                *(parts[:-1] + ["%s.%s" % (parts[-1], time.strftime("%Y%m%d-%H%M%S"))]))
            if os.path.lexists(kept):
                kept += "-%d" % os.getpid()
            try:
                if not os.path.isdir(os.path.dirname(kept)):
                    os.makedirs(os.path.dirname(kept))
                if os.path.islink(target):
                    with io.open(kept, "w", encoding="utf-8") as f:
                        f.write(u"link to %s\n" % os.readlink(target))
                else:
                    shutil.copyfile(target, kept)
                os.remove(target)  # a link is unlinked, never written through
            except (IOError, OSError) as e:
                return fail("could not keep %s before restoring it (%s): nothing restored" % (rel, e))
        if not removing:
            if not os.path.isdir(os.path.dirname(target)):
                os.makedirs(os.path.dirname(target))
            shutil.copyfile(copy, target)
    out = {"job": job, "restored": rel, "same": file_mark(cwd, rel) == snap["files"][rel]}
    if kept:
        out["kept"] = native_path(kept)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def job_files(job, info, cwd, marks=None):
    """The job's own uncommitted changes in `cwd`: {"paths": what the job's commit takes, "shared": the
    user's uncommitted files the job changed too that nobody settled yet (in "paths" as well), "left":
    such files the user said to leave out, "user": how many of the user's files stay as they were,
    "new": the job's new (untracked) files, "check_left": files the Check command's runs made or
    changed (never committed), "nested": git repositories inside the checkout that are not the
    user's (never the job's)}. In place, a file counts as the job's when it differs from the
    snapshot taken at the job's start, or is new. None when git fails."""
    entries = dirty_paths(cwd)
    if entries is None:
        return None
    own = marks is None
    marks = marks or Marks(job)
    whole = read_json(snapshot_path(job), None) if in_place(info) else None
    snap = (whole or {}).get("files") or {}
    goes_on = plan_files(job, "Continues")
    settled = dict((path_key(rel_path(k)), v) for k, v in (info.get("user_files") or {}).items())
    # in place without a record of the start (an older job): once the user named the job's files, only those
    named = [k for k, v in settled.items() if v == "include"] if in_place(info) and whole is None else []
    by_check = info.get("byproducts") or {}
    out = {"paths": [], "shared": [], "left": [], "user": 0, "new": [], "check_left": [], "nested": []}
    seen = set()
    for xy, rel in entries:
        if rel in seen or (xy == "??" and byproduct("?? " + rel)):
            continue
        seen.add(rel)
        full = os.path.join(cwd, *rel.split("/"))
        if xy == "??" and os.path.isdir(full) and not os.path.islink(full):
            # git lists an untracked folder (with -uall) only when it is a repository of its own
            if rel in snap:
                out["user"] += 1
            else:
                out["nested"].append(rel)
            continue
        if xy == "??" and rel in by_check and settled.get(path_key(rel)) != "include" \
                and marks.mark(cwd, rel) == by_check[rel]:
            out["check_left"].append(rel)  # the Check command made or changed it: left as it is, uncommitted
            continue
        if named and not names_path(rel, named):
            out["user"] += 1
            continue
        if rel in snap:
            if marks.mark(cwd, rel) == snap[rel]:
                out["user"] += 1  # the user's, untouched: never committed
                continue
            choice = "include" if names_path(rel, goes_on) else settled.get(path_key(rel))
            if choice == "leave":
                out["left"].append(rel)
                continue
            if choice != "include":
                out["shared"].append(rel)
        elif xy == "??":
            out["new"].append(rel)
        out["paths"].append(rel)
    if own:
        marks.save()
    return out


AGENT_NOTE = re.compile(r"^type:[ \t]*\w+[ \t]*\r?\n(?:[^\n]*\n){0,3}seq:", re.M)


def stray_notes(files, cwd):
    """New files of the job that are an OKe agent's report or board (a `type:` then `seq:` header):
    seen live, a worker wrote its report.md in the repository and delivery committed it."""
    out = []
    for rel in files.get("new") or []:
        if os.path.basename(rel).lower() not in ("report.md", "board.md"):
            continue
        try:
            with io.open(os.path.join(cwd, rel), encoding="utf-8", errors="replace") as f:
                head = f.read(400)
        except (IOError, OSError):
            continue
        if AGENT_NOTE.match(head):
            out.append(rel)
    return out


def nested_block(files, cwd):
    """The refusal for git repositories in the job's checkout that are not the user's, or for an agent's
    report or board written there; None when neither."""
    stray = stray_notes(files or {}, cwd)
    if stray:
        return ("%s in %s %s an OKe agent's report or board, written into the repository by mistake: an agent "
                "writes these in its own folder. Move %s there, then run this again"
                % (", ".join(stray[:5]), cwd, "is" if len(stray) == 1 else "are", "it" if len(stray) == 1 else "them"))
    if not files or not files.get("nested"):
        return None
    return ("%s in %s %s a git repository of its own, made during the job: OKe never commits one into the "
            "job's change. Move it out of the checkout (or ask the user), then run this again"
            % (", ".join(files["nested"][:5]), cwd, "is" if len(files["nested"]) == 1 else "are"))


def tree_of(cwd, paths):
    """The tree of HEAD with `paths` as they are in `cwd`, through a temporary index (the real one is
    untouched): what committing the job's change gives. None when git fails."""
    idx = os.path.join(oke_home(), "tree-%d.index" % os.getpid())
    env = dict(os.environ, GIT_INDEX_FILE=idx)
    git = ["git", "--literal-pathspecs", "-C", cwd]
    try:
        head = git_out(cwd, "rev-parse", "-q", "--verify", "HEAD^{tree}")[1]
        steps = [["read-tree", head] if head else ["read-tree", "--empty"]]
        for n in range(0, len(paths), 200):  # a command line has a limit (Windows: 32k)
            steps.append(["add", "-A", "--"] + paths[n:n + 200])
        steps.append(["write-tree"])
        for argv in steps:
            result = run_timed(git + argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if result.returncode != 0:
                return None
        return result.stdout.decode().strip()
    finally:
        try:
            os.remove(idx)
        except OSError:
            pass


def job_tree(job, info, cwd=None):
    """(tree, files) of the job's work: its checkout's HEAD with the job's own changes, or (None, files)."""
    cwd = cwd or work_dir(info)
    files = job_files(job, info, cwd)
    if files is None:
        return None, None
    return tree_of(cwd, files["paths"]), files


def work_dir(info):
    """Where a git job's files are: its own worktree, else its checkout."""
    wt = (info.get("worktree") or {}).get("path")
    return native_path(wt) if wt else checkout_dir(info)


def checkout_dir(info):
    """The checkout a git job's path names: the main checkout, or a linked worktree the user named.
    A job works and delivers there unless it has a worktree of its own."""
    if info.get("path") and not info.get("direct"):
        return native_path(info["path"])
    return native_path(repo_dir(info["git_key"]))


def commit_groups(job, paths):
    """[(message, [paths])]: the plan's `## Commits` groups in order (`- <message>: <path>, <path>`,
    one group may be `rest`), each path of the job in the first group naming it, else in `rest`.
    Without the section: one commit. A string says why the groups do not fit."""
    section = plan_section(job, u"Commits")
    lines = [l for l in section.splitlines() if re.match(r"^\s*[-*+]\s", l)]
    if not lines:
        return [("oke: %s" % job, list(paths))] if paths else []
    groups, rest = [], None
    for line in lines:
        body = re.sub(r"^\s*[-*+]\s+", "", line).strip()
        if ": " not in body:
            return ("a `## Commits` line is `- <message>: <path>, <path>` (or `rest` for the files no "
                    "other line names), not %r: fix plan.md, then deliver again" % body)
        message, names = body.rsplit(": ", 1)
        names = [rel_path(n.strip().strip("`\"'")) for n in names.split(",")]
        groups.append((message.strip().strip("`"), [n for n in names if n], []))
        if [n.lower() for n in groups[-1][1]] == ["rest"]:
            rest = groups[-1]
    unnamed = []
    for p in paths:
        g = next((g for g in groups if g is not rest and names_path(p, g[1])), rest)
        (g[2] if g is not None else unnamed).append(p)
    if unnamed:
        return ("job %s changed files no `## Commits` line names, and there is no `rest` line: %s. Add them to "
                "a line (or add `- <message>: rest`) in plan.md, then deliver again"
                % (job, ", ".join(unnamed[:10]) + (" and %d more" % (len(unnamed) - 10) if len(unnamed) > 10 else "")))
    return [(m, ps) for m, _, ps in groups if ps]


def commit_paths(cwd, message, paths):
    """One commit of `paths` only, as they are now (`git commit --only`: what the user staged
    elsewhere stays staged, hooks run). (ok, git's output)."""
    git = ["git", "--literal-pathspecs", "-C", cwd]
    new = [p for p in paths if git_out(cwd, "--literal-pathspecs", "ls-files", "--", p)[1] == "" and
           os.path.lexists(os.path.join(cwd, *p.split("/")))]
    for n in range(0, len(new), 200):
        r = run_timed(git + ["add", "--"] + new[n:n + 200], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if r.returncode != 0:
            return False, r.stdout.decode("utf-8", "replace")
    argv = git + ["commit", "-q", "-m", message, "--only"]
    spec = None
    if sum(len(p) + 3 for p in paths) > 20000:  # too long for one command line: git 2.25 reads a file
        spec = os.path.join(oke_home(), "tmp", "commit-%d.paths" % os.getpid())
        if not os.path.isdir(os.path.dirname(spec)):
            os.makedirs(os.path.dirname(spec))
        with io.open(spec, "wb") as f:
            f.write(b"\0".join(p.encode("utf-8") for p in paths))
        argv += ["--pathspec-from-file=" + spec, "--pathspec-file-nul"]
    else:
        argv += ["--"] + paths
    try:
        r = run_timed(argv, COMMIT_TIMEOUT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    finally:
        if spec:
            try:
                os.remove(spec)
            except OSError:
                pass
    if r.returncode != 0:
        for n in range(0, len(new), 200):  # unstage what was added for it; the files stay as they are
            call_timed(git + ["reset", "-q", "--"] + new[n:n + 200], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    return r.returncode == 0, r.stdout.decode("utf-8", "replace")


def paths_differ(cwd, paths):
    """Does any of `paths` differ from HEAD in `cwd` (changed, staged, deleted or untracked)? True
    when git cannot tell: the commit then says."""
    for n in range(0, len(paths), 200):
        code, out = git_out(cwd, "--literal-pathspecs", "status", "--porcelain", "-uall", "--",
                            *paths[n:n + 200])
        if code != 0 or out:
            return True
    return False


def staged_entries(cwd, paths):
    """The index entries of `paths` in `cwd` ({path bytes: b"mode sha stage"}), or None when git fails."""
    out = {}
    for n in range(0, len(paths), 200):
        p = run_timed(["git", "--literal-pathspecs", "-C", cwd, "ls-files", "-s", "-z", "--"] + paths[n:n + 200],
                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if p.returncode != 0:
            return None
        for rec in p.stdout.split(b"\0"):
            if b"\t" in rec:
                entry, path = rec.split(b"\t", 1)
                out[path] = entry
    return out


def restore_staged(cwd, paths, staged):
    """Put the index entries `staged_entries` read back: a path that was not in the index leaves it."""
    if staged is None:
        return
    lines = [entry + b"\t" + path for path, entry in staged.items()]
    lines += [b"0 " + b"0" * 40 + b"\t" + p.encode("utf-8") for p in paths if p.encode("utf-8") not in staged]
    if lines:
        run_timed(["git", "-C", cwd, "update-index", "-z", "--index-info"], input=b"\0".join(lines) + b"\0",
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def commit_job(job, cwd, paths, reviewed):
    """Commit the job's `paths` in `cwd`, one commit per `## Commits` group, and make sure the
    result is the reviewed tree. (commits made, None) or (None, why); on a failure nothing stays
    committed."""
    groups = commit_groups(job, paths)
    if isinstance(groups, str):
        return None, groups
    pre = git_out(cwd, "rev-parse", "HEAD")[1]
    made = []
    staged = staged_entries(cwd, paths)  # what the user had staged: an undo gives it back

    def undo(why):
        if made:
            call_timed(["git", "-C", cwd, "reset", "-q", "--soft", pre])
            for n in range(0, len(paths), 200):
                call_timed(["git", "--literal-pathspecs", "-C", cwd, "reset", "-q", "--"] + paths[n:n + 200],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            restore_staged(cwd, paths, staged)
        return None, why
    for message, group in groups:
        if not paths_differ(cwd, group):
            continue  # a group whose files hold what HEAD has: no empty commit
        ok, text = commit_paths(cwd, message, group)
        if not ok:
            return undo("git commit failed: %s" % text.strip())
        made.append(git_out(cwd, "rev-parse", "HEAD")[1])
    if git_out(cwd, "rev-parse", "HEAD^{tree}")[1] != reviewed:  # changed meanwhile, or a hook changed it
        return undo("the changes in %s moved during the commit (or a commit hook changed them): run `$OKE job "
                    "deliver %s` again" % (cwd, job))
    return made, None


PHASE_FROM = {
    "planning": ("intake", "planning", "awaiting_approval", "working", "reviewing"),
    "working": ("awaiting_approval", "working", "reviewing"),
    "done": ("working", "reviewing", "done"),
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
    if not isinstance(data, dict) or data.get("reason") in TRIAGE_SKIPS and not data.get("rounds"):
        data = {"rounds": [], "done": False}  # `job triage` skipped it, but the request is unclear after all
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


# ---------------------------------------------------------------- triage

# A first look at the user's words, so the orchestrator follows facts, not a judgement: how big the
# request looks (the PM may still choose another size) and whether it names something concrete.
SMALL_PREFIX = re.compile(r"^\s*(?:quick|simple|tiny|minor|small|just|only)\s*:", re.I)
LARGE_SIGNAL = re.compile(u"\\b(?:refactor\\w*|migrat\\w*|architect\\w*|redesign\\w*|rewrite)\\b|"
                          u"\\b(?:entire|whole) (?:codebase|code base|repo(?:sitory)?|project)\\b|"
                          u"리팩터|리팩토링|마이그레이션|아키텍처|재설계|전체|구조(?!체)", re.I)
SMALL_SIGNAL = re.compile(u"\\b(?:typos?|renam\\w*|one[- ]liner|one[- ]line)\\b|"
                          u"오타|이름\\s*(?:을|만)?\\s*바꿔|이름\\s*변경|한\\s*줄|간단히|간단한", re.I)
LARGE_WORDS, SMALL_WORDS = 200, 50
# a path: starts at the top, home, here or a drive letter, or has 2+ separators, or ends in a file name
PATH_LIKE = re.compile(r"(?<![\w.~:/\\-])(?:[A-Za-z]:[\\/]|~[\\/]|\.{1,2}[\\/]|[\\/])?[\w.~-]+(?:[\\/][\w.~-]*)+")
PATH_START = re.compile(r"^(?:[A-Za-z]:[\\/]|~[\\/]|\.{1,2}[\\/]|[\\/][\w.~-])")
NOT_PATHS = frozenset(["ui/ux", "client/server", "input/output", "read/write", "on/off", "and/or", "yes/no",
                       "i/o", "either/or", "w/o", "true/false", "front/back", "pros/cons", "tcp/ip"])
# a framework or product name, not a file the user means
NOT_FILES = frozenset(["node.js", "vue.js", "next.js", "nuxt.js", "react.js", "angular.js", "express.js",
                       "nest.js", "three.js", "chart.js", "ember.js", "backbone.js", "svelte.js", "deno.js",
                       "asp.net", "ado.net", "vb.net", "socket.io", "math.js", "moment.js", "d3.js"])
FILE_LIKE = re.compile(r"\b[A-Za-z_][\w-]+\.(?:[a-z]{2,5}|[ch]|[A-Z]{2,4})\b")


def path_anchor(words):
    for m in PATH_LIKE.finditer(words):
        text = m.group(0).rstrip(".,;:!?)")
        if text.lower() in NOT_PATHS or "://" in words[m.start():m.end() + 3]:
            continue
        parts = [s for s in re.split(r"[\\/]", text) if s]
        if PATH_START.match(text) or len(re.findall(r"[\\/]", text.rstrip("/\\"))) >= 2 \
                or re.search(r"\.\w{1,5}$", parts[-1] if parts else ""):
            return text
    return None


def file_anchor(words):
    for m in FILE_LIKE.finditer(words):
        if m.group(0).lower() not in NOT_FILES:
            return m.group(0)
    return None


ANCHORS = (  # (kind, pattern or function): something concrete the PM can start from
    ("path", path_anchor),
    ("file", file_anchor),
    # code, a call, camelCase with 2+ humps, snake_case, a::b
    ("symbol", re.compile(r"`[^`\n]+`|\b[A-Za-z_]\w*\(\)|\b[a-z]+(?:[A-Z][a-z0-9]+){2,}\b|"
                          r"\b\w*[A-Za-z]_[A-Za-z]\w*\b|\b\w+::\w+")),
    ("error", re.compile(r"\b\w*(?:Error|Exception)\b|Traceback|\berror:|\bE\d{3,5}\b|exit (?:code|status) \d+")),
    ("command", re.compile(r"\b(?:(?:npm|pnpm|yarn) (?:run|test|install|build|i)|npx \w+|pytest|cargo \w+|"
                           r"go (?:test|build|run|vet)|make (?:test|build|check|all|install|lint)|"
                           r"python3? (?:-m \w+|\S+\.py)|node \S+\.[cm]?js|(?:ba)?sh \S+\.sh|pip3? install|"
                           r"git (?:status|diff|log|commit|push|pull|merge|rebase|checkout|branch|clone|stash|"
                           r"reset|add|show|fetch|cherry-pick|bisect|blame)|mvn \w+|gradle \w+|dotnet \w+|"
                           r"docker \w+|kubectl \w+)\b")),
    ("url", re.compile(r"https?://\S+")),
    ("issue", re.compile(r"(?<![\w&])#\d+\b")),
)
NUMBERED_STEP = re.compile(r"^\s*\d+[.)]\s+\S", re.M)
# only the explicit phrases: "자동으로 저장되게" or "알아서 잘" asks for a feature or for care, not for no questions
# said to the agent: "확인 없이 삭제되는 버그" or "deletes files without asking" describes the program
AUTO_ASKED = re.compile(u"묻지\\s*말고|물어보지\\s*말고|확인\\s*없이\\s*(?:바로\\s*)?(?:진행|해|고쳐|처리)|승인\\s*없이\\s*(?:바로\\s*)?진행|"
                        u"\\bno need to ask\\b|\\bdon[’']?t ask me\\b|\\bdo not ask me\\b|"
                        u"\\bwithout asking me\\b", re.I)
TRIAGE_SKIPS = ("anchored", "small")  # interview.json reasons for an interview `job triage` skipped


def size_hint(words):
    """(small | normal | large, why) from the user's words alone: an escape prefix (quick:, just:, ...),
    large signals or more than LARGE_WORDS words, small signals or at most SMALL_WORDS words."""
    count = len(words.split())
    if SMALL_PREFIX.match(words):
        return "small", "starts with %s" % words.strip().split(":", 1)[0].lower() + ":"
    m = LARGE_SIGNAL.search(words)
    if m:
        return "large", "says %s" % m.group(0)
    if count > LARGE_WORDS:
        return "large", "%d words" % count
    m = SMALL_SIGNAL.search(words)
    if m:
        return "small", "says %s" % m.group(0)
    if count <= SMALL_WORDS:
        return "small", "%d words" % count
    return "normal", "%d words" % count


def anchors_in(words):
    """The concrete things the user's words name: [{"kind", "text"}], at most one per kind."""
    found = []
    for kind, pattern in ANCHORS:
        if callable(pattern):
            text = pattern(words)
        else:
            m = pattern.search(words)
            text = m.group(0) if m else None
        if text:
            found.append({"kind": kind, "text": text.strip()[:80]})
    if len(NUMBERED_STEP.findall(words)) >= 2:
        found.append({"kind": "steps", "text": "%d numbered steps" % len(NUMBERED_STEP.findall(words))})
    return found


def triage(words):
    hint, why = size_hint(words)
    anchors = anchors_in(words)
    return {"size_hint": hint, "why": why, "anchors": anchors,
            # an interview whenever the words name nothing concrete, whatever their size
            "interview": not anchors,
            "auto_asked": bool(AUTO_ASKED.search(words))}


def cmd_triage(job):
    """`job triage`: size hint and anchors from intake.md's User's words, recorded on the job. A
    request that needs no interview gets a finished one, so planning may start."""
    try:
        with io.open(os.path.join(oke_home(), "jobs", job, "intake.md"), encoding="utf-8-sig") as f:
            words = (text_section(f.read(), u"User's words") or "").strip()
    except (IOError, OSError, UnicodeDecodeError):
        words = ""
    if not words:
        return fail("job %s: write intake.md with the user's words under `## User's words` first" % job)
    out = dict(triage(words), job=job)
    with locked():
        state = load_state()
        if job not in state["jobs"]:
            return fail("unknown job: %s" % job)
        state["jobs"][job].update(size_hint=out["size_hint"], anchors=[a["kind"] for a in out["anchors"]])
        save_state(state)
    data = read_json(interview_path(job), None)
    asked = isinstance(data, dict) and data.get("rounds")
    if not out["interview"] and not asked:
        write_json_atomic(interview_path(job), {"rounds": [], "done": True, "reason": "anchored"})
    elif out["interview"] and not asked and isinstance(data, dict) and data.get("reason") in TRIAGE_SKIPS:
        os.remove(interview_path(job))  # new words that name nothing concrete: the interview runs
    print(json.dumps(out, ensure_ascii=False))
    return 0


def archived_dirs(job):
    """The archive folders of `job`, oldest first."""
    return sorted(d for d in glob.glob(os.path.join(oke_home(), "archive", glob.escape(job) + "-*"))
                  if re.match(re.escape(job) + r"-\d{8}-\d{6}$", os.path.basename(d)))


def archived_dir(job):
    """The newest archive folder of `job`, or None."""
    found = archived_dirs(job)
    return found[-1] if found else None


def kept_worktree(kept):
    """The worktree an archived job left for the user (a code job's delivered branch, an
    experiment's outputs, one a no-change end could not remove), or None."""
    wt = (kept.get("worktree") or {}).get("path") if isinstance(kept, dict) else None
    if wt and not kept.get("worktree_removed") and os.path.isdir(native_path(wt)):
        return wt
    return None


def kept_worktrees():
    """[{job, path, branch, done}] of archived jobs whose worktree is still there for the user
    (newest archive of a name wins). `branch`: oke/<job> while it exists (else None); `done`:
    the job finished (a cancelled job's worktree is kept too). A done code job with a branch is
    one a new request may continue."""
    out = {}
    for path in sorted(glob.glob(os.path.join(oke_home(), "archive", "*", "job.json")),
                       key=lambda p: os.path.basename(os.path.dirname(p))[-15:]):
        kept = read_json(path, {})
        wt = kept_worktree(kept)
        if not wt:
            continue
        job = re.sub(r"-\d{8}-\d{6}$", "", os.path.basename(os.path.dirname(path)))
        branch = None
        if kept.get("git_key") and not not_code(kept):
            main = native_path(repo_dir(kept["git_key"]))
            if git_out(main, "rev-parse", "-q", "--verify", "refs/heads/oke/" + job)[0] == 0:
                branch = "oke/" + job
        out[normalize_path(wt)] = {"job": job, "path": wt, "branch": branch, "done": kept.get("phase") == "done"}
    return sorted(out.values(), key=lambda k: k["job"])


def worktree_near(rt, info):
    """The pane whose workspace a job's worktree tab opens in: the caller's when it is the
    job's PM, else the PM's recorded pane while it is still the PM's (pane ids come back after
    a restart: its terminal must match the recorded one), else the caller's, else None."""
    here = rt.whoami() if rt.inside() else None
    state = load_state()
    try:
        me = whoami(state, here)[0] if here else None
    except HerdrError:
        me = None
    if me and me[0] == "pm" and me[2] == info.get("pm"):
        return here
    record = (state["pms"].get(info.get("pm")) or {}).get("pm") or {}
    stored = record.get("pane")
    try:
        if stored and (rt.place_id(stored) == record["terminal"] if record.get("terminal")
                       else rt.place_exists(stored)):
            return stored  # a terminal id is never reused: that pane is still the PM's
    except HerdrError:
        pass  # herdr did not answer: as if the pane were gone
    return here


def own_places(info):
    """The tab and workspace ids OKe opened for a job's worktree and herdr still had when OKe last
    looked (`tabs`; an older record without it: its `id`). A tab id dropped from `tabs` is never
    OKe's again, even when the stored `id` names it."""
    wt = info.get("worktree") or {}
    if "tabs" not in wt:
        return [wt["id"]] if wt.get("id") else []
    return [t for t in wt["tabs"] if t] + ([wt["id"]] if wt.get("id") and ":t" not in wt["id"] else [])


def close_worktree_places(rt, main, wt, own=()):
    """Close the tabs and the workspace OKe opened for the worktree at `wt` (ids in `own`, see
    own_places) whose panes all sit in it: a tab the user opened there stays, and so does one
    whose id herdr gave to another tab after a restart. The folder stays."""
    closed = rt.worktree_places(main, wt, own)
    for wid in closed:
        rt.worktree_remove(wid)
    return closed


def remove_kept_worktree(job, kept, force):
    """Remove a finished job's worktree (when it is still there) and its branch. With force (no
    change was checked, or the user said to throw it away, or the branch is in its base) the
    branch goes even when git calls it unmerged; otherwise only with -d. Returns (removed,
    branch_left)."""
    wt = native_path(kept["worktree"]["path"]) if (kept.get("worktree") or {}).get("path") else None
    main = native_path(repo_dir(kept["git_key"]))
    branch = job_branch(job, kept)
    if wt and os.path.isdir(wt):
        # through the runtime first: herdr closes the worktree's tab and the panes in it (on
        # Windows git cannot delete a folder a shell still sits in). Found by its path, never
        # by the stored id: herdr reuses ids, and that id may be another job's tab now. The
        # runtime removes the folder once the closed panes let go of it
        try:
            rt = runtime()
            close_worktree_places(rt, main, wt, own=own_places(kept))
            rt.worktree_remove(force=force, path=wt, repo=main)
        except HerdrError:
            pass  # herdr did not answer, or git refused: git is asked once more below
    if wt and os.path.isdir(wt):
        try:
            runtime_bg.git_worktree_remove(wt, main, force=force)
        except HerdrError:
            pass
    if wt:
        forget_worktree(wt, worktree_admins(main))
    if wt and os.path.isdir(wt):
        return False, None
    call_timed(["git", "-C", main, "branch", "-q", "-D" if force else "-d", branch],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    left = git_out(main, "rev-parse", "-q", "--verify", "refs/heads/" + branch)[0] == 0
    return True, branch if left else None


def clean_job(job, force=False):
    """`job clean <job>`: remove a finished job's worktree and branch (the user said so). An
    experiment's edits are thrown away; a code job's branch that is not in its base branch,
    or a worktree with uncommitted files, goes only with --force (the user agreed). What
    the user removed already counts as clean."""
    info, folder = job_record(job)
    if info is None:
        return fail("job %s is no job OKe knows" % job)
    for d in reversed(archived_dirs(job) if folder else []):  # the newest archive of that name that had a worktree
        kept = read_json(os.path.join(d, "job.json"), None)
        if isinstance(kept, dict) and (kept.get("worktree") or {}).get("path"):
            info, folder = kept, d
            break
    if folder is None and info.get("phase") not in DONE_PHASES:
        return fail("job %s is %s: clean only a finished job" % (job, info.get("phase")))
    if not info.get("git_key") or info.get("direct") or is_answer(info):
        return fail("job %s has no worktree or branch" % job)
    main = repo_dir(info["git_key"])
    if not main or not os.path.isdir(native_path(main)):
        return fail("the checkout of job %s is gone" % job)
    main = native_path(main)
    wt = (info.get("worktree") or {}).get("path")
    branch = job_branch(job, info)
    has_dir = bool(wt) and os.path.isdir(native_path(wt))
    has_branch = git_out(main, "rev-parse", "-q", "--verify", "refs/heads/" + branch)[0] == 0
    throwaway = not_code(info)  # an experiment's edits, a no-change end: nothing to keep
    if has_dir and not throwaway and not force:
        cwd = native_path(wt)
        code, dirty = git_out(cwd, "status", "--porcelain", "-uno")
        loose = untracked_work(cwd)
        if code != 0 or dirty or loose:
            return fail("the worktree %s has uncommitted files (%s): ask the user; `$OKE job clean %s --force` "
                        "deletes them" % (wt, ", ".join(([l[3:] for l in dirty.splitlines()] + loose)[:5]) or "git status failed",
                                         job))
    if has_branch and not throwaway and not force:
        base = info.get("base")
        merged = bool(base) and git_out(main, "merge-base", "--is-ancestor", "refs/heads/" + branch,
                                        "refs/heads/" + base)[0] == 0
        if not merged:
            return fail("branch %s has commits that are not in %s (not merged): ask the user; `$OKE job clean %s "
                        "--force` deletes it" % (branch, base or "its base branch", job))
    out = {"job": job}
    if not has_dir and not has_branch:
        if wt:
            forget_worktree(native_path(wt), worktree_admins(main))
        out["result"] = "already clean"
    else:
        # checked above: what goes is the user's call, or only byproducts and a merged branch
        removed, branch_left = remove_kept_worktree(job, info, force=True)
        if not removed:
            return fail("could not remove %s: close what still runs in it, then run this again" % wt)
        out.update(result="removed", worktree=wt if has_dir else None, branch=branch if has_branch else None)
        if branch_left:
            out["branch_left"] = branch_left
    if folder:
        info["worktree_removed"] = True
        write_json_atomic(os.path.join(folder, "job.json"), info)
    else:
        with locked():
            state = load_state()
            if job in state["jobs"]:
                state["jobs"][job]["worktree_removed"] = True
                save_state(state)
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
        if args.shared_wiki and args.path:
            return fail("a shared-wiki curation changes no folder: no --path")
        os.makedirs(job_dir)
        state["jobs"][args.slug] = {"pm": args.pm, "path": None, "phase": "intake", "worktree": None,
                                    "phase_at": time.time(), "created": time.time(), "base": None}
        if args.shared_wiki:
            state["jobs"][args.slug]["orchestrator_job"] = True
        if args.path:
            try:
                set_job_path(state, args.slug, args.path)
            except ValueError as e:
                os.rmdir(job_dir)
                return fail(str(e))
        save_state(state)
        info = state["jobs"][args.slug]
        out = {"job_dir": job_dir.replace("\\", "/"), "path": info.get("path"),
               "direct": bool(info.get("direct")) if info.get("path") else None,
               "pm_job_dir": pm_job_dir(args.slug).replace("\\", "/"),
               "pm_dir": pm_dir(args.pm).replace("\\", "/"),
               "pm_agent": args.pm,
               "wiki": wiki_dir(args.pm).replace("\\", "/"),
               "shared_wiki": os.path.join(load_config()["wiki_path"], "_shared").replace("\\", "/")}
        if args.research:  # older instructions: the job starts like any other
            out["note"] = ("--research is gone: every job starts the same way. A job that only answers ends "
                           "with `job answer <job>` while it plans, then its done report")
        print(json.dumps(out, ensure_ascii=False))
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
        research = is_answer(info) and not info.get("path") \
            and info.get("phase") not in DONE_PHASES  # an answer: it may find its path at any step
        started = info.get("worked") or info.get("worktree") or info.get("approved")
        if (info.get("phase") not in ("intake", "planning", "awaiting_approval") or started) and not research:
            if is_answer(info):
                return fail("job %s is an answer and has its path %s: it keeps its path; report a "
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
        if is_answer(info):
            return fail("job %s is an answer: it is not split (it changes nothing)" % args.slug)
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
        intake += u"\n## Split\nSplit from job %s by its PM: this job changes only %s.\n" % (
            args.slug, state["jobs"][new]["path"])
        with io.open(os.path.join(new_dir, "intake.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(intake)
        if os.path.exists(interview_path(args.slug)):
            shutil.copyfile(interview_path(args.slug), interview_path(new))
        save_state(state)
        out = {"job": new, "split_from": args.slug, "path": state["jobs"][new]["path"],
               "direct": bool(state["jobs"][new].get("direct"))}
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
        no_code = not_code(info)
        if no_code and args.phase == "done" and (info.get("no_change") or is_answer(info)):
            allowed = allowed + GATE_PHASES  # ended before it worked: the user agreed, or the PM answered
        if is_answer(info) and args.phase in ("awaiting_approval", "working", "reviewing") and now in GATE_PHASES:
            # an older research job that already works goes on as it was (a dialog sets working again)
            return fail("job %s is answered (`job answer`): it has no plan or work; it ends with `job phase %s "
                        "done` after its done report" % (args.slug, args.slug))
        if args.phase == "awaiting_approval" and now != "awaiting_approval" \
                and not os.path.isfile(os.path.join(oke_home(), "jobs", args.slug, "plan.md")):
            # seen live: a PM did the change while planning and reported done with no plan, and the
            # orchestrator took it for one and let the job work
            return fail("job %s has no plan.md: the user approves a plan, so there is none to show yet. A PM "
                        "plans, it never makes the change itself: give it its planning assignment again (or, if "
                        "it already changed files, tell the user what it did and ask whether to keep it)"
                        % args.slug)
        if args.phase == "working" and now in ("intake", "planning") and not is_answer(info):
            # the older research flow set working at once: a job works only on an approved plan
            return fail("job %s cannot go from %s to working: a job works only on an approved plan. If it only "
                        "answers a question, its PM runs `$OKE job answer %s` and reports done (then `job phase %s "
                        "done`); if it changes something, its plan goes to the user first (`job phase %s "
                        "awaiting_approval`)" % (args.slug, now, args.slug, args.slug, args.slug))
        if allowed is not None and now not in allowed:
            return fail("job %s cannot go from %s to %s: allowed only from %s"
                        % (args.slug, now, args.phase, ", ".join(allowed)))
        if args.phase == "working" and not is_answer(info) and no_path(args.slug, info):
            return fail(no_path(args.slug, info))
        if args.phase in ("awaiting_approval", "working") and not changes_now(info) and not is_answer(info) \
                and info.get("path"):
            # it read only so far: from now on it changes its path, so no other job may change the
            # same files, and its branch must be its own
            problem = path_clash(state, args.slug, info["path"], bool(info.get("direct")), branch_taken=(
                "branch oke/%(job)s already exists in %(checkout)s (an earlier job): ask the user: delete that "
                "branch, or cancel this job and start it under another name"))
            if problem:
                return fail(problem)
        if args.phase in ("awaiting_approval", "working", "reviewing", "done") and experiment_none(args.slug, info):
            return fail(experiment_none(args.slug, info))
        if args.phase in ("awaiting_approval", "working") and not is_answer(info) and plan_too_big(args.slug):
            return fail(plan_too_big(args.slug) + ". Give the PM `kind: plan` to shorten it")
        starts = args.phase == "working" and now == "awaiting_approval" and not is_answer(info)
        in_checkout = bool(info.get("git_key")) and not info.get("direct") and not plan_worktree(args.slug)
        if starts and in_checkout:
            # two jobs changing one checkout in place would mix their files: the later one waits
            other = checkout_busy(state, args.slug)
            if other:
                info["queued"] = {"for": other, "at": time.time()}
                save_state(state)
                print(json.dumps({"job": args.slug, "result": "waiting", "waits_for": other,
                                  "note": "job %s works in the same checkout: this job starts when it is delivered, "
                                          "archived or stopped (`$OKE pending` lists it as `released`); write no "
                                          "execute assignment yet" % other}, ensure_ascii=False))
                return 3
        if starts and in_checkout and not snapshot_dirty(args.slug, info, getattr(args, "snapshot", None)):
            # what is uncommitted there now is the user's: only what the job changes after this is its own
            return fail("git cannot read the checkout %s of job %s" % (info.get("path"), args.slug))
        if args.phase == "done" and experiment(info) and not info.get("no_change"):
            job_dir = os.path.join(oke_home(), "jobs", args.slug)
            review = latest_report(job_dir, r"-rv\d*$")
            worker = latest_report(job_dir, r"-w\d+$")
            if not review or review_verdict(review[1]) != "pass" or (worker and worker[0] > review[0]):
                return fail("job %s is an experiment: done needs the reviewer's `verdict: pass` on its latest "
                            "result" % args.slug)
        if args.phase == "done" and not no_code and deliver_block(args.slug, info):
            # done tells the user the work is ready: its branch on the latest base, checked there
            return fail(deliver_block(args.slug, info))
        if args.phase == "done" and info.get("pm") and not any(
                wiki.logged(w, args.slug, wiki_since(info))  # the shared wiki: the orchestrator curated it
                for w in (wiki_dir(info["pm"]), shared_wiki())):
            return fail("job %s: the PM has not recorded what it taught. It writes the lessons with "
                        "`$OKE wiki write --job %s`, or `$OKE wiki log --job %s --note none`"
                        % (args.slug, args.slug, args.slug))
        if args.phase in ("planning", "working") and now == "intake":  # a re-plan needs no new interview
            problem = interview_problem(args.slug)
            if problem:
                return fail(problem)
        state["jobs"][args.slug].update(phase=args.phase, phase_at=time.time())
        if args.phase != "awaiting_approval":
            info.pop("queued", None)
        if args.phase == "working" and now not in ("working", "reviewing"):
            # set before its execute (curate) assignment is written: until a newer seq is,
            # whatever the PM reports is about the plan (or an answer to it), not the work.
            # Set after it (a research start, a misordered approval): no mark.
            seq = pm_job_report(args.slug)[0]
            if assignment_kind(args.slug) not in WORK_KINDS:
                state["jobs"][args.slug]["start_seq"] = seq
            else:
                state["jobs"][args.slug].pop("start_seq", None)
        if args.phase == "working":
            info["worked"] = True  # a later re-plan does not undo the edits already made
            if not plan_worktree(args.slug):
                info["worked_in_main"] = True  # its edits may sit in the main checkout, whatever a re-plan says
            else:
                info.setdefault("worked_in_main", False)  # new state: never read as an old job's
        if args.phase not in ("intake", "planning", "awaiting_approval", "failed"):
            info["approved"] = True  # past its approval once: its path stays
        save_state(state)
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
    if args.action == "auto":
        info = state["jobs"][args.slug]
        if info.get("phase") in DONE_PHASES:
            return fail("job %s is finished: nothing left to approve" % args.slug)
        if args.off:
            info.pop("auto", None)
        else:
            info["auto"] = True
        save_state(state)
        print(json.dumps({"job": args.slug, "auto": bool(info.get("auto"))}))
        return 0
    if args.action == "files":
        # the user's answer about their own uncommitted files the job changed too: commit them with the
        # job (their edits included), or leave them out of its commits (both edits stay uncommitted)
        info = state["jobs"][args.slug]
        if info.get("phase") in DONE_PHASES:
            return fail("job %s is %s: nothing left to commit" % (args.slug, info.get("phase")))
        settled = info.setdefault("user_files", {})
        for f in args.files:
            settled[rel_path(f)] = args.choice
        save_state(state)
        print(json.dumps({"job": args.slug, "user_files": settled,
                          "next": "the job's tree changed: `$OKE check %s`, a new review with the new `tree:`, "
                                  "then deliver" % args.slug if args.choice == "leave" else
                                  "deliver again"}, ensure_ascii=False))
        return 0
    if args.action == "output":
        return set_output(state, args)
    if args.action == "answer":
        return answer_job(state, args.slug, undo=args.undo)
    if args.action == "size":
        info = state["jobs"][args.slug]
        if info.get("answer"):
            return fail("job %s is an answer: it has no size (`job answer %s --undo` while it plans, if it needs a "
                        "change after all)" % (args.slug, args.slug))
        if info.get("size") == "research":
            # an older research job its PM planned as a change (`kind: research` read as `kind: plan`): the
            # size it records makes it a job with a plan, no longer an answer. It changed nothing (an answer
            # has no write area), so the flags an older OKe set when it started working go: its plan
            # needs the user's approval before anything is written, and it plans again if it was working
            if info.get("phase") not in GATE_PHASES + ("working",):
                return fail("job %s is an answer: it has no size" % args.slug)
            replan = info.get("phase") == "working"
            for flag in ("approved", "worked", "worked_in_main", "start_seq"):
                info.pop(flag, None)
            info["size"] = args.size
            if replan:
                info.update(phase="planning", phase_at=time.time())
            save_state(state)
            print(json.dumps({"job": args.slug, "size": args.size, "phase": info["phase"]}))
            return 0
        if args.size == "small" and experiment(info):
            return fail("job %s is an experiment: it is normal or large (only a worker pane waits for a long "
                        "run)" % args.slug)
        grows = SIZES.index(args.size) > SIZES.index(info.get("size") if info.get("size") in SIZES else "large")
        if info.get("phase") not in ("intake", "planning", "awaiting_approval") and not (
                grows and info.get("phase") in ("working", "reviewing")):
            return fail("job %s: in phase %s a size can only grow, while it works"
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
        if info.get("phase") in DONE_PHASES:
            return fail("job %s is %s: only an open job can go on hold" % (args.slug, info.get("phase")))
        early = is_answer(info) or (
            info.get("phase") in ("intake", "planning", "awaiting_approval") and not info.get("worked"))
        if info.get("direct") and not early:
            return fail("job %s changes its folder in place (no git): cancel it, or let it finish, not hold it"
                        % args.slug)
        main = native_path(os.path.dirname(info.get("git_key") or ""))
        # a plan with a worktree names its branch: main's changes are the user's, not this job's
        uses_worktree = plan_worktree(args.slug)
        if not early and not uses_worktree and info.get("git_key") \
                and git_out(main, "rev-parse", "-q", "--verify", "refs/heads/" + job_branch(args.slug, info))[0] != 0:
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
            try:  # what it was (its worktree and branch): `job clean`, `job pr` and a job continuing it ask
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
        elif experiment(info_was) and wt:
            print("kept_worktree: %s (the experiment's outputs: remove it with `$OKE job clean %s` when the user "
                  "says so)" % (wt, args.slug))
        elif job_output(info_was) == "code" and wt and not info_was.get("worktree_removed"):
            why = "for the user to merge" if info_was.get("phase") == "done" else "the cancelled job's work"
            print("kept_worktree: %s (branch %s, %s: remove it with `$OKE job clean %s` when "
                  "the user says so)" % (wt, job_branch(args.slug, info_was), why, args.slug))
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
                    # it is still there and refused: keep its record
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


def wiki_since(info):
    """From when a wiki log line counts for the job: its start, or after its answer was undone
    (`job answer --undo`), since lessons logged for the answer do not cover the change."""
    undone = info.get("answer_undone")
    return max(info.get("created") or 0, undone + 1) if undone else info.get("created")


def answer_job(state, job, undo=False):
    """`job answer <job>`: the PM investigated and nothing needs to change, so the job ends with
    its answer (the done report): no plan, approval, worktree, review or delivery. `--undo`, while
    the job still plans: the investigation found a change after all, and the job goes on to a plan
    (state is under the lock)."""
    info = state["jobs"][job]
    if undo:
        if not is_answer(info):
            print(json.dumps({"job": job, "answer": False, "unchanged": True}))
            return 0
        if info.get("phase") not in ("intake", "planning"):
            return fail("job %s is %s: an answer goes back to a plan only while its PM investigates it"
                        % (job, info.get("phase")))
        info.pop("answer", None)
        info["answer_undone"] = time.time()  # what it logged as an answer is not the change's lessons
        if info.get("size") == "research":
            info.pop("size")  # an older research job: from now on a job with a plan
        save_state(state)
        print(json.dumps({"job": job, "answer": False}))
        return 0
    if is_answer(info):
        print(json.dumps({"job": job, "answer": True, "unchanged": True}))
        return 0
    if info.get("held"):
        return fail("job %s is on hold: leave it until a message says it is back" % job)
    if info.get("phase") != "planning":
        return fail("job %s is %s: a job is answered only while its PM investigates it (phase planning)"
                    % (job, info.get("phase")))
    if info.get("worked") or info.get("worktree") or info.get("approved"):
        return fail("job %s was approved and has worked: it does not end as an answer. If it needs no change, "
                    "undo what it changed and report the no-change question" % job)
    info["answer"] = True
    save_state(state)
    print(json.dumps({"job": job, "answer": True}))
    return 0


def set_output(state, args):
    """`job output <job> code|result [--no-change]` (state is under the lock)."""
    info = state["jobs"][args.slug]
    if info.get("held"):
        return fail("job %s is on hold: leave it until a message says it is back" % args.slug)
    if is_answer(info):
        return fail("job %s is an answer: its output is its answer (result)" % args.slug)
    # the output needs no path: the planner names both at once, and the PM may record either first
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
        if phase not in GATE_PHASES or deliver_state(args.slug)[0] != "none" \
                or ((info.get("worked") or info.get("worktree")) and job_changes(args.slug, info)):
            return fail("job %s has worked on its plan (%s): its output stays %s. Report a question: the user "
                        "decides (cancel it and start a new job)" % (args.slug, phase, job_output(info)))
        if args.output == "result" and phase == "awaiting_approval" and \
                experiment_none(args.slug, dict(info, output="result")):
            return fail(experiment_none(args.slug, dict(info, output="result")))
        info["output"] = args.output
        out = {"job": args.slug, "output": args.output}
        if args.output == "result" and info.get("size") == "small":
            info["size"] = out["size"] = "normal"  # an experiment's run needs a worker pane
        save_state(state)
        print(json.dumps(out))
        return 0
    # the user ends a job that changed nothing: only the orchestrator, on the user's answer
    if not caller_is_orchestrator(state, save=False):
        return fail("only the orchestrator ends a job with no change, on the user's answer: report a "
                    "question")
    if args.output != "result":
        return fail("--no-change ends the job with output result")
    if phase in DONE_PHASES or deliver_state(args.slug)[0] != "none":
        return fail("job %s is %s: too late to end it with no change" % (args.slug, phase))
    # a folder outside git has nothing to compare with: the user's word is the check
    changed = None if info.get("direct") or not info.get("git_key") else job_changes(args.slug, info)
    if changed:
        return fail("job %s changed something (%s): it does not end with no change; finish it, or cancel it"
                    % (args.slug, changed))
    if info.get("pm") in state["pms"] and not wiki.logged(wiki_dir(info["pm"]), args.slug, wiki_since(info)):
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
    main = checkout_dir(info)
    # the main checkout counts only when the job worked there (a job from before the flag:
    # it worked without a worktree in its plan); otherwise its changes are the user's or another job's
    in_main = info.get("worked_in_main") or (
        "worked_in_main" not in info and info.get("worked") and not wt and not plan_worktree(job))
    if not wt and not in_main:
        return None  # it never worked, or its work belongs in a worktree it does not have yet
    cwd = native_path(wt) if wt else main
    for where in ([cwd, main] if wt and in_main else [cwd]):
        # in the checkout, the user's uncommitted files from before the job are not its change
        files = job_files(job, dict(info, worktree=None) if where == main else info, where)
        if files is None:
            return "git status failed in %s" % where
        if files["paths"]:
            return "changed files%s: " % (" in the checkout" if where != cwd else "") + ", ".join(files["paths"][:5])
    branch = "refs/heads/" + job_branch(job, info)
    point = base_point(info, cwd, job)
    tips = [git_out(cwd, "rev-parse", "HEAD")[1]] if wt else []
    if branch and git_out(main, "rev-parse", "-q", "--verify", branch)[0] == 0:
        tips.append(git_out(main, "rev-parse", branch)[1])
    for tip in tips:
        # the tree counts, not the commits: a reverted change leaves commits but no change
        if call_timed(["git", "-C", main, "diff", "--quiet", point or "HEAD", tip],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
            return "commits on its branch change files"
    return None


def wiki_verified(job, info):
    """Lessons count only from work that was checked (Hermes: learning from unchecked
    sessions taught wrong rules)."""
    if is_answer(info) or info.get("no_change"):
        return True
    if not_code(info):
        review = latest_report(os.path.join(oke_home(), "jobs", job), r"-rv\d*$")
        return bool(review) and review_verdict(review[1]) == "pass"
    return deliver_state(job)[0] == "delivered"


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
                return fail("job %s is not checked yet (delivered, the passing review of a job whose output is "
                            "not code, an answer (`job answer`), or an end with no change): write its lessons after that"
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


def never_started(state, agent):
    """What to do when `agent` names a PM OKe knows that was never started, else None."""
    if agent not in state["pms"] or (state["pms"][agent].get("pm") or {}).get("agent"):
        return None
    return ("PM %s was never started, so it has no inbox yet: start it first (`$OKE place %s` prints a place, "
            "then `$OKE spawn %s --pane <place>`), then send again. Never do the job yourself"
            % (agent, agent, os.path.join(pm_dir(agent), "assignment.md").replace("\\", "/")))


def cmd_send(args):
    text = " ".join(" ".join(args.text).split())
    if not text:
        return fail("empty message")
    if text.startswith("/"):
        return fail("a message must not start with '/': Git Bash rewrites it into a Windows path")
    state = load_state()
    found = find_target(state, args.agent)
    if found is None:
        return fail(never_started(state, args.agent) or "unknown agent: %s" % args.agent)
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

# an assignment written (an approval or an answer acted on) but not sent yet comes before anything else:
# the user's answer is never pushed back by other jobs' reports
PENDING_RANKS = {"unsent": -1, "unassigned": -1, "split": 0, "report_done": 0, "answer": 0, "no_change": 0, "silent": 1,
                 "question": 1, "blocked": 1, "failed": 1, "peer": 1, "plan": 2, "released": 0}
ASKING_PHASES = ("planning", "working", "reviewing")


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
    if job.get("no_change"):
        # the user ended it with no change and a compaction came before `job phase done`
        kind, since, mark = "no_change", job.get("phase_at"), "end"
    elif phase == "working" and "start_seq" in job and job["start_seq"] == seq:
        # phase set first (approval, a curate start), its assignment never
        # written: the report is not the work's, and this job must not look done
        kind, since, mark = "unassigned", job.get("phase_at"), seq
    elif seq is None and phase == "intake" and job.get("split_from"):
        kind, since, mark = "split", job.get("phase_at"), job.get("split_from")  # a PM split it off
    elif seq is not None and job.get("sent_seq") != seq:
        kind, since, mark = "unsent", job.get("phase_at"), seq  # written, never sent
    elif rtype == "question" and pm_question_problem(name) and not is_silent(name, job, seq) and not ruling:
        return None  # the PM's wait sends it back to the PM to ask in the required form
    elif rtype in ("question", "blocked", "failed") and not (rtype == "question" and pm_question_problem(name)):
        kind, since, mark = rtype, mtime, seq
    elif rtype not in FINAL_TYPES and is_silent(name, job, seq) or rtype == "question" and not ruling:
        # no report, a bad one, or a wait the PM was told twice will not end
        kind, since, mark = "silent", mtime, seq
    elif ruling:
        # the PMs are in the mark: a later missing answer is a new item, even after a deferral
        kind, since, mark = "peer", os.path.getmtime(peers_path(name)), "%s:%s" % (job.get("sent_seq"), ",".join(ruling))
    elif rtype == "done" and (phase in ASKING_PHASES or phase == "intake" and is_answer(job)):
        # an older research job may report done still in intake: it is an answer too
        # an answer (`job answer`) ends with its report: no plan to show, nothing to deliver
        kind, since, mark = "answer" if is_answer(job) else "report_done", mtime, seq
    elif phase == "awaiting_approval" and job.get("queued"):
        # approved, waiting for another job in the same checkout: released once that one is out of the way
        if checkout_busy(load_state(), name):
            return None
        kind, since, mark = "released", job["queued"].get("at"), job["queued"].get("for")
    elif phase == "awaiting_approval":
        kind, since, mark = "plan", job.get("phase_at"), job.get("phase_at")
    else:
        return None
    key = "%s:%r" % (kind, mark)
    item = {"job": name, "kind": kind, "rank": PENDING_RANKS[kind], "since": since or 0,
            "key": key, "deferred": job.get("deferred") == key}
    if ruling and kind in ("peer", "question", "blocked", "failed"):
        item["peers"] = ruling  # the answer that settles this item must settle these too
    if job.get("auto") and not ruling:  # the user said not to ask again for this job (`job auto`)
        if kind == "plan":
            item["auto"] = "approve"
        elif kind == "question":
            picks = recommended_picks(name)
            if picks:
                item.update(auto="recommended", picks=picks)
    if kind == "blocked" and report.get("rule"):
        item["oke_rule"] = report["rule"]  # an OKe rule stops the work, not a dialog: a bug for OKe's maintainer
    if kind == "no_change":
        item["text"] = ("job %s ended with no change (the user agreed): do not ask again; finish it as the "
                        "Flow row says (wiki, `job phase done`, archive)" % name)
    if kind == "unassigned":
        item["text"] = ("job %s is in phase working but its assignment for that (execute or curate) "
                        "was never written: write and send it now" % name)
    if kind == "released":
        item["text"] = ("job %s was approved and waited for job %s in the same checkout, which is out of the "
                        "way now: run `$OKE job phase %s working`, then give the PM `kind: execute` (no question)"
                        % (name, job["queued"].get("for"), name))
    if kind == "answer":
        item["text"] = ("job %s is answered: nothing changes. Tell the user the report's Summary, then "
                        "`job phase %s done` and `job archive %s`" % (name, name, name))
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
    new.add_argument("--research", action="store_true", help=argparse.SUPPRESS)  # older instructions: ignored
    new.add_argument("--shared-wiki", action="store_true",
                     help="the orchestrator's own job, curating the shared wiki: --pm only for its wiki log, no PM "
                          "works on it")
    ja = job_sub.add_parser("answer", help="the PM investigated and nothing needs to change: the job ends with "
                            "its done report (no plan, approval or delivery)")
    ja.add_argument("slug")
    ja.add_argument("--undo", action="store_true", help="while it still plans: the job needs a change after all")
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
    rounds = job_sub.add_parser("rounds", help="show a job's review round and limit, or set the limit")
    rounds.add_argument("slug")
    rounds.add_argument("n", nargs="?", type=int)
    output = job_sub.add_parser("output", help="what the job hands over: code (its branch) or result "
                                "(an answer or a run's outputs)")
    output.add_argument("slug")
    output.add_argument("output", choices=OUTPUTS)
    output.add_argument("--no-change", action="store_true",
                        help="the orchestrator, on the user's answer: end a job that changed nothing")
    clean = job_sub.add_parser("clean", help="remove a finished job's worktree and branch (the user said so)")
    clean.add_argument("slug")
    clean.add_argument("--force", action="store_true",
                       help="the user agreed: also an unmerged branch or uncommitted files")
    size = job_sub.add_parser("size", help="the job's size, which the PM decides while planning")
    size.add_argument("slug")
    size.add_argument("size", choices=SIZES)
    job_sub.add_parser("deliver", help="after the review: the branch onto the latest base, checked there "
                       "(no worktree: commit in the main checkout)").add_argument("slug")
    job_sub.add_parser("pr", help="push the delivered branch and open a GitHub pull request").add_argument("slug")
    jbase = job_sub.add_parser("base", help="the branch a job's work goes onto, when it has none (the user names it)")
    jbase.add_argument("slug")
    jbase.add_argument("branch")
    job_sub.add_parser("tree", help="the tree of the job's work (its own changes on HEAD), which a review of "
                       "uncommitted work names").add_argument("slug")
    job_sub.add_parser("plan", help="the PM, after writing plan.md: refuses a plan too big to show or check"
                       ).add_argument("slug")
    jf = job_sub.add_parser("files", help="the user's answer about their uncommitted files the job changed too")
    jf.add_argument("slug")
    jf.add_argument("choice", choices=("include", "leave"))
    jf.add_argument("files", nargs="+")
    job_sub.add_parser("triage", help="size hint and anchors from intake.md's User's words; says whether to "
                       "interview").add_argument("slug")
    jauto = job_sub.add_parser("auto", help="the user said: do not ask again for this job (approvals, questions "
                               "with a recommended option)")
    jauto.add_argument("slug")
    jauto.add_argument("--off", action="store_true", help="ask again")
    jr = job_sub.add_parser("restore-user", help="bring one of the user's files back to what it held when the "
                            "job began (never `git checkout` it)")
    jr.add_argument("slug")
    jr.add_argument("file")
    jac = job_sub.add_parser("accept-check", help="the user accepts a failing check whose base could not run, "
                             "or whose new failures the base has too (base_has_it) (orchestrator)")
    jac.add_argument("slug")
    jac.add_argument("target", help="the commit or tree:<sha> `oke check` printed")
    jac.add_argument("--by-user", action="store_true", help="the user answered it (needed on `job auto`)")
    jah = job_sub.add_parser("accept-head", help="the user made the commits in the checkout during the job "
                             "(orchestrator)")
    jah.add_argument("slug")
    jah.add_argument("--by-user", action="store_true", help="the user answered it (needed on `job auto`)")
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

    bl = sub.add_parser("baseline")  # no help line: older skills' no-op, `check` runs the base itself
    bl.add_argument("job")
    bl.set_defaults(func=cmd_baseline)
    ck = sub.add_parser("check", help="run the Check command on the job's work; on a failure, also on its base "
                        "commit, so failures that were there already do not count")
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
    tick.add_argument("--session", help="the session the mod runs in")
    tick.add_argument("--board", action="store_true")
    tick.add_argument("--events-from", type=int)
    tick.set_defaults(func=cmd_tick)

    answered = sub.add_parser("answered", help="the orchestrator's user answered a popup (mod)")
    answered.add_argument("--pane", required=True)
    answered.add_argument("--session")
    answered.set_defaults(func=cmd_answered)

    take = sub.add_parser("take-mail", help="the agent's mod takes its inbox after a turn (mod)")
    take.add_argument("--agent", required=True)
    take.add_argument("--pane", required=True)
    take.add_argument("--give-back", nargs="+", metavar="NAME")
    take.set_defaults(func=cmd_take_mail)

    boundary = sub.add_parser("boundary", help="the assignments this agent has answered (mod)")
    boundary.add_argument("--pane", required=True)
    boundary.add_argument("--session", help="the session the mod runs in")
    boundary.set_defaults(func=cmd_boundary)
    retry = sub.add_parser("api-retry")
    retry.add_argument("--pane", required=True)
    retry.add_argument("--session", help="the session the mod runs in")
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
    pc.add_argument("--session", help="the session the mod runs in")
    pc.add_argument("--path", required=True)
    pc.set_defaults(func=cmd_path_check)

    board = sub.add_parser("board", help="the board as text, for hosts without the mod pane")
    board.add_argument("--full", action="store_true", help="also each job's PM, agents, cost and tasks")
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
