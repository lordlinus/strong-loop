"""P0 spike: can a caller create a hosted-agent session, upload a file into its $HOME,
and have the agent see it on a Responses turn pinned to that session?

Raw REST on purpose — the production caller will be the Node API, so the URL shapes are
what is being verified, not an SDK. Reads coordinates from the azd env.

    python tools/spike_session_files.py            # against the deployed agent
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import httpx
from azure.identity import DefaultAzureCredential

ENV = pathlib.Path(__file__).resolve().parents[1] / ".azure" / "strong-loop" / ".env"


def azd_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v.strip().strip('"')
    return out


def main() -> int:
    env = azd_env()
    project = env["AGENT_STRONG_LOOP_PROJECT_ENDPOINT"].rstrip("/")
    agent = env["AGENT_STRONG_LOOP_NAME"]
    version = env["AGENT_STRONG_LOOP_VERSION"]
    responses = env["AGENT_STRONG_LOOP_RESPONSES_ENDPOINT"]
    token = DefaultAzureCredential().get_token("https://ai.azure.com/.default").token
    h = {"Authorization": f"Bearer {token}"}
    api = {"api-version": "v1"}
    c = httpx.Client(timeout=120)

    # 1. create a session pinned to the deployed version
    r = c.post(f"{project}/agents/{agent}/endpoint/sessions", params=api, headers=h,
               json={"version_indicator": {"kind": "version", "agent_version": version}})
    print("create_session", r.status_code, r.text[:300])
    if r.status_code >= 300:
        # try the alternate discriminator shape before giving up
        r = c.post(f"{project}/agents/{agent}/endpoint/sessions", params=api, headers=h,
                   json={"version_indicator": {"kind": "agent_version", "agent_version": version}})
        print("create_session(alt)", r.status_code, r.text[:300])
        if r.status_code >= 300:
            return 1
    sid = r.json().get("agent_session_id") or r.json().get("id")
    print("session", sid)

    # 2. upload a file relative to $HOME
    body = b"claim_id,settled_amount\n1,100\n2,250\n"
    r = c.put(f"{project}/agents/{agent}/endpoint/sessions/{sid}/files/content",
              params={**api, "path": "intake/probe.csv"}, headers={**h, "Content-Type": "application/octet-stream"},
              content=body)
    print("upload", r.status_code, r.text[:300])

    r = c.get(f"{project}/agents/{agent}/endpoint/sessions/{sid}/files", params={**api, "path": "intake"}, headers=h)
    print("list", r.status_code, r.text[:400])

    # 3. a turn pinned to that session. The agent's steer is irrelevant here; we only need
    # the iteration_start event, which carries session_id, to prove the pin took.
    with c.stream("POST", responses, headers={**h, "Accept": "text/event-stream", "Content-Type": "application/json"},
                  json={"model": agent, "input": "spike", "stream": True, "agent_session_id": sid}) as s:
        print("responses", s.status_code, "x-agent-session-id:", s.headers.get("x-agent-session-id"))
        seen = 0
        for line in s.iter_lines():
            if line.startswith("data: ") and "loop.iteration_start" in line or "session_id" in line:
                print(line[:400]); seen += 1
            if seen >= 2:
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
