"""Unit: OCO batch rollback when one leg fails."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from ingest.oco_rollback import rollback_incomplete_oco_batch


def _sig(api_id, peer_id, source="orb"):
    s = MagicMock()
    s.api_signal_id = api_id
    s.oco_peer_api_id = peer_id
    s.source = source
    return s


def test_rollback_cancels_submitted_when_peer_errors():
    db = MagicMock()
    db.get_signal_by_api_id.return_value = None

    signals = [
        _sig("orb:preplace:COIN:2026-06-29:1:LONG", "orb:preplace:COIN:2026-06-29:1:SHORT"),
        _sig("orb:preplace:COIN:2026-06-29:1:SHORT", "orb:preplace:COIN:2026-06-29:1:LONG"),
    ]
    details = [
        {"action": "duplicate"},
        {"action": "error"},
    ]

    def _get_row(source, api_id):
        if api_id.endswith(":LONG"):
            return {"id": 1, "status": "submitted", "symbol": "COINUSDT", "result_json": "{}"}
        return None

    db.get_signal_by_api_id.side_effect = _get_row

    with patch("trading.entry_cancel.cancel_pending_entry_by_api_id", return_value=True) as cancel:
        n = rollback_incomplete_oco_batch(signals, details, db)

    assert n == 1
    cancel.assert_called_once()
    assert cancel.call_args[0][1].endswith(":LONG")


def test_rollback_skips_when_peer_traded():
    db = MagicMock()
    signals = [
        _sig("a:L", "a:S"),
        _sig("a:S", "a:L"),
    ]
    details = [{"action": "traded"}, {"action": "cancelled"}]

    with patch("trading.entry_cancel.cancel_pending_entry_by_api_id") as cancel:
        n = rollback_incomplete_oco_batch(signals, details, db)

    assert n == 0
    cancel.assert_not_called()
