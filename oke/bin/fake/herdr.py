#!/usr/bin/env python3
"""Fake herdr for tests.

Reads $FAKE_HERDR_DIR/scenario.json: {"<argv prefix joined by spaces>": [response, ...]}.
The longest matching prefix wins. Each call uses the next response; the last one repeats.
A response is {"stdout": "...", "stderr": "...", "exit": 0}. Every call is appended to calls.log.
"""
import io
import json
import os
import sys


def main():
    d = os.environ["FAKE_HERDR_DIR"]
    argv = sys.argv[1:]
    with io.open(os.path.join(d, "calls.log"), "a", encoding="utf-8") as f:
        f.write(json.dumps(argv, ensure_ascii=False) + u"\n")
    with io.open(os.path.join(d, "scenario.json"), encoding="utf-8") as f:
        scenario = json.load(f)
    joined = " ".join(argv)
    best = None
    for key in scenario:
        if (joined == key or joined.startswith(key + " ")) and (best is None or len(key) > len(best)):
            best = key
    if best is None:
        sys.stderr.write("fake herdr: no scenario for %r\n" % joined)
        return 9
    counters_path = os.path.join(d, "counters.json")
    try:
        with io.open(counters_path, encoding="utf-8") as f:
            counters = json.load(f)
    except (IOError, ValueError):
        counters = {}
    n = counters.get(best, 0)
    counters[best] = n + 1
    with io.open(counters_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(counters))
    responses = scenario[best]
    r = responses[min(n, len(responses) - 1)]
    sys.stdout.write(r.get("stdout", ""))
    sys.stderr.write(r.get("stderr", ""))
    return r.get("exit", 0)


if __name__ == "__main__":
    sys.exit(main())
