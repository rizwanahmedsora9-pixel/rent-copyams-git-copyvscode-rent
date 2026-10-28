"""Balance sheet statement (book, whole paisa)."""
from __future__ import annotations

from app.services.ledger import query as q


def build(*, as_of=None) -> dict:
    return q.balance_sheet(as_of=as_of)
