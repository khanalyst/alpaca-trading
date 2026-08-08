"""Small deployment helpers; none of these modules may place orders."""

from __future__ import annotations

from pathlib import Path


def load_config(path: str | Path) -> dict:
    """Load deployment config without making probes depend on PyYAML.

    Production images install PyYAML, but health/scheduler probes should still
    be importable in a minimal recovery shell.  The fallback intentionally
    parses only scalar top-level and one-level section values used by probes.
    """
    source = Path(path)
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        value = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        return value if isinstance(value, dict) else {}
    result: dict = {}
    section: dict | None = None
    for raw in source.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        indent = len(line) - len(line.lstrip())
        key, value = (part.strip() for part in line.strip().split(":", 1))
        if indent == 0 and not value:
            section = {}
            result[key] = section
            continue
        target = section if indent else result
        if target is None:
            target = result
        value = value.strip().strip('"\'')
        if value.lower() in {"true", "false"}:
            parsed: object = value.lower() == "true"
        else:
            try:
                parsed = float(value) if "." in value else int(value)
            except ValueError:
                parsed = value
        target[key] = parsed
    return result
