"""bench/teardown.py <run_dir> [--keep-run-dir]

Stops ONLY the herdr session `oke-bench` (never the default session), prunes the bench repos'
worktrees, removes the ~/.herdr/worktrees/<repo>-* folders whose worktrees belong to the bench
repos under <run_dir>, then removes <run_dir> (unless --keep-run-dir). Nothing else is touched.
"""
import argparse
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import time

SESSION = "oke-bench"
REPOS = ("calc", "conv", "shop", "notes")


RUNNING = re.compile(r"status:\s*running")


def herdr_env():
    """This process's env without HERDR_*: an inherited socket must not point `--session` elsewhere."""
    return dict((k, v) for k, v in os.environ.items() if not k.upper().startswith("HERDR_"))


def herdr(*args, timeout=30, session=True):
    try:
        p = subprocess.run(["herdr"] + (["--session", SESSION] if session else []) + list(args),
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, env=herdr_env())
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def norm(p):
    return os.path.normcase(os.path.abspath(p.replace("/", os.sep))).rstrip("\\/")


def under(path, root):
    path, root = norm(path), norm(root)
    return path == root or path.startswith(root + os.sep)


def onerror(func, path, _exc):
    try:
        os.chmod(path, stat.S_IWRITE)  # git objects are read-only on Windows
        func(path)
    except OSError:
        pass


def rmtree(path):
    for _ in range(5):
        shutil.rmtree(path, onerror=onerror)
        if not os.path.exists(path):
            return True
        time.sleep(2)  # a process that just died may still hold a handle
    return False


def stop_session():
    code, out, _ = herdr("status", "server")
    if not RUNNING.search(out):
        print("teardown: session %s is not running" % SESSION)
    else:
        code, out, err = herdr("server", "stop", timeout=60)
        print("teardown: herdr --session %s server stop -> %s %s" % (SESSION, code, (out + err).strip()[:200]))
        for _ in range(30):
            if not RUNNING.search(herdr("status", "server")[1]):
                break
            time.sleep(1)
    # the stopped session's saved layout would come back on the next run
    code, out, err = herdr("session", "delete", SESSION, session=False)
    print("teardown: herdr session delete %s -> %s %s" % (SESSION, code, err.strip()[:200]))


def bench_worktree_dirs(run_dir):
    """~/.herdr/worktrees/<repo>-<hash> folders whose checkouts are worktrees of a bench repo."""
    root = os.path.join(os.path.expanduser("~"), ".herdr", "worktrees")
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        if not any(name.startswith(r + "-") for r in REPOS):
            continue
        group = os.path.join(root, name)
        owned, foreign = 0, 0
        for sub in os.listdir(group) if os.path.isdir(group) else []:
            gitfile = os.path.join(group, sub, ".git")
            try:
                with io.open(gitfile, encoding="utf-8") as f:
                    line = f.read().strip()
            except (IOError, OSError, UnicodeDecodeError):
                foreign += 1  # unreadable: maybe not ours, so the folder stays
                continue
            target = line[len("gitdir:"):].strip() if line.startswith("gitdir:") else ""
            if target and under(target, run_dir):
                owned += 1
            else:
                foreign += 1
        if owned and not foreign:
            out.append(group)
        elif foreign:
            print("teardown: leaving %s: it holds worktrees of other repos" % group)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--keep-run-dir", action="store_true")
    args = ap.parse_args()
    run_dir = os.path.abspath(args.run_dir)
    if not os.path.isfile(os.path.join(run_dir, "bench-run.json")):
        print("teardown: %s has no bench-run.json: not a bench run dir, nothing removed" % run_dir)
        stop_session()
        return 1
    if norm(run_dir) in (norm(os.path.expanduser("~")), norm(os.path.join(os.path.expanduser("~"), ".oke"))):
        print("teardown: refusing to remove %s" % run_dir)
        return 1
    stop_session()
    groups = bench_worktree_dirs(run_dir)
    for group in groups:
        print("teardown: removing %s: %s" % (group, "ok" if rmtree(group) else "FAILED (still there)"))
    for r in REPOS:
        repo = os.path.join(run_dir, r)
        if os.path.isdir(os.path.join(repo, ".git")):
            subprocess.run(["git", "-C", repo, "worktree", "prune"], timeout=60)
    if args.keep_run_dir:
        print("teardown: kept %s" % run_dir)
        return 0
    ok = rmtree(run_dir)
    print("teardown: removing %s: %s" % (run_dir, "ok" if ok else "FAILED (still there)"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
