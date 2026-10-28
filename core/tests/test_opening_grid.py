"""Opening balances by control account (cash, UVE, ER) giving the total opening."""
import uuid

from django.test import TestCase

from core.models import Transaction, TxType

from .helpers import D, OrgMixin
from .test_web import PW


class OpeningGrid(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.c = self.client_class()
        self.assertTrue(self.c.login(username="admin", password=PW))

    def test_grid_records_all_and_totals(self):
        r = self.c.get("/setup/opening-balances/")
        self.assertContains(r, "TOTAL OPENING")
        self.assertContains(r, f'name="acct_{self.d_f1.pk}"')
        self.assertContains(r, f'name="acct_{self.er_f1.pk}"')
        self.assertNotContains(r, f'name="acct_{self.mtn_f1.pk}"')  # MTN goes in "other"
        data = {"action": "grid", "batch": str(uuid.uuid4()), "business_date": "2026-10-28", "note": "launch",
                f"acct_{self.d_f1.pk}": "1 000 000", f"acct_{self.uve_f1.pk}": "2 500 000",
                f"acct_{self.er_f1.pk}": "300 000", f"acct_{self.d_me.pk}": ""}
        r = self.c.post("/setup/opening-balances/", data)
        self.assertEqual(r.status_code, 302)
        self.assertEqual((self.bal(self.d_f1), self.bal(self.uve_f1), self.bal(self.er_f1)),
                         (D(1000000), D(2500000), D(300000)))
        self.assertEqual(Transaction.objects.filter(type=TxType.OPENING_BALANCE).count(), 3)
        page = self.c.get("/setup/opening-balances/")
        self.assertEqual(page.context["grand"], D(3800000))
        self.assertNotContains(page, f'name="acct_{self.d_f1.pk}"')  # recorded: no longer editable
        # resubmitting the same form records nothing twice
        self.c.post("/setup/opening-balances/", data)
        self.assertEqual(Transaction.objects.filter(type=TxType.OPENING_BALANCE).count(), 3)

    def test_all_or_nothing(self):
        data = {"action": "grid", "batch": str(uuid.uuid4()), "business_date": "2026-10-28",
                f"acct_{self.d_f1.pk}": "1000", f"acct_{self.uve_f1.pk}": "-5"}  # wallets cannot open negative
        r = self.c.post("/setup/opening-balances/", data)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Transaction.objects.filter(type=TxType.OPENING_BALANCE).count(), 0)

    def test_other_openings_still_one_by_one(self):
        r = self.c.post("/setup/opening-balances/", {"action": "single", "idem": str(uuid.uuid4()),
                                                     "account": self.a2.account.pk, "amount": "150 000",
                                                     "business_date": "2026-10-28"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.bal(self.a2.account), D(150000))
