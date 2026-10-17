"""Deterministic opportunity-highlight overlay: transparent rules, no AI, no trading.

Every highlight comes from a configured rule or a documented formula; the same data and
settings always give the same output. See docs/OVERLAY.md for every formula.
"""
from .config import OverlayConfig, OverlayConfigError, load_overlay_config
from .engine import OverlayScan, frames_from_prices, results_frame, scan

__all__ = ["OverlayConfig", "OverlayConfigError", "OverlayScan", "frames_from_prices", "load_overlay_config",
           "results_frame", "scan"]
