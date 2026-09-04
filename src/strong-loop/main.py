"""main.py — the Foundry hosted-agent entry point. Run locally exactly as the platform
runs it: `python main.py`, or `azd ai agent run` from the repository root.

Step 1: a plain agent with no tools, to prove the hosting path and the APIM route.
"""

from __future__ import annotations

import logging

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
