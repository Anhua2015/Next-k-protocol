"""Characterization: expire_open_positions force-closes past-deadline rows."""
from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.characterization


def test_expire_closes_past_deadline(seeded_config, mock_binance):
    pos_id = seeded_config.insert_position(
        signal_log_id=None, symbol="BTCUSDT", side="LONG",
        entry_order_id="11111111", sl_order_id="33333333", tp_order_id="33333334",
        entry_price=67250.5, sl_price=66500.0, tp_price=68500.0,
        quantity=0.012, notional_usdt=100.0, leverage=10,
        opened_at="2026-05-26T00:00:00+00:00", play="PLAY01", source="zct_vwap",
    )
    import sqlite3
    conn = sqlite3.connect(str(seeded_config.DB_PATH))
    conn.execute(
        "UPDATE positions SET expire_at='1999-01-01T00:00:00+00:00' WHERE id=?",
        (pos_id,),
    )
    conn.commit()
    conn.close()
    mock_binance.all()
    sys.modules.pop("trader", None)
    from trader import expire_open_positions
    expire_open_positions()
    assert len(seeded_config.get_open_positions()) == 0


def test_expire_skips_not_yet_due(seeded_config, mock_binance):
    pos_id = seeded_config.insert_position(
        signal_log_id=None, symbol="BTCUSDT", side="LONG",
        entry_order_id="11111111", sl_order_id="33333333", tp_order_id="33333334",
        entry_price=67250.5, sl_price=66500.0, tp_price=68500.0,
        quantity=0.012, notional_usdt=100.0, leverage=10,
        opened_at="2026-05-26T00:00:00+00:00", play="PLAY01", source="zct_vwap",
    )
    import sqlite3
    conn = sqlite3.connect(str(seeded_config.DB_PATH))
    conn.execute(
        "UPDATE positions SET expire_at='2099-01-01T00:00:00+00:00' WHERE id=?",
        (pos_id,),
    )
    conn.commit()
    conn.close()
    sys.modules.pop("trader", None)
    from trader import expire_open_positions
    expire_open_positions()
    open_rows = seeded_config.get_open_positions()
    assert len(open_rows) == 1
