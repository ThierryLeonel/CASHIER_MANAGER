"""Customer debt followed per product: split openings and payments."""
import uuid

from django.test import TestCase


from .helpers import D, OrgMixin
from .test_web import PW


class OneCombinedDebt(OrgMixin, TestCase):
    """Payments and openings are not split by product: an agent has one combined debt."""

    def setUp(self):
        self.make_org()
        self.fund_all()

    def test_forms_have_no_product_parts(self):
        c = self.client_class()
        c.login(username="cashier.fako1", password=PW)
        self.assertNotContains(c.get("/collection/new/"), 'data-split=')
        a = self.client_class()
        a.login(username="admin", password=PW)
        self.assertNotContains(a.get("/setup/opening-balances/"), 'name="split_UVE"')
        r = a.post("/setup/opening-balances/", {"action": "single", "idem": str(uuid.uuid4()),
                                                "account": self.a2.account.pk, "amount": "150 000",
                                                "business_date": "2026-10-28"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.bal(self.a2.account), D(150000))
        self.assertEqual(a.get("/reports/debt_by_product/?preset=all").status_code, 404)


class AutoAccounts(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.c = self.client_class()
        self.assertTrue(self.c.login(username="cashier.fako2", password=PW))

    def test_payment_goes_to_the_subzone_account_of_the_method(self):
        for method, acct in (("CASH", self.d_f2), ("MTN", self.mtn_f2), ("UVE", self.uve_f2)):
            before = self.bal(acct)
            r = self.c.post("/collection/new/", {"idem": str(uuid.uuid4()), "payer": "691000001", "amount": "1 000",
                                                 "method": method})
            self.assertEqual(r.status_code, 302, (method, r.content[:1500]))
            self.assertEqual(self.bal(acct), before + 1000)

    def test_supply_takes_the_subzone_wallet_of_the_product(self):
        before = self.bal(self.uve_f2)
        r = self.c.post("/supply/agent/", {"idem": str(uuid.uuid4()), "product": "UVE", "agent": "691000002",
                                           "amount": "2 000"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        self.assertEqual(self.bal(self.uve_f2), before - 2000)
