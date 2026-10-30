"""Capital injections, withdrawals and transfers by finance / the senior manager / the super admin."""
import uuid

from django.test import TestCase

from core.models import Account, Transaction, TxType
from core.services import capital as cap
from core.services import corrections as corr
from core.services.errors import BusinessError, InsufficientFunds, PermissionDenied

from .helpers import D, OrgMixin
from .test_web import PW

K = Account.Kind


class CapitalMovements(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_inject_withdraw_transfer(self):
        before = self.bal(self.d_f1)
        t = cap.inject(self.finance, account=self.d_f1, amount=D(500000), source="Owner's savings",
                       purpose="Increase Fako 1 float")
        self.assertEqual(self.bal(self.d_f1), before + 500000)
        self.assertEqual(cap.net_capital(), D(500000))
        self.assertEqual((t.zone, t.fund_source, t.fund_purpose), (self.zones["fako1"], "Owner's savings",
                                                                    "Increase Fako 1 float"))
        self.assertIsNotNone(t.cashier_day_id)  # in Fako 1's business day: the cashier sees it, the closing counts it
        # UVE into a shop
        cap.inject(self.manager, account=self.shop.account(K.UVE), amount=D(200000), source="Partner",
                   purpose="Shop launch")
        self.assertEqual(self.bal(self.shop.account(K.UVE)), D(200000))
        # transfer cashier cash -> shop cash, and cash -> bank
        m = cap.transfer(self.finance, source=self.d_f1, destination=self.shop.account(K.CASH_DRAWER),
                         amount=D(100000), purpose="Shop float")
        self.assertEqual((m.shop, self.bal(self.shop.account(K.CASH_DRAWER))), (self.shop, D(100000)))
        cap.transfer(self.finance, source=self.d_f1, destination=self.bank, amount=D(50000), purpose="Bank deposit")
        self.assertEqual(self.bal(self.bank), D(50000))
        with self.assertRaises(BusinessError):  # UVE cannot become cash
            cap.transfer(self.finance, source=self.uve_f1, destination=self.d_f2, amount=D(1), purpose="x")
        with self.assertRaises(BusinessError):
            cap.transfer(self.finance, source=self.d_f1, destination=self.d_f1, amount=D(1), purpose="x")
        # withdrawal
        cap.withdraw(self.finance, account=self.bank, amount=D(30000), beneficiary="Owner", purpose="Dividend")
        self.assertEqual(cap.net_capital(), D(670000))
        with self.assertRaises(InsufficientFunds):
            cap.withdraw(self.finance, account=self.bank, amount=D(10_000_000), beneficiary="Owner", purpose="x")
        with self.assertRaises(BusinessError):  # source and purpose are required
            cap.inject(self.finance, account=self.d_f1, amount=D(1), source="", purpose="x")
        with self.assertRaises(BusinessError):
            cap.withdraw(self.finance, account=self.d_f1, amount=D(1), beneficiary="Owner", purpose="")
        for u in (self.c_f1, self.junior, self.o["shop_senior"]):
            with self.assertRaises(PermissionDenied):
                cap.inject(u, account=self.d_f1, amount=D(1), source="x", purpose="y")
        tot = cap.totals()
        self.assertEqual((tot["injected"], tot["withdrawn"], tot["moved"]), (D(700000), D(30000), D(150000)))

    def test_cancel_a_capital_movement(self):
        t = cap.inject(self.finance, account=self.d_f1, amount=D(500000), source="Owner", purpose="Float")
        before = self.bal(self.d_f1)
        with self.assertRaises(BusinessError):  # only cancelled, then recorded again
            corr.correct_transaction(self.finance, t, kind="REPLACE", reason="typo", new_values={})
        corr.correct_transaction(self.finance, t, kind="VOID", reason="wrong account")
        self.assertEqual(self.bal(self.d_f1), before - 500000)
        self.assertEqual(cap.net_capital(), D(0))

    def test_pages_and_visibility(self):
        fin = self.client_for("finance")
        self.assertEqual(fin.get("/capital/").status_code, 200)
        for op in ("inject", "withdraw", "transfer"):
            self.assertEqual(fin.get(f"/capital/{op}/").status_code, 200)
        r = fin.post("/capital/inject/", {"idem": str(uuid.uuid4()), "account": self.d_f2.pk, "amount": "250 000",
                                          "source": "Bank loan AFB", "purpose": "Float Fako 2"})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        t = Transaction.objects.get(type=TxType.CAPITAL_IN)
        rec = fin.get(f"/transactions/{t.pk}/")
        self.assertContains(rec, "Bank loan AFB")
        self.assertContains(rec, "Float Fako 2")
        r = fin.post("/capital/transfer/", {"idem": str(uuid.uuid4()), "source": self.d_f2.pk,
                                            "destination": self.shop.account(K.CASH_DRAWER).pk, "amount": "40 000",
                                            "purpose": "Shop float"})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        mv = Transaction.objects.get(type=TxType.CAPITAL_MOVE)
        # the cashier of Fako 2 and the shop both see the transfer; Fako 1's cashier does not
        self.assertEqual(self.client_for("cashier.fako2").get(f"/transactions/{mv.pk}/").status_code, 200)
        self.assertEqual(self.client_for("shop.molyko").get(f"/transactions/{mv.pk}/").status_code, 200)
        self.assertEqual(self.client_for("cashier.fako1").get(f"/transactions/{mv.pk}/").status_code, 404)
        self.assertContains(fin.get("/capital/"), "Bank loan AFB")
        for u in ("cashier.fako1", "junior.fako", "shop.senior", "agent.ndive"):
            self.assertEqual(self.client_for(u).get("/capital/").status_code, 403)
        # the reconciliation still balances (capital column)
        rep = self.client_for("manager").get("/reports/reconciliation/?preset=all")
        self.assertContains(rep, "Capital movements")
        self.assertNotContains(rep, "MISMATCH")
