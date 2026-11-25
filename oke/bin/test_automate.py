"""oke automate: schedules, crontab and Task Scheduler entries, and runs (the OS
scheduler and claude are faked)."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

import automate
import oke
import runtime_bg


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.home)
        os.makedirs(self.work)
        self._env = dict(os.environ)
        os.environ["OKE_HOME"] = self.home
        os.environ["OKE_CLAUDE"] = sys.executable  # any program on disk resolves
        self.crontab = []
        self.cmds = []
        self.claude = []
        self.rows = []
        self.win = False
        self.answers = {}  # argv prefix -> (exit, stdout, stderr)
        self.patch(automate, "run_cmd", self.fake_cmd)
        self.patch(automate, "windows", lambda: self.win)
        self.patch(runtime_bg, "run_claude", self.fake_claude)
        self.patch(runtime_bg.BgRuntime, "rows", lambda rt: self.rows)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def patch(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, old)

    def fake_cmd(self, argv, stdin=None):
        self.cmds.append(argv)
        for prefix, answer in self.answers.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return answer
        if argv[:2] == ["crontab", "-l"]:
            if not self.crontab:
                return 1, "", "no crontab for me\n"
            return 0, "\n".join(self.crontab) + "\n", ""
        if argv[:2] == ["crontab", "-"]:
            self.crontab = stdin.splitlines()
            return 0, "", ""
        if argv[0] == "schtasks" and argv[1] == "/Query":
            return 0, '"\\OKe\\nightly","n/a","Ready"\n"\\Other\\x","n/a","Ready"\n', ""
        return 0, "SUCCESS", ""

    def fake_claude(self, args, cwd=None):
        self.claude.append((args, cwd))
        return ""

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = oke.main(["automate"] + list(argv))
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def add(self, name="nightly", schedule="daily 03:00", *extra):
        return self.cli("add", name, "--schedule", schedule, "--cwd", self.work,
                        "--prompt", "Run the tests and fix what broke.", *extra)


class TestSchedule(unittest.TestCase):
    def setUp(self):
        old = automate.windows
        automate.windows = lambda: False
        self.addCleanup(setattr, automate, "windows", old)

    def cron(self, text):
        return automate.cron_fields(automate.parse_schedule(text))

    def test_forms_become_cron_fields(self):
        self.assertEqual(self.cron("every 15m"), "*/15 * * * *")
        self.assertEqual(self.cron("every 6h"), "0 */6 * * *")
        self.assertEqual(self.cron("daily 9:05"), "5 9 * * *")
        self.assertEqual(self.cron("weekdays 18:30"), "30 18 * * 1-5")
        self.assertEqual(self.cron("weekly thu,Mon 08:00"), "0 8 * * 1,4")
        self.assertEqual(self.cron("cron 0 3 1 * *"), "0 3 1 * *")

    def test_forms_become_task_scheduler_arguments(self):
        sc = lambda t: automate.schtasks_args(automate.parse_schedule(t))
        self.assertEqual(sc("every 30m"), ["/SC", "MINUTE", "/MO", "30", "/ST", "00:00"])
        self.assertEqual(sc("every 2h"), ["/SC", "HOURLY", "/MO", "2", "/ST", "00:00"])
        self.assertEqual(sc("daily 03:00"), ["/SC", "DAILY", "/ST", "03:00"])
        self.assertEqual(sc("weekdays 09:00"), ["/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "09:00"])
        self.assertEqual(sc("weekly sun 10:00"), ["/SC", "WEEKLY", "/D", "SUN", "/ST", "10:00"])

    def test_bad_schedules_say_what_works(self):
        for bad in ("", "every 7m", "every 5h", "daily 24:00", "weekly fun 09:00", "hourly", "cron 1 2 3"):
            with self.assertRaises(automate.AutomateError):
                automate.parse_schedule(bad)

    def test_cron_expressions_need_crontab(self):
        automate.windows = lambda: True
        with self.assertRaises(automate.AutomateError):
            automate.parse_schedule("cron 0 3 * * *")


class TestCrontab(Base):
    def test_add_writes_one_marked_line_and_keeps_the_rest(self):
        self.crontab = ["MAILTO=me", "0 1 * * * backup.sh"]
        self.assertEqual(self.add(), 0)
        self.assertEqual(self.crontab[:2], ["MAILTO=me", "0 1 * * * backup.sh"])
        line = self.crontab[2]
        self.assertTrue(line.startswith("0 3 * * * "), line)
        self.assertTrue(line.endswith("# oke-automate:nightly"), line)
        self.assertIn("automate run", line)
        folder = os.path.join(self.home, "automations", "nightly")
        self.assertIn(os.path.join(folder, "scheduler.log"), line)
        info = json.load(io.open(os.path.join(folder, "automation.json"), encoding="utf-8"))
        self.assertEqual(info["cwd"], self.work)
        self.assertEqual(info["claude"], [sys.executable])  # absolute: cron's PATH is short
        self.assertEqual(io.open(os.path.join(folder, "prompt.md"), encoding="utf-8").read(),
                         "Run the tests and fix what broke.\n")

    def test_add_again_replaces_the_line(self):
        self.add()
        self.add("nightly", "every 30m")
        self.assertEqual(len(self.crontab), 1)
        self.assertTrue(self.crontab[0].startswith("*/30 * * * * "))

    def test_a_percent_in_a_path_is_escaped(self):
        self.work = os.path.join(self.tmp, "100%")
        os.makedirs(self.work)
        os.environ["OKE_HOME"] = os.path.join(self.tmp, "h%me")
        self.add()
        self.assertNotIn("h%me", self.crontab[0])
        self.assertIn("h\\%me", self.crontab[0])

    def test_remove_drops_only_its_line_and_folder(self):
        self.crontab = ["0 1 * * * backup.sh"]
        self.add()
        self.add("weekly-report", "weekly fri 17:00")
        self.assertEqual(self.cli("remove", "nightly"), 0)
        self.assertEqual(len(self.crontab), 2)
        self.assertFalse(any(l.endswith(":nightly") for l in self.crontab))
        self.assertFalse(os.path.exists(os.path.join(self.home, "automations", "nightly")))
        self.assertEqual(self.cli("remove", "nightly"), 1)

    def test_list_shows_registration_and_last_run(self):
        self.add()
        self.add("other", "every 1h")
        self.crontab = [l for l in self.crontab if not l.endswith(":other")]  # removed by hand
        with io.open(os.path.join(self.home, "automations", "nightly", "run.log"), "w", encoding="utf-8") as f:
            f.write("x started\ny started auto-nightly-1125-0300\n")
        out = automate.listing(self.home)
        by = dict((a["name"], a) for a in out)
        self.assertTrue(by["nightly"]["registered"])
        self.assertFalse(by["other"]["registered"])
        self.assertEqual(by["nightly"]["last_run"], "y started auto-nightly-1125-0300")
        self.assertEqual(by["nightly"]["schedule"], "daily 03:00")

    def test_bad_input_is_refused_before_anything_is_written(self):
        self.assertEqual(self.add("Bad Name"), 1)
        self.assertEqual(self.add("ok", "every 7m"), 1)
        self.assertEqual(self.cli("add", "ok", "--schedule", "daily 03:00", "--cwd", os.path.join(self.tmp, "none"),
                                  "--prompt", "x"), 1)
        self.assertEqual(self.cli("add", "ok", "--schedule", "daily 03:00", "--cwd", self.work,
                                  "--prompt", "--dangerously-skip-permissions"), 1)
        self.assertEqual(self.crontab, [])
        self.assertFalse(os.path.exists(os.path.join(self.home, "automations")))

    def test_a_line_edited_by_hand_still_counts_and_a_commented_one_does_not(self):
        self.add()
        self.crontab = [self.crontab[0] + "  ", "# " + self.crontab[0].strip()]
        self.assertEqual(automate.registered_names(), {"nightly"})
        self.add("nightly", "every 1h")
        self.assertEqual(len([l for l in self.crontab if not l.startswith("#")]), 1)
        self.crontab = ["# " + self.crontab[-1]]
        self.assertEqual(automate.registered_names(), set())

    def test_remove_refuses_any_name_that_is_not_an_automation(self):
        self.add()
        marker = os.path.join(self.home, "keep.txt")
        io.open(marker, "w").close()
        for bad in ("..", ".", self.work, "", "Nightly"):
            self.assertEqual(self.cli("remove", bad), 1, bad)
        self.assertTrue(os.path.exists(marker))
        self.assertTrue(os.path.isdir(self.work))
        self.assertTrue(os.path.isdir(os.path.join(self.home, "automations", "nightly")))

    def test_a_failed_re_add_keeps_the_old_automation(self):
        self.add()
        folder = os.path.join(self.home, "automations", "nightly")
        before = io.open(os.path.join(folder, "automation.json"), encoding="utf-8").read()
        self.answers[("crontab", "-")] = (1, "", "bad minute")
        self.assertEqual(self.cli("add", "nightly", "--schedule", "cron 99 * * * *", "--cwd", self.work,
                                  "--prompt", "Something else", "--permission-mode", "bypassPermissions"), 1)
        self.assertEqual(io.open(os.path.join(folder, "automation.json"), encoding="utf-8").read(), before)
        self.assertIn("Run the tests", io.open(os.path.join(folder, "prompt.md"), encoding="utf-8").read())

    def test_a_failed_first_add_leaves_nothing(self):
        self.answers[("crontab", "-")] = (1, "", "no cron here")
        self.assertEqual(self.add(), 1)
        self.assertFalse(os.path.exists(os.path.join(self.home, "automations", "nightly")))

    def test_the_users_path_and_locale_go_with_the_run(self):
        os.environ["PATH"] = os.pathsep.join([os.path.dirname(sys.executable), "/opt/node/bin"])
        os.environ["LANG"] = "ko_KR.UTF-8"
        self.add()
        os.environ["PATH"] = "/usr/bin:/bin"  # what cron gives
        os.environ.pop("LANG")
        self.assertEqual(self.cli("run", "nightly"), 0)
        self.assertIn("/opt/node/bin", os.environ["PATH"])
        self.assertEqual(os.environ["LANG"], "ko_KR.UTF-8")
        self.cli("list")
        self.assertNotIn("env", json.loads(self.out)[0])

    def test_a_missing_claude_is_refused(self):
        os.environ["OKE_CLAUDE"] = "no-such-claude-here"
        self.assertEqual(self.add(), 1)
        self.assertEqual(self.crontab, [])


class TestTaskScheduler(Base):
    def setUp(self):
        Base.setUp(self)
        self.win = True

    def test_add_creates_a_task_that_starts_run_cmd(self):
        self.assertEqual(self.add("nightly", "weekdays 09:00"), 0)
        create = [c for c in self.cmds if c[:2] == ["schtasks", "/Create"]][-1]
        self.assertEqual(create[:5], ["schtasks", "/Create", "/F", "/TN", "\\OKe\\nightly"])
        script = os.path.join(self.home, "automations", "nightly", "run.cmd")
        self.assertEqual(create[6], '"%s"' % script)
        self.assertEqual(create[7:], ["/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "09:00"])
        body = io.open(script, encoding="utf-8").read()
        self.assertIn("automate run", body)
        self.assertIn(os.path.join(self.home, "automations", "nightly", "scheduler.log"), body)

    def test_remove_deletes_the_task(self):
        self.add()
        self.assertEqual(self.cli("remove", "nightly"), 0)
        self.assertEqual(self.cmds[-1], ["schtasks", "/Delete", "/F", "/TN", "\\OKe\\nightly"])

    def test_a_task_that_cannot_be_deleted_keeps_its_folder(self):
        self.add()
        self.answers[("schtasks", "/Delete")] = (1, "", "ERROR: Access is denied.")
        self.assertEqual(self.cli("remove", "nightly"), 1)
        self.assertIn("Access is denied", self.err)
        self.assertTrue(os.path.isdir(os.path.join(self.home, "automations", "nightly")))

    def test_no_task_and_no_folder_is_no_automation(self):
        self.answers[("schtasks", "/Query", "/TN")] = (1, "", "ERROR: not found")
        self.assertEqual(self.cli("remove", "nightly"), 1)
        self.assertFalse(any(c[:2] == ["schtasks", "/Delete"] for c in self.cmds))

    def test_the_task_runs_on_battery_too(self):
        self.add()
        ps = [c for c in self.cmds if c[0] == "powershell"][-1]
        self.assertIn("-AllowStartIfOnBatteries", ps[-1])
        self.assertIn("-TaskName 'nightly'", ps[-1])
        self.answers[("powershell",)] = (1, "", "no")
        self.add()
        self.assertIn("only on AC power", self.out)

    def test_a_percent_in_a_path_is_doubled_in_run_cmd(self):
        os.environ["OKE_HOME"] = os.path.join(self.tmp, "h%me")
        self.add()
        body = io.open(os.path.join(self.tmp, "h%me", "automations", "nightly", "run.cmd"), encoding="utf-8").read()
        self.assertIn("h%%me", body)
        self.assertIn("--scheduled", body)

    def test_list_reads_the_oke_folder_of_task_scheduler(self):
        self.add()
        self.add("other")
        by = dict((a["name"], a) for a in automate.listing(self.home))
        self.assertTrue(by["nightly"]["registered"])
        self.assertFalse(by["other"]["registered"])


class TestRun(Base):
    def folder(self):
        return os.path.join(self.home, "automations", "nightly")

    def test_a_run_starts_a_named_bg_session_in_the_folder(self):
        self.add("nightly", "daily 03:00", "--permission-mode", "acceptEdits", "--model", "sonnet")
        self.assertEqual(self.cli("run", self.folder()), 0)
        args, cwd = self.claude[-1]
        self.assertEqual(cwd, self.work)
        self.assertEqual(args[0], "Run the tests and fix what broke.")
        self.assertEqual(args[1:3], ["--bg", "--name"])
        self.assertTrue(args[3].startswith("auto-nightly-"))
        self.assertEqual(args[4:8], ["--permission-mode", "acceptEdits", "--model", "sonnet"])
        settings = json.loads(args[args.index("--settings") + 1])
        self.assertEqual(settings["worktree"], {"bgIsolation": "none"})  # stays in the chosen folder
        self.assertEqual(os.environ["OKE_CLAUDE"], json.dumps([sys.executable]))

    def test_without_a_mode_the_users_own_setting_holds(self):
        self.add()
        self.assertEqual(self.cli("run", "nightly"), 0)  # a name works too
        self.assertNotIn("--permission-mode", self.claude[-1][0])

    def test_a_run_waits_for_the_last_one(self):
        self.add()
        self.rows = [{"id": "b1", "name": "auto-nightly-1124-030000", "state": "blocked", "status": "busy"},
                     {"id": "b0", "name": "auto-nightly-1123-030000", "state": "done"}]
        self.assertEqual(self.cli("run", "nightly"), 0)
        self.assertEqual(self.claude, [])
        self.rows = [{"id": "b0", "name": "auto-nightly-1123-030000", "state": "done"},
                     {"id": "c1", "name": "auto-nightly-x-1124-030000", "state": "working"}]
        self.assertEqual(self.cli("run", "nightly"), 0)
        self.assertEqual(len(self.claude), 1)

    def test_a_failed_start_is_one_log_line_and_exit_1(self):
        self.add()

        def broken(args, cwd=None):
            raise runtime_bg.HerdrError("claude_failed", "folder not trusted")
        runtime_bg.run_claude = broken
        self.assertEqual(automate.run(self.folder()).split(" ", 2)[2], "failed: folder not trusted")
        self.assertEqual(self.cli("run", self.folder()), 1)

    def test_every_run_leaves_one_log_line(self):
        self.add()
        self.assertEqual(self.cli("run", "nightly"), 0)
        self.assertIn("started auto-nightly-", self.out)
        self.assertEqual(self.cli("run", "nightly", "--scheduled"), 0)
        self.assertEqual(self.out, "")  # the scheduler's redirect would write it twice
        lines = io.open(os.path.join(self.folder(), "run.log"), encoding="utf-8").read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(automate.listing(self.home)[0]["last_run"], lines[-1])

    def test_a_missing_folder_is_a_failure_line(self):
        self.assertIn(" failed: ", automate.run(os.path.join(self.tmp, "gone")))


if __name__ == "__main__":
    unittest.main()
