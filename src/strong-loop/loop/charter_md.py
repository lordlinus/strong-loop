"""charter_md.py — a role charter written as a Markdown document, in one standard shape.

YAML is the source for shipped charters; this is the format a person writes one in, and
the form every charter is shown in. Both become the same `RoleCharter` through the same
`load_charter` path, so the fingerprint, the signature check, `extends` and every screen
are unchanged. Parsed by fixed rules — never by a model: a charter is signed authority,
and whatever reads it decides what a role may do.

    ---
    role: retention_manager
    version: 1
    ---
    # Retention manager
    Owns subscriber retention.                      <- description (the paragraph here)

    ## Glossary
    - **churn**: the subscriber cancelled service

    ## Accountabilities
    ### reduce_churn — Reduce the share of subscribers who cancel
    - metric: `Churn`
    - direction: decrease
    - horizon: 90 days
    - priority: 1
    - lead: month-to-month contracts                 <- repeatable

    ## Decision rights
    ### retention_offer — Send a defined group a retention offer
    - autonomy: L1_recommend
    - reversible: yes
    - cap: 500 subscribers per run
    - observe: `Churn` after 60 days
    - approval: head_of_retention

    ## Evidence standard
    - min sample: 100
    - max p: 0.01
    ### Override: retention_offer                    <- a stricter bar for one action
    - requires holdout: yes

    ## Constraints
    - never use: `assessor_name`
    - leaks the outcome: `cancel_reason`
    - sensitive columns: blocked

Strict on purpose: an unknown heading or key is an error with its line number, not
ignored — a constraint that is silently dropped is a permission nobody granted. Lines
starting with `>` are commentary and ignored, so a template can annotate itself. The
JavaScript copy on the charter page must pass the same `tests/charter_md_cases.json`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .charter import RoleCharter


class CharterFormatError(ValueError):
    """A Markdown charter that does not follow the standard shape."""

    def __init__(self, line: int, message: str):
        super().__init__(f"line {line}: {message}")
        self.line = line


_FRONT = ("role", "version", "extends", "status", "ratified_by", "ratified_at", "content_hash")
_SECTIONS = ("glossary", "accountabilities", "decision rights", "evidence standard", "constraints")
_HEADING = re.compile(r"^(\S+)(?:\s+[—–-]{1,2}\s+(.*))?$")
_CAP = re.compile(r"^(\d+)(?:\s+(.+?))?\s+per\s+run$", re.I)
_OBSERVE = re.compile(r"^(?:`?([^`\s]+)`?)?\s*(?:after\s+(\d+)\s+days?)?$", re.I)
_DAYS = re.compile(r"^(\d+)(?:\s*days?)?$", re.I)
_BOLD_TERM = re.compile(r"^\*\*(.+?)\*\*\s*:\s*(.*)$")

_ACC_KEYS = ("metric", "direction", "horizon", "priority", "lead")
_RIGHT_KEYS = ("autonomy", "reversible", "cap", "max value at risk", "observe", "approval")
_STANDARD_KEYS = {
    "min sample": ("min_sample_size", "int"), "max p": ("max_p_value", "float"),
    "min effect": ("min_effect_size", "float"), "correction": ("multiple_testing_correction", "str"),
    "requires causal design": ("requires_causal_design", "bool"),
    "requires holdout": ("requires_holdout", "bool"),
    "requires confound control": ("requires_confound_control", "bool"),
}
_CONSTRAINT_KEYS = {
    "never use": "forbidden_features", "leaks the outcome": "leakage_features",
    "only use": "allowed_features", "sensitive columns": "block_sensitive_columns", "notes": "notes",
}


def _text(value: str) -> str:
    return " ".join(value.split())


def _ident(value: str) -> str:
    return value.strip().strip("`").strip()


def _idents(value: str) -> list[str]:
    if value.strip().lower() == "none":
        return []
    return [i for i in (_ident(v) for v in value.split(",")) if i]


def _bool(n: int, key: str, value: str) -> bool:
    v = value.strip().lower()
    if v in ("yes", "true", "y"):
        return True
    if v in ("no", "false", "n"):
        return False
    raise CharterFormatError(n, f"`{key}` must be yes or no, not {value!r}")


def _number(n: int, key: str, value: str, kind: str) -> int | float:
    try:
        return int(value) if kind == "int" else float(value)
    except ValueError:
        raise CharterFormatError(n, f"`{key}` must be a {'whole number' if kind == 'int' else 'number'}, "
                                    f"not {value!r}") from None


def _typed(n: int, key: str, value: str, kind: str) -> Any:
    if kind == "bool":
        return _bool(n, key, value)
    if kind in ("int", "float"):
        return _number(n, key, value, kind)
    return value.strip()


def _allowed(keys) -> str:
    return ", ".join(f"`{k}`" for k in keys)


def parse(text: str) -> dict[str, Any]:
    """Markdown -> the raw charter mapping `load_charter` validates (only keys present)."""
    lines = text.splitlines()
    raw: dict[str, Any] = {}
    n = 0
    while n < len(lines) and not lines[n].strip():
        n += 1
    if n >= len(lines) or lines[n].strip() != "---":
        raise CharterFormatError(n + 1, "a charter starts with front matter: a `---` line, "
                                        "`role: <name>`, then another `---`")
    n += 1
    while True:
        if n >= len(lines):
            raise CharterFormatError(n, "front matter is never closed with `---`")
        line = lines[n].strip()
        n += 1
        if line == "---":
            break
        if not line:
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or key not in _FRONT:
            raise CharterFormatError(n, f"front matter takes {_allowed(_FRONT)} as `key: value`, "
                                        f"not {line!r}")
        if key in raw:
            raise CharterFormatError(n, f"`{key}` is given twice")
        value = value.strip().strip("'\"")
        raw[key] = _number(n, key, value, "int") if key == "version" else value
    if "role" not in raw:
        raise CharterFormatError(n, "front matter must name the `role`")

    section: str | None = None
    seen: set[str] = set()
    block: dict[str, Any] | None = None     # the current `###` item, or the section's own dict
    block_keys: set[str] = set()
    titled = False
    description: list[str] = []

    for n, line in enumerate(lines[n:], start=n + 1):
        s = line.strip()
        if not s or s.startswith(">"):
            continue
        if s.startswith("## "):
            name = _text(s[3:]).lower()
            if name not in _SECTIONS:
                raise CharterFormatError(n, f"unknown section {s[3:].strip()!r}; sections are "
                                            + ", ".join(f"`## {x.capitalize()}`" for x in _SECTIONS))
            if name in seen:
                raise CharterFormatError(n, f"section `## {s[3:].strip()}` appears twice")
            seen.add(name)
            section, block, block_keys = name, None, set()
            if name == "evidence standard":
                block = raw.setdefault("evidence_standards", {})
            elif name == "constraints":
                block = raw.setdefault("constraints", {})
            continue
        if s.startswith("### "):
            block, block_keys = _item(n, section, s[4:].strip(), raw), set()
            continue
        if s.startswith("# "):
            if section is not None or titled or description:
                raise CharterFormatError(n, "a `# title` belongs at the top, before the description")
            titled = True
            continue
        if s.startswith("#"):
            raise CharterFormatError(n, f"unexpected heading {s!r}; use `##` sections and `###` items")
        if section is None:
            description.append(s)
            continue
        if not s.startswith("- "):
            raise CharterFormatError(n, f"inside `## {section.capitalize()}` every line is a "
                                        f"`- key: value` bullet or a `###` item; start commentary with `>`")
        _bullet(n, section, s[2:].strip(), raw, block, block_keys)

    if description:
        raw["description"] = _text(" ".join(description))
    return raw


def _item(n: int, section: str | None, heading: str, raw: dict[str, Any]) -> dict[str, Any]:
    if section == "evidence standard":
        m = re.match(r"^override\s*:\s*(\S+)$", heading, re.I)
        if not m:
            raise CharterFormatError(n, "under `## Evidence standard` an item is `### Override: <action_type>`")
        overrides = raw.setdefault("evidence_overrides", {})
        name = _ident(m.group(1))
        if name in overrides:
            raise CharterFormatError(n, f"override {name!r} is given twice")
        overrides[name] = {}
        return overrides[name]
    m = _HEADING.match(heading)
    if section == "accountabilities" and m:
        items, key, text_key = raw.setdefault("accountabilities", []), "id", "statement"
    elif section == "decision rights" and m:
        items, key, text_key = raw.setdefault("decision_rights", []), "action_type", "description"
    else:
        raise CharterFormatError(n, "a `### <id> — <statement>` item belongs under `## Accountabilities` "
                                    "or `## Decision rights`")
    name = _ident(m.group(1))
    if any(i[key] == name for i in items):
        raise CharterFormatError(n, f"{name!r} is defined twice")
    item: dict[str, Any] = {key: name}
    if m.group(2) and _text(m.group(2)):
        item[text_key] = _text(m.group(2))
    items.append(item)
    return item


def _bullet(n: int, section: str, body: str, raw: dict[str, Any], block: dict[str, Any] | None,
            block_keys: set[str]) -> None:
    if section == "glossary":
        m = _BOLD_TERM.match(body)
        term, meaning = (m.group(1), m.group(2)) if m else body.partition(":")[::2]
        term, meaning = _text(term), _text(meaning)
        if not term or not meaning:
            raise CharterFormatError(n, "a glossary line is `- **term**: what it means here`")
        glossary = raw.setdefault("glossary", {})
        if term in glossary:
            raise CharterFormatError(n, f"glossary term {term!r} is defined twice")
        glossary[term] = meaning
        return

    key, sep, value = body.partition(":")
    key = _text(key).lower()
    value = value.strip()
    if not sep or not value:
        raise CharterFormatError(n, f"expected `- key: value`, got {body!r}")
    if block is None:
        what = "accountability" if section == "accountabilities" else "decision right"
        raise CharterFormatError(n, f"start each {what} with a `### <id> — <text>` line before its bullets")
    if key in block_keys and key != "lead":
        raise CharterFormatError(n, f"`{key}` is given twice")
    block_keys.add(key)

    if section == "accountabilities":
        if key == "metric":
            block["metric"] = _ident(value)
        elif key == "direction":
            block["direction"] = value.lower()
        elif key == "horizon":
            m = _DAYS.match(value)
            if not m:
                raise CharterFormatError(n, f"`horizon` is a number of days, e.g. `90 days`, not {value!r}")
            block["horizon_days"] = int(m.group(1))
        elif key == "priority":
            block["priority"] = _number(n, key, value, "int")
        elif key == "lead":
            block.setdefault("leads", []).append(_text(value))
        else:
            raise CharterFormatError(n, f"an accountability takes {_allowed(_ACC_KEYS)}, not `{key}`")
    elif section == "decision rights":
        if key == "autonomy":
            block["autonomy_level"] = value
        elif key == "reversible":
            block["reversible"] = _bool(n, key, value)
        elif key == "cap":
            m = _CAP.match(value)
            if not m:
                raise CharterFormatError(n, f"`cap` is `<number> <unit> per run`, e.g. `200 claims per run`, "
                                            f"not {value!r}")
            radius = block.setdefault("blast_radius", {})
            radius["max_per_run"] = int(m.group(1))
            if m.group(2):
                radius["unit"] = _text(m.group(2))
        elif key == "max value at risk":
            block.setdefault("blast_radius", {})["max_value_at_risk"] = _number(n, key, value, "float")
        elif key == "observe":
            m = _OBSERVE.match(value)
            if not m or not (m.group(1) or m.group(2)):
                raise CharterFormatError(n, f"`observe` is `` `metric` after <n> days ``, not {value!r}")
            if m.group(1):
                block["observation_metric"] = m.group(1)
            if m.group(2):
                block["observation_lag_days"] = int(m.group(2))
        elif key == "approval":
            block["requires_authority"] = _ident(value)
        else:
            raise CharterFormatError(n, f"a decision right takes {_allowed(_RIGHT_KEYS)}, not `{key}`")
    elif section == "evidence standard":
        if key not in _STANDARD_KEYS:
            raise CharterFormatError(n, f"an evidence standard takes {_allowed(_STANDARD_KEYS)}, not `{key}`")
        field, kind = _STANDARD_KEYS[key]
        block[field] = _typed(n, key, value, kind)
    elif section == "constraints":
        if key not in _CONSTRAINT_KEYS:
            raise CharterFormatError(n, f"constraints take {_allowed(_CONSTRAINT_KEYS)}, not `{key}`")
        field = _CONSTRAINT_KEYS[key]
        if field == "block_sensitive_columns":
            v = value.lower()
            if v not in ("blocked", "allowed"):
                raise CharterFormatError(n, f"`sensitive columns` is `blocked` or `allowed`, not {value!r}")
            block[field] = v == "blocked"
        elif field == "notes":
            block[field] = _text(value)
        else:
            block[field] = _idents(value)


# --------------------------------------------------------------------------------------
# render — a loaded charter as the standard Markdown
# --------------------------------------------------------------------------------------

def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() and abs(value) >= 1 else repr(float(value))


def _cols(values: list[str]) -> str:
    return ", ".join(f"`{v}`" for v in values) if values else "none"


def _standard_lines(std: Any) -> list[str]:
    return [
        f"- min sample: {std.min_sample_size}",
        f"- max p: {_num(std.max_p_value)}",
        f"- min effect: {_num(std.min_effect_size)}",
        f"- correction: {std.multiple_testing_correction}",
        f"- requires causal design: {_yn(std.requires_causal_design)}",
        f"- requires holdout: {_yn(std.requires_holdout)}",
        f"- requires confound control: {_yn(std.requires_confound_control)}",
    ]


def through_markdown(charter: "RoleCharter") -> "RoleCharter":
    """`charter` exactly as its Markdown form reads back: free text whitespace-normalised.
    Sign this, not the YAML-loaded object, when the signed copy is written as Markdown —
    otherwise the hash covers whitespace the file no longer has."""
    from .charter import RoleCharter

    return RoleCharter.model_validate(parse(render(charter)))


def render(charter: "RoleCharter") -> str:
    """Every field stated, defaults included: a charter should say everything it grants."""
    out = ["---", f"role: {charter.role}", f"version: {charter.version}"]
    if charter.extends:
        out.append(f"extends: {charter.extends}")
    if charter.content_hash:
        out += [f"status: {charter.status}", f"ratified_by: {charter.ratified_by}",
                f"ratified_at: {charter.ratified_at}", f"content_hash: {charter.content_hash}"]
    out += ["---", f"# {charter.role.replace('_', ' ').capitalize()}"]
    if charter.description.strip():
        out.append(_text(charter.description))

    if charter.glossary:
        out += ["", "## Glossary"] + [f"- **{_text(t)}**: {_text(m)}" for t, m in charter.glossary.items()]

    out += ["", "## Accountabilities"]
    for a in charter.accountabilities:
        out += [f"### {a.id} — {_text(a.statement)}", f"- metric: `{a.metric}`",
                f"- direction: {a.direction}", f"- horizon: {a.horizon_days} days",
                f"- priority: {a.priority}"]
        out += [f"- lead: {_text(lead)}" for lead in a.leads]
        out.append("")
    if out[-1] == "":
        out.pop()

    if charter.decision_rights:
        out += ["", "## Decision rights"]
        for r in charter.decision_rights:
            desc = _text(r.description)
            out += [f"### {r.action_type}" + (f" — {desc}" if desc else ""),
                    f"- autonomy: {r.autonomy_level.value}", f"- reversible: {_yn(r.reversible)}",
                    f"- cap: {r.blast_radius.max_per_run} {_text(r.blast_radius.unit)} per run"]
            if r.blast_radius.max_value_at_risk is not None:
                out.append(f"- max value at risk: {_num(r.blast_radius.max_value_at_risk)}")
            metric = f"`{r.observation_metric}` " if r.observation_metric else ""
            out.append(f"- observe: {metric}after {r.observation_lag_days} days")
            if r.requires_authority:
                out.append(f"- approval: {r.requires_authority}")
            out.append("")
        out.pop()

    out += ["", "## Evidence standard"] + _standard_lines(charter.evidence_standards)
    for name, std in charter.evidence_overrides.items():
        out += [f"### Override: {name}"] + _standard_lines(std)

    c = charter.constraints
    out += ["", "## Constraints"]
    if c.forbidden_features:
        out.append(f"- never use: {_cols(c.forbidden_features)}")
    if c.leakage_features:
        out.append(f"- leaks the outcome: {_cols(c.leakage_features)}")
    if c.allowed_features is not None:
        out.append(f"- only use: {_cols(c.allowed_features)}")
    out.append(f"- sensitive columns: {'blocked' if c.block_sensitive_columns else 'allowed'}")
    if c.notes.strip():
        out.append(f"- notes: {_text(c.notes)}")
    return "\n".join(out) + "\n"
