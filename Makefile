.PHONY: install run test lint format clean help

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
	@echo "  make test      Run pytest"
	@echo "  make lint      Run ruff check"
	@echo "  make format    Run ruff format"
	@echo "  make clean     Remove caches (keeps .venv)"

install:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

run:
	$(UVICORN) api.main:app --reload --host 0.0.0.0 --port $(PORT)

test:
	$(PYTEST) tests/ -v

lint:
	$(RUFF) check api/ tests/

format:
	$(RUFF) format api/ tests/

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +
