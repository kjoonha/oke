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


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self._env = dict(os.environ)
        os.environ["OKE_HOME"] = self.home
        for k in ("HERDR_ENV", "HERDR_PANE_ID", "OKE_HERDR", "FAKE_HERDR_DIR"):
            os.environ.pop(k, None)
        self.out = ""
        self.err = ""

    def tearDown(self):
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

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = oke.main([str(a) for a in args])
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
        self.assertEqual(oke.load_state(), {"orchestrator": {}, "repos": {}, "jobs": {}})
        self.assertEqual(oke.load_config()["autonomy"], "balanced")
        self.assertTrue(os.path.isdir(home))

    def test_config_file_overrides_defaults(self):
        self.write("home/config.json", '{"autonomy": "full", "models": {"worker": "haiku"}}')
        c = oke.load_config()
        self.assertEqual(c["autonomy"], "full")
        self.assertEqual(c["models"], {"pm": "opus", "worker": "haiku", "reviewer": "sonnet"})
        self.assertFalse(c["wiki_path"].startswith("~"))

    def test_broken_state_is_set_aside(self):
        self.write("home/state.json", "{broken")
        self.assertTrue(oke.load_state()["_recovered"])
        self.assertTrue(os.path.exists(os.path.join(self.home, "state.json.bad")))

    def test_save_state_drops_recovered_flag(self):
        s = oke.load_state()
        s["_recovered"] = True
        oke.save_state(s)
        self.assertNotIn("_recovered", oke.load_state())

    def test_read_header(self):
        p = self.write("r.md", "type: done\nseq: 3\n---\n## 요약\n")
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

    def test_repo_key_rejects_non_repo(self):
        with self.assertRaises(ValueError):
            oke.repo_key(self.home)

    def test_normalize_windows_forms(self):
        self.assertEqual(oke.normalize_path("/c/Users/X/r/.git", windows=True), "c:/users/x/r/.git")
        self.assertEqual(oke.normalize_path("C:\\Users\\X\\r\\.git\\", windows=True), "c:/users/x/r/.git")
        self.assertEqual(oke.normalize_path("/home/U/r/.git/", windows=False), "/home/U/r/.git")

    def test_slug(self):
        self.assertEqual(oke.slug("My.Repo_v2"), "my-repo-v2")
        self.assertEqual(oke.slug("9lives"), "x9lives")
        self.assertEqual(len(oke.slug("a" * 40)), 24)
        self.assertEqual(oke.slug("--a--b--"), "a-b")

    def test_job_new_phase_archive(self):
        repo = self.git_repo("r")
        self.assertEqual(self.cli("job", "new", "fix-a", "--repo", repo), 0)
        info = json.loads(self.out)
        self.assertEqual(info["repo"], oke.repo_key(repo))
        self.assertTrue(info["job_dir"].endswith("/jobs/fix-a"))
        self.assertTrue(info["pm_dir"].endswith("/repos/r/pm"))
        self.assertEqual(info["pm_agent"], "pm-r")
        self.assertEqual(self.cli("job", "new", "fix-a", "--repo", repo), 1)
        self.assertEqual(self.cli("job", "phase", "fix-a", "nonsense"), 1)
        self.assertEqual(self.cli("job", "phase", "nope", "planning"), 1)
        self.assertEqual(self.cli("job", "phase", "fix-a", "planning"), 0)
        st = oke.load_state()
        self.assertEqual(st["jobs"]["fix-a"]["phase"], "planning")
        self.assertEqual(st["jobs"]["fix-a"]["repo"], oke.repo_key(repo))
        self.assertTrue(os.path.isdir(os.path.join(self.home, "jobs", "fix-a")))
        self.assertEqual(self.cli("job", "archive", "fix-a"), 0)
        self.assertNotIn("fix-a", oke.load_state()["jobs"])
        self.assertEqual(len(os.listdir(os.path.join(self.home, "archive"))), 1)
        self.assertFalse(os.path.exists(os.path.join(self.home, "jobs", "fix-a")))

    def test_job_new_rejects_bad_slug(self):
        repo = self.git_repo("r")
        self.assertEqual(self.cli("job", "new", "Bad Name", "--repo", repo), 1)


def J(result):
    return {"stdout": json.dumps({"id": "x", "result": result}), "exit": 0}


def E(code, message="m"):
    # real herdr prints error JSON on stderr with an empty stdout
    return {"stderr": json.dumps({"id": "x", "error": {"code": code, "message": message}}), "exit": 1}


OK = {"stdout": "", "exit": 0}


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
                   "worktree create --help": [OK]})
        self.assertEqual(oke.missing_commands(), ["agent wait"])


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
            "agent get": [AGENT()]}


class SpawnBase(Base):
    def setUp(self):
        Base.setUp(self)
        self.repo = self.git_repo("toy")
        oke.DIALOG_POLL = 0

    def job(self, name, phase):
        self.assertEqual(self.cli("job", "new", name, "--repo", self.repo), 0)
        self.assertEqual(self.cli("job", "phase", name, phase), 0)

    def assignment(self, agent, role, job=None, seq=1, cwd=None):
        lines = ["agent: " + agent, "role: " + role, "cwd: " + (cwd or self.repo), "seq: %d" % seq]
        if job:
            lines.append("job: " + job)
        return self.write("asg/%s.md" % agent, "\n".join(lines) + "\n---\n## 목표\n")

    def children(self):
        p = os.path.join(self.home, "repos", "toy", "pm", "children.json")
        with io.open(p, encoding="utf-8") as f:
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

    def test_spawn_order_and_record(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a", seq=4)
        self.fake(spawn_ok())
        self.assertEqual(self.cli("spawn", a, "--split-from", "w1:p1"), 0)
        self.assertEqual([c[:2] for c in self.calls()],
                         [["agent", "list"], ["pane", "split"], ["agent", "start"],
                          ["agent", "prompt"], ["agent", "get"]])
        rec = self.children()["fix-a-w1"]
        self.assertEqual((rec["session"], rec["pane"], rec["seq"], rec["job"], rec["role"]),
                         ("s-1", "w1:p9", 4, "fix-a", "worker"))
        self.assertEqual(json.loads(self.out), {"agent": "fix-a-w1", "pane": "w1:p9", "session": "s-1"})
        prompt = self.argv_of("agent", "prompt")[0]
        self.assertEqual(prompt[2], "fix-a-w1")
        self.assertIn("oke:worker", prompt[3])
        self.assertIn(a.replace("\\", "/"), prompt[3])

    def test_spawn_pm_records_in_state(self):
        a = self.assignment("pm-toy", "pm")
        self.fake(spawn_ok())
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 0)
        self.assertEqual(self.argv_of("pane", "split"), [])
        pm = oke.load_state()["repos"][oke.repo_key(self.repo)]["pm"]
        self.assertEqual((pm["agent"], pm["pane"], pm["session"]), ("pm-toy", "w1:p9", "s-1"))

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
        self.fake(s)
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 1)
        self.assertIn("Select login method", self.err)
        self.assertEqual(self.argv_of("agent", "send-keys"), [])

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
        self.assertEqual(args[args.index("--model") + 1], "sonnet")
        self.assertNotIn("--bare", args)
        r = oke.launch_args("reviewer", c, "/h", "/p")
        self.assertNotIn("bypassPermissions", r)
        self.assertIn("dontAsk", r)
        self.assertNotIn("Write", r)
        self.assertNotIn("Bash", r)

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
        self.assertEqual(self.cli("job", "phase", "fix-s", "working"), 0)
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

    def test_gone(self):
        self.fake({"agent get": [E("agent_not_found")]})
        self.assertEqual(oke.check_once("a", self.path("none.md"), 2), "gone")

    def test_herdr_down_is_exit_2_not_gone(self):
        self.fake({"agent get": [{"stdout": "connection refused", "exit": 1}]})
        code = self.cli("wait", "a", "--report", self.path("none.md"), "--seq", "2", "--poll", "0")
        self.assertEqual(code, 2)
        self.assertNotIn("gone", self.out)

    def test_cli_prints_result(self):
        r = self.report("type: failed\nseq: 7\n---\n")
        self.fake({})
        self.assertEqual(self.cli("wait", "a", "--report", r, "--seq", "7"), 0)
        self.assertEqual(self.out.strip(), "report failed")


def help_ok():
    return dict((c + " --help", [OK]) for c in oke.REQUIRED_COMMANDS)


class TestStatus(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        os.environ["HERDR_ENV"] = "1"
        os.environ["HERDR_PANE_ID"] = "w7:p1"
        self.key = oke.repo_key(self.repo)

    def set_pm(self, agent="pm-toy", pane="w1:p3", session="s-pm"):
        s = oke.load_state()
        s["repos"][self.key] = {"pm": {"agent": agent, "pane": pane, "session": session, "seq": 1}}
        oke.save_state(s)

    def status(self, agents=(), *flags):
        s = help_ok()
        s["agent list"] = [J({"agents": list(agents)})]
        self.fake(s)
        self.code = self.cli("status", *flags)
        return json.loads(self.out) if self.out.strip() else None

    def pm_record(self):
        return oke.load_state()["repos"][self.key]["pm"]

    def test_outside_herdr(self):
        os.environ.pop("HERDR_ENV", None)
        self.fake({})
        self.assertEqual(self.cli("status"), 0)
        self.assertEqual(json.loads(self.out)["herdr"], {"inside": False, "missing": []})
        self.assertEqual(self.calls(), [])

    def test_missing_commands_reported(self):
        s = help_ok()
        s["agent wait --help"] = [{"stdout": "", "exit": 2}]
        s["agent list"] = [J({"agents": []})]
        self.fake(s)
        self.cli("status")
        self.assertEqual(json.loads(self.out)["herdr"], {"inside": True, "missing": ["agent wait"]})

    def test_pm_found_by_name_after_pane_move(self):
        self.set_pm()
        st = self.status([AG(name="pm-toy", pane="w9:p1", session="other", cwd=self.repo)])
        self.assertEqual(st["repos"][self.key], {"pm": "alive", "agent": "pm-toy", "pane": "w9:p1"})
        self.assertEqual(self.pm_record()["pane"], "w9:p1")

    def test_pm_found_by_pane_after_rename(self):
        self.set_pm()
        st = self.status([AG(name="renamed", pane="w1:p3", session="other", cwd=self.repo)])
        self.assertEqual(st["repos"][self.key]["pm"], "alive")
        self.assertEqual(self.pm_record()["agent"], "renamed")

    def test_pm_found_by_session(self):
        self.set_pm()
        st = self.status([AG(name="x", pane="w5:p5", session="s-pm", cwd=self.repo)])
        self.assertEqual(st["repos"][self.key]["pm"], "alive")
        self.assertEqual((self.pm_record()["agent"], self.pm_record()["pane"]), ("x", "w5:p5"))

    def test_pm_in_other_repo_cwd_is_dead(self):
        self.set_pm()
        other = self.git_repo("other")
        st = self.status([AG(name="pm-toy", pane="w1:p3", session="s-pm", cwd=other)])
        self.assertEqual(st["repos"][self.key]["pm"], "dead")
        self.assertEqual(self.pm_record()["pane"], "w1:p3")

    def test_pm_missing_is_dead(self):
        self.set_pm()
        self.assertEqual(self.status([])["repos"][self.key]["pm"], "dead")

    def test_job_classes(self):
        self.set_pm()
        self.job("j-work", "working")
        self.job("j-merge", "awaiting_merge")
        oke.write_json_atomic(oke.children_path(self.key), {
            "j-work-w1": {"agent": "j-work-w1", "job": "j-work", "pane": "w2:p1"},
            "j-work-rv": {"agent": "j-work-rv", "job": "j-work", "pane": "w2:p2"}})
        alive = self.status([AG(name="pm-toy", pane="w1:p3", cwd=self.repo), AG(name="j-work-w1", pane="w2:p1")])
        self.assertEqual(alive["jobs"]["j-work"],
                         {"phase": "working", "class": "alive",
                          "agents": {"j-work-w1": "alive", "j-work-rv": "dead"}})
        self.assertEqual(alive["jobs"]["j-merge"]["class"], "awaiting_user")
        dead = self.status([])
        self.assertEqual(dead["jobs"]["j-work"]["class"], "disconnected")
        self.assertEqual(dead["jobs"]["j-merge"]["class"], "awaiting_user")

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
        oke.write_json_atomic(oke.children_path(self.key), {
            "fix-a-w1": {"agent": "fix-a-w1", "job": "fix-a", "pane": "w2:p2", "role": "worker"}})
        os.environ["HERDR_PANE_ID"] = "w2:p2"
        self.assertEqual(self.cli("compact-hook"), 0)
        base = os.path.join(self.home, "jobs", "fix-a", "fix-a-w1").replace("\\", "/")
        self.assertIn(base + "/assignment.md", self.out)
        self.assertIn(base + "/board.md", self.out)

    def test_compact_hook_for_pm(self):
        s = oke.load_state()
        s["repos"][self.key] = {"pm": {"agent": "pm-toy", "pane": "w1:p3"}}
        oke.save_state(s)
        os.environ["HERDR_PANE_ID"] = "w1:p3"
        self.cli("compact-hook")
        self.assertIn(oke.pm_dir(self.key).replace("\\", "/") + "/assignment.md", self.out)

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


if __name__ == "__main__":
    unittest.main()
