"""Shops: separate from the cashiers, own capital (cash, UVE, ER), services, sales, agents and visibility."""
import uuid

from django.utils.html import escape

from django.test import TestCase

from core.models import Account, ServiceCount, Shop, ShopService, SimRetrieval, Transaction, TxType, User
from core.services import shops as sh
from core.services.errors import BusinessError, InsufficientFunds, PermissionDenied

from .helpers import D, OrgMixin
from .test_web import PW

K = Account.Kind


def open_shifts(o):
    """Shop staff record only during an open shift."""
    from core.services import shop_shifts as ss

    b = sh.shop_balances(o["shop"])
    ss.open_shift(o["shop_mgr"], cash=b[K.CASH_DRAWER], uve=b[K.UVE], er=b[K.ER])
    for k in ("shop_om", "shop_air"):
        ss.open_shift(o[k])


class ShopMoney(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop, self.m = self.o["shop"], self.o["shop_mgr"]
        for kind, amt in ((K.CASH_DRAWER, 300000), (K.UVE, 800000), (K.ER, 150000)):
            self.fund(self.shop.account(kind), amt)
        open_shifts(self.o)

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

    CLIENT = dict(client_name="Ngwa Peter", client_address="Molyko, Buea", client_country="Cameroon",
                  id_type="CNI", id_number="998877", phone="677445566", whatsapp="677445566")

    def test_airtime_kinds(self):
        n = sh.airtime(self.m, amount=D(5000))
        g = sh.airtime(self.m, amount=D(3000), kind="GOOD_DEAL")
        self.assertEqual((n.subtype, g.subtype), ("NORMAL", "GOOD_DEAL"))
        with self.assertRaises(BusinessError):
            sh.airtime(self.m, amount=D(1), kind="BAD")

    def test_transfer_to_cashier_cash_and_uve_on_cashier_side(self):
        f1 = self.zones["fako1"]
        ubefore, dbefore, sb = self.bal(self.uve_f1), self.bal(self.d_f1), self.b()
        t = sh.to_cashier(self.m, desk=f1, cash=D(30000), uve=D(100000))
        self.assertEqual(self.bal(self.uve_f1), ubefore + 100000)
        self.assertEqual(self.bal(self.d_f1), dbefore + 30000)
        self.assertEqual((self.b()[0], self.b()[1]), (sb[0] - 30000, sb[1] - 100000))
        parts = t.payment_parts
        self.assertEqual(len(parts), 2)  # one record: cash part + UVE part, one receipt
        self.assertEqual(sum(1 for p in parts if p.receipt_no), 1)
        self.assertIsNotNone(t.cashier_day_id)  # recorded in the cashier subzone's day
        self.assertEqual(t.cashier_day.zone, f1)
        one = sh.to_cashier(self.m, desk=f1, uve=D(1000))  # a single product: no group
        self.assertIsNone(one.payment_group)
        with self.assertRaises(BusinessError):  # nothing typed
            sh.to_cashier(self.m, desk=f1)

    def test_cashier_transfers_cash_and_uve_to_shop(self):
        sb, ubefore, dbefore = self.b(), self.bal(self.uve_f1), self.bal(self.d_f1)
        t = sh.desk_to_shop(self.c_f1, shop=self.shop, cash=D(20000), uve=D(40000))
        self.assertEqual((t.type, t.shop, t.cashier), (TxType.DESK_TO_SHOP, self.shop, self.c_f1))
        self.assertEqual((self.b()[0], self.b()[1]), (sb[0] + 20000, sb[1] + 40000))
        self.assertEqual((self.bal(self.uve_f1), self.bal(self.d_f1)), (ubefore - 40000, dbefore - 20000))
        self.assertEqual(len(t.payment_parts), 2)
        with self.assertRaises(PermissionDenied):
            sh.desk_to_shop(self.m, shop=self.shop, uve=D(1))  # shop managers are not cashiers

    def test_agent_owes_the_shop(self):
        sh.destockage(self.m, agent=self.a1, amount=D(30000), pay="CREDIT")
        x = sh.shop_balances(self.shop)
        self.assertEqual((x[K.CASH_DRAWER], x[K.UVE], x["owed_by_agents"]), (D(300000), D(770000), D(30000)))
        with self.assertRaises(BusinessError):
            sh.agent_repay(self.m, agent=self.a1, amount=D(40000), pay="CASH")  # owes only 30 000
        sh.agent_repay(self.m, agent=self.a1, amount=D(20000), pay="CASH")
        sh.agent_repay(self.m, agent=self.a1, amount=D(10000), pay="UVE")
        x = sh.shop_balances(self.shop)
        self.assertEqual((x[K.CASH_DRAWER], x[K.UVE], x["owed_by_agents"]), (D(320000), D(780000), D(0)))
        self.assertEqual(self.bal(self.a1.account), D(0))  # the cashier-side balance is untouched

    def test_stock_by_serial_and_sale_with_client(self):
        from core.models import ItemSale, StockUnit

        phone = self.o["items"]["phone"]
        units = sh.receive_stock(self.o["shop_senior"], shop=self.shop, item=phone,
                                 serials="356000000000011\n356000000000029, 356000000000037")
        self.assertEqual((len(units), phone.stock_at(self.shop)), (3, 3))
        with self.assertRaises(BusinessError):  # already in stock
            sh.receive_stock(self.o["shop_senior"], shop=self.shop, item=phone, serials="356000000000011")
        with self.assertRaises(BusinessError):  # typed twice
            sh.receive_stock(self.o["shop_senior"], shop=self.shop, item=phone, serials="A1\nA1")
        with self.assertRaises(BusinessError):  # no unit chosen
            sh.sell(self.m, item=phone, units=[], **self.CLIENT)
        bad = dict(self.CLIENT, client_name="")
        with self.assertRaises(BusinessError):  # the client must be identified
            sh.sell(self.m, item=phone, units=units[:1], **bad)
        t = sh.sell(self.m, item=phone, units=units[:2], **self.CLIENT)
        self.assertEqual((t.amount, phone.stock_at(self.shop)), (D(130000), 1))
        self.assertEqual(self.b()[0], D(430000))
        s = ItemSale.objects.get(transaction=t)
        self.assertEqual((s.client_name, s.id_number, s.units.count()), ("Ngwa Peter", "998877", 2))
        with self.assertRaises(BusinessError):  # cannot sell the same unit twice
            sh.sell(self.m, item=phone, units=units[:1], **self.CLIENT)
        sh.remove_unit(self.o["shop_senior"], serial="356000000000037", note="damaged")
        self.assertEqual(StockUnit.objects.get(serial="356000000000037").status, StockUnit.Status.REMOVED)
        self.assertEqual(phone.stock_at(self.shop), 0)

    def test_services_and_retrieval(self):
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
        open_shifts(self.o)

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_shop_manager_desk_and_privacy(self):
        c = self.client_for("shop.molyko")
        self.assertRedirects(c.get("/"), "/shops/", fetch_redirect_response=False)
        self.assertRedirects(c.get("/shops/"), f"/shops/{self.shop.pk}/")
        r = c.post("/shops/do/cashin/", {"idem": str(uuid.uuid4()), "amount": "50 000", "commission": "250"})
        self.assertEqual(r.status_code, 200)  # the account name is required
        self.assertContains(r, "Account name")
        r = c.post("/shops/do/cashin/", {"idem": str(uuid.uuid4()), "amount": "50 000", "commission": "250",
                                         "account_name": "Ngwa Peter", "client_phone": "677123456"})
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
        # a UVE transfer to a cashier shows on both sides
        r = c.post("/shops/do/to_cashier/", {"idem": str(uuid.uuid4()), "desk": self.zones["fako1"].pk, "uve": "5000"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        tr = Transaction.objects.get(type=TxType.SHOP_TO_DESK)
        self.assertEqual(self.client_for("cashier.fako1").get(f"/transactions/{tr.pk}/").status_code, 200)
        self.assertEqual(c.get(f"/transactions/{tr.pk}/").status_code, 200)
        # the cashier's transfer to the shop page
        cf = self.client_for("cashier.fako1")
        self.assertEqual(cf.get("/transfer/to-shop/").status_code, 200)
        r = cf.post("/transfer/to-shop/", {"idem": str(uuid.uuid4()), "shop": self.shop.pk, "uve": "7 000"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        self.assertTrue(Transaction.objects.filter(type=TxType.DESK_TO_SHOP, amount=7000).exists())
        # and cashiers do not see shop dealings
        self.assertEqual(self.client_for("cashier.fako1").get(f"/transactions/{shop_txn.pk}/").status_code, 404)
        self.assertEqual(self.client_for("cashier.fako1").get("/shops/").status_code, 403)

    def test_services_retrieval_sale_pages(self):
        c = self.client_for("shop.molyko")
        svc = ShopService.objects.get(name__startswith="OTT")
        maxit = ShopService.objects.get(name__startswith="MAX IT")
        page = c.get("/shops/services/new/")
        for x in ShopService.objects.filter(active=True, kind=ShopService.Kind.COUNT):
            self.assertContains(page, escape(x.name))  # every service is listed, no drop-down
        r = c.post("/shops/services/new/", {"idem": str(uuid.uuid4()), "business_date": "2026-10-30"})
        self.assertContains(r, "at least one service")
        r = c.post("/shops/services/new/", {"idem": str(uuid.uuid4()), f"svc_{svc.pk}": "4",
                                            f"svc_{maxit.pk}": "2", "business_date": "2026-10-30"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        self.assertEqual(sorted(ServiceCount.objects.values_list("service__name", "count")),
                         sorted([(svc.name, 4), (maxit.name, 2)]))
        r = c.post("/shops/retrieve/new/", {"idem": str(uuid.uuid4()), "sim_number": "699887766",
                                            "holder_name": "Ebai Joseph", "id_type": "CNI", "id_number": "123",
                                            "business_date": "2026-10-30"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        self.assertEqual(SimRetrieval.objects.count(), 1)
        self.assertContains(c.get("/shops/retrievals/"), "Ebai Joseph")
        kit = self.o["items"]["kit"]
        units = sh.receive_stock(self.o["shop_senior"], shop=self.shop, item=kit, serials="OEK-1\nOEK-2")
        phone = self.o["items"]["phone"]
        ph = sh.receive_stock(self.o["shop_senior"], shop=self.shop, item=phone, serials="IMEI-A\nIMEI-B")
        form = c.get("/shops/do/sale/")
        self.assertContains(form, 'data-cat="%s"' % kit.category_id)
        for txt in ("OEK-1", "IMEI-A", "Basket"):
            self.assertContains(form, txt)
        client = {"client_name": "Ngwa Peter", "client_address": "Molyko", "client_country": "Cameroon",
                  "id_type": "PASSPORT", "id_number": "P123", "phone": "677445566", "whatsapp": ""}
        # one client buys an energy kit and two phones (one price negotiated) in a single sale
        r = c.post("/shops/do/sale/", {"idem": str(uuid.uuid4()), "units": [units[0].pk, ph[0].pk, ph[1].pk],
                                       f"price_{phone.pk}": "60 000", **client})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        sales = list(Transaction.objects.filter(type=TxType.SHOP_SALE).order_by("id"))
        self.assertEqual(len(sales), 2)  # one line per item, one group, one receipt
        self.assertEqual(sales[0].payment_group, sales[1].payment_group)
        self.assertEqual(sorted(t.amount for t in sales), [D(45000), D(120000)])
        self.assertEqual(sum(1 for t in sales if t.receipt_no), 1)
        rec = c.get(f"/transactions/{sales[0].pk}/")
        for txt in ("Ngwa Peter", "P123", "OEK-1", "IMEI-A", "IMEI-B", "Cameroon", "165 000"):
            self.assertContains(rec, txt)
        # nothing ticked is refused, and a sold unit cannot be sold again
        r = c.post("/shops/do/sale/", {"idem": str(uuid.uuid4()), **client})
        self.assertEqual(r.status_code, 200)
        r = c.post("/shops/do/sale/", {"idem": str(uuid.uuid4()), "units": [ph[0].pk], **client})
        self.assertEqual(r.status_code, 200)
        self.assertContains(c.get("/shops/stock/?q=OEK-1"), "Ngwa Peter")
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
        r = senior.post("/shops/setup/", {"action": "category", "cat-name": "Solar lamps", "cat-active": "on",
                                          "cat-sort": "9"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        from core.models import ItemCategory, ShopConversionRate
        self.assertTrue(ItemCategory.objects.filter(name="Solar lamps").exists())
        # the admin sets the shop rate from the main conversion-rate page too
        adm = self.client_for("admin")
        self.assertContains(adm.get("/setup/conversion-rate/"), "Shops")
        r = adm.post("/setup/conversion-rate/", {"action": "shop", "shop-percent": "4.5"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        self.assertEqual(ShopConversionRate.current().percent, D("4.50"))
        # finance and the senior manager see cashiers and shops in two separate spaces
        fin = self.client_for("finance")
        rep = fin.get("/reports/reconciliation/?preset=all")
        self.assertContains(rep, "CASHIERS TOTAL")
        self.assertNotContains(rep, "Shop Molyko")  # shops are not mixed into the cashiers' reports
        self.assertContains(rep, 'class="space-switch"')
        self.assertNotContains(rep, "/shops/attendance/")  # cashiers' space: no shop menu
        shops_page = fin.get("/shops/")
        self.assertContains(shops_page, "/shops/attendance/")
        self.assertNotContains(shops_page, "/closings/")  # shops' space: no cashier menu
        self.assertEqual(self.client_for("junior.fako").get("/reports/").status_code, 200)
        # opening grid has the shop row
        # the shops have their own opening page; the cashiers' grid no longer lists them
        self.assertNotContains(self.client_for("admin").get("/setup/opening-balances/"), "Shop Molyko")
        self.assertContains(self.client_for("admin").get("/shops/opening-balances/"), "Shop Molyko")

    def test_shop_manager_needs_shop(self):
        from django.core.exceptions import ValidationError

        u = User(username="x", role=User.Role.SHOP_MANAGER)
        with self.assertRaises(ValidationError):
            u.clean()


class ShopDutiesAndAdministration(OrgMixin, TestCase):
    """Segmented shops (staff duties) and the senior shop manager's administration of shops and staff."""

    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]
        for kind, amt in ((K.CASH_DRAWER, 300000), (K.UVE, 800000), (K.ER, 150000)):
            self.fund(self.shop.account(kind), amt)
        open_shifts(self.o)

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_duties_are_enforced(self):
        om, air = self.o["shop_om"], self.o["shop_air"]
        self.assertEqual(om.shop_duty_set, {"OM"})
        sh.cash_in(om, amount=D(1000))
        with self.assertRaises(PermissionDenied):
            sh.airtime(om, amount=D(1000))
        with self.assertRaises(PermissionDenied):
            sh.appro(om, agent=self.a1, amount=D(1000))
        with self.assertRaises(PermissionDenied):
            sh.receive_stock(om, shop=self.shop, item=self.o["items"]["kit"], serials="Z1")
        with self.assertRaises(PermissionDenied):
            sh.cash_out(air, amount=D(1000))
        sh.airtime(air, amount=D(1000))
        sh.count_service(air, service=ShopService.objects.get(name__startswith="OTT"), count=1)
        # pages: the Orange Money desk sees only its own entries in the menu
        c = self.client_for("shop.molyko.om")
        self.assertEqual(c.get("/shops/do/cashin/").status_code, 200)
        self.assertEqual(c.get("/shops/do/airtime/").status_code, 403)
        self.assertEqual(c.get("/shops/services/new/").status_code, 403)
        page = c.get(f"/shops/{self.shop.pk}/")
        self.assertContains(page, "/shops/do/cashin/")
        self.assertNotContains(page, "/shops/do/airtime/")
        self.assertNotContains(page, "/shops/do/appro/")
        # the shop page lists each staff member with their duties and activity
        senior_page = self.client_for("shop.senior").get(f"/shops/{self.shop.pk}/")
        self.assertContains(senior_page, "Staff and duties")
        self.assertContains(senior_page, "Molyko Orange Money desk")

    def test_senior_manages_shops(self):
        from core.models import Account
        c = self.client_for("shop.senior")
        self.assertEqual(c.get("/shops/manage/").status_code, 200)
        r = c.post("/shops/manage/", {"action": "shop_save", "shop-name": "Shop Mile 17", "shop-code": "shop-mile17",
                                      "shop-sim_number": "690000123", "shop-address": "Mile 17", "shop-active": "on"})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        new = Shop.objects.get(code="shop-mile17")
        self.assertEqual(Account.objects.filter(shop=new).count(), 4)  # cash, UVE, ER and stock at cost
        r = c.post(f"/shops/manage/?shop={new.pk}", {"action": "shop_save", "shop-name": "Shop Mile 17 Buea",
                                                    "shop-code": "shop-mile17", "shop-active": "on"})
        self.assertEqual(r.status_code, 302)
        new.refresh_from_db()
        self.assertEqual(new.name, "Shop Mile 17 Buea")
        # a used shop cannot be deleted (staff, operations) — an unused one can
        c.post("/shops/manage/", {"action": "shop_delete", "pk": self.shop.pk})
        self.assertTrue(Shop.objects.filter(pk=self.shop.pk).exists())
        c.post("/shops/manage/", {"action": "shop_delete", "pk": new.pk})
        self.assertFalse(Shop.objects.filter(pk=new.pk).exists())
        # other roles cannot
        self.assertEqual(self.client_for("shop.molyko").get("/shops/manage/").status_code, 403)
        self.assertEqual(self.client_for("finance").get("/shops/manage/").status_code, 403)

    def test_senior_manages_staff_access(self):
        c = self.client_for("shop.senior")
        other = Shop.objects.create(name="Shop Limbe", code="shop-limbe")
        r = c.post("/shops/manage/", {"action": "staff_save", "staff-username": "limbe.om", "staff-first_name": "Ayuk",
                                      "staff-shop": other.pk, "staff-duties": ["OM"]})
        self.assertEqual(r.status_code, 200, r.content[:2000])
        self.assertContains(r, "Temporary password")
        u = User.objects.get(username="limbe.om")
        self.assertEqual((u.role, u.shop, u.shop_duties, u.must_change_password),
                         (User.Role.SHOP_MANAGER, other, "OM", True))
        # transfer to Molyko with all duties
        r = c.post(f"/shops/manage/?staff={u.pk}", {"action": "staff_save", "staff-first_name": "Ayuk",
                                                   "staff-shop": self.shop.pk,
                                                   "staff-duties": ["OM", "AIRTIME", "SALES", "SERVICES", "AGENTS",
                                                                    "EXPENSES"]})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        u.refresh_from_db()
        self.assertEqual((u.shop, u.shop_duties, u.username), (self.shop, "", "limbe.om"))
        r = c.post("/shops/manage/", {"action": "staff_reset", "pk": u.pk})
        self.assertContains(r, "Temporary password")
        c.post("/shops/manage/", {"action": "staff_toggle", "pk": u.pk})
        u.refresh_from_db()
        self.assertFalse(u.is_active)
        # someone who recorded operations is only deactivated, never deleted
        sh.cash_in(self.o["shop_om"], amount=D(1000))
        c.post("/shops/manage/", {"action": "staff_delete", "pk": self.o["shop_om"].pk})
        self.assertTrue(User.objects.filter(pk=self.o["shop_om"].pk).exists())
        c.post("/shops/manage/", {"action": "staff_delete", "pk": u.pk})
        self.assertFalse(User.objects.filter(pk=u.pk).exists())
        # the senior cannot touch non-shop accounts
        c.post("/shops/manage/", {"action": "staff_toggle", "pk": self.c_f1.pk})
        self.c_f1.refresh_from_db()
        self.assertTrue(self.c_f1.is_active)

    def test_senior_deletes_unused_definitions(self):
        from core.models import ItemCategory, ShopItem
        c = self.client_for("shop.senior")
        cat = ItemCategory.objects.create(name="Solar lamps")
        item = ShopItem.objects.create(name="Lamp", category=cat, price=5000)
        svc = ShopService.objects.create(name="Unused service", kind=ShopService.Kind.COUNT)
        c.post("/shops/setup/", {"action": "delete_category", "pk": cat.pk})  # has an item: refused
        self.assertTrue(ItemCategory.objects.filter(pk=cat.pk).exists())
        c.post("/shops/setup/", {"action": "delete_item", "pk": item.pk})
        c.post("/shops/setup/", {"action": "delete_category", "pk": cat.pk})
        c.post("/shops/setup/", {"action": "delete_service", "pk": svc.pk})
        self.assertFalse(ShopItem.objects.filter(pk=item.pk).exists())
        self.assertFalse(ItemCategory.objects.filter(pk=cat.pk).exists())
        self.assertFalse(ShopService.objects.filter(pk=svc.pk).exists())
        used = ShopService.objects.get(name__startswith="OTT")
        sh.count_service(self.o["shop_mgr"], service=used, count=1)
        r = c.post("/shops/setup/", {"action": "delete_service", "pk": used.pk}, follow=True)
        self.assertContains(r, "cannot be deleted")
        self.assertTrue(ShopService.objects.filter(pk=used.pk).exists())


class ShopShiftsAndReconciliation(OrgMixin, TestCase):
    def setUp(self):
        from core.services import shop_shifts as ss

        self.ss = ss
        self.make_org()
        self.fund_all()
        self.shop, self.m = self.o["shop"], self.o["shop_mgr"]
        for kind, amt in ((K.CASH_DRAWER, 300000), (K.UVE, 800000), (K.ER, 150000)):
            self.fund(self.shop.account(kind), amt)

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_no_recording_without_an_open_shift(self):
        with self.assertRaises(PermissionDenied):
            sh.cash_in(self.m, amount=D(1000))
        c = self.client_for("shop.molyko")
        r = c.get("/shops/do/cashin/")
        self.assertRedirects(r, "/shops/shift/?next=/shops/do/cashin/", fetch_redirect_response=False)
        self.assertContains(c.get(f"/shops/{self.shop.pk}/"), "Your shift is closed")

    def test_open_and_close_shifts_with_counts(self):
        ss = self.ss
        with self.assertRaises(BusinessError):  # first on shift: the till must be counted
            ss.open_shift(self.m)
        s1 = ss.open_shift(self.m, cash=D(300000), uve=D(800000), er=D(149000))  # 1 000 ER missing
        self.assertEqual(s1.open_diffs, {"cash": D(0), "uve": D(0), "er": D(-1000)})
        with self.assertRaises(BusinessError):
            ss.open_shift(self.m, cash=D(1), uve=D(1), er=D(1))  # already open
        s2 = ss.open_shift(self.o["shop_om"])  # someone already counted: optional
        self.assertIsNone(s2.open_cash)
        sh.cash_in(self.o["shop_om"], amount=D(50000), commission=D(250))
        sh.airtime(self.m, amount=D(5000))
        ss.close_shift(self.o["shop_om"])  # others still on shift: counts optional
        with self.assertRaises(BusinessError):  # last to leave counts the till
            ss.close_shift(self.m)
        with self.assertRaises(BusinessError):  # a difference must be explained
            ss.close_shift(self.m, cash=D(354000), uve=D(750000), er=D(144000))
        done = ss.close_shift(self.m, cash=D(354000), uve=D(750000), er=D(144000), note="1 000 short, recounting")
        self.assertEqual(done.close_diffs, {"cash": D(-1000), "uve": D(0), "er": D(-1000)})
        with self.assertRaises(PermissionDenied):
            sh.cash_in(self.m, amount=D(1))  # closed again
        rec = ss.day_reconciliation(self.shop, s1.business_date)
        cash, uve, er = rec["rows"]
        # the test funding is posted the same day, so it counts as money in
        self.assertEqual((cash["sys_open"], cash["money_in"], cash["sys_close"]), (D(0), D(355000), D(355000)))
        self.assertEqual((uve["money_out"], uve["sys_close"]), (D(50000), D(750000)))
        self.assertEqual((er["open_diff"], er["close_diff"], cash["close_diff"]), (D(-1000), D(-1000), D(-1000)))
        self.assertEqual(rec["status"], "DIFFERENCE")
        self.assertEqual(rec["commission"], D(250))
        with self.assertRaises(BusinessError):  # a difference must be explained
            ss.review_day(self.o["shop_senior"], shop=self.shop, day=s1.business_date)
        with self.assertRaises(PermissionDenied):
            ss.review_day(self.m, shop=self.shop, day=s1.business_date, note="x")
        ss.review_day(self.o["shop_senior"], shop=self.shop, day=s1.business_date, note="1 000 short, recounted")
        _q, people = ss.attendance([self.shop], s1.business_date, s1.business_date)
        self.assertEqual({str(p["user"]) for p in people}, {str(self.m), str(self.o["shop_om"])})

    def test_balanced_day_and_forced_close_pages(self):
        ss = self.ss
        s = ss.open_shift(self.m, cash=D(300000), uve=D(800000), er=D(150000))
        senior = self.client_for("shop.senior")
        url = f"/shops/{self.shop.pk}/reconciliation/?date={s.business_date.isoformat()}"
        self.assertContains(senior.get(url), "Shift(s) still open")
        r = senior.post(url, {"action": "review", "note": ""})
        self.assertContains(r, "still open")
        senior.post(url, {"action": "force", "pk": s.pk, "note": ""})  # a reason is required
        s.refresh_from_db()
        self.assertEqual(s.status, "OPEN")
        senior.post(url, {"action": "force", "pk": s.pk, "note": "Forgot to close"})
        s.refresh_from_db()
        self.assertEqual((s.status, s.closed_by), ("CLOSED", self.o["shop_senior"]))
        self.assertEqual(ss.day_reconciliation(self.shop, s.business_date)["status"], "NOT_COUNTED")
        # a counted, balanced day
        c = self.client_for("shop.molyko")
        r = c.post("/shops/shift/", {"cash": "300 000", "uve": "800 000", "er": "150 000"})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        r = c.post("/shops/shift/", {"cash": "300 000", "uve": "800 000", "er": "150 000"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(ss.day_reconciliation(self.shop, s.business_date)["status"], "BALANCED")
        r = senior.post(url, {"action": "review", "note": ""})
        self.assertEqual(r.status_code, 302)
        # pages for the different roles
        for u in ("finance", "manager", "shop.senior", "shop.molyko", "admin"):
            cl = self.client_for(u)
            self.assertContains(cl.get(f"/shops/reconciliation/?date={s.business_date.isoformat()}"), "Balanced")
            self.assertEqual(cl.get(url).status_code, 200)
            self.assertEqual(cl.get("/shops/attendance/").status_code, 200)
        self.assertContains(c.get("/shops/attendance/"), "Shop manager Molyko")
        self.assertEqual(self.client_for("cashier.fako1").get("/shops/attendance/").status_code, 403)
        # a staff member cannot be transferred during an open shift
        from core.services import shop_admin as sa
        ss.open_shift(self.m, cash=D(300000), uve=D(800000), er=D(150000))
        other = Shop.objects.create(name="Shop Limbe", code="shop-limbe")
        with self.assertRaises(BusinessError):
            sa.update_staff(self.o["shop_senior"], self.m, first_name="X", shop=other, duties=[])


class ShopReports(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]
        for kind, amt in ((K.CASH_DRAWER, 300000), (K.UVE, 800000), (K.ER, 150000)):
            self.fund(self.shop.account(kind), amt)
        open_shifts(self.o)
        sh.cash_in(self.o["shop_om"], amount=D(50000), commission=D(250))
        sh.airtime(self.o["shop_air"], amount=D(10000), kind="GOOD_DEAL")
        sh.count_services(self.o["shop_air"], counts={ShopService.objects.get(name__startswith="OTT"): 3})

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_daily_weekly_monthly(self):
        from core.reports.shops import ShopReportForm, shop_summary
        from core.services.days import douala_today

        today = douala_today()
        shops = Shop.objects.filter(active=True)
        for period, tables in (("day", 5), ("week", 7), ("month", 7)):
            form = ShopReportForm(shops, {"period": period, "day": today.isoformat()})
            rep, p, d_from, d_to = shop_summary(form, shops)
            self.assertEqual(len(rep.tables), tables)
            titles = [t.title for t in rep.tables]
            self.assertIn("SIM retrievals", titles)
            self.assertIn("Attendance summary", titles)
            by_shop = next(t for t in rep.tables if t.title == "By shop — activity").rows[0]
            self.assertEqual((by_shop["cashin"], by_shop["commission"], by_shop["good_deal"], by_shop["services"]),
                             (D(50000), D(250), D(10000), 3))
            self.assertEqual(by_shop["cash_c"], D(360000))
        self.assertEqual(shop_summary(ShopReportForm(shops, {"period": "week", "day": today.isoformat()}),
                                      shops)[0].tables[0].rows.__len__(), 7)
        fin = self.client_for("finance")
        for q in ("", "?period=week", "?period=month", "?period=custom&date_from=2026-10-01&date_to=2026-10-31",
                  "?period=month&format=xlsx", "?period=week&format=pdf"):
            self.assertEqual(fin.get("/shops/reports/" + q).status_code, 200, q)
        self.assertContains(fin.get("/shops/reports/?period=month"), "Monthly shop report")
        self.assertEqual(self.client_for("shop.molyko").get("/shops/reports/").status_code, 200)
        self.assertEqual(self.client_for("cashier.fako1").get("/shops/reports/").status_code, 403)


class DutyBasedViews(OrgMixin, TestCase):
    """A segmented shop's staff see only the operations of their duties, everywhere."""

    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]
        for kind, amt in ((K.CASH_DRAWER, 300000), (K.UVE, 800000), (K.ER, 150000)):
            self.fund(self.shop.account(kind), amt)
        open_shifts(self.o)
        self.cashin = sh.cash_in(self.o["shop_om"], amount=D(50000), commission=D(250))
        self.air = sh.airtime(self.o["shop_air"], amount=D(7000), kind="GOOD_DEAL")
        sh.retrieve_sim(self.o["shop_air"], sim_number="699887766", holder_name="Ebai Joseph", id_type="CNI",
                        id_number="1234567")

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_orange_money_desk_sees_only_orange_money(self):
        c = self.client_for("shop.molyko.om")
        self.assertEqual(c.get(f"/transactions/{self.cashin.pk}/").status_code, 200)
        self.assertEqual(c.get(f"/transactions/{self.air.pk}/").status_code, 404)
        lst = c.get("/shops/transactions/")
        self.assertContains(lst, self.cashin.reference)
        self.assertNotContains(lst, self.air.reference)
        page = c.get(f"/shops/{self.shop.pk}/")
        self.assertContains(page, "Orange Money cash-in")
        self.assertNotContains(page, "airtime sale")
        self.assertNotContains(page, "Owed to agents")
        self.assertNotContains(page, "Staff and duties")
        for url in ("/shops/retrievals/", "/shops/services/", "/shops/stock/"):
            self.assertEqual(c.get(url).status_code, 403, url)
        rep = c.get("/shops/reports/?period=week")
        self.assertContains(rep, "Your duties only")
        self.assertContains(rep, "Cash-in")
        self.assertNotContains(rep, "of which good deal")
        self.assertNotContains(rep, "Ebai Joseph")  # SIM retrievals belong to the services duty
        self.assertNotContains(rep, "Molyko airtime desk")  # attendance: own shifts only
        rec = c.get(f"/shops/{self.shop.pk}/reconciliation/")
        self.assertContains(rec, "Orange Money cash-in")
        self.assertNotContains(rec, "airtime sale")

    def test_airtime_desk_and_full_manager(self):
        c = self.client_for("shop.molyko.airtime")
        self.assertEqual(c.get(f"/transactions/{self.cashin.pk}/").status_code, 404)
        self.assertEqual(c.get(f"/transactions/{self.air.pk}/").status_code, 200)
        rep = c.get("/shops/reports/?period=week")
        self.assertContains(rep, "of which good deal")
        self.assertContains(rep, "Ebai Joseph")
        self.assertNotContains(rep, "Cash-in")
        full = self.client_for("shop.molyko")
        self.assertEqual(full.get(f"/transactions/{self.air.pk}/").status_code, 200)
        r = full.get("/shops/reports/?period=week")
        for txt in ("Cash-in", "of which good deal", "Ebai Joseph", "Molyko airtime desk", "Attendance summary"):
            self.assertContains(r, txt)
        self.assertNotContains(r, "Your duties only")


class CashManagerAdministration(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_finance_adds_edits_deletes_cash_managers(self):
        from core.models import CashManager
        from core.services import operations as ops

        fin = self.client_for("finance")
        page = fin.get("/cash-managers/")
        self.assertContains(page, "Add a cash manager")
        r = fin.post("/cash-managers/", {"action": "cm_save", "cm-name": "Mama Rose", "cm-phone": "677 11 22 33",
                                         "cm-notes": "Market", "cm-active": "on"})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        cm = CashManager.objects.get(name="Mama Rose")
        self.assertEqual(cm.phone, "677112233")
        r = fin.post(f"/cash-managers/?cm={cm.pk}", {"action": "cm_save", "cm-name": "Mama Rose Ngwa",
                                                    "cm-phone": "677112233", "cm-active": "on"})
        self.assertEqual(r.status_code, 302)
        cm.refresh_from_db()
        self.assertEqual(cm.name, "Mama Rose Ngwa")
        fin.post("/cash-managers/", {"action": "cm_delete", "pk": cm.pk})
        self.assertFalse(CashManager.objects.filter(pk=cm.pk).exists())
        # one with exchanges is only deactivated
        used = CashManager.objects.first()
        ops.record_exchange(self.c_f1, cash_manager=used, amount=D(10000), give_kind="CASH", amount_served=D(10000))
        r = fin.post("/cash-managers/", {"action": "cm_delete", "pk": used.pk}, follow=True)
        self.assertContains(r, "cannot be deleted")
        self.assertTrue(CashManager.objects.filter(pk=used.pk).exists())
        # only finance and the super admin manage them
        self.assertContains(self.client_for("admin").get("/cash-managers/"), "Add a cash manager")
        for u in ("manager", "junior.fako", "cashier.fako1"):
            self.assertNotContains(self.client_for(u).get("/cash-managers/"), "Add a cash manager")
        n = CashManager.objects.count()
        self.client_for("manager").post("/cash-managers/", {"action": "cm_save", "cm-name": "X",
                                                            "cm-phone": "677000001", "cm-active": "on"})
        self.assertEqual(CashManager.objects.count(), n)


class ShopOpeningBalances(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.shop = self.o["shop"]

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_grid_and_agent_positions(self):
        adm = self.client_for("admin")
        page = adm.get("/shops/opening-balances/")
        self.assertContains(page, "Each shop: cash, UVE and ER")
        cash, uve, er = (self.shop.account(k) for k in (K.CASH_DRAWER, K.UVE, K.ER))
        r = adm.post("/shops/opening-balances/", {"action": "grid", "batch": str(uuid.uuid4()),
                                                  "business_date": "2026-10-01", "note": "launch",
                                                  f"acct_{cash.pk}": "300 000", f"acct_{uve.pk}": "800 000",
                                                  f"acct_{er.pk}": "150 000"})
        self.assertRedirects(r, "/shops/opening-balances/", fetch_redirect_response=False)
        self.assertEqual((self.bal(cash), self.bal(uve), self.bal(er)), (D(300000), D(800000), D(150000)))
        t = Transaction.objects.get(type=TxType.OPENING_BALANCE, opening_for_account=cash)
        self.assertEqual((t.shop, str(t.business_date)), (self.shop, "2026-10-01"))
        self.assertContains(adm.get("/shops/opening-balances/"), "All control accounts have their opening balance")
        # positions with commercial agents
        r = adm.post("/shops/opening-balances/", {"action": "single", "idem": str(uuid.uuid4()), "shop": self.shop.pk,
                                                  "agent": "691000001", "direction": "AGENT_OWES", "amount": "20 000",
                                                  "business_date": "2026-10-01"})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        r = adm.post("/shops/opening-balances/", {"action": "single", "idem": str(uuid.uuid4()), "shop": self.shop.pk,
                                                  "agent": "691000002", "direction": "SHOP_OWES", "amount": "35 000",
                                                  "business_date": "2026-10-01"})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        b = sh.shop_balances(self.shop)
        self.assertEqual((b["owed_by_agents"], b["owed_agents"]), (D(20000), D(35000)))
        self.assertEqual(self.bal(self.a1.account), D(0))  # the agent's balance with the company is untouched
        rec = adm.get("/shops/opening-balances/")
        self.assertContains(rec, "the agent owes the shop")
        self.assertContains(rec, "the shop owes the agent")
        # a second opening for the same position is refused
        r = adm.post("/shops/opening-balances/", {"action": "single", "idem": str(uuid.uuid4()), "shop": self.shop.pk,
                                                  "agent": "691000001", "direction": "AGENT_OWES", "amount": "1 000",
                                                  "business_date": "2026-10-01"})
        self.assertContains(r, "already has an opening balance")
        # the shop sees its openings; the cashiers' page lists none of them
        self.assertEqual(self.client_for("shop.molyko").get(f"/transactions/{t.pk}/").status_code, 200)
        self.assertNotContains(adm.get("/setup/opening-balances/"), t.reference)
        for u in ("finance", "shop.senior", "shop.molyko"):
            self.assertEqual(self.client_for(u).get("/shops/opening-balances/").status_code, 403)


class ReceiveUnitsByLine(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.shop = self.o["shop"]

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_quantity_then_imei_and_serial_per_unit(self):
        from core.models import StockUnit

        phone = self.o["items"]["phone"]
        c = self.client_for("shop.senior")
        page = c.get("/shops/stock/")
        self.assertContains(page, "Number of units received")
        self.assertNotContains(page, "<textarea")
        base = {"action": "receive", "shop": self.shop.pk, "category": phone.category_id, "item": phone.pk,
                "quantity": "3", "note": "delivery 12"}
        r = c.post("/shops/stock/", dict(base, unit_imei_1="356 000 000 000 011", unit_serial_1="SN-A1",
                                         unit_imei_2="356000000000029", unit_serial_2="",
                                         unit_imei_3="", unit_serial_3="SN-A3"))
        self.assertRedirects(r, "/shops/stock/", fetch_redirect_response=False)
        u1 = StockUnit.objects.get(imei="356000000000011")
        self.assertEqual((u1.serial, u1.ident), ("SN-A1", "IMEI 356000000000011 · S/N SN-A1"))
        self.assertEqual(StockUnit.objects.get(serial="SN-A3").imei, None)
        self.assertEqual(phone.stock_at(self.shop), 3)
        # a line with neither number, a bad IMEI, a duplicate, or a number already registered: nothing is saved
        for extra, msg in (({"unit_imei_1": "", "unit_serial_1": ""}, "enter its IMEI or its serial number"),
                           ({"unit_imei_1": "12345", "unit_serial_1": ""}, "an IMEI has 15 digits"),
                           ({"unit_imei_1": "", "unit_serial_1": "sn-a1"}, "Already registered"),
                           ({"unit_imei_1": "356000000000037", "unit_serial_1": "X"}, "Typed twice")):
            data = dict(base, quantity="2", unit_imei_2="356000000000037", unit_serial_2="", **extra)
            r = c.post("/shops/stock/", data)
            self.assertContains(r, msg)
            self.assertContains(r, 'name="unit_imei_2"')  # the lines come back with what was typed
        self.assertEqual(phone.stock_at(self.shop), 3)
        # find and remove by IMEI; the sale picker shows the IMEI
        self.assertContains(c.get("/shops/stock/?q=356000000000029"), "356000000000029")
        r = c.post("/shops/stock/", {"action": "remove", "serial": "356000000000029", "note": "damaged"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(StockUnit.objects.get(imei="356000000000029").status, StockUnit.Status.REMOVED)
        self.assertContains(self.client_for("admin").get("/shops/do/sale/"), "IMEI 356000000000011")


class AdminSpace(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()

    def test_administration_lives_in_its_own_tab(self):
        c = self.client_class()
        self.assertTrue(c.login(username="admin", password=PW))
        cash = c.get("/")
        self.assertContains(cash, 'class="space-switch three"')
        for url in ("/administration/users/", "/administration/import/", "/shops/manage/", "/capital/"):
            self.assertNotContains(cash, f'href="{url}"')  # not crowding the cashiers' menu
        shops = c.get("/shops/")
        for url in ("/administration/users/", "/shops/manage/", "/shops/opening-balances/"):
            self.assertNotContains(shops, f'href="{url}"')
        admin = c.get("/administration/")
        for url in ("/administration/users/", "/administration/import/", "/administration/clear/", "/shops/manage/",
                    "/shops/opening-balances/", "/setup/opening-balances/", "/capital/"):
            self.assertContains(admin, f'href="{url}"')
        self.assertNotContains(admin, 'href="/closings/"')  # the Admin tab shows only administration
        # finance keeps two tabs and its Capital link; the senior shop manager keeps Shops & staff
        f = self.client_class()
        f.login(username="finance", password=PW)
        page = f.get("/")
        self.assertContains(page, 'class="space-switch"')
        self.assertContains(page, 'href="/capital/"')
        s = self.client_class()
        s.login(username="shop.senior", password=PW)
        self.assertContains(s.get("/shops/"), 'href="/shops/manage/"')


class ShopExpensesPurchasesIncome(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop, self.m = self.o["shop"], self.o["shop_mgr"]
        for kind, amt in ((K.CASH_DRAWER, 300000), (K.UVE, 800000), (K.ER, 150000)):
            self.fund(self.shop.account(kind), amt)
        open_shifts(self.o)

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_expense_purchase_cost_of_sales_income_statement(self):
        from core.models import ExpenseCategory, StockUnit
        from core.reports.shops import ShopReportForm, income_statement

        cat = ExpenseCategory.objects.first()
        sh.expense(self.m, category=cat, amount=D(12000), pay="CASH", description="Electricity October")
        sh.expense(self.m, category=cat, amount=D(3000), pay="UVE", description="Transport")
        with self.assertRaises(BusinessError):
            sh.expense(self.m, category=cat, amount=D(1), description="")
        box = self.o["items"]["box"]
        p = sh.purchase(self.m, item=box, units=[("356222222222222", "FB-01"), ("", "FB-02"), ("", "FB-03")],
                        unit_cost=D(22000), pay="CASH", supplier="Orange Cameroun", description="INV-77")
        self.assertEqual((p.amount, p.supplier), (D(66000), "Orange Cameroun"))
        self.assertEqual(self.bal(self.shop.account(K.SHOP_INVENTORY)), D(66000))
        b = sh.shop_balances(self.shop)
        self.assertEqual((b[K.CASH_DRAWER], b[K.UVE]), (D(300000 - 12000 - 66000), D(797000)))
        units = list(StockUnit.objects.filter(item=box).order_by("id"))
        self.assertEqual([u.cost for u in units], [D(22000)] * 3)
        sh.sell(self.m, units=units[:2], prices={box.pk: D(30000)}, client_name="X", client_address="Y",
                client_country="Z", id_type="CNI", id_number="1", phone="677445566")
        self.assertEqual(self.bal(self.shop.account(K.SHOP_INVENTORY)), D(22000))  # cost of the 2 sold left
        sh.remove_unit(self.o["shop_senior"], serial="FB-03", note="damaged")
        self.assertEqual(self.bal(self.shop.account(K.SHOP_INVENTORY)), D(0))
        sh.cash_in(self.o["shop_om"], amount=D(50000), commission=D(300))
        sh.convert(self.m, amount=D(100000))  # shop rate 3.5 % -> 3 500 gain
        shops = Shop.objects.filter(active=True)
        from core.services.days import douala_today
        rep, *_x = income_statement(ShopReportForm(shops, {"kind": "income", "period": "day",
                                                            "day": douala_today().isoformat()}), shops)
        rows = {r["line"]: r[f"s{self.shop.pk}"] for r in rep.tables[0].rows}
        self.assertEqual(rows["Item sales"], D(60000))
        self.assertEqual(rows["Cost of the items sold"], D(44000))
        self.assertEqual(rows["Margin on items"], D(16000))
        self.assertEqual(rows["Orange Money commissions"], D(300))
        self.assertEqual(rows["UVE→ER conversion gain"], D(3500))
        self.assertEqual(rows["TOTAL INCOME"], D(19800))
        self.assertEqual(rows["Stock written off"], D(22000))
        self.assertEqual(rows["TOTAL COSTS"], D(15000 + 22000))
        self.assertEqual(rows["NET RESULT"], D(19800 - 37000))

    def test_pages_duties_and_section_downloads(self):
        from core.models import ExpenseCategory

        c = self.client_for("shop.molyko")
        cat = ExpenseCategory.objects.first()
        r = c.post("/shops/do/expense/", {"idem": str(uuid.uuid4()), "category": cat.pk, "amount": "8 000",
                                          "pay": "CASH", "description": "Rent"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        kit = self.o["items"]["kit"]
        self.assertContains(c.get("/shops/purchase/"), "Number of units bought")
        r = c.post("/shops/purchase/", {"idem": str(uuid.uuid4()), "category": kit.category_id, "item": kit.pk,
                                        "quantity": "2", "unit_imei_1": "", "unit_serial_1": "OEK-P1",
                                        "unit_imei_2": "", "unit_serial_2": "OEK-P2", "unit_cost": "30 000",
                                        "pay": "UVE", "supplier": "Supplier SA"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        pt = Transaction.objects.get(type=TxType.SHOP_PURCHASE)
        rec = c.get(f"/transactions/{pt.pk}/")
        for txt in ("Supplier SA", "OEK-P1", "OEK-P2", "30 000"):
            self.assertContains(rec, txt)
        # the expenses duty is needed; the Orange Money desk has not got it
        om = self.client_for("shop.molyko.om")
        self.assertEqual(om.get("/shops/do/expense/").status_code, 403)
        self.assertEqual(om.get("/shops/purchase/").status_code, 403)
        self.assertEqual(om.get("/shops/reports/?kind=income").status_code, 403)
        # income statement page, and downloading one section only
        fin = self.client_for("finance")
        page = fin.get("/shops/reports/?kind=income&period=month")
        self.assertContains(page, "NET RESULT")
        self.assertContains(page, "The whole report")
        week = fin.get("/shops/reports/?period=week")
        self.assertContains(week, "&format=xlsx&table=0")
        whole = fin.get("/shops/reports/?period=week&format=xlsx")
        one = fin.get("/shops/reports/?period=week&format=xlsx&table=0")
        self.assertEqual((whole.status_code, one.status_code), (200, 200))
        self.assertLess(len(one.content), len(whole.content))
        self.assertEqual(fin.get("/shops/reports/?kind=income&format=pdf&table=0").status_code, 200)
        self.assertEqual(fin.get("/reports/reconciliation/?preset=all&format=xlsx&table=1").status_code, 200)


class DeskTransfers(OrgMixin, TestCase):
    """Cashier -> cashier transfers in cash, UVE and / or MTN float, in one form with one box per product."""

    def setUp(self):
        self.make_org()
        self.fund_all()

    def test_cash_and_uve_between_desks(self):
        from core.services import operations as ops

        f2 = self.zones["fako2"]
        d2, u2, d1, u1 = self.bal(self.d_f2), self.bal(self.uve_f2), self.bal(self.d_f1), self.bal(self.uve_f1)
        txns = ops.record_desk_transfer(self.c_f1, desk=f2, cash=D(50000), uve=D(80000), description="float")
        self.assertEqual(len(txns), 2)
        self.assertEqual((self.bal(self.d_f2), self.bal(self.uve_f2)), (d2 + 50000, u2 + 80000))
        self.assertEqual((self.bal(self.d_f1), self.bal(self.uve_f1)), (d1 - 50000, u1 - 80000))
        self.assertEqual(txns[0].payment_group, txns[1].payment_group)
        with self.assertRaises(BusinessError):
            ops.record_desk_transfer(self.c_f1, desk=self.zones["fako1"], cash=D(1))  # not to yourself
        with self.assertRaises(BusinessError):
            ops.record_desk_transfer(self.c_f1, desk=f2)  # nothing typed
        # the page: two boxes, no product drop-down; the receiving cashier sees it
        c = self.client_class()
        c.login(username="cashier.fako1", password=PW)
        page = c.get("/transfer/new/")
        self.assertContains(page, 'name="cash"')
        self.assertContains(page, 'name="uve"')
        self.assertNotContains(page, 'name="source"')
        r = c.post("/transfer/new/", {"idem": str(uuid.uuid4()), "desk": f2.pk, "cash": "10 000", "uve": "5 000"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        rec = c.get(r["Location"])
        self.assertContains(rec, "15 000")
        c2 = self.client_class()
        c2.login(username="cashier.fako2", password=PW)
        self.assertEqual(c2.get(r["Location"]).status_code, 200)


class TransfersForEveryStaffMember(OrgMixin, TestCase):
    """Transfers between a shop and the cashiers, and between shops, are open to every staff member."""

    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]
        self.other = Shop.objects.create(name="Shop Limbe", code="shop-limbe")
        for kind, amt in ((K.CASH_DRAWER, 300000), (K.UVE, 800000), (K.ER, 150000)):
            self.fund(self.shop.account(kind), amt)
        open_shifts(self.o)

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_orange_money_desk_transfers_to_cashier_and_shop(self):
        om = self.o["shop_om"]  # duties: Orange Money only
        t = sh.to_cashier(om, desk=self.zones["fako1"], cash=D(10000))
        self.assertEqual(t.type, TxType.SHOP_TO_DESK)
        s = sh.to_shop(om, dest_shop=self.other, cash=D(20000), uve=D(50000), description="float for Limbe")
        self.assertEqual(len(s.payment_parts), 2)
        self.assertEqual(sum(1 for p in s.payment_parts if p.receipt_no), 1)
        self.assertEqual((self.bal(self.other.account(K.CASH_DRAWER)), self.bal(self.other.account(K.UVE))),
                         (D(20000), D(50000)))
        b = sh.shop_balances(self.shop)
        self.assertEqual((b[K.CASH_DRAWER], b[K.UVE]), (D(300000 - 10000 - 20000), D(750000)))
        with self.assertRaises(BusinessError):
            sh.to_shop(om, dest_shop=self.shop, cash=D(1))  # not to itself
        with self.assertRaises(BusinessError):
            sh.to_shop(om, dest_shop=self.other)  # nothing typed
        # pages, menu and shop-page buttons for the Orange Money desk
        c = self.client_for("shop.molyko.om")
        page = c.get(f"/shops/{self.shop.pk}/")
        self.assertContains(page, "/shops/do/to_cashier/")
        self.assertContains(page, "/shops/do/to_shop/")
        form = c.get("/shops/do/to_shop/")
        self.assertContains(form, 'name="cash"')
        self.assertContains(form, 'name="uve"')
        self.assertNotContains(form, f'<option value="{self.shop.pk}"')  # not its own shop
        r = c.post("/shops/do/to_shop/", {"idem": str(uuid.uuid4()), "dest_shop": self.other.pk, "uve": "5 000"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        self.assertEqual(c.get(f"/transactions/{s.pk}/").status_code, 200)
        self.assertContains(c.get("/shops/transactions/"), s.reference)
        # the receiving shop sees it too: its staff, its page and its list
        lim = User.objects.create(username="limbe.mgr", role=User.Role.SHOP_MANAGER, shop=self.other)
        lim.set_password(PW)
        lim.save()
        lc = self.client_for("limbe.mgr")
        self.assertEqual(lc.get(f"/transactions/{s.pk}/").status_code, 200)
        self.assertContains(lc.get("/shops/transactions/"), s.reference)
        self.assertContains(lc.get(f"/shops/{self.other.pk}/"), "transfer received from another shop")
        # cashiers never see shop-to-shop transfers
        self.assertEqual(self.client_for("cashier.fako1").get(f"/transactions/{s.pk}/").status_code, 404)
        # the report: transfers appear for the Orange Money desk too
        rep = c.get("/shops/reports/?period=day")
        self.assertContains(rep, "To other shops")
        self.assertContains(rep, "To cashiers")


class CashInAccountName(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.shop = self.o["shop"]
        for kind, amt in ((K.CASH_DRAWER, 300000), (K.UVE, 800000), (K.ER, 150000)):
            self.fund(self.shop.account(kind), amt)
        open_shifts(self.o)

    def test_account_name_recorded_and_shown(self):
        t = sh.cash_out(self.o["shop_om"], amount=D(20000), commission=D(200), account_name=" Ebai Joseph ",
                        client_phone="699887766")
        self.assertEqual(t.account_name, "Ebai Joseph")
        c = self.client_class()
        c.login(username="shop.molyko.om", password=PW)
        self.assertContains(c.get(f"/transactions/{t.pk}/"), "Ebai Joseph")
        self.assertContains(c.get("/shops/transactions/"), "Ebai Joseph")
        self.assertContains(c.get("/shops/do/cashout/"), 'name="account_name"')


class TransferReconciliation(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]
        self.other = Shop.objects.create(name="Shop Limbe", code="shop-limbe")
        for kind, amt in ((K.CASH_DRAWER, 300000), (K.UVE, 800000), (K.ER, 150000)):
            self.fund(self.shop.account(kind), amt)
        open_shifts(self.o)

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_every_internal_transfer_reconciled(self):
        from core.services import capital as cap
        from core.services import operations as ops

        ops.record_desk_transfer(self.c_f1, desk=self.zones["fako2"], cash=D(50000), uve=D(80000))
        sh.desk_to_shop(self.c_f1, shop=self.shop, cash=D(20000))
        sh.to_cashier(self.o["shop_om"], desk=self.zones["fako2"], uve=D(30000))
        sh.to_shop(self.o["shop_om"], dest_shop=self.other, cash=D(10000))
        cap.transfer(self.finance, source=self.d_me, destination=self.bank, amount=D(5000), purpose="deposit")
        fin = self.client_for("finance")
        r = fin.get("/reports/transfers/?preset=all")
        self.assertContains(r, "Transfer reconciliation")
        self.assertContains(r, "OK — sent equals received")
        for txt in ("Who sent what to whom", "By desk and shop", "Shop [SAMPLE] Shop Molyko", "Shop Shop Limbe",
                    "Fako 2", "195 000"):  # 50 000 + 80 000 + 20 000 + 30 000 + 10 000 + 5 000
            self.assertContains(r, txt)
        self.assertEqual(fin.get("/reports/transfers/?preset=all&format=xlsx&table=1").status_code, 200)
        self.assertEqual(fin.get("/reports/transfers/?preset=all&format=pdf").status_code, 200)
        # a cashier sees the transfers his desk sent or received
        mine = self.client_for("cashier.fako2").get("/reports/transfers/?preset=all")
        self.assertContains(mine, "80 000")
        self.assertNotContains(mine, "Shop Limbe")
        # the shops' side: transfers touching the shops only
        sr = self.client_for("shop.senior").get("/shops/reports/?kind=transfers&period=day")
        self.assertContains(sr, "Transfer reconciliation")
        self.assertContains(sr, "Shop Shop Limbe")
        self.assertNotContains(sr, "80 000")  # cashier-to-cashier transfers are not the shops'
        om = self.client_for("shop.molyko.om").get("/shops/reports/?kind=transfers&period=day")
        self.assertEqual(om.status_code, 200)


class ShiftClosingAndTransferPayments(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]
        self.m = self.o["shop_mgr"]
        for kind, amt in ((K.CASH_DRAWER, 300000), (K.UVE, 800000), (K.ER, 150000)):
            self.fund(self.shop.account(kind), amt)

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_expected_shown_and_note_required_on_difference(self):
        from core.services import shop_shifts as ss

        ss.open_shift(self.m, cash=D(300000), uve=D(800000), er=D(150000))
        c = self.client_for("shop.molyko")
        page = c.get("/shops/shift/")
        self.assertContains(page, '"cash": 300000')  # the expected running balances are on the page
        self.assertContains(page, "Expected now")
        r = c.post("/shops/shift/", {"cash": "299 000", "uve": "800 000", "er": "150 000", "note": ""})
        self.assertContains(r, "Explain the difference in the note")
        self.assertIsNotNone(ss.current_shift(self.m))
        r = c.post("/shops/shift/", {"cash": "299 000", "uve": "800 000", "er": "150 000",
                                     "note": "1 000 given back to a client"})
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(ss.current_shift(self.m))

    def test_pay_back_a_received_transfer_and_automatic_note(self):
        from core.services import settlements as st
        from core.services import shop_shifts as ss

        t = sh.desk_to_shop(self.c_f1, shop=self.shop, cash=D(20000), uve=D(50000))
        self.assertTrue(t.to_settle)
        self.assertEqual(st.outstanding(t), D(70000))
        ss.open_shift(self.m, cash=D(320000), uve=D(850000), er=D(150000))
        c = self.client_for("shop.molyko")
        detail = c.get(f"/transactions/{t.pk}/")
        self.assertContains(detail, "Pay the sender")
        self.assertContains(detail, "still owed")
        self.assertContains(c.get("/shops/shift/"), "Transfers received not paid back yet")
        # the cashier who sent it cannot "pay" it; too much is refused
        self.assertFalse(st.can_pay(self.c_f1, t))
        r = c.post(f"/transactions/{t.pk}/pay/", {"idem": str(uuid.uuid4()), "cash": "80 000"})
        self.assertContains(r, "Only 70 000 FCFA is still owed")
        d1 = self.bal(self.d_f1)
        r = c.post(f"/transactions/{t.pk}/pay/", {"idem": str(uuid.uuid4()), "cash": "50 000"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        self.assertEqual(self.bal(self.d_f1), d1 + 50000)  # paid back to the sender's desk
        p = Transaction.objects.get(settles=t)
        self.assertEqual((p.type, p.to_settle), (TxType.SHOP_TO_DESK, False))  # a payment is not paid back again
        self.assertEqual(st.outstanding(t), D(20000))
        # closing with 20 000 still owed: listed automatically in the closing note, with the reason
        b = sh.shop_balances(self.shop)
        done = ss.close_shift(self.m, cash=b[K.CASH_DRAWER], uve=b[K.UVE], er=b[K.ER])
        self.assertIn("Transfers received not paid back yet", done.close_note)
        self.assertIn(t.reference, done.close_note)
        self.assertIn("20 000", done.close_note)
        # pay the rest: nothing owed any more
        ss.open_shift(self.m, cash=b[K.CASH_DRAWER], uve=b[K.UVE], er=b[K.ER])
        st.pay(self.m, t, uve=D(20000))
        self.assertEqual(st.outstanding(t), D(0))
        self.assertEqual(st.unpaid_for_shop(self.shop), [])
        self.assertContains(c.get(f"/transactions/{t.pk}/"), "Fully paid back")

    def test_cashier_pays_back_a_cashier(self):
        from core.services import operations as ops
        from core.services import settlements as st

        t = ops.record_desk_transfer(self.c_f1, desk=self.zones["fako2"], cash=D(30000))[0]
        self.assertTrue(st.can_pay(self.c_f2, t))
        self.assertEqual([h.pk for h, _o in st.unpaid_for_desk(self.zones["fako2"])], [t.pk])
        c = self.client_for("cashier.fako2")
        r = c.post(f"/transactions/{t.pk}/pay/", {"idem": str(uuid.uuid4()), "uve": "30 000"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        self.assertEqual(st.outstanding(t), D(0))


class CashierTransferPayments(OrgMixin, TestCase):
    """Cashiers see what they received and have not paid back, pay it, and the closing notes list what is left."""

    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]
        for kind, amt in ((K.CASH_DRAWER, 300000), (K.UVE, 800000), (K.ER, 150000)):
            self.fund(self.shop.account(kind), amt)
        open_shifts(self.o)

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_home_closing_and_payment(self):
        from core.services import closing as cl
        from core.services import operations as ops
        from core.services import settlements as st
        from core.services.ledger import balance

        t1 = sh.to_cashier(self.o["shop_om"], desk=self.zones["fako2"], uve=D(40000))  # shop -> Fako 2
        t2 = ops.record_desk_transfer(self.c_f1, desk=self.zones["fako2"], cash=D(25000))[0]  # Fako 1 -> Fako 2
        c = self.client_for("cashier.fako2")
        home = c.get("/")
        self.assertContains(home, "Transfers received not paid back yet")
        self.assertContains(home, t1.reference)
        self.assertContains(home, "Fako 1")  # the sending desk is named
        page = c.get("/closing/")
        self.assertContains(page, t2.reference)
        self.assertContains(page, "65 000")  # total still owed
        # pay the shop back from the closing page
        r = c.post(f"/transactions/{t1.pk}/pay/?next=/closing/", {"idem": str(uuid.uuid4()), "cash": "40 000"})
        self.assertRedirects(r, "/closing/", fetch_redirect_response=False)
        self.assertEqual(st.outstanding(t1), D(0))
        p = Transaction.objects.get(settles=t1)
        self.assertEqual((p.type, p.shop), (TxType.DESK_TO_SHOP, self.shop))
        # close the day: the transfer still owed to Fako 1 is written automatically into the notes
        actuals = {a.id: balance(a) for a in cl.closing_accounts(self.c_f2)}
        closing = cl.close_day(self.c_f2, actuals=actuals, notes="all good")
        self.assertIn("all good", closing.notes)
        self.assertIn(t2.reference, closing.notes)
        self.assertIn("25 000", closing.notes)
        self.assertNotIn(t1.reference, closing.notes)
        # the shop page shows its staff what the shop owes (nothing here) and cashiers what they owe
        self.assertNotContains(self.client_for("shop.molyko").get(f"/shops/{self.shop.pk}/"),
                               "Transfers received not paid back yet")
