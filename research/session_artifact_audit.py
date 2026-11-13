"""Reassess saved study claims without pretending to replay absent market data.

This command never writes a trading/proof ledger. Historical t-statistics are
read as reported; corrected timing or execution needs a separate raw-data run.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from statistics import NormalDist
from typing import Any, Mapping

from studies.session_edges.discover import benjamini_yekutieli, holm


SCHEMA = "session-edge-artifact-audit.v1"


def audit(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    raw = source.read_bytes()
    record = json.loads(raw)
    if record.get("schema") not in {"session-edges-discovery.v1", "session-edges-discovery.v2"}:
        raise ValueError("expected a versioned session discovery artifact")
    promoted = record.get("promoted")
    if not isinstance(promoted, Mapping):
        raise ValueError("artifact has no promoted hypotheses")
    alpha: dict[str, float] = {}
    returns: dict[str, float] = {}
    for key, row in promoted.items():
        holdout = row.get("holdout", {})
        # Every admitted historical hypothesis remains in the denominator.
        # Missing/non-finite/underpowered evidence is the conservative p=1.
        alpha[str(key)] = returns[str(key)] = 1.0
        if holdout.get("sessions", 0) < 500:
            continue
        for field, destination in (("alpha_t", alpha), ("t", returns)):
            value = holdout.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                destination[str(key)] = NormalDist().cdf(-float(value))
    legacy = record["schema"].endswith(".v1")
    return {
        "schema": SCHEMA,
        "authorizing": False,
        "raw_market_data_replayed": False,
        "source": str(source),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_schema": record["schema"],
        "hypotheses_tested": record.get("hypotheses_tested"),
        "promoted_count": len(promoted),
        "recorded_tiers": dict(Counter(row.get("tier", "missing") for row in promoted.values())),
        "recorded_sources": dict(Counter(row.get("source", "missing") for row in promoted.values())),
        "total_return_holm": sorted(holm(returns)),
        "total_return_by": sorted(benjamini_yekutieli(returns)),
        "alpha_holm": sorted(holm(alpha)),
        "alpha_by": sorted(benjamini_yekutieli(alpha)),
        "minimum_alpha_p": min(alpha.values(), default=None),
        "holm_first_threshold": 0.05 / len(alpha) if alpha else None,
        "limitations": [
            "recomputes correction from saved approximate statistics only",
            "does not validate causal timing, fills, costs, factor model or raw data",
            "requires a new exact-rule raw-data replay and main qualification",
            *(["legacy simulator and ledger identities cannot qualify the corrected implementation"] if legacy else []),
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    result = audit(args.source)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"promoted": result["promoted_count"],
                      "alpha_holm": len(result["alpha_holm"]),
                      "alpha_by": len(result["alpha_by"]),
                      "authorizing": False, "raw_market_data_replayed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
