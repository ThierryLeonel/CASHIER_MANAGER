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
        """The shop records its transfer (only the shop's cash goes down), the cashier records the entry (only his
        goes up): linked, never counted twice."""
        from core.services import shop_shifts as ss
        from core.services import shops as sh

        ss.open_shift(self.o["shop_mgr"], cash=D(200000), uve=D(0), er=D(0))
        shop_cash = self.shop.account(Account.Kind.CASH_DRAWER)
        d1, s1 = self.bal(self.d_f1), self.bal(shop_cash)
        sent = sh.to_cashier(self.o["shop_mgr"], desk=self.c_f1.subzone, cash=D(50000))
        self.assertTrue(sent.in_transit)
        self.assertEqual((self.bal(self.d_f1), self.bal(shop_cash)), (d1, s1 - 50000))
        self.assertIn(sent, list(transit.pending([self.d_f1])))
        e = ops.record_entry(self.c_f1, source="SHOP", shop=self.shop, product="CASH", amount=D(50000), reference="T1")
        self.assertTrue(e.is_entry)
        self.assertEqual((e.type, str(e.display_type), e.transit_match), (TxType.SHOP_TO_DESK, "Entry from a shop",
                                                                          sent))
        self.assertEqual((self.bal(self.d_f1), self.bal(shop_cash)), (d1 + 50000, s1 - 50000))
        self.assertEqual(self.bal(transit.account()), D(0))
        self.assertNotIn(sent, list(transit.pending([self.d_f1])))
        # the same transfer cannot be entered twice by choosing it
        with self.assertRaises(BusinessError):
            ops.record_entry(self.c_f1, source="SHOP", match=sent, amount=D(50000))
        # an entry the sender has not recorded yet waits in transit (the other way round)
        ops.record_entry(self.c_f1, source="SHOP", shop=self.shop, product="CASH", amount=D(7000))
        self.assertEqual(self.bal(transit.account()), D(-7000))

    def test_from_another_cashier_chosen_in_sent_to_you(self):
        u1, u2 = self.bal(self.uve_f1), self.bal(self.uve_f2)
        sent = ops.record_desk_transfer(self.c_f2, desk=self.c_f1.subzone, uve=D(30000))[0]
        self.assertEqual((self.bal(self.uve_f1), self.bal(self.uve_f2)), (u1, u2 - 30000))
        e = ops.record_entry(self.c_f1, source="DESK", match=sent, amount=D(30000))
        self.assertEqual((e.transit_match, e.to_settle), (sent, False))  # the sender's record carries the payback
        self.assertEqual((self.bal(self.uve_f1), self.bal(self.uve_f2)), (u1 + 30000, u2 - 30000))
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

    def test_sent_to_you_list_and_shop_entry_page(self):
        sent = ops.record_desk_transfer(self.c_f2, desk=self.c_f1.subzone, cash=D(12000))[0]
        c = self.client_class()
        c.login(username="cashier.fako1", password=PW)
        r = c.get("/entries/new/")
        self.assertContains(r, "Sent to you, waiting for your entry")
        self.assertContains(r, f"?match={sent.pk}")
        r = c.get(f"/entries/new/?match={sent.pk}")
        self.assertContains(r, "Check the amount you really received")
        r = c.post(f"/entries/new/?match={sent.pk}", {"idem": str(uuid.uuid4()), "source": "SHOP", "match": sent.pk,
                                                       "product": "CASH", "amount": "12 000"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(sent.transit_entries.exists())
        self.assertNotContains(c.get("/entries/new/"), f"?match={sent.pk}")
        s = self.client_class()
        s.login(username="shop.senior", password=PW)
        self.assertContains(s.get("/shops/do/entry/"), "Entry: money received")
