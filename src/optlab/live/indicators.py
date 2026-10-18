"""Technical studies for scan conditions.

`bar_functions(bars, timeframe)` returns the functions and names a condition can use
on one symbol's OHLCV bars. Every study returns a pandas Series aligned to the bars,
so conditions can compare studies, offset them with ago() and test crossovers.

Units: RSI, stochastics, Williams %R and ADX run 0-100; change_pct, bb_width and
distance_pct are percentages; hv is annualized and decimal (0.25 = 25%) so it
compares directly with option implied vol.

Adding a study: write a function of Series here and register it in bar_functions().
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# bars per year, for annualizing historical volatility on intraday timeframes
BARS_PER_YEAR = {"1m": 252 * 390, "2m": 252 * 195, "5m": 252 * 78, "15m": 252 * 26, "30m": 252 * 13,
                 "60m": 252 * 6.5, "1h": 252 * 6.5, "90m": 252 * 13 / 3, "1d": 252, "1wk": 52, "1mo": 12}


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def sma(s, n):
    return s.rolling(int(n), min_periods=int(n)).mean()


def ema(s, n):
    return s.ewm(span=int(n), adjust=False, min_periods=int(n)).mean()


def rsi(s, n=14):
    d = s.diff()
    up, down = _wilder(d.clip(lower=0), int(n)), _wilder(-d.clip(upper=0), int(n))
    out = 100 - 100 / (1 + up / down.replace(0, np.nan))
    # no losses in the window: RSI is 100 (50 if price didn't move at all), not undefined
    return out.mask(down == 0, np.where(up > 0, 100.0, 50.0))


def macd_parts(s, fast=12, slow=26, signal=9):
    line = ema(s, fast) - ema(s, slow)
    sig = line.ewm(span=int(signal), adjust=False, min_periods=int(signal)).mean()
    return line, sig, line - sig


def true_range(h, l, c):
    prev = c.shift()
    return pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)


def atr(h, l, c, n=14):
    return _wilder(true_range(h, l, c), int(n))


def adx_parts(h, l, c, n=14):
    up, down = h.diff(), -l.diff()
    plus = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=h.index)
    minus = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=h.index)
    tr = _wilder(true_range(h, l, c), int(n))
    pdi, mdi = 100 * _wilder(plus, int(n)) / tr, 100 * _wilder(minus, int(n)) / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return _wilder(dx, int(n)), pdi, mdi


def bollinger(s, n=20, k=2.0):
    mid = sma(s, n)
    sd = s.rolling(int(n), min_periods=int(n)).std(ddof=0)
    return mid + k * sd, mid, mid - k * sd


def stochastic(h, l, c, n=14, smooth=3, d=3):
    lo, hi = l.rolling(int(n)).min(), h.rolling(int(n)).max()
    k = 100 * (c - lo) / (hi - lo).replace(0, np.nan)
    k = k.rolling(int(smooth)).mean()
    return k, k.rolling(int(d)).mean()


def vwap(h, l, c, v, intraday: bool):
    """Session VWAP: resets each day on intraday bars, cumulative over the window on daily bars."""
    tp = (h + l + c) / 3
    if not intraday:
        return (tp * v).cumsum() / v.cumsum()
    day = pd.Series(pd.DatetimeIndex(c.index).normalize(), index=c.index)
    return (tp * v).groupby(day).cumsum() / v.groupby(day).cumsum()


# ------------------------------------------------------------ behaviour over several bars
# Each returns a boolean Series. A window containing a missing value (warm-up) is False.
# "bars=N" counts the current bar: stayed_above(a, b, bars=5) checks this bar and the 4 before.

def _window_all(cond: pd.Series, valid: pd.Series, n: int) -> pd.Series:
    x = cond.astype(float).where(valid)
    return (x.rolling(n, min_periods=n).min() == 1)


def _window_any(cond: pd.Series, valid: pd.Series, n: int) -> pd.Series:
    x = cond.astype(float).where(valid)
    return (x.rolling(n, min_periods=n).max() == 1)


def rising(x: pd.Series, n: int) -> pd.Series:
    """Strictly higher than the bar before on each of the last n steps: x[t] > x[t-1] > ... > x[t-n].
    A flat step (equal values) breaks it."""
    return _window_all(x > x.shift(), x.notna() & x.shift().notna(), n)


def falling(x: pd.Series, n: int) -> pd.Series:
    """Strictly lower than the bar before on each of the last n steps."""
    return _window_all(x < x.shift(), x.notna() & x.shift().notna(), n)


def increasing(x: pd.Series, n: int) -> pd.Series:
    """Never lower on each of the last n steps (equal values allowed) and higher overall: x[t] > x[t-n]."""
    return _window_all(x >= x.shift(), x.notna() & x.shift().notna(), n) & (x > x.shift(n))


def decreasing(x: pd.Series, n: int) -> pd.Series:
    """Never higher on each of the last n steps (equal values allowed) and lower overall: x[t] < x[t-n]."""
    return _window_all(x <= x.shift(), x.notna() & x.shift().notna(), n) & (x < x.shift(n))


def bar_functions(bars: pd.DataFrame, timeframe: str = "1d") -> tuple[dict, dict]:
    """(names, funcs) for the Evaluator, bound to one symbol's bars."""
    o, h, l, c, v = (bars[k].astype(float) for k in ("open", "high", "low", "close", "volume"))
    per_year = BARS_PER_YEAR.get(timeframe, 252)
    src = lambda x: c if x is None else x  # noqa: E731

    def change_pct(n=1, s=None):
        s = src(s)
        return 100 * (s / s.shift(_lookback(n, "change_pct")) - 1)

    def crosses_above(a, b):
        a, b = _series(a, c), _series(b, c)
        return (a > b) & (a.shift() <= b.shift())

    def crosses_below(a, b):
        a, b = _series(a, c), _series(b, c)
        return (a < b) & (a.shift() >= b.shift())

    funcs = {
        "sma": lambda n, s=None: sma(src(s), n),
        "ema": lambda n, s=None: ema(src(s), n),
        "rsi": lambda n=14, s=None: rsi(src(s), n),
        "macd": lambda fast=12, slow=26, signal=9, s=None: macd_parts(src(s), fast, slow, signal)[0],
        "macd_signal": lambda fast=12, slow=26, signal=9, s=None: macd_parts(src(s), fast, slow, signal)[1],
        "macd_hist": lambda fast=12, slow=26, signal=9, s=None: macd_parts(src(s), fast, slow, signal)[2],
        "atr": lambda n=14: atr(h, l, c, n),
        "atr_pct": lambda n=14: 100 * atr(h, l, c, n) / c,
        "adx": lambda n=14: adx_parts(h, l, c, n)[0],
        "plus_di": lambda n=14: adx_parts(h, l, c, n)[1],
        "minus_di": lambda n=14: adx_parts(h, l, c, n)[2],
        "bb_upper": lambda n=20, k=2.0, s=None: bollinger(src(s), n, k)[0],
        "bb_mid": lambda n=20, k=2.0, s=None: bollinger(src(s), n, k)[1],
        "bb_lower": lambda n=20, k=2.0, s=None: bollinger(src(s), n, k)[2],
        "bb_width": lambda n=20, k=2.0, s=None: (lambda u, m, lo: 100 * (u - lo) / m)(*bollinger(src(s), n, k)),
        "bb_pct": lambda n=20, k=2.0, s=None: (lambda u, m, lo: (src(s) - lo) / (u - lo))(*bollinger(src(s), n, k)),
        "stoch_k": lambda n=14, smooth=3, d=3: stochastic(h, l, c, n, smooth, d)[0],
        "stoch_d": lambda n=14, smooth=3, d=3: stochastic(h, l, c, n, smooth, d)[1],
        "williams_r": lambda n=14: -100 * (h.rolling(int(n)).max() - c) / (h.rolling(int(n)).max() - l.rolling(int(n)).min()),
        "cci": lambda n=20: (lambda tp: (tp - sma(tp, n)) / (0.015 * tp.rolling(int(n)).apply(
            lambda x: np.abs(x - x.mean()).mean(), raw=True)))((h + l + c) / 3),
        "obv": lambda: (np.sign(c.diff()).fillna(0) * v).cumsum(),
        "vwap": lambda: vwap(h, l, c, v, intraday=timeframe not in ("1d", "1wk", "1mo")),
        "rolling_vwap": lambda n=20: ((h + l + c) / 3 * v).rolling(_lookback(n, "rolling_vwap")).sum()
        / v.rolling(_lookback(n, "rolling_vwap")).sum(),
        "pct_rank": lambda x, n=252: 100 * _series(x, c).rolling(_lookback(n, "pct_rank", minimum=2)).rank(pct=True),
        "hv": lambda n=20: np.log(c / c.shift()).rolling(int(n)).std() * np.sqrt(per_year),
        "highest": lambda n, s=None: (h if s is None else s).rolling(int(n)).max(),
        "lowest": lambda n, s=None: (l if s is None else s).rolling(int(n)).min(),
        "change_pct": change_pct,
        "distance_pct": lambda a, b: 100 * (_series(a, c) / _series(b, c) - 1),
        "avg_volume": lambda n=20: v.rolling(int(n)).mean(),
        "rel_volume": lambda n=20: v / v.rolling(int(n)).mean(),
        "ago": lambda x, n=1: _series(x, c).shift(_lookback(n, "ago", minimum=0)),
        "crosses_above": crosses_above,
        "crosses_below": crosses_below,
        # behaviour across bars (see the functions above for exact definitions)
        "rising": lambda x, bars=3: rising(_series(x, c), _lookback(bars, "rising")),
        "falling": lambda x, bars=3: falling(_series(x, c), _lookback(bars, "falling")),
        "increasing": lambda x, bars=3: increasing(_series(x, c), _lookback(bars, "increasing")),
        "decreasing": lambda x, bars=3: decreasing(_series(x, c), _lookback(bars, "decreasing")),
        "stayed_above": lambda a, b, bars=3: (lambda A, B: _window_all(A > B, A.notna() & B.notna(),
                                                                       _lookback(bars, "stayed_above")))(_series(a, c), _series(b, c)),
        "stayed_below": lambda a, b, bars=3: (lambda A, B: _window_all(A < B, A.notna() & B.notna(),
                                                                       _lookback(bars, "stayed_below")))(_series(a, c), _series(b, c)),
        "crossed_above_within": lambda a, b, bars=3: _window_any(crosses_above(a, b), pd.Series(True, index=c.index),
                                                                 _lookback(bars, "crossed_above_within")),
        "crossed_below_within": lambda a, b, bars=3: _window_any(crosses_below(a, b), pd.Series(True, index=c.index),
                                                                 _lookback(bars, "crossed_below_within")),
        "touched": lambda a, b, tolerance_pct=0.25: (lambda A, B: ((A - B).abs() <= abs(float(tolerance_pct)) / 100 * B.abs())
                                                     & A.notna() & B.notna())(_series(a, c), _series(b, c)),
        "between": lambda x, lower, upper: (lambda X, L, U: (X >= L) & (X <= U))(_series(x, c), _series(lower, c),
                                                                                  _series(upper, c)),
        "abs": lambda x: x.abs() if isinstance(x, pd.Series) else abs(x),
        "min": lambda a, b: np.minimum(a, b),
        "max": lambda a, b: np.maximum(a, b),
    }
    names = {"open": o, "high": h, "low": l, "close": c, "price": c, "volume": v}
    return names, funcs


def _lookback(n, fname: str, minimum: int = 1) -> int:
    """Bar counts must look backwards: a negative shift would read future bars."""
    if not float(n).is_integer() or int(n) < minimum:
        from .expr import ExprError

        raise ExprError(f"{fname}() needs a whole number of bars >= {minimum}, got {n!r}"
                        + (" (negative values would look into the future)" if float(n) < 0 else ""))
    return int(n)


def _series(x, like: pd.Series) -> pd.Series:
    return x if isinstance(x, pd.Series) else pd.Series(float(x), index=like.index)


STUDY_HELP = {
    "sma(n)": "simple moving average of close (any study takes s=... to use another series)",
    "ema(n)": "exponential moving average", "rsi(n=14)": "relative strength index, 0-100",
    "macd() / macd_signal() / macd_hist()": "MACD line, signal line, histogram (12, 26, 9)",
    "atr(n=14) / atr_pct(n=14)": "average true range, in dollars / % of price",
    "adx(n=14), plus_di, minus_di": "trend strength and direction, 0-100",
    "bb_upper / bb_mid / bb_lower (n=20, k=2)": "Bollinger Bands", "bb_width, bb_pct": "band width %, position in band 0-1",
    "stoch_k / stoch_d (n=14)": "stochastic oscillator, 0-100", "williams_r(n=14)": "Williams %R, -100..0",
    "cci(n=20)": "commodity channel index", "obv()": "on-balance volume", "vwap()": "session VWAP (intraday)",
    "hv(n=20)": "historical (realized) volatility, annualized decimal",
    "rolling_vwap(n=20)": "volume-weighted average price over the last n bars (useful on daily bars)",
    "pct_rank(x, n=252)": "percentile rank 0-100 of x's latest value among its last n values (causal)",
    "highest(n) / lowest(n)": "highest high / lowest low over n bars",
    "change_pct(n=1)": "% change over n bars", "distance_pct(a, b)": "% that a is above b",
    "avg_volume(n=20), rel_volume(n=20)": "average volume, volume vs average",
    "ago(x, n=1)": "value of x n bars ago", "crosses_above(a, b), crosses_below(a, b)": "crossover on the latest bar",
    "rising(x, bars=3) / falling(x, bars=3)": "strictly up / down on each of the last N bars (a flat bar breaks it)",
    "increasing(x, bars=3) / decreasing(x, bars=3)": "never down / never up for N bars, and higher / lower overall",
    "stayed_above(a, b, bars=3) / stayed_below": "a > b (a < b) on each of the last N bars, current included",
    "crossed_above_within(a, b, bars=3) / crossed_below_within": "a crossover on any of the last N bars",
    "touched(a, b, tolerance_pct=0.25)": "|a - b| <= tolerance_pct % of b on this bar (equality counts)",
    "between(x, lower, upper)": "lower <= x <= upper (both ends count)",
    "open, high, low, close, volume": "latest bar values",
}
