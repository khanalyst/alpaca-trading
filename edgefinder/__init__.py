"""Automated edge discovery and a small trader for the edge it selects.

Nothing in ``agent/``, ``research/`` or ``deploy/`` imports this package, and
it imports none of their modules at run time, so it cannot change the
preregistered instrument identity or the paper-trial code identity.

Layout:

* ``data``       public daily and hourly bars, cached, split into legs
* ``costs``      per-symbol round-trip costs (the repository's v2 schedule)
* ``catalogue``  every hypothesis family and its parameter grid
* ``backtest``   one engine for every candidate: legs, weights, turnover
* ``stats``      HAC inference, deflated Sharpe, PBO, SPA, multiple testing
* ``discover``   the automated search, the selection rule and the verdicts
* ``report``     the markdown report and the frozen strategy spec
* ``trader``     the executor that trades the frozen spec through Alpaca
"""

SCHEMA = "edgefinder.v1"
