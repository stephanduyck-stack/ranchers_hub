-- PostgreSQL port of the accounting integrity layer (acc_001..acc_006).
-- Installed and verified at every boot by app.py when the backend is
-- PostgreSQL; the SQLite originals remain the source of the semantics.
--
-- Idempotent: CREATE OR REPLACE FUNCTION + DROP TRIGGER IF EXISTS.
-- Trigger names MUST match EXPECTED_ACC_TRIGGERS in app.py.
--
-- Translation notes:
--   SQLite RAISE(ABORT, msg)      -> RAISE EXCEPTION
--   SQLite "IS" (null-safe =)     -> IS NOT DISTINCT FROM
--   posted = 1 (integer)          -> posted (boolean)
--   Subqueries in WHEN clauses    -> moved into the trigger function
--     (PG WHEN may only reference OLD/NEW).

-- Shared unconditional abort: message arrives via TG_ARGV[0].
CREATE OR REPLACE FUNCTION acc_abort() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '%', TG_ARGV[0];
END $$ LANGUAGE plpgsql;

-- ============================================================ acc_001: ledger
-- 1. Journal lines are append-only.
DROP TRIGGER IF EXISTS acc_line_no_update ON acc_journal_line;
CREATE TRIGGER acc_line_no_update
BEFORE UPDATE ON acc_journal_line
FOR EACH ROW EXECUTE FUNCTION
  acc_abort('ledger: journal lines are append-only; post a reversing entry');

-- 2. Lines of a posted entry cannot be deleted (parent lookup -> function).
CREATE OR REPLACE FUNCTION acc_line_no_delete_fn() RETURNS trigger AS $$
BEGIN
  IF (SELECT posted FROM acc_journal_entry WHERE id = OLD.entry_id) THEN
    RAISE EXCEPTION 'ledger: lines of a posted entry cannot be deleted';
  END IF;
  RETURN OLD;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS acc_line_no_delete ON acc_journal_line;
CREATE TRIGGER acc_line_no_delete
BEFORE DELETE ON acc_journal_line
FOR EACH ROW EXECUTE FUNCTION acc_line_no_delete_fn();

-- 3. No line may be added to an already-posted entry.
CREATE OR REPLACE FUNCTION acc_line_no_insert_posted_fn() RETURNS trigger AS $$
BEGIN
  IF (SELECT posted FROM acc_journal_entry WHERE id = NEW.entry_id) THEN
    RAISE EXCEPTION 'ledger: entry is already posted';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS acc_line_no_insert_posted ON acc_journal_line;
CREATE TRIGGER acc_line_no_insert_posted
BEFORE INSERT ON acc_journal_line
FOR EACH ROW EXECUTE FUNCTION acc_line_no_insert_posted_fn();

-- 4. Line shape: non-negative, exactly one positive side.
DROP TRIGGER IF EXISTS acc_line_shape ON acc_journal_line;
CREATE TRIGGER acc_line_shape
BEFORE INSERT ON acc_journal_line
FOR EACH ROW
WHEN (NEW.debit < 0 OR NEW.credit < 0
      OR (NEW.debit > 0 AND NEW.credit > 0)
      OR (NEW.debit = 0 AND NEW.credit = 0))
EXECUTE FUNCTION acc_abort('ledger: a line carries exactly one positive side');

-- 5. THE BALANCE GUARANTEE: posting only when debits = credits, >= 2 lines.
CREATE OR REPLACE FUNCTION acc_entry_post_check_fn() RETURNS trigger AS $$
BEGIN
  IF (SELECT COALESCE(SUM(debit), 0) - COALESCE(SUM(credit), 0)
        FROM acc_journal_line WHERE entry_id = NEW.id) <> 0 THEN
    RAISE EXCEPTION 'ledger: entry does not balance (debits != credits)';
  END IF;
  IF (SELECT COUNT(*) FROM acc_journal_line WHERE entry_id = NEW.id) < 2 THEN
    RAISE EXCEPTION 'ledger: an entry needs at least two lines';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS acc_entry_post_check ON acc_journal_entry;
CREATE TRIGGER acc_entry_post_check
BEFORE UPDATE OF posted ON acc_journal_entry
FOR EACH ROW
WHEN (NEW.posted AND NOT OLD.posted)
EXECUTE FUNCTION acc_entry_post_check_fn();

-- 6. Posted entries are immutable.
DROP TRIGGER IF EXISTS acc_entry_no_edit_posted ON acc_journal_entry;
CREATE TRIGGER acc_entry_no_edit_posted
BEFORE UPDATE ON acc_journal_entry
FOR EACH ROW
WHEN (OLD.posted
      AND (NOT NEW.posted
           OR NEW.entry_date  IS DISTINCT FROM OLD.entry_date
           OR NEW.memo        IS DISTINCT FROM OLD.memo
           OR NEW.source_type IS DISTINCT FROM OLD.source_type
           OR NEW.source_id   IS DISTINCT FROM OLD.source_id
           OR NEW.entry_no    IS DISTINCT FROM OLD.entry_no))
EXECUTE FUNCTION
  acc_abort('ledger: posted entries are immutable; post a reversing entry');

DROP TRIGGER IF EXISTS acc_entry_no_delete ON acc_journal_entry;
CREATE TRIGGER acc_entry_no_delete
BEFORE DELETE ON acc_journal_entry
FOR EACH ROW
WHEN (OLD.posted)
EXECUTE FUNCTION acc_abort('ledger: posted entries cannot be deleted');

CREATE INDEX IF NOT EXISTS ix_acc_journal_line_entry_id   ON acc_journal_line (entry_id);
CREATE INDEX IF NOT EXISTS ix_acc_journal_line_account_id ON acc_journal_line (account_id);
CREATE INDEX IF NOT EXISTS ix_acc_journal_entry_date      ON acc_journal_entry (entry_date);
CREATE INDEX IF NOT EXISTS ix_acc_journal_entry_source    ON acc_journal_entry (source_type, source_id);

-- ================================================= acc_002/003: inventory
-- Valued movements append-only in every ECONOMIC column; the one allowed
-- update is linking journal_entry_id (NULL -> value) with all else unchanged.
DROP TRIGGER IF EXISTS acc_inv_mv_no_update ON acc_inv_movement;
CREATE TRIGGER acc_inv_mv_no_update
BEFORE UPDATE ON acc_inv_movement
FOR EACH ROW
WHEN (NOT (OLD.journal_entry_id IS NULL
           AND NEW.journal_entry_id IS NOT NULL
           AND NEW.item_id     IS NOT DISTINCT FROM OLD.item_id
           AND NEW.kind        IS NOT DISTINCT FROM OLD.kind
           AND NEW.qty         IS NOT DISTINCT FROM OLD.qty
           AND NEW.value_ugx   IS NOT DISTINCT FROM OLD.value_ugx
           AND NEW.qty_after   IS NOT DISTINCT FROM OLD.qty_after
           AND NEW.value_after IS NOT DISTINCT FROM OLD.value_after
           AND NEW.order_id    IS NOT DISTINCT FROM OLD.order_id
           AND NEW.created_at  IS NOT DISTINCT FROM OLD.created_at))
EXECUTE FUNCTION
  acc_abort('inventory: valued movements are append-only; post an adjustment');

DROP TRIGGER IF EXISTS acc_inv_mv_no_delete ON acc_inv_movement;
CREATE TRIGGER acc_inv_mv_no_delete
BEFORE DELETE ON acc_inv_movement
FOR EACH ROW EXECUTE FUNCTION
  acc_abort('inventory: valued movements cannot be deleted');

CREATE INDEX IF NOT EXISTS ix_acc_inv_movement_item_id ON acc_inv_movement (item_id);
CREATE INDEX IF NOT EXISTS ix_acc_inv_movement_journal ON acc_inv_movement (journal_entry_id);
CREATE INDEX IF NOT EXISTS ix_acc_inv_movement_kind    ON acc_inv_movement (kind);
CREATE INDEX IF NOT EXISTS ix_acc_item_stage           ON acc_item (stage);

-- ======================================================= acc_003: invoices
DROP TRIGGER IF EXISTS acc_invoice_freeze_money ON acc_invoice;
CREATE TRIGGER acc_invoice_freeze_money
BEFORE UPDATE ON acc_invoice
FOR EACH ROW
WHEN (OLD.status IN ('posted', 'credited')
      AND (NEW.net_minor    IS DISTINCT FROM OLD.net_minor
           OR NEW.vat_minor   IS DISTINCT FROM OLD.vat_minor
           OR NEW.gross_minor IS DISTINCT FROM OLD.gross_minor
           OR NEW.currency    IS DISTINCT FROM OLD.currency
           OR NEW.invoice_no  IS DISTINCT FROM OLD.invoice_no
           OR NEW.order_id    IS DISTINCT FROM OLD.order_id
           OR NEW.journal_entry_id IS DISTINCT FROM OLD.journal_entry_id))
EXECUTE FUNCTION
  acc_abort('invoice: posted amounts are immutable; use a credit note');

DROP TRIGGER IF EXISTS acc_invoice_no_delete ON acc_invoice;
CREATE TRIGGER acc_invoice_no_delete
BEFORE DELETE ON acc_invoice
FOR EACH ROW EXECUTE FUNCTION
  acc_abort('invoice: fiscal documents cannot be deleted');

DROP TRIGGER IF EXISTS acc_invoice_line_no_update ON acc_invoice_line;
CREATE TRIGGER acc_invoice_line_no_update
BEFORE UPDATE ON acc_invoice_line
FOR EACH ROW EXECUTE FUNCTION acc_abort('invoice lines are immutable');

DROP TRIGGER IF EXISTS acc_invoice_line_no_delete ON acc_invoice_line;
CREATE TRIGGER acc_invoice_line_no_delete
BEFORE DELETE ON acc_invoice_line
FOR EACH ROW EXECUTE FUNCTION acc_abort('invoice lines cannot be deleted');

CREATE INDEX IF NOT EXISTS ix_acc_invoice_order   ON acc_invoice (order_id);
CREATE INDEX IF NOT EXISTS ix_acc_invoice_efris   ON acc_invoice (efris_status);
CREATE INDEX IF NOT EXISTS ix_acc_invoice_date    ON acc_invoice (invoice_date);
CREATE INDEX IF NOT EXISTS ix_acc_efris_queue_due ON acc_efris_queue (status, next_attempt_at);

-- ====================================================== acc_004: purchases
DROP TRIGGER IF EXISTS acc_purchase_freeze_money ON acc_purchase;
CREATE TRIGGER acc_purchase_freeze_money
BEFORE UPDATE ON acc_purchase
FOR EACH ROW
WHEN (OLD.status IN ('posted', 'reversed')
      AND (NEW.net_minor     IS DISTINCT FROM OLD.net_minor
           OR NEW.vat_minor    IS DISTINCT FROM OLD.vat_minor
           OR NEW.gross_minor  IS DISTINCT FROM OLD.gross_minor
           OR NEW.currency     IS DISTINCT FROM OLD.currency
           OR NEW.purchase_no  IS DISTINCT FROM OLD.purchase_no
           OR NEW.supplier_id  IS DISTINCT FROM OLD.supplier_id
           OR NEW.journal_entry_id IS DISTINCT FROM OLD.journal_entry_id))
EXECUTE FUNCTION
  acc_abort('purchase: posted amounts are immutable; reverse it instead');

DROP TRIGGER IF EXISTS acc_purchase_no_delete ON acc_purchase;
CREATE TRIGGER acc_purchase_no_delete
BEFORE DELETE ON acc_purchase
FOR EACH ROW EXECUTE FUNCTION
  acc_abort('purchase: posted bills cannot be deleted');

DROP TRIGGER IF EXISTS acc_purchase_line_no_update ON acc_purchase_line;
CREATE TRIGGER acc_purchase_line_no_update
BEFORE UPDATE ON acc_purchase_line
FOR EACH ROW EXECUTE FUNCTION acc_abort('purchase lines are immutable');

CREATE OR REPLACE FUNCTION acc_purchase_line_no_delete_fn() RETURNS trigger AS $$
BEGIN
  IF (SELECT status FROM acc_purchase WHERE id = OLD.purchase_id)
       IN ('posted', 'reversed') THEN
    RAISE EXCEPTION 'purchase lines cannot be deleted';
  END IF;
  RETURN OLD;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS acc_purchase_line_no_delete ON acc_purchase_line;
CREATE TRIGGER acc_purchase_line_no_delete
BEFORE DELETE ON acc_purchase_line
FOR EACH ROW EXECUTE FUNCTION acc_purchase_line_no_delete_fn();

CREATE INDEX IF NOT EXISTS ix_acc_purchase_supplier ON acc_purchase (supplier_id);
CREATE INDEX IF NOT EXISTS ix_acc_purchase_date     ON acc_purchase (purchase_date);
CREATE INDEX IF NOT EXISTS ix_acc_purchase_status   ON acc_purchase (status);

-- ==================================================== acc_005: cash & bank
DROP TRIGGER IF EXISTS acc_receipt_freeze ON acc_receipt;
CREATE TRIGGER acc_receipt_freeze
BEFORE UPDATE ON acc_receipt
FOR EACH ROW
WHEN (OLD.status IN ('posted', 'reversed')
      AND (NEW.amount_minor  IS DISTINCT FROM OLD.amount_minor
           OR NEW.wht_minor    IS DISTINCT FROM OLD.wht_minor
           OR NEW.currency     IS DISTINCT FROM OLD.currency
           OR NEW.receipt_no   IS DISTINCT FROM OLD.receipt_no
           OR NEW.customer_id  IS DISTINCT FROM OLD.customer_id
           OR NEW.journal_entry_id IS DISTINCT FROM OLD.journal_entry_id))
EXECUTE FUNCTION
  acc_abort('receipt: posted amounts are immutable; reverse it instead');

DROP TRIGGER IF EXISTS acc_receipt_no_delete ON acc_receipt;
CREATE TRIGGER acc_receipt_no_delete
BEFORE DELETE ON acc_receipt
FOR EACH ROW EXECUTE FUNCTION
  acc_abort('receipt: money documents cannot be deleted');

DROP TRIGGER IF EXISTS acc_payment_freeze ON acc_supplier_payment;
CREATE TRIGGER acc_payment_freeze
BEFORE UPDATE ON acc_supplier_payment
FOR EACH ROW
WHEN (OLD.status IN ('posted', 'reversed')
      AND (NEW.amount_minor IS DISTINCT FROM OLD.amount_minor
           OR NEW.payment_no  IS DISTINCT FROM OLD.payment_no
           OR NEW.supplier_id IS DISTINCT FROM OLD.supplier_id
           OR NEW.journal_entry_id IS DISTINCT FROM OLD.journal_entry_id))
EXECUTE FUNCTION
  acc_abort('payment: posted amounts are immutable; reverse it instead');

DROP TRIGGER IF EXISTS acc_payment_no_delete ON acc_supplier_payment;
CREATE TRIGGER acc_payment_no_delete
BEFORE DELETE ON acc_supplier_payment
FOR EACH ROW EXECUTE FUNCTION
  acc_abort('payment: money documents cannot be deleted');

CREATE INDEX IF NOT EXISTS ix_acc_receipt_customer ON acc_receipt (customer_id);
CREATE INDEX IF NOT EXISTS ix_acc_receipt_date     ON acc_receipt (receipt_date);
CREATE INDEX IF NOT EXISTS ix_acc_payment_supplier ON acc_supplier_payment (supplier_id);
CREATE INDEX IF NOT EXISTS ix_acc_recon_account    ON acc_reconciliation (account_id, status);

-- ======================================================= acc_006: own shops
DROP TRIGGER IF EXISTS acc_transfer_no_delete ON acc_transfer;
CREATE TRIGGER acc_transfer_no_delete
BEFORE DELETE ON acc_transfer
FOR EACH ROW EXECUTE FUNCTION
  acc_abort('transfer: stock documents cannot be deleted');

DROP TRIGGER IF EXISTS acc_transfer_line_no_update ON acc_transfer_line;
CREATE TRIGGER acc_transfer_line_no_update
BEFORE UPDATE ON acc_transfer_line
FOR EACH ROW EXECUTE FUNCTION acc_abort('transfer lines are immutable');

DROP TRIGGER IF EXISTS acc_shop_sale_freeze ON acc_shop_sale;
CREATE TRIGGER acc_shop_sale_freeze
BEFORE UPDATE ON acc_shop_sale
FOR EACH ROW
WHEN (OLD.status IN ('posted', 'reversed')
      AND (NEW.gross_minor  IS DISTINCT FROM OLD.gross_minor
           OR NEW.net_minor   IS DISTINCT FROM OLD.net_minor
           OR NEW.vat_minor   IS DISTINCT FROM OLD.vat_minor
           OR NEW.cogs_minor  IS DISTINCT FROM OLD.cogs_minor
           OR NEW.sale_no     IS DISTINCT FROM OLD.sale_no
           OR NEW.location_id IS DISTINCT FROM OLD.location_id
           OR NEW.journal_entry_id IS DISTINCT FROM OLD.journal_entry_id))
EXECUTE FUNCTION
  acc_abort('shop sale: posted amounts are immutable; reverse via journal');

DROP TRIGGER IF EXISTS acc_shop_sale_no_delete ON acc_shop_sale;
CREATE TRIGGER acc_shop_sale_no_delete
BEFORE DELETE ON acc_shop_sale
FOR EACH ROW EXECUTE FUNCTION
  acc_abort('shop sale: posted documents cannot be deleted');

CREATE INDEX IF NOT EXISTS ix_acc_transfer_date   ON acc_transfer (transfer_date);
CREATE INDEX IF NOT EXISTS ix_acc_shop_sale_date  ON acc_shop_sale (sale_date);
CREATE INDEX IF NOT EXISTS ix_acc_item_loc_lookup ON acc_item_location (location_id, item_id);
