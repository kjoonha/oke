# -*- coding: utf-8 -*-
"""Round 3: restore-user keeps what it overwrites, the orchestrator gate after a restart, junctions
without a shell, head_moved's question, the model allowlist, the in-place queue, check rows with a
log path holding spaces, the index after an undone commit, the marks cache."""
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest

import oke
from test_deliver import DeliverBase
from test_oke import AG, J, SpawnBase, help_ok
from test_pms import PmBase


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = oke.main(argv)
    return code, out.getvalue(), err.getvalue()


def dir_link(target, link):
    """A folder link: a junction on Windows, a symlink elsewhere."""
    if os.name == "nt":
        return oke.make_junction(target, link)
    os.symlink(target, link, target_is_directory=True)
    return True


class TestRestoreUser(DeliverBase):
    def setUp(self):
        DeliverBase.setUp(self)
        self.commit_main("gone.txt", "tracked\n")
        if os.path.exists(oke.snapshot_path("fix-a")):
            os.remove(oke.snapshot_path("fix-a"))
        self.write("toy/notes.txt", "the user's notes\n")
        os.remove(os.path.join(self.repo, "gone.txt"))  # the user deleted it before the job
        self.assertTrue(oke.snapshot_dirty("fix-a", oke.load_state()["jobs"]["fix-a"]))

    def restore(self, name):
        return self.cli("job", "restore-user", "fix-a", name)

    def read(self, path):
        with io.open(path, encoding="utf-8") as f:
            return f.read()

    def test_the_current_text_is_kept_before_the_restore(self):
        self.write("toy/notes.txt", "a worker wrote over it\n")
        self.assertEqual(self.restore("notes.txt"), 0, self.err)
        out = json.loads(self.out)
        self.assertTrue(out["same"])
        self.assertEqual(self.read(os.path.join(self.repo, "notes.txt")), "the user's notes\n")
        kept = out["kept"]
        self.assertTrue(oke.normalize_path(kept).startswith(
            oke.normalize_path(os.path.join(self.home, "jobs", "fix-a", "restored")) + "/"))
        self.assertTrue(os.path.basename(kept).startswith("notes.txt."))
        self.assertEqual(self.read(kept), "a worker wrote over it\n")

    def test_a_file_the_user_had_deleted_is_kept_then_removed(self):
        self.write("toy/gone.txt", "the worker made it again\n")
        self.assertEqual(self.restore("gone.txt"), 0, self.err)
        out = json.loads(self.out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "gone.txt")))
        self.assertEqual(self.read(out["kept"]), "the worker made it again\n")

    def test_a_link_is_replaced_never_written_through(self):
        outside = self.write("outside.txt", "not the user's file\n")
        target = os.path.join(self.repo, "notes.txt")
        os.remove(target)
        try:
            os.symlink(outside, target)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        self.assertEqual(self.restore("notes.txt"), 0, self.err)
        self.assertFalse(os.path.islink(target))
        self.assertEqual(self.read(target), "the user's notes\n")
        self.assertEqual(self.read(outside), "not the user's file\n")
        self.assertIn("link to", self.read(json.loads(self.out)["kept"]))

    def test_a_path_outside_the_checkout_is_refused(self):
        snap = oke.read_json(oke.snapshot_path("fix-a"), None)
        snap["files"]["../evil.txt"] = "x"
        snap["copied"].append("../evil.txt")
        oke.write_json_atomic(oke.snapshot_path("fix-a"), snap)
        self.assertEqual(self.restore("../evil.txt"), 1)
        self.assertIn("outside", self.err)

    def test_a_folder_link_that_leads_out_is_refused(self):
        outside = self.path("elsewhere")
        os.makedirs(outside)
        try:
            if not dir_link(outside, os.path.join(self.repo, "sub")):
                self.skipTest("no folder links here")
        except (OSError, NotImplementedError):
            self.skipTest("no folder links here")
        snap = oke.read_json(oke.snapshot_path("fix-a"), None)
        snap["files"]["sub/a.txt"] = "x"
        snap["copied"].append("sub/a.txt")
        oke.write_json_atomic(oke.snapshot_path("fix-a"), snap)
        self.assertEqual(self.restore("sub/a.txt"), 1)
        self.assertIn("outside", self.err)
        self.assertEqual(os.listdir(outside), [])


class TestOrchestratorAfterRestart(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        os.environ["HERDR_ENV"] = "1"
        self.job("q", "intake")
        st = oke.load_state()
        st["orchestrator"] = {"pane": "w7:p1", "session": "s-old"}
        oke.save_state(st)
        oke.write_json_atomic(oke.children_path(self.pm), {
            "q-w1": {"agent": "q-w1", "job": "q", "pane": "w2:p1", "session": "s-w", "role": "worker"}})

    def agents(self, *agents):
        s = help_ok()
        s["agent list"] = [J({"agents": list(agents)})]
        self.fake(s)

    def test_a_new_session_in_the_recorded_pane_is_the_orchestrator(self):
        os.environ.update(HERDR_PANE_ID="w7:p1", CLAUDE_CODE_SESSION_ID="s-new")
        self.agents(AG(name=None, pane="w7:p1", session="s-new"))
        code, _, err = run_main(["job", "auto", "q"])
        self.assertEqual(code, 0, err)
        st = oke.load_state()
        self.assertTrue(st["jobs"]["q"]["auto"])
        self.assertEqual(st["orchestrator"]["session"], "s-new")
        # herdr gone now: the recorded session alone still says who it is
        self.fake({})
        self.assertEqual(run_main(["job", "auto", "q", "--off"])[0], 0)

    def test_a_worker_pane_is_still_refused(self):
        os.environ.update(HERDR_PANE_ID="w2:p1", CLAUDE_CODE_SESSION_ID="s-w")
        self.agents(AG(name="q-w1", pane="w2:p1", session="s-w"), AG(name=None, pane="w7:p1", session="s-old"))
        code, _, err = run_main(["job", "auto", "q"])
        self.assertEqual(code, 1)
        self.assertIn("only the orchestrator", err)
        self.assertEqual(oke.load_state()["orchestrator"]["session"], "s-old")

    def test_a_worker_in_a_pane_id_that_came_back_is_refused(self):
        os.environ.update(HERDR_PANE_ID="w7:p1", CLAUDE_CODE_SESSION_ID="s-w")
        self.agents(AG(name="q-w1", pane="w7:p1", session="s-w"))
        self.assertEqual(run_main(["job", "auto", "q"])[0], 1)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s-other"
        self.agents(AG(name="some-agent", pane="w7:p1", session="s-other"))
        self.assertEqual(run_main(["job", "auto", "q"])[0], 1)
        self.assertFalse(oke.load_state()["jobs"]["q"].get("auto"))

    def test_the_claim_records_the_callers_session(self):
        os.environ.update(HERDR_PANE_ID="w5:p1", CLAUDE_CODE_SESSION_ID="s-claim")
        st = oke.load_state()
        st["orchestrator"] = {}
        oke.save_state(st)
        self.agents()
        code, _, err = run_main(["status", "--claim"])
        self.assertEqual(code, 0, err)
        orch = oke.load_state()["orchestrator"]
        self.assertEqual((orch["pane"], orch["session"]), ("w5:p1", "s-claim"))


@unittest.skipUnless(os.name == "nt", "junctions are Windows only")
class TestJunction(SpawnBase):
    def check(self):
        target = self.path("deps & more %PATH% (x)")
        os.makedirs(target)
        self.write("deps & more %PATH% (x)/f.txt", "x\n")
        link = self.path("link & y")
        self.assertTrue(oke.make_junction(target, link))
        self.assertTrue(os.path.isfile(os.path.join(link, "f.txt")))

    def test_a_junction_to_a_path_with_shell_characters(self):
        self.check()

    def test_the_mklink_fallback_quotes_the_paths(self):
        import _winapi
        saved = _winapi.CreateJunction
        del _winapi.CreateJunction
        try:
            self.check()
        finally:
            _winapi.CreateJunction = saved


class TestHeadMovedQuestion(DeliverBase):
    def test_the_next_text_names_both_answers(self):
        info = oke.load_state()["jobs"]["fix-a"]
        info.pop("worktree", None)
        self.assertTrue(oke.snapshot_dirty("fix-a", info))
        self.commit_main("other.txt", "someone\n")
        moved = oke.head_moved("fix-a", info)
        self.assertEqual(moved["result"], "head_moved")
        self.assertIn("accept-head fix-a --by-user", moved["next"])
        self.assertIn("OKe never resets commits", moved["next"])


class TestAllowedModels(SpawnBase):
    def args(self, header, allowed):
        config = oke.load_config()
        config.update(lean_launch=False, allowed_models=allowed)
        config["models"]["pm"] = "free-coder"
        argv = oke.launch_args("pm", config, self.home, self.home, header=header)
        return argv[argv.index("--model") + 1]

    def test_a_header_model_outside_the_list_falls_back(self):
        self.assertEqual(self.args({"model": "claude-opus-4"}, ["free-coder", "free-fast"]), "free-coder")
        self.assertEqual(self.args({"model": "free-fast"}, ["free-coder", "free-fast"]), "free-fast")
        self.assertEqual(self.args({"model": "sonnet"}, []), "sonnet")  # empty: any
        self.assertEqual(oke.load_config()["allowed_models"], [])


class TestQueueAfterDelivery(PmBase):
    def setUp(self):
        PmBase.setUp(self)
        self.pmc("add", "a")
        for name in ("one", "two"):
            self.assertEqual(self.cli("job", "new", name, "--pm", "a", "--path", self.repo), 0, self.err)
            self.write("home/jobs/%s/plan.md" % name, "job: %s\n---\n## Workspace\nnone\n" % name)

    def delivered(self, name):
        oke.ledger_add(name, "oke", "abc1234", "deliver", "ok", "main pre=abc1234")

    def test_two_jobs_back_from_a_delivery_never_wait_for_each_other(self):
        self.advance("one", "working")
        self.delivered("one")
        self.advance("two", "working")  # one is delivered: it is out of the way
        self.delivered("two")
        st = oke.load_state()
        for name in ("one", "two"):  # both back to planning after a change request, both approved again
            st["jobs"][name].update(phase="awaiting_approval", phase_at=time.time() + 10)
        oke.save_state(st)
        self.assertEqual(self.cli("job", "phase", "one", "working"), 3, self.err)
        self.assertEqual(json.loads(self.out)["waits_for"], "two")
        self.assertEqual(self.cli("job", "phase", "two", "working"), 0, self.err)  # one waits itself
        st = oke.load_state()
        self.assertEqual(st["jobs"]["two"]["phase"], "working")
        self.assertEqual(oke.waits_for(st, "one"), "two")
        self.assertEqual(self.cli("status"), 0, self.err)
        self.assertEqual(json.loads(self.out)["jobs"]["one"]["waiting_for"], "two")

    def test_the_name_waited_for_is_read_again(self):
        self.advance("one", "working")
        self.advance("two", "awaiting_approval")
        self.assertEqual(self.cli("job", "phase", "two", "working"), 3)
        st = oke.load_state()
        st["jobs"]["two"]["queued"]["for"] = "someone-old"
        oke.save_state(st)
        self.assertEqual(oke.waits_for(oke.load_state(), "two"), "one")
        info = oke.load_state()["jobs"]["two"]
        self.assertEqual(oke.job_step("two", info, None, None, oke.waits_for(oke.load_state(), "two"))[0],
                         "waiting for one to finish")


class TestCheckRowLog(DeliverBase):
    def test_a_log_path_with_spaces_is_read_whole(self):
        self.assertEqual(oke.check_detail("r1 new=1 pre_existing=0 base_unrunnable log=C:/a b/c d.log"),
                         (["r1", "new=1", "pre_existing=0", "base_unrunnable"], "C:/a b/c d.log"))
        self.assertEqual(oke.check_detail("r1 carried from x"), (["r1", "carried", "from", "x"], None))
        rev = oke.criteria_rev("fix-a")
        tip = self.tip()
        oke.ledger_add("fix-a", "oke", tip, "check", "fail",
                       "%s new=1 pre_existing=0 %s log=C:/my logs/run x=1.log" % (rev, oke.BASE_UNRUNNABLE))
        self.assertEqual(self.cli("job", "accept-check", "fix-a", tip, "--by-user"), 0, self.err)
        row = self.ledger()["rows"][-1]
        self.assertTrue(row["detail"].endswith("log=C:/my logs/run x=1.log"), row["detail"])


class TestUndoKeepsTheIndex(SpawnBase):
    def test_an_undone_commit_gives_back_what_the_user_staged(self):
        self.job("q", "intake")
        self.write("toy/x.txt", "base\n")
        self.git(self.repo, "add", "x.txt")
        self.git(self.repo, "commit", "-q", "-m", "x")
        pre = subprocess.check_output(["git", "-C", self.repo, "rev-parse", "HEAD"]).decode().strip()
        self.write("toy/x.txt", "staged by the user\n")
        self.git(self.repo, "add", "x.txt")
        self.write("toy/x.txt", "the job's edit on top\n")
        self.write("toy/n.txt", "new\n")
        made, why = oke.commit_job("q", self.repo, ["x.txt", "n.txt"], "0" * 40)  # never the reviewed tree
        self.assertIsNone(made)
        self.assertIn("moved during the commit", why)
        git = lambda *a: subprocess.check_output(["git", "-C", self.repo] + list(a)).decode()
        self.assertEqual(git("rev-parse", "HEAD").strip(), pre)
        self.assertEqual(git("show", ":x.txt"), "staged by the user\n")
        self.assertEqual(git("ls-files", "--", "n.txt"), "")
        with io.open(os.path.join(self.repo, "x.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "the job's edit on top\n")


class TestMarks(SpawnBase):
    def test_a_file_written_right_before_its_hash_is_hashed_again(self):
        os.makedirs(os.path.join(self.home, "jobs", "m"))
        p = self.write("toy/f.txt", "aaaa\n")
        marks = oke.Marks("m")
        first = marks.mark(self.repo, "f.txt")
        st = os.stat(p)
        self.write("toy/f.txt", "bbbb\n")  # same size, same mtime: only a new hash tells
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertNotEqual(marks.mark(self.repo, "f.txt"), first)

    def test_an_old_file_is_taken_from_the_cache(self):
        os.makedirs(os.path.join(self.home, "jobs", "m"))
        p = self.write("toy/f.txt", "aaaa\n")
        old = time.time() - 100
        os.utime(p, (old, old))
        marks = oke.Marks("m")
        first = marks.mark(self.repo, "f.txt")
        marks.save()
        again = oke.Marks("m")
        self.assertEqual(again.mark(self.repo, "f.txt"), first)
        self.assertFalse(again.changed)

    @unittest.skipIf(os.name == "nt", "st_ctime is the creation time on Windows")
    def test_a_changed_inode_time_is_hashed_again(self):
        os.makedirs(os.path.join(self.home, "jobs", "m"))
        p = self.write("toy/f.txt", "aaaa\n")
        old = time.time() - 100
        os.utime(p, (old, old))
        marks = oke.Marks("m")
        first = marks.mark(self.repo, "f.txt")
        time.sleep(0.05)
        with io.open(p, "w", encoding="utf-8") as f:
            f.write(u"bbbb\n")
        os.utime(p, (old, old))
        self.assertNotEqual(marks.mark(self.repo, "f.txt"), first)


class TestSnapshotRename(SpawnBase):
    def test_a_failed_rename_is_a_clean_error(self):
        self.job("q", "intake")
        info = oke.load_state()["jobs"]["q"]
        self.write("toy/u.txt", "user\n")
        real = os.rename

        def refuse(a, b):
            raise OSError(13, "held by another process")
        os.rename = refuse
        try:
            with self.assertRaises(oke.StateError) as c:
                oke.snapshot_dirty("q", info)
        finally:
            os.rename = real
        self.assertIn("could not keep the copies", str(c.exception))
        self.assertFalse(os.path.exists(oke.snapshot_path("q")))
        leftovers = [n for n in os.listdir(os.path.join(self.home, "jobs", "q")) if ".new-" in n]
        self.assertEqual(leftovers, [])


class TestRunTimed(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "process groups are POSIX")
    def test_a_command_runs_in_its_own_session(self):
        p = oke.run_timed([sys.executable, "-c", "import os; print(os.getsid(0))"], stdout=subprocess.PIPE)
        self.assertNotEqual(int(p.stdout.decode().strip()), os.getsid(0))


if __name__ == "__main__":
    unittest.main()
