#!/usr/bin/env bash
set -euo pipefail

# Research is an explicit offline job. It must be given a normalized JSONL
# dataset; a default container startup must not invent one or touch the broker.
dataset="${ALPACA_RESEARCH_DATASET:-}"
if [ -z "$dataset" ]; then
  echo "ALPACA_RESEARCH_DATASET is unset; research is optional and remains idle." >&2
  exit 2
fi
feed="${ALPACA_DATA_FEED:-${ALPACA_STOCK_FEED:-iex}}"
python_bin="${PYTHON:-python}"
"$python_bin" research.py validate-data "$dataset" \
  --provider alpaca --feed "$feed"

# The same normalized bars can be replayed without a broker.  Set this to 0
# when a validation-only dataset intentionally contains quote/option rows.
if [ "${ALPACA_RESEARCH_BACKTEST:-1}" = "1" ]; then
  "$python_bin" research.py backtest-ibr "$dataset" \
    --provider alpaca --feed "$feed" --vehicle equity
fi
