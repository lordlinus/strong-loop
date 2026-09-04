"""The explainer page is generated from a recorded run. If the engine changes shape, the
generator must still build — and the committed run must still be readable."""

from __future__ import annotations

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
