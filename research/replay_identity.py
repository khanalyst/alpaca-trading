"""Source identity shared by the paper incumbent and recorder-backed shadow."""
from __future__ import annotations

import hashlib
from pathlib import Path


def runtime_code_identity(root: str | Path | None = None) -> str:
    """Hash the contents and relative names of the replay source bundle."""
    repo = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    files = {
        path.relative_to(repo).as_posix()
        for name in ("agent", "research")
        for path in (repo / name).rglob("*.py")
        if path.is_file()
    }
    files.update(("deploy/recorder.py", "deploy/recorder_market.py",
                  "deploy/research_dataset.py", "requirements.lock.txt"))
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8") + b"\0")
        digest.update((repo / name).read_bytes())
    return digest.hexdigest()
