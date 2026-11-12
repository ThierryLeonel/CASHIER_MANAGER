"""Entries: money a cashier received and records himself (from a shop, another cashier, the company, outside)."""
import uuid
from decimal import Decimal as D

from django.test import TestCase

from core.models import Account, Transaction, TxType
from core.services import operations as ops
from core.services.errors import BusinessError

from .helpers import OrgMixin
from .test_web import PW


class Entries(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]
        self.fund(self.shop.account(Account.Kind.CASH_DRAWER), 200000)

    def test_from_a_shop_once_only(self):
        shop_cash = self.shop.account(Account.Kind.CASH_DRAWER)
        before = (self.bal(self.d_f1), self.bal(shop_cash))
        t = ops.record_entry(self.c_f1, source="SHOP", shop=self.shop, product="CASH", amount=D(50000),
                             reference="T1")
        self.assertTrue(t.is_entry)
        self.assertEqual((t.type, str(t.display_type)), (TxType.SHOP_TO_DESK, "Entry from a shop"))
        self.assertEqual((self.bal(self.d_f1), self.bal(shop_cash)), (before[0] + 50000, before[1] - 50000))
        # the same again the same day: refused as a double record, unless it is another transfer
        with self.assertRaises(BusinessError):
            ops.record_entry(self.c_f1, source="SHOP", shop=self.shop, product="CASH", amount=D(50000))
        ops.record_entry(self.c_f1, source="SHOP", shop=self.shop, product="CASH", amount=D(50000), another=True)
        # more than the shop holds: refused
        with self.assertRaises(BusinessError):
            ops.record_entry(self.c_f1, source="SHOP", shop=self.shop, product="CASH", amount=D(500000))

    def test_from_another_cashier_and_double_record(self):
        before = (self.bal(self.uve_f1), self.bal(self.uve_f2))
        ops.record_entry(self.c_f1, source="DESK", desk=self.c_f2.subzone, product="UVE", amount=D(30000))
        self.assertEqual((self.bal(self.uve_f1), self.bal(self.uve_f2)), (before[0] + 30000, before[1] - 30000))
        # the other cashier had already sent it from his side → an entry for it is refused
        ops.record_desk_transfer(self.c_f2, desk=self.c_f1.subzone, cash=D(7000))
        with self.assertRaises(BusinessError) as e:
            ops.record_entry(self.c_f1, source="DESK", desk=self.c_f2.subzone, product="CASH", amount=D(7000))
        self.assertIn("already recorded", e.exception.message)
        with self.assertRaises(BusinessError):  # not from his own desk
            ops.record_entry(self.c_f1, source="DESK", desk=self.c_f1.subzone, product="CASH", amount=D(1))

    def test_from_outside_and_company_account(self):
        before = self.bal(self.mtn_f1)
        t = ops.record_entry(self.c_f1, source="OTHER", from_whom="Owner Mr X", product="MTN", amount=D(12000))
        self.assertEqual((t.type, t.fund_source), (TxType.CAPITAL_IN, "Owner Mr X"))
        self.assertEqual(self.bal(self.mtn_f1), before + 12000)
        with self.assertRaises(BusinessError):
            ops.record_entry(self.c_f1, source="OTHER", product="CASH", amount=D(1))
        company = self.uve_me  # an account that is not the cashier's
        t = ops.record_entry(self.c_f1, source="COMPANY", company_account=company, product="", amount=D(4000))
        self.assertEqual((t.type, t.product, t.dest_account.kind), (TxType.CAPITAL_MOVE, "UVE", Account.Kind.UVE))
        with self.assertRaises(BusinessError):  # his own account is not a source
            ops.record_entry(self.c_f1, source="COMPANY", company_account=self.uve_f1, product="", amount=D(1))

    def test_page(self):
        c = self.client_class()
        c.login(username="cashier.fako1", password=PW)
        r = c.get("/entries/new/")
        self.assertContains(r, "Entry: money received")
        self.assertContains(c.get("/"), "/entries/new/")
        r = c.post("/entries/new/", {"idem": str(uuid.uuid4()), "source": "SHOP", "shop": self.shop.pk,
                                     "product": "CASH", "amount": "10 000"})
        self.assertEqual(r.status_code, 302, getattr(r, "context", {}) and r.context["form"].errors)
        t = Transaction.objects.filter(is_entry=True).get()
        self.assertEqual(t.amount, D(10000))
        self.assertContains(c.get(f"/transactions/{t.pk}/"), "Entry from a shop")
