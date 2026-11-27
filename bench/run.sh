#!/bin/sh
# bench/run.sh [--keep] [--only T01-answer,T02-anchored-typo] [--from ID] [--budget-scale X] [run_dir]
#
# setup -> launcher (free-proxy env probe in herdr session oke-bench) -> driver -> score -> teardown.
# --keep: leave the oke-bench session and the run dir for inspection (teardown later with
#         `python bench/teardown.py <run_dir>`).
# The report and the driver logs are copied to bench/results/<stamp>/ before teardown.
set -u
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'   # Git Bash must not rewrite /oke:start or paths
# every herdr call reaches the bench session only: an inherited HERDR_* (a socket path) beats --session
for v in $(env | sed -n 's/^\(HERDR_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$v"; done
HERE="$(cd "$(dirname "$0")" && (pwd -W 2>/dev/null || pwd))"   # c:/... on Git Bash: Windows python needs it
PY="$(command -v python || command -v python3)"
STAMP="$(date +%Y%m%d-%H%M%S)"
KEEP=0
DRIVER_ARGS=""
RUN=""
while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1 ;;
        --only|--from|--budget-scale) DRIVER_ARGS="$DRIVER_ARGS $1 $2"; shift ;;
        -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
        *) RUN="$1" ;;
    esac
    shift
done
if [ -z "$RUN" ]; then
    BASE="${LOCALAPPDATA:-$HOME}"
    command -v cygpath >/dev/null 2>&1 && BASE="$(cygpath -m "$BASE")"
    RUN="$BASE/oke-bench/run-$STAMP"
fi
RESULTS="$HERE/results/$STAMP"
ps_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

teardown() {
    if [ "$KEEP" = 1 ]; then
        echo "run: --keep: session oke-bench and $RUN stay; later: $PY $HERE/teardown.py $RUN"
    else
        "$PY" "$HERE/teardown.py" "$RUN"
    fi
}
save_results() {
    mkdir -p "$RESULTS"
    [ -f "$RUN/report.md" ] && cp "$RUN/report.md" "$RESULTS/"
    [ -d "$RUN/logs" ] && cp -r "$RUN/logs" "$RESULTS/"
    echo "run: results in $RESULTS"
}
trap 'echo "run: interrupted"; save_results; teardown; exit 130' INT TERM

echo "run: dir $RUN"
sh "$HERE/setup.sh" "$RUN" || exit 1
RUN="$(cd "$RUN" && (pwd -W 2>/dev/null || pwd))"

if ! timeout 300 powershell -NoProfile -ExecutionPolicy Bypass -File "$(ps_path "$HERE/launch.ps1")" -RunDir "$(ps_path "$RUN")"; then
    echo "run: launcher failed (it stopped its own session); nothing ran"
    save_results
    "$PY" "$HERE/teardown.py" "$RUN" --keep-run-dir
    exit 1
fi

# every task has its own budget; this is only the backstop for the whole night
# shellcheck disable=SC2086
timeout --kill-after=60 36000 "$PY" "$HERE/driver.py" "$RUN" $DRIVER_ARGS
DCODE=$?
echo "run: driver exit $DCODE"
"$PY" "$HERE/score.py" "$RUN"
save_results
trap - INT TERM
teardown
exit $DCODE
