"""The wiki: caps, read before write, duplicates, usage, archiving, and the gates."""
import io
import json
import os

from test_oke import SpawnBase  # noqa: F401
import oke
import wiki


class TestWiki(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        self.repo = self.git_repo("r")
        self.assertEqual(self.cli("job", "new", "fix-a", "--repo", self.repo), 0, self.err)
        self.dir = oke.wiki_dir(oke.repo_key(self.repo))

    def page(self, name, text, base="new", *extra):
        src = self.write("src.md", text)
        return self.cli("wiki", "write", name, "--repo", self.repo, "--from", src, "--base", base,
                        "--note", "n", *extra)

    def base(self, name):
        self.assertEqual(self.cli("wiki", "read", name, "--repo", self.repo), 0, self.err)
        return json.loads(self.out.splitlines()[0])["base"]

    def test_a_write_starts_from_the_current_text(self):
        self.assertEqual(self.page("build.md", "- run make test before a commit\n"), 0, self.err)
        self.assertEqual(self.page("build.md", "- other\n"), 1)  # "new", but it exists
        self.assertIn("did not read it", self.err)
        self.assertEqual(self.page("build.md", "- other\n", self.base("build.md")), 0, self.err)
        self.assertIn("curate: build.md: n", io.open(os.path.join(self.dir, "log.md"), encoding="utf-8").read())

    def test_over_the_cap_is_refused(self):
        self.assertEqual(self.page("big.md", "x" * (wiki.PAGE_CHARS + 1)), 1)
        self.assertIn("over its cap", self.err)
        self.assertEqual(self.page("index.md", "x" * (wiki.INDEX_CHARS + 1)), 1)

    def test_a_reworded_line_of_another_page_is_refused(self):
        self.assertEqual(self.page("build.md", "- run `make test` before every commit\n"), 0, self.err)
        self.assertEqual(self.page("tests.md", "- run make test before each commit\n"), 1)
        self.assertIn("build.md", self.err)
        self.assertEqual(self.page("tests.md", "- run make test before each commit\n", "new", "--dup-ok"), 0)

    def test_lessons_wait_for_a_verified_merge(self):
        src = self.write("src.md", "- a fact about the build system\n")
        args = ("wiki", "write", "build.md", "--job", "fix-a", "--from", src, "--base", "new", "--note", "n")
        self.assertEqual(self.cli(*args), 1)
        self.assertIn("not verified", self.err)
        oke.ledger_add("fix-a", "oke", "abc", "merge", "ok")
        oke.ledger_add("fix-a", "oke", "abc", "verify", "pass")
        self.assertEqual(self.cli(*args), 0, self.err)

    def test_the_shared_wiki_takes_no_repository_path(self):
        src = self.write("src.md", "- the tests of %s are slow\n" % self.repo.replace("\\", "/"))
        self.assertEqual(self.cli("wiki", "write", "x.md", "--shared", "--from", src, "--base", "new",
                                  "--note", "n"), 1)
        self.assertIn("beyond one repository", self.err)

    def test_reports_count_use_and_old_pages_are_archived_not_deleted(self):
        self.assertEqual(self.page("build.md", "- run make test before a commit\n"), 0, self.err)
        self.assertEqual(self.page("old.md", "- something nobody needs any more at all\n"), 0, self.err)
        self.assertEqual(self.page("index.md", "- [build](build.md)\n- [old](old.md)\n"), 0, self.err)
        self.write("home/jobs/fix-a/fix-a-w1/report.md",
                   "type: done\n---\n## 요약\nok\n## wiki\n- build.md: wrong: it is make check now\n- x.md: ok\n")
        self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        usage = wiki.load_usage(self.dir)
        self.assertEqual((usage["build.md"]["uses"], usage["build.md"]["wrong"]), (1, 1))
        later = wiki.time.time() + 31 * wiki.DAY
        usage["build.md"]["used"] = later
        wiki.save_usage(self.dir, usage)
        self.assertEqual(wiki.prune(self.dir, False, later), ["old.md"])
        self.assertEqual(wiki.prune(self.dir, True, later), ["old.md"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "old.md")))
        self.assertEqual(len(os.listdir(os.path.join(self.dir, ".archive"))), 1)
        self.assertNotIn("old.md", io.open(os.path.join(self.dir, "index.md"), encoding="utf-8").read())

    def test_a_pinned_page_stays(self):
        self.assertEqual(self.page("keep.md", "- a rule that holds for years and years\n"), 0, self.err)
        self.assertEqual(self.cli("wiki", "pin", "keep.md", "--repo", self.repo), 0, self.err)
        self.assertEqual(wiki.prune(self.dir, True, wiki.time.time() + 99 * wiki.DAY), [])

    def test_the_archive_says_when_a_curation_is_due(self):
        for i in range(oke.CURATE_EVERY):
            wiki.log(self.dir, "job%d" % i, "none")
        self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        self.assertIn("kind: curate", self.out)
        wiki.log(self.dir, "curate", "merged two pages")
        self.assertEqual(self.cli("job", "new", "fix-b", "--repo", self.repo), 0, self.err)
        self.assertEqual(self.cli("job", "archive", "fix-b"), 0, self.err)
        self.assertNotIn("curate", self.out)

    def test_report_entries(self):
        text = "## 요약\n- a.md: ok\n## wiki\n- `b.md`: ok\n* c.md: wrong - stale flag\n- d.md: unused\n## 배운 것\n- e.md: ok\n"
        self.assertEqual(wiki.report_entries(text),
                         [("b.md", "ok", ""), ("c.md", "wrong", "stale flag"), ("d.md", "unused", "")])


if __name__ == "__main__":
    import unittest
    unittest.main()
