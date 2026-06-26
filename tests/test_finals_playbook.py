"""Finals 36h playbook — broker sizing, BTC-only MTF, manual-close cooldowns."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from live.brain.manual_close import apply_manual_close_policy, classify_manual_close
from live.bridge.symbol_specs import SymbolSpec
from live.config import FINALS_MTF_SYMBOLS
from live.state.store import LivePosition, LiveState


def _pos(
    symbol: str = "BTCUSD",
    side: int = 1,
    notional: float = 500_000.0,
    sleeve: str = "mtf",
    pair_id: str = "",
    peak_mtm_pct: float = 0.0,
) -> LivePosition:
    return LivePosition(
        ticket=42,
        symbol=symbol,
        side=side,
        lots=1.0,
        notional_usd=notional,
        entry_price=100_000.0,
        entry_ts=datetime.now(timezone.utc),
        sleeve=sleeve,
        pair_id=pair_id,
        peak_mtm_pct=peak_mtm_pct,
    )


def test_symbol_spec_caps_crypto_volume_max():
    spec = SymbolSpec(
        symbol="ETHUSD",
        contract_size=1.0,
        volume_min=0.01,
        volume_max=25.0,
        volume_step=0.01,
    )
    price = 3_000.0
    target_notional = 3_000_000.0
    lots = spec.notional_to_lots(target_notional, price)
    assert lots == pytest.approx(25.0)
    assert spec.lots_to_notional(lots, price) == pytest.approx(75_000.0)
    assert spec.volume_was_capped(target_notional / (spec.contract_size * price))


def test_finals_mtf_symbols_btc_and_metals():
    assert FINALS_MTF_SYMBOLS == ["BTCUSD", "XAUUSD", "XAGUSD"]


def test_manual_close_loss_sets_mtf_cooldown():
    state = LiveState()
    pos = _pos(sleeve="mtf", notional=1_000_000.0)
    reason = apply_manual_close_policy(
        state, pos, pnl=-8_000.0, equity=1_000_000.0, bar_i=10, mtf_stop_cooldown_bars=12
    )
    assert reason == "manual_close_loss"
    assert state.mtf_cooldown["BTCUSD"] == 22


def test_manual_close_profit_blocks_mtf_side():
    state = LiveState()
    pos = _pos(sleeve="mtf", peak_mtm_pct=0.01)
    reason = apply_manual_close_policy(
        state, pos, pnl=4_000.0, equity=1_000_000.0, bar_i=5
    )
    assert reason == "manual_close_profit"
    assert state.mtf_side_block["BTCUSD:1"] == 13


def test_manual_close_css_loss_cools_pair():
    state = LiveState()
    pos = _pos(symbol="EURUSD", sleeve="css", pair_id="EURUSD-GBPUSD")
    reason = apply_manual_close_policy(
        state, pos, pnl=-6_000.0, equity=1_000_000.0, bar_i=3
    )
    assert reason == "manual_close_loss"
    assert state.css_pair_cooldown["EURUSD-GBPUSD"] == 11


def test_classify_scratch_is_neutral():
    pos = _pos(notional=500_000.0)
    assert classify_manual_close(pos, pnl=200.0, equity=1_000_000.0) == "manual_close_scratch"
