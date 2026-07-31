"""Real-time maker research from an observed two-sided order book.

This is a forward-only contract.  It does not infer queue position or a fill
from candles.  A proposal exists only to paper-simulate a declared passive
entry at the observed best quote; absent book, spread, depth or imbalance is
reported as missing data and never replaced with a guessed trade.
"""

from __future__ import annotations

from . import finite as _finite, register


def setup_evidence(snapshot: dict, cfg: dict) -> dict:
    block = cfg["strategy"]
    book = snapshot.get("_enrichment")
    book = book if isinstance(book, dict) else {}
    bid = _finite(book.get("book_best_bid"))
    ask = _finite(book.get("book_best_ask"))
    spread = _finite(book.get("book_spread_pct"))
    bid_depth = _finite(book.get("book_bid_depth_usd"))
    ask_depth = _finite(book.get("book_ask_depth_usd"))
    bid_top = _finite(book.get("book_top_bid_size"))
    ask_top = _finite(book.get("book_top_ask_size"))
    book_ts = _finite(book.get("book_ts"))

    required = {
        "book_best_bid": bid, "book_best_ask": ask,
        "book_spread_pct": spread, "book_bid_depth_usd": bid_depth,
        "book_ask_depth_usd": ask_depth, "book_top_bid_size": bid_top,
        "book_top_ask_size": ask_top, "book_ts": book_ts,
    }
    missing = sorted(key for key, value in required.items() if value is None)
    valid_book = (
        not missing and ask > bid > 0 and spread >= 0
        and bid_depth >= 0 and ask_depth >= 0
        and bid_top >= 0 and ask_top >= 0)
    depth_total = ((bid_depth or 0.0) + (ask_depth or 0.0))
    imbalance = ((bid_depth - ask_depth) / depth_total
                 if valid_book and depth_total > 0 else None)

    max_spread = float(block.get("scalp_max_spread_pct", 0.02))
    min_imbalance = float(block.get("scalp_min_abs_imbalance", 0.15))
    min_depth = float(block.get("scalp_min_depth_usd", 10_000.0))
    liquid = (valid_book and spread <= max_spread
              and min(bid_depth, ask_depth) >= min_depth)
    long_ = bool(liquid and imbalance is not None
                 and imbalance >= min_imbalance)
    short_ = bool(liquid and imbalance is not None
                  and imbalance <= -min_imbalance)
    return {
        "spread_capture": {"long": long_, "short": short_},
        "extension_atr": {
            "long": 0.0 if valid_book else None,
            "short": 0.0 if valid_book else None,
        },
        "hard_no_chase_atr": float(block["hard_max_entry_extension_atr"]),
        "book_imbalance": (
            round(imbalance, 6) if imbalance is not None else None),
        "book_spread_pct": spread,
        "data_missing": missing,
    }


register("scalp-maker", setup_evidence)
