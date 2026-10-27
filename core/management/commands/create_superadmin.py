"""Create the first super administrator (use instead of createsuperuser).

Non-interactive (e.g. Laravel Cloud command runner): set DJANGO_SUPERADMIN_PASSWORD
in the environment, run `python manage.py create_superadmin admin`, then remove the variable."""
import getpass
import os

from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError

from core.models import User


class Command(BaseCommand):
    help = "Create a SUPERADMIN user."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--name", default="")

    def handle(self, *args, **opts):
        if User.objects.filter(username=opts["username"]).exists():
            raise CommandError("User already exists")
        pw = os.environ.get("DJANGO_SUPERADMIN_PASSWORD")
        if not pw:  # interactive
            pw = getpass.getpass("Password: ")
            if pw != getpass.getpass("Password (again): "):
                raise CommandError("Passwords differ")
        u = User(username=opts["username"], first_name=opts["name"], role=User.Role.SUPERADMIN, is_staff=True,
                 is_superuser=True)
        validate_password(pw, u)
        u.set_password(pw)
        u.save()
        self.stdout.write(self.style.SUCCESS(f"Super administrator {u.username} created."))
