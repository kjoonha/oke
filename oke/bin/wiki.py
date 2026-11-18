"""The repository and shared wikis: capped pages, read-before-write, near-duplicate
refusal, a log line per job, usage from worker reports, and time-based archiving.
Lessons from Hermes Agent: bloat starts at write time (reworded duplicates), an
unattended pass must never delete, and a page nobody uses goes away by itself."""
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import time

PAGE_CHARS = 3000
INDEX_CHARS = 2000
STALE_DAYS = 14
ARCHIVE_DAYS = 30
DUP_RATIO = 0.85
DUP_MIN = 25  # shorter lines (headings, "- none") are not facts worth comparing
DAY = 86400
PAGE = re.compile(r"^[a-z0-9][a-z0-9-]*\.md$")
SPECIAL = ("index.md", "log.md")


class WikiError(Exception):
    pass


def check_name(page):
    if not PAGE.match(page) or page == "log.md":
        raise WikiError("bad page name %r: lowercase letters, digits and -, ending in .md "
                        "(log.md is written by `oke wiki log`)" % page)


def _read(path):
    try:
        with io.open(path, encoding="utf-8") as f:
            return f.read()
    except (IOError, OSError):
        return None


def _write(path, text):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def sha(text):
    return "new" if text is None else hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def usage_path(wiki):
    return os.path.join(wiki, ".usage.json")


def load_usage(wiki):
    try:
        with io.open(usage_path(wiki), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (IOError, OSError, ValueError):
        return {}


def save_usage(wiki, data):
    _write(usage_path(wiki), json.dumps(data, indent=1, sort_keys=True))


def pages(wiki):
    try:
        return sorted(n for n in os.listdir(wiki) if PAGE.match(n) and n not in SPECIAL)
    except OSError:
        return []


def read(wiki, page):
    check_name(page)
    text = _read(os.path.join(wiki, page))
    return {"page": page, "base": sha(text), "chars": len(text or ""), "cap": cap(page),
            "text": text}


def cap(page):
    return INDEX_CHARS if page == "index.md" else PAGE_CHARS


def facts(text):
    return [l.strip(" -*\t") for l in (text or "").splitlines() if len(l.strip(" -*\t")) >= DUP_MIN]


def duplicates(wiki, page, text, old, merging=()):
    """New lines of `text` that reword a line already on another page (not on the
    pages being merged into it, which are archived next)."""
    known = set(facts(old))
    fresh = [l for l in facts(text) if l not in known]
    found = []
    for other in pages(wiki):
        if other == page or other in merging:
            continue
        for line in facts(_read(os.path.join(wiki, other))):
            for new in fresh:
                if difflib.SequenceMatcher(None, new.lower(), line.lower()).ratio() >= DUP_RATIO:
                    found.append({"new": new, "page": other, "line": line})
    return found


def stamp(now=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))


def log(wiki, job, note, now=None):
    line = "%s %s: %s\n" % (stamp(now), job, " ".join(note.split()))
    with io.open(os.path.join(wiki, "log.md"), "a", encoding="utf-8", newline="\n") as f:
        f.write(line)


def logged(wiki, job, since=None):
    """A log.md line for `job` written at or after `since` (a time): an earlier job of
    the same name does not count."""
    after = stamp(since) if since else ""
    return any(m.group(1) >= after for m in re.finditer(
        r"(?m)^(\S+) %s: " % re.escape(job), _read(os.path.join(wiki, "log.md")) or ""))


def write(wiki, page, text, base, job, note, dup_ok=False, now=None, merging=()):
    """Replace `page` with `text`. `base` is what `read` printed: a page changed since
    (or never read) is refused, so an edit always starts from the current text."""
    check_name(page)
    if not note or not note.strip():
        raise WikiError("say in --note what changed")
    path = os.path.join(wiki, page)
    old = _read(path)
    if base != sha(old):
        raise WikiError("%s changed since you read it (or you did not read it): run `oke wiki read %s` "
                        "and edit that text" % (page, page))
    if len(text) > cap(page):
        raise WikiError("%s would be %d characters, over its cap of %d: merge or drop entries "
                        "(on this page or by archiving another) and write it again"
                        % (page, len(text), cap(page)))
    dups = [] if dup_ok or page == "index.md" else duplicates(wiki, page, text, old, merging)
    if dups:
        raise WikiError("these lines repeat another page; strengthen that line instead, or pass "
                        "--dup-ok if they really differ:\n" + "\n".join(
                            "  %r ~ %s: %r" % (d["new"], d["page"], d["line"]) for d in dups))
    _write(path, text)
    now = time.time() if now is None else now
    if page != "index.md":
        usage = load_usage(wiki)
        usage.setdefault(page, {"created": now})
        save_usage(wiki, usage)
    log(wiki, job, "%s: %s" % (page, note), now)
    return {"page": page, "base": sha(text), "chars": len(text), "cap": cap(page)}


WIKI_LINE = re.compile(r"^\s*[-*]?\s*`?([a-z0-9][a-z0-9-]*\.md)`?\s*:\s*(ok|wrong|unused)\b\s*[:,-]?\s*(.*)$", re.I)


def report_entries(text):
    """(page, verdict, why) from a report's `## wiki` section."""
    out, inside = [], False
    for line in (text or "").splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower().startswith("wiki")
            continue
        m = WIKI_LINE.match(line) if inside else None
        if m:
            out.append((m.group(1), m.group(2).lower(), m.group(3).strip()))
    return out


def record(wiki, entries, now=None):
    """Count what a finished job's reports said about the pages of `wiki`."""
    now = time.time() if now is None else now
    usage = load_usage(wiki)
    seen = False
    for page, verdict, why in entries:
        if verdict == "unused" or page in SPECIAL or not os.path.isfile(os.path.join(wiki, page)):
            continue
        u = usage.setdefault(page, {"created": now})
        u["used"] = now
        u["uses"] = u.get("uses", 0) + 1
        if verdict == "wrong":
            u["wrong"] = u.get("wrong", 0) + 1
            u["wrong_why"] = why[:200]
        seen = True
    if seen:
        save_usage(wiki, usage)


def status(wiki, now=None):
    now = time.time() if now is None else now
    usage = load_usage(wiki)
    out = []
    new = [p for p in pages(wiki) if p not in usage]
    for page in new:  # written before usage was kept: its 30 days start now
        usage[page] = {"created": now}
    if new:
        save_usage(wiki, usage)
    for page in pages(wiki):
        u = usage[page]
        last = u.get("used") or u.get("created", now)
        idle = int((now - last) // DAY)
        out.append({"page": page, "chars": len(_read(os.path.join(wiki, page)) or ""), "cap": PAGE_CHARS,
                    "uses": u.get("uses", 0), "idle_days": idle, "wrong": u.get("wrong", 0),
                    "wrong_why": u.get("wrong_why"), "pinned": bool(u.get("pinned")),
                    "state": "pinned" if u.get("pinned") else
                    "archive_due" if idle >= ARCHIVE_DAYS else "stale" if idle >= STALE_DAYS else "active"})
    index = _read(os.path.join(wiki, "index.md")) or ""
    return {"wiki": wiki.replace("\\", "/"), "pages": out,
            "index": {"chars": len(index), "cap": INDEX_CHARS}}


def archive(wiki, page, reason, job="curate", now=None):
    """Move a page to .archive/ (never delete) and drop its index lines."""
    check_name(page)
    if page == "index.md":
        raise WikiError("index.md is not archived")
    src = os.path.join(wiki, page)
    if not os.path.isfile(src):
        raise WikiError("no page %s" % page)
    dst_dir = os.path.join(wiki, ".archive")
    if not os.path.isdir(dst_dir):
        os.makedirs(dst_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    shutil.move(src, os.path.join(dst_dir, "%s-%s" % (stamp, page)))
    index = os.path.join(wiki, "index.md")
    text = _read(index)
    if text is not None:
        named = re.compile(r"(?<![a-z0-9-])%s" % re.escape(page))  # old.md, not bold.md
        kept = [l for l in text.splitlines(True) if not named.search(l)]
        if len(kept) != len(text.splitlines(True)):
            _write(index, "".join(kept))
    usage = load_usage(wiki)
    if usage.pop(page, None) is not None:
        save_usage(wiki, usage)
    log(wiki, job, "archived %s: %s" % (page, reason), now)


def prune(wiki, apply, now=None):
    """Pages unused for ARCHIVE_DAYS (pinned ones never) go to .archive/."""
    due = [p["page"] for p in status(wiki, now)["pages"] if p["state"] == "archive_due"]
    if apply:
        for page in due:
            archive(wiki, page, "unused for %d days" % ARCHIVE_DAYS, now=now)
    return due


def pin(wiki, page, on):
    check_name(page)
    if not os.path.isfile(os.path.join(wiki, page)):
        raise WikiError("no page %s" % page)
    usage = load_usage(wiki)
    usage.setdefault(page, {"created": time.time()})["pinned"] = bool(on)
    save_usage(wiki, usage)
