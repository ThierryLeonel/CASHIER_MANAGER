"""Each record belongs to whoever recorded it: an entry only says where the money came from. Entries recorded
before carried an "owed back to the sender" obligation (Pay the sender / unpaid in the closing note); clear it.
Agent paybacks to a shop waiting for the shop's confirmation become the agent's own record (the shop records the
payment itself), like paybacks to a cashier."""
from django.db import migrations


def forward(apps, schema_editor):
    Transaction = apps.get_model("core", "Transaction")
    pg = schema_editor.connection.vendor == "postgresql"
    if pg:  # ledger rows are append-only: this one-off clean-up uses the admin override, like 0004
        with schema_editor.connection.cursor() as cur:
            cur.execute("SET LOCAL core.admin_override = 'on'")
    Transaction.objects.filter(is_entry=True, to_settle=True).update(to_settle=False)
    if pg:
        with schema_editor.connection.cursor() as cur:
            cur.execute("SET LOCAL core.admin_override = 'off'")
    AgentPayback = apps.get_model("core", "AgentPayback")
    AgentPayback.objects.filter(shop__isnull=False, status="PENDING").update(status="RECORDED")


class Migration(migrations.Migration):
    dependencies = [("core", "0055_stock_exchange_types")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
