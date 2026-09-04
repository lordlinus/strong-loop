"""Rule 1, enforced rather than documented: only `loop/gates.py` may construct an Evidence.

Rule 2's counterpart: only `loop/tools.py` may construct a Finding, Decision or Action,
because those are the three doors through `authorise_action` and `confidence_from`. A new
module that mints any of them is exactly the change this test exists to refuse.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "strong-loop"
ALLOWED = {
    "Evidence": {"loop/gates.py"},
    "Finding": {"loop/tools.py"},
    "Decision": {"loop/tools.py"},
    "Action": {"loop/tools.py"},
}


def _constructions(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in ALLOWED:
                names.add(name)
    return names


def test_certified_records_have_exactly_one_door():
    offenders = []
    for path in SRC.rglob("*.py"):
        if ".venv" in path.parts:
            continue
        rel = str(path.relative_to(SRC))
        for name in _constructions(path):
            if rel not in ALLOWED[name]:
                offenders.append(f"{rel} constructs {name}")
    assert not offenders, "\n".join(offenders)
