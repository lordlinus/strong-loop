"""main.py — the Foundry hosted-agent entry point. Run locally exactly as the platform
runs it: `python main.py`, or `azd ai agent run` from the repository root.

One NEW conversation = one run. The message a person sends is the optional human steer,
and it is what every iteration starts from — Ralph's PROMPT.md. The charter and dataset
come from the environment (`LOOP_CHARTER`, `LOOP_DATA`, relative to this directory), the
budget from `LOOP_MAX_ITERATIONS`.

History is switched off on purpose (`history_source="agent"`, below). By default the
hosting layer prepends a conversation's transcript to every request, so a second turn in
the same conversation would replay the whole first run — nudges, tool calls and all — as
the "original input" of every fresh-context iteration; the model rejected exactly that
with a 400 in testing. The ledger is this agent's memory, not the transcript.
"""

from __future__ import annotations

import logging
import os
import pathlib

# The agentserver's OpenTelemetry distro probes the Azure instance-metadata endpoint at
# start-up. In a hosted container that is right; on a laptop it is a wall of
# ConnectTimeout tracebacks. The platform sets this variable, so its absence means local.
if not os.environ.get("FOUNDRY_HOSTING_ENVIRONMENT"):
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")

import pandas as pd
from agent_framework_foundry_hosting import ResponsesHostServer

from loop.charter import load_charter
from loop.inputs import session_home
from loop.models import describe, load_env
from loop.runner import DEFAULT_MAX_ITERATIONS, build_agent, default_runs_dir

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

HERE = pathlib.Path(__file__).resolve().parent
load_env()

# `azd ai agent run` injects every variable named in azure.yaml, empty when unset — so
# "unset" and "" must mean the same thing here.
charter_path = HERE / (os.environ.get("LOOP_CHARTER") or "charters/claims_analyst.yaml")
data_path = HERE / (os.environ.get("LOOP_DATA") or "data/claims.csv")
max_iterations = int(os.environ.get("LOOP_MAX_ITERATIONS") or DEFAULT_MAX_ITERATIONS)

charter = load_charter(charter_path)
data = pd.read_csv(data_path)
missing = [a.metric for a in charter.accountabilities if a.metric not in data.columns]
if missing:
    raise SystemExit(f"{charter_path.name}: accountability metric(s) not in {data_path.name}: {missing}")

log.info("model routing: %s", describe())
log.info("role %s over %s (%d rows), %d iterations, runs under %s",
         charter.role, data_path.name, len(data), max_iterations, default_runs_dir())

agent, _scope = build_agent(charter, data, max_iterations=max_iterations, echo=True)

HOSTED = bool(os.environ.get("FOUNDRY_HOSTING_ENVIRONMENT"))


def _local_session_routes():
    """A stand-in for the platform's session-file API, local only.

    Hosted, a client creates a session and PUTs files into it through the Foundry project
    endpoint; they land in the container's `$HOME`. Locally nothing does that, so the same
    two verbs are served here and write to `loop.inputs.session_home(<id>)`. Same paths,
    same bodies, so `docs/live.html` talks to both without knowing which it has.
    """
    import re
    import uuid

    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    # Only the files the loop reads; a client cannot write anywhere else.
    ALLOWED = {"intake/request.json", "intake/charter.yaml", "intake/charter.md", "intake/data.csv", "intake/accept.json"}
    MAX_BYTES = 50 * 1024 * 1024

    async def create(request: Request) -> Response:
        sid = uuid.uuid4().hex
        session_home(sid).mkdir(parents=True, exist_ok=True)
        return JSONResponse({"agent_session_id": sid, "status": "active"}, status_code=201)

    async def put_file(request: Request) -> Response:
        sid = request.path_params["sid"]
        if not re.fullmatch(r"[0-9a-f]{32}", sid):
            return JSONResponse({"error": "unknown session"}, status_code=404)
        path = request.query_params.get("path", "")
        if path not in ALLOWED:
            return JSONResponse({"error": f"path must be one of {sorted(ALLOWED)}"}, status_code=400)
        body = await request.body()
        if len(body) > MAX_BYTES:
            return JSONResponse({"error": "too large"}, status_code=413)
        target = session_home(sid) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return JSONResponse({"path": path, "bytes_written": len(body), "success": True}, status_code=201)

    async def list_files(request: Request) -> Response:
        sid = request.path_params["sid"]
        home = session_home(sid)
        sub = request.query_params.get("path", "").strip("/")
        folder = (home / sub) if sub else home
        if not re.fullmatch(r"[0-9a-f]{32}", sid) or not folder.resolve().is_relative_to(home.resolve()):
            return JSONResponse({"error": "unknown session"}, status_code=404)
        entries = [{"name": p.name, "size": p.stat().st_size, "is_directory": p.is_dir()}
                   for p in sorted(folder.glob("*"))] if folder.exists() else []
        return JSONResponse({"path": sub, "entries": entries, "has_more": False})

    return [
        Route("/sessions", create, methods=["POST"]),
        Route("/sessions/{sid}/files/content", put_file, methods=["PUT"]),
        Route("/sessions/{sid}/files", list_files, methods=["GET"]),
    ]


# Only the current request reaches the agent; no transcript replay (see module docstring).
app = ResponsesHostServer(agent, history_source="agent", routes=None if HOSTED else _local_session_routes())

if not HOSTED:
    # Locally, let a browser page (docs/live.html, any origin) read the stream. Hosted
    # traffic comes through the platform gateway with its own auth, so this stays local.
    from starlette.middleware.cors import CORSMiddleware

    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

if __name__ == "__main__":
    app.run()
