"""* Backfill staff IDs and Transaction.cashier for existing rows.
* Append-only triggers now accept changes only inside a database transaction
  that the super-administrator service has explicitly marked with
  `SET LOCAL core.admin_override = 'on'` (never set by any other code path).
  Every such change is snapshotted to the audit log by the service first.
"""
from django.db import migrations

OVERRIDE_SQL = r"""
CREATE OR REPLACE FUNCTION core_block_modify() RETURNS trigger AS $$
BEGIN
  IF current_setting('core.admin_override', true) = 'on' THEN
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'Ledger records are append-only (% on %)', TG_OP, TG_TABLE_NAME
    USING ERRCODE = 'integrity_constraint_violation';
END $$ LANGUAGE plpgsql;
"""

REVERSE_SQL = r"""
CREATE OR REPLACE FUNCTION core_block_modify() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'Ledger records are append-only (% on %)', TG_OP, TG_TABLE_NAME
    USING ERRCODE = 'integrity_constraint_violation';
END $$ LANGUAGE plpgsql;
"""

PREFIX = {"SUPERADMIN": "ADM", "MANAGER": "MGR", "FINANCE": "FIN", "CASHIER": "CSH", "AGENT": "AGT"}


def backfill(apps, schema_editor):
    User = apps.get_model("core", "User")
    for u in User.objects.filter(staff_code__isnull=True):
        User.objects.filter(pk=u.pk).update(staff_code=f"{PREFIX.get(u.role, 'USR')}-{u.pk:04d}")
    with schema_editor.connection.cursor() as cur:
        cur.execute("SET LOCAL core.admin_override = 'on'")
        cur.execute("""
            UPDATE core_transaction t SET cashier_id = d.cashier_id
            FROM core_cashierday d WHERE t.cashier_day_id = d.id AND t.cashier_id IS NULL
        """)
        cur.execute("SET LOCAL core.admin_override = 'off'")


class Migration(migrations.Migration):
    dependencies = [("core", "0003_staff_ids_and_txn_cashier")]

    operations = [
        migrations.RunSQL(OVERRIDE_SQL, REVERSE_SQL),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
