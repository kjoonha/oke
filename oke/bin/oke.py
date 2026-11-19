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

import runtime_bg
import runtime_herdr
import runtime_orca
import wiki
from runtime_herdr import DialogError, HerdrError, herdr, herdr_text, session_of  # noqa: F401 (tests)

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
        "models": {"pm": "opus", "worker": "opus", "reviewer": "opus"},
        "agents": {"worker": "claude", "reviewer": "claude"},  # which CLI runs each role
        "lean_launch": True,  # children skip the user's plugins, hooks and MCP servers
        "max_review_rounds": 10,  # a job's reviews stop there and the PM asks the user; null: no limit
        "theme": "disco",  # the band's characters: disco, slime or mushroom
    }
    try:
        with io.open(os.path.join(home, "config.json"), encoding="utf-8") as f:
            user = json.load(f)
    except (IOError, OSError, ValueError):
        user = {}
    if isinstance(user, dict):
        models = user.pop("models", None)
        agents = user.pop("agents", None)
        config.update(user)
        if isinstance(models, dict):
            config["models"].update(models)
        if isinstance(agents, dict):
            config["agents"].update(agents)
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
    for job in state["jobs"].values():
        if isinstance(job, dict) and "lane" in job:
            lane = job.pop("lane")
            if lane in OLD_LANES:
                job["size"] = OLD_LANES[lane]
    if loaded.get("runtime") in RUNTIMES:
        state["runtime"] = loaded["runtime"]
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


def slug(text, limit=24):
    s = re.sub(r"[^a-z0-9-]", "-", text.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    if not s or not s[0].isalpha():
        s = "x" + s
    return s[:limit].rstrip("-")


# ---------------------------------------------------------------- herdr

# ---------------------------------------------------------------- runtime

RUNTIMES = {"herdr": runtime_herdr.HerdrRuntime, "bg": runtime_bg.BgRuntime, "orca": runtime_orca.OrcaRuntime}


def default_runtime():
    """herdr inside a herdr pane, orca inside an Orca terminal or chat, else bg."""
    if os.environ.get("HERDR_ENV") == "1":
        return "herdr"
    return "orca" if os.environ.get("ORCA_TERMINAL_HANDLE") else "bg"
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
KINDS = ("claude", "codex", "opencode")


def first_prompt(kind, role, path):
    if kind == "claude":
        return FIRST_PROMPT.format(role=role, path=path)
    # Other CLIs do not load Claude Code plugins: point them at the skill file itself.
    plugin = plugin_root().replace("\\", "/")
    return ("Read the OKe {role} instructions in {plugin}/skills/{role}/SKILL.md and follow them. "
            "Where they say ${{CLAUDE_PLUGIN_ROOT}}, use {plugin}. Your assignment file is {path}. "
            "Read it first.").format(role=role, plugin=plugin, path=path)


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


def agent_command(kind, role, config, home, plugin_dir, folder, resume=None):
    """herdr's --kind and the CLI arguments for one worker or reviewer."""
    bank_usage(folder)
    if runtime().name == "orca":
        config = dict(config, lean_launch=False)  # Orca starts the agent: no flags reach it
    if kind == "claude":
        header = read_header(os.path.join(folder, "assignment.md")) if folder else None
        return kind, launch_args(role, config, home, plugin_dir, resume, folder, header)
    conservative = config.get("autonomy") == "conservative"
    if kind == "codex":
        # Hooks off: codex asks about untrusted hooks at a moment herdr cannot see,
        # and a prompt typed into that question can pick "Trust all" (seen live).
        args = ["-c", "features.hooks=false"] + (["resume", resume] if resume else [])
        if role == "reviewer":
            # Reads anywhere, writes only its own OKe folder: the sandbox enforces it.
            args += ["-C", folder, "-s", "workspace-write", "-a", "never"]
        elif conservative:
            args += ["-s", "workspace-write", "-a", "on-request",
                     "--add-dir", home, "--add-dir", config["wiki_path"]]
        else:
            args += ["--dangerously-bypass-approvals-and-sandbox"]
        return kind, args
    # opencode has no read-only mode: a reviewer stays read-only by its instructions,
    # and the PM checks `git status` after the review (pm skill).
    args = ["--session", resume] if resume else []
    if not (conservative and role == "worker"):
        args += ["--auto"]
    return kind, args
REVIEWER_TOOLS = ["Read", "Grep", "Glob", "Skill", "Bash(git diff:*)", "Bash(git log:*)",
                  "Bash(git show:*)", "Bash(git status:*)"]
# shortcut: relies on `*` matching anywhere in a Bash rule (not probed live, see
# docs/probe-results.md); the PM's `git status` check after a review is the backstop.
REVIEWER_DENIED = ["Bash(git *--output*)"]


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
    id from them, revive needs it), env, and the plugins its assignment asks for.
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
    # the user's compaction and effort settings hold in children too
    for name in ("autoCompactWindow", "effortLevel", "modelSettings"):
        if name in user:
            settings[name] = user[name]
    if plugins:
        settings["enabledPlugins"] = dict((p, True) for p in plugins)
    if dirs:
        # `--add-dir` stops every new session at a trust dialog for a folder never
        # trusted (seen live with a fresh OKe home); this settings key does not
        settings["permissions"] = {"additionalDirectories": list(dirs)}
    path = os.path.join(folder, "lean-settings.json")
    try:
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(settings, indent=2, ensure_ascii=False))
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
    "reviewer": ["Bash", "Read", "Glob", "Grep", "Skill", "TaskCreate", "TaskUpdate", "TaskList",
                 "WebFetch", "WebSearch"],
}
# Always on, whatever the PM writes: built-in tools only (user's choice). MCP servers
# and plugins come only from the assignment header.


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
        rt.close(args.place)
        out = {"closed": args.place}
    elif args.rt == "place":
        repo = native_path(args.repo)
        if not os.path.isdir(repo):  # herdr opens a missing --cwd in the home folder
            return fail("place needs the repository's path, not its id: %s is no directory" % args.repo)
        out = {"place": rt.place_for_repo(repo, os.path.basename(repo.rstrip("/\\")))}
    elif args.rt == "worktree":
        if args.remove:
            rt.worktree_remove(args.remove)
            out = {"removed": args.remove}
        else:
            if not args.job or not args.repo:
                return fail("worktree needs <job> and --repo (or --remove <id>)")
            if not os.path.isdir(native_path(args.repo)):
                return fail("worktree --repo needs the repository's path: %s is no directory" % args.repo)
            # shortcut: `oke job merge` merges only oke/<job>, so the branch name is fixed here
            out = rt.worktree_create(native_path(args.repo), "oke/" + args.job, args.job)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def launch_args(role, config, home, plugin_dir, resume=None, folder=None, header=None):
    """`header` is the agent's assignment header: its `tools:`, `mcp:` and `plugins:`
    (comma separated) are what the PM switched on for this job."""
    model = (header or {}).get("model") or config["models"].get(role, "opus")
    args = ["--model", model, "--plugin-dir", plugin_dir]
    # Measured: the user's plugins, hooks and MCP servers add about 36k tokens to every
    # child's context. Without a settings file, start as before so the herdr hook stays.
    folder = folder or home
    dirs = [home, config["wiki_path"]]
    settings = config.get("lean_launch", True) and write_lean_settings(folder, header_list(header, "plugins"), dirs)
    if not settings:
        args += ["--add-dir", home, "--add-dir", config["wiki_path"]]
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
                with io.open(path, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"mcpServers": servers}, indent=2, ensure_ascii=False))
                args += ["--mcp-config", path]
            except (IOError, OSError):
                pass  # the child starts without them
    if resume:
        args += ["--resume", resume]
    if role == "reviewer":
        # dontAsk denies everything not listed without stopping at a dialog.
        args += ["--permission-mode", "dontAsk", "--allowedTools"] + REVIEWER_TOOLS
        args += edit_rules(home) + edit_rules(config["wiki_path"])
        # `git diff/log/show --output=<file>` writes a file: deny rules beat allow rules
        args += ["--disallowedTools"] + REVIEWER_DENIED
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


def remote_url(key):
    """The repository's origin URL, or "" without one."""
    p = subprocess.run(["git", "--git-dir", native_path(key), "config", "--get", "remote.origin.url"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.stdout.decode("utf-8", "replace").strip() if p.returncode == 0 else ""


def wiki_name(key, url):
    """A repository's wiki folder name, the same on every machine and checkout path:
    `owner-repo` from the origin URL, else the repository's own name."""
    parts = [x for x in re.split(r"[/:]", re.sub(r"(\.git)?/*$", "", url)) if x] if url else []
    if len(parts) >= 2:
        return "%s-%s" % (slug(parts[-2], 18), slug(parts[-1]))
    return re.sub(r"-[0-9a-f]{6}$", "", repo_name(key))


WIKI_MARK = ".oke-repo"


def wiki_dir(key):
    """The repository's wiki folder, made if missing. The folder notes whose it is:
    another repository with the same name gets the name plus a hash instead. A wiki
    under the older `<name>-<hash>` folder moves to the new name."""
    root = load_config()["wiki_path"]
    url = remote_url(key)
    owner = url or key
    path = os.path.join(root, wiki_name(key, url))
    try:
        with io.open(os.path.join(path, WIKI_MARK), encoding="utf-8") as f:
            if f.read().strip() != owner:
                path = os.path.join(root, repo_name(key))  # the name is taken
    except (IOError, OSError):
        pass
    old = os.path.join(root, repo_name(key))
    if old != path and os.path.isdir(old) and not os.path.exists(path):
        os.rename(old, path)
    if not os.path.isdir(path):
        os.makedirs(path)
    mark = os.path.join(path, WIKI_MARK)
    if not os.path.exists(mark):
        with io.open(mark, "w", encoding="utf-8") as f:
            f.write(owner + "\n")
    return path


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
    clear_hold(agent)
    config = load_config()
    kind = str(header.get("kind") or "").strip().lower() or ("claude" if role == "pm" else config["agents"].get(role, "claude"))
    if kind not in KINDS:
        return fail("unknown agent kind %r: use one of %s" % (kind, ", ".join(KINDS)))
    if role == "pm" and kind != "claude":
        return fail("a PM must be claude: it needs the OKe mod, hooks and AskUserQuestion")

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
        over = role == "reviewer" and over_rounds(header["job"], job, config, int(header["seq"]))
        if over:
            return fail(over)
        key = job["repo"]

    if any(a.get("name") == agent for a in list_agents()):
        return fail("agent name in use: %s" % agent)

    # A report left by an earlier run would satisfy the first `oke wait` at once.
    stale = os.path.join(os.path.dirname(path), "report.md")
    if os.path.exists(stale):
        os.remove(stale)

    pane = args.pane
    if not pane:
        pane = runtime().split(args.split_from, cwd)

    record = {"agent": agent, "pane": pane, "session": None, "kind": kind,
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
                where = runtime().place_cwd(pane)
                return repo_key(where) == key
        except (ValueError, HerdrError):
            return False
        return True

    kind, cli_args = agent_command(kind, role, config, oke_home(), plugin_root(),
                                   os.path.dirname(path), args.resume)
    try:
        info = runtime().start(agent, pane, cli_args, first_prompt(kind, role, path),
                           trusted_folder, save_record, kind)
    except DialogError as e:
        # The record stays (the user may answer the dialog); a revive closes the old
        # pane only when it knows this terminal.
        record["terminal"] = runtime().place_id(pane)
        save_record()
        return fail("agent %s is stopped at a dialog in pane %s:\n%s" % (agent, pane, e))
    except HerdrError as e:
        if e.code == "herdr_unavailable":
            raise
        return fail("spawn of %s failed in pane %s: %s" % (agent, pane, e))
    record["pane"] = info.get("pane_id", pane)
    record["session"] = session_of(info)
    record["terminal"] = runtime().place_id(record["pane"])  # lets a revive close this pane safely
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
    job_dir = os.path.dirname(os.path.dirname(report))
    job = load_state()["jobs"].get(os.path.basename(job_dir), {})
    archived = not job and (normalize_path(os.path.dirname(job_dir))
                            == normalize_path(os.path.join(oke_home(), "jobs")))
    if archived or job.get("cancelled"):
        return "cancelled"  # the user gave the job up: nothing about its children matters
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


_sessions = {}


def remember_session(agent, info):
    """codex gets its session id only after the first turn: record it when herdr
    shows it, so a revive resumes that session."""
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
        if result == "gone" and jobs_key is not None and not open_jobs(load_state(), jobs_key):
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
GONE_CONFIRMATIONS = 3  # one failed look (a CLI hiccup) is not a death
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
            "findings; the user may allow more (`oke job rounds %s <n>`)" % (job, seq, limit, job))


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
    return runtime().new_place(parents, cwd)


def relaunch(state, role, key, name, record, folder):
    if role == "orchestrator":
        agent, prompt, cwd = ORCH_NAME, "/oke:start", native_path(record.get("cwd") or oke_home())
        args = strip_resume(record["argv"])
    else:
        path = os.path.join(folder, "assignment.md").replace("\\", "/")
        cwd = native_path((read_header(path) or {}).get("cwd") or oke_home())
        kind = record.get("kind") or "claude"
        agent = name
        if kind == "claude":
            prompt = REVIVE_PROMPT.format(role=role, path=path)
        else:
            prompt = "You were restarted after a crash. " + first_prompt(kind, role, path) + \
                " Also re-read your board.md, then continue."
        if role == "pm":
            prompt += pm_jobs_note(state, key)
        kind, args = agent_command(kind, role, load_config(), oke_home(), plugin_root(), folder,
                                   record.get("session"))
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
        return runtime().start(agent, pane, args, prompt, lambda: False,
                           kind=kind if role != "orchestrator" else "claude")
    except (HerdrError, DialogError):
        # herdr keeps the name of a start that failed; with the pane closed it is
        # gone again, so the next watcher round does not read it as alive.
        try:
            runtime().close(pane)
        except HerdrError:
            pass
        raise


def clear_hold(agent):
    """The user's hold on a dead PM (the orchestrator's `$OKE_HOME/hold/<pm>`) ends when
    the PM is started or revived again, or has no open job left."""
    try:
        os.remove(os.path.join(oke_home(), "hold", agent))
    except OSError:
        pass


def revive(name):
    """Bring a dead OKe agent back in its own session. Returns revived, alive, busy
    (another process is reviving it), not_needed, failed or unknown. Only an explicit
    request revives: nothing in OKe restarts a dead agent by itself."""
    clear_hold(name)
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
        revives = 0  # it reported since the last revive (the board shows the count)
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
            # herdr was down, the agent did not crash again: do not count this try
            update_record(role, key, name, {"revives": revives})
            raise
        log_event("revive_failed %s: %s" % (name, (str(e).strip().splitlines() or [""])[0]))
        return "failed"
    fields.update(pane=info.get("pane_id"), session=session_of(info) or record.get("session"))
    fields["terminal"] = runtime().place_id(fields["pane"]) if fields["pane"] else None
    if role == "orchestrator":
        fields["agent"] = ORCH_NAME
    update_record(role, key, name, fields)
    log_event("revived %s pane %s" % (name, fields["pane"]))
    return "revived"


def cmd_revive(args):
    if args.reset:  # a fresh start of the restart count the board shows
        found = find_target(load_state(), args.agent)
        if found:
            update_record(found[0], found[2], found[1], {"revives": 0, "gave_up": False})
    print(revive(args.agent))
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
    pane = getattr(runtime(), "canonical", lambda p: p)(pane)  # Orca: a follow-up's new terminal
    if pane and pane.startswith("session:"):
        # a session id is never reused: no need to ask the runtime who is there
        sid = pane[len("session:"):]
        mine = [r for r in records(state) if r[3].get("pane") == pane or r[3].get("session") == sid]
        return (mine[0] if mine else None), list_agents()
    mine = [r for r in records(state) if r[3].get("pane") == pane]
    agents = None
    if not mine and hasattr(runtime(), "canonical"):
        # Orca: an agent whose follow-up started in a new terminal, before anyone looked
        agents = list_agents()  # looking moves the registry to its new terminal
        pane = runtime().canonical(pane)
        mine = [r for r in records(state) if r[3].get("pane") == pane]
    if not mine:
        return None, []
    agents = agents if agents is not None else list_agents()
    here = [a for a in agents if a.get("pane_id") == pane]
    if not here:
        # Orca lists only the workers OKe started, never the user's own session: there
        # the orchestrator's recorded place is all there is (seen live: role null)
        if not runtime().owns_orchestrator:
            return next((r for r in mine if r[0] == "orchestrator"), None), agents
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
    # the agent's own task list, written by its OKe mod
    todos = read_json(os.path.join(folder, "todos.json"), []) if folder else []
    todos = [t for t in todos if isinstance(t, dict)] if isinstance(todos, list) else []
    # what the agent's session has cost so far, written by its OKe mod after each turn
    cost = folder_cost(folder) if folder and (os.path.exists(os.path.join(folder, "usage.json"))
                                              or os.path.exists(os.path.join(folder, "usage-prev.json"))) else None
    return {"agent": name, "role": role, "status": a.get("agent_status") if a else "dead",
            "pane": (a or {}).get("pane_id") or record.get("pane"),
            "report": (report or {}).get("type"), "board": board,
            "revives": record.get("revives", 0),
            "cost": cost if isinstance(cost, (int, float)) else None,
            "todos": [{"content": str(t.get("content", "")), "status": str(t.get("status", "pending"))}
                      for t in todos]}


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
            info = state["jobs"][job]
            waiting = pending_item(job, info)
            jobs.append({"job": job, "phase": info.get("phase"), "agents": nodes,
                         "size": info.get("size"), "direct": bool(info.get("direct")),
                         "merge": merge_state(job)[0],
                         "pending": waiting["kind"] if waiting and not waiting["deferred"] else None})
        tree.append({"repo": repo_name(key), "key": key, "jobs": jobs,
                     "pm": board_node("pm", pm["agent"], key, pm, agents) if pm and pm.get("agent") else None})
    return tree


TODO_MARKS = {"completed": "v", "in_progress": ">", "pending": "o"}


def board_lines(tree):
    """The board as (depth, text) lines: one renderer-neutral list for --text and --html."""
    out = []

    def agent(n, depth):
        out.append((depth, "%s %s: %s%s" % (n["role"], n["agent"], n["status"],
                                            ", report " + n["report"] if n.get("report") else "")))
        for t in n.get("todos") or []:
            out.append((depth + 1, "%s %s" % (TODO_MARKS.get(t["status"], "o"), t["content"])))

    for repo in tree:
        out.append((0, "repo " + repo["repo"]))
        if repo["pm"]:
            agent(repo["pm"], 1)
        for job in repo["jobs"]:
            if job["phase"] in DONE_PHASES:
                continue
            out.append((1, "job %s: %s%s" % (job["job"], job["phase"],
                                             ", waiting for you (%s)" % job["pending"] if job["pending"] else "")))
            for n in job["agents"]:
                agent(n, 2)
    return out or [(0, "no OKe agents yet")]


def cmd_board(args):
    """The board for hosts that cannot draw the mod's pane (Orca chat, SDK hosts)."""
    lines = board_lines(board_tree(load_state(), list_agents()))
    if not args.html:
        print("\n".join("  " * d + t for d, t in lines))
        return 0
    import html
    items = "".join('<li style="margin-left:%dem">%s</li>' % (d * 1.5, html.escape(t)) for d, t in lines)
    print('<div style="font-family:monospace"><h3>OKe board</h3><ul style="list-style:none;padding:0">%s</ul></div>'
          % items)
    return 0


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
        out["folder"] = agent_folder(role, name, key, record)
        # the orchestrator writes only in OKe's home outside the wikis, and its own memory
        out["home"] = oke_home()
        config = load_config()
        out["wiki"], out["theme"] = config["wiki_path"], config["theme"]
        out["claude_dir"] = os.path.abspath(native_path(claude_config_dir()))
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
    """The assignment seq the report in `folder` answers, or None while it owes one."""
    seq = (read_header(os.path.join(folder, "assignment.md")) or {}).get("seq")
    report = read_header(os.path.join(folder, "report.md")) or {}
    return seq if seq is not None and report.get("seq") == seq else None


def cmd_boundary(args):
    """The assignments this PM, worker or reviewer has answered, as `job:seq`. A new
    one since the mod last looked is a task boundary, where the mod compacts."""
    state = load_state()
    me, _ = whoami(state, args.pane)
    out = {"role": None, "answered": []}
    if me is not None:
        role, name, key, record = me
        out["role"] = role
        if role == "pm":
            jobs = sorted(n for n, j in state["jobs"].items() if j.get("repo") == key)
            channels = [(job, pm_job_dir(job)) for job in jobs]
        elif role in ("worker", "reviewer") and record.get("job"):
            channels = [(record["job"], agent_folder(role, name, key, record))]
        else:
            channels = []
        for job, folder in channels:
            seq = answered(folder)
            if seq is not None:
                out["answered"].append("%s:%s" % (job, seq))
    print(json.dumps(out))
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
    herdr_info = None
    if runtime().inside():
        herdr_info = (runtime().missing(), list_agents())
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
              "orchestrator_conflict": None, "repos": {}, "jobs": {}, "orphan_job_dirs": []}
    jobs_dir = os.path.join(home, "jobs")
    if os.path.isdir(jobs_dir):
        result["orphan_job_dirs"] = sorted(d for d in os.listdir(jobs_dir) if d not in state["jobs"])
    if not result["herdr"]["inside"]:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    result["herdr"]["missing"], agents = herdr_info
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
                                "pm_job_dir": pm_job_dir(name).replace("\\", "/"),
                                "size": job.get("size"), "direct": bool(job.get("direct"))}

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
    repos = state.get("repos") or {}
    keys = set(repos) | set(j.get("repo") for j in (state.get("jobs") or {}).values() if j.get("repo"))
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

def cmd_job(args):
    if args.action == "merge":
        return merge_job(args.slug)  # git can take a while: not under the state lock
    if args.action == "tree":
        info = load_state()["jobs"].get(args.slug)
        if not info:
            return fail("unknown job: %s" % args.slug)
        tree = pending_tree(native_path(os.path.dirname(info["repo"])))
        if not tree:
            return fail("git could not read the main checkout of %s" % args.slug)
        print(json.dumps({"job": args.slug, "tree": tree}))
        return 0
    if args.action == "cancel":
        with locked():
            code = job_locked(args)
        if code:
            return code
        return cancel_children(args.slug, load_state()["jobs"][args.slug].get("repo"))
    if args.action == "verify":
        if bool(args.command) == bool(args.accept):
            return fail("give the 검증 명령, or --accept \"<why>\" when the user accepts the merge as it is")
        return verify_job(args.slug, args.command, args.accept)  # can take hours
    key = None
    if args.action == "archive":
        key = (load_state()["jobs"].get(args.slug) or {}).get("repo")
    with locked():
        code = job_locked(args)
    if code == 0 and key:
        # a PM can forget to close a finished job's worker (seen live): archive does it,
        # outside the state lock. Best effort: the archive already happened, so its exit
        # code and printed lines never depend on this (flushed first, in case it hangs).
        sys.stdout.flush()
        try:
            _, left, refused = close_children(args.slug, key)
            left = left + refused
        except Exception:  # noqa: BLE001 - cleanup only
            left = None
        if left:
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


def merge_job(job):
    """Merge oke/<job> into the main checkout, only if the user approved the merge and
    the last review passed on the very commit the branch points at. A worker that
    reported again (a rebase, a fix) needs a new review first (seen live: a PM skipped it).
    The main checkout must be clean, on the job's base branch and not mid-merge: the
    abort after a conflict must only ever undo OKe's own merge."""
    state = load_state()
    info = state["jobs"].get(job)
    if not info:
        return fail("unknown job: %s" % job)
    if info.get("size") == "research" or info.get("direct"):
        return fail("job %s: %s" % (job, NO_MERGE))
    if info.get("phase") != "merging":
        return fail("job %s is in phase %s: set phase merging after the user approves the merge"
                    % (job, info.get("phase")))
    already = merge_state(job)[0]
    if already in ("unverified", "verified"):
        return fail("job %s is already merged (%s): run `oke job verify %s`, not a second merge"
                    % (job, already, job))
    # every job of this repository, a failed or cancelled one too: its merge is still in main
    for other in [n for n, j in state["jobs"].items() if j.get("repo") == info["repo"]]:
        if other != job and merge_state(other)[0] == "unverified":
            return fail("the merge of %s is not verified yet: run `oke job verify %s` first"
                        % (other, other))
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
    main = native_path(os.path.dirname(info["repo"]))
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
    if git_out(main, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0:
        return fail("a merge is already in progress in %s: leave it to the user" % main)
    code, dirty = git_out(main, "status", "--porcelain")
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


# Tracked changes always go in; a new file only when the PM staged it (`git add`): only
# the PM can tell the job's new files from what its checks left (seen live: `add -A`
# committed __pycache__).
ADD = ["add", "-u"]


def pending_tree(main):
    """Tree sha of the main checkout as `oke job merge` would commit it: what the PM
    staged plus every tracked change. The real index is untouched. The review of a job without a worktree names this tree."""
    idx = os.path.join(oke_home(), "tree-%d.index" % os.getpid())
    env = dict(os.environ, GIT_INDEX_FILE=idx)
    real = os.path.join(main, git_out(main, "rev-parse", "--git-path", "index")[1])
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
    code, dirty = git_out(main, "status", "--porcelain")
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
        return fail("the changes in %s moved during the commit: run `oke job merge %s` again" % (main, job))
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


def ledger_add(job, by, commit, check, result, detail=""):
    """Append one row. Rows are never rewritten: a later row supersedes an earlier one."""
    values = [time.strftime("%Y-%m-%dT%H:%M:%S"), by, commit, check, result, detail]
    line = "\t".join(" ".join(str(v or "-").split()) for v in values)
    path = ledger_path(job)
    try:
        with io.open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            torn = f.read(1) != b"\n"  # a writer died mid-line: do not glue this row onto it
    except (IOError, OSError):
        torn = False
    with io.open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write((u"\n" if torn else u"") + line + u"\n")


def ledger_rows(job):
    try:
        with io.open(ledger_path(job), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (IOError, OSError, UnicodeDecodeError):
        return []
    return [dict(zip(LEDGER_FIELDS, l.split("\t"))) for l in lines if l.count("\t") == 5]


# Rows only OKe itself writes; `oke ledger add` refuses them so nobody forges a verdict.
OKE_CHECKS = ("review", "merge", "verify", "rollback")


def merge_state(job):
    """(state, last merge row): state is none, unverified, verified or rolled_back."""
    rows = ledger_rows(job)
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
    """bash for the plan's 검증 명령. On Windows only Git Bash: the `bash` on PATH may be
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


VERIFY_LOCK_TOUCH = 60  # seconds: keeps the lock fresh during a check of any length


def run_check(command, cwd, log, lock):
    """Exit code of `command` in bash (else the platform shell), output to `log`. No time
    limit; the lock's mtime is refreshed so it never looks stale while the check runs."""
    bash = find_bash()
    argv = [bash, "-c", command] if bash else command
    with io.open(log, "wb") as f:
        p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=f,
                             stderr=subprocess.STDOUT, shell=bash is None)
        while True:
            try:
                return p.wait(timeout=VERIFY_LOCK_TOUCH)
            except subprocess.TimeoutExpired:
                try:
                    os.utime(lock_path(lock), None)
                except OSError:
                    pass


def verify_job(job, command, accept=None):
    """Run the 검증 명령 on the merged main checkout. A failure undoes the merge, but never
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
        return verify_locked(job, command, accept, info, lock)
    finally:
        unlock(lock)


def verify_locked(job, command, accept, info, lock):
    status, row = merge_state(job)
    if status == "verified":
        print(json.dumps({"result": "verified", "job": job, "commit": row["commit"], "already": True}))
        return 0
    if status != "unverified":
        return fail("job %s has no merge to verify (%s): run `oke job merge %s` first" % (job, status, job))
    if accept:
        # the user's call, e.g. after the main branch moved on: recorded as such
        ledger_add(job, "user", row["commit"], "verify", "pass", "accepted: " + accept)
        print(json.dumps({"result": "verified", "job": job, "commit": row["commit"], "accepted": True}))
        return 0
    main = native_path(os.path.dirname(info["repo"]))
    if git_out(main, "rev-parse", "HEAD")[1] != row["commit"]:
        return fail("the main checkout moved past the merge of %s (%s): ask the user; if they accept "
                    "the merge as it is, `oke job verify %s --accept \"<why>\"`" % (job, row["commit"][:12], job))
    n = len([r for r in ledger_rows(job) if r["check"] == "verify"]) + 1
    log = os.path.join(oke_home(), "jobs", job, "verify-%d.log" % n)
    code = run_check(command, main, log, lock)
    with io.open(log, encoding="utf-8", errors="replace") as f:
        tail = f.read().splitlines()[-40:]
    log = log.replace("\\", "/")
    if code == 0:
        ledger_add(job, "oke", row["commit"], "verify", "pass", command)
        print(json.dumps({"result": "verified", "job": job, "commit": row["commit"], "log": log}))
        return 0
    if code in (126, 127):  # the shell could not run the command: not a verdict on the merge
        return fail("the 검증 명령 could not run (exit %d, see %s): fix the command; the merge stays"
                    % (code, log))
    ledger_add(job, "oke", row["commit"], "verify", "fail", "exit %d: %s" % (code, command))
    words = row["detail"].split()
    pre = words[0][len("pre="):] if words and words[0].startswith("pre=") else None
    # a direct commit (no worktree) goes back to uncommitted changes for the worker to fix
    mode = "--mixed" if "direct" in words else "--keep"
    head = git_out(main, "rev-parse", "HEAD")[1]
    if not pre or head != row["commit"] or subprocess.call(
            ["git", "-C", main, "reset", "-q", mode, pre],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        return fail("the 검증 명령 failed on the merge of %s (log %s), and the merge was NOT undone: "
                    "the main checkout moved on or has local edits in the merged files. Ask the user."
                    % (job, log))
    ledger_add(job, "oke", pre, "rollback", "ok", "after verify fail")
    print(json.dumps({"result": "verify_failed", "job": job, "exit": code, "log": log,
                      "rolled_back_to": pre, "uncommitted": mode == "--mixed", "tail": tail}))
    return 3


def cmd_ledger(args):
    if args.action == "add":
        if not os.path.isdir(os.path.dirname(ledger_path(args.job))):
            return fail("unknown job: %s" % args.job)
        if args.check in OKE_CHECKS:
            return fail("check %r is written by OKe itself; use a name like worker-verify" % args.check)
        ledger_add(args.job, args.by, args.commit, args.check, args.result, args.detail)
        return 0
    status, _ = merge_state(args.job)
    print(json.dumps({"job": args.job, "merge": status, "rows": ledger_rows(args.job)}, indent=2))
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
# research: the PM looks and reports, no change, no plan approval, no merge.
# A direct job (a folder outside git) changes the files in place: no branch, no merge.
NO_MERGE = "the job has nothing to merge (research or direct): set phase done after its report"

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
        return ("job %s has no finished interview: run `oke interview %s --goal .. --constraints .. "
                "--criteria ..` until it says done" % (job, job))
    return None


def cmd_interview(args):
    """One round of the intake interview: record the orchestrator's clarity scores
    (and the question just answered), compute ambiguity, say what to ask next."""
    path = interview_path(args.job)
    if not os.path.isdir(os.path.dirname(path)):
        return fail("unknown job: %s (run `oke job new` first)" % args.job)
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
        direct = not os.path.exists(key)
        busy = [n for n, j in state["jobs"].items()  # research changes nothing: never in the way
                if direct and not args.research and j.get("repo") == key and j.get("size") != "research"
                and j.get("phase") not in DONE_PHASES]
        if busy:
            return fail("job %s already changes %s in place (no git, no undo): finish or archive it first"
                        % (busy[0], args.repo))
        branch = "refs/heads/oke/" + args.slug
        if not direct and subprocess.call(["git", "-C", native_path(args.repo), "show-ref", "--verify",
                                           "--quiet", branch]) == 0:
            return fail("branch oke/%s already exists (an earlier job): pick another job name" % args.slug)
        os.makedirs(job_dir)
        # the branch the main checkout is on now: `job merge` merges only into it
        base = None if direct else git_out(native_path(os.path.dirname(key)), "symbolic-ref", "-q", "--short", "HEAD")[1]
        state["jobs"][args.slug] = {"repo": key, "phase": "intake", "worktree": None,
                                    "phase_at": time.time(), "created": time.time(), "base": base or None}
        if args.research:
            state["jobs"][args.slug]["size"] = "research"
        if direct:
            state["jobs"][args.slug]["direct"] = True
        save_state(state)
        print(json.dumps({"job_dir": job_dir.replace("\\", "/"), "repo": key,
                          "size": "research" if args.research else None, "direct": direct,
                          "pm_job_dir": pm_job_dir(args.slug).replace("\\", "/"),
                          "pm_dir": pm_dir(key).replace("\\", "/"),
                          "pm_agent": "pm-" + repo_name(key),
                          "wiki": wiki_dir(key).replace("\\", "/"),
                          "shared_wiki": os.path.join(load_config()["wiki_path"], "_shared").replace("\\", "/")},
                         ensure_ascii=False))
        return 0
    if args.slug not in state["jobs"]:
        return fail("unknown job: %s" % args.slug)
    if args.action == "phase":
        if args.phase not in PHASES:
            return fail("bad phase %r: one of %s" % (args.phase, ", ".join(PHASES)))
        now = state["jobs"][args.slug].get("phase")
        allowed = PHASE_FROM.get(args.phase)
        info = state["jobs"][args.slug]
        no_merge = info.get("size") == "research" or info.get("direct")
        if args.phase == "merging" and info.get("size") == "small":
            # the user approved the plan and the merge in one go
            allowed = allowed + ("working", "reviewing")
        if no_merge and args.phase in ("awaiting_merge", "merging"):
            return fail("job %s: %s" % (args.slug, NO_MERGE))
        if no_merge and args.phase == "done":
            allowed = allowed + ("working", "reviewing")
        if info.get("size") == "research" and args.phase == "working":
            allowed = allowed + ("intake", "planning")  # no plan to approve
        if allowed is not None and now not in allowed:
            return fail("job %s cannot go from %s to %s: allowed only from %s"
                        % (args.slug, now, args.phase, ", ".join(allowed)))
        if args.phase == "done" and merge_state(args.slug)[0] in ("unverified", "rolled_back"):
            # a job merged without a worktree has no ledger merge row: only OKe's merges are held
            return fail("job %s: its merge is %s, run `oke job verify` (or merge again) first"
                        % (args.slug, merge_state(args.slug)[0]))
        if args.phase == "done" and info.get("repo") and not wiki.logged(wiki_dir(info["repo"]), args.slug,
                                                                         info.get("created")):
            return fail("job %s: the PM has not recorded what it taught. It writes the lessons with "
                        "`oke wiki write --job %s`, or `oke wiki log --job %s --note none`"
                        % (args.slug, args.slug, args.slug))
        if args.phase in ("planning", "working") and now == "intake":  # a re-plan needs no new interview
            problem = interview_problem(args.slug)
            if problem:
                return fail(problem)
        state["jobs"][args.slug].update(phase=args.phase, phase_at=time.time())
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
    if args.action == "size":
        info = state["jobs"][args.slug]
        if info.get("size") == "research":
            return fail("job %s is research: it has no size" % args.slug)
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
    if args.action == "cancel":
        info = state["jobs"][args.slug]
        merged = merge_state(args.slug)[0]
        if info.get("phase") == "done" or merged in ("unverified", "verified"):
            return fail("job %s is %s: its merge is in main or under way. Let it finish (verify, or "
                        "roll it back), not cancel" % (args.slug, info.get("phase") if merged == "none" else
                                                       "merged (%s)" % merged))
        pm = (state["repos"].get(info.get("repo")) or {}).get("pm") or {}
        if pm.get("agent"):
            # queued before the phase changes: the PM's wait delivers it before it can say no_jobs
            queue_mail(pm["agent"], "oke", "Job %s was cancelled by the user. Forget it, close what is "
                       "left of it, and write no report for it." % args.slug)
        info.update(phase="failed", phase_at=time.time(), cancelled=True)
        save_state(state)  # nothing may start for this job from here on
        if pm.get("agent") and not open_jobs(state, info.get("repo")):
            clear_hold(pm["agent"])
        return 0
    if args.action == "archive":
        src = os.path.join(home, "jobs", args.slug)
        if state["jobs"][args.slug].get("repo"):
            drop_job_mail(args.slug, state["jobs"][args.slug]["repo"])
        dst = os.path.join(home, "archive", "%s-%s" % (args.slug, time.strftime("%Y%m%d-%H%M%S")))
        key = state["jobs"][args.slug].get("repo")
        hint = record_wiki_use(src, key) if key and os.path.isdir(src) else None
        if os.path.isdir(src):
            if not os.path.isdir(os.path.dirname(dst)):
                os.makedirs(os.path.dirname(dst))
            shutil.move(src, dst)
        del state["jobs"][args.slug]
        save_state(state)
        print(dst)
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


def drop_job_mail(job, key):
    """Move the queued messages about `job` out of its PM's and children's inboxes, so a
    later agent never acts on a closed job (OMC deletes a finished team's mailboxes)."""
    names = ["pm-" + repo_name(key)] + [n for n, r in read_children(key).items() if r.get("job") == job]
    mark = "Job %s:" % job
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
    repository's if it has one by that name, else the shared wiki's. Says when the
    repository's wiki is due for a curation."""
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
        return ("curate: the wiki of %s took %d jobs since its last curation: give its PM `kind: curate`"
                % (os.path.dirname(key), len(jobs)))
    return None


def wiki_verified(job, info):
    """Lessons count only from work that was checked (Hermes: learning from unchecked
    sessions taught wrong rules)."""
    if info.get("size") == "research":
        return True
    if info.get("direct"):
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
        where = wiki_dir(state["jobs"][args.job]["repo"])
    elif args.repo:
        try:
            where = wiki_dir(repo_key(args.repo))
        except ValueError as e:
            return fail(str(e))
    else:
        return fail("name the wiki: --job <job>, --repo <path> or --shared")
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
                return fail("job %s is not verified yet (merge verified, a direct job's review passed, or "
                            "research): write its lessons after that" % args.job)
            with io.open(args.source, encoding="utf-8-sig") as f:
                text = f.read()
            if args.shared:
                low = text.replace("\\", "/").lower()
                keys = set(state["repos"]) | set(j.get("repo") for j in state["jobs"].values() if j.get("repo"))
                for key in keys:
                    checkout = os.path.dirname(key).lower()
                    bash = re.sub(r"^([a-z]):/", r"/\1/", checkout)  # c:/x as Git Bash writes it: /c/x
                    if len(checkout) > 3 and (checkout in low or bash in low):
                        return fail("the shared wiki holds what is true beyond one repository: drop the "
                                    "path %s" % os.path.dirname(key))
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
        text = "OKe inbox: " + " | ".join(t for _, t in msgs)
        took = runtime().prompt(agent, text)
        if not took:
            return False
        done = os.path.join(inbox_dir(agent), "read")
        if not os.path.isdir(done):
            os.makedirs(done, exist_ok=True)
        # shortcut: with ask_caller the mail counts as read once handed out; a caller
        # compacted before its SendMessage loses it (the file stays in read/)
        for path, _ in msgs:
            replace_file(path, os.path.join(done, os.path.basename(path)))
        return text if took == "ask_caller" else True
    finally:
        unlock(lock)


# ---------------------------------------------------------------- pending

PENDING_RANKS = {"legacy": 0, "unsent": 0, "report_done": 0, "silent": 1, "question": 1, "blocked": 1, "failed": 1, "plan": 2, "merge": 3}
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
    if seq is None and phase in ("planning", "working", "reviewing", "merging"):
        kind, since, mark = "legacy", job.get("phase_at"), phase  # started before slice 3
    elif seq is not None and job.get("sent_seq") != seq:
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
    new.add_argument("--repo", required=True, help="a git checkout, or any folder for a direct job")
    new.add_argument("--research", action="store_true", help="look and report: no change, no merge")
    phase = job_sub.add_parser("phase")
    phase.add_argument("slug")
    phase.add_argument("phase")
    job_sub.add_parser("cancel", help="the user gave the job up: close it, its children and its mail").add_argument("slug")
    archive = job_sub.add_parser("archive")
    archive.add_argument("slug")
    defer = job_sub.add_parser("defer")
    defer.add_argument("slug")
    rounds = job_sub.add_parser("rounds", help="show a job's review round and limit, or set the limit")
    rounds.add_argument("slug")
    rounds.add_argument("n", nargs="?", type=int)
    size = job_sub.add_parser("size", help="the job's size, which the PM decides while planning")
    size.add_argument("slug")
    size.add_argument("size", choices=SIZES)
    merge = job_sub.add_parser("merge")
    merge.add_argument("slug")
    job_sub.add_parser("tree", help="the tree a review of a job without a worktree names").add_argument("slug")
    verify = job_sub.add_parser("verify")
    verify.add_argument("slug")
    verify.add_argument("command", nargs="?", help="the plan's 검증 명령, as one quoted string")
    verify.add_argument("--accept", help="the user accepts the merge without (another) check: why")
    job.set_defaults(func=cmd_job)

    wk = sub.add_parser("wiki", help="read and write a wiki: capped pages, read before write, a log line per job")
    wk_sub = wk.add_subparsers(dest="action")
    wk_sub.required = True
    for name in ("read", "write", "log", "status", "prune", "archive", "pin", "unpin"):
        p = wk_sub.add_parser(name)
        if name in ("read", "write", "archive", "pin", "unpin"):
            p.add_argument("page")
        p.add_argument("--job", help="the job's repository wiki; with write and log, the job the lines are for")
        p.add_argument("--repo", help="the wiki of this repository (curation)")
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

    for name, helptext in (("agents", "names of the running agents"),
                           ("read", "the agent's screen"), ("keys", "press keys in the agent"),
                           ("attach", "show the agent to the user"), ("close", "close a place"),
                           ("place", "a place for a new PM in a repository"),
                           ("worktree", "create or remove a job's worktree")):
        p = sub.add_parser(name, help=helptext)
        p.set_defaults(func=cmd_rt, rt=name)
        if name in ("read", "keys", "attach"):
            p.add_argument("agent")
        if name == "keys":
            p.add_argument("keys", nargs="+")
        if name == "close":
            p.add_argument("place")
        if name == "place":
            p.add_argument("repo", help="the repository's path")
        if name == "worktree":
            p.add_argument("job", nargs="?")
            p.add_argument("--repo")
            p.add_argument("--remove", metavar="ID")

    board = sub.add_parser("board", help="the board as text or HTML, for hosts without the mod pane")
    board.add_argument("--html", action="store_true")
    board.set_defaults(func=cmd_board)

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
