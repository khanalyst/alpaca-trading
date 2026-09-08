#!/usr/bin/env python3
"""Fail before expanding a corpus that exceeds an explicit research budget."""
import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.research_dataset import _source_paths


def check(source, partition_root, session_window, *, max_bytes, temporary_root,
          reserve_bytes=2*1024**3, expansion=8):
    if min(session_window, max_bytes, reserve_bytes) < 0 or expansion < 1:
        raise ValueError("research budget values must be nonnegative, with expansion >= 1")
    paths = _source_paths(source, partition_root=partition_root,
                          session_window=session_window)
    if any(p.is_symlink() or not p.is_file() for p in paths):
        raise ValueError("research budget input must contain regular files")
    size = sum(p.stat().st_size for p in paths)
    free = shutil.disk_usage(temporary_root).free
    result = {"schema": "research-resource-budget.v1", "input_files": len(paths),
              "input_bytes": size, "maximum_input_bytes": max_bytes,
              "free_bytes": free, "reserved_bytes": reserve_bytes,
              "estimated_expanded_bytes": size*expansion,
              "expansion_is_estimate": True}
    reason = ("input_byte_limit" if max_bytes and size > max_bytes else
              "insufficient_temporary_space" if size*expansion+reserve_bytes > free else None)
    return {**result, "ok": reason is None, "reason": reason}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--partition-root", type=Path)
    parser.add_argument("--session-window", type=int, default=0)
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--temporary-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = check(args.source, args.partition_root, args.session_window,
                       max_bytes=args.max_bytes, temporary_root=args.temporary_root)
    except (ValueError, OSError) as exc:
        result = {"ok": False, "reason": str(exc)}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
