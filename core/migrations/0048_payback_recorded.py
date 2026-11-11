from django.db import migrations, models


def desk_paybacks_recorded(apps, schema_editor):
    """Paybacks still waiting at a cashier become the agent's own record: cashiers record agents' payments
    themselves, so confirming these too would count them twice."""
    P = apps.get_model("core", "AgentPayback")
    P.objects.filter(desk__isnull=False, status="PENDING").update(status="RECORDED")


class Migration(migrations.Migration):
    dependencies = [("core", "0047_import_stats")]
    operations = [
        migrations.AlterField(
            model_name="agentpayback", name="status",
            field=models.CharField(choices=[("PENDING", "Waiting for the shop"),
                                            ("RECORDED", "Recorded by the agent (the cashier records the payment)"),
                                            ("CONFIRMED", "Confirmed"), ("REJECTED", "Rejected")],
                                   db_index=True, default="PENDING", max_length=9)),
        migrations.RunPython(desk_paybacks_recorded, migrations.RunPython.noop),
    ]
