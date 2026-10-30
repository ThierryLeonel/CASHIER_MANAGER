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

    CLIENT = dict(client_name="Ngwa Peter", client_address="Molyko, Buea", client_country="Cameroon",
                  id_type="CNI", id_number="998877", phone="677445566", whatsapp="677445566")

    def test_airtime_kinds(self):
        n = sh.airtime(self.m, amount=D(5000))
        g = sh.airtime(self.m, amount=D(3000), kind="GOOD_DEAL")
        self.assertEqual((n.subtype, g.subtype), ("NORMAL", "GOOD_DEAL"))
        with self.assertRaises(BusinessError):
            sh.airtime(self.m, amount=D(1), kind="BAD")

    def test_transfer_to_cashier_is_uve_and_on_cashier_side(self):
        before, sbefore = self.bal(self.uve_f1), self.b()[1]
        t = sh.to_cashier(self.m, destination=self.uve_f1, amount=D(100000))
        self.assertEqual(self.bal(self.uve_f1), before + 100000)
        self.assertEqual(self.b()[1], sbefore - 100000)
        self.assertIsNotNone(t.cashier_day_id)  # recorded in the cashier subzone's day
        self.assertEqual(t.zone, t.cashier_day.zone)
        with self.assertRaises(BusinessError):  # only UVE: never into a cash drawer
            sh.to_cashier(self.m, destination=self.d_f1, amount=D(1))

    def test_cashier_transfers_uve_to_shop(self):
        sbefore, before = self.b()[1], self.bal(self.uve_f1)
        t = sh.desk_to_shop(self.c_f1, shop=self.shop, amount=D(40000))
        self.assertEqual((t.type, t.shop, t.cashier), (TxType.DESK_TO_SHOP, self.shop, self.c_f1))
        self.assertEqual(self.b()[1], sbefore + 40000)
        self.assertEqual(self.bal(self.uve_f1), before - 40000)
        with self.assertRaises(PermissionDenied):
            sh.desk_to_shop(self.m, shop=self.shop, amount=D(1))  # shop managers are not cashiers

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
        # a UVE transfer to a cashier shows on both sides
        c.post("/shops/do/to_cashier/", {"idem": str(uuid.uuid4()), "destination": self.uve_f1.pk, "amount": "5000"})
        tr = Transaction.objects.get(type=TxType.SHOP_TO_DESK)
        self.assertEqual(self.client_for("cashier.fako1").get(f"/transactions/{tr.pk}/").status_code, 200)
        self.assertEqual(c.get(f"/transactions/{tr.pk}/").status_code, 200)
        # the cashier's transfer to the shop page
        cf = self.client_for("cashier.fako1")
        self.assertEqual(cf.get("/transfer/to-shop/").status_code, 200)
        r = cf.post("/transfer/to-shop/", {"idem": str(uuid.uuid4()), "shop": self.shop.pk, "amount": "7 000"})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        self.assertTrue(Transaction.objects.filter(type=TxType.DESK_TO_SHOP, amount=7000).exists())
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
        kit = self.o["items"]["kit"]
        units = sh.receive_stock(self.o["shop_senior"], shop=self.shop, item=kit, serials="OEK-1\nOEK-2")
        form = c.get("/shops/do/sale/")
        self.assertContains(form, 'data-category="%s"' % kit.category_id)
        self.assertContains(form, "OEK-1")
        r = c.post("/shops/do/sale/", {"idem": str(uuid.uuid4()), "category": kit.category_id, "item": kit.pk,
                                       "units": [units[0].pk], "client_name": "Ngwa Peter",
                                       "client_address": "Molyko", "client_country": "Cameroon", "id_type": "PASSPORT",
                                       "id_number": "P123", "phone": "677445566", "whatsapp": ""})
        self.assertEqual(r.status_code, 302, r.content[:1500])
        sale = Transaction.objects.get(type=TxType.SHOP_SALE)
        rec = c.get(f"/transactions/{sale.pk}/")
        for txt in ("Ngwa Peter", "P123", "OEK-1", "Cameroon"):
            self.assertContains(rec, txt)
        # a mismatched category is refused
        r = c.post("/shops/do/sale/", {"idem": str(uuid.uuid4()), "category": self.o["items"]["phone"].category_id,
                                       "item": kit.pk, "units": [units[1].pk], "client_name": "X",
                                       "client_address": "Y", "client_country": "Z", "id_type": "CNI",
                                       "id_number": "1", "phone": "677445566"})
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


class ShopDutiesAndAdministration(OrgMixin, TestCase):
    """Segmented shops (staff duties) and the senior shop manager's administration of shops and staff."""

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
        self.assertEqual(Account.objects.filter(shop=new).count(), 3)
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
                                                   "staff-duties": ["OM", "AIRTIME", "SALES", "SERVICES", "AGENTS"]})
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
