.PHONY: help local-ui agent ui-server

help:
	@echo "make local-ui  Start the local agent and live UI"

local-ui:
	@agent_pid=; ui_pid=; \
		cleanup() { \
			trap - INT TERM EXIT; \
			[ -z "$$agent_pid" ] || kill "$$agent_pid" 2>/dev/null || true; \
			[ -z "$$ui_pid" ] || kill "$$ui_pid" 2>/dev/null || true; \
			[ -z "$$agent_pid" ] || wait "$$agent_pid" 2>/dev/null || true; \
			[ -z "$$ui_pid" ] || wait "$$ui_pid" 2>/dev/null || true; \
		}; \
		trap cleanup INT TERM EXIT; \
		(cd src/strong-loop && python main.py) & agent_pid=$$!; \
		(python -m http.server 8000 --directory docs) & ui_pid=$$!; \
		sleep 2; \
		echo "Local UI: http://localhost:8000/live.html"; \
		echo "Agent:    http://localhost:8088/responses"; \
		if command -v xdg-open >/dev/null 2>&1; then xdg-open http://localhost:8000/live.html >/dev/null 2>&1 || true; fi; \
		wait "$$agent_pid" "$$ui_pid"

agent:
	cd src/strong-loop && python main.py

ui-server:
	python -m http.server 8000 --directory docs
