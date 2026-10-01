#!/usr/bin/env bash
# Runs after every Edit/Write inside the project. Auto-fixes lint issues,
# then checks types and runs the full test suite. A non-zero exit here
# surfaces the failure back to Claude as blocking feedback (see CLAUDE.md:
# "a failing hook means stop and fix before continuing").
set -uo pipefail
cd "$CLAUDE_PROJECT_DIR" || exit 1

# Skip gracefully if the project isn't installed yet (e.g. very first edit).
if ! command -v poetry >/dev/null 2>&1; then
  exit 0
fi

fail=0

echo "== ruff =="
poetry run ruff check . --fix -q || fail=1

echo "== mypy =="
poetry run mypy src || fail=1

echo "== pytest =="
poetry run pytest -q || fail=1

if [ "$fail" -ne 0 ]; then
  echo "post-edit-check FAILED — fix the above before continuing." >&2
  exit 2
fi

exit 0
