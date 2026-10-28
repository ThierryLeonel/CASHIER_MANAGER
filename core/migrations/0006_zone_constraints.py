# Constraints are added in their own migration (own transaction) after the
# zone backfill of 0005.
import django.db.models  # noqa: F401
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0005_zones_accounts_days")]

    operations = [
        migrations.AddConstraint(
            model_name="account",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("kind", "CASH_DRAWER"), _negated=True),
                    ("zone__isnull", False),
                    _connector="OR",
                ),
                name="drawer_has_zone",
            ),
        ),
        migrations.AddConstraint(
            model_name="cashierday",
            constraint=models.UniqueConstraint(
                fields=("zone", "business_date"), name="one_day_per_zone_date"
            ),
        ),
        migrations.AddConstraint(
            model_name="cashierday",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "OPEN")),
                fields=("zone",),
                name="one_open_day_per_zone",
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("role", "JUNIOR"), _negated=True),
                    ("subzone__isnull", False),
                    _connector="OR",
                ),
                name="junior_has_zone",
            ),
        ),
    ]
