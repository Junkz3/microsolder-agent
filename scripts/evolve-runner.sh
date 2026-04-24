#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Microsolder Evolve — Runner (infinite loop, agent-driven)
#
# Usage:
#   Foreground (smoke test):  ./scripts/evolve-runner.sh
#   Background (overnight):   nohup ./scripts/evolve-runner.sh 2>&1 &
#
# Override interval (default 60s between sessions):
#   EVOLVE_INTERVAL=120 ./scripts/evolve-runner.sh

set -uo pipefail
cd "$(dirname "$0")/.."

LOCKFILE="/tmp/microsolder-evolve.lock"
LOGFILE="/tmp/microsolder-evolve.log"
INTERVAL="${EVOLVE_INTERVAL:-60}"
SKILL_FILE=".claude/skills/microsolder-evolve/SKILL.md"

# --- Pre-flight ---
if [ ! -f "$SKILL_FILE" ]; then
  echo "ERROR: missing $SKILL_FILE — install the skill first." | tee -a "$LOGFILE"
  exit 1
fi

if [ ! -f "evolve/state.json" ] || [ ! -f "evolve/results.tsv" ]; then
  echo "ERROR: evolve/ not initialized. Run scripts/evolve-bootstrap.sh first." | tee -a "$LOGFILE"
  exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "ERROR: 'claude' CLI not found in PATH." | tee -a "$LOGFILE"
  exit 1
fi

# --- Cleanup on exit ---
trap "rm -f $LOCKFILE; echo '[EVOLVE] runner stopped at $(date)' >> $LOGFILE; exit 0" EXIT INT TERM

echo "[EVOLVE] runner started at $(date)" >> "$LOGFILE"
echo "[EVOLVE] interval: ${INTERVAL}s, log: $LOGFILE" >> "$LOGFILE"

# --- Main loop ---
while true; do
  if [ -f "$LOCKFILE" ]; then
    # Another session is running (shouldn't happen since claude -p is synchronous,
    # but guards against accidental double-start).
    echo "[EVOLVE] lockfile present, skipping (PID in lock: $(cat $LOCKFILE))" >> "$LOGFILE"
    sleep "$INTERVAL"
    continue
  fi

  echo $$ > "$LOCKFILE"
  echo "" >> "$LOGFILE"
  echo "=== EVOLVE SESSION $(date) ===" >> "$LOGFILE"

  # Invoke a fresh Claude session with the skill as system prompt.
  # --max-turns 100: hard cap so a stuck session can't burn unlimited tokens.
  # --dangerously-skip-permissions: required for autonomous git/file ops.
  echo "Execute one evolve session." | claude -p \
    --dangerously-skip-permissions \
    --max-turns 100 \
    --system-prompt-file "$SKILL_FILE" \
    >> "$LOGFILE" 2>&1 || true

  echo "=== EVOLVE EXIT $(date) (exit=$?) ===" >> "$LOGFILE"
  rm -f "$LOCKFILE"

  sleep "$INTERVAL"
done
