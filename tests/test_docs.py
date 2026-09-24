"""The site: three pages, one wheel. "How it works" is generated from a recorded run; if the
engine changes shape, the generator must still build and the committed run must still be
readable. Every page script must at least parse — a syntax error is a blank page."""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _latest_run() -> pathlib.Path:
    runs = sorted((ROOT / "docs" / "runs").glob("claims_analyst-*"))
    assert runs, "docs/runs must hold the recorded run page 1 is built from"
    return runs[-1]


def test_loop_doc_builds_from_the_recorded_run(tmp_path):
    """tools/make_loop_doc.py is no longer a page on the site, but make_showcase.py reads the
    run through it, so it must keep working on the committed run."""
    out = tmp_path / "loop.html"
    subprocess.run(
        [sys.executable, "-W", "ignore", str(ROOT / "tools" / "make_loop_doc.py"), str(_latest_run()), "--out", str(out)],
        check=True, capture_output=True, text=True,
    )
    page = out.read_text()
    for needle in ("Code disposes", "test_hypothesis", "SUPPORTED", "authorise_action", "report.json", "iteration 1 of"):
        assert needle in page, needle


def test_how_it_works_builds_from_the_recorded_run(tmp_path):
    out = tmp_path / "index.html"
    subprocess.run(
        [sys.executable, "-W", "ignore", str(ROOT / "tools" / "make_showcase.py"), str(_latest_run()), "--out", str(out)],
        check=True, capture_output=True, text=True,
    )
    page = out.read_text()
    assert "/*RUN_JSON*/" not in page, "the run payload was not substituted"
    for needle in ("Code disposes", '"trace"', '"report"', "SUPPORTED", "authorise", '"charter_md"', "## Accountabilities"):
        assert needle in page, needle
    for needle in ('src="/wheel.js"', 'href="/wheel.css"', 'href="/charters.html"', 'href="/live.html"', "Wheel.mount", "Wheel.narrate"):
        assert needle in page, needle


def test_committed_how_it_works_is_built_from_the_committed_run():
    """docs/index.html is committed, so it must be rebuilt when the run changes:
    `python tools/make_showcase.py docs/runs/<run>`."""
    page = (ROOT / "docs" / "index.html").read_text()
    assert f'"run_dir": "docs/runs/{_latest_run().name}"' in page


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


def test_static_web_app_serves_three_pages_and_one_wheel():
    config = json.loads((ROOT / "docs" / "staticwebapp.config.json").read_text())
    routes = {r["route"]: r for r in config["routes"]}
    for retired in ("/showcase.html", "/loop.html"):
        assert routes[retired]["redirect"] == "/" and routes[retired]["statusCode"] == 301
        assert not (ROOT / "docs" / retired.lstrip("/")).exists()
    for private in ("/live.html", "/charters.html", "/api/*"):
        assert routes[private]["allowedRoles"] == ["authenticated"], private

    pages = {name: (ROOT / "docs" / name).read_text() for name in ("index.html", "charters.html", "live.html")}
    for name, page in pages.items():
        for step in ('href="/"', 'href="/charters.html"', 'href="/live.html"'):
            assert step in page, (name, step)
        assert "aria-current" in page, name

    live = pages["live.html"]
    # the page speaks to the site's API for all three verbs, and renders the gate's answer
    for needle in ('"/api/stream-ticket"', '"/api/sessions"', "/files?path=", "upload_ticket", "loop.intake",
                   "intake/accept.json", "intake/charter.md", 'src="/wheel.js"', "Wheel.adapter()", "onWheelItem(it)"):
        assert needle in live, needle
    # option lists come from the manifest, never from hand-typed HTML
    assert 'fetch("/presets.json"' in live
    assert "<option" not in live.split('id="charter-preset"')[1].split("</select>")[0]


def test_charter_page_hands_markdown_to_the_run_page():
    charters = (ROOT / "docs" / "charters.html").read_text()
    live = (ROOT / "docs" / "live.html").read_text()
    assert 'src="/charter-md.js"' in charters and "CharterMd.parse" in charters and "CharterMd.check" in charters
    # one browser store, written by the charter page, read by the run page
    assert '"strong-loop.charters"' in charters and '"strong-loop.charters"' in live
    assert "/live.html?charter=" in charters
    manifest = json.loads((ROOT / "docs" / "presets.json").read_text())
    assert all(c["markdown"].startswith("---\nrole: ") for c in manifest["charters"])


NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="node is not installed")
@pytest.mark.parametrize("name", ["index.html", "charters.html", "live.html", "wheel.js", "charter-md.js"])
def test_page_scripts_parse(name, tmp_path):
    text = (ROOT / "docs" / name).read_text()
    scripts = [text] if name.endswith(".js") else [
        s for s in re.findall(r"<script(?![^>]*\bsrc=)(?![^>]*application/json)[^>]*>(.*?)</script>", text, re.S) if s.strip()]
    assert scripts, name
    for i, body in enumerate(scripts):
        f = tmp_path / f"{i}.js"
        f.write_text(body)
        proc = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert proc.returncode == 0, f"{name} script {i}: {proc.stderr}"


@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_the_wheel_narrates_every_event_of_the_recorded_run():
    """The caption is what makes the page self-explanatory; an event it cannot narrate is a
    silent step in the replay."""
    trace = [json.loads(l) for l in (_latest_run() / "trace.log").read_text().splitlines() if l.strip()]
    script = """
const W = require(process.argv[1]); const trace = JSON.parse(require("fs").readFileSync(0, "utf8"));
const out = trace.map(raw => { const ev = W.fromTrace(raw.event === "report" ? {...raw, report: {findings: [], actions: []}} : raw); const n = W.narrate(ev); return n && n.text ? null : raw.event + ":" + (raw.tool || ""); });
process.stdout.write(JSON.stringify(out.filter(Boolean)));"""
    proc = subprocess.run([NODE, "-e", script, str(ROOT / "docs" / "wheel.js")], input=json.dumps(trace),
                          capture_output=True, text=True, check=True)
    assert json.loads(proc.stdout) == []
