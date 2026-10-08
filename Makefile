SHELL := /bin/sh

PROJECT := voiceink-win
VENV := .venv

ifeq ($(OS),Windows_NT)
PYTHON ?= python
VENV_PYTHON := $(VENV)/Scripts/python.exe
VENV_PIP := $(VENV)/Scripts/python.exe -m pip
else
PYTHON ?= python3
VENV_PYTHON := $(VENV)/bin/python
VENV_PIP := $(VENV)/bin/python -m pip
endif

.PHONY: help setup format format-check lint spec-check test compile build build-deps require-windows diagnostic-build native-smoke check run run-shell clean install-hooks verify-branch push

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
	$(VENV_PYTHON) scripts/spec_check.py

## test: Run behavior-focused automated tests
test:
	$(VENV_PYTHON) -m pytest

## compile: Compile Python source without creating a distributable artifact
compile:
	$(VENV_PYTHON) -m compileall -q src scripts

## require-windows: Stop Windows-only packaging with an actionable error elsewhere
require-windows:
ifeq ($(OS),Windows_NT)
	@:
else
	@printf '%s\n' 'Frontend packaging requires 64-bit Windows (Windows_NT). Run `make check` on macOS/Linux, or use a Windows 10/11 x64 host for `make build`.' >&2; exit 1
endif

## build-deps: Create the build environment and verify the exact GUI/PyInstaller extras
build-deps: require-windows
	$(PYTHON) -c "import platform, sys; version = sys.version_info[:2]; allowed = {(3, 12), (3, 13), (3, 14)}; raise SystemExit('Python 3.12, 3.13, or 3.14 is required; found ' + platform.python_version()) if version not in allowed else None"
	$(PYTHON) -m venv $(VENV)
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install --editable ".[gui,build]"
	$(VENV_PIP) check
	$(VENV_PYTHON) -c "import importlib.metadata as metadata; import PyInstaller, PySide6, shiboken6; print('Verified build dependencies: ' + ', '.join(name + '==' + metadata.version(name) for name in ('PySide6', 'shiboken6', 'pyinstaller')))"

## build: Build and validate the Windows GUI and console smoke executables in dist/
build: require-windows build-deps
	$(VENV_PYTHON) scripts/frontend_build.py

## diagnostic-build: Build the Windows diagnostic executable (run on Windows)
diagnostic-build:
	@test -n "$(VOICEINK_FFMPEG_PATH)" || (printf '%s\n' 'VOICEINK_FFMPEG_PATH must point to an external ffmpeg.exe' >&2; exit 1)
	@test -n "$(VOICEINK_FFMPEG_MANIFEST)" || (printf '%s\n' 'VOICEINK_FFMPEG_MANIFEST must point to ffmpeg.manifest.json' >&2; exit 1)
	$(VENV_PYTHON) -m PyInstaller --clean --noconfirm --onefile --name voiceink-diagnostic --add-binary "$(VOICEINK_FFMPEG_PATH);." --add-data "$(VOICEINK_FFMPEG_MANIFEST);." scripts/diagnostic_cli.py

## native-smoke: Run the real Windows snapshot and FFmpeg smoke contract
native-smoke:
	$(VENV_PYTHON) scripts/native_smoke.py

## check: Run the complete platform-independent local quality gate
check: spec-check format-check lint test compile

## run: Start the application after the runtime is implemented
run:
	$(VENV_PYTHON) -m voiceink_win

## run-shell: Start the optional PySide6 desktop shell
run-shell:
	$(VENV_PYTHON) -m voiceink_win.presentation.app

## clean: Remove local caches and generated build directories
clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info

## install-hooks: Install repository pre-commit and pre-push checks
install-hooks:
	cp scripts/githooks/pre-commit .git/hooks/pre-commit
	cp scripts/githooks/pre-push .git/hooks/pre-push
	chmod +x .git/hooks/pre-commit .git/hooks/pre-push
	@printf '%s\n' 'Git hooks installed: pre-commit and pre-push run make check.'

## verify-branch: Verify that the current branch is publishable through a pull request
verify-branch:
	@branch="$$(git branch --show-current)"; \
	test -n "$$branch" || (printf '%s\n' 'Push rejected: detached HEAD is not publishable.' >&2; exit 1); \
	case "$$branch" in \
		feature/*|fix/*|refactor/*|docs/*|test/*|chore/*) ;; \
		*) printf '%s\n' "Push rejected: branch '$$branch' must use a feature, fix, refactor, docs, test, or chore prefix." >&2; exit 1 ;; \
	esac
	@test "$$(git branch --show-current)" != "main" || (printf '%s\n' 'Push rejected: main is protected; use a feature branch and a pull request.' >&2; exit 1)
	@test -z "$$(git status --porcelain)" || (printf '%s\n' 'Push rejected: the worktree must be clean and all changes committed.' >&2; exit 1)

## push: Run all checks and push the current non-main branch for a pull request
push: install-hooks check verify-branch
	@branch="$$(git branch --show-current)"; printf '%s\n' "Pushing $$branch for PR review"; git push -u origin "$$branch"
