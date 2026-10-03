SHELL := /bin/sh

PROJECT := voiceink-win
PYTHON ?= python3
VENV := .venv

ifeq ($(OS),Windows_NT)
VENV_PYTHON := $(VENV)/Scripts/python.exe
VENV_PIP := $(VENV)/Scripts/python.exe -m pip
else
VENV_PYTHON := $(VENV)/bin/python
VENV_PIP := $(VENV)/bin/python -m pip
endif

.PHONY: help setup format format-check lint spec-check test build check run clean install-hooks

## help: Show available development commands
help:
	@grep -E '^## ' Makefile | sed 's/^## //' | sort

## setup: Create the virtual environment and install development dependencies
setup:
	$(PYTHON) -m venv $(VENV)
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install --editable '.[dev]'

## format: Format Python source, scripts, and tests
format:
	$(VENV_PYTHON) -m ruff format src tests scripts

## format-check: Verify Python formatting without changing files
format-check:
	$(VENV_PYTHON) -m ruff format --check src tests scripts

## lint: Run static Python checks
lint:
	$(VENV_PYTHON) -m ruff check src tests scripts

## spec-check: Validate the living specification catalog and required sections
spec-check:
	$(PYTHON) scripts/spec_check.py

## test: Run behavior-focused automated tests
test:
	$(VENV_PYTHON) -m pytest

## build: Compile Python source without creating a distributable artifact
build:
	$(VENV_PYTHON) -m compileall -q src scripts

## check: Run the complete local quality gate
check: spec-check format-check lint test build

## run: Start the application after the runtime is implemented
run:
	$(VENV_PYTHON) -m voiceink_win

## clean: Remove local caches and generated build directories
clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info

## install-hooks: Install repository pre-commit and pre-push checks
install-hooks:
	cp scripts/githooks/pre-commit .git/hooks/pre-commit
	cp scripts/githooks/pre-push .git/hooks/pre-push
	chmod +x .git/hooks/pre-commit .git/hooks/pre-push
	@printf '%s\n' 'Git hooks installed: pre-commit and pre-push run make check.'
