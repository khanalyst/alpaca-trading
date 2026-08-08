"""Small content-addressed backup helper for research artifacts."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(source: str | Path, target: str | Path) -> dict:
    """Copy a research tree and write a checksum manifest; never overwrites."""
    source = Path(source)
    target = Path(target)
    if not source.is_dir():
        raise ValueError(f"source is not a directory: {source}")
    if target.exists():
        raise FileExistsError(target)
    shutil.copytree(source, target)
    files = {
        str(path.relative_to(target)): _digest(path)
        for path in sorted(target.rglob("*")) if path.is_file()
    }
    manifest = {
        "schema": "research-backup.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source), "files": files,
    }
    (target / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def verify_backup(target: str | Path) -> dict:
    target = Path(target)
    manifest_path = target / "MANIFEST.json"
    if not manifest_path.is_file():
        raise ValueError("backup is missing MANIFEST.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checked = 0
    errors = []
    for name, expected in manifest.get("files", {}).items():
        path = target / name
        if not path.is_file():
            errors.append(f"missing {name}")
            continue
        checked += 1
        actual = _digest(path)
        if actual != expected:
            errors.append(f"checksum mismatch {name}")
    return {"valid": not errors, "checked": checked, "errors": errors,
            "schema": manifest.get("schema")}


__all__ = ["create_backup", "verify_backup"]
