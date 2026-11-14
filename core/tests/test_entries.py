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
        self.assertFalse(got.to_settle)  # an entry only says where the money came from: nothing owed back
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


class IndividualRecords(OrgMixin, TestCase):
    """Each individual is in charge of his own record: no approval, no notification, no link, nothing owed."""

    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]
        self.m = self.o["shop_mgr"]
        from core.services import shop_shifts as ss

        self.fund(self.shop.account(Account.Kind.CASH_DRAWER), 300000)
        self.fund(self.shop.account(Account.Kind.UVE), 300000)
        ss.open_shift(self.m, cash=self.bal(self.shop.account(Account.Kind.CASH_DRAWER)),
                      uve=self.bal(self.shop.account(Account.Kind.UVE)),
                      er=self.bal(self.shop.account(Account.Kind.ER)))

    def login(self, username):
        c = self.client_class()
        self.assertTrue(c.login(username=username, password=PW))
        return c

    def test_shop_to_cashier_then_cashier_entry(self):
        from core.services import settlements as st
        from core.services import shops as sh

        drawer = self.shop.account(Account.Kind.CASH_DRAWER)
        s0, d0 = self.bal(drawer), self.bal(self.d_f1)
        sent = sh.to_cashier(self.m, desk=self.c_f1.subzone, cash=D(80000))
        self.assertEqual((self.bal(drawer), self.bal(self.d_f1)), (s0 - 80000, d0))  # only the shop's drawer
        # the cashier is not notified: nothing on his dashboard, nothing in his lists, nothing to confirm or pay
        c = self.login("cashier.fako1")
        self.assertNotContains(c.get("/"), sent.reference)
        self.assertNotContains(c.get("/transactions/?preset=all"), sent.reference)
        self.assertEqual(c.get(f"/transactions/{sent.pk}/").status_code, 404)
        got = ops.record_entry(self.c_f1, source="SHOP", shop=self.shop, product="CASH", amount=D(80000))
        self.assertEqual((self.bal(drawer), self.bal(self.d_f1)), (s0 - 80000, d0 + 80000))  # his cash goes up
        self.assertFalse(got.to_settle)
        self.assertEqual(st.unpaid_for_desk(self.c_f1.subzone), [])
        self.assertNotContains(c.get(f"/transactions/{got.pk}/"), "Pay the sender")
        # the shop never sees the cashier's entry; it sees its own transfer
        s = self.login("shop.molyko")
        page = s.get("/shops/transactions/?preset=all")
        self.assertContains(page, sent.reference)
        self.assertNotContains(page, got.reference)
        self.assertEqual(s.get(f"/transactions/{got.pk}/").status_code, 404)

    def test_cashier_to_shop_then_shop_entry(self):
        from core.services import settlements as st
        from core.services import shops as sh

        drawer = self.shop.account(Account.Kind.CASH_DRAWER)
        s0, d0 = self.bal(drawer), self.bal(self.d_f1)
        sent = sh.desk_to_shop(self.c_f1, shop=self.shop, cash=D(40000))
        self.assertEqual((self.bal(drawer), self.bal(self.d_f1)), (s0, d0 - 40000))
        s = self.login("shop.molyko")
        self.assertNotContains(s.get(f"/shops/{self.shop.pk}/"), sent.reference)
        self.assertNotContains(s.get("/shops/transactions/?preset=all"), sent.reference)
        got = sh.entry(self.m, source="DESK", desk=self.c_f1.subzone, product="CASH", amount=D(40000))
        self.assertEqual(self.bal(drawer), s0 + 40000)  # the shop's drawer goes up with its own entry
        self.assertFalse(got.to_settle)
        self.assertEqual(st.unpaid_for_shop(self.shop), [])
        c = self.login("cashier.fako1")
        self.assertEqual(c.get(f"/transactions/{got.pk}/").status_code, 404)

    def test_stock_exchange_each_side(self):
        from core.services import shops as sh

        cash, uve = self.shop.account(Account.Kind.CASH_DRAWER), self.shop.account(Account.Kind.UVE)
        c0, u0, d0, du0 = self.bal(cash), self.bal(uve), self.bal(self.d_f1), self.bal(self.uve_f1)
        t = sh.exchange(self.m, partner="DESK", desk=self.c_f1.subzone, give="UVE", amount=D(25000))
        self.assertEqual((self.bal(cash), self.bal(uve)), (c0 + 25000, u0 - 25000))
        self.assertEqual((self.bal(self.d_f1), self.bal(self.uve_f1)), (d0, du0))  # the cashier records his own
        self.assertEqual(str(t.display_type), "Stock exchange: gave UVE, received cash")
        x = ops.stock_exchange(self.c_f1, partner="SHOP", partner_shop=self.shop, give="CASH", amount=D(25000))
        self.assertEqual((self.bal(self.d_f1), self.bal(self.uve_f1)), (d0 - 25000, du0 + 25000))
        self.assertEqual((self.bal(cash), self.bal(uve)), (c0 + 25000, u0 - 25000))
        with self.assertRaises(BusinessError):
            ops.stock_exchange(self.c_f1, partner="DESK", desk=self.c_f1.subzone, give="CASH", amount=D(1))
        with self.assertRaises(BusinessError):
            sh.exchange(self.m, partner="SHOP", partner_shop=self.shop, give="CASH", amount=D(1))
        # shop to shop exchange
        from core.models import Shop, Zone

        other = Shop.objects.create(name="Other shop", zone=Zone.objects.filter(parent__isnull=False).first())
        sh.exchange(self.m, partner="SHOP", partner_shop=other, give="CASH", amount=D(5000))
        # pages
        s = self.login("shop.molyko")
        self.assertContains(s.get("/shops/do/exchange/"), "Stock exchange")
        r = s.post("/shops/do/exchange/", {"idem": str(uuid.uuid4()), "partner": "DESK", "desk": self.c_f2.subzone.pk,
                                           "give": "UVE", "amount": "1 000"})
        self.assertEqual(r.status_code, 302)
        self.assertNotContains(s.get("/shops/transactions/?preset=all"), x.reference)  # the cashier's own record
        c = self.login("cashier.fako1")
        self.assertContains(c.get("/"), "/stock-exchange/")
        r = c.post("/stock-exchange/", {"idem": str(uuid.uuid4()), "partner": "SHOP", "partner_shop": self.shop.pk,
                                        "give": "UVE", "amount": "2 000"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Transaction.objects.filter(type=TxType.STOCK_EXCHANGE).count(), 2)

    def test_agent_payback_at_a_shop_needs_no_confirmation(self):
        from core.models import AgentPayback, User
        from core.services import agents as ag

        agent_user = User.objects.filter(role=User.Role.AGENT).first()
        p = ag.request_payback(agent_user, shop=self.shop, cash=D(1000))
        self.assertEqual(p.status, AgentPayback.Status.RECORDED)
        self.assertFalse(ag.pending_for_shop(self.shop).exists())


class ShopDialCodes(OrgMixin, TestCase):
    """Shop cash-in / recharge / UVE transfers: record, then the Orange code goes to the dial pad."""

    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]
        from core.services import shop_shifts as ss

        self.m = self.o["shop_mgr"]
        for k in (Account.Kind.CASH_DRAWER, Account.Kind.UVE, Account.Kind.ER):
            self.fund(self.shop.account(k), 300000)
        ss.open_shift(self.m, cash=self.bal(self.shop.account(Account.Kind.CASH_DRAWER)),
                      uve=self.bal(self.shop.account(Account.Kind.UVE)), er=self.bal(self.shop.account(Account.Kind.ER)))
        self.c = self.client_class()
        self.assertTrue(self.c.login(username="shop.molyko", password=PW))

    def post(self, op, data):
        return self.c.post(f"/shops/do/{op}/", dict(data, idem=str(uuid.uuid4())), HTTP_X_REQUESTED_WITH="fetch").json()

    def test_codes_and_buttons(self):
        page = self.c.get("/shops/do/cashin/")
        self.assertContains(page, ">CASHIN<")
        self.assertNotContains(page, 'name="commission"')
        self.assertContains(self.c.get("/shops/do/cashout/"), ">CASHOUT<")
        self.assertContains(self.c.get("/shops/do/airtime/"), ">RECHARGE<")
        d = self.post("cashin", {"amount": "5 000", "client_phone": "677 12 34 56"})
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["code"], "#149*1*5000*677123456#")
        t = Transaction.objects.get(type=TxType.SHOP_CASHIN)
        self.assertIsNone(t.commission)
        self.assertFalse(self.post("cashin", {"amount": "5 000"})["ok"])  # the number is needed
        d = self.post("cashout", {"amount": "3 000"})
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["code"], "")  # recorded normally, nothing to dial
        d = self.post("airtime", {"kind": "NORMAL", "amount": "1 000", "client_phone": "699887766"})
        self.assertEqual(d["code"], "#189*1*699887766*1000#")
        d = self.post("pos_uve_out", {"pos": self.p1.business_sim, "amount": "2 000", "pay": "CASH"})
        self.assertEqual(d["code"], f"#149*5*{self.p1.business_sim}*2000#")
        d = self.post("destockage", {"agent": self.a1.business_sim, "amount": "4 000", "pay": "CASH"})
        self.assertEqual(d["code"], f"#149*5*{self.a1.business_sim}*4000#")
        # receiving UVE from an agent is recorded normally
        d = self.post("pos_uve_in", {"pos": self.a1.business_sim, "amount": "1 000", "pay": "CASH"})
        self.assertTrue(d["ok"], d)
        self.assertNotIn("code", {k for k, v in d.items() if v})
