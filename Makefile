# `python` is not on PATH under make's /bin/sh, and the repo's dependencies live in a
# 3.13 virtualenv rather than the system interpreter. Every target therefore runs
# .venv/bin/python (.venv/Scripts/python.exe on Windows), and `venv` creates it with uv
# if it is not there yet. `$(OS)` is set to `Windows_NT` by cmd.exe/PowerShell but not by
# a POSIX shell, which is what make uses on Windows to pick a branch below.
ifeq ($(OS),Windows_NT)
PYTHON := $(CURDIR)/.venv/Scripts/python.exe
else
PYTHON := $(CURDIR)/.venv/bin/python
endif

.PHONY: help venv test local-ui agent ui-server deploy deploy-check

help:
	@echo "make venv          Create .venv (uv, Python 3.13) and install requirements"
	@echo "make test          Run the test suite (no model, no network)"
	@echo "make local-ui      Start the local agent and live UI"
	@echo "make deploy-check  Preflight a machine for deployment (changes nothing)"
	@echo "make deploy        Deploy the agent, toolbox and tracing to Azure"

venv: $(PYTHON)

$(PYTHON):
ifeq ($(OS),Windows_NT)
	@where uv >nul 2>&1 || (echo uv is not installed: https://docs.astral.sh/uv/ & exit /b 1)
	uv venv --python 3.13
	set "VIRTUAL_ENV=$(CURDIR)\.venv" && uv pip install -r src/strong-loop/requirements.txt pytest
else
	@command -v uv >/dev/null 2>&1 || { echo "uv is not installed: https://docs.astral.sh/uv/"; exit 1; }
	uv venv --python 3.13
	VIRTUAL_ENV=$(CURDIR)/.venv uv pip install -r src/strong-loop/requirements.txt pytest
endif

test: venv
	$(PYTHON) -m pytest tests -q

# deploy.sh is bash; invoke it through bash explicitly rather than relying on the
# execute bit + shebang, which Windows honours neither of. Needs Git Bash or WSL there.
deploy:
	bash scripts/deploy.sh

deploy-check:
	bash scripts/deploy.sh --check-only

ifeq ($(OS),Windows_NT)
# cmd has no trap/kill/wait, so each process gets its own console window instead of this
# Makefile managing them as background jobs; close the windows (or Ctrl+C in each) to stop.
local-ui: venv
	@start "strong-loop agent" cmd /c "cd src/strong-loop && "$(PYTHON)" main.py"
	@start "strong-loop ui" cmd /c ""$(PYTHON)" -m http.server 8000 --directory docs"
	@timeout /t 2 /nobreak >nul
	@echo Local UI: http://localhost:8000/live.html
	@echo Agent:    http://localhost:8088/responses
	@start "" http://localhost:8000/live.html
else
local-ui: venv
	@agent_pid=; ui_pid=; \
		cleanup() { \
			trap - INT TERM EXIT; \
			[ -z "$$agent_pid" ] || kill "$$agent_pid" 2>/dev/null || true; \
			[ -z "$$ui_pid" ] || kill "$$ui_pid" 2>/dev/null || true; \
			[ -z "$$agent_pid" ] || wait "$$agent_pid" 2>/dev/null || true; \
			[ -z "$$ui_pid" ] || wait "$$ui_pid" 2>/dev/null || true; \
		}; \
		trap cleanup INT TERM EXIT; \
		(cd src/strong-loop && $(PYTHON) main.py) & agent_pid=$$!; \
		($(PYTHON) -m http.server 8000 --directory docs) & ui_pid=$$!; \
		sleep 2; \
		echo "Local UI: http://localhost:8000/live.html"; \
		echo "Agent:    http://localhost:8088/responses"; \
		if command -v xdg-open >/dev/null 2>&1; then xdg-open http://localhost:8000/live.html >/dev/null 2>&1 || true; fi; \
		wait "$$agent_pid" "$$ui_pid"
endif

agent: venv
	cd src/strong-loop && $(PYTHON) main.py

ui-server: venv
	$(PYTHON) -m http.server 8000 --directory docs
