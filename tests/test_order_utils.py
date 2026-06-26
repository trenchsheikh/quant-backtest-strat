"""Tests for MT5 order volume/deviation helpers."""
from __future__ import annotations

from types import SimpleNamespace

from live.bridge.order_utils import normalize_volume, order_deviation_points


def test_normalize_volume_respects_step_and_max() -> None:
    info = SimpleNamespace(volume_min=0.01, volume_max=10.0, volume_step=0.01)
    assert normalize_volume(6.614, info) == 6.61
    assert normalize_volume(12.0, info) == 10.0


def test_order_deviation_wider_for_crypto() -> None:
    info = SimpleNamespace(point=0.01, ask=60_000.0)
    pts = order_deviation_points("BTCUSD", info, 60_000.0)
    assert pts >= 2_000
