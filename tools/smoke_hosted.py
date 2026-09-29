"""Smoke-test a strong-loop agent without running the loop: no reasoning-model tokens (Jev may screen column names).

The agent's intake gate answers a session that has a charter and data but no acceptance
with the pairing report alone, before any model runs (`runner.IntakeGate`). That makes it
the one request that proves the deployment end to end — session API, file upload into the
session's $HOME, the Responses stream, the charter and data loading in the container, and
the brief being built — without spending an analysis. `azd ai agent invoke "…"` cannot do
this: every turn is a run, so a bare invoke starts the loop on the default pairing.

    python tools/smoke_hosted.py                               # the azd env's deployed agent
    python tools/smoke_hosted.py --local                       # `python main.py` on :8088
    python tools/smoke_hosted.py --charter hr_analyst --data hr_attrition

Exit 0 when the gate answered "awaiting" with a clean report and nothing else was streamed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

MARKER = "/endpoint/protocols/openai/responses"


def azd_value(name: str) -> str:
    out = subprocess.run(["azd", "env", "get-value", name], capture_output=True, text=True)
    value = out.stdout.strip()
    if out.returncode != 0 or not value:
        raise SystemExit(f"azd env has no {name}; pass --responses-url")
    return value


def endpoints(args: argparse.Namespace) -> tuple[str, str, dict[str, str], dict[str, str]]:
    """(responses url, sessions url, query params, headers) for local or hosted."""
    if args.local:
        return "http://localhost:8088/responses", "http://localhost:8088/sessions", {}, {}
    responses = args.responses_url or os.environ.get("AGENT_STRONG_LOOP_RESPONSES_ENDPOINT") \
        or azd_value("AGENT_STRONG_LOOP_RESPONSES_ENDPOINT")
    parts = urlsplit(responses)
    if not parts.path.endswith(MARKER):
        raise SystemExit(f"not an agent Responses endpoint: {responses}")
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    sessions = base[: -len(MARKER)] + "/endpoint/sessions"
    query = dict(p.split("=", 1) for p in parts.query.split("&") if "=" in p) or {"api-version": "v1"}
    from azure.identity import DefaultAzureCredential
    token = DefaultAzureCredential().get_token("https://ai.azure.com/.default").token
    return base, sessions, query, {"Authorization": f"Bearer {token}"}


def sse_items(response: httpx.Response):
    """Completed output items from a Responses SSE stream."""
    event = None
    for line in response.iter_lines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event in ("response.output_item.done", "error"):
            data = json.loads(line[6:])
            yield event, data.get("item") if event != "error" else data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--local", action="store_true", help="the agent on localhost:8088")
    ap.add_argument("--responses-url", default=None, help="default: the azd env's agent endpoint")
    ap.add_argument("--charter", default="claims_analyst", help="a shipped charter preset")
    ap.add_argument("--data", default="claims", help="a shipped dataset preset")
    ap.add_argument("--attempts", type=int, default=6, help="retries while a new session warms up")
    args = ap.parse_args()

    responses, sessions, query, headers = endpoints(args)
    client = httpx.Client(timeout=httpx.Timeout(300, connect=30))
    started = time.monotonic()

    r = client.post(sessions, params=query, headers=headers, json={})
    if r.status_code >= 300:
        print(f"FAIL create session: HTTP {r.status_code} {r.text[:300]}")
        return 1
    sid = r.json()["agent_session_id"]
    print(f"session {sid}")

    body = json.dumps({"charter": args.charter, "data": args.data}).encode()
    r = client.put(f"{sessions}/{sid}/files/content", params={**query, "path": "intake/request.json"},
                   headers={**headers, "Content-Type": "application/octet-stream"}, content=body)
    if r.status_code >= 300:
        print(f"FAIL upload intake/request.json: HTTP {r.status_code} {r.text[:300]}")
        return 1

    request = {"model": "strong-loop", "input": "check the pairing", "stream": True, "agent_session_id": sid}
    items: list[tuple[str, dict]] = []
    for attempt in range(1, args.attempts + 1):
        with client.stream("POST", responses, params=query, headers={**headers, "Accept": "text/event-stream"},
                           json=request) as resp:
            if resp.status_code == 424 and attempt < args.attempts:   # session_not_ready: still starting
                print(f"session not ready (attempt {attempt}); waiting")
                time.sleep(15)
                continue
            if resp.status_code >= 300:
                print(f"FAIL invoke: HTTP {resp.status_code} {resp.read().decode()[:300]}")
                return 1
            items = list(sse_items(resp))
        break

    calls = {i["call_id"]: i["name"] for e, i in items if e != "error" and i.get("type") == "function_call"}
    outputs = {i["call_id"]: i["output"] for e, i in items if e != "error" and i.get("type") == "function_call_output"}
    errors = [i for e, i in items if e == "error"]
    other = [i.get("type") for e, i in items if e != "error" and i.get("type") not in ("function_call", "function_call_output")]
    names = list(calls.values())
    intake = next((json.loads(outputs[c]) for c, n in calls.items() if n == "loop.intake" and c in outputs), None)

    problems = []
    if errors:
        problems.append(f"stream error: {errors[0]}")
    if names != ["loop.intake"] or other:
        problems.append(f"expected only loop.intake, got calls {names} and items {other}: the model may have run")
    if intake is None:
        problems.append("no loop.intake output")
    else:
        report = intake.get("report") or {}
        if intake.get("status") != "awaiting":
            problems.append(f"intake status {intake.get('status')!r}: {intake.get('problems')}")
        if report.get("problems"):
            problems.append(f"pairing problems: {report['problems']}")
        brief = report.get("brief") or {}
        if brief:
            open_ = [o["goal"] for o in brief.get("objectives", []) if o.get("status") == "open"]
            print(f"brief: {brief.get('role')} — {len(open_)} open objective(s): {', '.join(open_)}; "
                  f"{len(brief.get('plays', []))} play(s)")
        else:
            print("note: no brief in the intake report (an agent version older than the brief)")
        print(f"intake: {intake.get('status')} · {report.get('role')} over {report.get('rows')} rows · "
              f"{len(report.get('clarifications', []))} blocking, {len(report.get('advisories', []))} advisory question(s)")

    print(f"{time.monotonic() - started:.1f}s")
    if problems:
        print("FAIL " + "; ".join(problems))
        return 1
    print("OK — the agent answered from intake; the reasoning model did not run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
