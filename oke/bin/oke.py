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
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return 1 if e.code else 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
