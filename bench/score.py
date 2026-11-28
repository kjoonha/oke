"""bench/score.py <run_dir> [--tasks FILE]: score a bench run and write <run_dir>/report.md.

Reads only: the repos (git), OKE_HOME (<run_dir>/home: archive, jobs, ledgers, plans, reviews),
the driver's logs (<run_dir>/logs/<task>/: before/after snapshots, popups, phases, results) and
the orchestrator's transcript under CLAUDE_CONFIG_DIR (default ~/.claude-free) for English
narration lines. A task the driver never ran is PENDING; a missing piece of evidence fails its check.
"""
import argparse
import calendar
import glob
import io
import json
import os
import re
import subprocess
import sys
import time

BENCH = os.path.dirname(os.path.abspath(__file__))
IGNORED_KINDS = ("submit", "trust", "bypass_warning")


def read_json(path, default=None):
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return default


def read_text(path):
    try:
        with io.open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except (IOError, OSError):
        return None


def read_jsonl(path):
    out = []
    for line in (read_text(path) or "").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def git(repo, *args):
    try:
        p = subprocess.run(["git", "-C", repo, "-c", "core.quotepath=off"] + list(args), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


class Ctx(object):
    """One task's evidence."""

    def __init__(self, run, task):
        self.run = run
        self.task = task
        self.folder = os.path.join(run, "logs", task["id"])
        self.result = read_json(os.path.join(self.folder, "result.json"))
        self.before = read_json(os.path.join(self.folder, "before.json"))
        self.after = read_json(os.path.join(self.folder, "after.json"))
        self.popups = [p for p in read_jsonl(os.path.join(self.folder, "popups.jsonl"))
                       if p.get("kind") not in IGNORED_KINDS]
        self.phases = read_jsonl(os.path.join(self.folder, "phases.jsonl"))
        self.home = os.path.join(run, "home")

    def ran(self):
        return bool(self.result)

    def jobs(self):
        return (self.result or {}).get("jobs") or []

    def job_info(self, job):
        return ((self.after or {}).get("jobs") or {}).get(job) or {}

    def job_folder(self, job):
        f = self.job_info(job).get("folder")
        if f and os.path.isdir(os.path.join(self.home, f)):
            return os.path.join(self.home, f)
        found = sorted(glob.glob(os.path.join(self.home, "archive", glob.escape(job) + "-*")))
        if found:
            return found[-1]
        d = os.path.join(self.home, "jobs", job)
        return d if os.path.isdir(d) else None

    def repo_path(self, repo):
        return os.path.join(self.run, repo)

    def refs(self, which, repo):
        snap = self.before if which == "before" else self.after
        return (((snap or {}).get("repos") or {}).get(repo) or {}).get("refs") or {}

    def new_commits(self, repo, ref=None):
        """Commits reachable from the after-refs (or one ref) and from none of the before-refs."""
        after, before = self.refs("after", repo), self.refs("before", repo)
        if not after:
            return []
        tips = [after[ref]] if ref else sorted(set(v for k, v in after.items() if not k.startswith("refs/stash")))
        if ref and ref not in after:
            return []
        code, out = git(self.repo_path(repo), "rev-list", *(tips + ["--not"] + sorted(set(before.values()))))
        return out.split() if code == 0 else []

    def files_of(self, repo, commit):
        code, out = git(self.repo_path(repo), "show", "--name-only", "--format=", commit)
        return [l.strip() for l in out.splitlines() if l.strip()] if code == 0 else []

    def show(self, repo, commit, path):
        code, out = git(self.repo_path(repo), "show", "%s:%s" % (commit, path))
        return out if code == 0 else None

    def subject(self, repo, commit):
        return git(self.repo_path(repo), "log", "-1", "--format=%s", commit)[1].strip()


def section(text, name):
    m = re.search(r"^##\s+%s\s*$(.*?)(?=^##\s|\Z)" % re.escape(name), text or "", re.M | re.S)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------- checks: (ok, evidence)

def c_jobs_finished(c, e):
    st = (c.result or {}).get("status")
    if not c.jobs():
        return False, "status %s, no job" % st
    phases = dict((j, c.job_info(j).get("phase")) for j in c.jobs())
    ok = st == "DONE" and all(p == "done" for p in phases.values())
    return ok, "status %s; %s" % (st, ", ".join("%s=%s" % kv for kv in sorted(phases.items())))


def c_job_answered(c, e):
    ans = [j for j in c.jobs() if c.job_info(j).get("answer")]
    return bool(ans), "answered: %s" % (ans or "none")


def c_popup_count(c, e):
    kind = e.get("kind", "any")
    n = len([p for p in c.popups if kind == "any" or p.get("kind") == kind])
    ok = (e.get("min") is None or n >= e["min"]) and (e.get("max") is None or n <= e["max"])
    kinds = ", ".join(p.get("kind") for p in c.popups) or "none"
    return ok, "%s popups: %d (want %s..%s); all: %s" % (kind, n, e.get("min", 0), e.get("max", "inf"), kinds)


def c_no_new_commits(c, e):
    bad = dict((r, len(c.new_commits(r))) for r in e["repos"])
    bad = dict((r, n) for r, n in bad.items() if n)
    return not bad, "new commits: %s" % (bad or "none")


def c_no_new_branches(c, e):
    prefix = e.get("prefix", "refs/heads/")
    new = []
    for r in e["repos"]:
        new += ["%s:%s" % (r, k) for k in c.refs("after", r) if k.startswith(prefix) and k not in c.refs("before", r)]
    return not new, "new branches under %s: %s" % (prefix, new or "none")


def c_status_unchanged(c, e):
    b = (((c.before or {}).get("repos") or {}).get(e["repo"]) or {}).get("status")
    a = (((c.after or {}).get("repos") or {}).get(e["repo"]) or {}).get("status")
    return b is not None and a == b, "status before %s, after %s" % (b, a)


def c_file_fixed_in_new_commit(c, e):
    for commit in c.new_commits(e["repo"]):
        text = c.show(e["repo"], commit, e["file"])
        if text is not None and e["absent"] not in text and (not e.get("present") or e["present"] in text):
            return True, "%s %s: %s fixed (%s)" % (commit[:8], c.subject(e["repo"], commit), e["file"],
                                                   ", ".join(c.files_of(e["repo"], commit)))
    return False, "no new commit fixes %s (%d new commits)" % (e["file"], len(c.new_commits(e["repo"])))


def snap_file(c, repo, path):
    return ((((c.after or {}).get("repos") or {}).get(repo) or {}).get("files") or {}).get(path)


def c_worktree_contains(c, e):
    text = snap_file(c, e["repo"], e["file"])
    return text is not None and e["text"] in text, "%s/%s at the task's end %s %r" % (
        e["repo"], e["file"], "holds" if text and e["text"] in text else "lacks", e["text"])


def c_job_size(c, e):
    sizes = dict((j, c.job_info(j).get("size")) for j in c.jobs())
    return any(s == e["size"] for s in sizes.values()), "sizes: %s" % sizes


def plan_text(c, job):
    f = c.job_folder(job)
    return read_text(os.path.join(f, "plan.md")) if f else None


def c_plan_check_none(c, e):
    out = []
    for j in c.jobs():
        sec = section(plan_text(c, j), "Check command")
        if sec is not None:
            first = sec.splitlines()[0] if sec else ""
            out.append((j, first))
    ok = any(f.strip().strip("`").lower().startswith("none") for _, f in out)
    return ok, "check command: %s" % (out or "no plan.md")


def review_reports(c, job):
    f = c.job_folder(job)
    return sorted(glob.glob(os.path.join(f, "*-rv*", "report.md"))) if f else []


GRADE = re.compile(r"^\s*[-*]\s*[*_`#]*(?:AC)?\d+[*_`]*\s*:\s*[*_`]*(VERIFIED|PARTIAL|MISSING)", re.M)


def c_review_grades(c, e):
    for j in c.jobs():
        for rep in reversed(review_reports(c, j)):
            grades = GRADE.findall(section(read_text(rep), "Acceptance criteria") or "")
            if grades:
                ok = all(g == e.get("all", "VERIFIED") for g in grades)
                return ok, "%s: %s" % (os.path.relpath(rep, c.home), " ".join(grades))
    return False, "no review report with grades"


def c_file_in_new_commit(c, e):
    for commit in c.new_commits(e["repo"]):
        text = c.show(e["repo"], commit, e["file"])
        if text is not None and e["text"] in text:
            return True, "%s %s" % (commit[:8], c.subject(e["repo"], commit))
    return False, "no new commit has %r in %s" % (e["text"], e["file"])


def c_new_commits_on(c, e):
    n = len(c.new_commits(e["repo"], e["ref"]))
    return n >= e.get("min", 1), "%d new commits on %s" % (n, e["ref"])


def c_new_commits_only_touch(c, e):
    touched = set()
    for commit in c.new_commits(e["repo"], e["ref"]):
        touched.update(c.files_of(e["repo"], commit))
    extra = sorted(touched - set(e["allow"]))
    return bool(touched) and not extra, "touched %s%s" % (sorted(touched), "; NOT allowed: %s" % extra if extra else "")


def status_paths(c, repo):
    lines = (((c.after or {}).get("repos") or {}).get(repo) or {}).get("status") or []
    return dict((l[3:].strip().strip('"'), l[:2]) for l in lines if len(l) > 3)


def c_uncommitted(c, e):
    st = status_paths(c, e["repo"])
    missing = [f for f in e["files"] if f not in st]
    return not missing and bool(c.after), "status: %s%s" % (
        dict((f, st.get(f)) for f in e["files"]), "; committed or gone: %s" % missing if missing else "")


def c_head_file_contains(c, e):
    ref = "refs/heads/" + e["ref"]
    sha = c.refs("after", e["repo"]).get(ref)
    text = c.show(e["repo"], sha, e["file"]) if sha else None
    ok = bool(text) and re.search(e["regex"], text) is not None
    return ok, "%s:%s %s /%s/" % (e["ref"], e["file"], "matches" if ok else "does not match", e["regex"])


def ledger_rows(c, job):
    f = c.job_folder(job)
    rows = []
    for line in ((read_text(os.path.join(f, "ledger.tsv")) or "") if f else "").splitlines():
        cells = line.split("\t")
        if len(cells) >= 6:
            rows.append(cells)
    return rows


def c_pre_existing_reported(c, e):
    for j in c.jobs():
        for cells in ledger_rows(c, j):
            m = re.search(r"pre_existing=(\d+)", cells[5])
            if cells[3] == "check" and m and int(m.group(1)) > 0:
                return cells[4] == "pass" or c.job_info(j).get("phase") == "done", \
                    "ledger: check %s with pre_existing=%s; job %s" % (cells[4], m.group(1), c.job_info(j).get("phase"))
        f = c.job_folder(j)
        rep = read_text(os.path.join(f, "pm", "report.md")) if f else None
        if rep and re.search(r"(?i)(pre[-_ ]?existing|기존(에|부터)?\s*(있던|실패)|원래(부터)?\s*실패|이미 실패)", rep):
            return True, "PM report names the pre-existing failure"
    return False, "no check with pre_existing and no mention in the report"


def c_plan_size_ok(c, e):
    sizes = dict((j, len((plan_text(c, j) or "").encode("utf-8"))) for j in c.jobs())
    return bool(sizes) and all(0 < s <= e["max_bytes"] for s in sizes.values()), "plan bytes: %s" % sizes


def c_ref_unchanged(c, e):
    b, a = c.refs("before", e["repo"]).get(e["ref"]), c.refs("after", e["repo"]).get(e["ref"])
    return b is not None and a == b, "%s %s -> %s" % (e["ref"], (b or "-")[:8], (a or "-")[:8])


def c_no_new_worktrees(c, e):
    get = lambda s: set((((s or {}).get("repos") or {}).get(e["repo"]) or {}).get("worktrees") or [])
    new = sorted(get(c.after) - get(c.before))
    return bool(c.after) and not new, "new worktrees: %s" % (new or "none")


def c_two_commits_split(c, e):
    doc, code = set(e["doc"]), set(e["code"])
    kinds = []
    for commit in c.new_commits(e["repo"]):
        files = set(c.files_of(e["repo"], commit))
        if files and files <= doc:
            kinds.append(("doc", commit))
        elif files & code and not files & doc:
            kinds.append(("code", commit))
        else:
            kinds.append(("mixed", commit))
    ok = any(k == "doc" for k, _ in kinds) and any(k == "code" for k, _ in kinds)
    return ok, "new commits: %s" % ([("%s %s %s" % (k, s[:8], c.subject(e["repo"], s))) for k, s in kinds] or "none")


def c_file_exists(c, e):
    repo, _, rel = e["path"].partition("/")
    files = (((c.after or {}).get("repos") or {}).get(repo) or {}).get("files") or {}
    ok = rel in files or os.path.exists(os.path.join(c.run, e["path"])) and bool(c.after)
    return ok, "%s %s" % (e["path"], "exists" if ok else "missing")


def c_untracked_or_uncommitted(c, e):
    st = status_paths(c, e["repo"])
    return e["file"] in st, "status of %s: %s" % (e["file"], st.get(e["file"], "clean or missing"))


def c_job_auto(c, e):
    auto = [j for j in c.jobs() if c.job_info(j).get("auto")] + \
        [p["job"] for p in c.phases if p.get("auto")]
    return bool(auto), "auto on: %s" % (sorted(set(auto)) or "none")


def c_any_new_commit_contains(c, e):
    for commit in c.new_commits(e["repo"]):
        text = c.show(e["repo"], commit, e["file"])
        if text and re.search(e["regex"], text):
            return True, "%s %s" % (commit[:8], c.subject(e["repo"], commit))
    return False, "no new commit with /%s/ in %s" % (e["regex"], e["file"])


def c_queued_after(c, e, all_ctx):
    other = all_ctx.get(e["other"])
    if not other or not other.jobs() or not c.jobs():
        return False, "no jobs to compare"
    waited = [p for p in c.phases if p["job"] in c.jobs() and p.get("waiting_for")]
    if waited:
        return True, "job %s waited for %s" % (waited[0]["job"], waited[0]["waiting_for"])
    first_done = min([p["t"] for p in other.phases if p["job"] in other.jobs() and p["phase"] in ("done",)] or [0])
    started = min([p["t"] for p in c.phases if p["job"] in c.jobs() and p["phase"] == "working"] or [0])
    ok = bool(first_done and started and started >= first_done)
    return ok, "no waiting_for seen; other done at %s, this one working at %s" % (
        time.strftime("%H:%M:%S", time.localtime(first_done)) if first_done else "-",
        time.strftime("%H:%M:%S", time.localtime(started)) if started else "-")


def c_change_request_one_step(c, e):
    typed = [i for i, p in enumerate(c.popups) if p.get("typed")]
    if not typed:
        return False, "no change request was typed"
    i = typed[0]
    nxt = c.popups[i + 1] if i + 1 < len(c.popups) else None
    replanned = [p for p in c.phases if p["t"] > c.popups[i]["t"] and p["phase"] == "planning"]
    ok = bool(nxt) and nxt.get("kind") == "approval" and bool(replanned)
    return ok, "after the typed request: next popup %s; planning again: %s" % (
        nxt.get("kind") if nxt else "none", bool(replanned))


CHECKS = dict((n[2:], f) for n, f in globals().items() if n.startswith("c_"))


# ---------------------------------------------------------------- English narration

def slug(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def transcript_lines(run):
    """[(epoch, text)] of the orchestrator's assistant text, from its transcript(s)."""
    info = read_json(os.path.join(run, "bench.json"), {}) or {}
    orch = (info.get("orch") or os.path.join(run, "orch")).replace("/", "\\")
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude-free")
    want = slug(orch).lower()
    out = []
    for d in glob.glob(os.path.join(cfg, "projects", "*")):
        if os.path.basename(d).lower() != want:
            continue
        for f in glob.glob(os.path.join(d, "*.jsonl")):
            for rec in read_jsonl(f):
                if rec.get("type") != "assistant":
                    continue
                try:
                    t = calendar.timegm(time.strptime(rec["timestamp"][:19], "%Y-%m-%dT%H:%M:%S"))
                except (KeyError, ValueError):
                    t = 0
                for part in (rec.get("message") or {}).get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "text":
                        out.append((t, part.get("text") or ""))
    return out


def english_lines(text):
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    out = []
    for line in text.splitlines():
        s = re.sub(r"`[^`]*`|https?://\S+|[A-Za-z]:[\\/]\S*|\S*/\S*|\b[\w.-]+\.(py|md|json|txt|html)\b", " ", line)
        latin = re.findall(r"[A-Za-z]{2,}", s)
        hangul = len(re.findall(u"[가-힣]", s))
        letters = sum(len(w) for w in latin) + hangul
        if len(latin) >= 4 and letters and hangul / float(letters) < 0.2:
            out.append(line.strip())
    return out


# ---------------------------------------------------------------- report

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--tasks", default=os.path.join(BENCH, "tasks.json"))
    args = ap.parse_args()
    run = os.path.abspath(args.run_dir)
    cfg = read_json(args.tasks)
    tasks = sorted(cfg["tasks"], key=lambda t: t["order"])
    ctxs = dict((t["id"], Ctx(run, t)) for t in tasks)
    narration = transcript_lines(run)
    rows, verdicts, total_s, total_pop, details = [], {}, 0, 0, []
    prev_orch = 0
    for t in tasks:
        c = ctxs[t["id"]]
        checks = []
        for e in t["expect"]:
            f = CHECKS.get(e["check"])
            if not f:
                checks.append((e["check"], False, "unknown check"))
                continue
            try:
                ok, ev = f(c, e, ctxs) if e["check"] == "queued_after" else f(c, e)
            except Exception as ex:  # noqa: BLE001 - a broken check fails, never the report
                ok, ev = False, "check crashed: %r" % ex
            checks.append((e["check"], ok, ev))
        asks = (c.result or {}).get("chat_asks") or 0
        if asks:  # the driver answered in chat what the orchestrator should have asked in a popup
            checks.append(("asked_by_popup", False, "%d ask(s) came as chat text, not a popup "
                                                    "(logs/<task>/chat-ask-NN.txt)" % asks))
        if not c.ran():
            verdict = "PENDING"
        else:
            verdict = "PASS" if all(ok for _, ok, _ in checks) else "FAIL"
        verdicts[t["id"]] = verdict
        res = c.result or {}
        wall = res.get("wall_s") or 0
        total_s += wall
        total_pop += len(c.popups)
        # tokens: the task's jobs (archived or open) plus the orchestrator's growth since the last task
        cost = {}
        try:
            cost = json.loads(read_text(os.path.join(c.folder, "cost.txt")) or "{}")
        except ValueError:
            pass
        tok = 0
        for place, r in cost.items():
            if any(place == "jobs/" + j or re.match(r"archive/%s-\d{8}-\d{6}$" % re.escape(j), place)
                   for j in c.jobs()):
                tok += sum(r.get(k, 0) for k in ("input", "output", "cache_read", "cache_write"))
        orch = cost.get("orchestrator") or {}
        orch_total = sum(orch.get(k, 0) for k in ("input", "output", "cache_read", "cache_write"))
        orch_tok = orch_total - prev_orch if orch_total else 0
        prev_orch = orch_total or prev_orch
        eng = []
        if res.get("start"):
            for ts, text in narration:
                if res["start"] - 5 <= ts <= res.get("end", ts) + 5:
                    eng += english_lines(text)
        rows.append("| %s | %s | %s | %s | %d | %s | %s | %d |" % (
            t["id"], verdict, res.get("status", "-"), "%dm%02ds" % divmod(wall, 60) if wall else "-",
            len(c.popups), ", ".join(c.jobs()) or "-", "{:,}".format(tok + orch_tok) if (tok or orch_tok) else "-",
            len(eng)))
        details.append("### %s: %s\n\n%s\n\n%s\n" % (
            t["id"], verdict, "> " + t["prompt"],
            "\n".join("- %s `%s`: %s" % ("PASS" if ok else "FAIL", n, ev) for n, ok, ev in checks)
            + ("\n- popups: " + "; ".join("%s -> %s%s" % (p.get("kind"), p.get("label"),
                                                         " + typed" if p.get("typed") else "")
                                          for p in c.popups) if c.popups else "")
            + ("\n- English narration: " + " / ".join(eng[:5]) if eng else "")))
    cover = {}
    for t in tasks:
        for k in t.get("covers") or []:
            cover.setdefault(k, []).append(t["id"])
    check_rows = []
    for k in sorted(cfg.get("checklist") or {}, key=lambda s: (s[0], len(s), s)):
        ids = cover.get(k, [])
        vs = [verdicts[i] for i in ids]
        if not ids:
            v = "not covered"
        elif all(x == "PENDING" for x in vs):
            v = "PENDING"
        elif all(x == "PASS" for x in vs):
            v = "PASS"
        elif any(x == "PASS" for x in vs):
            v = "PARTIAL"
        else:
            v = "FAIL"
        check_rows.append("| %s | %s | %s | %s |" % (k, cfg["checklist"][k], ", ".join(ids) or "-", v))
    n_pass = sum(1 for v in verdicts.values() if v == "PASS")
    report = [
        "# OKe bench report", "",
        "run: `%s`  " % run.replace("\\", "/"),
        "scored: %s" % time.strftime("%Y-%m-%d %H:%M:%S"), "",
        "**%d/%d PASS**, %d pending; total %dm%02ds; %d popups" % (
            n_pass, len(tasks), sum(1 for v in verdicts.values() if v == "PENDING"),
            total_s // 60, total_s % 60, total_pop), "",
        "| task | verdict | driver | time | popups | jobs | tokens | English lines |",
        "|---|---|---|---|---|---|---|---|"] + rows + [
        "", "## Checklist", "", "| item | what | tasks | verdict |", "|---|---|---|---|"] + check_rows + [
        "", "## Evidence", ""] + details
    path = os.path.join(run, "report.md")
    with io.open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    print("score: %d/%d PASS -> %s" % (n_pass, len(tasks), path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
