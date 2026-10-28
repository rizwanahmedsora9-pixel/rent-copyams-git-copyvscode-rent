"""Profit & loss statement (book, whole paisa)."""
from __future__ import annotations

from app.services.ledger import query as q


def build(*, start=None, end=None) -> dict:
    return q.profit_and_loss(start=start, end=end)
