"""Database-level ledger integrity:

* append-only transactions and entries (UPDATE/DELETE raise),
* every transaction's entries sum to zero (deferred constraint trigger),
* company asset accounts never negative (deferred constraint trigger;
  the posting service also locks accounts to prevent races),
* sequences for references and receipt numbers,
* system accounts and the expense categories named in the requirements.
"""
from django.db import migrations

SQL = r"""
CREATE SEQUENCE IF NOT EXISTS core_txn_ref_seq;
CREATE SEQUENCE IF NOT EXISTS core_receipt_seq;
CREATE SEQUENCE IF NOT EXISTS core_exchange_ref_seq;

CREATE OR REPLACE FUNCTION core_block_modify() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'Ledger records are append-only (% on %)', TG_OP, TG_TABLE_NAME
    USING ERRCODE = 'integrity_constraint_violation';
END $$ LANGUAGE plpgsql;

CREATE TRIGGER core_entry_append_only BEFORE UPDATE OR DELETE ON core_entry
  FOR EACH ROW EXECUTE FUNCTION core_block_modify();
CREATE TRIGGER core_transaction_append_only BEFORE UPDATE OR DELETE ON core_transaction
  FOR EACH ROW EXECUTE FUNCTION core_block_modify();

CREATE OR REPLACE FUNCTION core_check_balanced() RETURNS trigger AS $$
DECLARE s numeric;
BEGIN
  SELECT COALESCE(SUM(amount), 0) INTO s FROM core_entry WHERE transaction_id = NEW.transaction_id;
  IF s <> 0 THEN
    RAISE EXCEPTION 'Unbalanced ledger transaction %: entries sum to %', NEW.transaction_id, s
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER core_entry_balanced AFTER INSERT ON core_entry
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION core_check_balanced();

CREATE OR REPLACE FUNCTION core_check_asset_nonnegative() RETURNS trigger AS $$
DECLARE k text; s numeric;
BEGIN
  SELECT kind INTO k FROM core_account WHERE id = NEW.account_id;
  IF k IN ('CASH_DRAWER', 'UVE', 'ER', 'MTN', 'BANK') THEN
    SELECT COALESCE(SUM(amount), 0) INTO s FROM core_entry WHERE account_id = NEW.account_id;
    IF s < 0 THEN
      RAISE EXCEPTION 'Company asset account % would become negative (%)', NEW.account_id, s
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER core_entry_asset_nonnegative AFTER INSERT ON core_entry
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION core_check_asset_nonnegative();
"""

REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS core_entry_asset_nonnegative ON core_entry;
DROP TRIGGER IF EXISTS core_entry_balanced ON core_entry;
DROP TRIGGER IF EXISTS core_transaction_append_only ON core_transaction;
DROP TRIGGER IF EXISTS core_entry_append_only ON core_entry;
DROP FUNCTION IF EXISTS core_check_asset_nonnegative();
DROP FUNCTION IF EXISTS core_check_balanced();
DROP FUNCTION IF EXISTS core_block_modify();
DROP SEQUENCE IF EXISTS core_txn_ref_seq;
DROP SEQUENCE IF EXISTS core_receipt_seq;
DROP SEQUENCE IF EXISTS core_exchange_ref_seq;
"""

CATEGORIES = [
    ("daily", "Daily expenses", "Dépenses journalières"),
    ("transport", "Transport", "Transport"),
    ("repairs", "Repairs and maintenance", "Réparations et entretien"),
    ("orange-prefinance", "Orange pre-finance", "Préfinancement Orange"),
    ("shop", "Shop expenditures", "Dépenses boutique"),
    ("fuel", "Fuel", "Carburant"),
    ("other", "Other", "Autres"),
]


def seed(apps, schema_editor):
    Account = apps.get_model("core", "Account")
    ExpenseCategory = apps.get_model("core", "ExpenseCategory")
    for kind, name in [("CONVERSION_UPLIFT", "Conversion uplift"),
                       ("RECON_ADJUSTMENT", "Reconciliation adjustments"),
                       ("OPENING_EQUITY", "Opening balance equity")]:
        Account.objects.get_or_create(kind=kind, defaults={"name": name})
    for code, en, fr in CATEGORIES:
        cat, _ = ExpenseCategory.objects.get_or_create(code=code, defaults={"name_en": en, "name_fr": fr})
        Account.objects.get_or_create(kind="EXPENSE", expense_category=cat, defaults={"name": f"Expense {en}"})


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [
        migrations.RunSQL(SQL, REVERSE_SQL),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
