-- 0003 — the book: immutability + indexes for the journal.
--
-- Tables themselves are declared by ``models/ledger.py`` and created by
-- ``db.create_all()``; this migration only adds what the ORM cannot express:
--
--   * ``journal_line`` rows may never be updated or deleted (append-only),
--   * ``journal_entry`` rows may never be deleted (reversal is the correction),
--   * covering indexes for the report queries.
--
-- Every statement is safe to re-run (``IF NOT EXISTS`` / trigger replace).

CREATE INDEX IF NOT EXISTS ix_journal_entry_period ON journal_entry(period_id);
CREATE INDEX IF NOT EXISTS ix_journal_entry_event ON journal_entry(source_type, source_id, event);
CREATE INDEX IF NOT EXISTS ix_journal_line_entry_account ON journal_line(entry_id, gl_account_id);
CREATE INDEX IF NOT EXISTS ix_journal_line_account_side ON journal_line(gl_account_id, debit_minor, credit_minor);

DROP TRIGGER IF EXISTS trg_journal_line_no_update;
CREATE TRIGGER trg_journal_line_no_update
BEFORE UPDATE ON journal_line
BEGIN
    SELECT RAISE(ABORT, 'journal_line is immutable — post a reversal instead');
END;

DROP TRIGGER IF EXISTS trg_journal_line_no_delete;
CREATE TRIGGER trg_journal_line_no_delete
BEFORE DELETE ON journal_line
BEGIN
    SELECT RAISE(ABORT, 'journal_line is immutable — post a reversal instead');
END;

DROP TRIGGER IF EXISTS trg_journal_entry_no_delete;
CREATE TRIGGER trg_journal_entry_no_delete
BEFORE DELETE ON journal_entry
BEGIN
    SELECT RAISE(ABORT, 'journal_entry is immutable — post a reversal instead');
END;
