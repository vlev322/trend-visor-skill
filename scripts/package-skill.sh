#!/usr/bin/env bash
set -euo pipefail

# Copies only what's needed to run this skill (no tests/evals/assets/dev planning docs,
# no .venv, no .env with real secrets). Usage: scripts/package-skill.sh <destination-dir> [--with-tests]

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <destination-dir> [--with-tests]" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="$1"
WITH_TESTS="${2:-}"

mkdir -p "$DEST_DIR"

ITEMS=(SKILL.md README.md pyproject.toml uv.lock .env.example references tools)
if [[ "$WITH_TESTS" == "--with-tests" ]]; then
  ITEMS+=(tests)
fi

for item in "${ITEMS[@]}"; do
  rsync -a --exclude='__pycache__' --exclude='*.pyc' "$SRC_DIR/$item" "$DEST_DIR/"
done

echo "Copied to $DEST_DIR"
echo "Next steps:"
echo "  cd $DEST_DIR"
echo "  cp .env.example .env   # fill in TREND_VISOR_USER_AGENT with real contact info"
echo "  uv sync --locked --extra charts"
