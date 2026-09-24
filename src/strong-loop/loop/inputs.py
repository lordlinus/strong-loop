"""inputs.py — what one session brought: a role, a dataset, and the answers to pair them.

Under Foundry hosting `$HOME` is the session's filesystem, and the platform's session
file API can write into it before the agent is ever invoked. That makes a folder the
input channel — not the prompt, not request metadata (which the hosting adapter does not
forward). The client puts files under `intake/`; the loop reads them:

    intake/request.json         {"charter": "<preset>", "data": "<preset>"}   (either optional)
    intake/charter.yaml         an uploaded role (wins over a preset)
    intake/charter.md           the same, in the standard Markdown shape (`loop/charter_md.py`)
    intake/data.csv             an uploaded dataset (wins over a preset)
    intake/accept.json          {"answers": {acc_id: column}, "ratified_by": "...", "iterations": n}
    intake/pairing.json         written HERE once accepted — the session is settled
    intake/charter.signed.yaml  the charter that actually runs

`settle()` is the whole decision. Pairing itself costs no API call; when it leaves a
metric genuinely unresolved — no column shares vocabulary with it — `settle()` calls a
TypeSafe Choice to review the mismatch before the human sees it (`loop.suggest.review`), so a
real-world export with no shared naming convention still gets a menu instead of silence.
That review is automatic, not a separate step a client has to opt into; it costs nothing
when a pairing is already clean or fully resolved lexically. Anything a client sends is
untrusted: preset names are matched against the shipped files, never joined to a path;
answers go through `intake.resolve`, which only takes picks from the list it offered.

A session with nothing under `intake/` runs the environment's defaults exactly as before,
so the CLI and a bare `azd ai agent invoke "go"` are unchanged.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .charter import RoleCharter, load_charter, sign, write_charter
from .derived import Derived, derive
from .intake import IntakeReport, normalize_columns, pair, resolve
from .suggest import review, semantic_screens

SERVICE = pathlib.Path(__file__).resolve().parent.parent
INTAKE = "intake"


def presets() -> dict[str, list[str]]:
    """The roles and datasets shipped with the service, by name.

    Shipped means committed: a charter or export someone keeps locally under a gitignored
    name is theirs to run by path, not a preset the customer page offers. Without git (the
    hosted container) everything present is shipped by definition.
    """
    return {
        "charters": sorted(p.stem for p in (SERVICE / "charters").glob("*.yaml") if not _ignored(p)),
        "data": sorted(p.stem for p in (SERVICE / "data").glob("*.csv") if not _ignored(p)),
    }


def _ignored(path: pathlib.Path) -> bool:
    import subprocess
    try:
        return subprocess.run(["git", "-C", str(SERVICE), "check-ignore", "-q", str(path)],
                              capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def session_home(session_id: str | None) -> pathlib.Path:
    """Where this session's files live.

    Hosted, the platform gives every session its own `$HOME`. Locally there is no such
    isolation, so the same layout is mirrored one directory per session id — which is
    exactly what the local file-API shim in `main.py` writes into.
    """
    if os.environ.get("FOUNDRY_HOSTING_ENVIRONMENT"):
        return pathlib.Path.home()
    root = pathlib.Path(os.environ.get("LOOP_SESSIONS_DIR") or pathlib.Path.home() / ".strong-loop" / "sessions")
    return root / _safe(session_id or "local")


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@-]", "-", str(value))[:96] or "unknown"


@dataclass
class Settled:
    """A pairing the loop may run."""

    charter: RoleCharter
    data: pd.DataFrame
    max_iterations: int
    source: dict[str, str] = field(default_factory=dict)   # {"charter": "upload"|"preset:x"|"default", ...}
    # What this data rules out for this charter (`loop.derived`), both tiers merged.
    derived: Derived | None = None


async def derive_all(charter: RoleCharter, data: pd.DataFrame,
                     column_labels: dict[str, str] | None = None) -> Derived:
    """Both tiers of derived screens: what the data proves (`derive`, always) under what
    the column names suggest (`semantic_screens`, when TypeSafe is configured). The code
    tier wins wherever both have an opinion."""
    code = derive(data, charter)
    try:
        semantic = await semantic_screens(charter, data, code, column_labels)
    except Exception:  # the loop never depends on the external call
        semantic = Derived()
    return code.merge(semantic)


@dataclass
class Outcome:
    status: str                              # "run" | "awaiting" | "refused"
    report: IntakeReport | None = None
    problems: list[str] = field(default_factory=list)
    settled: Settled | None = None
    source: dict[str, str] = field(default_factory=dict)

    def as_event(self) -> dict[str, Any]:
        """The `loop.intake` payload a client renders."""
        out: dict[str, Any] = {"status": self.status, "source": self.source, "problems": self.problems}
        if self.report is not None:
            out["report"] = self.report.model_dump(mode="json")
        return out


class Inputs:
    """The `intake/` folder of one session."""

    def __init__(self, home: pathlib.Path):
        self.dir = pathlib.Path(home) / INTAKE
        self.request_path = self.dir / "request.json"
        self.charter_path = self.dir / "charter.yaml"
        self.charter_md_path = self.dir / "charter.md"
        self.data_path = self.dir / "data.csv"
        self.accept_path = self.dir / "accept.json"
        self.pairing_path = self.dir / "pairing.json"
        self.signed_path = self.dir / "charter.signed.yaml"

    @property
    def engaged(self) -> bool:
        """Did a client speak intake at all? If not, the defaults run untouched."""
        return any(p.exists() for p in (self.request_path, self.charter_path, self.charter_md_path,
                                        self.data_path, self.accept_path))

    def _json(self, path: pathlib.Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        raw = json.loads(path.read_text() or "{}")
        if not isinstance(raw, dict):
            raise ValueError(f"{path.name} must be a JSON object")
        return raw

    def request(self) -> dict[str, Any]:
        return self._json(self.request_path) or {}

    def accept(self) -> dict[str, Any] | None:
        return self._json(self.accept_path)


def _pick(name: Any, kind: str, folder: str, suffix: str) -> pathlib.Path:
    known = presets()[kind]
    if name not in known:
        raise ValueError(f"unknown {folder} preset {name!r}; available: {known}")
    return SERVICE / folder / f"{name}{suffix}"


async def settle(inputs: Inputs, defaults: Settled) -> Outcome:
    """Decide whether this session may run, and on what. See the module docstring."""
    if not inputs.engaged:
        return Outcome("run", settled=defaults, source={"charter": "default", "data": "default"})

    # Already settled in an earlier turn of this session: run what was ratified.
    if inputs.pairing_path.exists() and inputs.signed_path.exists():
        pairing = json.loads(inputs.pairing_path.read_text())
        charter = load_charter(inputs.signed_path)
        data, labels = _load_data(inputs, pairing.get("source", {}).get("data", "default"), defaults)
        settled = Settled(charter, data, _iterations(pairing.get("iterations"), defaults.max_iterations),
                          source=pairing.get("source", {}),
                          derived=await derive_all(charter, data, labels))
        return Outcome("run", settled=settled, source=settled.source)

    source: dict[str, str] = {}
    problems: list[str] = []
    try:
        request = inputs.request()
    except (ValueError, OSError) as exc:
        return Outcome("refused", problems=[f"request.json: {exc}"])
    try:
        charter, source["charter"] = _load_charter(inputs, request, defaults)
    except Exception as exc:  # yaml, pydantic, signature and file errors alike: refuse, say why
        problems.append(f"charter: {_reason(exc)}")
        charter = None
    try:
        data_source = _data_source(inputs, request)
        data, labels = _load_data(inputs, data_source, defaults)
        source["data"] = data_source
    except Exception as exc:
        problems.append(f"data: {_reason(exc)}")
        data, labels = None, {}
    if problems or charter is None or data is None:
        return Outcome("refused", problems=problems, source=source)

    derived = await derive_all(charter, data, labels)
    report = await review(charter, data, pair(charter, data, labels, derived), labels)
    accept = inputs.accept()
    if accept is None:
        return Outcome("awaiting", report=report, source=source)

    answers = accept.get("answers") or {}
    if not isinstance(answers, dict):
        return Outcome("refused", report=report, problems=["accept.json: answers must be an object"], source=source)
    res = resolve(charter, report, {str(k): str(v) for k, v in answers.items()})
    if res.refused:
        return Outcome("refused", report=report, problems=[f"answer {r}" for r in res.refused], source=source)

    final = res.charter
    changed = bool(res.remapped or res.dropped)
    ratified_by = str(accept.get("ratified_by") or "").strip()
    if changed and not ratified_by:
        # Remapping changes what the role is answerable for. `resolve` un-signed it;
        # someone puts their name to the result or it does not run.
        return Outcome("refused", report=report, source=source,
                       problems=["accept.json: ratified_by is required to sign this pairing"])
    if ratified_by:
        final = sign(final, ratified_by)

    # Remapping changes which columns are metrics, so what leaks them changes too.
    final_derived = await derive_all(final, data, labels) if changed else derived
    final_report = pair(final, data, labels, final_derived)
    if final_report.problems:
        return Outcome("refused", report=final_report, problems=list(final_report.problems), source=source)

    iterations = _iterations(accept.get("iterations"), defaults.max_iterations)
    write_charter(final, inputs.signed_path)
    inputs.pairing_path.write_text(json.dumps({
        "accepted_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "remapped": res.remapped,
        "dropped": res.dropped,
        "ratified_by": final.ratified_by,
        "iterations": iterations,
        "report": final_report.model_dump(mode="json"),
    }, indent=2, default=str))
    settled = Settled(final, data, iterations, source=source, derived=final_derived)
    return Outcome("run", report=final_report, settled=settled, source=source)


def _load_charter(inputs: Inputs, request: dict[str, Any], defaults: Settled) -> tuple[RoleCharter, str]:
    uploaded = [p for p in (inputs.charter_path, inputs.charter_md_path) if p.exists()]
    if len(uploaded) > 1:
        # Which one the person meant is not ours to guess; the one that runs is the one signed.
        raise ValueError("both intake/charter.yaml and intake/charter.md were uploaded; upload one")
    if uploaded:
        return load_charter(uploaded[0]), "upload"
    if request.get("charter"):
        return load_charter(_pick(request["charter"], "charters", "charters", ".yaml")), f"preset:{request['charter']}"
    return defaults.charter, "default"


def _data_source(inputs: Inputs, request: dict[str, Any]) -> str:
    if inputs.data_path.exists():
        return "upload"
    if request.get("data"):
        _pick(request["data"], "data", "data", ".csv")   # validates the name
        return f"preset:{request['data']}"
    return "default"


def _load_data(inputs: Inputs, source: str, defaults: Settled) -> tuple[pd.DataFrame, dict[str, str]]:
    if source == "upload":
        if not inputs.data_path.exists():
            raise ValueError("uploaded data.csv is missing")
        data = pd.read_csv(inputs.data_path)
        if data.empty or not len(data.columns):
            raise ValueError("data.csv has no rows or no columns")
        return normalize_columns(data)
    if source.startswith("preset:"):
        return normalize_columns(pd.read_csv(_pick(source.split(":", 1)[1], "data", "data", ".csv")))
    return defaults.data, {}


def _reason(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return text[:400] if text else type(exc).__name__


def _iterations(requested: Any, cap: int) -> int:
    try:
        n = int(requested)
    except (TypeError, ValueError):
        return cap
    return max(1, min(n, cap))
