"""The explainer page is generated from a recorded run. If the engine changes shape, the
generator must still build — and the committed run must still be readable."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_loop_doc_builds_from_the_recorded_run(tmp_path):
    runs = sorted((ROOT / "docs" / "runs").glob("claims_analyst-*"))
    assert runs, "docs/runs must hold the recorded run the page is built from"
    out = tmp_path / "loop.html"
    subprocess.run(
        [sys.executable, "-W", "ignore", str(ROOT / "tools" / "make_loop_doc.py"), str(runs[-1]), "--out", str(out)],
        check=True, capture_output=True, text=True,
    )
    page = out.read_text()
    for needle in ("Code disposes", "test_hypothesis", "SUPPORTED", "authorise_action", "report.json", "iteration 1 of"):
        assert needle in page, needle
    for route in ("/showcase.html", "/loop.html", "/live.html"):
        assert route in page, route


def test_showcase_builds_from_the_recorded_run(tmp_path):
    runs = sorted((ROOT / "docs" / "runs").glob("claims_analyst-*"))
    out = tmp_path / "showcase.html"
    subprocess.run(
        [sys.executable, "-W", "ignore", str(ROOT / "tools" / "make_showcase.py"), str(runs[-1]), "--out", str(out)],
        check=True, capture_output=True, text=True,
    )
    page = out.read_text()
    assert "/*RUN_JSON*/" not in page, "the run payload was not substituted"
    for needle in ("Code disposes", "LEDGER", '"trace"', '"report"', "test_hypothesis", "SUPPORTED", "authorise", "report.json"):
        assert needle in page, needle
    for route in ("/showcase.html", "/loop.html", "/live.html"):
        assert route in page, route


def test_presets_manifest_matches_the_shipped_service():
    """docs/presets.json is what the customer page offers. It must be regenerated whenever a
    charter or dataset ships: `python -m loop presets --out docs/presets.json`."""
    sys.path.insert(0, str(ROOT / "src" / "strong-loop"))
    from loop.cli import presets_manifest

    committed = json.loads((ROOT / "docs" / "presets.json").read_text())
    fresh = presets_manifest()
    assert committed == fresh, "run: python -m loop presets --out docs/presets.json"
    assert len(fresh["pairings"]) >= 4, "every shipped charter must pair with a shipped dataset"
    for c in fresh["charters"]:
        assert any(p["charter"] == c["name"] for p in fresh["pairings"]), c["name"]


def test_static_web_app_customer_journey_is_complete():
    index = (ROOT / "docs" / "index.html").read_text()
    live = (ROOT / "docs" / "live.html").read_text()
    config = (ROOT / "docs" / "staticwebapp.config.json").read_text()

    for route in ("/showcase.html", "/loop.html", "/charters.html", "/live.html"):
        assert route in index, route
    # the page speaks to the site's API for all three verbs, and renders the gate's answer
    for needle in ('"/api/stream-ticket"', '"/api/sessions"', "/files?path=", "upload_ticket", "loop.intake", "intake/accept.json"):
        assert needle in live, needle
    # option lists come from the manifest, never from hand-typed HTML
    assert 'fetch("/presets.json"' in live
    assert "<option" not in live.split('id="charter-preset"')[1].split("</select>")[0]
    assert '"route": "/live.html"' in config
    assert '"route": "/charters.html"' in config
    assert '"route": "/api/*"' in config
    assert '"authenticated"' in config


def test_charter_editor_mirrors_the_schema():
    """The browser editor and loop/charter.py must agree on the closed vocabularies; a new
    autonomy level or solution-smell added to one and not the other would be caught here."""
    from loop.charter import _SOLUTION_SMELLS
    from loop.types import AutonomyLevel

    editor = (ROOT / "docs" / "charters.html").read_text()
    for level in AutonomyLevel:
        assert f'"{level.value}"' in editor, level.value
    for smell in _SOLUTION_SMELLS:
        assert f'"{smell}"' in editor, smell
    assert (ROOT / "docs" / "vendor" / "js-yaml.min.js").exists()
    assert 'src="/vendor/js-yaml.min.js"' in editor
