# -*- coding: utf-8 -*-
"""Slice 4: codex and opencode as worker and reviewer."""
import os
import unittest

from test_oke import AGENT, E, J, PANE, SpawnBase, spawn_ok
import oke

READY = "> Ask Codex to do anything\n  ? for shortcuts\n"


class KindBase(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        self.job("fix-a", "working")

    def start_argv(self):
        return self.argv_of("agent", "start")[0]

    def spawn(self, role="worker", kind=None, scenario=None):
        name = "fix-a-w1" if role == "worker" else "fix-a-rv"
        a = self.assignment(name, role, "fix-a")
        if kind:
            text = open(a).read().replace("seq: 1", "seq: 1\nkind: " + kind)
            with open(a, "w") as f:
                f.write(text)
        self.fake(scenario or spawn_ok())
        return self.cli("spawn", a, "--pane", "w1:p9")


class TestAgentCommand(unittest.TestCase):
    def setUp(self):
        self.config = {"autonomy": "balanced", "wiki_path": "/w", "models": {"worker": "sonnet"}}

    def test_codex_worker_bypasses_approvals(self):
        kind, args = oke.agent_command("codex", "worker", self.config, "/h", "/p", "/f")
        self.assertEqual(kind, "codex")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", args)

    def test_codex_conservative_worker_asks_and_writes_only_its_places(self):
        self.config["autonomy"] = "conservative"
        _, args = oke.agent_command("codex", "worker", self.config, "/h", "/p", "/f")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
        for pair in (["-s", "workspace-write"], ["-a", "on-request"], ["--add-dir", "/h"], ["--add-dir", "/w"]):
            self.assertIn(pair, [args[i:i + 2] for i in range(len(args))])

    def test_codex_reviewer_writes_only_its_own_folder(self):
        _, args = oke.agent_command("codex", "reviewer", self.config, "/h", "/p", "/f")
        for pair in (["-C", "/f"], ["-s", "workspace-write"], ["-a", "never"]):
            self.assertIn(pair, [args[i:i + 2] for i in range(len(args))])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)

    def test_codex_resume_goes_first(self):
        _, args = oke.agent_command("codex", "worker", self.config, "/h", "/p", "/f", resume="s-1")
        self.assertEqual(args[:4], ["-c", "features.hooks=false", "resume", "s-1"])

    def test_opencode_auto_approves_and_resumes_by_session(self):
        kind, args = oke.agent_command("opencode", "reviewer", self.config, "/h", "/p", "/f", resume="s-2")
        self.assertEqual(kind, "opencode")
        self.assertIn("--auto", args)
        self.assertIn(["--session", "s-2"], [args[i:i + 2] for i in range(len(args))])

    def test_opencode_conservative_worker_asks(self):
        self.config["autonomy"] = "conservative"
        _, args = oke.agent_command("opencode", "worker", self.config, "/h", "/p", "/f")
        self.assertNotIn("--auto", args)

    def test_claude_keeps_its_arguments(self):
        kind, args = oke.agent_command("claude", "worker", self.config, "/h", "/p", "/f")
        self.assertEqual((kind, args), ("claude", oke.launch_args("worker", self.config, "/h", "/p")))

    def test_other_kinds_get_the_skill_file_in_the_prompt(self):
        prompt = oke.first_prompt("codex", "worker", "/a/assignment.md")
        self.assertIn("skills/worker/SKILL.md", prompt.replace(os.sep, "/"))
        self.assertIn("/a/assignment.md", prompt)
        self.assertNotIn("oke:worker skill", prompt)


class TestSpawnKinds(KindBase):
    def test_config_picks_the_worker_kind(self):
        self.write("home/config.json", '{"agents": {"worker": "codex"}}')
        self.assertEqual(self.spawn(), 0)
        argv = self.start_argv()
        self.assertEqual(argv[argv.index("--kind") + 1], "codex")
        self.assertEqual(self.children()["fix-a-w1"]["kind"], "codex")

    def test_the_assignment_header_overrides_the_config(self):
        self.assertEqual(self.spawn(kind="opencode"), 0)
        argv = self.start_argv()
        self.assertEqual(argv[argv.index("--kind") + 1], "opencode")
        self.assertIn("--auto", argv)

    def test_default_kind_is_claude(self):
        self.assertEqual(self.spawn(), 0)
        argv = self.start_argv()
        self.assertEqual(argv[argv.index("--kind") + 1], "claude")

    def test_unknown_kind_is_refused(self):
        self.assertEqual(self.spawn(kind="gemini"), 1)
        self.assertEqual(self.argv_of("agent", "start"), [])

    def test_a_pm_must_be_claude(self):
        a = self.assignment("pm-toy", "pm")
        with open(a) as f:
            text = f.read()
        with open(a, "w") as f:
            f.write(text.replace("seq: 1", "seq: 1\nkind: codex"))
        self.fake(spawn_ok())
        self.assertEqual(self.cli("spawn", a, "--pane", "w1:p9"), 1)

    def test_codex_trust_dialog_takes_enter_only(self):
        s = spawn_ok()
        s["agent start"] = [E("agent_not_ready")]
        s["agent read"] = [{"stdout": "  Trust this folder? Codex can read...\n› 1. Trust and continue\n  2. Quit\n", "exit": 0}]
        s["agent send-keys"] = [J({"type": "ok"})]
        s["agent wait"] = [AGENT()]
        s["pane get"] = [PANE(self.repo)]
        self.assertEqual(self.spawn(kind="codex", scenario=s), 0)
        self.assertEqual(self.argv_of("agent", "send-keys"), [["agent", "send-keys", "fix-a-w1", "enter"]])

    def test_an_update_offer_is_skipped_then_the_trust_dialog_answered(self):
        s = spawn_ok()
        s["agent start"] = [E("agent_not_ready")]
        s["agent read"] = [{"stdout": "› 1. Update now (runs ...)\n  2. Skip\n", "exit": 0},
                           {"stdout": "  Trust this folder?\n› 1. Trust and continue\n", "exit": 0}]
        s["agent send-keys"] = [J({"type": "ok"})]
        s["agent wait"] = [E("timeout"), AGENT()]
        s["pane get"] = [PANE(self.repo)]
        self.assertEqual(self.spawn(kind="codex", scenario=s), 0)
        self.assertEqual([c[3:] for c in self.argv_of("agent", "send-keys")], [["esc"], ["enter"]])

    def test_another_dialog_stops_the_spawn(self):
        s = spawn_ok()
        s["agent start"] = [E("agent_not_ready")]
        s["agent read"] = [{"stdout": "  Sign in with ChatGPT\n", "exit": 0}]
        self.assertEqual(self.spawn(kind="codex", scenario=s), 1)
        self.assertEqual(self.argv_of("agent", "send-keys"), [])


class TestLiveFindings(KindBase):
    def test_a_report_with_a_byte_order_mark_is_read(self):
        # codex on Windows wrote report.md with a UTF-8 BOM (seen live)
        path = self.write("r/report.md", u"﻿type: done\nseq: 1\n---\n")
        self.assertEqual(oke.read_header(path), {"type": "done", "seq": 1})

    def test_a_screen_still_drawing_is_looked_at_again(self):
        s = spawn_ok()
        s["agent read"] = [{"stdout": "\n", "exit": 0},
                           {"stdout": "  3. Continue without trusting (hooks won't run)\n", "exit": 0},
                           {"stdout": READY, "exit": 0}]
        s["agent send-keys"] = [J({"type": "ok"})]
        self.assertEqual(self.spawn(kind="codex", scenario=s), 0)
        self.assertEqual([c[3:] for c in self.argv_of("agent", "send-keys")], [["esc"]])

    def test_the_hook_trust_question_is_skipped_before_the_first_prompt(self):
        s = spawn_ok()
        s["agent read"] = [{"stdout": "> 1. Review hooks\n  2. Trust all and continue\n"
                                      "  3. Continue without trusting (hooks won't run)\n", "exit": 0},
                           {"stdout": READY, "exit": 0}]
        s["agent send-keys"] = [J({"type": "ok"})]
        self.assertEqual(self.spawn(kind="codex", scenario=s), 0)
        self.assertEqual(self.argv_of("agent", "send-keys")[0][3:], ["esc"])

    def test_the_hook_question_after_the_first_prompt_is_skipped(self):
        # seen live: codex asks about hooks only when the first prompt is submitted
        s = spawn_ok()
        s["agent read"] = [{"stdout": READY, "exit": 0},
                           {"stdout": "  3. Continue without trusting (hooks won't run)\n", "exit": 0},
                           {"stdout": READY, "exit": 0}]
        s["agent prompt"] = [E("agent_prompt_stalled")]
        s["agent get"] = [J({"agent": {"agent_status": "idle"}}), J({"agent": {"agent_status": "working"}})]
        s["agent send-keys"] = [J({"type": "ok"})]
        self.spawn(kind="codex", scenario=s)
        self.assertEqual([c[3:] for c in self.argv_of("agent", "send-keys")], [["esc"], ["enter"]])

    def test_the_hook_review_panel_is_closed_before_the_first_prompt(self):
        # codex showed it after herdr called the agent ready; the prompt went nowhere
        s = spawn_ok()
        s["agent read"] = [{"stdout": "  1 hook needs review before it can run.\n  t trust all, esc close\n", "exit": 0},
                           {"stdout": READY, "exit": 0}]
        s["agent send-keys"] = [J({"type": "ok"})]
        self.assertEqual(self.spawn(kind="codex", scenario=s), 0)
        names = [" ".join(c[:2]) for c in self.calls()]
        self.assertLess(names.index("agent send-keys"), names.index("agent prompt"))
        self.assertEqual(self.argv_of("agent", "send-keys")[0][3:], ["esc"])


class TestReviewFindings(KindBase):
    def test_a_session_seen_before_the_record_exists_is_written_later(self):
        info = {"agent_status": "working", "agent_session": {"value": "s-early"}}
        self.fake({"agent get": [J({"agent": info})]})
        oke.look_at("fix-a-w1", self.home)  # no children.json record yet
        self.assertEqual(self.spawn(kind="codex"), 0)
        self.fake({"agent get": [J({"agent": info})]})
        oke.look_at("fix-a-w1", self.home)
        self.assertEqual(self.children()["fix-a-w1"]["session"], "s-early")

    def test_kind_ignores_case_and_spaces(self):
        self.assertEqual(self.spawn(kind="Codex "), 0)
        argv = self.start_argv()
        self.assertEqual(argv[argv.index("--kind") + 1], "codex")


class TestSessionLearnedLater(KindBase):
    def test_a_session_herdr_reports_after_the_first_turn_is_recorded(self):
        # codex has no session id until its first turn ends
        self.assertEqual(self.spawn(kind="codex"), 0)
        info = {"agent_status": "done", "agent_session": {"value": "s-codex"}}
        self.fake({"agent get": [J({"agent": info})]})
        oke.look_at("fix-a-w1", self.home)
        self.assertEqual(self.children()["fix-a-w1"]["session"], "s-codex")


if __name__ == "__main__":
    unittest.main()
