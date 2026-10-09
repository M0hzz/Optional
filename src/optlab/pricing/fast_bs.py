"""Vectorized Black-Scholes-Merton in NumPy.

QuantLib (see engine.py) is the source of truth for single-trade pricing, American
exercise and implied-vol solving. This module exists for speed: backtests and
synthetic data need millions of European prices, which NumPy does in milliseconds.
"""
from __future__ import annotations

import numpy as np
from scipy.special import ndtr

SQRT_2PI = np.sqrt(2.0 * np.pi)


def _d1d2(s, k, t, r, q, vol):
    t = np.maximum(t, 1e-8)
    vol = np.maximum(vol, 1e-8)
    vsqt = vol * np.sqrt(t)
    d1 = (np.log(s / k) + (r - q + 0.5 * vol * vol) * t) / vsqt
    return d1, d1 - vsqt


def bs_price(s, k, t, r, q, vol, is_call):
    """Price. `is_call` may be a bool or boolean array. t in years."""
    s, k, t, vol = map(np.asarray, (s, k, t, vol))
    d1, d2 = _d1d2(s, k, t, r, q, vol)
    dq, dr = np.exp(-q * t), np.exp(-r * t)
    call = s * dq * ndtr(d1) - k * dr * ndtr(d2)
    put = k * dr * ndtr(-d2) - s * dq * ndtr(-d1)
    px = np.where(is_call, call, put)
    intrinsic = np.where(is_call, np.maximum(s - k, 0.0), np.maximum(k - s, 0.0))
    return np.where(t <= 0, intrinsic, px)


def bs_delta(s, k, t, r, q, vol, is_call):
    s, k, t, vol = map(np.asarray, (s, k, t, vol))
    d1, _ = _d1d2(s, k, t, r, q, vol)
    dq = np.exp(-q * t)
    return np.where(is_call, dq * ndtr(d1), dq * (ndtr(d1) - 1.0))


def bs_greeks(s, k, t, r, q, vol, is_call) -> dict:
    """Delta, gamma, vega (per 1 vol point), theta (per calendar day)."""
    s, k, t, vol = map(np.asarray, (s, k, t, vol))
    t = np.maximum(t, 1e-8)
    d1, d2 = _d1d2(s, k, t, r, q, vol)
    dq, dr = np.exp(-q * t), np.exp(-r * t)
    pdf = np.exp(-0.5 * d1 * d1) / SQRT_2PI
    gamma = dq * pdf / (s * vol * np.sqrt(t))
    vega = s * dq * pdf * np.sqrt(t) / 100.0
    common = -s * dq * pdf * vol / (2 * np.sqrt(t))
    theta_c = common - r * k * dr * ndtr(d2) + q * s * dq * ndtr(d1)
    theta_p = common + r * k * dr * ndtr(-d2) - q * s * dq * ndtr(-d1)
    return {
        "delta": bs_delta(s, k, t, r, q, vol, is_call),
        "gamma": gamma,
        "vega": vega,
        "theta": np.where(is_call, theta_c, theta_p) / 365.0,
    }


def strike_for_delta(s, t, r, q, vol, target_delta, is_call):
    """Closed-form strike whose BS delta equals target_delta (use + for calls, - for puts)."""
    from scipy.special import ndtri

    s, t, vol = map(np.asarray, (s, t, vol))
    t = np.maximum(t, 1e-8)
    a = np.abs(target_delta) / np.exp(-q * t)
    d1 = np.where(is_call, ndtri(np.clip(a, 1e-6, 1 - 1e-6)), -ndtri(np.clip(a, 1e-6, 1 - 1e-6)))
    return s * np.exp(-(d1 * vol * np.sqrt(t)) + (r - q + 0.5 * vol * vol) * t)
