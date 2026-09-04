"""models.py — one env var picks the model; the client follows from its name.

Everything goes through the shared APIM gateway, so swapping models is a matter of
setting `LOOP_MODEL`. The gateway fronts two APIs on one subscription key:

    /openai/v1     OpenAI-compatible Responses API, model in the body, header
                   `Ocp-Apim-Subscription-Key`. Routes to the Azure OpenAI deployments.
    /anthropic     Anthropic Messages API passthrough, header `x-api-key`. Routes by
                   model-name substring to the Claude deployments.

Both verified HTTP 200 on 2026-09-04 (gpt-5.6-luna, gpt-5.4-mini, claude-sonnet-4-6,
claude-fable-5-1). Two things the gateway's README warns about, kept here so nobody
re-learns them: `/openai/v1/models` lists the whole Azure catalogue rather than what is
deployed, so only a real POST is proof — that is what `probe()` is for; and the anthropic
policy strips `temperature`/`top_p` because the adaptive-thinking Claude models reject
them, so never set either.

Where the gateway and key come from: on a laptop, `~/.config/azure-apim/<name>.env` (the
user's central store, never copied into this repo); in the hosted container, the same
variable names injected from `azure.yaml`. Same names, one code path. Explicit environment
always wins over either file, and a missing gateway is an error rather than a silent fall
back to some ambient credential.
"""

from __future__ import annotations

import os
import pathlib

from dotenv import dotenv_values

DEFAULT_MODEL = "gpt-5.6-luna"
APIM_STORE = pathlib.Path.home() / ".config" / "azure-apim" / "apim-ssattiraju-01.env"
HERE = pathlib.Path(__file__).resolve().parent.parent


def load_env() -> None:
    """Project `.env` first, then the central APIM store. Neither overrides a real value.

    "Real" excludes the empty string. `azd ai agent run` injects every variable named in
    `azure.yaml` into the process, and any the azd environment has no value for arrives
    as `""` — which `load_dotenv(override=False)` would treat as already set, and the
    gateway would look unconfigured on a laptop that has it configured.
    """
    for path in (HERE / ".env", APIM_STORE):
        if not path.exists():
            continue
        for key, value in dotenv_values(path).items():
            if value is not None and not os.environ.get(key):
                os.environ[key] = value


def resolve_model(model: str | None = None) -> str:
    return model or os.environ.get("LOOP_MODEL") or DEFAULT_MODEL


def build_chat_client(model: str | None = None):
    """The chat client for `model`, by name prefix. Raises if the gateway is not configured."""
    load_env()
    model = resolve_model(model)
    gateway = os.environ.get("APIM_GATEWAY_URL", "").rstrip("/")
    key = os.environ.get("APIM_SUBSCRIPTION_KEY", "")
    if not (gateway and key):
        raise RuntimeError(
            "APIM_GATEWAY_URL and APIM_SUBSCRIPTION_KEY are not set. Source the APIM store "
            f"({APIM_STORE}) or copy .env.example to .env."
        )

    if model.startswith("claude-"):
        from agent_framework_anthropic import AnthropicClient

        # The Anthropic SDK sends the key as `x-api-key` — the header this API expects —
        # and appends `/v1/messages` to the base URL itself.
        return AnthropicClient(model=model, api_key=key, base_url=f"{gateway}/anthropic")

    from agent_framework_openai import OpenAIChatClient

    return OpenAIChatClient(
        model=model,
        api_key=key,  # the SDK insists on one; the gateway reads the header below
        base_url=f"{gateway}/openai/v1",
        default_headers={"Ocp-Apim-Subscription-Key": key},
    )


def describe(model: str | None = None) -> dict[str, str]:
    """What `build_chat_client` would do, without doing it."""
    load_env()
    model = resolve_model(model)
    gateway = os.environ.get("APIM_GATEWAY_URL", "").rstrip("/") or "(unset)"
    if model.startswith("claude-"):
        return {"model": model, "client": "AnthropicClient", "endpoint": f"{gateway}/anthropic/v1/messages"}
    return {"model": model, "client": "OpenAIChatClient", "endpoint": f"{gateway}/openai/v1/responses"}


async def probe(model: str | None = None) -> str:
    """One tiny round-trip. The only authoritative answer to 'does this model work here'."""
    from agent_framework import Agent

    agent = Agent(client=build_chat_client(model), instructions="Reply with the single word: ok")
    response = await agent.run("ping")
    return response.text.strip()
