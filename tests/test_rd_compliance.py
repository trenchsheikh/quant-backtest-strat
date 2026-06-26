"""RD net-directional compliance — MTF filter, reconcile notional, risk gate."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from live.brain.reconcile import reconcile, _position_notional_usd
from live.brain.risk import LiveRiskEngine, NET_DIR_WARN
from live.brain.rd_monitor import NET_DIR_ENTRY_BLOCK
from live.state.store import LivePosition


def _pos(symbol: str, side: int, notional: float, sleeve: str = "mtf") -> LivePosition:
    return LivePosition(
        ticket=1,
        symbol=symbol,
        side=side,
        lots=1.0,
        notional_usd=notional,
        entry_price=1.0,
        entry_ts=datetime.now(timezone.utc),
        sleeve=sleeve,
    )


def test_reconcile_orphan_notional_nonzero():
    saved: list[LivePosition] = []
    mt5 = [{
        "ticket": 99,
        "symbol": "GBPUSD",
        "type": 1,  # sell
        "lots": 2.0,
        "price_open": 1.25,
    }]
    _, positions = reconcile(saved, mt5)
    assert len(positions) == 1
    assert positions[0].notional_usd > 0
    assert positions[0].sleeve == "unknown"
    assert positions[0].side == -1


def test_reconcile_refreshes_matched_notional():
    saved = [_pos("BTCUSD", 1, 0.0, sleeve="mtf")]
    saved[0] = saved[0].model_copy(update={"ticket": 10, "lots": 0.5})
    mt5 = [{
        "ticket": 10,
        "symbol": "BTCUSD",
        "type": 0,
        "lots": 0.5,
        "price_current": 100_000.0,
    }]
    _, positions = reconcile(saved, mt5)
    assert positions[0].notional_usd == pytest.approx(
        _position_notional_usd("BTCUSD", 0.5, 100_000.0)
    )


def test_hard_block_incomplete_pair_skips_net_dir_for_css_leg():
    """CSS leg 1 alone is 100% net-dir; allow_incomplete_pair waives until leg 2."""
    risk = LiveRiskEngine(init_equity=1_000_000)
    block = risk.hard_block(
        [],
        [("EURUSD", 1, 750_000.0)],
        1_000_000,
        allow_incomplete_pair=True,
        allow_incomplete_basket=False,
        allow_directional_sleeve=False,
    )
    assert block is None or "RD_NET_DIR" not in block


def test_hard_block_basket_alone_still_enforces_net_dir():
    """MTF basket approval must not waive net-dir without directional_sleeve."""
    risk = LiveRiskEngine(init_equity=1_000_000)
    block = risk.hard_block(
        [],
        [("BTCUSD", 1, 2_000_000.0)],
        1_000_000,
        allow_incomplete_pair=False,
        allow_incomplete_basket=True,
        allow_directional_sleeve=False,
    )
    assert block is not None
    assert "RD_NET_DIR" in block


def test_hard_block_css_full_pair_neutral_net_dir():
    risk = LiveRiskEngine(init_equity=1_000_000)
    block = risk.hard_block(
        [],
        [("EURUSD", 1, 750_000.0), ("USDCHF", -1, 750_000.0)],
        1_000_000,
    )
    assert block is None or "RD_NET_DIR" not in block


def test_hard_block_basket_does_not_skip_net_dir():
    risk = LiveRiskEngine(init_equity=1_000_000)
    # Two same-direction legs → 100% net dir
    legs = [("BTCUSD", 1, 2_000_000.0), ("ETHUSD", 1, 2_000_000.0)]
    block = risk.hard_block(
        [],
        legs,
        1_000_000,
        allow_incomplete_basket=True,
        allow_directional_sleeve=False,
    )
    assert block is not None
    assert "RD_NET_DIR" in block


def test_hard_block_directional_sleeve_still_skips_net_dir():
    risk = LiveRiskEngine(init_equity=1_000_000)
    legs = [("BTCUSD", 1, 2_000_000.0), ("ETHUSD", 1, 2_000_000.0)]
    block = risk.hard_block(
        [],
        legs,
        1_000_000,
        allow_incomplete_basket=True,
        allow_directional_sleeve=True,
    )
    assert block is None or "RD_NET_DIR" not in block


def test_mtf_allowed_after_css_hedge_same_bar():
    """MTF capped on flat book but allowed once CSS hedge is on book."""
    from momq.signals import SignalKind, TradeIntent
    from live.brain.loop import BrainLoop
    from live.config import Settings
    from live.state.store import LiveState

    loop = BrainLoop(Settings())
    loop._used_margin = 0.0
    equity = 1_200_000.0
    mtf = [
        TradeIntent(SignalKind.MTF, ("BTCUSD",), (1,), 2_000_000.0, priority=7.0, reason="t"),
        TradeIntent(SignalKind.MTF, ("ETHUSD",), (1,), 2_000_000.0, priority=7.0, reason="t"),
    ]
    flat = LiveState(round_kind="finals", positions=[])
    hedged = LiveState(
        round_kind="finals",
        positions=[
            _pos("AUDUSD", -1, 1_500_000.0, sleeve="css"),
            _pos("USDCAD", 1, 1_500_000.0, sleeve="css"),
        ],
    )
    mids = {
        "BTCUSD": 100_000.0,
        "ETHUSD": 3_000.0,
        "SOLUSD": 150.0,
    }
    assert loop._filter_mtf_rd_safe(mtf, flat, equity, mids) == []
    kept = loop._filter_mtf_rd_safe(mtf, hedged, equity, mids)
    assert len(kept) == 2


def test_filter_mtf_rd_safe_caps_third_leg():
    from momq.signals import SignalKind, TradeIntent
    from live.brain.loop import BrainLoop
    from live.config import Settings
    from live.state.store import LiveState

    loop = BrainLoop(Settings())
    loop._used_margin = 0.0
    state = LiveState(
        round_kind="finals",
        positions=[
            _pos("BTCUSD", 1, 2_000_000.0),
            _pos("ETHUSD", 1, 2_000_000.0),
        ],
    )
    per_leg = 2_000_000.0
    mtf = [
        TradeIntent(SignalKind.MTF, ("SOLUSD",), (1,), per_leg, priority=7.0, reason="t"),
    ]
    mids = {"SOLUSD": 150.0}
    kept = loop._filter_mtf_rd_safe(mtf, state, 1_000_000.0, mids)
    assert kept == []


def test_net_dir_warn_threshold():
    assert NET_DIR_WARN == 0.90
    assert NET_DIR_ENTRY_BLOCK == 0.92
