from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import translation

from core.i18n_fr import FR, FR_PLURAL
from core.management.commands.extract_messages import extract
from core.money import MoneyError, conversion_er_amount, to_money
from core.simnumbers import SimNumberError, normalize_sim

from .helpers import D, OrgMixin


class Units(SimpleTestCase):
    def test_money_parsing(self):
        self.assertEqual(to_money("100 000"), Decimal(100000))
        self.assertEqual(to_money("100,000"), Decimal(100000))
        self.assertEqual(to_money("1.000.000"), Decimal(1000000))
        for bad in (1.5, "12.5", "abc", "NaN"):
            with self.assertRaises(MoneyError):
                to_money(bad)

    def test_conversion_rounding(self):
        r = Decimal("0.04")
        self.assertEqual(conversion_er_amount(Decimal(100000), r), Decimal(104000))
        self.assertEqual(conversion_er_amount(Decimal(12345), r), Decimal(12839))
        self.assertEqual(conversion_er_amount(Decimal(1), Decimal("0.5")), Decimal(2))  # 1.5 -> 2 (half-up)

    def test_sim_normalisation(self):
        for raw in ("+237 699 12 34 56", "00237699123456", "237-699-123-456", "6 99 12 34 56", "699.12.34.56"):
            self.assertEqual(normalize_sim(raw), "699123456")
        for bad in ("12345", "799123456", "2376991234567"):
            with self.assertRaises(SimNumberError):
                normalize_sim(bad)

    def test_every_message_has_french(self):
        singles, plurals = extract()
        self.assertEqual(sorted(m for m in singles if m not in FR), [])
        self.assertEqual(sorted(m for m in plurals if m not in FR_PLURAL), [])
        for k, v in FR.items():  # placeholders must survive translation
            import re

            self.assertEqual(sorted(re.findall(r"%\(\w+\)s", k)), sorted(re.findall(r"%\(\w+\)s", v)), k)

    def test_compiled_catalogue_is_current(self):
        with translation.override("fr"):
            self.assertEqual(translation.gettext("Supply"), "Approvisionnement")
            self.assertEqual(translation.gettext("Close day"), "Clôture")


class LedgerCheckCommand(OrgMixin, TestCase):
    def test_ledger_check_passes_after_activity(self):
        self.make_org()
        self.fund_all()
        from core.services import operations as ops

        ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(5000), source_account=self.uve_f1)
        out = StringIO()
        call_command("ledger_check", stdout=out)
        self.assertNotIn("FAIL", out.getvalue())
