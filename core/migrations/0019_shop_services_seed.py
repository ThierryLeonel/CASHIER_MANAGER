from django.db import migrations

SERVICES = [
    ("SIM retrieval", "RETRIEVE", 10),
    ("Update identification of SIM cards", "COUNT", 20),
    ("OTT (Orange Money on other networks' SIMs)", "COUNT", 30),
    ("MAX IT application", "COUNT", 40),
    ("Ticket creation", "COUNT", 50),
]


def seed(apps, schema_editor):
    ShopService = apps.get_model("core", "ShopService")
    for name, kind, sort in SERVICES:
        ShopService.objects.get_or_create(name=name, defaults={"kind": kind, "sort": sort})


class Migration(migrations.Migration):
    dependencies = [("core", "0018_shops")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
