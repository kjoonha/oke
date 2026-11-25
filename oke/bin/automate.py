"""Scheduled Claude Code sessions: `oke automate add|list|remove|run`.

An automation is a folder $OKE_HOME/automations/<name>/ with automation.json (schedule,
working folder, launch options), prompt.md (the prompt each run starts with) and
run.log (one line per run; scheduler.log catches anything else a run prints).
The OS scheduler (crontab, or Task Scheduler on Windows)
calls `oke.py automate run <folder> --scheduled`, which starts `claude "<prompt>" --bg`
in the working folder. A run is skipped while the previous run of the same automation
still works or waits at a dialog.
"""
import io
import json
import locale
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

import runtime_bg
from runtime_herdr import HerdrError, write_private

NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
MARK = "# oke-automate:"          # crontab line suffix that names the automation
TASK_FOLDER = "OKe"               # Task Scheduler folder
MODES = ("manual", "acceptEdits", "auto", "dontAsk", "bypassPermissions")  # none: the user's own setting
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SCHEDULER_TIMEOUT = 60  # seconds
# cron starts with PATH=/usr/bin:/bin and no locale: a run gets the ones `add` saw, so an
# npm claude finds node and the session's tools find what the user's shell finds.
# Task Scheduler starts with the user's own environment.
SAVED_ENV = ("PATH", "LANG", "LC_ALL", "LC_CTYPE")
# schtasks /Create makes a task that never starts on battery and stops when the power
# cable comes out; this turns both off.
ON_BATTERY = ("$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
              "Set-ScheduledTask -TaskPath '\\%s\\' -TaskName '%s' -Settings $s | Out-Null")


class AutomateError(Exception):
    pass


def windows():
    return os.name == "nt"


# ---------------------------------------------------------------- schedule

def parse_schedule(text):
    """The schedule a user gave as one string, as a dict, or AutomateError:
    `every 30m`, `every 2h`, `daily 09:00`, `weekdays 09:00`, `weekly mon,thu 09:00`,
    or `cron <five fields>` (not on Windows)."""
    words = str(text or "").strip().split()
    if not words:
        raise AutomateError("give a schedule")
    kind = words[0].lower()
    if kind == "cron":
        if windows():
            raise AutomateError("cron expressions need crontab: on Windows use every, daily, weekdays or weekly")
        if len(words) != 6:
            raise AutomateError("cron needs five fields: minute hour day month weekday")
        return {"kind": "cron", "expr": " ".join(words[1:])}
    if kind == "every" and len(words) == 2:
        m = re.match(r"^(\d+)([mh])$", words[1].lower())
        if not m:
            raise AutomateError("every needs minutes or hours: every 30m, every 2h")
        n, unit = int(m.group(1)), m.group(2)
        if unit == "m" and not (1 <= n < 60 and 60 % n == 0):
            raise AutomateError("every Nm needs N that divides 60 (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30)")
        if unit == "h" and not (1 <= n < 24 and 24 % n == 0):
            raise AutomateError("every Nh needs N that divides 24 (1, 2, 3, 4, 6, 8, 12)")
        return {"kind": "every", "n": n, "unit": unit}
    if kind in ("daily", "weekdays") and len(words) == 2:
        return {"kind": kind, "time": clock(words[1])}
    if kind == "weekly" and len(words) == 3:
        days = [d.strip().lower()[:3] for d in words[1].split(",") if d.strip()]
        if not days or any(d not in DAYS for d in days):
            raise AutomateError("weekly needs days like mon,thu")
        days = sorted(set(days), key=DAYS.index)
        return {"kind": "weekly", "days": days, "time": clock(words[2])}
    raise AutomateError("unknown schedule %r: use every 30m, every 2h, daily 09:00, weekdays 09:00, "
                        "weekly mon,thu 09:00 or cron <five fields>" % text)


def clock(text):
    m = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise AutomateError("a time is HH:MM (24 hours): %r" % text)
    return "%02d:%02d" % (int(m.group(1)), int(m.group(2)))


def describe(s):
    if s["kind"] == "cron":
        return "cron " + s["expr"]
    if s["kind"] == "every":
        return "every %d%s" % (s["n"], s["unit"])
    if s["kind"] == "weekly":
        return "weekly %s %s" % (",".join(s["days"]), s["time"])
    return "%s %s" % (s["kind"], s["time"])


def cron_fields(s):
    """The five crontab fields of a schedule (local time)."""
    if s["kind"] == "cron":
        return s["expr"]
    if s["kind"] == "every":
        return ("*/%d * * * *" % s["n"]) if s["unit"] == "m" else ("0 */%d * * *" % s["n"])
    hour, minute = [int(x) for x in s["time"].split(":")]
    dow = {"daily": "*", "weekdays": "1-5"}.get(s["kind"])
    if dow is None:
        dow = ",".join(str(DAYS.index(d) + 1) for d in s["days"])  # cron: 1 = monday, 7 = sunday
    return "%d %d * * %s" % (minute, hour, dow)


def schtasks_args(s):
    """The /SC part of `schtasks /Create` for a schedule (local time)."""
    if s["kind"] == "every":  # from 00:00, so runs fall on the same times as cron's
        return ["/SC", "MINUTE" if s["unit"] == "m" else "HOURLY", "/MO", str(s["n"]), "/ST", "00:00"]
    if s["kind"] == "daily":
        return ["/SC", "DAILY", "/ST", s["time"]]
    days = DAYS[:5] if s["kind"] == "weekdays" else s["days"]
    return ["/SC", "WEEKLY", "/D", ",".join(d.upper() for d in days), "/ST", s["time"]]


# ---------------------------------------------------------------- scheduler

def run_cmd(argv, stdin=None):
    """(exit code, stdout, stderr) of one scheduler command; replaced in tests."""
    try:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        raise AutomateError("%s: %s" % (argv[0], e))
    try:
        out, err = p.communicate((stdin or "").encode("utf-8"), timeout=SCHEDULER_TIMEOUT)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise AutomateError("%s did not answer in %d s" % (argv[0], SCHEDULER_TIMEOUT))
    enc = locale.getpreferredencoding(False) if windows() else "utf-8"  # schtasks speaks the code page
    dec = lambda b: b.decode(enc, "replace").replace("\r\n", "\n")
    return p.returncode, dec(out), dec(err)


def crontab_lines():
    code, out, err = run_cmd(["crontab", "-l"])
    if code != 0:
        if "no crontab" in (err + out).lower():
            return []
        raise AutomateError("crontab -l: %s" % (err.strip() or "exit %d" % code))
    return out.splitlines()


def write_crontab(lines):
    text = "\n".join(lines) + "\n" if lines else ""
    code, out, err = run_cmd(["crontab", "-"], stdin=text)
    if code != 0:
        raise AutomateError("crontab -: %s" % (err.strip() or "exit %d" % code))


def is_mine(line, name):
    """A live crontab line of automation `name` (a line commented out is not)."""
    line = line.strip()
    return not line.startswith("#") and line.endswith(MARK + name)


def oke_script():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "oke.py")


def cron_line(s, folder):
    def q(x):
        return shlex.quote(x).replace("%", "\\%")  # cron turns a bare % into a newline
    command = "%s %s automate run %s --scheduled >> %s 2>&1" % (
        q(sys.executable), q(oke_script()), q(folder), q(os.path.join(folder, "scheduler.log")))
    return "%s %s %s%s" % (cron_fields(s), command, MARK, os.path.basename(folder))


def task_name(name):
    return "\\%s\\%s" % (TASK_FOLDER, name)


def run_script(folder):
    """The run.cmd Task Scheduler starts: /TR holds at most 261 characters, so the long
    command lives in this file."""
    path = os.path.join(folder, "run.cmd")
    q = lambda x: '"%s"' % x.replace("%", "%%")  # cmd expands %...% even inside quotes
    body = "@echo off\r\n%s %s automate run %s --scheduled >> %s 2>&1\r\n" % (
        q(sys.executable), q(oke_script()), q(folder), q(os.path.join(folder, "scheduler.log")))
    # shortcut: cmd reads the file in the console code page; a non-ASCII path may break here
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(body)
    return path


def register(name, s, folder):
    """Put the automation into the OS scheduler, replacing an entry of the same name.
    Returns a short description of the entry."""
    if windows():
        script = run_script(folder)
        code, out, err = run_cmd(["schtasks", "/Create", "/F", "/TN", task_name(name),
                                  "/TR", '"%s"' % script] + schtasks_args(s))
        if code != 0:
            raise AutomateError("schtasks /Create: %s" % ((err or out).strip() or "exit %d" % code))
        entry = "Task Scheduler %s" % task_name(name)
        try:
            code = run_cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            ON_BATTERY % (TASK_FOLDER, name)])[0]
        except AutomateError:
            code = 1
        if code != 0:
            entry += " (it runs only on AC power: PowerShell could not change that)"
        return entry
    keep = [l for l in crontab_lines() if not is_mine(l, name)]
    line = cron_line(s, folder)
    write_crontab(keep + [line])
    return "crontab: " + line


def unregister(name):
    """Take the automation out of the OS scheduler. True when there was an entry; an
    entry that stays raises."""
    if windows():
        if run_cmd(["schtasks", "/Query", "/TN", task_name(name)])[0] != 0:
            return False
        code, out, err = run_cmd(["schtasks", "/Delete", "/F", "/TN", task_name(name)])
        if code != 0:
            raise AutomateError("schtasks /Delete: %s" % ((err or out).strip() or "exit %d" % code))
        return True
    lines = crontab_lines()
    keep = [l for l in lines if not is_mine(l, name)]
    if len(keep) == len(lines):
        return False
    write_crontab(keep)
    return True


def registered_names():
    """The automation names the OS scheduler holds, or None when it cannot be read."""
    try:
        if windows():
            code, out, err = run_cmd(["schtasks", "/Query", "/FO", "CSV", "/NH"])
            if code != 0:
                return None
            prefix = "\\%s\\" % TASK_FOLDER
            names = set()
            for line in out.splitlines():
                first = line.split(",")[0].strip('"')
                if first.startswith(prefix):
                    names.add(first[len(prefix):])
            return names
        return set(l.strip().rsplit(MARK, 1)[1] for l in crontab_lines()
                   if MARK in l and not l.strip().startswith("#"))
    except AutomateError:
        return None


# ---------------------------------------------------------------- commands

def automations_dir(home):
    return os.path.join(home, "automations")


def automation_folder(home, name):
    if not NAME.match(name or ""):
        raise AutomateError("bad automation name %r: lowercase letters, digits and -, "
                            "starting with a letter (at most 32)" % name)
    return os.path.join(automations_dir(home), name)


def load(folder):
    path = os.path.join(folder, "automation.json")
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except (IOError, OSError, ValueError) as e:
        raise AutomateError("cannot read %s: %s" % (path, e))


def claude_command():
    """The claude to start, resolved now: the scheduler's PATH is not the user's."""
    argv = runtime_bg.claude_argv()
    found = shutil.which(argv[0])
    if not found:
        raise AutomateError("cannot find %s on PATH" % argv[0])
    return [found] + argv[1:]


def add(home, name, schedule, cwd, prompt, mode=None, model=None):
    folder = automation_folder(home, name)
    s = parse_schedule(schedule)
    if mode is not None and mode not in MODES:
        raise AutomateError("permission mode is one of: %s" % ", ".join(MODES))
    cwd = os.path.abspath(cwd)
    if not os.path.isdir(cwd):
        raise AutomateError("no such folder: %s" % cwd)
    if not (prompt or "").strip():
        raise AutomateError("the prompt is empty")
    if prompt.lstrip().startswith("-"):
        raise AutomateError("a prompt may not start with -: claude would read it as an option")
    info = {"name": name, "schedule": describe(s), "cwd": cwd, "permission_mode": mode,
            "model": model or None, "claude": claude_command(),
            "env": {} if windows() else dict((k, os.environ[k]) for k in SAVED_ENV if k in os.environ),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
    new = not os.path.isdir(folder)
    if new:
        os.makedirs(folder)
    try:
        entry = register(name, s, folder)
    except Exception:
        if new:
            shutil.rmtree(folder, ignore_errors=True)
        raise  # a re-add that failed leaves the old automation as it was
    write_private(os.path.join(folder, "prompt.md"), prompt.rstrip() + "\n")
    write_private(os.path.join(folder, "automation.json"), json.dumps(info, indent=2, ensure_ascii=False))
    info.pop("env")
    info["entry"] = entry
    info["folder"] = folder
    return info


def listing(home):
    root = automations_dir(home)
    names = sorted(n for n in os.listdir(root) if os.path.isfile(os.path.join(root, n, "automation.json"))) \
        if os.path.isdir(root) else []
    known = registered_names()
    out = []
    for n in names:
        folder = os.path.join(root, n)
        try:
            info = load(folder)
        except AutomateError as e:
            out.append({"name": n, "error": str(e)})
            continue
        info.pop("env", None)
        info["folder"] = folder
        info["registered"] = None if known is None else n in known
        info["last_run"] = last_log_line(folder)
        out.append(info)
    return out


def last_log_line(folder):
    try:
        with io.open(os.path.join(folder, "run.log"), encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f.read().splitlines() if l.strip()]
        return lines[-1] if lines else None
    except (IOError, OSError):
        return None


def remove(home, name):
    folder = automation_folder(home, name)
    was = unregister(name)  # raises when the entry stays: then the folder stays too
    if os.path.isdir(folder):
        shutil.rmtree(folder)
    elif not was:
        raise AutomateError("no automation named %s" % name)
    return {"name": name, "removed": True, "scheduler_entry_removed": was}


def busy_runs(name):
    """Sessions of an earlier run that still work or wait at a dialog."""
    rows = runtime_bg.BgRuntime().rows()
    mine = re.compile(r"^auto-%s-\d{4}-\d{6}$" % re.escape(name))  # not auto-<name>-x-...
    return [r.get("name") for r in rows if mine.match(str(r.get("name") or ""))
            and runtime_bg.status_of(r) in ("working", "blocked")]


def run(folder):
    """One run: start the session, or skip it. Appends one line to run.log and returns
    it; never raises."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    folder = os.path.abspath(folder)
    try:
        info = load(folder)
        name = info["name"]
        with io.open(os.path.join(folder, "prompt.md"), encoding="utf-8") as f:
            prompt = f.read().strip()
        os.environ.update(info.get("env") or {})
        os.environ["OKE_CLAUDE"] = json.dumps(info["claude"])
        busy = busy_runs(name)
        if busy:
            line = "%s skipped: %s is still running (claude attach %s)" % (stamp, busy[0], busy[0])
        else:
            session = "auto-%s-%s" % (name, time.strftime("%m%d-%H%M%S"))
            args = [prompt, "--bg", "--name", session]
            if info.get("permission_mode"):
                args += ["--permission-mode", info["permission_mode"]]
            if info.get("model"):
                args += ["--model", info["model"]]
            # OKe's bg settings: no worktree of its own (it works in the chosen folder)
            runtime_bg.run_claude(runtime_bg.with_bg_settings(args, folder), cwd=info["cwd"])
            line = "%s started %s in %s (claude attach %s)" % (stamp, session, info["cwd"], session)
    except HerdrError as e:
        line = "%s failed: %s" % (stamp, e.message)
    except (AutomateError, IOError, OSError, KeyError, ValueError) as e:
        line = "%s failed: %s" % (stamp, e)
    try:
        if os.path.isdir(folder):
            with io.open(os.path.join(folder, "run.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except (IOError, OSError) as e:
        sys.stderr.write("%s (run.log: %s)\n" % (line, e))  # lands in scheduler.log
    return line


def cmd(args, home, native=lambda p: p):
    """`oke automate ...`: prints JSON (run prints its log line). Returns the exit code.
    `native` turns a Git Bash /c/x path into one Windows takes."""
    try:
        if args.action == "run":
            target = args.target
            if NAME.match(target):
                target = automation_folder(home, target)
            line = run(native(target))
            if not args.scheduled:  # run.log has it; the scheduler's redirect is for crashes
                print(line)
            return 1 if " failed: " in line else 0
        if args.action == "add":
            if args.prompt_file:
                with io.open(native(args.prompt_file), encoding="utf-8-sig") as f:
                    prompt = f.read()
            else:
                prompt = args.prompt or ""
            result = add(home, args.name, args.schedule, native(args.cwd), prompt,
                         args.permission_mode, args.model)
        elif args.action == "list":
            result = listing(home)
        else:
            result = remove(home, args.name)
    except (AutomateError, IOError, OSError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def add_parser(sub, func):
    p = sub.add_parser("automate", help="scheduled Claude Code sessions: crontab or Task Scheduler")
    a = p.add_subparsers(dest="action")
    a.required = True
    add_p = a.add_parser("add", help="schedule a prompt (replaces one of the same name)")
    add_p.add_argument("name")
    add_p.add_argument("--schedule", required=True,
                       help="every 30m | every 2h | daily 09:00 | weekdays 09:00 | weekly mon,thu 09:00 | cron <5 fields>")
    add_p.add_argument("--cwd", required=True, help="the folder claude starts in (one claude already trusts)")
    src = add_p.add_mutually_exclusive_group(required=True)
    src.add_argument("--prompt-file")
    src.add_argument("--prompt")
    add_p.add_argument("--permission-mode", choices=MODES, help="left out: the user's own default")
    add_p.add_argument("--model")
    a.add_parser("list", help="the automations, whether the scheduler holds them, and their last run")
    a.add_parser("remove", help="unschedule an automation and delete its folder").add_argument("name")
    run_p = a.add_parser("run", help="start one run now (what the scheduler calls)")
    run_p.add_argument("target", help="the automation's name or folder")
    run_p.add_argument("--scheduled", action="store_true", help="called by the scheduler: print nothing")
    p.set_defaults(func=func)
