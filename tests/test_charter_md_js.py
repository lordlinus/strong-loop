"""The charter page's JavaScript parser must agree with loop/charter_md.py on every case.

The page gives instant feedback from `docs/charter-md.js`; the server parses again before
anything is signed. If the two disagree, a person is told "fine" by one and refused by the
other — so both run `tests/charter_md_cases.json`, and this runs the JS copy under node.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from loop.charter import RoleCharter

ROOT = pathlib.Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

RUNNER = r"""
const md = require(process.argv[1]);
const cases = require(process.argv[2]).cases;
const canon = (x) => JSON.stringify(x, (k, v) => v && typeof v === "object" && !Array.isArray(v) ? Object.fromEntries(Object.entries(v).sort()) : v);
const out = {failures: [], template: null, template_problems: null};
for (const c of cases) {
  try {
    const raw = md.parse(c.md);
    if ("error" in c) out.failures.push(`${c.name}: parsed, expected error ${c.error}`);
    else if (canon(raw) !== canon(c.raw)) out.failures.push(`${c.name}: ${canon(raw)} != ${canon(c.raw)}`);
  } catch (e) {
    if (!("error" in c)) out.failures.push(`${c.name}: threw ${e.message}`);
    else if (!e.message.includes(c.error)) out.failures.push(`${c.name}: "${e.message}" lacks "${c.error}"`);
  }
}
out.template = md.parse(md.TEMPLATE);
out.template_problems = md.check(out.template);
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def result():
    if not NODE:
        pytest.skip("node is not installed")
    proc = subprocess.run([NODE, "-e", RUNNER, str(ROOT / "docs" / "charter-md.js"),
                           str(ROOT / "tests" / "charter_md_cases.json")],
                          capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def test_js_parser_passes_every_shared_case(result):
    assert result["failures"] == []


def test_the_page_template_is_a_valid_charter(result):
    from loop import charter_md

    assert result["template_problems"] == []
    charter = RoleCharter.model_validate(result["template"])
    assert charter.accountabilities and charter.decision_rights
    # and the server reads the template exactly as the page does
    template = json.loads(subprocess.run(
        [NODE, "-e", "process.stdout.write(JSON.stringify(require(process.argv[1]).TEMPLATE))",
         str(ROOT / "docs" / "charter-md.js")], capture_output=True, text=True, check=True).stdout)
    assert charter_md.parse(template) == result["template"]


def test_js_check_mirrors_the_model_vocabularies():
    from loop.types import AutonomyLevel

    js = (ROOT / "docs" / "charter-md.js").read_text()
    for level in AutonomyLevel:
        assert f'"{level.value}"' in js, level.value
    for d in ("increase", "decrease", "stabilise"):
        assert f'"{d}"' in js
    for c in ("benjamini_hochberg", "bonferroni", "none"):
        assert f'"{c}"' in js
