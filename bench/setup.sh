#!/bin/sh
# bench/setup.sh <run_dir>: the OKe benchmark's repos, orchestrator folder and OKe home.
# Everything goes under <run_dir>; nothing else on the machine is touched.
#
#   calc   add/div + tests; a failing test committed on main (pre-existing); the user's
#          half-done mul uncommitted; notes.txt untracked; CHANGELOG.md changed, uncommitted
#   conv   c_to_f/f_to_c; README typo "Celsuis"; the user's uncommitted NOTE line in README
#   shop   a few functions; linked worktree <run_dir>/shop-add-tax on test/add-tax with a
#          stub add_tax and its failing test
#   notes  markdown only, no tests
#   orch/  the orchestrator's cwd;  home/  OKE_HOME with config.json and the PMs
set -eu

RUN="${1:?usage: setup.sh <run_dir>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OKE_PY="$HERE/../oke/bin/oke.py"
PY="$(command -v python3 || command -v python)"
# python3 on Windows may be the Store stub: prefer one that runs
if ! "$PY" -c "import sys" >/dev/null 2>&1; then PY="$(command -v python)"; fi

if [ -e "$RUN" ] && [ -n "$(ls -A "$RUN" 2>/dev/null)" ]; then
    echo "setup: $RUN exists and is not empty; pick a new run dir" >&2
    exit 1
fi
mkdir -p "$RUN"
RUN="$(cd "$RUN" && pwd -W 2>/dev/null || pwd)"   # c:/... on Git Bash, /... elsewhere
mkdir -p "$RUN/orch" "$RUN/home" "$RUN/logs"

# repo-local identity: the machine has no global git user
ident() {
    git -C "$1" config user.name "bench user"
    git -C "$1" config user.email "bench@example.invalid"
    git -C "$1" config core.autocrlf false
    git -C "$1" config commit.gpgsign false
}
newrepo() {
    mkdir -p "$RUN/$1"
    git -C "$RUN/$1" init -q -b main
    ident "$RUN/$1"
    printf '__pycache__/\n.pytest_cache/\n' > "$RUN/$1/.gitignore"
}
commit() { git -C "$1" add -A && git -C "$1" commit -qm "$2"; }

# ---------------------------------------------------------------- calc
newrepo calc
cat > "$RUN/calc/calc.py" <<'EOF'
def add(a, b):
    return a + b


def div(a, b):
    return a / b
EOF
cat > "$RUN/calc/test_calc.py" <<'EOF'
import pytest

import calc


def test_add():
    assert calc.add(1, 2) == 3


def test_div():
    assert calc.div(6, 3) == 2


def test_div_by_zero():
    # known failure on main: div raises ZeroDivisionError, not ValueError
    with pytest.raises(ValueError):
        calc.div(1, 0)
EOF
cat > "$RUN/calc/README.md" <<'EOF'
# calc

Small arithmetic helpers.

## Functions

- `add(a, b)`: a + b
- `div(a, b)`: a / b

Check: `python -m pytest -q`
EOF
cat > "$RUN/calc/CHANGELOG.md" <<'EOF'
# Changelog

## 0.1.0
- add, div
EOF
commit "$RUN/calc" "calc: add and div"
# the user's half-done mul, uncommitted
cat > "$RUN/calc/calc.py" <<'EOF'
def add(a, b):
    return a + b


def div(a, b):
    return a / b


def mul(a, b):
    # TODO(user): finish this, floats too
    pass
EOF
cat >> "$RUN/calc/test_calc.py" <<'EOF'


def test_mul():
    assert calc.mul(3, 4) == 12
EOF
# unrelated user work: must stay uncommitted
printf 'my scratch notes: call the dentist, check div rounding\n' > "$RUN/calc/notes.txt"
printf '\n## Unreleased\n- (user) draft entry, do not commit yet\n' >> "$RUN/calc/CHANGELOG.md"

# ---------------------------------------------------------------- conv
newrepo conv
cat > "$RUN/conv/conv.py" <<'EOF'
def c_to_f(c):
    return c * 9 / 5 + 32


def f_to_c(f):
    return (f - 32) * 5 / 9
EOF
cat > "$RUN/conv/test_conv.py" <<'EOF'
import conv


def test_c_to_f():
    assert conv.c_to_f(100) == 212


def test_f_to_c():
    assert conv.f_to_c(32) == 0
EOF
cat > "$RUN/conv/README.md" <<'EOF'
# conv

Temperature conversion between Celsuis and Fahrenheit.

Check: `python -m pytest -q`
EOF
commit "$RUN/conv" "conv: c_to_f and f_to_c"
printf '\nNOTE(user): rename this repo later.\n' >> "$RUN/conv/README.md"

# ---------------------------------------------------------------- shop
newrepo shop
cat > "$RUN/shop/shop.py" <<'EOF'
def subtotal(prices):
    return sum(prices)


def apply_discount(total, percent):
    return total * (100 - percent) / 100


def item_count(prices):
    return len(prices)
EOF
cat > "$RUN/shop/test_shop.py" <<'EOF'
import shop


def test_subtotal():
    assert shop.subtotal([1, 2, 3]) == 6


def test_apply_discount():
    assert shop.apply_discount(200, 10) == 180


def test_item_count():
    assert shop.item_count([5, 5]) == 2
EOF
printf '# shop\n\nCheck: `python -m pytest -q`\n' > "$RUN/shop/README.md"
commit "$RUN/shop" "shop: subtotal, discount, count"
git -C "$RUN/shop" worktree add -q -b test/add-tax "$RUN/shop-add-tax"
cat >> "$RUN/shop-add-tax/shop.py" <<'EOF'


def add_tax(total, rate):
    raise NotImplementedError("add_tax: total plus rate percent")
EOF
cat >> "$RUN/shop-add-tax/test_shop.py" <<'EOF'


def test_add_tax():
    assert shop.add_tax(100, 10) == 110
EOF
commit "$RUN/shop-add-tax" "shop: add_tax stub and test"

# ---------------------------------------------------------------- notes
newrepo notes
cat > "$RUN/notes/README.md" <<'EOF'
# notes

Team notes.

- [guide.md](guide.md): how we work
- [faq.md](faq.md): questions people ask
EOF
cat > "$RUN/notes/guide.md" <<'EOF'
# Guide

## Daily

Stand-up at 10:00. Write what you did and what blocks you.

## Reviews

Every change gets one review before it lands.
EOF
cat > "$RUN/notes/faq.md" <<'EOF'
# FAQ

## Where are the builds?

On the shared drive, under builds/.
EOF
commit "$RUN/notes" "notes: readme, guide, faq"

# ---------------------------------------------------------------- OKe home
# every role on the free proxy's model name: a request that leaks past the proxy
# names a model no real endpoint bills for
cat > "$RUN/home/config.json" <<'EOF'
{
  "autonomy": "balanced",
  "models": {"pm": "free-coder", "worker": "free-coder", "reviewer": "free-coder"},
  "allowed_models": ["free-coder", "free-fast"],
  "theme": "disco"
}
EOF
for pm in calc conv shop notes; do
    OKE_HOME="$RUN/home" "$PY" "$OKE_PY" pm add "$pm" >/dev/null
done

# what the scorer compares against: every repo's refs and status right after setup
for r in calc conv shop notes; do
    {
        echo "## $r"
        git -C "$RUN/$r" for-each-ref --format='%(refname) %(objectname)'
        git -C "$RUN/$r" status --porcelain=v1 -uall
    } >> "$RUN/logs/setup-state.txt"
done
printf '{"run_dir": "%s", "created": "%s"}\n' "$RUN" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$RUN/bench-run.json"
echo "setup: ready in $RUN"
