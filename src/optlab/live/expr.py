"""A small, safe expression language for scan and exit conditions.

Conditions are plain text in scans.yaml, for example

    rsi(14) < 30 and close > sma(200)
    crosses_above(ema(9), ema(21))
    iv > hv(20) * 1.2 and open_interest >= 500

They are parsed with Python's `ast` module and evaluated by walking the tree. Only
numbers, strings, names, whitelisted function calls, arithmetic, comparisons and
and/or/not are allowed: no attribute access, indexing, lambdas or imports, so a
config file can't run arbitrary code.

Values can be scalars, NumPy arrays (one value per option contract) or pandas
Series (one value per price bar); and/or/not work element-wise on all of them.
"""
from __future__ import annotations

import ast
import difflib
import operator
from functools import lru_cache

import numpy as np
import pandas as pd


class ExprError(ValueError):
    pass


_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_CMPOPS = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
           ast.Eq: operator.eq, ast.NotEq: operator.ne}
_ALLOWED = (ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Call, ast.Name, ast.Constant,
            ast.Load, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, ast.keyword, *_BINOPS, *_CMPOPS)


@lru_cache(maxsize=1024)
def parse(text: str) -> ast.Expression:
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"Can't parse condition {text!r}: {exc.msg}") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ExprError(f"{type(node).__name__} is not allowed in conditions: {text!r}")
        if isinstance(node, ast.Call) and not isinstance(node.func, ast.Name):
            raise ExprError(f"Only plain function calls are allowed: {text!r}")
    return tree


def names_used(text: str) -> set[str]:
    """Every bare name and function name in a condition (used to decide what data to fetch)."""
    return {n.id for n in ast.walk(parse(text)) if isinstance(n, ast.Name)}


def _and(a, b):
    if isinstance(a, (pd.Series, np.ndarray)) or isinstance(b, (pd.Series, np.ndarray)):
        return _bool(a) & _bool(b)
    return bool(a) and bool(b)


def _or(a, b):
    if isinstance(a, (pd.Series, np.ndarray)) or isinstance(b, (pd.Series, np.ndarray)):
        return _bool(a) | _bool(b)
    return bool(a) or bool(b)


def _bool(x):
    if isinstance(x, pd.Series):
        return x.fillna(0).astype(bool)
    if isinstance(x, np.ndarray):
        return np.nan_to_num(x.astype(float), nan=0.0).astype(bool) if x.dtype != bool else x
    return bool(x) if x == x else False  # NaN -> False


class Evaluator:
    """Evaluate conditions against `names` (values) and `funcs` (callables).

    `series_eval`, when given, is a second Evaluator over price-bar Series. Calls to the
    functions it knows (sma, rsi, ...) are handed to it whole and reduced to their latest
    value, so contract- and position-level conditions can mix chain columns with
    indicators: `bid > 1 and rsi(14) < 40`.
    """

    def __init__(self, names: dict, funcs: dict | None = None, series_eval: "Evaluator | None" = None):
        self.names, self.funcs, self.series_eval = names, funcs or {}, series_eval

    def __call__(self, text: str):
        return self._eval(parse(text).body, text)

    def _eval(self, node, text):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float, str, bool)):
                return node.value
            raise ExprError(f"Unsupported constant {node.value!r} in {text!r}")
        if isinstance(node, ast.Name):
            if node.id in self.names:
                return self.names[node.id]
            if self.series_eval is not None and node.id in self.series_eval.names:
                return last(self.series_eval.names[node.id])
            raise ExprError(self._unknown(node.id, text, "name"))
        if isinstance(node, ast.Call):
            fname = node.func.id
            if fname in self.funcs:
                args = [self._eval(a, text) for a in node.args]
                kwargs = {k.arg: self._eval(k.value, text) for k in node.keywords}
                try:
                    return self.funcs[fname](*args, **kwargs)
                except TypeError as exc:
                    raise ExprError(f"Bad arguments to {fname}() in {text!r}: {exc}") from None
            if self.series_eval is not None and fname in self.series_eval.funcs:
                return last(self.series_eval._eval(node, text))
            raise ExprError(self._unknown(fname, text, "function"))
        if isinstance(node, ast.BinOp):
            return _BINOPS[type(node.op)](self._eval(node.left, text), self._eval(node.right, text))
        if isinstance(node, ast.UnaryOp):
            v = self._eval(node.operand, text)
            if isinstance(node.op, ast.Not):
                b = _bool(v)
                return ~b if isinstance(b, (pd.Series, np.ndarray)) else not b
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BoolOp):
            vals = [self._eval(v, text) for v in node.values]
            out = vals[0]
            for v in vals[1:]:
                out = _and(out, v) if isinstance(node.op, ast.And) else _or(out, v)
            return out
        if isinstance(node, ast.Compare):  # a < b < c  ->  (a < b) and (b < c)
            left, out = self._eval(node.left, text), True
            for op, comp in zip(node.ops, node.comparators):
                right = self._eval(comp, text)
                out = _and(out, _CMPOPS[type(op)](left, right))
                left = right
            return out
        raise ExprError(f"Unsupported syntax in {text!r}")

    def _unknown(self, name, text, kind):
        pool = list(self.names) + list(self.funcs)
        if self.series_eval is not None:
            pool += list(self.series_eval.names) + list(self.series_eval.funcs)
        close = difflib.get_close_matches(name, pool, n=3)
        return f"Unknown {kind} {name!r} in {text!r}" + (f"; did you mean {', '.join(close)}?" if close else "")


def last(x):
    """Latest value of a Series/array; scalars pass through."""
    if isinstance(x, pd.Series):
        return x.iloc[-1] if len(x) else np.nan
    if isinstance(x, np.ndarray):
        return x[-1] if x.size else np.nan
    return x


def truthy(x) -> bool:
    """Final verdict of a stock-level condition: its value on the latest bar."""
    v = last(x)
    return bool(v) if v == v else False
