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
        for method, acct in (("cash", self.d_f2), ("mtn", self.mtn_f2), ("uve", self.uve_f2)):
            before = self.bal(acct)
            r = self.c.post("/collection/new/", {"idem": str(uuid.uuid4()), "payer": "691000001", method: "1 000"})
            self.assertEqual(r.status_code, 302, (method, r.content[:1500]))
            self.assertEqual(self.bal(acct), before + 1000)

    def test_one_payment_with_several_methods(self):
        from core.models import Transaction, TxType

        before_cust = self.bal(self.a1.account)
        before = {a: self.bal(a) for a in (self.d_f2, self.uve_f2, self.mtn_f2, self.bank)}
        r = self.c.post("/collection/new/", {"idem": str(uuid.uuid4()), "payer": "691000001", "cash": "200 000",
                                             "uve": "150 000", "mtn": "30 000", "bank": "20 000",
                                             "bank_reference": "AFB-7788"})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        parts = list(Transaction.objects.filter(type=TxType.COLLECTION).order_by("-id")[:4])[::-1]
        self.assertEqual(len({p.payment_group for p in parts}), 1)
        self.assertEqual([p.payment_method for p in parts], ["CASH", "UVE", "MTN", "BANK"])
        self.assertEqual(sum(1 for p in parts if p.receipt_no), 1)  # one receipt for the payment
        self.assertEqual(self.bal(self.a1.account), before_cust - 400000)
        self.assertEqual((self.bal(self.d_f2) - before[self.d_f2], self.bal(self.uve_f2) - before[self.uve_f2],
                          self.bal(self.mtn_f2) - before[self.mtn_f2], self.bal(self.bank) - before[self.bank]),
                         (200000, 150000, 30000, 20000))
        rc = self.c.get(f"/transactions/{parts[2].pk}/receipt/")  # any part opens the one receipt
        self.assertContains(rc, "400 000 FCFA")
        self.assertContains(rc, "Paid in MTN float")
        self.assertContains(rc, "AFB-7788")
        pdf = self.c.get(f"/transactions/{parts[0].pk}/receipt.pdf")
        self.assertEqual(pdf.status_code, 200)
        # a bank part without its reference is refused, nothing posted
        n = Transaction.objects.count()
        r = self.c.post("/collection/new/", {"idem": str(uuid.uuid4()), "payer": "691000001", "cash": "1 000",
                                             "bank": "1 000"})
        self.assertContains(r, "bank deposit reference")
        self.assertEqual(Transaction.objects.count(), n)

    def test_supply_takes_the_subzone_wallet_of_the_product(self):
        before = self.bal(self.uve_f2)
        r = self.c.post("/supply/agent/", {"idem": str(uuid.uuid4()), "agent": "691000002", "uve": "2 000"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        self.assertEqual(self.bal(self.uve_f2), before - 2000)

    def test_one_supply_of_cash_uve_and_er(self):
        from core.models import Transaction, TxType

        c1 = self.client_class()
        c1.login(username="cashier.fako1", password=PW)  # in charge of the Fako master SIM (ER)
        before = (self.bal(self.d_f1), self.bal(self.uve_f1), self.bal(self.er_f1), self.bal(self.a2.account))
        r = c1.post("/supply/agent/", {"idem": str(uuid.uuid4()), "agent": "691000002", "cash": "100 000",
                                       "uve": "500 000", "er": "120 000"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        parts = list(Transaction.objects.filter(type=TxType.SUPPLY).order_by("-id")[:3])[::-1]
        self.assertEqual([p.product for p in parts], ["CASH", "UVE", "ER"])
        self.assertEqual(len({p.payment_group for p in parts}), 1)
        self.assertEqual(sum(1 for p in parts if p.receipt_no), 1)
        self.assertEqual((self.bal(self.d_f1), self.bal(self.uve_f1), self.bal(self.er_f1), self.bal(self.a2.account)),
                         (before[0] - 100000, before[1] - 500000, before[2] - 120000, before[3] + 720000))
        self.assertEqual(parts[2].source_account, self.er_f1)  # ER from the master SIM
        rc = c1.get(f"/transactions/{parts[1].pk}/receipt/")
        self.assertContains(rc, "720 000 FCFA")
        self.assertContains(rc, "Supplied ER")
        # a POS supply made of several products, with an agent assuming the debt
        r = c1.post("/supply/pos/", {"idem": str(uuid.uuid4()), "pos": "692000001", "assign_agent": "691000001",
                                     "cash": "10 000", "uve": "20 000"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        pos_parts = Transaction.objects.filter(type=TxType.SUPPLY, counterparty_2__business_sim="692000001")
        self.assertEqual(sorted(p.amount for p in pos_parts), [D(10000), D(20000)])
        self.assertTrue(all(p.counterparty == self.a1 for p in pos_parts))
