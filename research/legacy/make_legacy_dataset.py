"""Adapt an `edge_lab` dataset into the layout the legacy phase1 backtest expects.

Running the repository's own backtest over the same bars as the independent
harness turns "two studies agreed" into real corroboration: two separately
written simulators, the same authoritative strategy code, the same data.

Spot candles stand in for the index price. Only the funding-squeeze contract
reads basis, and that contract additionally needs open interest, which the
legacy script deliberately holds at None - so a zero basis changes nothing
about which trades the legacy script produces.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pandas as pd


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--min-bars", type=int, default=20000)
    args = parser.parse_args()

    for folder in ("swap", "spot", "funding"):
        (args.dest / folder).mkdir(parents=True, exist_ok=True)

    symbols = []
    for path in sorted((args.source / "swap").glob("*.csv")):
        swap = pd.read_csv(path)
        funding_path = args.source / "funding" / path.name
        if len(swap) < args.min_bars or not funding_path.exists():
            continue
        columns = ["timestamp_ms", "open", "high", "low", "close", "volume"]
        swap[columns].to_csv(args.dest / "swap" / path.name, index=False)
        swap[columns].to_csv(args.dest / "spot" / path.name, index=False)
        shutil.copy(funding_path, args.dest / "funding" / path.name)
        symbols.append(path.stem.replace("_USDT_USDT", "/USDT:USDT"))

    source_manifest = {}
    manifest_path = args.source / "manifest.json"
    if manifest_path.exists():
        source_manifest = json.loads(manifest_path.read_text())
    (args.dest / "manifest.json").write_text(json.dumps({
        "source": source_manifest.get("source", "OKX public v5 REST"),
        "downloaded_at": source_manifest.get("downloaded_at", "unknown"),
        "symbols": sorted(symbols),
        "note": (
            "spot/ duplicates swap/ so perp-index basis is identically zero. "
            "Only the funding-squeeze contract reads basis, and that contract "
            "cannot fire in the legacy script because open interest is None."
        ),
    }, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"symbols": len(symbols), "dest": str(args.dest)},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
