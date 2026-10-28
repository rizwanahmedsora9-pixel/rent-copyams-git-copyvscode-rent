"""The book — chart of accounts, journal, periods, and the ledger version.

This is the storage half of the accounting master plan (§6 S1, §7 S2, §16 S11).
Everything here is additive: no existing table changes, and no existing page
has to know these tables exist until it is wired to a read model.

The three rules the tables enforce:

* **One entry per event.** ``journal_entry.idempotency_key`` is unique, so a
  retried save, a double-clicked submit or a replay pass cannot post the same
  document event twice.
* **Two sides.** Every entry stores equal debit and credit totals; the posting
  engine refuses to write an unbalanced entry and ``journal_line`` rejects
  negative amounts or a line with both sides filled.
* **Nothing is edited.** ``journal_line`` is append-only at the database level
  (see ``app/migrations/0003_create_ledger_tables.sql``); a correction is a
  mirror entry that references the one it reverses.
"""
from __future__ import annotations

from .__base import *  # noqa: F401,F403  (db, pk_model_now, …)
from .helpers import *  # noqa: F401,F403


class GLAccount(db.Model):
    """One line of the chart of accounts.

    ``linked_account_id`` maps a cash/bank/wallet master row (``account``) to
    its general-ledger account, so a cash movement and its journal line can
    never drift apart.  ``is_control`` marks the accounts whose balance must
    equal a supporting list (receivables, payables, inventory, driver payable).
    """

    __tablename__ = 'gl_account'
    __table_args__ = (
        UniqueConstraint('tenant_id', 'code', name='uq_gl_account_code'),
    )

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=True, index=True)
    code = db.Column(db.String(20), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    # asset | liability | equity | revenue | cost | expense | other_income | other_loss
    account_class = db.Column('class', db.String(20), nullable=False, index=True)
    subcategory = db.Column(db.String(80), nullable=True, index=True)
    normal_side = db.Column(db.String(6), nullable=False, default='debit')  # debit|credit
    is_control = db.Column(db.Boolean, default=False, index=True)
    is_cash = db.Column(db.Boolean, default=False, index=True)
    is_system = db.Column(db.Boolean, default=True, index=True)
    linked_account_id = db.Column(
        db.Integer, db.ForeignKey('account.id'), nullable=True, index=True
    )
    is_active = db.Column(db.Boolean, default=True, index=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<GLAccount {self.code} {self.name}>"


class AccountingPeriod(db.Model):
    """A calendar month with a controlled lifecycle.

    ``open`` → postings allowed; ``soft_closed`` → postings allowed but flagged
    as adjustments; ``locked`` → nothing posts, corrections go to the next open
    period as reversals.
    """

    __tablename__ = 'accounting_period'
    __table_args__ = (
        UniqueConstraint('tenant_id', 'year', 'month', name='uq_accounting_period'),
    )

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=True, index=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False, index=True)
    start_date = db.Column(db.Date, nullable=False, index=True)
    end_date = db.Column(db.Date, nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='open', index=True)
    closed_result_minor = db.Column(db.BigInteger, nullable=True)
    locked_by = db.Column(db.String(80), nullable=True)
    locked_at = db.Column(db.DateTime, nullable=True)
    reopened_by = db.Column(db.String(80), nullable=True)
    reopened_at = db.Column(db.DateTime, nullable=True)
    note = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)
    updated_at = db.Column(db.DateTime, default=pk_model_now, onupdate=pk_model_now)


class JournalEntry(db.Model):
    """The header of one balanced, immutable posting."""

    __tablename__ = 'journal_entry'
    __table_args__ = (
        UniqueConstraint('idempotency_key', name='uq_journal_entry_idempotency'),
        db.Index('ix_journal_entry_source', 'source_type', 'source_id'),
        db.Index('ix_journal_entry_date', 'entry_date'),
    )

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=True, index=True)
    entry_no = db.Column(db.String(40), nullable=False, index=True)
    entry_date = db.Column(db.Date, nullable=False, index=True)
    period_id = db.Column(
        db.Integer, db.ForeignKey('accounting_period.id'), nullable=True, index=True
    )
    event = db.Column(db.String(40), nullable=False, index=True)
    source_type = db.Column(db.String(60), nullable=False, index=True)
    source_id = db.Column(db.Integer, nullable=False, index=True)
    reference = db.Column(db.String(80), nullable=True, index=True)
    description = db.Column(db.String(300), nullable=True)
    revision = db.Column(db.Integer, default=1, nullable=False)
    payload_hash = db.Column(db.String(64), nullable=True, index=True)
    is_reversal_of = db.Column(
        db.Integer, db.ForeignKey('journal_entry.id'), nullable=True, index=True
    )
    is_adjustment = db.Column(db.Boolean, default=False, index=True)
    status = db.Column(db.String(20), default='posted', nullable=False, index=True)
    reversed_at = db.Column(db.DateTime, nullable=True)
    reversed_by = db.Column(db.String(80), nullable=True)
    reversal_reason = db.Column(db.String(300), nullable=True)
    idempotency_key = db.Column(db.String(140), nullable=False)
    created_by = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)

    lines = db.relationship(
        'JournalLine',
        backref='entry',
        lazy='selectin',
        cascade='all, delete-orphan',
        order_by='JournalLine.line_no',
    )
    period = db.relationship('AccountingPeriod')

    @property
    def total_minor(self) -> int:
        return sum(int(line.debit_minor or 0) for line in (self.lines or []))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<JournalEntry {self.entry_no} {self.event} {self.source_type}#{self.source_id}>"


class JournalLine(db.Model):
    """One side of an entry, with the dimensions the reports group by."""

    __tablename__ = 'journal_line'
    __table_args__ = (
        db.Index('ix_journal_line_entry', 'entry_id'),
        db.Index('ix_journal_line_account', 'gl_account_id'),
        db.Index('ix_journal_line_client', 'client_id'),
        db.Index('ix_journal_line_supplier', 'supplier_id'),
        db.Index('ix_journal_line_driver', 'delivery_person_id'),
        db.Index('ix_journal_line_document', 'document_type', 'document_id'),
        db.CheckConstraint(
            'debit_minor = 0 OR credit_minor = 0', name='ck_journal_line_one_side'
        ),
        db.CheckConstraint(
            'debit_minor >= 0 AND credit_minor >= 0', name='ck_journal_line_positive'
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(
        db.Integer, db.ForeignKey('journal_entry.id'), nullable=False, index=True
    )
    line_no = db.Column(db.Integer, nullable=False, default=1)
    gl_account_id = db.Column(
        db.Integer, db.ForeignKey('gl_account.id'), nullable=False, index=True
    )
    debit_minor = db.Column(db.BigInteger, nullable=False, default=0)
    credit_minor = db.Column(db.BigInteger, nullable=False, default=0)
    client_id = db.Column(db.Integer, nullable=True, index=True)
    supplier_id = db.Column(db.Integer, nullable=True, index=True)
    delivery_person_id = db.Column(db.Integer, nullable=True, index=True)
    material_name = db.Column(db.String(120), nullable=True, index=True)
    service_name = db.Column(db.String(120), nullable=True, index=True)
    document_type = db.Column(db.String(60), nullable=True, index=True)
    document_id = db.Column(db.Integer, nullable=True, index=True)
    branch_id = db.Column(db.Integer, nullable=True, index=True)
    memo = db.Column(db.String(300), nullable=True)

    account = db.relationship('GLAccount')

    @property
    def amount_minor(self) -> int:
        return int(self.debit_minor or 0) + int(self.credit_minor or 0)

    @property
    def side(self) -> str:
        return 'debit' if int(self.debit_minor or 0) else 'credit'


class LedgerVersion(db.Model):
    """Single-row counter bumped on every posting and reversal.

    Read models cache against this number, so a page refreshed after a new
    document shows the new totals without recomputing the whole history and
    without a restart.
    """

    __tablename__ = 'ledger_version'

    id = db.Column(db.Integer, primary_key=True)
    version = db.Column(db.BigInteger, nullable=False, default=0)
    updated_at = db.Column(
        db.DateTime, default=pk_model_now, onupdate=pk_model_now, nullable=True
    )


class LedgerSyncEvent(db.Model):
    """Audit trail for the sync layer (post / reverse / suppress / failure)."""

    __tablename__ = 'ledger_sync_event'

    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(20), nullable=False, index=True)
    source_type = db.Column(db.String(60), nullable=True, index=True)
    source_id = db.Column(db.Integer, nullable=True, index=True)
    event = db.Column(db.String(40), nullable=True, index=True)
    detail = db.Column(db.String(500), nullable=True)
    actor = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, default=pk_model_now, index=True)
