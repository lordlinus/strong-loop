"""The standard Markdown charter format: same charter as the YAML, refusals with a line.

`charter_md_cases.json` is shared with the charter page's JavaScript parser; both must
pass every case, so the instant check in the browser and the check that signs agree.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from loop import charter_md
from loop.charter import RoleCharter, load_charter, sign, write_charter
from loop.charter_md import CharterFormatError

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHARTERS = ROOT / "src" / "strong-loop" / "charters"
CASES = json.loads((ROOT / "tests" / "charter_md_cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shared_cases(case):
    if "error" in case:
        with pytest.raises(CharterFormatError) as exc:
            charter_md.parse(case["md"])
        assert case["error"] in str(exc.value)
    else:
        assert charter_md.parse(case["md"]) == case["raw"]


def _text_normalised(charter: RoleCharter) -> dict:
    """YAML's folded `>` scalars keep a trailing newline and line breaks; Markdown text is
    one normalised line. That whitespace is the only permitted difference."""
    def walk(x):
        if isinstance(x, str):
            return " ".join(x.split())
        if isinstance(x, list):
            return [walk(i) for i in x]
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        return x
    return walk(charter.model_dump(mode="json", exclude={"status", "ratified_by", "ratified_at", "content_hash"}))


@pytest.mark.parametrize("path", sorted(CHARTERS.glob("*.yaml")), ids=lambda p: p.stem)
def test_every_shipped_charter_round_trips(path, tmp_path):
    charter = load_charter(path)
    md = tmp_path / f"{path.stem}.md"
    write_charter(charter, md)
    again = load_charter(md)
    assert _text_normalised(again) == _text_normalised(charter)
    # Rendering what was parsed changes nothing, so a Markdown charter can be signed and
    # re-read without its hash moving.
    assert charter_md.render(again) == md.read_text()
    assert again.fingerprint() == load_charter(md).fingerprint()


def test_a_signed_markdown_charter_verifies_and_catches_tampering(tmp_path):
    md = tmp_path / "claims.md"
    yaml_loaded = sign(load_charter(CHARTERS / "claims_analyst.yaml"), "alice")
    with pytest.raises(ValueError, match="does not survive conversion"):
        write_charter(yaml_loaded, md)   # the hash covers whitespace Markdown drops
    assert not md.exists()
    write_charter(sign(charter_md.through_markdown(load_charter(CHARTERS / "claims_analyst.yaml")), "alice"), md)
    loaded = load_charter(md)
    assert loaded.status == "SIGNED" and loaded.ratified_by == "alice"
    md.write_text(md.read_text().replace("cap: 200 claims per run", "cap: 20000 claims per run"))
    with pytest.raises(ValueError, match="modified since it was ratified"):
        load_charter(md)


def test_cli_signs_and_renders_markdown(tmp_path, capsys):
    from loop.cli import main

    md = tmp_path / "claims.md"
    assert main(["sign", str(CHARTERS / "claims_analyst.yaml"), "--by", "bob", "--out", str(md)]) == 0
    assert load_charter(md).ratified_by == "bob"
    capsys.readouterr()
    assert main(["render", str(md)]) == 0                       # md -> yaml, signature intact
    assert "ratified_by: bob" in capsys.readouterr().out


def test_a_markdown_charter_gets_the_yaml_shape_checks(tmp_path):
    """The anti-solution screen reads the raw mapping; a Markdown charter cannot slip a
    hard-coded workflow past it — there is no heading for one, and it says so."""
    md = tmp_path / "c.md"
    md.write_text("---\nrole: r\n---\n## Questions\n")
    with pytest.raises(CharterFormatError, match="unknown section"):
        load_charter(md)


def test_markdown_child_extends_a_yaml_base(tmp_path):
    (tmp_path / "base.yaml").write_text(
        "role: base\naccountabilities:\n  - {id: a, statement: s, metric: m, direction: increase}\n")
    child = tmp_path / "child.md"
    child.write_text("---\nrole: child\nextends: base\n---\n## Constraints\n- never use: `x`\n")
    c = load_charter(child)
    assert c.role == "child" and [a.id for a in c.accountabilities] == ["a"]
    assert c.constraints.forbidden_features == ["x"]


def test_intake_refuses_two_uploaded_charters(tmp_path):
    from loop.inputs import Inputs, _load_charter

    inputs = Inputs(tmp_path)
    inputs.dir.mkdir(parents=True)
    inputs.charter_path.write_text((CHARTERS / "claims_analyst.yaml").read_text())
    write_charter(load_charter(CHARTERS / "claims_analyst.yaml"), inputs.charter_md_path)
    with pytest.raises(ValueError, match="upload one"):
        _load_charter(inputs, {}, None)
    inputs.charter_path.unlink()
    charter, source = _load_charter(inputs, {}, None)
    assert source == "upload" and charter.role == "claims_analyst"
