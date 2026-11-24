"""Lift a suspension after wrong passwords from the command line (e.g. when no administrator can sign in):
python manage.py unlock_user <username>"""
from django.core.management.base import BaseCommand, CommandError

from core.models import User


class Command(BaseCommand):
    help = "Lift the suspension of an account suspended after wrong passwords."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **opts):
        n = User.objects.filter(username=opts["username"]).update(suspended_at=None, failed_logins=0)
        if not n:
            raise CommandError(f"No user {opts['username']}")
        self.stdout.write(self.style.SUCCESS(f"{opts['username']} can sign in again."))
