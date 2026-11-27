"""bench/driver.py <run_dir> [options]: drive the OKe orchestrator through bench/tasks.json.

Python 3 stdlib only. Every herdr call goes to the session `oke-bench` (never the default
session) and has a timeout; every wait has a budget. Per task it writes <run_dir>/logs/<task>/:
  before.json / after.json   repo refs, status, worktrees, OKe jobs (score.py compares them)
  popup-NN.txt               each popup screen, its kind and the answer given
  popups.jsonl               one line per popup
  phases.jsonl               job phase changes seen while the task ran
  final.txt, board.txt       the orchestrator screen and `oke board --full` at the end
  cost.json                  `oke cost` at the end (tokens per job, PM, orchestrator)
  result.json                status DONE | TIMEOUT | STUCK | NO_JOB | ERROR, times, jobs
and <run_dir>/logs/results.json for all tasks.

Options:
  --tasks FILE        default bench/tasks.json
  --only IDS          comma separated task ids
  --from ID           start at this task (in order)
  --budget-scale X    multiply every time budget (default 1)
  --dry-run           check tasks and the pane, start nothing (no claude), exit
  --no-start          claude already runs as the orchestrator in the pane: do not start it
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time

BENCH = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BENCH)
OKE_DIR = os.path.join(REPO, "oke")
OKE_PY = os.path.join(OKE_DIR, "bin", "oke.py")
SESSION = "oke-bench"
REPOS = ("calc", "conv", "shop", "notes")
DONE_PHASES = ("done", "failed")
POLL = 3  # seconds between screen reads
HERDR_TIMEOUT = 30

LOG = None


# ---------------------------------------------------------------- small helpers

def log(text):
    line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), text)
    print(line, flush=True)
    if LOG:
        with io.open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)


def read_json(path, default=None):
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return default


def append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)


def run(argv, timeout=60, env=None, cwd=None):
    """(code, stdout, stderr); never raises, never hangs past timeout."""
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, env=env, cwd=cwd)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ss" % timeout
    except OSError as e:
        return 127, "", str(e)


def herdr_env():
    """This process's env without HERDR_*: an inherited socket must not point `--session` elsewhere."""
    return dict((k, v) for k, v in os.environ.items() if not k.upper().startswith("HERDR_"))


def herdr(*args, timeout=HERDR_TIMEOUT):
    return run(["herdr", "--session", SESSION] + list(args), timeout=timeout, env=herdr_env())


# ---------------------------------------------------------------- the free-proxy guard

USER_PROJECTS = os.path.join(os.path.expanduser("~"), ".claude", "projects")
FREE_PROJECTS = os.path.join(os.path.expanduser("~"), ".claude-free", "projects")
GUARD_EVERY = 60  # seconds between two looks for a leaked session


def project_slug(path):
    """The folder name Claude Code gives a cwd under <config dir>/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path.replace("/", os.sep)))


def bench_prefixes(run_dir):
    """Project folder names of every cwd a bench agent has: the run dir and herdr's worktrees of
    the bench repos."""
    wt = project_slug(os.path.join(os.path.expanduser("~"), ".herdr", "worktrees"))
    return [project_slug(run_dir).lower()] + ["%s-%s-" % (wt.lower(), r) for r in REPOS]


def session_files(root, prefixes, since):
    """Session files (*.jsonl, at any depth) under the project folders of `root` whose name starts
    with one of `prefixes`, written at or after `since`."""
    found = []
    try:
        names = os.listdir(root)
    except OSError:
        return found
    for name in names:
        if not any(name.lower().startswith(p) for p in prefixes):
            continue
        for dirpath, _, files in os.walk(os.path.join(root, name)):
            for f in files:
                path = os.path.join(dirpath, f)
                try:
                    if f.endswith(".jsonl") and os.path.getmtime(path) >= since - 1:
                        found.append(path)
                except OSError:
                    pass
    return found


def stop_bench(why):
    """Stop the bench session at once (never another one) and end the run."""
    log("ABORT: %s; stopping only the %s session" % (why, SESSION))
    code, out, err = herdr("server", "stop", timeout=60)
    log("server stop -> %s %s" % (code, (out + err).strip()[:200]))
    raise SystemExit(why)


def herdr_json(*args):
    code, out, err = herdr(*args)
    if code != 0:
        return None
    try:
        return json.loads(out).get("result")
    except ValueError:
        return None


# ---------------------------------------------------------------- the bench's world

class Bench(object):
    def __init__(self, run_dir):
        self.run = os.path.abspath(run_dir).replace("\\", "/")
        self.home = self.run + "/home"
        self.logs = self.run + "/logs"
        info = read_json(os.path.join(self.run, "bench.json"), {}) or {}
        self.pane = info.get("pane")
        self.orch = info.get("orch") or self.run + "/orch"
        try:  # the run started when the launcher wrote bench.json (or now)
            self.started = min(time.time(), os.path.getmtime(os.path.join(self.run, "bench.json")))
        except OSError:
            self.started = time.time()
        self.guarded = 0

    def guard(self, force=False):
        """Every GUARD_EVERY s: a session file of this run under the user's own ~/.claude/projects
        means a claude of the bench runs on the real account. Record LEAK and stop at once."""
        if not force and time.time() - self.guarded < GUARD_EVERY:
            return
        self.guarded = time.time()
        leaked = session_files(USER_PROJECTS, bench_prefixes(self.run), self.started)
        if leaked:
            write_json(os.path.join(self.logs, "LEAK.json"), {"t": time.time(), "files": leaked})
            results = read_json(os.path.join(self.logs, "results.json"), {}) or {}
            results["LEAK"] = leaked
            write_json(os.path.join(self.logs, "results.json"), results)
            stop_bench("LEAK: a bench session wrote %s under the real ~/.claude" % leaked[0])

    def env(self):
        env = dict(os.environ)
        for k in list(env):
            if k.startswith(("HERDR_", "CLAUDE", "OKE_")):
                del env[k]
        env["OKE_HOME"] = self.home
        env["OKE_HERDR"] = json.dumps(["herdr", "--session", SESSION])
        env["PYTHONIOENCODING"] = "utf-8"
        assert env["OKE_HOME"].startswith(self.run), "OKE_HOME must be the bench's"
        return env

    def oke(self, *args, timeout=120):
        return run([sys.executable, OKE_PY] + list(args), timeout=timeout, env=self.env())

    def state(self):
        return read_json(os.path.join(self.home, "state.json"), {}) or {}

    def archived(self):
        """{job: job.json} of archived jobs (the newest folder per job)."""
        root = os.path.join(self.home, "archive")
        out = {}
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return out
        for n in names:
            m = re.match(r"^(.+)-(\d{8}-\d{6})$", n)
            if m:
                info = read_json(os.path.join(root, n, "job.json"), None)
                if isinstance(info, dict):
                    out[m.group(1)] = dict(info, _archive=n)
        return out

    JOB_FIELDS = ("phase", "pm", "path", "size", "output", "base", "worktree", "waiting_for", "continues")

    def all_jobs(self):
        """{job: {phase, held, auto, answer, archived, folder, size, ...}} from state.json and the archive."""
        out = {}
        for n, j in self.archived().items():
            out[n] = dict(((k, j.get(k)) for k in self.JOB_FIELDS), archived=True, auto=bool(j.get("auto")),
                          answer=bool(j.get("answer")), held=bool(j.get("held")),
                          folder="archive/" + j["_archive"])
            out[n]["waiting_for"] = None
        for n, j in (self.state().get("jobs") or {}).items():
            out[n] = dict(((k, j.get(k)) for k in self.JOB_FIELDS), archived=False, auto=bool(j.get("auto")),
                          answer=bool(j.get("answer")), held=bool(j.get("held")), folder="jobs/" + n)
        return out

    def snapshot(self):
        """Repo refs, status and worktrees, plus OKe's jobs: what score.py compares."""
        snap = {"t": time.time(), "repos": {}, "jobs": self.all_jobs()}
        for r in REPOS + ("shop-add-tax",):
            path = os.path.join(self.run, r)
            if not os.path.isdir(path):
                continue
            _, refs, _ = run(["git", "-C", path, "for-each-ref", "--format=%(refname) %(objectname)"])
            _, st, _ = run(["git", "-C", path, "-c", "core.quotepath=off", "status", "--porcelain=v1", "-uall"])
            _, wt, _ = run(["git", "-C", path, "worktree", "list", "--porcelain"])
            _, files, _ = run(["git", "-C", path, "-c", "core.quotepath=off", "ls-files", "-co",
                               "--exclude-standard"])
            contents = {}
            for f in files.splitlines():
                full = os.path.join(path, f)
                try:
                    if os.path.getsize(full) <= 20000:
                        with io.open(full, encoding="utf-8", errors="replace") as fh:
                            contents[f] = fh.read()
                    else:
                        contents[f] = None  # there, too big to keep
                except OSError:
                    pass
            snap["repos"][r] = {
                "refs": dict(l.split(" ", 1) for l in refs.splitlines() if " " in l),
                "status": st.splitlines(),
                "worktrees": [l[len("worktree "):] for l in wt.splitlines() if l.startswith("worktree ")],
                "files": contents,
            }
        return snap

    def panes(self):
        res = herdr_json("pane", "list") or {}
        return res.get("panes") or []

    def pane_status(self, pane=None):
        res = herdr_json("pane", "get", pane or self.pane) or {}
        return (res.get("pane") or {}).get("agent_status") or "unknown"

    def screen(self, pane=None, lines=None, source="visible"):
        args = ["pane", "read", pane or self.pane, "--source", source]
        if lines:
            args += ["--lines", str(lines)]
        code, out, _ = herdr(*args)
        return out if code == 0 else ""

    def keys(self, *keys):
        herdr("pane", "send-keys", self.pane, *keys)

    def text(self, text):
        herdr("pane", "send-text", self.pane, text)


# ---------------------------------------------------------------- popups

OPTION = re.compile(r"^[\s│|]*(❯|›|>)?\s*(\d{1,2})\.\s+(.*?)\s*[│|]?\s*$")
FOOTER = re.compile(r"(?i)(enter to (select|confirm|submit)|to navigate|esc to (cancel|exit)|tab to)")
FREE_TEXT = re.compile(r"(?i)^(type something|other|기타|직접 입력)")
# options that are never a default answer: they leave the question instead of answering it
SPECIAL = re.compile(r"(?i)^(chat about this|skip)")


def parse_popup(screen):
    """{question, options: [(n, label)], cursor, text} of the selection popup at the bottom of
    the screen, or None."""
    lines = screen.splitlines()[-45:]
    found, cursor, first = [], None, None
    for i, line in enumerate(lines):
        m = OPTION.match(line)
        if not m:
            continue
        if line.find(m.group(2) + ".") > 10:
            continue  # a numbered line of the preview on the right, not an option
        n = int(m.group(2))
        label = re.split(r"\s{3,}|│", m.group(3))[0].strip()
        if n == 1:
            found, cursor, first = [], None, i  # a list starts again: keep the last one
        if found and n != found[-1][0] + 1:
            continue
        found.append((n, label))
        if m.group(1):
            cursor = n
    if len(found) < 2 or cursor is None:
        return None
    text = "\n".join(lines)
    if not FOOTER.search(text) and not any(FREE_TEXT.match(l) for _, l in found):
        return None
    question = "\n".join(l.strip(" │|") for l in lines[max(0, first - 12):first] if l.strip(" │|─╭╮╰╯"))
    return {"question": question, "options": found, "cursor": cursor, "text": text}


def classify(p):
    labels = " | ".join(l for _, l in p["options"])
    text = p["text"]
    if re.search(r"(?i)trust", text) and re.search(r"(?i)(folder|files|폴더)", text) and re.search(r"(?i)yes", labels):
        return "trust"
    if re.search(r"(?i)bypass permissions", text):
        return "bypass_warning"
    if re.search(r"(?i)(submit answers|review your answers)", text):
        return "submit"
    if re.search(r"(?i)(enough|start planning|계획(을)? ?(바로 )?(시작|세우)|바로 계획)", labels):
        return "interview"
    if re.search(r"(?i)(new pm|새 pm|새로운 pm)", labels):
        return "pm_pick"
    if re.search(r"(?i)(request changes|수정 요청|변경 요청|고쳐 ?달|바꿔 ?달|수정해)", labels) and \
            re.search(r"(?i)(approve|승인)", labels):
        return "approval"
    if re.search(r"(?i)(as usual|평소|매번|그대로 물|물어봐|ask me)", labels) and \
            re.search(r"(?i)(묻지|확인 없이|without asking|don.t ask|자동)", text):
        return "auto_confirm"
    if re.search(r"(?i)(approve once|한 번 (허용|승인)|이번만|deny|거부)", labels):
        return "dialog"
    if re.search(r"(?i)(approve|승인)", labels):
        return "approval"
    return "question"


def signature(p):
    return (p["question"][-200:], tuple(p["options"]))


class Popups(object):
    def __init__(self, bench, tasks_cfg):
        self.b = bench
        self.global_rules = tasks_cfg.get("global_popups") or []
        self.avoid = re.compile(tasks_cfg.get("avoid_options") or r"(?!)")
        self.used = {}  # (task, rule index) -> times used
        self.count = {}  # (task, kind) -> popups seen
        self.seq = {}  # task -> popups logged (numbering never restarts: no popup file is overwritten)
        self.by_job = {}  # job -> its task, across every group

    def decide(self, task, p, kind):
        rules = [("t%d" % i, r) for i, r in enumerate(task.get("popups") or [])] + \
                [("g%d" % i, r) for i, r in enumerate(self.global_rules)]
        for rid, r in rules:
            if r.get("kind") not in (None, "any", kind):
                continue
            if r.get("when") and not re.search(r["when"], p["text"]):
                continue
            if self.count.get((task["id"], kind), 0) < r.get("after", 0):
                continue
            key = (task["id"], rid)
            if r.get("times") is not None and self.used.get(key, 0) >= r["times"]:
                continue
            self.used[key] = self.used.get(key, 0) + 1
            return r.get("do", "first"), rid
        return "first", None

    def target(self, p, action):
        """(option number, text to type or None, why)."""
        opts = [(n, l) for n, l in p["options"]]
        plain = [(n, l) for n, l in opts if not FREE_TEXT.match(l) and not SPECIAL.match(l)]
        if isinstance(action, dict) and "type" in action:
            free = [n for n, l in opts if FREE_TEXT.match(l)] or [opts[-1][0]]
            return free[0], action["type"], "type"
        if isinstance(action, dict) and "pick" in action:
            for n, l in plain:
                if re.search(action["pick"], l):
                    return n, None, "pick"
            log("popup: no option matches %r; using the first safe one" % action["pick"])
        safe = [(n, l) for n, l in plain if not self.avoid.search(l)] or plain or opts
        return safe[0][0], None, "first"

    def answer(self, task, p, kind, folder, seq):
        """Answer the popup by the policy; log the screen. Returns the record."""
        if kind in ("trust", "bypass_warning"):
            pick = r"(?i)yes" if kind == "trust" else r"(?i)yes,? i accept"
            action, rid = {"pick": pick}, "startup"
        else:
            action, rid = self.decide(task, p, kind)
        n, typed, how = self.target(p, action)
        ok = self.move_to(n)
        if typed is not None and ok:
            self.b.text(typed)
            time.sleep(1.5)
            if typed[:8] not in self.b.screen():
                log("popup: typed text not visible on screen; sending it again")
                self.b.text(typed)
                time.sleep(1)
        if ok:
            self.b.keys("enter")
        rec = {"t": time.time(), "task": task["id"], "kind": kind, "seq": seq, "option": n,
               "label": dict(p["options"]).get(n), "typed": typed, "how": how, "rule": rid, "moved": ok,
               "options": [l for _, l in p["options"]], "question": p["question"][-600:]}
        write_text(os.path.join(folder, "popup-%02d.txt" % seq),
                   "kind: %s\nanswer: %s. %s%s (%s, rule %s)\n\n%s\n" % (
                       kind, n, rec["label"], (" + typed: " + typed) if typed else "", how, rid, p["text"]))
        append_jsonl(os.path.join(folder, "popups.jsonl"), rec)
        if kind not in ("submit", "trust", "bypass_warning"):
            self.count[(task["id"], kind)] = self.count.get((task["id"], kind), 0) + 1
        log("popup %s #%d (%s): answered %s. %s%s" % (task["id"], seq, kind, n, rec["label"],
                                                     " + text" if typed else ""))
        # wait until this popup is gone or changed: one key per popup, never two
        sig, deadline = signature(p), time.time() + 20
        while time.time() < deadline:
            time.sleep(1)
            q = parse_popup(self.b.screen())
            if not q or signature(q) != sig:
                break
        return rec

    def move_to(self, n):
        """Move the popup's cursor to option n, checking the screen after each try."""
        for _ in range(4):
            p = parse_popup(self.b.screen())
            if not p:
                return False
            delta = n - p["cursor"]
            if delta == 0:
                return True
            self.b.keys(*(["down"] * delta if delta > 0 else ["up"] * -delta))
            time.sleep(0.7)
        p = parse_popup(self.b.screen())
        return bool(p) and p["cursor"] == n


# ---------------------------------------------------------------- orchestrator start

STARTUP = [
    (re.compile(r"(?i)(select login method|log in with|login with|claude account with subscription|"
                r"paste code here|anthropic console account)"), "abort"),
    (re.compile(r"(?i)(choose the text style|dark mode|light mode)"), "enter"),
    (re.compile(r"(?i)press enter to continue"), "enter"),
]


def start_orchestrator(b, args):
    started = time.time()
    if not args.no_start:
        plugin = OKE_DIR.replace("\\", "/")
        log("starting claude in pane %s (plugin %s, model free-coder)" % (b.pane, plugin))
        # typed into the probed shell only: claude gets that pane's env (the bench server's), never this process's
        code, out, err = herdr("pane", "run", b.pane, "claude --plugin-dir '%s' --model free-coder "
                               "--permission-mode bypassPermissions" % plugin)
        log("pane run -> %s %s" % (code, (out + err).strip()[:300]))
    deadline, popups, ready_since = time.time() + 240, Popups(b, {}), None
    dummy = {"id": "startup"}
    seq = 0
    while time.time() < deadline:
        time.sleep(2)
        b.guard()
        scr = b.screen()
        hit = [what for rx, what in STARTUP if rx.search(scr)]
        if hit:
            if hit[0] == "abort":
                write_text(os.path.join(b.logs, "startup-abort.txt"), scr)
                raise SystemExit("claude asks to log in: the free-proxy env did not reach it; aborting")
            write_text(os.path.join(b.logs, "startup-dialog-%d.txt" % time.time()), scr)
            b.keys(hit[0])
            ready_since = None
            continue
        p = parse_popup(scr)
        if p:
            kind = classify(p)
            if kind in ("trust", "bypass_warning"):
                seq += 1
                popups.answer(dummy, p, kind, os.path.join(b.logs, "startup"), seq)
                ready_since = None
                continue
            write_text(os.path.join(b.logs, "startup-unknown.txt"), scr)
            raise SystemExit("an unknown dialog at claude's start (logs/startup-unknown.txt): aborting")
        if b.pane_status() in ("idle", "done") and re.search(r"(?i)(bypass permissions|for shortcuts|❯)", scr):
            ready_since = ready_since or time.time()
            if time.time() - ready_since > 4:
                break
    else:
        write_text(os.path.join(b.logs, "startup-timeout.txt"), b.screen())
        raise SystemExit("claude did not become ready in 4 min")
    log("claude is ready; running /oke:start")
    for attempt in range(2):
        b.text("/oke:start")
        time.sleep(2)
        b.keys("enter")
        if attempt == 0 and not args.no_start:
            landed(b, started)
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(POLL)
            b.guard()
            p = parse_popup(b.screen())
            if p:
                write_text(os.path.join(b.logs, "startup-popup.txt"), p["text"])
                log("a popup during /oke:start (logs/startup-popup.txt): answering the first safe option")
                seq += 1
                popups.answer(dummy, p, classify(p), os.path.join(b.logs, "startup"), seq)
            orch = b.state().get("orchestrator") or {}
            if orch.get("pane") and b.pane_status() in ("idle", "done"):
                log("orchestrator claimed pane %s" % orch.get("pane"))
                write_text(os.path.join(b.logs, "startup-screen.txt"), b.screen(lines=200, source="recent"))
                return
        log("/oke:start did not claim the orchestrator (try %d)" % (attempt + 1))
    write_text(os.path.join(b.logs, "startup-screen.txt"), b.screen(lines=300, source="recent"))
    raise SystemExit("/oke:start never claimed the orchestrator: a plain claude would do the jobs itself; aborting")


def landed(b, since):
    """claude's session file must appear under ~/.claude-free/projects within 60 s of its first
    prompt, and none of this run under ~/.claude/projects: else the run stops."""
    deadline = time.time() + 60
    while time.time() < deadline:
        b.guard(force=True)
        free = session_files(FREE_PROJECTS, bench_prefixes(b.run), since)
        if free:
            log("claude's session file is under ~/.claude-free: %s" % free[0])
            return
        time.sleep(3)
    stop_bench("no session file of the orchestrator under %s in 60 s: claude may not run on the free "
               "config" % FREE_PROJECTS)


# ---------------------------------------------------------------- tasks

def load_tasks(path, run_dir):
    cfg = read_json(path)
    if not cfg:
        raise SystemExit("cannot read %s" % path)
    for t in cfg["tasks"]:
        t["text"] = t["prompt"].replace("{run}", run_dir)
        t["budget_min"] = cfg["budgets"].get(t.get("budget"), t.get("budget")) \
            if not isinstance(t.get("budget"), (int, float)) else t["budget"]
    cfg["tasks"].sort(key=lambda t: t["order"])
    return cfg


def groups_of(tasks):
    out, seen = [], {}
    for t in tasks:
        g = t.get("group")
        if g and g in seen:
            seen[g].append(t)
        else:
            out.append([t])
            if g:
                seen[g] = out[-1]
    return out


def send_prompt(b, text):
    """Type a request into the orchestrator once no popup is on screen; make sure it was sent."""
    deadline = time.time() + 120
    while parse_popup(b.screen()) and time.time() < deadline:
        time.sleep(POLL)
    b.text(text)
    time.sleep(1.5)
    b.keys("enter")
    for _ in range(10):
        time.sleep(2)
        if b.pane_status() in ("working", "blocked"):
            return True
    # still idle: the Enter may have closed a completion menu instead of sending
    b.keys("enter")
    for _ in range(10):
        time.sleep(2)
        if b.pane_status() in ("working", "blocked"):
            return True
    return False


def unfinished(jobs, names):
    return [n for n in names if n in jobs and jobs[n]["phase"] not in DONE_PHASES and not jobs[n]["held"]
            and not jobs[n]["archived"]]


def capture_all(b, folder, prefix):
    for p in b.panes():
        pid = p.get("pane_id")
        write_text(os.path.join(folder, "%s-%s.txt" % (prefix, pid.replace(":", "_"))),
                   "pane %s status %s cwd %s\n\n%s" % (pid, p.get("agent_status"), p.get("cwd"),
                                                       b.screen(pid, lines=300, source="recent-unwrapped")))


def answer_child_trust(b, folder):
    """A child pane stuck at the folder trust dialog (OKe answers it itself; this is a backstop).
    Nothing else is ever answered in a child pane."""
    for p in b.panes():
        pid = p.get("pane_id")
        if pid == b.pane or p.get("agent_status") != "blocked":
            continue
        q = parse_popup(b.screen(pid))
        if q and classify(q) == "trust":
            n = [n for n, l in q["options"] if re.search(r"(?i)yes", l)]
            if n:
                delta = n[0] - q["cursor"]
                herdr("pane", "send-keys", pid, *(["down"] * delta if delta > 0 else ["up"] * -delta))
                time.sleep(0.5)
                herdr("pane", "send-keys", pid, "enter")
                write_text(os.path.join(folder, "child-trust-%s-%d.txt" % (pid.replace(":", "_"), time.time())),
                           q["text"])
                log("answered a folder trust dialog in child pane %s" % pid)


def run_group(b, group, cfg, popups, args):
    """Send the group's prompts, answer popups, wait until its jobs end or a budget runs out."""
    stuck_s = cfg.get("stuck_minutes", 8) * 60
    for t in group:
        t["folder"] = os.path.join(b.logs, t["id"])
        os.makedirs(t["folder"], exist_ok=True)
        t["jobs"], t["status"], t["popups"] = [], None, 0
    before = b.snapshot()
    for t in group:
        write_json(os.path.join(t["folder"], "before.json"), before)
    known = set(before["jobs"])
    start = time.time()
    budget = max(t["budget_min"] for t in group) * 60 * args.budget_scale
    last_phase = {}
    last_activity = time.time()
    seq = popups.seq
    by_job = popups.by_job  # job -> task, kept across groups
    pending_send = list(group)
    sent = []
    current = None
    settle_since = None
    status = None
    while True:
        b.guard()
        now = time.time()
        if now - start > budget:  # first, so a popup that keeps coming back cannot hold the loop
            status = "TIMEOUT"
            break
        # 1. send the next prompt when it is due
        if pending_send:
            nxt = pending_send[0]
            due = not sent or (nxt.get("send_after") == "job_created" and sent[-1]["jobs"]) or \
                (nxt.get("send_after") is None and not unfinished(b.all_jobs(), sent[-1]["jobs"]) and sent[-1]["jobs"])
            if due:
                log("task %s: sending prompt" % nxt["id"])
                nxt["sent_at"] = time.time()
                ok = send_prompt(b, nxt["text"])
                if not ok:
                    log("task %s: the orchestrator did not start working after the prompt" % nxt["id"])
                sent.append(pending_send.pop(0))
                current = nxt
                last_activity = time.time()
        # 2. popups
        scr = b.screen()
        p = parse_popup(scr)
        if p:
            kind = classify(p)
            owner = current
            for job, task in by_job.items():
                if re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(job), p["text"]):
                    owner = task
                    break
            seq[owner["id"]] = seq.get(owner["id"], 0) + 1
            rec = popups.answer(owner, p, kind, owner["folder"], seq[owner["id"]])
            if kind not in ("submit", "trust", "bypass_warning"):
                owner["popups"] = owner.get("popups", 0) + 1
            if not rec["moved"]:
                time.sleep(POLL)  # the cursor would not move: look again, the budget still runs
            last_activity = time.time()
            settle_since = None
            continue
        # 3. jobs and phases
        jobs = b.all_jobs()
        for n in sorted(set(jobs) - known):
            # a new job belongs to the task whose prompt went last before it appeared
            owner = sent[-1] if sent else group[0]
            owner["jobs"].append(n)
            by_job[n] = owner
            known.add(n)
            log("task %s: job %s" % (owner["id"], n))
        for n, task in by_job.items():
            j = jobs.get(n) or {}
            ph = (j.get("phase"), j.get("held"), j.get("waiting_for"), j.get("archived"), j.get("auto"))
            if last_phase.get(n) != ph:
                last_phase[n] = ph
                last_activity = time.time()
                append_jsonl(os.path.join(task["folder"], "phases.jsonl"),
                             {"t": time.time(), "job": n, "phase": ph[0], "held": ph[1], "waiting_for": ph[2],
                              "archived": ph[3], "auto": ph[4]})
        # 4. activity: any agent working or blocked
        panes = b.panes()
        if any(pp.get("agent_status") in ("working", "blocked") for pp in panes):
            last_activity = time.time()
        if int(now) % 30 < POLL:
            answer_child_trust(b, current["folder"] if current else b.logs)
        # 5. the end
        orch_idle = b.pane_status() in ("idle", "done")
        all_sent = not pending_send
        open_jobs = [n for t in sent for n in unfinished(jobs, t["jobs"])]
        if all_sent and all(t["jobs"] for t in sent) and not open_jobs and orch_idle:
            settle_since = settle_since or now
            if now - settle_since > 20:
                status = "DONE"
                break
        else:
            settle_since = None
        if all_sent and orch_idle and not any(t["jobs"] for t in sent) and now - sent[-1]["sent_at"] > 360:
            status = "NO_JOB"
            break
        if now - start > budget:
            status = "TIMEOUT"
            break
        if now - last_activity > stuck_s:
            status = "STUCK"
            break
        time.sleep(POLL)
    end = time.time()
    if status in ("TIMEOUT", "STUCK"):
        capture_all(b, group[-1]["folder"], status.lower())
        jobs = b.all_jobs()
        left = [n for t in sent for n in unfinished(jobs, t["jobs"])]
        for n in left:
            # set it aside (agents closed, worktree and branch kept): it must not run into the next task
            log("task: %s; holding job %s" % (status, n))
            send_prompt(b, "hold %s" % n)
            hold_deadline = time.time() + 120
            while time.time() < hold_deadline:
                time.sleep(POLL)
                q = parse_popup(b.screen())
                if q:
                    owner = by_job.get(n, group[-1])
                    seq[owner["id"]] = seq.get(owner["id"], 0) + 1
                    popups.answer(owner, q, classify(q), owner["folder"], seq[owner["id"]])
                    continue
                if (b.all_jobs().get(n) or {}).get("held") or n not in unfinished(b.all_jobs(), [n]):
                    break
    after = b.snapshot()
    _, board, _ = b.oke("board", "--full")
    _, cost, _ = b.oke("cost")
    final = b.screen(lines=400, source="recent-unwrapped")
    for t in group:
        if not t["jobs"] and status == "DONE":
            t_status = "NO_JOB"
        else:
            t_status = status
        t["status"] = t_status
        write_json(os.path.join(t["folder"], "after.json"), after)
        write_text(os.path.join(t["folder"], "board.txt"), board)
        write_text(os.path.join(t["folder"], "cost.txt"), cost)
        write_text(os.path.join(t["folder"], "final.txt"), final)
        res = {"task": t["id"], "status": t_status, "start": t.get("sent_at", start), "end": end,
               "wall_s": round(end - t.get("sent_at", start)), "jobs": t["jobs"], "popups": t["popups"],
               "group": t.get("group")}
        write_json(os.path.join(t["folder"], "result.json"), res)
        log("task %s: %s in %ds, jobs %s, popups %d" % (t["id"], t_status, res["wall_s"], t["jobs"], t["popups"]))
    return status


# ---------------------------------------------------------------- dry run

def dry_run(b, cfg):
    """Everything that needs no LLM: the tasks, their triage as OKe sees it, the pane."""
    sys.path.insert(0, os.path.join(OKE_DIR, "bin"))
    problems = []
    try:
        import oke  # noqa: E402 - only for its triage(), a pure function
        triage = oke.triage
    except Exception as e:  # noqa: BLE001
        triage = None
        problems.append("cannot import oke.triage: %s" % e)
    for t in cfg["tasks"]:
        line = "%-22s budget %4s min" % (t["id"], t["budget_min"])
        if triage:
            tr = triage(t["text"])
            line += "  size_hint %-6s interview %-5s auto %-5s anchors %s" % (
                tr["size_hint"], tr["interview"], tr["auto_asked"], ",".join(a["kind"] for a in tr["anchors"]))
            want_interview = any(e.get("kind") == "interview" and e.get("min") for e in t["expect"])
            if want_interview != tr["interview"]:
                problems.append("%s: triage says interview=%s" % (t["id"], tr["interview"]))
            want_auto = any(e.get("kind") == "auto_confirm" and e.get("min") for e in t["expect"])
            if want_auto != tr["auto_asked"]:
                problems.append("%s: triage says auto_asked=%s" % (t["id"], tr["auto_asked"]))
        print(line)
        print("    " + t["text"])
    if b.pane:
        print("pane %s status %s" % (b.pane, b.pane_status()))
        scr = b.screen()
        print("popup on screen: %s" % bool(parse_popup(scr)))
    else:
        print("no bench.json pane (launcher not run)")
    snap = b.snapshot()
    print("snapshot: repos %s, jobs %d" % (sorted(snap["repos"]), len(snap["jobs"])))
    for prob in problems:
        print("PROBLEM: " + prob)
    return 1 if problems else 0


def main():
    global LOG
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--tasks", default=os.path.join(BENCH, "tasks.json"))
    ap.add_argument("--only")
    ap.add_argument("--from", dest="start_at")
    ap.add_argument("--budget-scale", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-start", action="store_true")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")  # a cp949 console must not crash the night
    except AttributeError:
        pass
    b = Bench(args.run_dir)
    os.makedirs(b.logs, exist_ok=True)
    LOG = os.path.join(b.logs, "driver.log")
    cfg = load_tasks(args.tasks, b.run)
    if args.dry_run:
        return dry_run(b, cfg)
    if not b.pane:
        log("no bench.json: running the launcher")
        code, out, err = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                              os.path.join(BENCH, "launch.ps1"), "-RunDir", b.run], timeout=300)
        log((out + err).strip()[-800:])
        if code != 0:
            raise SystemExit("launcher failed")
        b = Bench(args.run_dir)
    if not read_json(os.path.join(b.run, "bench.json"), {}).get("probe"):
        raise SystemExit("bench.json has no passing env probe: run bench/launch.ps1 first")
    start_orchestrator(b, args)
    tasks = cfg["tasks"]
    if args.only:
        ids = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in ids]
    if args.start_at:
        idx = [i for i, t in enumerate(tasks) if t["id"] == args.start_at]
        tasks = tasks[idx[0]:] if idx else tasks
    popups = Popups(b, cfg)
    results = read_json(os.path.join(b.logs, "results.json"), {}) or {}
    for group in groups_of(tasks):
        try:
            run_group(b, group, cfg, popups, args)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001 - one task's crash must not stop the night
            log("task %s: ERROR %r" % (group[0]["id"], e))
            for t in group:
                t["status"] = "ERROR"
                write_json(os.path.join(b.logs, t["id"], "result.json"),
                           {"task": t["id"], "status": "ERROR", "error": repr(e), "jobs": t.get("jobs", [])})
        for t in group:
            results[t["id"]] = t.get("status")
        write_json(os.path.join(b.logs, "results.json"), results)
    log("all tasks ran: %s" % json.dumps(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
