# `python` is not on PATH under make's /bin/sh, and the repo's dependencies live in a
# 3.13 virtualenv rather than the system interpreter. Every target therefore runs
# .venv/bin/python, and `venv` creates it with uv if it is not there yet.
PYTHON := $(CURDIR)/.venv/bin/python

.PHONY: help venv test local-ui agent ui-server deploy deploy-check

help:
	@echo "make venv          Create .venv (uv, Python 3.13) and install requirements"
	@echo "make test          Run the test suite (no model, no network)"
	@echo "make local-ui      Start the local agent and live UI"
	@echo "make deploy-check  Preflight a machine for deployment (changes nothing)"
	@echo "make deploy        Deploy the agent, toolbox and tracing to Azure"

venv: $(PYTHON)

$(PYTHON):
	@command -v uv >/dev/null 2>&1 || { echo "uv is not installed: https://docs.astral.sh/uv/"; exit 1; }
	uv venv --python 3.13
	VIRTUAL_ENV=$(CURDIR)/.venv uv pip install -r src/strong-loop/requirements.txt pytest

test: venv
	$(PYTHON) -m pytest tests -q

deploy:
	./scripts/deploy.sh

deploy-check:
	./scripts/deploy.sh --check-only

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

agent: venv
	cd src/strong-loop && $(PYTHON) main.py

ui-server: venv
	$(PYTHON) -m http.server 8000 --directory docs
