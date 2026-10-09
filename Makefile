SHELL := /bin/sh

PROJECT := voiceink-win
VENV := .venv
PIP_VERSION := 26.2.1
BUILD_CONSTRAINTS := packaging/windows-build-constraints.txt
WINDOWS_RELEASE_LOCK ?= .github/native-smoke/artifact-lock.template.json

ifeq ($(OS),Windows_NT)
PYTHON ?= python
VENV_PYTHON := $(VENV)/Scripts/python.exe
VENV_PIP := $(VENV)/Scripts/python.exe -m pip
else
PYTHON ?= python3
VENV_PYTHON := $(VENV)/bin/python
VENV_PIP := $(VENV)/bin/python -m pip
endif

.PHONY: help setup format format-check lint spec-check wasapi-contract-check test compile build build-deps portable-package windows-release-smoke require-portable-artifacts require-windows diagnostic-build native-smoke check run run-shell clean install-hooks verify-branch push

## help: Show available development commands
help:
	@grep -E '^## ' Makefile | sed 's/^## //' | sort

## setup: Create the virtual environment and install development dependencies
setup:
	$(PYTHON) -m venv $(VENV)
	$(VENV_PIP) install --upgrade "pip==$(PIP_VERSION)"
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
spec-check: wasapi-contract-check
	$(VENV_PYTHON) scripts/spec_check.py

## wasapi-contract-check: Validate the disabled native WASAPI scaffold and provenance template
wasapi-contract-check:
	$(VENV_PYTHON) scripts/wasapi_contract.py

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
	$(PYTHON) -c "import platform, sys; version = sys.version_info[:2]; allowed = {(3, 12), (3, 13), (3, 14)}; sys.exit('Python 3.12, 3.13, or 3.14 is required; found ' + platform.python_version()) if version not in allowed else None"
	$(PYTHON) -m venv $(VENV)
	$(VENV_PIP) install --upgrade "pip==$(PIP_VERSION)"
	$(VENV_PIP) install --editable ".[gui,build]" --constraint "$(BUILD_CONSTRAINTS)"
	$(VENV_PIP) check
	$(VENV_PYTHON) -c "import importlib.metadata as metadata; import sys; expected = {'Pillow': '12.3.0', 'PySide6': '6.12.0', 'PySide6-Addons': '6.12.0', 'PySide6-Essentials': '6.12.0', 'PySide6-Pdf': '6.12.0.140', 'PySide6-WebEngine': '6.12.0.140', 'altgraph': '0.17.5', 'packaging': '26.3', 'pefile': '2024.8.26', 'pyinstaller': '6.22.3', 'pyinstaller-hooks-contrib': '2026.8', 'pywin32-ctypes': '0.2.3', 'shiboken6': '6.12.0'}; actual = {name: metadata.version(name) for name in expected}; mismatches = [name + '==' + actual[name] + ' (expected ' + version + ')' for name, version in expected.items() if actual[name] != version]; sys.exit('Build dependency pin mismatch: ' + ', '.join(mismatches)) if mismatches else print('Verified build dependency pins: ' + ', '.join(name + '==' + actual[name] for name in sorted(actual)))"

## build: Build and validate the Windows GUI and console smoke executables in dist/
build: require-windows build-deps
	$(VENV_PYTHON) scripts/frontend_build.py

## portable-package: Stage a relocatable CPU runtime bundle from pinned local artifacts
portable-package: require-windows require-portable-artifacts build
	$(VENV_PYTHON) scripts/portable_package.py --shell-dist dist --ffmpeg "$(VOICEINK_FFMPEG_PATH)" --sidecar "$(VOICEINK_SIDECAR_PATH)" --model "$(VOICEINK_MODEL_PATH)" --output release/voiceink-shell-windows-x64

## windows-release-smoke: Download tracked pins, build, relocate, smoke-test, and bundle the Windows release package
windows-release-smoke: build
	$(VENV_PYTHON) scripts/windows_release_package.py --lock "$(WINDOWS_RELEASE_LOCK)" --shell-dist dist --output release/voiceink-shell-windows-x64 --bundle release/voiceink-shell-windows-x64.zip --report release/windows-release-smoke.json

## require-portable-artifacts: Verify portable package inputs before the Windows build
require-portable-artifacts:
	@test -n "$(VOICEINK_FFMPEG_PATH)" || (printf '%s\n' 'VOICEINK_FFMPEG_PATH must point to a pinned ffmpeg.exe' >&2; exit 1)
	@test -n "$(VOICEINK_SIDECAR_PATH)" || (printf '%s\n' 'VOICEINK_SIDECAR_PATH must point to a pinned nemo-speech.exe' >&2; exit 1)
	@test -n "$(VOICEINK_MODEL_PATH)" || (printf '%s\n' 'VOICEINK_MODEL_PATH must point to a pinned Parakeet model' >&2; exit 1)

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
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist release *.egg-info

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
