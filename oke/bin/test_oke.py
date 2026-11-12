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


if __name__ == "__main__":
    unittest.main()
