"""OrcaRuntime against a fake `orca` binary (the fake herdr script, which answers any
argv from a scenario)."""
import io
import json
import os
import unittest

from test_oke import SpawnBase  # noqa: F401
import oke
import runtime_orca


def OK(result):
    return {"stdout": json.dumps({"ok": True, "result": result}), "exit": 0}


def ERR(code, message=None):
    return {"stdout": json.dumps({"ok": False, "error": {"code": code, "message": message or code + " here"}}),
            "exit": 1}


STARTED = {"dispatchId": "ctx_1", "terminal": "termw_1", "state": "ready"}
WORKING = {"liveness": "live", "activity": "working", "terminalState": "retained"}


class OrcaBase(SpawnBase):
    def fake(self, scenario):
        SpawnBase.fake(self, scenario)
        os.environ["OKE_ORCA"] = os.environ["OKE_HERDR"]

    def setUp(self):
        SpawnBase.setUp(self)
        self.rt = runtime_orca.OrcaRuntime()
        runtime_orca.ENDED_SEEN.clear()

    def register(self, name="fix-a-w1", dispatch="ctx_1", handle="termw_1", **more):
        reg = runtime_orca.load_registry()
        reg[name] = dict({"dispatch": dispatch, "handle": handle, "cwd": self.repo}, **more)
        runtime_orca.save_registry(reg)


class TestOrcaRuntime(OrcaBase):
    def test_status_mapping(self):
        s = runtime_orca.status_of
        self.assertEqual(s(WORKING), "working")
        self.assertEqual(s(dict(WORKING, activity="idle")), "idle")
        self.assertEqual(s(dict(WORKING, activity="waiting")), "blocked")  # a permission prompt
        self.assertEqual(s(dict(WORKING, activity=None)), "working")  # unknown is never dead
        settled = {"liveness": "unverifiable", "stage": "settled", "state": "succeeded"}
        self.assertEqual(s(settled), "idle")  # seen live: a PM done with its task
        # seen live: a PM settled after worker_done, its terminal kept for the next mail
        self.assertEqual(s(dict(settled, liveness="exited", terminalState="retained")), "idle")
        # seen live: Orca closed a settled PM's terminal; the next mail reopens it
        self.assertEqual(s(dict(settled, liveness="exited", terminalState="released")), "idle")
        self.assertIsNone(s(dict(settled, stage="running", activity="working", terminalState="released")))
        self.assertIsNone(s(dict(WORKING, liveness="exited")))  # died mid-task
        self.assertIsNone(s(dict(WORKING, terminalState="released")))
        self.assertIsNone(s(None))  # Orca no longer knows the dispatch
        # seen live after an Orca app restart: the PM's terminal is gone, the task abandoned
        self.assertIsNone(s({"state": "abandoned", "stage": "terminal_missing", "liveness": "unverifiable",
                             "terminalState": "retained"}))

    def test_start_runs_a_kept_open_worker_with_the_model_and_records_it(self):
        self.fake({"orchestration worker-start": [OK(STARTED)]})
        seen = []
        info = self.rt.start("fix-a-w1", self.repo, ["--model", "opus", "--plugin-dir", "/p"], "go",
                             lambda: False, lambda: seen.append(1))
        argv = self.calls()[0]
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertNotIn("--plugin-dir", argv)
        self.assertIn("--keep-open", argv)
        self.assertEqual(argv[argv.index("--worktree") + 1], "path:" + self.repo.replace("\\", "/"))
        spec = argv[argv.index("--spec") + 1]
        self.assertTrue(spec.endswith("go") and "worker_done" in spec)
        self.assertEqual((info["pane_id"], info["agent_session"]), ("termw_1", {"value": "ctx_1"}))
        self.assertEqual(seen, [1])
        self.assertEqual(runtime_orca.load_registry()["fix-a-w1"]["dispatch"], "ctx_1")

    def test_the_tab_says_who_is_there(self):
        self.fake({"orchestration worker-start": [OK(STARTED)], "terminal rename": [OK({})]})
        self.rt.start("pm-toy-6b0484", self.repo, [], "go", lambda: False)
        self.assertEqual(self.calls()[1], ["terminal", "rename", "--terminal", "termw_1",
                                           "--title", "OKe PM: toy", "--json"])
        t = runtime_orca.title_of
        self.assertEqual((t("fix-a-w1"), t("fix-a-rv"), t("odd")),
                         ("OKe worker 1: fix-a", "OKe reviewer: fix-a", "OKe: odd"))

    def test_a_revive_gets_the_dead_sessions_transcript(self):
        projects = os.path.join(self.path("claude"), "projects", "x")
        os.makedirs(projects)
        with io.open(os.path.join(projects, "s.jsonl"), "w", encoding="utf-8") as f:
            f.write(u'{"text": "Your dispatch is ctx_old"}\n')
        os.environ["CLAUDE_CONFIG_DIR"] = self.path("claude")
        self.fake({"orchestration worker-start": [OK(STARTED)]})
        self.rt.start("fix-a-w1", self.repo, ["--resume", "ctx_old"], "go", lambda: False)
        spec = self.calls()[0][self.calls()[0].index("--spec") + 1]
        self.assertIn("s.jsonl", spec)
        self.assertNotIn("--resume", self.calls()[0])

    def test_agents_drop_gone_workers(self):
        self.register()
        self.register("pm-toy", "ctx_2", "termw_2")
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(WORKING)],
                   "orchestration worker-show --dispatch ctx_2": [OK(dict(WORKING, liveness="lost"))]})
        self.assertEqual([(a["name"], a["agent_status"]) for a in self.rt.agents()], [("fix-a-w1", "working")])
        with self.assertRaises(oke.HerdrError) as c:
            self.rt.agent("pm-toy")
        self.assertEqual(c.exception.code, "agent_not_found")

    def test_a_follow_up_dispatch_takes_over_the_entry(self):
        self.register("pm-toy", "ctx_1", "termw_1")
        settled = {"stage": "settled", "liveness": "unverifiable", "runId": "run_1"}
        rows = {"workers": [{"resource": {"terminalHandle": "termw_9", "ownerDispatchId": "ctx_8"}},
                            {"resource": {"terminalHandle": "termw_1", "ownerDispatchId": "ctx_2"}}]}
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(settled)],
                   "orchestration worker-show --dispatch ctx_2": [OK(WORKING)],
                   "orchestration worker-list": [OK(rows)],
                   "orchestration worker-keep-open": [OK({})],
                   "orchestration send": [OK({"id": "msg_1"})]})
        self.assertEqual(self.rt.agent("pm-toy")["agent_status"], "working")
        self.assertIn(["orchestration", "worker-keep-open", "--dispatch", "ctx_2", "--json"], self.calls())
        entry = runtime_orca.load_registry()["pm-toy"]
        self.assertEqual((entry["dispatch"], entry["origin"]), ("ctx_2", "ctx_1"))
        self.assertEqual(self.rt.agent("pm-toy")["agent_session"], {"value": "ctx_1"})  # the transcript's id
        self.rt.prompt("pm-toy", "go")
        argv = self.calls()[-1]
        self.assertEqual(argv[argv.index("--to") + 1], "dispatch:ctx_2")

    def test_a_follow_up_in_a_new_terminal_is_found_by_the_dispatch_it_follows(self):
        # seen live: Orca closed the PM's terminal, and OKe's mail reopened it in a new one
        self.register("pm-toy", "ctx_1", "term_1")
        settled = {"stage": "settled", "liveness": "exited", "terminalState": "released", "runId": "run_1"}
        rows = {"workers": [
            {"dispatchId": "ctx_3", "resource": {"terminalHandle": "term_3", "ownerDispatchId": "ctx_3"}},
            {"dispatchId": "ctx_2", "resource": {"terminalHandle": "term_2", "ownerDispatchId": "ctx_2"}},
            {"dispatchId": "ctx_1", "resource": {"terminalHandle": "term_1", "ownerDispatchId": "ctx_1",
                                                 "releaseState": "released"}}]}
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(settled)],
                   "orchestration worker-show --dispatch ctx_3 --full": [OK({"dispatch": {"followUpOf": "ctx_9"}})],
                   "orchestration worker-show --dispatch ctx_2 --full": [OK({"dispatch": {"followUpOf": "ctx_1"},
                                                                            "worker": {"agentTerminalHandle": "term_2"}})],
                   "orchestration worker-show --dispatch ctx_2": [OK(WORKING)],
                   "orchestration worker-list": [OK(rows)],
                   "orchestration worker-keep-open": [OK({})]})
        agent = self.rt.agent("pm-toy")
        # the first handle stays its place (OKe's state records it); it runs in term_2
        self.assertEqual((agent["agent_status"], agent["pane_id"], agent["terminal_id"]),
                         ("working", "term_1", "term_2"))
        entry = runtime_orca.load_registry()["pm-toy"]
        self.assertEqual((entry["dispatch"], entry["handle"], entry["terminal"], entry["origin"]),
                         ("ctx_2", "term_1", "term_2", "ctx_1"))
        self.assertIn(["orchestration", "worker-keep-open", "--dispatch", "ctx_2", "--json"], self.calls())
        self.assertEqual(self.rt.canonical("term_2"), "term_1")  # its mod ticks with term_2
        self.assertEqual(self.rt.canonical("term_7"), "term_7")
        self.assertEqual(self.rt.entry_of("term_1")[0], "pm-toy")  # close and cancel find it

    def test_a_follow_up_is_found_while_orca_still_keeps_the_old_terminal(self):
        # seen live (O2): after an Orca restart the old terminal stays "retained" under the
        # settled dispatch, mail reopened the PM in a new terminal, and OKe kept reading the
        # old dispatch as idle while the PM worked: "PM silent" during its merge check
        self.register("pm-toy", "ctx_1", "term_1")
        settled = {"stage": "settled", "liveness": "unverifiable", "terminalState": "retained", "runId": "run_1"}
        rows = {"workers": [
            {"dispatchId": "ctx_2", "resource": {"terminalHandle": "term_2", "ownerDispatchId": "ctx_2"}},
            {"dispatchId": "ctx_1", "resource": {"terminalHandle": "term_1", "ownerDispatchId": "ctx_1"}}]}
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(settled)],
                   "orchestration worker-show --dispatch ctx_2 --full": [OK({"dispatch": {"followUpOf": "ctx_1"},
                                                                            "worker": {"agentTerminalHandle": "term_2"}})],
                   "orchestration worker-show --dispatch ctx_2": [OK(WORKING)],
                   "orchestration worker-list": [OK(rows)],
                   "orchestration worker-keep-open": [OK({})]})
        self.assertEqual(self.rt.agent("pm-toy")["agent_status"], "working")
        self.assertEqual(runtime_orca.load_registry()["pm-toy"]["dispatch"], "ctx_2")

    def test_a_resting_worker_asks_each_newer_dispatch_once(self):
        self.register("pm-toy", "ctx_1", "term_1")
        settled = {"stage": "settled", "liveness": "exited", "terminalState": "released", "runId": "run_1"}
        rows = {"workers": [
            {"dispatchId": "ctx_3", "resource": {"terminalHandle": "term_3", "ownerDispatchId": "ctx_3"}},
            {"dispatchId": "ctx_1", "resource": {"terminalHandle": "term_1", "ownerDispatchId": "ctx_1",
                                                 "releaseState": "released"}}]}
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(settled)],
                   "orchestration worker-show --dispatch ctx_3 --full": [OK({"dispatch": {"followUpOf": "ctx_9"}})],
                   "orchestration worker-list": [OK(rows)]})
        for _ in range(3):
            self.assertEqual(self.rt.agent("pm-toy")["agent_status"], "idle")
        asked = [c for c in self.calls() if c[:4] == ["orchestration", "worker-show", "--dispatch", "ctx_3"]]
        self.assertEqual(len(asked), 1)

    KILLED = {"state": "ready", "stage": "input_accepted", "liveness": "unverifiable",
              "terminalState": "retained", "attention": ["unverifiable"]}
    DROPPED = {"projection": {"liveness": {"verdict": "unverifiable", "reason": "missing_status"},
                              "evidence": {"liveStatus": "unavailable", "lastObservedAt": None}}}

    def test_an_agent_killed_mid_turn_is_gone(self):
        # measured live: taskkill of a worker's claude mid-turn; Orca keeps the summary
        # ready/input_accepted/unverifiable and drops the agent's status row
        self.register(kind="claude")
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(WORKING), OK(self.KILLED)],
                   "orchestration worker-show --dispatch ctx_1 --full": [OK(self.DROPPED)]})
        self.assertEqual(self.rt.agent("fix-a-w1")["agent_status"], "working")
        self.assertEqual(runtime_orca.load_registry()["fix-a-w1"]["live"], "ctx_1")  # kept for later sessions
        with self.assertRaises(oke.HerdrError) as c:
            self.rt.agent("fix-a-w1")
        self.assertEqual(c.exception.code, "agent_not_found")
        self.assertEqual(self.rt.agents(), [])
        # a dead dispatch is asked in full once per process
        self.assertEqual(len([c for c in self.calls() if "--full" in c]), 1)

    def test_a_starting_agent_is_not_gone(self):
        # measured live: a follow-up reads terminal_readying, then authority_attached with
        # missing_status and no evidence for ~15 s, before its first hook
        self.register(kind="claude", live="ctx_0")  # its previous dispatch read live, not this one
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(dict(self.KILLED, stage="authority_attached"))],
                   "orchestration worker-show --dispatch ctx_1 --full": [OK(self.DROPPED)]})
        self.assertEqual(self.rt.agent("fix-a-w1")["agent_status"], "working")
        self.assertFalse([c for c in self.calls() if "--full" in c])

    def follow_up_scenario(self, sent, show2):
        self.register("pm-toy", "ctx_1", "term_1", kind="claude", live="ctx_1")
        # a settled dispatch can read live (its terminal kept): that must not move the mark
        settled = {"stage": "settled", "state": "succeeded", "liveness": "live", "activity": "idle",
                   "runId": "run_1"}
        rows = {"workers": [{"dispatchId": "ctx_2", "resource": {"terminalHandle": "term_2", "ownerDispatchId": "ctx_2"}},
                            {"dispatchId": "ctx_1", "resource": {"terminalHandle": "term_1", "ownerDispatchId": "ctx_1",
                                                                 "releaseState": "released"}}]}
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(settled)],
                   "orchestration worker-list": [OK({"workers": []}), OK(rows)],
                   "orchestration send": [OK(sent)],
                   "orchestration worker-show --dispatch ctx_2 --full": [
                       OK({"dispatch": {"followUpOf": "ctx_1"}, "worker": {"agentTerminalHandle": "term_2"}}),
                       OK(self.DROPPED)],
                   "orchestration worker-show --dispatch ctx_2": [OK(show2)],
                   "orchestration worker-keep-open": [OK({})]})
        self.assertTrue(self.rt.prompt("pm-toy", "go"))

    def test_a_follow_up_killed_right_after_the_mail_is_gone(self):
        # measured live: mail reopened a closed PM; send answered state ready (Orca saw the
        # turn start), the claude was killed 3 s later, before any look read it live
        self.follow_up_scenario({"delivery": "reopened", "followUpOf": "ctx_1", "dispatchId": "ctx_2",
                                 "state": "ready"}, self.KILLED)
        entry = runtime_orca.load_registry()["pm-toy"]
        # e2e: the row moves to the follow-up and its new terminal at once, not on a later look
        self.assertEqual((entry["live"], entry["dispatch"], entry["terminal"], entry["handle"], entry["origin"]),
                         ("ctx_2", "ctx_2", "term_2", "term_1", "ctx_1"))
        self.assertIn(["orchestration", "worker-keep-open", "--dispatch", "ctx_2", "--json"], self.calls())
        self.assertEqual(self.rt.agents(), [])

    def test_a_follow_up_whose_turn_start_orca_never_saw_is_not_gone(self):
        # measured live: claude killed during the follow-up's startup; send answered
        # outcome_unknown, the dispatch reads start_unknown/turn_start_unobserved with
        # missing_status for good, the same as a slow start ("may still be starting")
        self.follow_up_scenario({"delivery": "reopened", "dispatchId": "ctx_2", "state": "outcome_unknown"},
                                dict(self.KILLED, state="start_unknown", stage="turn_start_unobserved",
                                     terminalState="active"))
        entry = runtime_orca.load_registry()["pm-toy"]
        self.assertEqual((entry["live"], entry["dispatch"], entry["terminal"]), ("ctx_1", "ctx_2", "term_2"))
        # blocked: the user is asked (revive or wait), nothing reads it dead
        self.assertEqual([a["agent_status"] for a in self.rt.agents()], ["blocked"])
        shows = [c[3] for c in self.calls() if "--full" in c and c[1] == "worker-show"]
        self.assertEqual(shows, ["ctx_2"])  # only the follow-up search
        note = self.rt.screen("pm-toy")
        self.assertIn("pm-toy may have failed to start", note)
        self.assertIn("`$OKE close term_1`, then `$OKE revive pm-toy --reset`", note)
        self.assertFalse([c for c in self.calls() if c[1] == "worker-read"])

    UNSEEN = {"state": "start_unknown", "stage": "turn_start_unobserved", "liveness": "unverifiable",
              "terminalState": "active", "attention": ["unverifiable"]}

    def test_a_start_orca_never_saw_is_blocked_until_its_turn_comes(self):
        self.register(kind="claude")
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(self.UNSEEN), OK(self.UNSEEN), OK(WORKING)],
                   "orchestration worker-read": [OK({"transcript": {"messages": []}})]})
        self.assertEqual(self.rt.agent("fix-a-w1")["agent_status"], "blocked")
        self.assertIn("may have failed to start", self.rt.screen("fix-a-w1"))
        # a slow start: Orca moves start_unknown to ready when the turn comes
        self.assertEqual(self.rt.agent("fix-a-w1")["agent_status"], "working")
        self.assertEqual(self.rt.screen("fix-a-w1"), "\n")  # the transcript again
        self.assertFalse([c for c in self.calls() if "--full" in c])

    def test_a_dispatch_seen_live_and_killed_in_start_unknown_is_gone(self):
        # its live mark is this very dispatch: it started, and Orca dropped its status row
        # because the process is gone (a live claude keeps the row): gone, not blocked
        self.register(kind="claude", live="ctx_1")
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(self.UNSEEN)],
                   "orchestration worker-show --dispatch ctx_1 --full": [OK(self.DROPPED)]})
        self.assertEqual(self.rt.agents(), [])

    def test_a_dispatch_seen_live_in_start_unknown_with_its_status_row_is_blocked(self):
        self.register(kind="claude", live="ctx_1")
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(self.UNSEEN)],
                   "orchestration worker-show --dispatch ctx_1 --full": [OK({"projection": {
                       "liveness": {"verdict": "unverifiable", "reason": "stale_status"},
                       "evidence": {"liveStatus": "stale", "lastObservedAt": 1795232230069}}})]})
        self.assertEqual([a["agent_status"] for a in self.rt.agents()], ["blocked"])

    def test_a_mail_answered_by_a_revived_agent_keeps_its_new_row(self):
        self.register("pm-toy", "ctx_5", "term_5", kind="claude")  # a revive replaced the row
        self.fake({"orchestration worker-show --dispatch ctx_2 --full": [OK({"worker": {"agentTerminalHandle": "term_2"}})],
                   "orchestration worker-keep-open": [OK({})]})
        self.rt.sent({"dispatch": "ctx_1", "handle": "term_1"}, "pm-toy", {"dispatchId": "ctx_2", "state": "ready"})
        self.assertEqual(runtime_orca.load_registry()["pm-toy"]["dispatch"], "ctx_5")

    def test_a_follow_up_whose_terminal_cannot_be_read_stays_for_a_look_but_is_live(self):
        self.register("pm-toy", "ctx_1", "term_1", kind="claude")
        self.fake({"orchestration worker-show --dispatch ctx_2 --full": [ERR("orca_failed")]})
        self.rt.sent(runtime_orca.load_registry()["pm-toy"], "pm-toy", {"dispatchId": "ctx_2", "state": "ready"})
        entry = runtime_orca.load_registry()["pm-toy"]
        # the row stays (follow_up moves it with the real terminal); the live mark is kept
        self.assertEqual((entry["dispatch"], entry.get("terminal"), entry["live"]), ("ctx_1", None, "ctx_2"))

    def test_a_failed_keep_open_keeps_the_move_and_live_mark(self):
        self.register("pm-toy", "ctx_1", "term_1", kind="claude")
        self.fake({"orchestration worker-show --dispatch ctx_2 --full": [OK({"worker": {"agentTerminalHandle": "term_2"}})],
                   "orchestration worker-keep-open": [ERR("herdr_unavailable")]})
        self.rt.sent(runtime_orca.load_registry()["pm-toy"], "pm-toy", {"dispatchId": "ctx_2", "state": "ready"})
        entry = runtime_orca.load_registry()["pm-toy"]
        self.assertEqual((entry["dispatch"], entry["terminal"], entry["live"]), ("ctx_2", "term_2", "ctx_2"))

    def test_start_unknown_is_only_blocked_for_claude(self):
        self.register("w-old", "ctx_2", "s2")  # no `kind`
        self.fake({"orchestration worker-show": [OK(self.UNSEEN)]})
        self.assertEqual([a["agent_status"] for a in self.rt.agents()], ["working"])

    def test_a_dead_agent_in_another_unsettled_stage_is_gone(self):
        self.register(kind="claude", live="ctx_1")
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(dict(self.KILLED, stage="turn_ended"))],
                   "orchestration worker-show --dispatch ctx_1 --full": [OK(self.DROPPED)]})
        self.assertEqual(self.rt.agents(), [])

    def test_only_a_dropped_status_row_is_death(self):
        self.register("w-stale", "ctx_2", "s2", kind="claude", live="ctx_2")
        self.register("w-seen", "ctx_3", "s3", kind="claude", live="ctx_3")
        self.register("w-fail", "ctx_4", "s4", kind="claude", live="ctx_4")
        self.register("w-odd", "ctx_5", "s5", kind="claude", live="ctx_5")
        self.register("w-old", "ctx_7", "s7", live="ctx_7")  # no `kind`
        self.fake({"orchestration worker-show": [OK(self.KILLED)],
                   "orchestration worker-show --dispatch ctx_7 --full": [OK(self.DROPPED)],
                   # a long silent turn: Orca keeps its last status, only stale
                   "orchestration worker-show --dispatch ctx_2 --full": [OK({"projection": {
                       "liveness": {"verdict": "unverifiable", "reason": "stale_status"},
                       "evidence": {"liveStatus": "stale", "lastObservedAt": 1795232230069}}})],
                   # missing_status with evidence (a provider session row only): not a dropped row
                   "orchestration worker-show --dispatch ctx_3 --full": [OK({"projection": {
                       "liveness": {"verdict": "unverifiable", "reason": "missing_status"},
                       "evidence": {"liveStatus": "stale", "lastObservedAt": 1795232230069}}})],
                   "orchestration worker-show --dispatch ctx_4 --full": [ERR("orca_failed")],
                   "orchestration worker-show --dispatch ctx_5 --full": [OK({"projection": ["odd"]})]})
        self.assertEqual(sorted(a["name"] for a in self.rt.agents() if a["agent_status"] == "working"),
                         ["w-fail", "w-odd", "w-old", "w-seen", "w-stale"])
        asked = sorted(c[3] for c in self.calls() if "--full" in c)
        self.assertEqual(asked, ["ctx_2", "ctx_3", "ctx_4", "ctx_5"])  # never for an entry without `kind`

    def test_odd_full_results_are_not_death(self):
        for result in (["x"], {"projection": {"liveness": "missing_status", "evidence": None}},
                       {"projection": {"liveness": {"reason": "missing_status"}, "evidence": "none"}}):
            self.fake({"orchestration worker-show --dispatch ctx_9 --full": [OK(result)]})
            self.assertFalse(runtime_orca.agent_ended("ctx_9"))

    def test_start_records_the_kind(self):
        self.fake({"orchestration worker-start": [OK(STARTED)]})
        self.rt.start("fix-a-w1", self.repo, [], "go", lambda: False)
        self.assertEqual(runtime_orca.load_registry()["fix-a-w1"]["kind"], "claude")
        argv = self.calls()[0]
        self.assertEqual(argv[argv.index("--agent") + 1], "claude")

    def test_a_chat_default_is_refused_and_its_worker_stopped(self):
        # measured: with Orca's default agent surface chat, worker-start gives a structworker_ handle
        self.fake({"orchestration worker-start": [OK(dict(STARTED, terminal="structworker_1"))],
                   "orchestration worker-stop": [OK({})], "orchestration worker-release": [OK({})]})
        with self.assertRaises(oke.HerdrError) as c:
            self.rt.start("pm-toy-abc123", self.repo, [], "go", lambda: False)
        self.assertEqual(c.exception.code, "orca_chat_default")
        self.assertIn("CLI", c.exception.message)
        cmds = [x[:2] for x in self.calls()]
        self.assertIn(["orchestration", "worker-stop"], cmds)
        self.assertIn(["orchestration", "worker-release"], cmds)
        self.assertNotIn("pm-toy-abc123", runtime_orca.load_registry())

    def test_a_chat_worker_that_cannot_be_stopped_is_named(self):
        # it already has its task: the user must close it, else two would work the job
        for stop in (ERR("boom"), ERR("herdr_unavailable")):
            self.fake({"orchestration worker-start": [OK(dict(STARTED, terminal="structworker_1"))],
                       "orchestration worker-stop": [stop], "orchestration worker-release": [ERR("boom")]})
            with self.assertRaises(oke.HerdrError) as c:
                self.rt.start("pm-toy-abc123", self.repo, [], "go", lambda: False)
            self.assertEqual(c.exception.code, "orca_chat_default")  # never a bare outage
            self.assertIn("ctx_1", c.exception.message)
            self.assertIn("structworker_1", c.exception.message)

    def test_a_fenced_chat_worker_stop_rebinds(self):
        self.fake({"orchestration worker-start": [OK(dict(STARTED, terminal="structworker_1", runId="run_3"))],
                   "orchestration worker-stop": [ERR("consumer_fenced"), OK({})],
                   "orchestration run-use": [OK({})], "orchestration worker-release": [OK({})]})
        with self.assertRaises(oke.HerdrError) as c:
            self.rt.start("pm-toy-abc123", self.repo, [], "go", lambda: False)
        self.assertNotIn("could not stop", c.exception.message)
        self.assertIn(["orchestration", "run-use"], [x[:2] for x in self.calls()])

    def test_oke_refuses_to_run_in_an_orca_chat_and_never_falls_to_bg(self):
        os.environ.pop("OKE_RUNTIME", None)
        os.environ["ORCA_CHAT_SCRATCH_DIR"] = self.tmp
        self.assertEqual(oke.default_runtime(), "orca")
        self.assertEqual(self.cli("status", "--claim"), 1)
        self.assertIn("not a chat", self.err)
        os.environ.pop("ORCA_CHAT_SCRATCH_DIR")
        os.environ["ORCA_TERMINAL_HANDLE"] = "structworker_7"
        self.assertTrue(runtime_orca.in_chat())

    def test_a_worker_killed_before_the_first_look_is_gone(self):
        # measured: worker-start returns only after the agent's first hook
        self.fake({"orchestration worker-start": [OK(STARTED)],
                   "orchestration worker-show --dispatch ctx_1": [OK(self.KILLED)],
                   "orchestration worker-show --dispatch ctx_1 --full": [OK(self.DROPPED)]})
        self.rt.start("fix-a-w1", self.repo, [], "go", lambda: False)
        self.assertEqual(runtime_orca.load_registry()["fix-a-w1"]["live"], "ctx_1")
        self.assertEqual(self.rt.agents(), [])

    def test_a_held_registry_lock_does_not_break_a_look(self):
        from unittest import mock
        self.register(kind="claude")
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(WORKING)]})
        locked = mock.patch.object(runtime_orca, "update_registry",
                                   side_effect=oke.HerdrError("registry_locked", "locked"))
        with locked:
            self.assertEqual(self.rt.agent("fix-a-w1")["agent_status"], "working")
        other = mock.patch.object(runtime_orca, "update_registry", side_effect=oke.HerdrError("boom", "x"))
        with other, self.assertRaises(oke.HerdrError):
            self.rt.agent("fix-a-w1")

    def test_a_live_or_settled_worker_needs_no_full_read(self):
        self.register(kind="claude", live="ctx_1")
        self.register("pm-toy", "ctx_2", "termw_2", kind="claude", live="ctx_2")
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(WORKING)],
                   "orchestration worker-show --dispatch ctx_2": [OK(
                       {"stage": "settled", "state": "succeeded", "liveness": "unverifiable"})]})
        self.assertEqual([a["agent_status"] for a in self.rt.agents()], ["working", "idle"])
        self.assertFalse([c for c in self.calls() if "--full" in c])

    def test_an_unknown_dispatch_is_gone(self):
        self.register()
        self.fake({"orchestration worker-show": [ERR("dispatch_not_found")]})
        self.assertEqual(self.rt.agents(), [])

    def test_the_registry_survives_parallel_writers(self):
        import threading
        names = ["w%d" % i for i in range(8)]
        threads = [threading.Thread(target=runtime_orca.update_registry,
                                    args=(lambda r, n=n: r.__setitem__(n, {"dispatch": n}),)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(runtime_orca.load_registry()), names)

    def test_orca_down_is_unavailable_not_dead(self):
        self.register()
        self.fake({"orchestration worker-show": [ERR("runtime_unavailable")]})
        with self.assertRaises(oke.HerdrError) as c:
            self.rt.agents()
        self.assertEqual(c.exception.code, "herdr_unavailable")

    def test_a_cli_that_answers_no_json_is_unavailable_not_dead(self):
        # the Linux CLI shim's refusal, and a launcher failing with empty stderr
        for broken in ({"stderr": "Orca CLI is unavailable; reopen Orca or register the CLI again.\n", "exit": 1},
                       {"exit": 1}):
            self.register()
            self.fake({"orchestration worker-show": [broken]})
            with self.assertRaises(oke.HerdrError) as c:
                self.rt.agents()
            self.assertEqual(c.exception.code, "herdr_unavailable")

    def test_prompt_is_orca_mail_to_the_dispatch(self):
        self.register()
        self.fake({"orchestration send": [OK({"id": "msg_1"})],
                   "orchestration worker-show": [OK(WORKING)]})
        self.assertTrue(self.rt.prompt("fix-a-w1", "OKe inbox: go"))
        argv = self.calls()[-1]
        self.assertEqual(argv[argv.index("--to") + 1], "dispatch:ctx_1")
        self.assertEqual(argv[argv.index("--body") + 1], "OKe inbox: go")

    def test_a_fenced_send_resumes_the_run_and_retries(self):
        # a new orchestrator session is not bound to the Run its PM was started in
        self.register()
        self.fake({"orchestration send": [ERR("consumer_fenced"), OK({"id": "msg_1"})],
                   "orchestration run-use": [OK({})],
                   "orchestration worker-show": [OK(dict(WORKING, runId="run_7"))]})
        self.assertTrue(self.rt.prompt("fix-a-w1", "OKe inbox: go"))
        cmds = [c[:2] for c in self.calls()]
        self.assertEqual(cmds[-3:], [["orchestration", "send"], ["orchestration", "run-use"], ["orchestration", "send"]])
        self.assertIn("run_7", self.calls()[-2])

    def test_a_dispatch_orca_cannot_continue_is_a_gone_agent(self):
        # seen live after the Orca app was killed: worker-show still reads settled
        self.register("pm-r-abc123", "ctx_2f", "term_9")
        msg = ("Dispatch ctx_2f has no worker Orca started that it can continue; start a new worker "
               "with worker-start. No effects were applied.")
        self.fake({"orchestration send": [ERR("runtime_error", msg)],
                   "orchestration worker-release": [OK({})],
                   "orchestration worker-show": [OK(dict(WORKING, activity="idle"))]})
        with self.assertRaises(runtime_orca.HerdrError) as c:
            self.rt.prompt("pm-r-abc123", "OKe inbox: go")
        self.assertEqual(c.exception.code, "agent_not_found")
        self.assertNotIn("pm-r-abc123", runtime_orca.load_registry())  # a revive may start it again
        self.assertIn(["orchestration", "worker-release"], [c[:2] for c in self.calls()])

    def test_a_follow_up_orca_cannot_start_is_a_gone_agent(self):
        # O5, seen live: Orca killed and restarted; the PM's mail needs a follow-up dispatch,
        # which fails at terminal_create. Its worker-show (verbatim fields) reads failed.
        self.register("pm-repo-4612b2", "ctx_55cf9f4912f7", "term_9")
        settled = {"dispatchId": "ctx_55cf9f4912f7", "runId": "run_a6", "state": "succeeded", "stage": "settled",
                   "liveness": "unverifiable", "terminal": "term_9", "terminalState": "retained"}
        failed = {"dispatchId": "ctx_f04d0236a217", "taskId": "task_cb381d4c6199", "runId": "run_a6",
                  "followUpOf": "ctx_55cf9f4912f7", "state": "failed", "stage": "terminal_create",
                  "liveness": "unverifiable", "attention": ["failure", "unverifiable"], "waitingOnHuman": "unknown",
                  "lastError": "never started: The worker's conversation is open somewhere Orca cannot reach; "
                               "nothing was sent."}
        self.assertIsNone(runtime_orca.status_of(failed))
        # `worker_done --outcome failed`: failed but settled, alive and idle (no second worker)
        self.assertEqual(runtime_orca.status_of({"state": "failed", "stage": "settled", "activity": "idle",
                                                 "liveness": "live", "terminalState": "retained"}), "idle")
        msg = ("error: start_failed: terminal_create: The worker's conversation is open somewhere Orca "
               "cannot reach; nothing was sent.")
        self.fake({"orchestration send": [{"stdout": "", "stderr": msg, "exit": 1}],
                   "orchestration worker-show": [OK(settled), OK(settled),
                                                 OK({"dispatch": {"followUpOf": "ctx_55cf9f4912f7"}}), OK(failed)],
                   "orchestration worker-list": [OK({"workers": []}),
                                                 OK({"workers": [{"dispatchId": "ctx_f04d0236a217"}]})],
                   "orchestration worker-keep-open": [OK({})],
                   "orchestration worker-release": [OK({})]})
        with self.assertRaises(runtime_orca.HerdrError) as c:
            self.rt.prompt("pm-repo-4612b2", "OKe inbox: merge")
        self.assertEqual(c.exception.code, "agent_not_found")
        self.assertNotIn("pm-repo-4612b2", runtime_orca.load_registry())  # gone: a revive starts a fresh one
        self.assertEqual([c for c in self.calls() if c[:2] == ["orchestration", "send"]].__len__(), 1)
        # both released: the old settled one keeps the retained terminal
        released = [c[c.index("--dispatch") + 1] for c in self.calls() if c[:2] == ["orchestration", "worker-release"]]
        self.assertEqual(sorted(released), ["ctx_55cf9f4912f7", "ctx_f04d0236a217"])

    def test_a_fenced_send_without_a_run_still_fails(self):
        self.register()
        self.fake({"orchestration send": [ERR("consumer_fenced")],
                   "orchestration worker-show": [OK(WORKING)]})
        with self.assertRaises(runtime_orca.HerdrError):
            self.rt.prompt("fix-a-w1", "OKe inbox: go")

    def test_the_incident_a_resting_pm_from_an_earlier_session(self):
        # company PC: the PM settled in the earlier session's Run, no follow-up since;
        # the new orchestrator's mail is fenced until it binds that Run
        self.register("pm-master-abc123", "ctx_bc", "term_19")
        settled = {"dispatchId": "ctx_bc", "runId": "run_79", "state": "succeeded", "stage": "settled",
                   "liveness": "unverifiable", "terminal": "term_19", "terminalState": "retained"}
        rows = {"workers": [{"dispatchId": "ctx_bc", "resource": {"terminalHandle": "term_19",
                                                                  "ownerDispatchId": "ctx_bc"}}]}
        self.fake({"orchestration worker-show": [OK(settled)],
                   "orchestration worker-list": [OK(rows)],
                   "orchestration send": [ERR("consumer_fenced"), OK({"id": "msg_1"})],
                   "orchestration run-use": [OK({})]})
        self.assertEqual(self.rt.agent("pm-master-abc123")["agent_status"], "idle")
        self.assertNotIn("run-use", [c[1] for c in self.calls()])  # a status read binds nothing
        self.assertTrue(self.rt.prompt("pm-master-abc123", "OKe inbox: job j"))
        self.assertEqual([c[1] for c in self.calls()][-3:], ["send", "run-use", "send"])
        self.assertIn(["orchestration", "run-use", "--id", "run_79", "--json"], self.calls())

    def test_a_fenced_keep_open_does_not_rebind(self):
        self.register("fix-a-rv", "ctx_1", "term_1")
        settled = {"stage": "settled", "liveness": "exited", "terminalState": "released", "runId": "run_1"}
        rows = {"workers": [
            {"dispatchId": "ctx_2", "resource": {"terminalHandle": "term_1", "ownerDispatchId": "ctx_2"}}]}
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(settled)],
                   "orchestration worker-show --dispatch ctx_2": [OK(WORKING)],
                   "orchestration worker-list": [OK(rows)],
                   "orchestration worker-keep-open": [ERR("consumer_fenced")]})
        self.assertEqual(self.rt.agent("fix-a-rv")["agent_status"], "working")
        self.assertNotIn("run-use", [c[1] for c in self.calls()])

    def test_a_refused_bind_raises_the_fence(self):
        self.register()
        self.fake({"orchestration send": [ERR("consumer_fenced")],
                   "orchestration run-use": [ERR("invalid_argument")],
                   "orchestration worker-show": [OK(dict(WORKING, runId="run_7"))]})
        with self.assertRaises(runtime_orca.HerdrError) as e:
            self.rt.prompt("fix-a-w1", "OKe inbox: go")
        self.assertEqual(e.exception.code, "consumer_fenced")

    def test_an_outage_in_the_bind_stays_an_outage(self):
        self.register()
        self.fake({"orchestration send": [ERR("consumer_fenced")],
                   "orchestration run-use": [ERR("runtime_unavailable")],
                   "orchestration worker-show": [OK(dict(WORKING, runId="run_7"))]})
        with self.assertRaises(runtime_orca.HerdrError) as e:
            self.rt.prompt("fix-a-w1", "OKe inbox: go")
        self.assertEqual(e.exception.code, "herdr_unavailable")

    def test_a_fenced_stop_rebinds_and_forgets(self):
        self.register()
        self.fake({"orchestration worker-stop": [ERR("consumer_fenced"), OK({})],
                   "orchestration run-use": [OK({})],
                   "orchestration worker-show": [OK(dict(WORKING, runId="run_7"))]})
        self.rt.close("termw_1")
        self.assertEqual([c[1] for c in self.calls()][-2:], ["run-use", "worker-stop"])
        self.assertEqual(runtime_orca.load_registry(), {})

    def test_a_fenced_revive_binds_the_dead_agents_run(self):
        # relaunch closes the dead agent first (its row leaves the registry), then starts
        self.register()
        self.fake({"orchestration worker-start": [ERR("consumer_fenced"), OK(dict(STARTED, dispatchId="ctx_9"))],
                   "orchestration worker-stop": [ERR("already_settled")],
                   "orchestration run-use": [OK({})],
                   "orchestration worker-show": [OK(dict(WORKING, runId="run_7"))],
                   "terminal rename": [OK({})]})
        self.rt.close("termw_1")
        self.assertEqual(runtime_orca.load_registry(), {})
        n = len(self.calls())
        self.rt.start("fix-a-w1", self.repo, ["--resume", "ctx_1"], "go", lambda: False)
        self.assertIn(["orchestration", "worker-show", "--dispatch", "ctx_1", "--json"], self.calls()[n:])
        self.assertIn(["orchestration", "run-use", "--id", "run_7", "--json"], self.calls()[n:])
        self.assertEqual(runtime_orca.load_registry()["fix-a-w1"]["dispatch"], "ctx_9")

    def test_an_outage_in_the_revive_lookup_stays_an_outage(self):
        self.fake({"orchestration worker-start": [ERR("consumer_fenced")],
                   "orchestration worker-show": [ERR("runtime_unavailable")]})
        with self.assertRaises(runtime_orca.HerdrError) as e:
            self.rt.start("fix-a-w1", self.repo, ["--resume", "ctx_1"], "go", lambda: False)
        self.assertEqual(e.exception.code, "herdr_unavailable")

    def test_a_fenced_start_without_resume_still_fails(self):
        self.fake({"orchestration worker-start": [ERR("consumer_fenced")]})
        with self.assertRaises(runtime_orca.HerdrError) as e:
            self.rt.start("fix-a-w1", self.repo, [], "go", lambda: False)
        self.assertEqual(e.exception.code, "consumer_fenced")
        self.assertEqual([c[1] for c in self.calls()], ["worker-start"])

    def test_an_unknown_cli_is_never_read_as_gone(self):
        self.register()
        os.environ["OKE_ORCA"] = ""
        os.environ.pop("ORCA_CLI_COMMAND", None)
        which, platform, real = runtime_orca.shutil.which, runtime_orca.sys.platform, runtime_orca.os.path.realpath
        runtime_orca.sys.platform = "linux"
        runtime_orca.os.path.realpath = lambda p: p
        runtime_orca.shutil.which = lambda n: "/usr/bin/orca" if n == "orca" else None
        try:
            with self.assertRaises(runtime_orca.HerdrError) as e:
                self.rt.agents()
        finally:
            runtime_orca.shutil.which, runtime_orca.sys.platform = which, platform
            runtime_orca.os.path.realpath = real
        self.assertEqual(e.exception.code, "orca_cli_unknown")

    def test_close_releases_a_settled_worker(self):
        self.register()
        self.fake({"orchestration worker-stop": [OK({"state": "succeeded", "alreadySettled": True,
                                                       "processAction": "none"})],
                   "orchestration worker-release": [OK({"state": "released"})],
                   "orchestration worker-show": [OK(WORKING)]})
        self.rt.close("termw_1")
        self.assertEqual([c[1] for c in self.calls()][-2:], ["worker-stop", "worker-release"])
        self.assertEqual(self.calls()[-1][2:4], ["--dispatch", "ctx_1"])
        self.assertEqual(runtime_orca.load_registry(), {})

    def test_close_of_a_running_worker_only_stops(self):
        self.register()
        self.fake({"orchestration worker-stop": [OK({"state": "stopped", "processAction": "closed_agent_terminal"})],
                   "orchestration worker-show": [OK(WORKING)]})
        self.rt.close("termw_1")
        self.assertNotIn("worker-release", [c[1] for c in self.calls()])

    def test_close_of_an_unknown_place_says_so(self):
        self.register()
        with self.assertRaises(runtime_orca.HerdrError) as e:
            self.rt.close("fix-a-w1")  # the agent's name, not its pane
        self.assertEqual(e.exception.code, "agent_not_found")
        self.assertIn("fix-a-w1", runtime_orca.load_registry())

    def test_close_stops_and_forgets(self):
        self.register()
        self.fake({"orchestration worker-show": [ERR("dispatch_not_found")],
                   "orchestration worker-stop": [ERR("already_settled")]})
        self.rt.close("termw_1")
        self.assertEqual(runtime_orca.load_registry(), {})
        self.rt.close(self.repo)  # a folder: nothing started there

    def test_close_stops_the_follow_up_that_runs_now(self):
        self.register("fix-a-rv", "ctx_1", "term_1")
        settled = {"stage": "settled", "liveness": "exited", "terminalState": "released", "runId": "run_1"}
        rows = {"workers": [
            {"dispatchId": "ctx_2", "resource": {"terminalHandle": "term_2", "ownerDispatchId": "ctx_2"}},
            {"dispatchId": "ctx_1", "resource": {"terminalHandle": "term_1", "releaseState": "released"}}]}
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(settled)],
                   "orchestration worker-show --dispatch ctx_2 --full": [OK({"dispatch": {"followUpOf": "ctx_1"}})],
                   "orchestration worker-show --dispatch ctx_2": [OK(WORKING)],
                   "orchestration worker-list": [OK(rows)],
                   "orchestration worker-keep-open": [OK({})],
                   "orchestration worker-stop": [OK({})]})
        self.rt.close("term_1")
        self.assertEqual(self.calls()[-1], ["orchestration", "worker-stop", "--dispatch", "ctx_2", "--json"])
        self.assertEqual(runtime_orca.load_registry(), {})

    def test_worktree_is_an_orca_worktree_on_the_branch(self):
        self.fake({"worktree create": [OK({"worktree": {"id": "r::/w", "path": "/w"}})],
                   "worktree rm": [OK({})]})
        wt = self.rt.worktree_create(self.repo, "oke/fix-a", "fix-a")
        self.assertEqual(wt, {"id": "r::/w", "path": "/w", "place": "/w"})
        argv = self.calls()[0]
        self.assertEqual(argv[argv.index("--branch") + 1], "oke/fix-a")
        self.rt.worktree_remove("r::/w")
        self.assertEqual(self.calls()[1][:4], ["worktree", "rm", "--worktree", "id:r::/w"])

    def test_a_plain_folder_is_added_to_orca_as_a_folder(self):
        plain = self.path("plain")
        os.makedirs(plain)
        self.fake({"repo add": [OK({}), OK({})]})
        self.rt.place_for_repo(self.repo, "r")
        self.rt.place_for_repo(plain, "p")
        kinds = [argv[argv.index("--kind") + 1] for argv in self.calls()]
        self.assertEqual(kinds, ["git", "folder"])

    def test_screen_is_the_transcript_end(self):
        self.register()
        msgs = [{"role": "assistant", "blocks": [{"type": "text", "text": "Allow edit?"}]}]
        self.fake({"orchestration worker-read": [OK({"transcript": {"messages": msgs}})]})
        self.assertEqual(self.rt.screen("fix-a-w1"), "assistant: Allow edit?\n")


class TestOrcaChoice(OrcaBase):
    def test_inside_orca_the_default_is_orca_and_children_are_not_lean(self):
        del os.environ["OKE_RUNTIME"]
        os.environ["ORCA_TERMINAL_HANDLE"] = "term_1"
        self.assertEqual(oke.runtime().name, "orca")
        self.assertEqual(self.rt.whoami(), "term_1")
        args = oke.agent_command("worker", oke.load_config(), self.home, "/p", None)
        self.assertNotIn("--settings", args)
        os.environ["HERDR_ENV"] = "1"
        self.assertEqual(oke.runtime().name, "herdr")

    def test_tick_knows_the_orchestrator_orca_does_not_list(self):
        os.environ["OKE_RUNTIME"] = "orca"
        state = oke.load_state()
        state["orchestrator"] = {"pane": "term_1", "agent": "orchestrator"}
        oke.save_state(state)
        self.fake({})  # no workers started yet: Orca lists nobody
        self.assertEqual(self.cli("tick", "--pane", "term_1", "--board"), 0, self.err)
        self.assertEqual(json.loads(self.out)["role"], "orchestrator")
        self.assertEqual(self.cli("tick", "--pane", "term_2"), 0, self.err)
        self.assertIsNone(json.loads(self.out)["role"])




class TestOrcaCli(unittest.TestCase):
    """Which Orca CLI OKe runs when nothing names one (Orca's own skill order)."""

    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in ("OKE_ORCA", "ORCA_CLI_COMMAND", "ORCA_DEV_REPO_ROOT")}
        self.which, self.realpath, self.platform = runtime_orca.shutil.which, runtime_orca.os.path.realpath, runtime_orca.sys.platform

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        runtime_orca.shutil.which, runtime_orca.os.path.realpath = self.which, self.realpath
        runtime_orca.sys.platform = self.platform

    def linux(self, found):
        runtime_orca.sys.platform = "linux"
        runtime_orca.shutil.which = lambda name: found.get(name)
        runtime_orca.os.path.realpath = lambda p: p

    def test_linux_prefers_orca_ide(self):
        self.linux({"orca-ide": "/opt/orca-ide", "orca": "/usr/bin/orca"})
        self.assertEqual(runtime_orca.orca_argv(), ["/opt/orca-ide"])

    def test_linux_refuses_the_screen_reader(self):
        self.linux({"orca": "/usr/bin/orca"})
        with self.assertRaises(runtime_orca.HerdrError) as e:
            runtime_orca.orca_argv()
        self.assertEqual(e.exception.code, "orca_cli_unknown")

    def test_a_dev_checkout_uses_orca_dev(self):
        self.linux({"orca-ide": "/opt/orca-ide"})
        os.environ["ORCA_DEV_REPO_ROOT"] = "/src/orca"
        self.assertEqual(runtime_orca.orca_argv(), ["orca-dev"])

    def test_env_wins(self):
        self.linux({"orca": "/usr/bin/orca"})
        os.environ["ORCA_CLI_COMMAND"] = "my-orca --x"
        self.assertEqual(runtime_orca.orca_argv(), ["my-orca", "--x"])

if __name__ == "__main__":
    unittest.main()
