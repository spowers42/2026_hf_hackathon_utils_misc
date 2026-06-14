#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROFILE="${1:-local}"
ENV_FILE="$SCRIPT_DIR/.env.$PROFILE"

if [ ! -f "$ENV_FILE" ]; then
  echo "Error: $ENV_FILE not found" >&2
  echo "Usage: $0 [local|claude]" >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

if [ "$PROFILE" = "claude" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Error: ANTHROPIC_API_KEY is not set in $ENV_FILE" >&2
  exit 1
fi

BATCHES=(
  #"$SCRIPT_DIR/batch_00_test.json"
  "$SCRIPT_DIR/batch_01_trail_lookup.json"
  "$SCRIPT_DIR/batch_02_progression.json"
  "$SCRIPT_DIR/batch_03_gear_safety.json"
  "$SCRIPT_DIR/batch_04_recommendation.json"
  "$SCRIPT_DIR/batch_05_weather.json"
)

overall_start=$(date +%s)

for batch in "${BATCHES[@]}"; do
  name="$(basename "$batch" .json)"
  echo "=== $name ==="
  batch_start=$(date +%s)
  python "$SCRIPT_DIR/generate_qa.py" \
    --batch "$batch" \
    --backend "$BACKEND" \
    ${SEED:+--seed "$SEED"}
  batch_end=$(date +%s)
  printf "  %-30s %ds\n" "Batch time:" $((batch_end - batch_start))
  echo
done

overall_end=$(date +%s)
printf "%-30s %ds\n" "Total time:" $((overall_end - overall_start))
