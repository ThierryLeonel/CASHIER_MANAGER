"""Shops: separate from the cashiers, own capital (cash, UVE, ER), services, sales, agents and visibility."""
import uuid

from django.test import TestCase

from core.models import Account, ServiceCount, Shop, ShopService, SimRetrieval, Transaction, TxType, User
from core.services import shops as sh
from core.services.errors import BusinessError, InsufficientFunds, PermissionDenied

from .helpers import D, OrgMixin
from .test_web import PW

K = Account.Kind


class ShopMoney(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop, self.m = self.o["shop"], self.o["shop_mgr"]
        for kind, amt in ((K.CASH_DRAWER, 300000), (K.UVE, 800000), (K.ER, 150000)):
            self.fund(self.shop.account(kind), amt)

    def b(self):
        x = sh.shop_balances(self.shop)
        return (x[K.CASH_DRAWER], x[K.UVE], x[K.ER], x["owed_agents"])

    def test_cash_in_out_airtime_conversion(self):
        t = sh.cash_in(self.m, amount=D(50000), commission=D(250), client_phone="677123456")
        self.assertEqual((t.shop, t.commission, t.cashier, t.zone), (self.shop, D(250), None, None))
        self.assertEqual(self.b(), (D(350000), D(750000), D(150000), D(0)))
        sh.cash_out(self.m, amount=D(20000), commission=D(200))
        self.assertEqual(self.b(), (D(330000), D(770000), D(150000), D(0)))
        sh.airtime(self.m, amount=D(5000))
        self.assertEqual(self.b(), (D(335000), D(770000), D(145000), D(0)))
        c = sh.convert(self.m, amount=D(100000))  # sample shop rate 3.5 %
        self.assertEqual((c.rate_used, c.amount_received), (D("0.035"), D(103500)))
        self.assertEqual(self.b(), (D(335000), D(670000), D(248500), D(0)))
        with self.assertRaises(InsufficientFunds):
            sh.cash_out(self.m, amount=D(10_000_000))

    def test_agent_dealings_and_credit(self):
        sh.appro(self.m, agent=self.a1, amount=D(200000), pay="CASH")
        self.assertEqual(self.b(), (D(100000), D(1000000), D(150000), D(0)))
        sh.er_supply(self.m, agent=self.a2, amount=D(50000), pay="CREDIT")
        self.assertEqual(self.b(), (D(100000), D(1000000), D(200000), D(50000)))
        sh.er_supply(self.m, agent=self.a2, amount=D(10000), pay="UVE")
        self.assertEqual(self.b(), (D(100000), D(990000), D(210000), D(50000)))
        sh.destockage(self.m, agent=self.a1, amount=D(30000))
        self.assertEqual(self.b(), (D(130000), D(960000), D(210000), D(50000)))
        with self.assertRaises(BusinessError):
            sh.pay_agent(self.m, agent=self.a2, amount=D(60000), pay="CASH")  # owes only 50 000
        sh.pay_agent(self.m, agent=self.a2, amount=D(50000), pay="CASH")
        self.assertEqual(self.b(), (D(80000), D(960000), D(210000), D(0)))
        # the agent's balance with the company (cashier side) is untouched by shop dealings
        self.assertEqual(self.bal(self.a1.account), D(0))
        self.assertEqual(self.bal(self.a2.account), D(0))

    def test_transfer_to_cashier_sale_services_retrieval(self):
        before = self.bal(self.d_f1)
        sh.to_cashier(self.m, kind="CASH", destination=self.d_f1, amount=D(100000))
        self.assertEqual(self.bal(self.d_f1), before + 100000)
        with self.assertRaises(BusinessError):  # cash must go to a drawer
            sh.to_cashier(self.m, kind="CASH", destination=self.uve_f1, amount=D(1))
        phone = self.o["items"]["phone"]
        with self.assertRaises(BusinessError):  # nothing in stock yet
            sh.sell(self.m, item=phone, quantity=1)
        sh.receive_stock(self.o["shop_senior"], shop=self.shop, item=phone, quantity=3)
        t = sh.sell(self.m, item=phone, quantity=2)
        self.assertEqual((t.amount, phone.stock_at(self.shop)), (D(130000), 1))
        self.assertEqual(self.b()[0], D(330000))
        sh.count_service(self.m, service=ShopService.objects.get(name__startswith="MAX IT"), count=6)
        self.assertEqual(ServiceCount.objects.get().count, 6)
        r = sh.retrieve_sim(self.m, sim_number="+237 699 88 77 66", holder_name="Ebai Joseph", id_type="CNI",
                            id_number="1234567")
        self.assertEqual((r.sim_number, r.shop), ("699887766", self.shop))
        with self.assertRaises(BusinessError):
            sh.retrieve_sim(self.m, sim_number="699887766", holder_name="", id_type="CNI", id_number="1")

    def test_only_own_shop(self):
        other = Shop.objects.create(name="Other shop", code="other")
        with self.assertRaises(PermissionDenied):
            sh.cash_in(self.m, shop=other, amount=D(1))
        with self.assertRaises(PermissionDenied):
            sh.cash_in(self.c_f1, amount=D(1))  # cashiers do not record shop operations
        sh.cash_in(self.o["shop_senior"], shop=self.shop, amount=D(1000))  # the senior shop manager chooses a shop


class ShopPages(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]
        for kind, amt in ((K.CASH_DRAWER, 300000), (K.UVE, 800000), (K.ER, 150000)):
            self.fund(self.shop.account(kind), amt)

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_shop_manager_desk_and_privacy(self):
        c = self.client_for("shop.molyko")
        self.assertRedirects(c.get("/"), "/shops/", fetch_redirect_response=False)
        self.assertRedirects(c.get("/shops/"), f"/shops/{self.shop.pk}/")
        r = c.post("/shops/do/cashin/", {"idem": str(uuid.uuid4()), "amount": "50 000", "commission": "250"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        r = c.post("/shops/do/appro/", {"idem": str(uuid.uuid4()), "agent": "691000001", "amount": "10 000",
                                        "pay": "CREDIT"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        page = c.get(f"/shops/{self.shop.pk}/")
        self.assertContains(page, "350 000")  # cash after the cash-in
        self.assertContains(page, "Owed to commercial agents")
        # shops never see the agents' balances or the cashier side
        res = c.get("/api/customers/?q=691000001").json()["results"]
        self.assertTrue(res and all(x["hide_balance"] and x["balance"] == 0 for x in res))
        self.assertEqual(c.get("/customers/").status_code, 403)
        self.assertEqual(c.get("/transactions/").status_code, 403)
        self.assertEqual(c.get("/reports/statement/?customer_sim=691000001").status_code, 403)
        cashier_txn = Transaction.objects.filter(shop__isnull=True).first()
        self.assertEqual(c.get(f"/transactions/{cashier_txn.pk}/").status_code, 404)
        shop_txn = Transaction.objects.filter(shop=self.shop).first()
        self.assertEqual(c.get(f"/transactions/{shop_txn.pk}/").status_code, 200)
        # and cashiers do not see shop dealings
        self.assertEqual(self.client_for("cashier.fako1").get(f"/transactions/{shop_txn.pk}/").status_code, 404)
        self.assertEqual(self.client_for("cashier.fako1").get("/shops/").status_code, 403)

    def test_services_retrieval_sale_pages(self):
        c = self.client_for("shop.molyko")
        svc = ShopService.objects.get(name__startswith="OTT")
        r = c.post("/shops/services/new/", {"idem": str(uuid.uuid4()), "service": svc.pk, "count": "4",
                                            "business_date": "2026-10-30"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        r = c.post("/shops/retrieve/new/", {"idem": str(uuid.uuid4()), "sim_number": "699887766",
                                            "holder_name": "Ebai Joseph", "id_type": "CNI", "id_number": "123",
                                            "business_date": "2026-10-30"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        self.assertEqual(SimRetrieval.objects.count(), 1)
        self.assertContains(c.get("/shops/retrievals/"), "Ebai Joseph")
        sh.receive_stock(self.o["shop_senior"], shop=self.shop, item=self.o["items"]["kit"], quantity=2)
        r = c.post("/shops/do/sale/", {"idem": str(uuid.uuid4()), "item": self.o["items"]["kit"].pk, "quantity": "1"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        self.assertContains(c.get("/shops/stock/"), "Orange Energie kit S")
        self.assertEqual(c.get("/shops/setup/").status_code, 403)  # only the senior shop manager / admin

    def test_finance_and_senior_see_all_shops(self):
        sh.cash_in(self.o["shop_mgr"], amount=D(50000), commission=D(250))
        for u in ("finance", "manager", "shop.senior", "admin"):
            r = self.client_for(u).get("/shops/")
            self.assertContains(r, "Shop Molyko")
            self.assertContains(r, "350 000")
        self.assertEqual(self.client_for("junior.fako").get("/shops/").status_code, 403)
        senior = self.client_for("shop.senior")
        r = senior.post("/shops/setup/", {"action": "service", "svc-name": "Internet bundle activation",
                                          "svc-kind": "COUNT", "svc-active": "on", "svc-sort": "60"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(ShopService.objects.filter(name="Internet bundle activation").exists())
        r = senior.post("/shops/setup/", {"action": "rate", "rate-percent": "3"})
        self.assertEqual(r.status_code, 302)
        # the finance reconciliation includes the shops in the company total
        rep = self.client_for("finance").get("/reports/reconciliation/?preset=all")
        self.assertContains(rep, "SHOPS")
        # opening grid has the shop row
        self.assertContains(self.client_for("admin").get("/setup/opening-balances/"), "Shop Molyko")

    def test_shop_manager_needs_shop(self):
        from django.core.exceptions import ValidationError

        u = User(username="x", role=User.Role.SHOP_MANAGER)
        with self.assertRaises(ValidationError):
            u.clean()
