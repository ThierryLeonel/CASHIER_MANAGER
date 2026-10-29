"""Customer debt followed per product: split openings and payments."""
import uuid

from django.test import TestCase

from core.models import Transaction, TxType
from core.reports.defs import debt_by_product, product_balances
from core.reports.engine import Filters
from core.services import corrections as corr
from core.services import operations as ops
from core.services.errors import BusinessError

from .helpers import D, OrgMixin
from .test_web import PW


class ProductSplit(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.admin_c = self.client_class()
        self.assertTrue(self.admin_c.login(username="admin", password=PW))

    def test_split_opening_from_page(self):
        r = self.admin_c.post("/setup/opening-balances/", {
            "action": "single", "idem": str(uuid.uuid4()), "account": self.a2.account.pk,
            "split_UVE": "100 000", "split_CASH": "30 000", "split_ER": "20 000", "amount": "",
            "business_date": "2026-10-28"})
        self.assertEqual(r.status_code, 302)
        t = Transaction.objects.get(type=TxType.OPENING_BALANCE, opening_for_account=self.a2.account)
        self.assertEqual((t.amount, t.product_split), (D(150000), {"UVE": 100000, "CASH": 30000, "ER": 20000}))
        self.assertEqual(self.bal(self.a2.account), D(150000))

    def test_split_rejected_for_wallets_and_mismatch(self):
        r = self.admin_c.post("/setup/opening-balances/", {
            "action": "single", "idem": str(uuid.uuid4()), "account": self.a2.account.pk,
            "split_UVE": "100", "amount": "500", "business_date": "2026-10-28"})
        self.assertContains(r, "must equal the amount")
        with self.assertRaises(BusinessError):
            ops.record_opening_balance(self.admin, account=self.bank, amount=D(10), split={"UVE": 10})

    def test_payment_settles_products_and_report(self):
        ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(300000), source_account=self.uve_f1)
        ops.record_supply(self.c_f1, product="ER", customer=self.a1, amount=D(100000), source_account=self.er_f1)
        pay = ops.record_collection(self.c_f1, payer=self.a1, method="CASH", amount=D(250000), account=self.d_f1,
                                    split={"UVE": 200000, "ER": 50000})
        self.assertEqual(pay.product_split, {"UVE": 200000, "ER": 50000})
        b = product_balances([self.a1])[self.a1.id]
        self.assertEqual((b["UVE"], b["ER"], b["CASH"], b["unsplit"], b["total"]),
                         (D(100000), D(50000), D(0), D(0), D(150000)))
        with self.assertRaises(BusinessError):
            ops.record_collection(self.c_f1, payer=self.a1, method="CASH", amount=D(1000), account=self.d_f1,
                                  split={"UVE": 999})
        # a payment without parts stays "not split"; the columns still add up to the balance
        ops.record_collection(self.c_f1, payer=self.a1, method="CASH", amount=D(10000), account=self.d_f1)
        b = product_balances([self.a1])[self.a1.id]
        self.assertEqual((b["unsplit"], b["total"]), (D(-10000), D(140000)))
        # cancelling the split payment restores the product debts
        corr.correct_transaction(self.manager, pay, kind="VOID", reason="wrong")
        b = product_balances([self.a1])[self.a1.id]
        self.assertEqual((b["UVE"], b["ER"]), (D(300000), D(100000)))
        rep = debt_by_product(Filters(None, None, user=self.manager))
        row = next(r for r in rep.tables[0].rows if r["sim"] == self.a1.sim_display)
        self.assertEqual(row["microzone"], "Fako 1 › Molyko")
        self.assertEqual(row["total"], self.bal(self.a1.account))

    def test_payment_form_fills_amount_from_parts(self):
        c = self.client_class()
        c.login(username="cashier.fako1", password=PW)
        page = c.get("/collection/new/")
        self.assertContains(page, 'data-split="UVE"')
        r = c.post("/collection/new/", {"idem": str(uuid.uuid4()), "payer": "691000001", "split_UVE": "5 000",
                                        "split_CASH": "1 000", "amount": "", "method": "CASH",
                                        "account": self.d_f1.pk})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        t = Transaction.objects.filter(type=TxType.COLLECTION).latest("posted_at")
        self.assertEqual((t.amount, t.product_split), (D(6000), {"UVE": 5000, "CASH": 1000}))
        self.assertEqual(c.get("/reports/debt_by_product/?preset=all").status_code, 200)


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
