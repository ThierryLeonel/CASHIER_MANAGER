"""Write locale/fr/LC_MESSAGES/django.po and django.mo from core/i18n_fr.py."""
from pathlib import Path

import polib
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.i18n_fr import FR, FR_PLURAL

from .extract_messages import extract


class Command(BaseCommand):
    help = "Compile the French translation catalogue (no GNU gettext needed)."

    def handle(self, *args, **opts):
        singles, plurals = extract()
        missing = sorted(m for m in singles if m not in FR)
        missing += sorted(m for m in plurals if m not in FR_PLURAL)
        if missing:
            for m in missing:
                self.stderr.write(f"missing: {m!r}")
            raise CommandError(f"{len(missing)} message(s) lack a French translation")
        po = polib.POFile()
        po.metadata = {"Content-Type": "text/plain; charset=UTF-8", "Language": "fr",
                       "Plural-Forms": "nplurals=2; plural=(n > 1);"}
        for msgid in sorted(FR):
            po.append(polib.POEntry(msgid=msgid, msgstr=FR[msgid], flags=["python-format"] if "%(" in msgid else []))
        for msgid, (plural, forms) in FR_PLURAL.items():
            po.append(polib.POEntry(msgid=msgid, msgid_plural=plural, msgstr_plural={0: forms[0], 1: forms[1]}))
        out = Path(settings.BASE_DIR) / "locale" / "fr" / "LC_MESSAGES"
        out.mkdir(parents=True, exist_ok=True)
        po.save(str(out / "django.po"))
        po.save_as_mofile(str(out / "django.mo"))
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(po)} French messages to {out}"))
