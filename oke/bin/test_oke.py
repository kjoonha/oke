import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import oke  # noqa: E402
import runtime_herdr  # noqa: E402


def put_pm(s, key, record):
    """In state `s`: make record's agent a PM with that record, and give it the jobs whose
    git key is `key` (tests). A PM left with no job and no record goes."""
    name = record["agent"]
    e = s["pms"].setdefault(name, {"pm": {}, "places": []})
    e["pm"] = record
    for j in s["jobs"].values():
        if j.get("git_key") == key:
            j["pm"] = name
    for n, other in list(s["pms"].items()):
        if n != name and not (other.get("pm") or {}).get("agent") \
                and not any(j.get("pm") == n for j in s["jobs"].values()):
            del s["pms"][n]


class Base(unittest.TestCase):
    def archive(self, job):
        """`job archive` of a job these tests leave open: only a finished job is archived."""
        st = oke.load_state()
        if job in st["jobs"] and st["jobs"][job].get("phase") not in ("done", "failed"):
            st["jobs"][job]["phase"] = "done"
            oke.save_state(st)
        return self.cli("job", "archive", job)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self._env = dict(os.environ)
        self._grace = oke.BLOCKED_GRACE
        self._worktrees = runtime_herdr.WORKTREES_ROOT
        runtime_herdr.WORKTREES_ROOT = os.path.join(self.tmp, "herdr-worktrees")  # never the real ~/.herdr
        self._waits = runtime_herdr.CLOSE_WAIT, runtime_herdr.REMOVE_POLL
        runtime_herdr.CLOSE_WAIT, runtime_herdr.REMOVE_POLL = 0, 0  # a fake pane list never changes
        os.environ["OKE_HOME"] = self.home
        for k in ("HERDR_ENV", "HERDR_PANE_ID", "OKE_HERDR", "FAKE_HERDR_DIR", "OKE_RUNTIME",
                  "OKE_CLAUDE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR"):
            os.environ.pop(k, None)
        os.environ["OKE_RUNTIME"] = "herdr"  # outside a herdr pane OKe would pick bg
        self.out = ""
        self.err = ""

    def tearDown(self):
        oke.BLOCKED_GRACE = self._grace
        runtime_herdr.WORKTREES_ROOT = self._worktrees
        runtime_herdr.CLOSE_WAIT, runtime_herdr.REMOVE_POLL = self._waits
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def write(self, name, text):
        p = self.path(name)
        d = os.path.dirname(p)
        if not os.path.isdir(d):
            os.makedirs(d)
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return p

    def git(self, cwd, *args):
        subprocess.check_call(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t"] + list(args),
            cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def git_repo(self, name):
        p = self.path(name)
        os.makedirs(p)
        self.git(p, "init", "-q")
        self.git(p, "commit", "-q", "--allow-empty", "-m", "init")
        return p

    def git_worktree(self, repo, name):
        p = self.path(name)
        self.git(repo, "worktree", "add", "-q", p, "-b", "wt")
        return p

    PATH = ("intake", "planning", "awaiting_approval", "working", "done")

    def advance(self, name, phase):
        """Walk job `name` through the phases up to `phase`, as the orchestrator does
        (a clear interview first: planning needs one)."""
        if not os.path.exists(oke.interview_path(name)):
            self.assertEqual(self.cli("interview", name, "--goal", 1, "--constraints", 1, "--criteria", 1), 0)
        if phase in ("failed", "reviewing"):
            self.assertEqual(self.cli("job", "phase", name, phase), 0, self.err)
            return
        now = oke.load_state()["jobs"][name]["phase"]
        start = self.PATH.index(now) + 1 if now in self.PATH else 0
        for step in self.PATH[start:self.PATH.index(phase) + 1]:
            if step == "done":
                self.logged(name)
                self.verified(name)
            self.assertEqual(self.cli("job", "phase", name, step), 0, self.err)

    def verified(self, name):
        """A delivery OKe recorded, which `job phase done` asks for of a code job."""
        oke.ledger_add(name, "oke", "abc1234", "deliver", "ok", "main pre=abc1234")

    def logged(self, name):
        """The PM's wiki line for the job, which `job phase done` asks for."""
        self.assertEqual(self.cli("wiki", "log", "--job", name, "--note", "none"), 0, self.err)

    def pm_for(self, path):
        """Tests that only need some PM get `pm-<folder name>` for a path (the user picks
        the PM in real use), made if missing."""
        st = oke.load_state()
        _, folder, _ = oke.path_facts(str(path))
        name = "pm-" + oke.slug(os.path.basename(folder.rstrip("/")), 20)
        if name not in st["pms"]:
            st["pms"][name] = {"pm": {}, "places": []}
            oke.save_state(st)
        return name

    def cli(self, *args):
        args = [str(a) for a in args]
        # `--repo <path>` in a test: `--pm pm-<folder> --path <path>`
        if args[:2] == ["job", "new"] and "--repo" in args:
            i = args.index("--repo")
            try:
                pm = self.pm_for(args[i + 1])
            except ValueError:  # a path job new refuses: some PM, so the path is what fails
                pm = self.pm_for(self.home)
            args[i:i + 2] = ["--pm", pm, "--path", args[i + 1]]
        elif args[:1] == ["wiki"] and "--repo" in args:
            i = args.index("--repo")
            args[i:i + 2] = ["--pm", self.pm_for(args[i + 1])]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = oke.main(args)
        self.out, self.err = out.getvalue(), err.getvalue()
        return code


    def fake(self, scenario):
        d = self.path("fake")
        if not os.path.isdir(d):
            os.makedirs(d)
        for name in ("calls.log", "counters.json"):
            if os.path.exists(os.path.join(d, name)):
                os.remove(os.path.join(d, name))
        with io.open(os.path.join(d, "scenario.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(scenario))
        os.environ["FAKE_HERDR_DIR"] = d
        os.environ["OKE_HERDR"] = json.dumps([sys.executable, os.path.join(HERE, "fake", "herdr.py")])

    def calls(self):
        p = os.path.join(self.path("fake"), "calls.log")
        if not os.path.exists(p):
            return []
        with io.open(p, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class TestState(Base):
    def test_first_run_creates_home_and_defaults(self):
        home = os.path.join(self.tmp, "nope")
        os.environ["OKE_HOME"] = home
        self.assertEqual(oke.load_state(), {"orchestrator": {}, "pms": {}, "jobs": {}})
        self.assertEqual(oke.load_config()["autonomy"], "balanced")
        self.assertTrue(os.path.isdir(home))

    def test_config_file_overrides_defaults(self):
        self.write("home/config.json", '{"autonomy": "full", "models": {"worker": "haiku"}}')
        c = oke.load_config()
        self.assertEqual(c["autonomy"], "full")
        self.assertEqual(c["models"], {"pm": "opus", "worker": "haiku", "reviewer": "opus"})
        self.assertFalse(c["wiki_path"].startswith("~"))

    def test_broken_state_is_set_aside(self):
        self.write("home/state.json", "{broken")
        with self.assertRaises(oke.StateError):
            oke.load_state()
        self.assertTrue(oke.load_state(recover=True)["_recovered"])
        self.assertTrue(os.path.exists(os.path.join(self.home, "state.json.bad")))

    def test_save_state_drops_recovered_flag(self):
        s = oke.load_state()
        s["_recovered"] = True
        oke.save_state(s)
        self.assertNotIn("_recovered", oke.load_state())

    def test_read_header(self):
        p = self.write("r.md", "type: done\nseq: 3\n---\n## Summary\n")
        self.assertEqual(oke.read_header(p), {"type": "done", "seq": 3})
        self.assertIsNone(oke.read_header(self.write("half.md", "type: do")))
        self.assertIsNone(oke.read_header(self.path("missing.md")))

    def test_read_header_value_with_colon(self):
        p = self.write("a.md", "cwd: C:/a b/wt\nseq: x\n---\n")
        self.assertEqual(oke.read_header(p), {"cwd": "C:/a b/wt", "seq": "x"})

    def test_repo_key_same_from_worktree_and_spaced_path(self):
        repo = self.git_repo("내 repo dir")
        wt = self.git_worktree(repo, "wt one")
        self.assertEqual(oke.repo_key(repo), oke.repo_key(wt))
        self.assertNotIn("\\", oke.repo_key(repo))

    def test_repo_key_outside_git_is_where_git_init_would_put_it(self):
        self.assertTrue(oke.repo_key(self.home).endswith("/home/.git"))
        with self.assertRaises(ValueError):
            oke.repo_key(self.path("missing"))

    def test_normalize_windows_forms(self):
        self.assertEqual(oke.normalize_path("/c/Users/X/r/.git", windows=True), "c:/users/x/r/.git")
        self.assertEqual(oke.normalize_path("C:\\Users\\X\\r\\.git\\", windows=True), "c:/users/x/r/.git")
        self.assertEqual(oke.normalize_path("/home/U/r/.git/", windows=False), "/home/U/r/.git")

    def test_slug(self):
        self.assertEqual(oke.slug("My.Repo_v2"), "my-repo-v2")
        self.assertEqual(oke.slug("9lives"), "x9lives")
        self.assertEqual(len(oke.slug("a" * 40)), 24)
        self.assertEqual(oke.slug("--a--b--"), "a-b")

    def test_the_wiki_is_one_folder_per_pm(self):
        root = oke.load_config()["wiki_path"]
        path = oke.wiki_dir("backend")
        self.assertEqual(os.path.normcase(path), os.path.normcase(os.path.join(root, "backend")))
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(oke.wiki_dir("backend"), path)
    def test_size_is_set_while_planning_and_only_grows_once_work_started(self):
        repo = self.git_repo("r")
        for job in ("tiny", "big"):
            self.assertEqual(self.cli("job", "new", job, "--repo", repo), 0)
            self.advance(job, "planning")
        self.assertEqual(self.cli("job", "size", "tiny", "small"), 0, self.err)
        self.assertNotEqual(self.cli("job", "size", "tiny", "huge"), 0)  # not a size
        self.assertEqual(oke.load_state()["jobs"]["tiny"]["size"], "small")
        for job in ("tiny", "big"):
            self.advance(job, "working")
        self.assertEqual(self.cli("job", "size", "big", "normal"), 1)  # no shrinking once work started
        self.assertEqual(self.cli("job", "size", "tiny", "normal"), 0, self.err)  # a size may grow
        self.advance("tiny", "done")
        self.assertEqual(self.cli("job", "size", "tiny", "large"), 1)  # not once done

    def test_an_old_lane_in_state_json_becomes_a_size(self):
        jobs = {"a": {"phase": "working", "lane": "trivial"}, "b": {"phase": "working", "lane": "small"},
                "c": {"phase": "working", "lane": "normal"}, "d": {"phase": "intake", "lane": "research"}}
        with io.open(oke.state_path(), "w", encoding="utf-8") as f:
            f.write(json.dumps({"jobs": jobs}))
        got = oke.load_state()["jobs"]
        self.assertEqual({k: v.get("size") for k, v in got.items()},
                         {"a": "small", "b": "normal", "c": "large", "d": "research"})
        self.assertFalse(any("lane" in v for v in got.values()))

    def test_review_rounds_stop_at_the_limit_until_the_user_allows_more(self):
        repo = self.git_repo("r")
        self.assertEqual(self.cli("job", "new", "j", "--repo", repo), 0)
        rv = os.path.join(self.home, "jobs", "j", "j-rv")
        os.makedirs(rv)
        with open(os.path.join(rv, "assignment.md"), "w") as f:
            f.write("agent: j-rv\nrole: reviewer\nseq: 11\n---\n")
        self.assertEqual(oke.review_round("j"), 11)
        info = oke.load_state()["jobs"]["j"]
        self.assertIsNone(oke.over_rounds("j", info, oke.load_config(), 10))
        self.assertIn("review round 11 is past its limit of 10", oke.over_rounds("j", info, oke.load_config(), 11))
        self.assertEqual(self.cli("job", "rounds", "j", "15"), 0, self.err)
        self.assertEqual(json.loads(self.out.strip().splitlines()[-1]), {"job": "j", "round": 11, "limit": 15})
        self.assertIsNone(oke.over_rounds("j", oke.load_state()["jobs"]["j"], oke.load_config(), 11))
        self.assertNotEqual(self.cli("job", "rounds", "j", "0"), 0)

    def test_a_revived_agents_cost_adds_to_what_its_folder_cost_before(self):
        d = os.path.join(self.home, "jobs", "j", "j-w1")
        os.makedirs(d)
        with open(os.path.join(d, "usage.json"), "w") as f:
            json.dump({"usd": 3.0}, f)
        oke.bank_usage(d)  # a new session's mod counts from zero
        with open(os.path.join(d, "usage.json"), "w") as f:
            json.dump({"usd": 1.0}, f)
        self.assertEqual(oke.folder_cost(d), 4.0)

    def test_job_new_phase_archive(self):
        repo = self.git_repo("r")
        self.assertEqual(self.cli("job", "new", "fix-a", "--repo", repo), 0)
        info = json.loads(self.out)
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["git_key"], oke.repo_key(repo))
        self.assertNotIn("repo", info)
        # seen live: an orchestrator used `repo` (the .git folder) as the PM's cwd
        self.assertEqual(oke.normalize_path(info["path"]), oke.normalize_path(repo))
        # seen live: a PM guessed `main` as the reviewer's base on a `master` repository
        branch = subprocess.check_output(["git", "-C", repo, "symbolic-ref", "--short", "HEAD"]).decode().strip()
        self.assertEqual(self.cli("status"), 0, self.err)
        self.assertEqual(json.loads(self.out)["jobs"]["fix-a"]["base"], branch)
        self.assertTrue(info["job_dir"].endswith("/jobs/fix-a"))
        self.assertTrue(info["pm_dir"].endswith("/pms/pm-r"))
        self.assertEqual(info["pm_agent"], "pm-r")
        self.assertTrue(info["wiki"].endswith("/wiki/pm-r"))
        self.assertTrue(info["shared_wiki"].endswith("/wiki/_shared"))
        self.assertEqual(self.cli("job", "new", "fix-a", "--repo", repo), 1)
        self.assertEqual(self.cli("job", "phase", "fix-a", "nonsense"), 1)
        self.assertEqual(self.cli("job", "phase", "nope", "planning"), 1)
        self.advance("fix-a", "planning")
        st = oke.load_state()
        self.assertEqual(st["jobs"]["fix-a"]["phase"], "planning")
        self.assertEqual(st["jobs"]["fix-a"]["git_key"], oke.repo_key(repo))
        self.assertTrue(os.path.isdir(os.path.join(self.home, "jobs", "fix-a")))
        os.makedirs(os.path.join(self.home, "jobs", "fix-a", "fix-a-w1"))
        for name in ("lean-settings.json", "report.md"):
            with open(os.path.join(self.home, "jobs", "fix-a", "fix-a-w1", name), "w") as f:
                f.write("{}")
        self.assertEqual(self.archive("fix-a"), 0)
        self.assertNotIn("fix-a", oke.load_state()["jobs"])
        self.assertEqual(len(os.listdir(os.path.join(self.home, "archive"))), 1)
        kept = os.path.join(self.home, "archive", os.listdir(os.path.join(self.home, "archive"))[0], "fix-a-w1")
        self.assertEqual(os.listdir(kept), ["report.md"])  # the settings copy held the user's env
        self.assertFalse(os.path.exists(os.path.join(self.home, "jobs", "fix-a")))

    def test_job_new_path_of_a_submodule_and_a_bare_repository(self):
        # a submodule's git common dir is <super>/.git/modules/<name>: its parent is no checkout
        self.git_repo("sub")
        sup = self.git_repo("sup")
        self.git(sup, "-c", "protocol.file.allow=always", "submodule", "-q", "add", self.path("sub"), "s")
        self.assertEqual(self.cli("job", "new", "fix-s", "--repo", os.path.join(sup, "s")), 0, self.err)
        info = json.loads(self.out)
        self.assertEqual(oke.normalize_path(info["path"]), oke.normalize_path(os.path.join(sup, "s")))
        self.assertTrue(oke.load_state()["jobs"]["fix-s"]["base"])
        main = oke.load_state()["jobs"]["fix-s"]
        self.assertEqual(oke.normalize_path(oke.check_dir(main)[0]), oke.normalize_path(os.path.join(sup, "s")))
        bare = self.path("b.git")
        self.git(self.home, "init", "-q", "--bare", bare)
        self.assertEqual(self.cli("job", "new", "fix-b", "--repo", bare), 1)
        self.assertIn("bare", self.err)
        self.assertNotIn("fix-b", oke.load_state()["jobs"])

    def test_a_name_whose_branch_exists_is_refused_when_its_plan_goes_to_the_user(self):
        # an archived job keeps its branch; reusing the name would review and deliver the old branch.
        # A job that only reads never makes one: the check waits until it may change the repository
        repo = self.git_repo("r")
        self.git(repo, "branch", "oke/fix-a")
        self.assertEqual(self.cli("job", "new", "fix-a", "--repo", repo), 0, self.err)
        self.advance("fix-a", "planning")
        self.assertEqual(self.cli("job", "phase", "fix-a", "awaiting_approval"), 1)
        self.assertIn("oke/fix-a", self.err)
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["phase"], "planning")

    def symlink_or_skip(self, target, link):
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("symlinks not available")

    def test_repo_key_same_through_a_symlink(self):
        repo = self.git_repo("real/r")
        self.symlink_or_skip(self.path("real"), self.path("link"))
        if os.path.realpath(self.path("link")) == self.path("link"):
            self.skipTest("realpath does not resolve symlinks here")
        self.assertEqual(oke.repo_key(os.path.join(self.path("link"), "r")), oke.repo_key(repo))

    def test_edit_rules_cover_the_resolved_path(self):
        os.makedirs(self.path("real"))
        self.symlink_or_skip(self.path("real"), self.path("link"))
        real = os.path.realpath(self.path("link"))
        if real == self.path("link"):
            self.skipTest("realpath does not resolve symlinks here")
        rules = oke.edit_rules(self.path("link"))
        for r in oke.edit_rules(real):
            self.assertIn(r, rules)
        self.assertIn(oke.edit_rules(self.path("link"))[0], rules)

    def test_job_new_rejects_bad_slug(self):
        repo = self.git_repo("r")
        self.assertEqual(self.cli("job", "new", "Bad Name", "--repo", repo), 1)

    def test_a_folder_outside_git_is_a_direct_job_with_no_branch(self):
        folder = self.path("plain")
        os.makedirs(folder)
        self.assertEqual(self.cli("job", "new", "calc", "--repo", folder), 0, self.err)
        self.assertTrue(json.loads(self.out)["direct"])
        self.assertEqual(oke.repo_key(folder), oke.load_state()["jobs"]["calc"]["git_key"])
        self.assertEqual(self.cli("job", "new", "x", "--repo", self.path("missing")), 1)
        self.advance("calc", "working")
        self.assertEqual(self.cli("job", "deliver", "calc"), 1)
        self.assertEqual(self.cli("job", "phase", "calc", "done"), 1)  # no wiki line yet
        self.logged("calc")
        self.assertEqual(self.cli("job", "phase", "calc", "done"), 0, self.err)

    def test_an_answer_skips_the_plan_and_the_delivery(self):
        repo = self.git_repo("r")
        self.assertEqual(self.cli("job", "new", "look", "--repo", repo), 0, self.err)
        self.assertNotIn("size", json.loads(self.out))  # no kind of job to pick at intake
        self.assertEqual(self.cli("job", "answer", "look"), 1)  # only while its PM investigates
        self.assertIn("phase planning", self.err)
        self.assertEqual(self.cli("interview", "look", "--goal", 1, "--constraints", 1, "--criteria", 1), 0)
        self.assertEqual(self.cli("job", "phase", "look", "planning"), 0, self.err)
        self.assertEqual(self.cli("job", "answer", "look"), 0, self.err)
        self.assertEqual(json.loads(self.out), {"job": "look", "answer": True})
        self.assertEqual(self.cli("job", "answer", "look"), 0, self.err)  # again: no change
        self.assertTrue(json.loads(self.out)["unchanged"])
        self.assertEqual(oke.job_output(oke.load_state()["jobs"]["look"]), "result")
        for refused in (("job", "size", "look", "small"), ("job", "deliver", "look"),
                        ("job", "phase", "look", "awaiting_approval"), ("job", "phase", "look", "working")):
            self.assertEqual(self.cli(*refused), 1, refused)
        self.assertEqual(oke.write_areas(oke.load_state(), "pm", oke.load_state()["jobs"]["look"]["pm"]), [])
        self.assertEqual(self.cli("job", "phase", "look", "done"), 1)  # the wiki log line first
        self.logged("look")
        self.assertEqual(self.cli("job", "phase", "look", "done"), 0, self.err)  # straight from planning

    def test_a_job_past_its_approval_is_no_answer(self):
        repo = self.git_repo("r")
        self.assertEqual(self.cli("job", "new", "fix", "--repo", repo), 0, self.err)
        self.assertEqual(self.cli("interview", "fix", "--goal", 1, "--constraints", 1, "--criteria", 1), 0)
        self.assertEqual(self.cli("job", "phase", "fix", "planning"), 0, self.err)
        st = oke.load_state()
        st["jobs"]["fix"]["approved"] = True  # approved once, then planned again
        oke.save_state(st)
        self.assertEqual(self.cli("job", "answer", "fix"), 1)
        self.assertIn("no-change question", self.err)
        st["jobs"]["fix"].update(approved=False, held=True)
        oke.save_state(st)
        self.assertEqual(self.cli("job", "answer", "fix"), 1)
        self.assertIn("on hold", self.err)
        self.assertEqual(self.cli("job", "phase", "fix", "done"), 1)  # a plan that is no answer: no shortcut

    def test_an_older_research_job_still_closes_without_a_plan(self):
        repo = self.git_repo("r")
        self.assertEqual(self.cli("job", "new", "look", "--repo", repo), 0, self.err)
        st = oke.load_state()
        st["jobs"]["look"].update(size="research", phase="working")  # started by an older OKe
        oke.save_state(st)
        self.assertEqual(self.cli("job", "deliver", "look"), 1)
        self.logged("look")
        self.assertEqual(self.cli("job", "phase", "look", "done"), 0, self.err)


def J(result):
    return {"stdout": json.dumps({"id": "x", "result": result}), "exit": 0}


def E(code, message="m"):
    # real herdr prints error JSON on stderr with an empty stdout
    return {"stderr": json.dumps({"id": "x", "error": {"code": code, "message": message}}), "exit": 1}


OK = {"stdout": "", "exit": 0}


class TestCost(Base):
    def csv(self, rel, *rows):
        path = os.path.join(self.home, *rel.split("/"))
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(u"time,model,input,output,cache_read,cache_write\n" + u"".join(r + u"\n" for r in rows))

    def test_cost_adds_up_turns_per_job_pm_and_orchestrator(self):
        self.csv("jobs/fix-a/fix-a-w1/usage.csv", "t,sonnet,1,2,3,4", "t,sonnet,10,20,30,40")
        self.csv("jobs/fix-a/fix-a-rv1/usage.csv", "t,opus,100,0,0,0", "broken line")
        self.csv("archive/fix-a-20261122-101010/fix-a-w1/usage.csv", "t,opus,5,5,5,5")
        self.csv("pms/backend/usage.csv", "t,opus,7,7,7,7")
        self.csv("usage.csv", "t,opus,9,9,9,9")
        self.assertEqual(self.cli("cost"), 0)
        out = json.loads(self.out)
        job = out["jobs/fix-a"]
        self.assertEqual(sorted(job.pop("models")), ["opus", "sonnet"])
        self.assertEqual(job, {"turns": 3, "input": 111, "output": 22, "cache_read": 33, "cache_write": 44})
        self.assertEqual(out["pms/backend"]["turns"], 1)
        self.assertEqual(out["orchestrator"]["input"], 9)
        self.assertEqual(self.cli("cost", "fix-a"), 0)
        self.assertEqual(sorted(json.loads(self.out)), ["archive/fix-a-20261122-101010", "jobs/fix-a"])


class TestHerdr(Base):
    def test_herdr_returns_result(self):
        self.fake({"agent list": [J({"agents": []})]})
        self.assertEqual(oke.herdr("agent", "list"), {"agents": []})

    def test_herdr_error_code(self):
        self.fake({"agent get": [E("agent_not_found")]})
        with self.assertRaises(oke.HerdrError) as c:
            oke.herdr("agent", "get", "x")
        self.assertEqual(c.exception.code, "agent_not_found")

    def test_herdr_error_on_stdout_also_parsed(self):
        self.fake({"agent get": [{"stdout": json.dumps({"error": {"code": "agent_not_found", "message": "m"}}), "exit": 1}]})
        with self.assertRaises(oke.HerdrError) as c:
            oke.herdr("agent", "get", "x")
        self.assertEqual(c.exception.code, "agent_not_found")

    def test_herdr_text_error_on_stderr(self):
        self.fake({"agent read": [E("agent_not_idle")]})
        with self.assertRaises(oke.HerdrError) as c:
            oke.herdr_text("agent", "read", "x")
        self.assertEqual(c.exception.code, "agent_not_idle")

    def test_herdr_garbage_is_unavailable(self):
        self.fake({"agent list": [{"stdout": "connection refused", "exit": 1}]})
        with self.assertRaises(oke.HerdrError) as c:
            oke.herdr("agent", "list")
        self.assertEqual(c.exception.code, "herdr_unavailable")

    def test_herdr_missing_binary_is_unavailable(self):
        os.environ["OKE_HERDR"] = json.dumps([self.path("no-such-herdr")])
        with self.assertRaises(oke.HerdrError) as c:
            oke.herdr("agent", "list")
        self.assertEqual(c.exception.code, "herdr_unavailable")

    def test_herdr_text(self):
        self.fake({"agent read": [{"stdout": "line one\nline two\n", "exit": 0}]})
        self.assertEqual(oke.herdr_text("agent", "read", "a"), "line one\nline two\n")

    def test_missing_commands(self):
        self.fake({"agent start --help": [OK], "agent prompt --help": [OK],
                   "agent wait --help": [{"stdout": "", "exit": 2}], "agent read --help": [OK],
                   "agent send-keys --help": [OK], "pane split --help": [OK],
                   "tab create --help": [OK]})
        self.assertEqual(runtime_herdr.missing_commands(), ["agent wait"])


def AG(name="a", status="idle", pane="w1:p9", session="s-1", cwd=None):
    a = {"agent": "claude", "agent_status": status, "pane_id": pane}
    if name:
        a["name"] = name
    if session:
        a["agent_session"] = {"value": session}
    if cwd:
        a["cwd"] = cwd
    return a


def AGENT(status="idle", **kw):
    return J({"agent": AG(status=status, **kw), "type": "agent_info"})


def spawn_ok(agents=()):
    return {"agent list": [J({"agents": list(agents)})],
            "pane split": [J({"pane": {"pane_id": "w1:p9"}})],
            "agent start": [AGENT()],
            "agent prompt": [J({"type": "agent_prompted"})],
            "agent get": [AGENT("working")]}


class SpawnBase(Base):
    def setUp(self):
        Base.setUp(self)
        self.repo = self.git_repo("toy")
        runtime_herdr.DIALOG_POLL = 0
        runtime_herdr.PROMPT_POLL = 0
        self.pm = "pm-toy"

    def job(self, name, phase):
        self.assertEqual(self.cli("job", "new", name, "--repo", self.repo), 0)
        self.advance(name, phase)

    def assignment(self, agent, role, job=None, seq=1, cwd=None):
        if role == "pm":  # a PM runs in its own folder and owns the test repository
            st = oke.load_state()
            if agent not in st["pms"]:
                put_pm(st, oke.repo_key(self.repo), {"agent": agent})
                st["pms"][agent]["pm"] = {}
                oke.save_state(st)
            cwd = cwd or oke.pm_dir(agent)
        lines = ["agent: " + agent, "role: " + role, "cwd: " + (cwd or self.repo), "seq: %d" % seq]
        if job:
            lines.append("job: " + job)
        return self.write("asg/%s.md" % agent, "\n".join(lines) + "\n---\n## Goal\n")

    def children(self):
        with io.open(oke.children_path(self.pm), encoding="utf-8") as f:
            return json.load(f)

    def argv_of(self, *prefix):
        return [c for c in self.calls() if c[:len(prefix)] == list(prefix)]


class TestSpawn(SpawnBase):
    def test_spawn_worker_refused_before_approval(self):
        self.job("fix-a", "awaiting_approval")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        self.fake({})
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p2"), 1)
        self.assertIn("plan not approved", self.err)
        self.assertEqual(self.calls(), [])

    def test_spawn_survives_a_children_file_in_list_form(self):
        # live run: a PM rewrote children.json as a list, and the next spawn crashed with TypeError
        self.job("fix-a", "working")
        oke.write_json_atomic(oke.children_path(self.pm),
                              [{"agent": "fix-a-w1", "job": "fix-a", "pane": "w1:p5"}])
        a = self.assignment("fix-a-rv", "reviewer", "fix-a")
        self.fake(spawn_ok())
        self.assertEqual(self.cli("spawn", a, "--split-from", "w1:p1"), 0)
        kids = self.children()
        self.assertEqual(kids["fix-a-rv"]["pane"], "w1:p9")
        self.assertEqual(kids["fix-a-w1"]["pane"], "w1:p5")

    def test_spawn_submits_a_first_prompt_left_in_the_input(self):
        # live run: the prompt text was typed but its Enter was lost, so the agent sat idle
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        sc = spawn_ok()
        sc["agent prompt"] = [E("timeout")]
        sc["agent get"] = [AGENT("idle"), AGENT("working")]
        sc["agent send-keys"] = [J({"type": "ok"})]
        sc["agent wait"] = [AGENT("working")]
        self.fake(sc)
        self.assertEqual(self.cli("spawn", a, "--split-from", "w1:p1"), 0)
        self.assertEqual(self.argv_of("agent", "send-keys"), [["agent", "send-keys", "fix-a-w1", "enter"]])

    def test_spawn_order_and_record(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a", seq=4)
        self.fake(spawn_ok())
        self.assertEqual(self.cli("spawn", a, "--split-from", "w1:p1"), 0)
        self.assertEqual([c[:2] for c in self.calls()],
                         [["agent", "list"], ["pane", "split"], ["agent", "start"], ["agent", "read"],
                          ["agent", "prompt"], ["agent", "get"], ["agent", "read"], ["pane", "get"]])
        rec = self.children()["fix-a-w1"]
        self.assertEqual((rec["session"], rec["pane"], rec["seq"], rec["job"], rec["role"]),
                         ("s-1", "w1:p9", 4, "fix-a", "worker"))
        self.assertEqual(json.loads(self.out), {"agent": "fix-a-w1", "pane": "w1:p9", "session": "s-1"})
        prompt = self.argv_of("agent", "prompt")[0]
        self.assertEqual(prompt[2], "fix-a-w1")
        self.assertIn("oke:worker", prompt[3])
        self.assertIn(a.replace("\\", "/"), prompt[3])
        start = self.argv_of("agent", "start")[0]
        self.assertEqual(start[start.index("--kind") + 1], "claude")

    def test_a_report_with_a_byte_order_mark_is_read(self):
        path = self.write("r/report.md", u"﻿type: done\nseq: 1\n---\n")
        self.assertEqual(oke.read_header(path), {"type": "done", "seq": 1})

    def test_spawn_pm_records_in_state(self):
        a = self.assignment("pm-toy", "pm")
        self.fake(spawn_ok())
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 0)
        self.assertEqual(self.argv_of("pane", "split"), [])
        pm = oke.load_state()["pms"]["pm-toy"]["pm"]
        self.assertEqual((pm["agent"], pm["pane"], pm["session"]), ("pm-toy", "w1:p9", "s-1"))

    def test_a_pm_removed_while_it_starts_has_its_pane_closed(self):
        a = self.assignment("pm-toy", "pm")
        self.fake(dict(spawn_ok(), **{"pane close": [J({"type": "ok"})]}))
        cls = type(oke.runtime())
        real = cls.start

        def start(rt, *args, **kw):
            st = oke.load_state()
            st["pms"].pop("pm-toy")  # `pm remove` meanwhile
            oke.save_state(st)
            return real(rt, *args, **kw)
        cls.start = start
        try:
            self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 1)
        finally:
            cls.start = real
        self.assertIn("was removed while it started: closed it", self.err)
        self.assertEqual(self.argv_of("pane", "close"), [["pane", "close", "w1:p9"]])

    def test_spawn_pm_refuses_an_old_per_job_assignment(self):
        # before slice 3 the PM folder held one job's instruction; it is no standing assignment
        a = self.assignment("pm-toy", "pm", job="fix-a")
        self.fake(spawn_ok())
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 1)
        self.assertEqual(self.argv_of("agent", "start"), [])

    def test_spawn_name_in_use(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        self.fake(spawn_ok(agents=[AG(name="fix-a-w1")]))
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p2"), 1)
        self.assertIn("in use", self.err)
        self.assertEqual(self.argv_of("agent", "start"), [])

    def test_spawn_rejects_bad_agent_name(self):
        self.job("fix-a", "working")
        a = self.assignment("Fix_A W1", "worker", "fix-a")
        self.fake(spawn_ok())
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p2"), 1)
        self.assertEqual(self.calls(), [])

    def test_spawn_missing_header_field(self):
        a = self.write("asg/x.md", "agent: x\nrole: worker\n---\n")
        self.fake(spawn_ok())
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p2"), 1)
        self.assertEqual(self.calls(), [])

    def test_spawn_accepts_trust_dialog_for_known_repo(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        s = spawn_ok()
        s["agent start"] = [E("agent_not_ready")]
        s["agent read"] = [{"stdout": " ❯ No, exit\n   Yes, I trust this folder\n", "exit": 0}]
        s["agent send-keys"] = [J({"type": "ok"})]
        s["agent wait"] = [AGENT()]
        s["pane get"] = [PANE(self.repo)]
        self.fake(s)
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 0)
        self.assertEqual(self.argv_of("agent", "send-keys"), [["agent", "send-keys", "fix-a-w1", "down", "enter"]])

    def test_spawn_waits_for_trust_dialog_to_render(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        s = spawn_ok()
        s["agent start"] = [E("agent_not_ready")]
        s["agent read"] = [{"stdout": "PS C:/x> claude\n", "exit": 0},
                           {"stdout": " Accessing workspace:\n", "exit": 0},
                           {"stdout": "   Yes, I trust this folder\n", "exit": 0}]
        s["agent send-keys"] = [J({"type": "ok"})]
        s["agent wait"] = [AGENT()]
        s["pane get"] = [PANE(self.repo)]
        self.fake(s)
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 0)
        self.assertEqual(len(self.argv_of("agent", "send-keys")), 1)

    def test_spawn_waits_for_the_first_turn_to_start(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        self.fake(spawn_ok())
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 0)
        prompt = self.argv_of("agent", "prompt")[0]
        self.assertEqual(prompt[4:], ["--wait", "--until", "working", "--until", "blocked", "--timeout", "20000"])

    def test_spawn_types_a_lost_first_prompt_again(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        s = spawn_ok()
        s["agent read"] = [{"stdout": " Claude Code\n > \n", "exit": 0}]  # no prompt on screen
        s["agent get"] = [AGENT("working"), AGENT("idle")]
        self.fake(s)
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 0)
        self.assertEqual(len(self.argv_of("agent", "prompt")), 3)  # the first and two more, then it stops

    def test_spawn_does_not_take_an_earlier_copy_of_the_prompt_for_this_one(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        prompt = oke.FIRST_PROMPT.format(role="worker", path=os.path.abspath(a))
        s = spawn_ok()
        s["agent read"] = [{"stdout": " > " + prompt + "\n", "exit": 0}]  # a resumed session's old one
        s["agent get"] = [AGENT("working"), AGENT("idle")]
        self.fake(s)
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 0)
        self.assertEqual(len(self.argv_of("agent", "prompt")), 3)

    def test_spawn_does_not_type_the_prompt_again_into_a_busy_agent(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        s = spawn_ok()  # herdr says working throughout
        s["agent read"] = [{"stdout": " Claude Code\n", "exit": 0}]
        s["agent prompt"] = [J({"type": "agent_prompted"}), E("agent_not_idle")]
        self.fake(s)
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 0, self.err)
        self.assertEqual(len(self.argv_of("agent", "prompt")), 1)

    def test_spawn_types_a_first_prompt_on_screen_once(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        prompt = oke.FIRST_PROMPT.format(role="worker", path=os.path.abspath(a))
        s = spawn_ok()
        # claude wraps the prompt over lines on a narrow pane
        s["agent read"] = [{"stdout": " Claude Code\n", "exit": 0},  # before it is typed
                           {"stdout": " > " + prompt[:30] + "\n   " + prompt[30:] + "\n", "exit": 0}]
        s["agent get"] = [AGENT("working"), AGENT("idle")]  # a retype would be allowed
        self.fake(s)
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 0)
        self.assertEqual(len(self.argv_of("agent", "prompt")), 1)

    def test_spawn_survives_a_stalled_first_prompt(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        s = spawn_ok()
        s["agent prompt"] = [E("agent_prompt_stalled")]
        self.fake(s)
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 0)
        self.assertIn("fix-a-w1", self.children())

    def test_spawn_refuses_trust_dialog_outside_known_repo(self):
        self.job("fix-a", "working")
        other = self.git_repo("stranger")
        a = self.assignment("fix-a-w1", "worker", "fix-a", cwd=other)
        s = spawn_ok()
        s["agent start"] = [E("agent_not_ready")]
        s["agent read"] = [{"stdout": "Yes, I trust this folder\n", "exit": 0}]
        self.fake(s)
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 1)
        self.assertEqual(self.argv_of("agent", "send-keys"), [])

    def test_spawn_fails_on_unknown_dialog(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        s = spawn_ok()
        s["agent start"] = [E("agent_not_ready")]
        s["agent read"] = [{"stdout": "Select login method\n", "exit": 0}]
        s["pane get"] = [J({"pane": {"terminal_id": "t-9"}})]
        self.fake(s)
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 1)
        self.assertIn("Select login method", self.err)
        self.assertEqual(self.argv_of("agent", "send-keys"), [])
        # the user may answer it: a revive must be able to close this pane
        self.assertEqual(self.children()["fix-a-w1"]["terminal"], "t-9")

    def test_spawn_reports_herdr_error_as_exit_1(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        s = spawn_ok()
        s["pane split"] = [E("pane_not_found", "pane w1:p1 not found")]
        self.fake(s)
        self.assertEqual(self.cli("spawn", a, "--split-from", "w1:p1"), 1)
        self.assertIn("pane_not_found", self.err)

    def test_launch_args_by_autonomy(self):
        c = dict(oke.load_config(), autonomy="conservative")
        self.assertIn("manual", oke.launch_args("worker", c, "/h", "/p"))
        c["autonomy"] = "balanced"
        args = oke.launch_args("worker", c, "/h", "/p")
        self.assertIn("bypassPermissions", args)
        self.assertEqual(args[args.index("--model") + 1], "opus")
        self.assertNotIn("--bare", args)
        r = oke.launch_args("reviewer", c, "/h", "/p")
        self.assertNotIn("bypassPermissions", r)
        self.assertIn("dontAsk", r)
        self.assertNotIn("Write", r)
        self.assertNotIn("Bash", r)

    def test_lean_launch_keeps_the_users_deny_rules_and_guard_hooks(self):
        conf = os.path.join(self.home, "claude-conf")
        os.makedirs(conf)
        guard = {"matcher": "Bash", "hooks": [{"type": "command", "command": "node guard.mjs"}]}
        with open(os.path.join(conf, "settings.json"), "w") as f:
            json.dump({"hooks": {"PreToolUse": [guard]},
                       "permissions": {"deny": ["Read(~/.ssh/**)"], "ask": ["Bash(npm publish:*)"]}}, f)
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = conf
        try:
            args = oke.launch_args("worker", oke.load_config(), self.home, "/p")
        finally:
            if old is None:
                del os.environ["CLAUDE_CONFIG_DIR"]
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old
        path = args[args.index("--settings") + 1]
        with open(path) as f:
            lean = json.load(f)
        self.assertEqual(lean["permissions"]["deny"], ["Read(~/.ssh/**)"])
        self.assertNotIn("ask", lean["permissions"])
        self.assertEqual(lean["hooks"]["PreToolUse"], [guard])
        if os.name != "nt":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_lean_launch_keeps_only_herdr_hooks_and_env(self):
        conf = os.path.join(self.home, "claude-conf")
        os.makedirs(conf)
        herdr_group = {"matcher": "startup", "hooks": [{"type": "command", "command": "pwsh herdr-agent-state.ps1"}]}
        other = {"hooks": [{"type": "command", "command": "node caveman.mjs"}]}
        with open(os.path.join(conf, "settings.json"), "w") as f:
            json.dump({"hooks": {"SessionStart": [other, herdr_group], "Stop": [other]},
                       "env": {"PYTHONUTF8": "1"}, "enabledPlugins": {"x@y": True},
                       "autoCompactWindow": 400000, "effortLevel": "high",
                       "modelSettings": {"claude-opus-5-5": {"effortLevel": "medium"}}}, f)
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = conf
        try:
            c = oke.load_config()
            args = oke.launch_args("reviewer", c, self.home, "/p")
        finally:
            if old is None:
                del os.environ["CLAUDE_CONFIG_DIR"]
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old
        self.assertEqual(args[args.index("--setting-sources") + 1], "project,local")
        self.assertIn("--strict-mcp-config", args)
        # before --allowedTools, which takes every argument after it
        self.assertLess(args.index("--settings"), args.index("--allowedTools"))
        with open(args[args.index("--settings") + 1]) as f:
            lean = json.load(f)
        self.assertEqual(lean["hooks"], {"SessionStart": [herdr_group]})
        self.assertEqual(lean["autoCompactWindow"], 400000)
        # the user's effort does not reach children: a user's max would run them all at max
        self.assertNotIn("effortLevel", lean)
        self.assertNotIn("modelSettings", lean)
        self.assertEqual(args[args.index("--model") + 1], "opus")
        picked = oke.launch_args("worker", c, self.home, "/p", header={"model": "sonnet"})
        self.assertEqual(picked[picked.index("--model") + 1], "sonnet")  # the job's assignment chose it
        self.assertEqual(lean["env"], {"PYTHONUTF8": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                                       "OKE_HOME": oke.oke_home()})
        # the OKe folders come from the settings file: --add-dir stops at a trust dialog
        self.assertEqual(lean["permissions"]["additionalDirectories"], [self.home, c["wiki_path"]])
        self.assertNotIn("--add-dir", args)
        plain = oke.launch_args("worker", dict(c, lean_launch=False), self.home, "/p")
        self.assertNotIn("--setting-sources", plain)
        self.assertEqual(plain.count("--add-dir"), 2)
        # the reviewer has the write tools for its report, but no bare Edit/Write allow rule:
        # only the OKe folders' Edit(...) rules let it write
        self.assertTrue({"Edit", "Write"} <= set(args[args.index("--tools") + 1].split(",")))
        allowed = args[args.index("--allowedTools") + 1:args.index("--disallowedTools")]
        self.assertFalse({"Edit", "Write"} & set(allowed))
        self.assertTrue(any(a.startswith("Edit(") for a in allowed))
        for role in ("pm", "worker", "reviewer"):
            tools = oke.launch_args(role, c, self.home, "/p")
            self.assertTrue({"WebFetch", "WebSearch"} <= set(tools[tools.index("--tools") + 1].split(",")))
        w = oke.launch_args("worker", c, self.home, "/p")
        self.assertIn("Agent", w[w.index("--tools") + 1].split(","))
        # a lean child does not see the user's bypass acceptance: no warning dialog
        self.assertNotIn("bypassPermissions", w)
        self.assertEqual(w[w.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(w[w.index("--allowedTools") + 1:w.index("--disallowedTools")], w[w.index("--tools") + 1].split(","))
        full = oke.launch_args("worker", dict(c, lean_launch=False), self.home, "/p")
        self.assertIn("bypassPermissions", full)

    def test_assignment_header_switches_on_tools_mcp_and_plugins(self):
        conf = os.path.join(self.home, "claude-conf")
        os.makedirs(conf)
        with open(os.path.join(conf, ".claude.json"), "w") as f:
            json.dump({"mcpServers": {"codegraph": {"command": "codegraph"}, "big": {"command": "big"}}}, f)
        with open(os.path.join(conf, "settings.json"), "w") as f:
            json.dump({"enabledPlugins": {"sp@m": True, "off@m": False}}, f)
        folder = os.path.join(self.home, "child")
        os.makedirs(folder)
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = conf
        try:
            header = {"tools": "WebFetch, Bash", "mcp": "codegraph, nope", "plugins": "sp@m"}
            args = oke.launch_args("worker", oke.load_config(), self.home, "/p", folder=folder, header=header)
            self.assertEqual(oke.user_plugins(), ["sp@m"])
        finally:
            if old is None:
                del os.environ["CLAUDE_CONFIG_DIR"]
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old
        tools = args[args.index("--tools") + 1].split(",")
        self.assertIn("WebFetch", tools)
        self.assertEqual(tools.count("Bash"), 1)
        with open(args[args.index("--mcp-config") + 1]) as f:
            self.assertEqual(json.load(f), {"mcpServers": {"codegraph": {"command": "codegraph"}}})
        with open(args[args.index("--settings") + 1]) as f:
            self.assertEqual(json.load(f)["enabledPlugins"], {"sp@m": True})

    def test_conservative_agents_can_still_write_their_own_files(self):
        c = dict(oke.load_config(), autonomy="conservative", wiki_path="/w")
        w = oke.launch_args("worker", c, "/h", "/p")
        allowed = w[w.index("--allowedTools") + 1:w.index("--disallowedTools")]
        self.assertIn(oke.edit_rules("/h")[0], allowed)
        self.assertFalse([t for t in allowed if t.startswith("Bash")])
        pm = oke.launch_args("pm", c, "/h", "/p")
        allowed = pm[pm.index("--allowedTools") + 1:pm.index("--disallowedTools")]
        self.assertIn(oke.edit_rules("/w")[0], allowed)
        self.assertIn("Bash(herdr:*)", allowed)
        self.assertIn("Bash(git:*)", allowed)

    def test_edit_rules(self):
        self.assertEqual(oke.edit_rules("/home/u/.oke", windows=False), ["Edit(//home/u/.oke/**)"])
        self.assertEqual(oke.edit_rules("C:\\Users\\u\\.oke", windows=True),
                         ["Edit(//c/Users/u/.oke/**)", "Edit(//C:/Users/u/.oke/**)"])

    def test_spawn_resume_passes_session(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        self.fake(spawn_ok())
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9", "--resume", "s-9"), 0)
        start = self.argv_of("agent", "start")[0]
        self.assertEqual(start[start.index("--resume") + 1], "s-9")
        self.assertLess(start.index("--"), start.index("--resume"))

    def test_spawn_cwd_with_space_is_one_argv(self):
        repo = self.git_repo("a b 한글")
        self.assertEqual(self.cli("job", "new", "fix-s", "--repo", repo), 0)
        self.advance("fix-s", "working")
        a = self.assignment("fix-s-w1", "worker", "fix-s", cwd=repo)
        self.fake(spawn_ok())
        self.assertEqual(self.cli("spawn", a, "--split-from", "w1:p1"), 0)
        split = self.argv_of("pane", "split")[0]
        self.assertEqual(split[split.index("--cwd") + 1], repo)


class TestWait(Base):
    def report(self, text):
        return self.write("report.md", text)

    def test_report_done(self):
        r = self.report("type: done\nseq: 2\n---\n")
        self.fake({})
        self.assertEqual(oke.check_once("a", r, 2), "report done")
        self.assertEqual(self.calls(), [])

    def test_old_seq_while_working_continues(self):
        r = self.report("type: done\nseq: 1\n---\n")
        self.fake({"agent get": [AGENT("working")]})
        self.assertIsNone(oke.check_once("a", r, 2))

    def test_half_written_while_working_is_not_bad(self):
        r = self.report("type: do")
        self.fake({"agent get": [AGENT("working")]})
        self.assertIsNone(oke.wait_loop("a", r, 2, poll=0, max_polls=5))

    def test_unknown_status_continues(self):
        self.fake({"agent get": [AGENT("unknown")]})
        self.assertIsNone(oke.wait_loop("a", self.path("none.md"), 2, poll=0, max_polls=5))

    def test_waiting_keeps_waiting(self):
        r = self.report("type: waiting\nseq: 2\non: fix-a-w1\n---\n")
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(oke.wait_loop("a", r, 2, poll=0, max_polls=5))

    def test_waiting_with_old_seq_is_no_report(self):
        r = self.report("type: waiting\nseq: 1\n---\n")
        self.fake({"agent get": [AGENT("done")]})
        self.assertEqual(oke.wait_loop("a", r, 2, poll=0, max_polls=3), "idle_no_report")

    def test_idle_no_report_needs_three_polls(self):
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(oke.wait_loop("a", self.path("none.md"), 2, poll=0, max_polls=2))
        self.assertEqual(oke.wait_loop("a", self.path("none.md"), 2, poll=0, max_polls=3), "idle_no_report")

    def test_idle_then_working_resets_count(self):
        self.fake({"agent get": [AGENT("idle"), AGENT("idle"), AGENT("working"), AGENT("idle"), AGENT("idle")]})
        self.assertIsNone(oke.wait_loop("a", self.path("none.md"), 2, poll=0, max_polls=5))

    def test_bad_report_when_idle(self):
        r = self.report("typo: x\nseq: 2\n---\n")
        self.fake({"agent get": [AGENT("idle")]})
        self.assertTrue(oke.wait_loop("a", r, 2, poll=0, max_polls=3).startswith("bad_report"))

    def test_bad_type_when_idle(self):
        r = self.report("type: finished\nseq: 2\n---\n")
        self.fake({"agent get": [AGENT("idle")]})
        self.assertTrue(oke.wait_loop("a", r, 2, poll=0, max_polls=3).startswith("bad_report"))

    def test_report_arrives_after_idle_polls(self):
        r = self.path("report.md")
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(oke.wait_loop("a", r, 2, poll=0, max_polls=2))
        self.report("type: question\nseq: 2\n---\n")
        self.assertEqual(oke.wait_loop("a", r, 2, poll=0, max_polls=1), "report question")

    def test_blocked_includes_screen(self):
        self.fake({"agent get": [AGENT("blocked")],
                   "agent read": [{"stdout": "Do you want to create r.md?\n", "exit": 0}]})
        out = oke.check_once("a", self.path("none.md"), 2)
        self.assertEqual(out.splitlines()[0], "blocked")
        self.assertIn("Do you want to create r.md?", out)

    def test_a_report_written_while_the_child_read_blocked_comes_first(self):
        r = self.path("report.md")
        original = oke.look_at

        def blocked_then_report(*a, **kw):
            self.report("type: done\nseq: 2\n---\n")
            return "blocked\nAllow? 1. Yes"
        oke.look_at = blocked_then_report
        try:
            self.assertEqual(oke.check_once("a", r, 2), "report done")
        finally:
            oke.look_at = original

    def test_gone(self):
        self.fake({"agent get": [E("agent_not_found")]})
        self.assertEqual(oke.check_once("a", self.path("none.md"), 2), "gone")

    def test_gone_when_agent_dies_before_read(self):
        self.fake({"agent get": [AGENT("blocked")], "agent read": [E("agent_not_found")]})
        self.assertEqual(oke.check_once("a", self.path("none.md"), 2), "gone")

    def test_wait_survives_a_short_herdr_outage(self):
        down = {"stdout": "connection refused", "exit": 1}
        self.fake({"agent get": [down, down, AGENT("blocked")],
                   "agent read": [{"stdout": "Do you want to proceed?", "exit": 0}]})
        out = oke.wait_loop("a", self.path("none.md"), 2, poll=0, max_polls=10)
        self.assertEqual(out.splitlines()[0], "blocked")

    def test_herdr_down_is_exit_2_not_gone(self):
        self.addCleanup(setattr, oke, "HERDR_RETRIES", oke.HERDR_RETRIES)
        oke.HERDR_RETRIES = 2
        self.fake({"agent get": [{"stdout": "connection refused", "exit": 1}]})
        code = self.cli("wait", "a", "--report", self.path("none.md"), "--seq", "2", "--poll", "0")
        self.assertEqual(code, 2)
        self.assertNotIn("gone", self.out)

    def test_cli_prints_result(self):
        r = self.report("type: failed\nseq: 7\n---\n")
        self.fake({})
        self.assertEqual(self.cli("wait", "a", "--report", r, "--seq", "7"), 0)
        self.assertEqual(self.out.strip(), "report failed")

    def test_bg_waits_look_every_10_s(self):
        # each bg look runs a node program; herdr's is cheap
        polls = []
        self.addCleanup(setattr, oke, "wait_loop", oke.wait_loop)
        oke.wait_loop = lambda *a, **k: polls.append(k["poll"])
        for runtime, extra in (("herdr", []), ("bg", []), ("bg", ["--poll", "3"])):
            os.environ["OKE_RUNTIME"] = runtime
            self.assertEqual(self.cli("wait", "a", "--report", "r.md", "--seq", "1", *extra), 0, self.err)
        self.assertEqual(polls, [2, 10, 3])


def help_ok():
    return dict((c + " --help", [OK]) for c in runtime_herdr.REQUIRED_COMMANDS)


class TestStatus(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        os.environ["HERDR_ENV"] = "1"
        os.environ["HERDR_PANE_ID"] = "w7:p1"
        self.key = oke.repo_key(self.repo)

    def set_pm(self, agent="pm-toy", pane="w1:p3", session="s-pm"):
        s = oke.load_state()
        put_pm(s, self.key, {"agent": agent, "pane": pane, "session": session, "seq": 1})
        oke.save_state(s)

    def status(self, agents=(), *flags):
        s = help_ok()
        s["agent list"] = [J({"agents": list(agents)})]
        self.fake(s)
        self.code = self.cli("status", *flags)
        return json.loads(self.out) if self.out.strip() else None

    def pm_record(self):
        return oke.load_state()["pms"][self.pm]["pm"]

    def test_outside_herdr(self):
        os.environ.pop("HERDR_ENV", None)
        self.set_pm()
        self.fake({})  # herdr unreachable: the state still shows, liveness unknown
        self.assertEqual(self.cli("status"), 0)
        st = json.loads(self.out)
        self.assertEqual(st["herdr"], {"inside": False, "missing": []})
        self.assertEqual(st["pms"][self.pm]["pm"], "unknown")

    def test_outside_herdr_shows_state_and_never_claims(self):
        os.environ.pop("HERDR_ENV", None)
        self.set_pm()
        st = self.status([AG(name="pm-toy", pane="w1:p3", session="s-pm", cwd=oke.pm_dir(self.pm))], "--claim")
        self.assertEqual((st["herdr"]["inside"], st["pms"][self.pm]["pm"]), (False, "alive"))
        self.assertIsNone(oke.load_state()["orchestrator"].get("pane"))

    def test_missing_commands_reported(self):
        s = help_ok()
        s["agent wait --help"] = [{"stdout": "", "exit": 2}]
        s["agent list"] = [J({"agents": []})]
        self.fake(s)
        self.cli("status")
        self.assertEqual(json.loads(self.out)["herdr"], {"inside": True, "missing": ["agent wait"]})

    def test_pm_found_by_name_after_pane_move(self):
        self.set_pm()
        st = self.status([AG(name="pm-toy", pane="w9:p1", session="other", cwd=oke.pm_dir(self.pm))])
        repo = st["pms"][self.pm]
        self.assertEqual((repo["pm"], repo["agent"], repo["pane"]), ("alive", "pm-toy", "w9:p1"))
        self.assertEqual(self.pm_record()["pane"], "w9:p1")

    def test_status_fills_in_missing_session(self):
        self.set_pm(session=None)
        self.status([AG(name="pm-toy", pane="w1:p3", session="s-late", cwd=oke.pm_dir(self.pm))])
        self.assertEqual(self.pm_record()["session"], "s-late")

    def test_pm_found_by_pane_after_rename(self):
        self.set_pm()
        st = self.status([AG(name="renamed", pane="w1:p3", session="other", cwd=oke.pm_dir(self.pm))])
        self.assertEqual(st["pms"][self.pm]["pm"], "alive")
        self.assertEqual(self.pm_record()["agent"], "renamed")

    def test_pm_found_by_session(self):
        self.set_pm()
        st = self.status([AG(name="x", pane="w5:p5", session="s-pm", cwd=oke.pm_dir(self.pm))])
        self.assertEqual(st["pms"][self.pm]["pm"], "alive")
        self.assertEqual((self.pm_record()["agent"], self.pm_record()["pane"]), ("x", "w5:p5"))

    def test_pm_in_other_repo_cwd_is_dead(self):
        self.set_pm()
        other = self.git_repo("other")
        st = self.status([AG(name="pm-toy", pane="w1:p3", session="s-pm", cwd=other)])
        self.assertEqual(st["pms"][self.pm]["pm"], "dead")
        self.assertEqual(self.pm_record()["pane"], "w1:p3")

    def test_pm_missing_is_dead(self):
        self.set_pm()
        self.assertEqual(self.status([])["pms"][self.pm]["pm"], "dead")

    def test_job_classes(self):
        self.set_pm()
        self.job("j-work", "working")
        self.job("j-plan", "awaiting_approval")
        oke.write_json_atomic(oke.children_path(self.pm), {
            "j-work-w1": {"agent": "j-work-w1", "job": "j-work", "pane": "w2:p1"},
            "j-work-rv": {"agent": "j-work-rv", "job": "j-work", "pane": "w2:p2"}})
        alive = self.status([AG(name="pm-toy", pane="w1:p3", cwd=oke.pm_dir(self.pm)), AG(name="j-work-w1", pane="w2:p1")])
        work = alive["jobs"]["j-work"]
        self.assertEqual((work["phase"], work["class"], work["agents"]),
                         ("working", "alive", {"j-work-w1": "alive", "j-work-rv": "dead"}))
        self.assertEqual(alive["jobs"]["j-plan"]["class"], "awaiting_user")
        dead = self.status([])
        self.assertEqual(dead["jobs"]["j-work"]["class"], "disconnected")
        self.assertEqual(dead["jobs"]["j-plan"]["class"], "awaiting_user")

    def test_orphan_job_dir(self):
        os.makedirs(os.path.join(self.home, "jobs", "old-job"))
        self.job("fix-a", "working")
        self.assertEqual(self.status([])["orphan_job_dirs"], ["old-job"])

    def test_orchestrator_conflict(self):
        s = oke.load_state()
        s["orchestrator"] = {"pane": "w8:p1"}
        oke.save_state(s)
        st = self.status([AG(name=None, pane="w8:p1")])
        self.assertEqual(st["orchestrator_conflict"], "w8:p1")
        self.status([AG(name=None, pane="w8:p1")], "--claim")
        self.assertEqual(self.code, 1)
        self.assertEqual(oke.load_state()["orchestrator"]["pane"], "w8:p1")

    def test_another_agent_in_a_reused_pane_is_no_conflict(self):
        # pane ids come back after a herdr restart: a different session there is not us
        s = oke.load_state()
        s["orchestrator"] = {"pane": "w8:p1", "session": "s-orch"}
        oke.save_state(s)
        st = self.status([AG(name=None, pane="w8:p1", session="s-other")])
        self.assertIsNone(st["orchestrator_conflict"])
        st = self.status([AG(name=None, pane="w8:p1", session="s-orch")])
        self.assertEqual(st["orchestrator_conflict"], "w8:p1")

    def test_claim_takes_over_dead_orchestrator(self):
        s = oke.load_state()
        s["orchestrator"] = {"pane": "w8:p1"}
        oke.save_state(s)
        st = self.status([], "--claim")
        self.assertEqual(self.code, 0)
        self.assertIsNone(st["orchestrator_conflict"])
        self.assertEqual(oke.load_state()["orchestrator"]["pane"], "w7:p1")

    def test_recovered_flag(self):
        self.write("home/state.json", "{broken")
        self.assertTrue(self.status([])["state_recovered"])


class TestCompactHook(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        self.key = oke.repo_key(self.repo)

    def test_compact_hook_silent_for_strangers(self):
        os.environ["HERDR_PANE_ID"] = "w3:p9"
        self.assertEqual(self.cli("compact-hook"), 0)
        self.assertEqual(self.out, "")

    def test_compact_hook_silent_without_pane_env(self):
        self.assertEqual(self.cli("compact-hook"), 0)
        self.assertEqual(self.out, "")

    def test_compact_hook_for_worker(self):
        self.job("fix-a", "working")
        oke.write_json_atomic(oke.children_path(self.pm), {
            "fix-a-w1": {"agent": "fix-a-w1", "job": "fix-a", "pane": "w2:p2", "role": "worker"}})
        os.environ["HERDR_PANE_ID"] = "w2:p2"
        self.assertEqual(self.cli("compact-hook"), 0)
        base = os.path.join(self.home, "jobs", "fix-a", "fix-a-w1").replace("\\", "/")
        self.assertIn(base + "/assignment.md", self.out)
        self.assertIn(base + "/board.md", self.out)

    def test_compact_hook_for_pm(self):
        s = oke.load_state()
        put_pm(s, self.key, {"agent": "pm-toy", "pane": "w1:p3"})
        oke.save_state(s)
        os.environ["HERDR_PANE_ID"] = "w1:p3"
        self.cli("compact-hook")
        self.assertIn(oke.pm_dir(self.pm).replace("\\", "/") + "/assignment.md", self.out)

    def test_compact_hook_for_orchestrator(self):
        s = oke.load_state()
        s["orchestrator"] = {"pane": "w7:p1"}
        oke.save_state(s)
        os.environ["HERDR_PANE_ID"] = "w7:p1"
        self.cli("compact-hook")
        self.assertIn("state.json", self.out)
        self.assertIn("status", self.out)

    def test_compact_hook_never_fails(self):
        self.write("home/state.json", "{broken")
        os.environ["HERDR_PANE_ID"] = "w7:p1"
        self.assertEqual(self.cli("compact-hook"), 0)
        self.assertEqual(self.out, "")
        self.assertTrue(os.path.exists(os.path.join(self.home, "state.json")))


def PANE(cwd, pane="w1:p9"):
    return J({"pane": {"pane_id": pane, "cwd": cwd}, "type": "pane_info"})


def trust_scenario(pane_cwd):
    s = spawn_ok()
    s["agent start"] = [E("agent_not_ready")]
    s["agent read"] = [{"stdout": "   Yes, I trust this folder\n", "exit": 0}]
    s["agent send-keys"] = [J({"type": "ok"})]
    s["agent wait"] = [AGENT()]
    s["pane get"] = [PANE(pane_cwd)]
    return s


class TestReviewFixes(SpawnBase):
    """One test per Important finding of the final review."""

    # 1. trust dialog accepts too widely
    def test_trust_refused_when_pane_is_not_in_the_repo(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        self.fake(trust_scenario(self.home))
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 1)
        self.assertEqual(self.argv_of("agent", "send-keys"), [])

    def test_pm_trust_refused_outside_its_folder(self):
        a = self.assignment("pm-toy", "pm")
        self.fake(trust_scenario(self.repo))  # the pane sits in the repository, not the PM folder
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 1)
        self.assertEqual(self.argv_of("agent", "send-keys"), [])

    def test_pm_trust_accepted_for_its_own_folder(self):
        self.job("fix-a", "planning")
        a = self.assignment("pm-toy", "pm")
        os.makedirs(oke.pm_dir("pm-toy"))
        self.fake(trust_scenario(oke.pm_dir("pm-toy")))
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 0)
        self.assertEqual(len(self.argv_of("agent", "send-keys")), 1)

    # 2. only `status` may set a broken state.json aside
    def test_other_commands_do_not_rename_broken_state(self):
        self.write("home/state.json", "{broken")
        self.assertEqual(self.cli("job", "phase", "x", "planning"), 1)
        self.assertIn("state.json", self.err)
        self.assertTrue(os.path.exists(os.path.join(self.home, "state.json")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "state.json.bad")))

    # 3. blocked loop
    def test_after_blocked_ignores_the_dialog_until_status_changes(self):
        self.fake({"agent get": [AGENT("blocked"), AGENT("blocked"), AGENT("working"), AGENT("blocked")],
                   "agent read": [{"stdout": "Do you want to proceed?\n", "exit": 0}]})
        oke.BLOCKED_GRACE = 100
        out = oke.wait_loop("a", self.path("none.md"), 2, poll=0, max_polls=4, after_blocked=True)
        self.assertEqual(out.splitlines()[0], "blocked")
        self.assertEqual(len(self.argv_of("agent", "get")), 4)

    def test_after_blocked_reports_a_dialog_that_outlives_the_grace_period(self):
        # a second dialog can already be up when the wait starts: it must not be ignored forever
        # fake clock: each time.time() call advances 1s, so the result does not depend on machine speed
        oke.BLOCKED_GRACE = 3
        ticks = iter(range(1000))
        real_time = oke.time.time
        oke.time.time = lambda: float(next(ticks))
        self.addCleanup(setattr, oke.time, "time", real_time)
        self.fake({"agent get": [AGENT("blocked")],
                   "agent read": [{"stdout": "Do you want to proceed?", "exit": 0}]})
        out = oke.wait_loop("a", self.path("none.md"), 2, poll=0, max_polls=200, after_blocked=True)
        self.assertEqual(out.splitlines()[0], "blocked")
        self.assertGreater(len(self.argv_of("agent", "get")), 1)

    def test_after_blocked_cli_flag(self):
        self.write("report.md", "type: done\nseq: 2\n---\n")
        self.fake({})
        self.assertEqual(self.cli("wait", "a", "--report", self.path("report.md"), "--seq", "2", "--after-blocked"), 0)

    # 4. folder and name collisions
    def test_same_basename_repos_get_different_pm_folders(self):
        one = self.git_repo("a/proj")
        two = self.git_repo("b/proj")
        k1, k2 = oke.repo_key(one), oke.repo_key(two)
        self.assertNotEqual(oke.pm_dir(k1), oke.pm_dir(k2))
        self.assertNotEqual(oke.repo_name(k1), oke.repo_name(k2))
        self.assertTrue(oke.repo_name(k1).startswith("proj-"))

    def test_non_ascii_repo_names_do_not_collide(self):
        k1, k2 = oke.repo_key(self.git_repo("한글")), oke.repo_key(self.git_repo("회사"))
        self.assertNotEqual(oke.repo_name(k1), oke.repo_name(k2))
        self.assertTrue(oke.AGENT_NAME.match("pm-" + oke.repo_name(k1)))

    def test_long_repo_name_still_gives_a_valid_agent_name(self):
        k = oke.repo_key(self.git_repo("My.Very_Long.Repository_Name-With.Many.Parts"))
        self.assertTrue(oke.AGENT_NAME.match("pm-" + oke.repo_name(k)))

    # 5. stale report beside a new assignment
    def test_spawn_removes_a_stale_report(self):
        a = self.assignment("pm-toy", "pm")
        stale = self.write("asg/report.md", "type: done\nseq: 1\n---\n")
        self.fake(spawn_ok())
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 0)
        self.assertFalse(os.path.exists(stale))

    # 6. stdout encoding
    def test_cli_prints_non_ascii_screen_text_under_a_legacy_locale(self):
        self.fake({"agent get": [AGENT("blocked")],
                   "agent read": [{"stdout": u" ❯ 1. Yes 한글\n", "exit": 0}]})
        env = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="ascii")
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "oke.py"), "wait", "a", "--report",
                              self.path("none.md"), "--seq", "2", "--poll", "0"],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        self.assertEqual(p.returncode, 0, err)
        self.assertIn(u"❯ 1. Yes 한글", out.decode("utf-8"))

    # 7. orphan job directories
    def test_archive_moves_an_orphan_job_dir(self):
        os.makedirs(os.path.join(self.home, "jobs", "old-job"))
        self.assertEqual(self.archive("old-job"), 0)
        self.assertFalse(os.path.exists(os.path.join(self.home, "jobs", "old-job")))
        self.assertEqual(len(os.listdir(os.path.join(self.home, "archive"))), 1)

    def test_archive_unknown_job_still_fails(self):
        self.assertEqual(self.archive("nope"), 1)

    # 8. spawn leftovers
    def test_failed_first_prompt_leaves_a_record_and_names_the_pane(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        s = spawn_ok()
        s["agent prompt"] = [E("agent_blocked")]
        self.fake(s)
        self.assertEqual(self.cli("spawn", a, "--split-from", "w1:p1"), 1)
        self.assertIn("w1:p9", self.err)
        self.assertEqual(self.children()["fix-a-w1"]["pane"], "w1:p9")

    def test_failed_start_names_the_split_pane(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        s = spawn_ok()
        s["agent start"] = [E("timeout")]
        self.fake(s)
        self.assertEqual(self.cli("spawn", a, "--split-from", "w1:p1"), 1)
        self.assertIn("w1:p9", self.err)

    # 9. status gives the orchestrator what its skill needs
    def test_status_prints_pm_dir_and_session(self):
        key = oke.repo_key(self.repo)
        s = oke.load_state()
        put_pm(s, key, {"agent": "pm-toy", "pane": "w1:p3", "session": "s-pm", "seq": 1})
        oke.save_state(s)
        os.makedirs(oke.pm_dir(self.pm))
        with io.open(os.path.join(oke.pm_dir(self.pm), "assignment.md"), "w", encoding="utf-8") as f:
            f.write(u"agent: pm-toy\nseq: 5\n---\n")
        os.environ["HERDR_ENV"] = "1"
        sc = help_ok()
        sc["agent list"] = [J({"agents": []})]
        self.fake(sc)
        self.assertEqual(self.cli("status"), 0)
        repo = json.loads(self.out)["pms"][self.pm]
        self.assertEqual(repo["session"], "s-pm")  # seq is per job now: status.jobs[..].pm_seq
        self.assertEqual(repo["pm_dir"], oke.pm_dir(self.pm).replace("\\", "/"))
        self.assertEqual(repo["places"], oke.load_state()["pms"][self.pm]["places"])


class TestRepoDir(unittest.TestCase):
    def test_old_git_printing_nothing_is_not_the_root(self):
        real = oke.git_out
        oke.git_out = lambda key, *a: (0, "") if "--show-toplevel" in a else (0, "false")
        try:
            self.assertEqual(oke.repo_dir("C:/x/.git"), "C:/x")
        finally:
            oke.git_out = real


if __name__ == "__main__":
    unittest.main()

