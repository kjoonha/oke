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


STARTED = {"dispatchId": "ctx_1", "terminal": "structworker_1", "state": "ready"}
WORKING = {"liveness": "live", "activity": "working", "terminalState": "retained"}


class OrcaBase(SpawnBase):
    def fake(self, scenario):
        SpawnBase.fake(self, scenario)
        os.environ["OKE_ORCA"] = os.environ["OKE_HERDR"]

    def setUp(self):
        SpawnBase.setUp(self)
        self.rt = runtime_orca.OrcaRuntime()

    def register(self, name="fix-a-w1", dispatch="ctx_1", handle="structworker_1"):
        reg = runtime_orca.load_registry()
        reg[name] = {"dispatch": dispatch, "handle": handle, "cwd": self.repo}
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
        self.assertEqual((info["pane_id"], info["agent_session"]), ("structworker_1", {"value": "ctx_1"}))
        self.assertEqual(seen, [1])
        self.assertEqual(runtime_orca.load_registry()["fix-a-w1"]["dispatch"], "ctx_1")

    def test_the_tab_says_who_is_there(self):
        self.fake({"orchestration worker-start": [OK(STARTED)], "terminal rename": [OK({})]})
        self.rt.start("pm-toy-6b0484", self.repo, [], "go", lambda: False)
        self.assertEqual(self.calls()[1], ["terminal", "rename", "--terminal", "structworker_1",
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
        self.register("pm-toy", "ctx_2", "structworker_2")
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(WORKING)],
                   "orchestration worker-show --dispatch ctx_2": [OK(dict(WORKING, liveness="lost"))]})
        self.assertEqual([(a["name"], a["agent_status"]) for a in self.rt.agents()], [("fix-a-w1", "working")])
        with self.assertRaises(oke.HerdrError) as c:
            self.rt.agent("pm-toy")
        self.assertEqual(c.exception.code, "agent_not_found")

    def test_a_follow_up_dispatch_takes_over_the_entry(self):
        self.register("pm-toy", "ctx_1", "structworker_1")
        settled = {"stage": "settled", "liveness": "unverifiable", "runId": "run_1"}
        rows = {"workers": [{"resource": {"terminalHandle": "structworker_9", "ownerDispatchId": "ctx_8"}},
                            {"resource": {"terminalHandle": "structworker_1", "ownerDispatchId": "ctx_2"}}]}
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
        self.rt.close("structworker_1")
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
        self.rt.close("structworker_1")
        self.assertEqual(runtime_orca.load_registry(), {})
        n = len(self.calls())
        self.rt.start("fix-a-w1", self.repo, ["--resume", "ctx_1"], "go", lambda: False)
        self.assertIn(["orchestration", "worker-show", "--dispatch", "ctx_1", "--json"], self.calls()[n:])
        self.assertIn(["orchestration", "run-use", "--id", "run_7", "--json"], self.calls()[n:])
        self.assertEqual(runtime_orca.load_registry()["fix-a-w1"]["dispatch"], "ctx_9")

    def test_a_fenced_codex_revive_binds_too(self):
        self.fake({"orchestration worker-start": [ERR("consumer_fenced"), OK(STARTED)],
                   "orchestration run-use": [OK({})],
                   "orchestration worker-show": [OK(dict(WORKING, runId="run_7"))],
                   "terminal rename": [OK({})]})
        self.rt.start("fix-a-w1", self.repo, ["-c", "features.hooks=false", "resume", "ctx_1"], "go",
                      lambda: False, kind="codex")
        self.assertIn(["orchestration", "worker-show", "--dispatch", "ctx_1", "--json"], self.calls())
        self.assertIn(["orchestration", "run-use", "--id", "run_7", "--json"], self.calls())

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
        self.rt.close("structworker_1")
        self.assertEqual([c[1] for c in self.calls()][-2:], ["worker-stop", "worker-release"])
        self.assertEqual(self.calls()[-1][2:4], ["--dispatch", "ctx_1"])
        self.assertEqual(runtime_orca.load_registry(), {})

    def test_close_of_a_running_worker_only_stops(self):
        self.register()
        self.fake({"orchestration worker-stop": [OK({"state": "stopped", "processAction": "closed_agent_terminal"})],
                   "orchestration worker-show": [OK(WORKING)]})
        self.rt.close("structworker_1")
        self.assertNotIn("worker-release", [c[1] for c in self.calls()])

    def test_close_of_an_unknown_place_says_so(self):
        self.register()
        with self.assertRaises(runtime_orca.HerdrError) as e:
            self.rt.close("fix-a-w1")  # the agent's name, not its pane
        self.assertEqual(e.exception.code, "agent_not_found")
        self.assertIn("fix-a-w1", runtime_orca.load_registry())

    def test_close_stops_and_forgets(self):
        self.register()
        self.fake({"orchestration worker-stop": [ERR("already_settled")]})
        self.rt.close("structworker_1")
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
        _, args = oke.agent_command("claude", "worker", oke.load_config(), self.home, "/p", None)
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
