"""Env probe typed into the orchestrator pane before claude starts (bench/launch.ps1).

Prints yes/no per check, never a value. The launcher reads the line after BENCHPROBE-RESULT
and aborts unless every check says yes.
usage: python probe.py --home <OKE_HOME expected> --cwd <orchestrator folder expected>
"""
import argparse
import json
import os
import re
import sys
import urllib.request

# a proxy model that could reach a billed endpoint: a Claude or Anthropic name, or a wildcard route
BILLED = re.compile(r"(?i)claude|anthropic|[*?]")


def proxy_models(base, token):
    """The proxy's model names (`/v1/models` with the key), or None when it does not answer. The key
    goes only into the request header."""
    req = urllib.request.Request(base + "/v1/models", headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - any failure is a "no"
        return None
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    return [str(m.get("id") or "") for m in rows if isinstance(m, dict)]


def same(a, b):
    norm = lambda p: os.path.normcase(os.path.abspath(p.replace("/", os.sep))).rstrip("\\/")
    return bool(a) and bool(b) and norm(a) == norm(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--cwd", required=True)
    args = ap.parse_args()
    env = os.environ
    model_vars = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                  "ANTHROPIC_DEFAULT_FABLE_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")
    try:
        with urllib.request.urlopen(env.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:4000").rstrip("/")
                                    + "/health/liveliness", timeout=5) as r:
            proxy = r.status == 200
    except Exception:  # noqa: BLE001 - any failure is a "no"
        proxy = False
    models = proxy_models(env.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:4000").rstrip("/"),
                          env.get("ANTHROPIC_AUTH_TOKEN", ""))
    checks = [
        ("config_dir", env.get("CLAUDE_CONFIG_DIR", "").rstrip("\\/").endswith(".claude-free")),
        ("base_url", env.get("ANTHROPIC_BASE_URL", "").rstrip("/") == "http://127.0.0.1:4000"),
        ("auth_token", bool(env.get("ANTHROPIC_AUTH_TOKEN", "").strip())),
        ("api_key_unset", "ANTHROPIC_API_KEY" not in env),
        ("models", all(env.get(v) == "free-coder" for v in model_vars)
         and env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL") == "free-fast"),
        ("no_traffic", env.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC") == "1"),
        ("oke_home", same(env.get("OKE_HOME", ""), args.home)),
        ("oke_herdr", "oke-bench" in env.get("OKE_HERDR", "")),
        ("herdr_pane", env.get("HERDR_ENV") == "1" and bool(env.get("HERDR_PANE_ID"))),
        ("herdr_socket", "oke-bench" in env.get("HERDR_SOCKET_PATH", "")),
        ("no_session_vars", not any(k.startswith(("CLAUDECODE", "CLAUDE_CODE_SESSION", "CLAUDE_CODE_ENTRYPOINT",
                                                  "CLAUDE_PID", "CLAUDE_CODE_CHILD")) for k in env)),
        ("cwd", same(os.getcwd(), args.cwd)),
        ("proxy_alive", proxy),
        # only free routes: no claude-*/anthropic/* name and no wildcard a real Claude model would match
        ("proxy_models_free", bool(models) and not any(BILLED.search(m) for m in models)),
    ]
    line = " ".join("%s=%s" % (k, "yes" if ok else "no") for k, ok in checks)
    sys.stdout.write("BENCHPROBE-RESULT " + line + " BENCHPROBE-END\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
