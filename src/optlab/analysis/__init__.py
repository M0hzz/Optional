"""Historical analysis (Phase 6): open the Historical Signal Explorer from a plan, position, alert rule,
watched contract or event, with the conditions that object uses, and describe what price did afterwards.

    translate.py   live conditions -> historical conditions (clauses with no timestamped history are left
                   out, each with the reason)
    drafts.py      a draft analysis for a dashboard object (symbols, timeframe, range, conditions, horizons)
    stats.py       forward statistics, win/loss distributions, regime / volatility / time-of-day breakdowns,
                   data coverage, and the bias checks (including a truncation test for look-ahead)
    service.py     runs: validate, execute (in a background thread in the API), store, never recompute

Descriptive statistics only: nothing here predicts, recommends or places anything.
"""
