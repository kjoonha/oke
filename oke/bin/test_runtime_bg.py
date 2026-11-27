"""BgRuntime against a fake `claude` binary (the fake herdr script, which answers
any argv from a scenario)."""
import io
import json
import os
import subprocess
import sys
import unittest

from test_oke import put_pm, SpawnBase, HERE, spawn_ok  # noqa: F401
import oke
import runtime_bg

ROWS = [
    {"id": "a1", "sessionId": "s-1", "name": "fix-a-w1", "state": "working", "cwd": "/r"},
    {"id": "a2", "sessionId": "s-2", "name": "pm-toy", "state": "done", "cwd": "/r"},
    {"id": "a0", "sessionId": "s-0", "name": "fix-a-w1", "state": "stopped", "cwd": "/r"},
]


def OUT(text):
    return {"stdout": text, "exit": 0}


class BgBase(SpawnBase):
    def fake(self, scenario):
        SpawnBase.fake(self, scenario)
        os.environ["OKE_CLAUDE"] = os.environ["OKE_HERDR"]

    def setUp(self):
        SpawnBase.setUp(self)
        self.rt = runtime_bg.BgRuntime()
        runtime_bg.START_POLLS = 2

    def argv(self):
        return self.calls()


class TestBgRuntime(BgBase):
    def test_agents_map_states_and_drop_dead_ones(self):
        self.fake({"agents --json": [OUT(json.dumps(ROWS))]})
        agents = self.rt.agents()
        self.assertEqual([(a["name"], a["pane_id"], a["agent_status"]) for a in agents],
                         [("fix-a-w1", "a1", "working"), ("pm-toy", "a2", "idle")])
        self.assertEqual(agents[0]["agent_session"], {"value": "s-1"})
        self.assertEqual(self.rt.place_id("a0"), "a0")  # a dead one can still be closed
        self.assertFalse(self.rt.place_exists("a0"))

    def test_status_decides_between_working_and_idle(self):
        row = {"id": "a", "name": "n", "state": "working", "status": "idle"}
        self.assertEqual(runtime_bg.status_of(row), "idle")  # seen live on a waiting PM
        self.assertEqual(runtime_bg.status_of(dict(row, status="busy")), "working")
        self.assertEqual(runtime_bg.status_of(dict(row, state="done", status=None)), "idle")
        self.assertEqual(runtime_bg.status_of(dict(row, state="blocked")), "idle")  # a question, not a dialog
        self.assertEqual(runtime_bg.status_of(dict(row, state="blocked", status="busy")), "blocked")
        self.assertIsNone(runtime_bg.status_of(dict(row, state="stopped")))

    def test_unknown_agent(self):
        self.fake({"agents --json": [OUT("[]")]})
        with self.assertRaises(oke.HerdrError) as c:
            self.rt.agent("nobody")
        self.assertEqual(c.exception.code, "agent_not_found")

    def test_start_puts_prompt_first_and_adds_bg_settings(self):
        folder = self.path("agent")
        os.makedirs(folder)
        lean = os.path.join(folder, "lean-settings.json")
        with io.open(lean, "w", encoding="utf-8") as f:
            f.write(json.dumps({"env": {"X": "1"}}))
        self.fake({"go": [OUT("started")],
                   "agents --json": [OUT("[]"), OUT(json.dumps(ROWS))]})
        info = self.rt.start("fix-a-w1", self.repo, ["--settings", lean, "--allowedTools", "Bash"], "go",
                             trusted=lambda: False)
        self.assertEqual(info["pane_id"], "a1")
        start = self.argv()[0]
        self.assertEqual(start[:4], ["go", "--bg", "--name", "fix-a-w1"])
        with io.open(start[start.index("--settings") + 1], encoding="utf-8") as f:
            merged = json.load(f)
        self.assertEqual(merged["env"], {"X": "1"})
        self.assertEqual(merged["worktree"], {"bgIsolation": "none"})
        self.assertEqual(merged["crossSessionInbound"], "accept")

    def claude_json(self, projects):
        conf = self.path("conf")
        os.makedirs(conf, exist_ok=True)
        with io.open(os.path.join(conf, ".claude.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"projects": projects}))
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = conf
        self.addCleanup(lambda: os.environ.pop("CLAUDE_CONFIG_DIR") if old is None
                        else os.environ.__setitem__("CLAUDE_CONFIG_DIR", old))

    def test_a_start_in_an_untrusted_folder_says_how_to_trust_it(self):
        place = self.path("home/pms/backend")
        os.makedirs(place)
        self.claude_json({os.path.expanduser("~").replace("\\", "/"): {"hasTrustDialogAccepted": True}})
        self.fake({"go": [{"stderr": "no session", "exit": 1}]})
        with self.assertRaises(oke.HerdrError) as c:
            self.rt.start("backend", place, [], "go", trusted=lambda: False)
        self.assertEqual(c.exception.code, "claude_failed")
        self.assertIn("has not trusted", c.exception.message)  # the home folder's trust does not count
        self.assertFalse(runtime_bg.trusted_by_claude(place))
        self.claude_json({self.path("home").replace("\\", "/"): {"hasTrustDialogAccepted": True}})
        self.assertTrue(runtime_bg.trusted_by_claude(place))  # trusted from the OKe home above it
        self.fake({"go": [{"stderr": "no session", "exit": 1}]})
        with self.assertRaises(oke.HerdrError) as c:
            self.rt.start("backend", place, [], "go", trusted=lambda: False)
        self.assertNotIn("has not trusted", c.exception.message)

    def test_bypass_becomes_dont_ask_with_the_role_tools(self):
        argv = runtime_bg.without_bypass(["--tools", "Bash,Edit", "--permission-mode", "bypassPermissions"])
        self.assertEqual(argv, ["--tools", "Bash,Edit", "--permission-mode", "dontAsk",
                                "--allowedTools", "Bash", "Edit"])
        self.assertEqual(runtime_bg.without_bypass(["--permission-mode", "dontAsk"]),
                         ["--permission-mode", "dontAsk"])

    def test_close_stops_then_removes(self):
        self.fake({"stop": [{"stderr": "not running", "exit": 1}], "rm": [OUT("")]})
        self.rt.close("a0")
        self.assertEqual(self.argv(), [["stop", "a0"], ["rm", "a0"]])

    def test_keys_and_attach_name_the_attach_command(self):
        self.fake({"agents --json": [OUT(json.dumps(ROWS))]})
        self.assertEqual(self.rt.attach("pm-toy"), "claude attach a2")
        with self.assertRaises(oke.HerdrError) as c:
            self.rt.keys("pm-toy", ["enter"])
        self.assertIn("claude attach a2", str(c.exception))

    def test_an_npm_shim_starts_the_exe_it_names(self):
        bin_dir = os.path.join(self.tmp, "npm")
        exe = os.path.join(bin_dir, "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
        os.makedirs(os.path.dirname(exe))
        io.open(exe, "w").close()
        shim = os.path.join(bin_dir, "claude.cmd")
        with io.open(shim, "w") as f:
            # a JS shim names node.exe first: never start that
            f.write('@ECHO off\r\nIF EXIST "%dp0%\\node.exe" (SET "_prog=%dp0%\\node.exe")\r\n'
                    '"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"   %*\r\n')
        io.open(os.path.join(bin_dir, "node.exe"), "w").close()
        old = runtime_bg.shutil.which
        runtime_bg.shutil.which = lambda name: shim
        try:
            self.assertEqual(runtime_bg.find_claude(), exe)
            os.remove(exe)
            self.assertEqual(runtime_bg.find_claude(), shim)  # the shim names nothing there: run it
            runtime_bg.shutil.which = lambda name: None
            self.assertEqual(runtime_bg.find_claude(), "claude")
        finally:
            runtime_bg.shutil.which = old

    def test_child_env_drops_nesting_variables(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ.update({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "x", "AI_AGENT": "d",
                           "CLAUDE_CONFIG_DIR": "/c", "PATH_KEEP_ME": "y",
                           "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_OAUTH_TOKEN": "t",
                           "CLAUDE_CODE_SESSION_ID": "s"})
        env = runtime_bg.child_env()
        self.assertEqual(env["CLAUDE_CODE_USE_BEDROCK"], "1")  # the user's provider and login stay
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "t")
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", env)
        self.assertNotIn("AI_AGENT", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/c")
        self.assertEqual(env["PATH_KEEP_ME"], "y")

    def test_worktree_inside_the_repo(self):
        git = lambda *a: subprocess.check_call(["git", "-C", self.repo] + list(a),
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not os.path.isdir(os.path.join(self.repo, ".git")):
            git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
        wt = self.rt.worktree_create(self.repo, "oke/fix-a", "fix-a")
        self.assertEqual(wt["path"], os.path.join(self.repo, ".claude", "worktrees", "oke-fix-a"))
        self.assertTrue(os.path.isdir(wt["path"]))
        # main stays clean; a second worktree adds no second line
        self.rt.worktree_remove(self.rt.worktree_create(self.repo, "oke/fix-b", "fix-b")["id"])
        status = subprocess.check_output(["git", "-C", self.repo, "status", "--porcelain"])
        self.assertEqual(status.strip(), b"")
        with open(os.path.join(self.repo, ".git", "info", "exclude")) as f:
            self.assertEqual(f.read().count("/.claude/worktrees/"), 1)
        self.rt.worktree_remove(wt["id"])
        self.assertFalse(os.path.isdir(wt["path"]))


class TestSpawnInBg(BgBase):
    def test_spawn_records_the_session_id_as_the_place(self):
        os.environ["OKE_RUNTIME"] = "bg"
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        prompt = oke.FIRST_PROMPT.format(role="worker", path=a.replace("\\", "/"))
        self.fake({"agents --json": [OUT("[]"), OUT("[]"), OUT(json.dumps(ROWS))],
                   prompt: [OUT("started")]})
        self.assertEqual(self.cli("spawn", a), 0, self.err)
        self.assertEqual(json.loads(self.out), {"agent": "fix-a-w1", "pane": "a1", "session": "s-1"})
        kid = self.children()["fix-a-w1"]
        self.assertEqual((kid["pane"], kid["terminal"]), ("a1", "a1"))
        self.assertEqual(self.argv()[1][0], prompt)

    def test_outside_herdr_the_default_is_bg(self):
        del os.environ["OKE_RUNTIME"]
        self.assertEqual(oke.runtime().name, "bg")
        os.environ["HERDR_ENV"] = "1"
        self.assertEqual(oke.runtime().name, "herdr")

    def test_runtime_named_in_state(self):
        del os.environ["OKE_RUNTIME"]
        st = oke.load_state()
        st["runtime"] = "bg"
        oke.save_state(st)
        self.assertEqual(oke.runtime().name, "bg")
        os.environ["OKE_RUNTIME"] = "herdr"
        self.assertEqual(oke.runtime().name, "herdr")


class TestDeliverInBg(BgBase):
    def test_wait_hands_the_mail_to_the_caller(self):
        os.environ["OKE_RUNTIME"] = "bg"
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        oke.write_json_atomic(oke.children_path(self.pm),
                              {"fix-a-w1": {"agent": "fix-a-w1", "job": "fix-a", "pane": "a1",
                                            "role": "worker", "seq": 1}})
        rows = [dict(ROWS[0], state="done")]
        self.fake({"agents --json": [OUT(json.dumps(rows))]})
        self.assertEqual(self.cli("send", "fix-a-w1", "stop", "now"), 0, self.err)
        report = os.path.join(os.path.dirname(a), "report.md")
        line = oke.wait_loop("fix-a-w1", report, 1, poll=0, max_polls=10)
        self.assertEqual(json.loads(line), {"deliver": {"to": "fix-a-w1", "text": "OKe inbox: user: stop now"}})
        self.assertEqual(oke.inbox_messages("fix-a-w1"), [])


class TestIdentityInBg(BgBase):
    def setUp(self):
        BgBase.setUp(self)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-orch"

    def test_claim_outside_herdr_picks_bg_and_records_the_session(self):
        del os.environ["OKE_RUNTIME"]
        self.fake({"agents --json": [OUT("[]")]})
        self.assertEqual(self.cli("status", "--claim"), 0, self.err)
        st = oke.load_state()
        self.assertEqual(st["runtime"], "bg")
        self.assertEqual(st["orchestrator"]["pane"], "session:sid-orch")

    def test_tick_knows_a_bg_child_by_its_session(self):
        os.environ["OKE_RUNTIME"] = "bg"
        self.job("fix-a", "working")
        oke.write_json_atomic(oke.children_path(self.pm),
                              {"fix-a-w1": {"agent": "fix-a-w1", "job": "fix-a", "pane": "a1",
                                            "session": "s-1", "role": "worker", "seq": 1}})
        self.fake({"agents --json": [OUT(json.dumps(ROWS))]})
        self.assertEqual(self.cli("tick", "--pane", "session:s-1"), 0, self.err)
        self.assertEqual(json.loads(self.out)["agent"], "fix-a-w1")

    def test_compact_hook_finds_a_bg_child_by_its_session(self):
        os.environ["OKE_RUNTIME"] = "bg"
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s-1"
        self.job("fix-a", "working")
        oke.write_json_atomic(oke.children_path(self.pm),
                              {"fix-a-w1": {"agent": "fix-a-w1", "job": "fix-a", "pane": "a1",
                                            "session": "s-1", "role": "worker", "seq": 1}})
        self.assertIn("fix-a/fix-a-w1/assignment.md", oke.compact_message())

    def test_a_bg_pm_never_revives_the_orchestrator(self):
        os.environ["OKE_RUNTIME"] = "bg"
        st = oke.load_state()
        st["orchestrator"] = {"pane": "session:sid-orch"}
        put_pm(st, oke.repo_key(self.repo), {"agent": "pm-toy", "pane": "a2", "session": "s-2"})
        oke.save_state(st)
        self.fake({"agents --json": [OUT(json.dumps(ROWS))]})
        self.addCleanup(setattr, oke, "revive", oke.revive)
        oke.revive = lambda name: self.fail("revived " + name)
        self.assertEqual(self.cli("tick", "--pane", "session:s-2"), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual(out["role"], "pm")


class TestBoardAndScreen(BgBase):
    def test_board_text(self):
        os.environ["OKE_RUNTIME"] = "bg"
        self.job("fix-a", "working")
        oke.write_json_atomic(oke.children_path(self.pm),
                              {"fix-a-w1": {"agent": "fix-a-w1", "job": "fix-a", "pane": "a1",
                                            "session": "s-1", "role": "worker", "seq": 1}})
        self.fake({"agents --json": [OUT(json.dumps(ROWS))]})
        self.assertEqual(self.cli("board"), 0, self.err)
        self.assertIn("job fix-a: working", self.out)
        self.assertIn("    worker fix-a-w1: working", self.out)

    def test_screen_strips_terminal_escapes(self):
        log = "\x1b[38;2;1;2;3mhello\x1b[K\n\x1b[mhello\nworld \x1b]0;t\x07\n"
        self.fake({"agents --json": [OUT(json.dumps(ROWS))], "logs": [OUT(log)]})
        self.assertEqual(self.rt.screen("pm-toy"), "hello\nworld\n")


if __name__ == "__main__":
    unittest.main()
