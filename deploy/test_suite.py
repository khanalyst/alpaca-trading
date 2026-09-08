#!/usr/bin/env python3
"""Run a disjoint CI shard of the complete discovered unittest suite."""
from pathlib import Path
import argparse
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SHARDS = ("edge", "factory", "research", "runtime")


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def shard_for(test):
    module = test.__class__.__module__
    if module == "tests.research.test_edge_discovery":
        return "edge"
    if module == "tests.research.test_factory_end_to_end":
        return "factory"
    return "research" if module.startswith("tests.research.") else "runtime"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", choices=SHARDS)
    parser.add_argument("--list", action="store_true", dest="list_only")
    args = parser.parse_args()
    discovered = list(cases(unittest.defaultTestLoader.discover(
        str(ROOT / "tests"), top_level_dir=str(ROOT))))
    if unittest.defaultTestLoader.errors:
        for error in unittest.defaultTestLoader.errors:
            print(error, file=sys.stderr)
        return 1
    counts = {name: sum(shard_for(test) == name for test in discovered)
              for name in SHARDS}
    assert sum(counts.values()) == len(discovered)
    print(f"Discovered {len(discovered)} tests; disjoint shards: {counts}", flush=True)
    selected = [test for test in discovered
                if args.shard is None or shard_for(test) == args.shard]
    if args.list_only:
        return 0
    if not selected:
        parser.error("selected shard contains no tests")
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
