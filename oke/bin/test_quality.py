# -*- coding: utf-8 -*-
"""Quality protocol: failure signatures, baseline, oke check."""
import os
import tempfile
import unittest

import oke

PYTEST_A = ("tests/t.py::test_div FAILED [ 50%]\nFAILED tests/t.py::test_div - ZeroDivisionError\n"
            "===== 1 failed, 4 passed in 3.21s (0:00:03) =====\n....F\n")
PYTEST_B = PYTEST_A.replace("3.21s (0:00:03)", "9.80s (0:00:09)")


def entry(exit, signatures=(), unrunnable=False):
    return {"exit": exit, "unrunnable": unrunnable, "signatures": list(signatures)}


class TestSignatures(unittest.TestCase):
    def test_volatile_values_give_equal_signatures(self):
        self.assertEqual(oke.signature_lines(PYTEST_A), oke.signature_lines(PYTEST_B))
        self.assertIn("FAILED tests/t.py::test_div - ZeroDivisionError", oke.signature_lines(PYTEST_A))

    def test_noise_is_dropped_and_plain_lines_kept(self):
        out = (u"\x1b[31mresult mismatch at step 4\x1b[0m\n\u2713 adds\nok 1 - x\n1..3\n# pass 2\n# fail 0\n"
               u"INFO starting\nnpm warn deprecated\n.....\n12 passed in 0.5s\n  at foo (a.js:1:1)\n"
               u"12| const x = 1\nnot ok 2 - y\n")
        self.assertEqual(oke.signature_lines(out), ["not ok 2 - y", "result mismatch at step 4"])

    def test_failure_words_keep_log_and_summary_lines(self):
        out = "INFO validation failed\nTests 1 failed | 3 passed\nTests 4 passed\n"
        self.assertEqual(oke.signature_lines(out), ["INFO validation failed", "Tests 1 failed | 3 passed"])

    def test_temp_paths_hex_and_node_pid_normalized(self):
        a = "Error in /tmp/abc123/x deadbeef01 (node:4242) [DEP0040] DeprecationWarning: p\n"
        b = "Error in /tmp/zz9/x cafebabe99 (node:7) [DEP0040] DeprecationWarning: p\n"
        self.assertEqual(oke.signature_lines(a), oke.signature_lines(b))
        w1 = "Error C:\\Users\\me\\AppData\\Local\\Temp\\tmpab12\\x.py\n"
        w2 = "Error C:\\Users\\me\\AppData\\Local\\Temp\\tmpzz99\\x.py\n"
        self.assertEqual(oke.signature_lines(w1), oke.signature_lines(w2))

    def test_cap_and_long_lines(self):
        self.assertEqual(len(oke.signature_lines("\n".join("E%d boom" % i for i in range(300)))), 200)
        self.assertEqual(oke.signature_lines("x" * 501), [])

    def test_diff_green_to_red_is_new_even_without_lines(self):
        new, _ = oke.diff_baseline(entry(0), entry(1))
        self.assertEqual(new, ["<exited with code 1>"])

    def test_diff_red_reports_only_new_lines_and_counts_resolved(self):
        new, resolved = oke.diff_baseline(entry(1, ["old", "gone"]), entry(1, ["old", "fresh"]))
        self.assertEqual((new, resolved), (["fresh"], ["gone"]))

    def test_diff_unrunnable_and_no_baseline(self):
        new, _ = oke.diff_baseline(entry(1), entry(127, unrunnable=True))
        self.assertEqual(new, ["<command became unrunnable>"])
        self.assertEqual(oke.diff_baseline(None, entry(2, ["x"]))[0], ["<exited with code 2>", "x"])

    def test_run_signatures_ignores_output_on_success(self):
        d = tempfile.mkdtemp()
        old = os.environ.get("OKE_HOME")
        os.environ["OKE_HOME"] = d
        try:
            log = os.path.join(d, "x.log")
            self.assertEqual(oke.run_signatures("echo TypeError: fine", d, log, "t"), entry(0))
            self.assertEqual(oke.run_signatures("echo boom; exit 3", d, log, "t"), entry(3, ["boom"]))
        finally:
            if old is None:
                os.environ.pop("OKE_HOME")
            else:
                os.environ["OKE_HOME"] = old


if __name__ == "__main__":
    unittest.main()
