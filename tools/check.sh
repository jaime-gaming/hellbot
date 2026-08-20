#!/usr/bin/env bash
# One command to check everything before shipping:
#
#   ./tools/check.sh            lint + types + tests + offline event simulation
#   ./tools/check.sh --fast     skip the simulations
#
# Uses .venv when present, otherwise whatever python3 is on PATH.
set -euo pipefail

cd "$(dirname "$0")/.."

PY=python3
[[ -x .venv/bin/python ]] && PY=.venv/bin/python

fail=0
step() {
    printf '\n\033[1m== %s\033[0m\n' "$1"
}
run() {
    if "$@"; then
        printf '\033[32mok\033[0m\n'
    else
        printf '\033[31mFAILED: %s\033[0m\n' "$*"
        fail=1
    fi
}

step "Byte-compile"
run "$PY" -m compileall -q hell launcher tools Announcements.py bot.py launcher_main.py

step "Lint (pyflakes)"
run "$PY" -m pyflakes hell launcher tests tools bot.py Announcements.py

step "Lint (ruff)"
if "$PY" -c "import ruff" 2>/dev/null || command -v ruff >/dev/null; then
    run "$PY" -m ruff check .
else
    echo "ruff not installed (optional) — pip install ruff"
fi

step "Types (mypy)"
if "$PY" -c "import mypy" 2>/dev/null; then
    run "$PY" -m mypy
else
    echo "mypy not installed — pip install -r requirements-dev.txt"
fi

step "Tests"
run "$PY" -m pytest

if [[ "${1:-}" != "--fast" ]]; then
    step "Offline event simulation"
    run "$PY" tools/simulate.py --step 900
    run "$PY" tools/simulate.py --fail-at 40 --step 900
fi

printf '\n'
if [[ $fail -eq 0 ]]; then
    printf '\033[32mAll checks passed.\033[0m\n'
else
    printf '\033[31mSome checks failed (see above).\033[0m\n'
fi
exit $fail
