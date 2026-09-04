"""main.py — the Foundry hosted-agent entry point. Run locally exactly as the platform
runs it: `python main.py`, or `azd ai agent run` from the repository root.

Step 1: a plain agent with no tools, to prove the hosting path and the APIM route.
"""

from __future__ import annotations

import logging
import os

# The agentserver's OpenTelemetry distro probes the Azure instance-metadata endpoint at
# start-up. In a hosted container that is right; on a laptop it is a wall of
# ConnectTimeout tracebacks. The platform sets this variable, so its absence means local.
if not os.environ.get("FOUNDRY_HOSTING_ENVIRONMENT"):
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from agent_framework import Agent
from agent_framework_foundry_hosting import ResponsesHostServer

from loop.models import build_chat_client, describe

logging.basicConfig(level=logging.INFO)
logging.getLogger(__name__).info("model routing: %s", describe())

agent = Agent(
    client=build_chat_client(),
    name="strong-loop",
    instructions="You are a placeholder. Answer briefly.",
)

app = ResponsesHostServer(agent)

if __name__ == "__main__":
    app.run()
