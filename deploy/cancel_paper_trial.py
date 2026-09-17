#!/usr/bin/env python3
"""Cancel an evidence-empty paused paper incumbent after an auditable check."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.config import load_config  # noqa: E402
from agent.paper_trial_operator import (  # noqa: E402
    PaperTrialOperatorError,
    cancel_paper_trial,
)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    command.add_argument("--confirm-trial-id", required=True)
    command.add_argument("--confirm-incumbent-identity", required=True)
    command.add_argument("--reason", required=True,
                         help="bounded operator reason for cancellation")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        env_file = os.getenv("ALPACA_AGENT_SECRETS_FILE")
        if env_file:
            from deploy.recorder import load_dotenv
            load_dotenv(env_file, override=False)
        config = load_config(args.config)
        result = cancel_paper_trial(
            config,
            confirm_trial_id=args.confirm_trial_id,
            confirm_incumbent_identity=args.confirm_incumbent_identity,
            reason=args.reason,
        )
    except PaperTrialOperatorError as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)},
                         sort_keys=True), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        # Configuration/path errors are useful to the operator, while keeping
        # provider-specific identifiers out of the command's stderr surface.
        print(json.dumps({"status": "rejected", "error": str(exc)},
                         sort_keys=True), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({
            "status": "rejected",
            "error": f"paper cancellation failed: {type(exc).__name__}",
        }, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
