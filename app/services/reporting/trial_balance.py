"""Trial balance statement (book, whole paisa)."""
from __future__ import annotations

from app.services.ledger import query as q


def build(*, start=None, end=None, as_of=None) -> dict:
    return q.trial_balance(start=start, end=end, as_of=as_of)
