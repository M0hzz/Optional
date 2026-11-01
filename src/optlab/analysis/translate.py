"""Turn the conditions a dashboard object uses (monitoring rules, plan triggers, contract entry conditions)
into conditions the historical search can evaluate on price bars.

A condition is split at its top-level `and` into clauses, so one live-only part doesn't drop the whole rule.
Each clause is kept if every name and function in it has price/volume history; otherwise it's left out with
the reason. Leaving a clause out makes the historical search looser than the live rule: that's reported.
"""
from __future__ import annotations

import ast

from ..history.strategy import NOT_HISTORICAL, check_expression
from ..live.expr import ExprError, parse

POSITION_ONLY = {"pnl", "pnl_pct", "value", "entry", "dte", "short_strike_otm_pct", "position_delta", "position_gamma",
                 "theta_day", "position_vega", "loss_pct_of_max", "short_itm", "earnings_before_expiry"}
OPTION_QUOTE = {"bid", "ask", "mid", "last", "open_interest", "oi", "iv", "delta", "abs_delta", "gamma", "theta", "vega",
                "spread", "spread_pct", "max_spread_pct", "min_open_interest", "strike", "underlying_price", "intrinsic",
                "extrinsic", "breakeven", "otm_pct", "target_entry"}
NEWS_PREFIXES = ("news_", "minutes_since_news", "bullish_article_count", "bearish_article_count", "market_news_risk")


def split_clauses(text: str) -> list[str]:
    """`a and b and (c or d)` -> ['a', 'b', 'c or d']. Anything that isn't a top-level `and` stays whole."""
    tree = parse(text)
    body = tree.body
    if isinstance(body, ast.BoolOp) and isinstance(body.op, ast.And):
        return [ast.unparse(v) for v in body.values]
    return [text.strip()]


def why_not_historical(names: set[str]) -> str | None:
    pos = sorted(names & POSITION_ONLY)
    if pos:
        return (f"uses {', '.join(pos)}: position values (P&L, Greeks, days to expiration) exist only while the position "
                "is open, so there's no history to search")
    opt = sorted(names & OPTION_QUOTE)
    if opt:
        return f"uses {', '.join(opt)}: option quote history isn't stored (only today's chain), so it can't be searched"
    news = sorted(n for n in names if n.startswith(NEWS_PREFIXES))
    if news:
        return (f"uses {', '.join(news)}: news history exists only since optlab started collecting it, too short and "
                "too uneven to search")
    hist = sorted(names & set(NOT_HISTORICAL))
    if hist:
        return f"uses {', '.join(hist)}: needs historical {', '.join(NOT_HISTORICAL[h] for h in hist)}, which isn't stored"
    return None


def translate(sources: list[tuple[str, str]]) -> tuple[list[dict], list[dict]]:
    """sources: [(origin, condition text)]. Returns (included clauses, excluded clauses)."""
    included: list[dict] = []
    excluded: list[dict] = []
    seen: set[str] = set()
    for origin, text in sources:
        if not text or not text.strip():
            continue
        try:
            clauses = split_clauses(text)
        except ExprError as exc:
            excluded.append({"expression": text, "origin": origin, "reason": f"can't be parsed: {exc}"})
            continue
        for c in clauses:
            if c in seen:
                continue
            seen.add(c)
            names = {n.id for n in ast.walk(parse(c)) if isinstance(n, ast.Name)}
            why = why_not_historical(names)
            if why is None:
                problems = check_expression(c, "condition")
                why = "; ".join(p.replace("condition: ", "") for p in problems) if problems else None
            if why:
                excluded.append({"expression": c, "origin": origin, "reason": why})
            else:
                included.append({"expression": c, "origin": origin})
    return included, excluded
