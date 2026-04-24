.PHONY: install run test test-eval lint format clean help build-field-corpus

PYTHON ?= python3
VENV ?= .venv
PIP = $(VENV)/bin/pip
PY = $(VENV)/bin/python
UVICORN = $(VENV)/bin/uvicorn
PYTEST = $(VENV)/bin/pytest
RUFF = $(VENV)/bin/ruff

PORT ?= 8000

help:
	@echo "microsolder-agent — common tasks"
	@echo ""
	@echo "  make install   Create .venv and install dependencies (incl. dev)"
	@echo "  make run       Start uvicorn in dev mode on port $(PORT) with --reload"
	@echo "  make test      Run pytest (fast subset, skips slow benchmarks)"
	@echo "  make test-all  Run all pytest tests (incl. slow accuracy benchmarks)"
	@echo "  make lint      Run ruff check"
	@echo "  make format    Run ruff format"
	@echo "  make clean     Remove caches (keeps .venv)"

install:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

run:
	$(UVICORN) api.main:app --reload --host 0.0.0.0 --port $(PORT)

# Rebuild the field-calibrated benchmark fixture from persisted data
# (live outcome.json + legacy field_reports/*.md). Commit the fixture
# after running so diffs show corpus drift.
build-field-corpus:
	$(PY) scripts/build_benchmark_corpus.py

test:
	$(PYTEST) tests/ -v -m "not slow"

test-all:
	$(PYTEST) tests/ -v

# Score floor guard: fail the build if the simulator + hypothesize stack
# drops below 0.5 on the frozen MNT Reform bench. The floor only becomes
# meaningful once axes 2/3 are fully implemented — until then the gate is
# informational. Intentionally non-fatal on missing graph (exit 2 from the
# CLI bubbles up so the failure reason is visible).
test-eval:
	@SCORE=$$($(PY) -m scripts.eval_simulator --device mnt-reform-motherboard | $(PY) -c "import json, sys; print(json.loads(sys.stdin.read())['score'])"); \
		echo "simulator score = $$SCORE"; \
		$(PY) -c "import sys; sys.exit(0 if float('$$SCORE') >= 0.5 else 1)" || (echo "FAIL: score below 0.5 floor" && exit 1)

lint:
	$(RUFF) check api/ tests/

format:
	$(RUFF) format api/ tests/

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +

# --- Evolve (overnight self-improvement loop) ---

.PHONY: evolve-bootstrap evolve-run evolve-run-bg evolve-stop evolve-status

evolve-bootstrap:
	@./scripts/evolve-bootstrap.sh

evolve-run:
	@./scripts/evolve-runner.sh

evolve-run-bg:
	@nohup ./scripts/evolve-runner.sh >> /tmp/microsolder-evolve.log 2>&1 &
	@echo "Evolve runner started in background. Tail: tail -f /tmp/microsolder-evolve.log"
	@echo "Stop:  make evolve-stop"

evolve-stop:
	@if [ -f /tmp/microsolder-evolve.lock ]; then \
		PID=$$(cat /tmp/microsolder-evolve.lock); \
		echo "Killing runner PID $$PID"; \
		kill $$PID 2>/dev/null || true; \
		rm -f /tmp/microsolder-evolve.lock; \
	fi
	@pkill -f '[e]volve-runner.sh' 2>/dev/null || true
	@echo "Evolve runner stopped."

evolve-status:
	@echo "=== State ==="
	@cat evolve/state.json 2>/dev/null || echo "(not initialized)"
	@echo ""
	@echo "=== Last 10 results ==="
	@tail -10 evolve/results.tsv 2>/dev/null || echo "(no results yet)"
	@echo ""
	@echo "=== Lock ==="
	@if [ -f /tmp/microsolder-evolve.lock ]; then echo "Locked by PID $$(cat /tmp/microsolder-evolve.lock)"; else echo "No lock"; fi
	@echo ""
	@echo "=== Last 20 log lines ==="
	@tail -20 /tmp/microsolder-evolve.log 2>/dev/null || echo "(no log)"
