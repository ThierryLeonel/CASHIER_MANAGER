"""Entries: money a cashier received and records himself (from a shop, another cashier, the company, outside)."""
import uuid
from decimal import Decimal as D

from django.test import TestCase

from core.models import Account, Transaction, TxType
from core.services import operations as ops
from core.services import transit
from core.services.errors import BusinessError

from .helpers import OrgMixin
from .test_web import PW


class Entries(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]
        self.fund(self.shop.account(Account.Kind.CASH_DRAWER), 200000)

    def test_each_side_records_its_own_money(self):
        """Cashier A sends 200 000 to B: A records the transfer (only A's balance moves), B records her entry (only
        B's moves). No record is attached to the other."""
        d1, d2 = self.bal(self.d_f1), self.bal(self.d_f2)
        sent = ops.record_desk_transfer(self.c_f1, desk=self.c_f2.subzone, cash=D(200000))[0]
        self.assertEqual((self.bal(self.d_f1), self.bal(self.d_f2)), (d1 - 200000, d2))
        self.assertFalse(sent.to_settle)
        got = ops.record_entry(self.c_f2, source="DESK", desk=self.c_f1.subzone, product="CASH", amount=D(200000),
                               reference="T1")
        self.assertEqual((self.bal(self.d_f1), self.bal(self.d_f2)), (d1 - 200000, d2 + 200000))
        self.assertEqual((got.transit_match, got.is_entry, str(got.display_type)), (None, True, "Entry from a cashier"))
        self.assertTrue(got.to_settle)  # what B owes back, on her own record
        # B may enter a different amount or before A records anything: each record stands alone
        ops.record_entry(self.c_f2, source="DESK", desk=self.c_f1.subzone, product="CASH", amount=D(5000))
        self.assertEqual(self.bal(self.d_f1), d1 - 200000)

    def test_from_a_shop(self):
        shop_cash = self.shop.account(Account.Kind.CASH_DRAWER)
        s1 = self.bal(shop_cash)
        e = ops.record_entry(self.c_f1, source="SHOP", shop=self.shop, product="CASH", amount=D(50000))
        self.assertEqual((e.type, str(e.display_type)), (TxType.SHOP_TO_DESK, "Entry from a shop"))
        self.assertEqual(self.bal(shop_cash), s1)  # the shop's balance is the shop's own records
        with self.assertRaises(BusinessError):
            ops.record_entry(self.c_f1, source="SHOP", shop=self.shop, product="MTN", amount=D(1))
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
        self.assertEqual(r.status_code, 302)
        t = Transaction.objects.filter(is_entry=True).get()
        self.assertEqual(t.amount, D(10000))
        self.assertContains(c.get(f"/transactions/{t.pk}/"), "Entry from a shop")

    def test_receiver_does_not_see_the_senders_record_and_shop_entry_page(self):
        sent = ops.record_desk_transfer(self.c_f2, desk=self.c_f1.subzone, cash=D(12000))[0]
        c = self.client_class()
        c.login(username="cashier.fako1", password=PW)
        self.assertNotContains(c.get("/entries/new/"), sent.reference)
        self.assertEqual(c.get(f"/transactions/{sent.pk}/").status_code, 404)
        s = self.client_class()
        s.login(username="shop.senior", password=PW)
        self.assertContains(s.get("/shops/do/entry/"), "Entry: money received")
