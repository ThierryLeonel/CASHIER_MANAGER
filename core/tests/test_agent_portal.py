"""Commercial agent portal: supplies to clients, client payments, zone visibility, paybacks confirmed by cashiers."""
import uuid

from django.test import TestCase

from core.models import AgentClientEntry, AgentPayback, Transaction, TxType
from core.services import agents as ag
from core.services import operations as ops
from core.services.errors import BusinessError, PermissionDenied

from .helpers import D, OrgMixin
from .test_web import PW


class AgentPortal(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]
        # the office supplies agent Ndive (Fako zone)
        ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(300000), source_account=self.uve_f1)
        ops.record_supply(self.c_f1, product="ER", customer=self.a1, amount=D(100000), source_account=self.er_f1)
        self.ua = self.agent_user
        from core.models import Counterparty

        # a POS of the Meme zone: outside agent Ndive's zone (Fako)
        self.p3 = Counterparty.objects.create(kind="POS", business_sim="692000003", name="POS Kumba station",
                                              microzone=self.microzones["kumba"])

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_zone_and_supplies(self):
        self.assertEqual(ag.zone_of(self.a1), self.zones["fako"])
        self.assertIn(self.p1, ag.zone_pos(self.a1))
        self.assertIn(self.p2, ag.zone_pos(self.a1))  # Bonduma, Fako 2
        self.assertNotIn(self.p3, ag.zone_pos(self.a1))  # Kumba is in the Meme zone
        # supply a POS of the zone, part paid
        e = ag.record_supply(self.ua, client_kind="POS", pos_sim=self.p1.business_sim, uve="50 000", er="20000",
                             paid="30000")
        self.assertEqual((e.total, e.balance_effect), (D(70000), D(40000)))
        # supply a POS outside the zone is allowed
        ag.record_supply(self.ua, client_kind="POS", pos_sim="692 00 00 03", cash=D(10000))
        # a shop and another client
        ag.record_supply(self.ua, client_kind="SHOP", shop=self.shop, uve=D(25000), paid=D(25000))
        ag.record_supply(self.ua, client_kind="OTHER", client_phone="677 11 22 33", er=D(5000))
        ag.record_client_payment(self.ua, client_kind="POS", pos_sim=self.p1.business_sim, amount=D(15000))
        self.assertEqual(ag.pos_owed(self.a1, self.p1), D(25000))
        owing = {g["label"]: g["owed"] for g in ag.clients_owing(self.a1)}
        self.assertEqual(owing[self.p1.name], D(25000))
        self.assertEqual(owing[self.p3.name], D(10000))
        self.assertEqual(owing["677112233"], D(5000))  # another client: known by the number only
        self.assertNotIn(self.shop.name, owing)  # paid in full
        stock = dict(ag.stock_estimate(self.a1))
        self.assertEqual((stock["UVE"], stock["ER"]), (D(300000 - 75000), D(100000 - 25000)))
        # cash in hand: cash supplied out (10 000) against what clients paid (30 000 + 25 000 + 15 000)
        self.assertEqual(stock["Cash"], D(70000 - 10000))
        # errors
        with self.assertRaises(BusinessError):
            ag.record_supply(self.ua, client_kind="POS", pos_sim="699999999", uve=D(1))
        with self.assertRaises(BusinessError):
            ag.record_supply(self.ua, client_kind="POS", pos_sim=self.p1.business_sim)
        with self.assertRaises(BusinessError):
            ag.record_supply(self.ua, client_kind="OTHER", uve=D(1))
        with self.assertRaises(BusinessError):
            ag.record_supply(self.ua, client_kind="POS", pos_sim=self.p1.business_sim, uve=D(10), paid=D(20))
        with self.assertRaises(PermissionDenied):
            ag.record_supply(self.c_f1, client_kind="POS", pos_sim=self.p1.business_sim, uve=D(10))
        # idempotent, and cancel today's mistake
        k = uuid.uuid4()
        x = ag.record_supply(self.ua, client_kind="OTHER", client_phone="655000001", cash=D(1000), idempotency_key=k)
        self.assertEqual(ag.record_supply(self.ua, client_kind="OTHER", client_phone="655000001", cash=D(1000),
                                          idempotency_key=k).pk, x.pk)
        ag.cancel_entry(self.ua, x, "typo")
        self.assertNotIn("655000001", {g["label"] for g in ag.clients_owing(self.a1)})
        with self.assertRaises(PermissionDenied):
            ag.cancel_entry(self.agent_user2, e, "not mine")

    def test_payback_confirm_and_reject(self):
        owed = self.cbal(self.a1)
        desks = list(ag.desks_for(self.a1))
        self.assertIn(self.c_f1.subzone, desks)
        self.assertNotIn(self.c_me.subzone, desks)
        p = ag.request_payback(self.ua, desk=self.c_f1.subzone, cash=D(100000), uve=D(50000), reference="OM123")
        self.assertEqual(self.cbal(self.a1), owed)  # nothing until the cashier confirms
        with self.assertRaises(PermissionDenied):
            ag.confirm_payback(self.c_f2, p)  # another desk
        with self.assertRaises(PermissionDenied):
            ag.confirm_payback(self.ua, p)
        drawer_before = self.bal(self.d_f1)
        p = ag.confirm_payback(self.c_f1, p)
        self.assertEqual(p.status, AgentPayback.Status.CONFIRMED)
        self.assertEqual(self.cbal(self.a1), owed - 150000)
        self.assertEqual(self.bal(self.d_f1), drawer_before + 100000)
        self.assertEqual(Transaction.objects.filter(type=TxType.COLLECTION, counterparty=self.a1).count(), 2)
        self.assertIn("AP-", p.transaction.description)
        with self.assertRaises(PermissionDenied):
            ag.confirm_payback(self.c_f1, p)  # only once
        q = ag.request_payback(self.ua, desk=self.c_f1.subzone, mtn=D(5000))
        with self.assertRaises(BusinessError):
            ag.reject_payback(self.c_f1, q, "")
        ag.reject_payback(self.c_f1, q, "Nothing received")
        self.assertEqual(self.cbal(self.a1), owed - 150000)
        with self.assertRaises(BusinessError):  # a desk outside the zone
            ag.request_payback(self.ua, desk=self.c_me.subzone, cash=D(1))
        with self.assertRaises(BusinessError):
            ag.request_payback(self.ua, desk=self.c_f1.subzone)

    def test_pages(self):
        c = self.client_for("agent.ndive")
        for url in ("/", "/agent/pos/", "/agent/supply/", "/agent/collect/", "/agent/payback/",
                    "/agent/activity/", "/agent/activity/?tab=office", "/agent/activity/?tab=paybacks"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
        self.assertContains(c.get("/agent/pos/"), self.p1.name)
        self.assertNotContains(c.get("/agent/pos/"), self.p3.name)
        self.assertEqual(c.get(f"/agent/pos/{self.p1.pk}/").status_code, 200)
        self.assertEqual(c.get(f"/agent/pos/{self.p3.pk}/").status_code, 404)  # other zone: no information
        F = {"HTTP_X_REQUESTED_WITH": "fetch"}
        r = c.post("/agent/supply/", {"idem": str(uuid.uuid4()), "client_kind": "POS",
                                      "pos_sim": self.p3.business_sim, "product": "UVE", "amount": "12 000",
                                      "paid": ""}, **F)
        self.assertEqual(r.status_code, 200, r.content[:3000])
        d = r.json()  # recorded at once; the page clears itself and dials the code
        self.assertEqual((d["ok"], d["code"]), (True, "#149*5*692000003*12000#"))
        e = AgentClientEntry.objects.get()
        self.assertEqual((e.pos, e.uve, e.er), (self.p3, D(12000), D(0)))
        page = c.get("/agent/supply/")
        self.assertContains(page, 'data-dial="Supply"')
        self.assertContains(page, 'data-plain="Record"')
        self.assertNotContains(page, 'name="client_name"')
        d = c.post("/agent/supply/", {"idem": str(uuid.uuid4()), "client_kind": "POS",
                                      "pos_sim": self.p1.business_sim, "product": "ER", "amount": "5000"}, **F).json()
        self.assertEqual(d["code"], "#189*2*692000001*5000#")
        d = c.post("/agent/supply/", {"idem": str(uuid.uuid4()), "client_kind": "SHOP", "shop": self.shop.pk,
                                      "product": "UVE", "amount": "7000"}, **F).json()
        self.assertEqual(d["code"], "#149*5*690000099*7000#")  # the shop's own number
        d = c.post("/agent/supply/", {"idem": str(uuid.uuid4()), "client_kind": "POS",
                                      "pos_sim": self.p1.business_sim, "product": "CASH", "amount": "3000"}, **F).json()
        self.assertEqual((d["ok"], d["code"]), (True, ""))  # cash: recorded, nothing to dial
        r = c.post("/agent/supply/", {"idem": str(uuid.uuid4()), "client_kind": "POS", "pos_sim": "699999999",
                                      "product": "UVE", "amount": "1000"}, **F)
        self.assertEqual(r.status_code, 400)
        self.assertIn("pos_sim", r.json()["errors"])
        # without the script: a normal post comes back to the form
        r = c.post("/agent/supply/", {"idem": str(uuid.uuid4()), "client_kind": "OTHER", "client_phone": "677 00 00 01",
                                      "product": "UVE", "amount": "2000"})
        self.assertEqual(r.status_code, 302)
        AgentClientEntry.objects.exclude(pk=e.pk).delete()
        act = c.get("/agent/activity/")
        self.assertContains(act, self.p3.name)  # its name only
        self.assertNotContains(act, f"/agent/pos/{self.p3.pk}/")
        d = c.post("/agent/payback/", {"idem": str(uuid.uuid4()), "to": "DESK", "desk": self.c_f1.subzone.pk,
                                       "method": "CASH", "amount": "40 000"}, **F).json()
        self.assertEqual((d["ok"], d["code"]), (True, ""))
        p = AgentPayback.objects.get()
        # the cashier of the desk sees it on the home page and confirms
        cf = self.client_for("cashier.fako1")
        self.assertContains(cf.get("/"), "Agent paybacks to confirm")
        self.assertContains(cf.get("/paybacks/"), p.code)
        self.assertNotContains(self.client_for("cashier.fako2").get("/"), "Agent paybacks to confirm")
        r = cf.post(f"/paybacks/{p.pk}/decide/", {"action": "confirm"})
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.status, "CONFIRMED")
        self.assertEqual(c.get(f"/transactions/{p.transaction.pk}/").status_code, 200)  # the agent sees his receipt
        self.assertEqual(self.client_for("manager").get("/paybacks/").status_code, 200)
        for u in ("cashier.fako1", "manager"):
            self.assertEqual(self.client_for(u).get("/agent/supply/").status_code, 403)
        self.assertEqual(c.get("/paybacks/").status_code, 403)

    def test_dial_codes(self):
        e = ag.record_supply(self.ua, client_kind="OTHER", client_phone="+237 677 11 22 33", er=D(2500))
        self.assertEqual(ag.ussd_code(e), "#189*2*677112233*2500#")
        with self.assertRaises(BusinessError):  # another client needs his number
            ag.record_supply(self.ua, client_kind="OTHER", uve=D(100))
        # a UVE payback dials the same code to the master SIM of the zone, or to the shop SIM
        p = ag.request_payback(self.ua, desk=self.c_f1.subzone, uve=D(20000))
        self.assertEqual(ag.payback_code(p), f"#149*5*{ag.desk_master_sim(self.c_f1.subzone)}*20000#")
        self.assertTrue(ag.desk_master_sim(self.c_f1.subzone))
        p = ag.request_payback(self.ua, shop=self.shop, uve=D(8000))
        self.assertEqual(ag.payback_code(p), "#149*5*690000099*8000#")
        self.assertEqual(ag.payback_code(ag.request_payback(self.ua, desk=self.c_f1.subzone, cash=D(1))), "")

    def test_transfer_between_agents(self):
        t = ag.record_supply(self.ua, client_kind="AGENT", agent_sim=self.a2.business_sim, uve=D(40000), paid=D(5))
        self.assertEqual((t.to_agent, t.paid, t.reference[:3]), (self.a2, D(0), "AT-"))
        self.assertEqual(ag.ussd_code(t), f"#149*5*{self.a2.business_sim}*40000#")
        t2 = ag.record_supply(self.ua, client_kind="AGENT", agent_sim=self.a2.business_sim, er=D(10000))
        self.assertEqual(ag.ussd_code(t2), f"#189*2*{self.a2.business_sim}*10000#")
        self.assertEqual(dict(ag.stock_estimate(self.a1))["UVE"], D(260000))
        self.assertEqual(dict(ag.stock_estimate(self.a2))["UVE"], D(40000))
        self.assertEqual(ag.clients_owing(self.a1), [])  # a transfer is not a debt of a client
        with self.assertRaises(BusinessError):
            ag.record_supply(self.ua, client_kind="AGENT", agent_sim=self.a1.business_sim, uve=D(1))
        c2 = self.client_for("agent.ekane")
        self.assertContains(c2.get("/agent/activity/?tab=agents"), t.reference)
        self.assertContains(self.client_for("agent.ndive").get("/agent/transfer/"), self.a2.name)

    def test_payback_at_a_shop(self):
        from core.services import shop_shifts as ss

        owed = self.cbal(self.a1)
        with self.assertRaises(BusinessError):
            ag.request_payback(self.ua, shop=self.shop, mtn=D(1000))
        p = ag.request_payback(self.ua, shop=self.shop, uve=D(30000), reference="MP1")
        with self.assertRaises(PermissionDenied):
            ag.confirm_payback(self.c_f1, p)  # the shop confirms, not a cashier
        mgr = self.o["shop_mgr"]
        with self.assertRaises(PermissionDenied):
            ag.confirm_payback(mgr, p)  # shift not open
        ss.open_shift(mgr, cash=D(0), uve=D(0), er=D(0))
        uve_before = self.bal(self.shop.account("UVE"))
        p = ag.confirm_payback(mgr, p)
        self.assertEqual(self.cbal(self.a1), owed - 30000)
        self.assertEqual(self.bal(self.shop.account("UVE")), uve_before + 30000)
        self.assertEqual(p.transaction.type, TxType.SHOP_AGENT_PAYBACK)
        ca = self.client_for("agent.ndive")
        self.assertEqual(ca.get(f"/transactions/{p.transaction.pk}/").status_code, 200)
        cs = self.client_for("shop.molyko")
        self.assertEqual(cs.get(f"/transactions/{p.transaction.pk}/").status_code, 200)
        self.assertEqual(cs.get("/paybacks/").status_code, 200)
        q = ag.request_payback(self.ua, shop=self.shop, cash=D(2000))
        self.assertContains(cs.get(f"/shops/{self.shop.pk}/"), q.code)

    def test_daily_reconciliation_and_office(self):
        from core.services.days import douala_today

        ag.record_supply(self.ua, client_kind="POS", pos_sim=self.p1.business_sim, uve=D(50000), paid=D(20000))
        ag.record_supply(self.ua, client_kind="AGENT", agent_sim=self.a2.business_sim, uve=D(10000))
        ag.request_payback(self.ua, desk=self.c_f1.subzone, uve=D(100000))
        day = douala_today()
        rec = ag.daily_reconciliation(self.a1, day)
        uve = next(r for r in rec["stock"] if r["code"] == "UVE")
        self.assertEqual((uve["office"], uve["to_clients"], uve["to_agents"], uve["paid_back"], uve["closing"]),
                         (D(300000), D(50000), D(10000), D(100000), D(140000)))
        cash = next(r for r in rec["stock"] if r["code"] == "CASH")
        self.assertEqual(cash["closing"], D(20000))
        self.assertEqual((rec["company"]["taken"], rec["company"]["closing"]), (D(400000), D(400000)))
        self.assertEqual((rec["clients"]["supplied"], rec["clients"]["closing"]), (D(50000), D(30000)))
        c = self.client_for("agent.ndive")
        for url in ("/agent/daily/", f"/agent/daily/?date={day.isoformat()}", "/agent/daily/?date=2020-01-01",
                    "/agent/office/", "/agent/transfer/", "/agent/activity/?tab=agents"):
            self.assertEqual(c.get(url).status_code, 200, url)
        office = c.get("/agent/office/")
        self.assertContains(office, "300 000")
        self.assertContains(office, "400 000")

    def test_admin_delete_and_clear(self):
        from core.services import admin as adm

        p = ag.request_payback(self.ua, desk=self.c_f1.subzone, cash=D(10000))
        p = ag.confirm_payback(self.c_f1, p)
        adm.delete_transaction(self.admin, p.transaction, reason="wrong")
        p.refresh_from_db()
        self.assertEqual((p.status, p.transaction), (AgentPayback.Status.REJECTED, None))
        ag.record_supply(self.ua, client_kind="OTHER", client_phone="655000002", cash=D(1000))
        q = ag.confirm_payback(self.c_f1, ag.request_payback(self.ua, desk=self.c_f1.subzone, cash=D(5000)))
        self.assertIsNotNone(q.transaction)
        adm.clear_data(self.admin, scope="transactions", password=PW, confirm_text="DELETE")
        self.assertFalse(AgentPayback.objects.exists() or AgentClientEntry.objects.exists())

    def test_stock_follows_how_the_client_paid(self):
        # cash supply paid back in UVE: cash goes down, UVE goes up by what was paid
        ag.record_supply(self.ua, client_kind="POS", pos_sim=self.p1.business_sim, cash=D(50000), paid_uve=D(30000))
        # UVE supply paid in cash and float: UVE down, cash and float up
        ag.record_supply(self.ua, client_kind="POS", pos_sim=self.p1.business_sim, uve=D(40000), paid_cash=D(10000),
                         paid_mtn=D(20000))
        # ER supply paid in float
        ag.record_supply(self.ua, client_kind="SHOP", shop=self.shop, er=D(10000), paid_mtn=D(10000))
        # later payment in several ways
        e = ag.record_client_payment(self.ua, client_kind="POS", pos_sim=self.p1.business_sim, cash=D(5000),
                                     uve=D(2000), mtn=D(3000))
        self.assertEqual((e.paid, e.paid_cash, e.paid_uve, e.paid_mtn), (D(10000), D(5000), D(2000), D(3000)))
        st = dict(ag.stock_estimate(self.a1))
        self.assertEqual(st["UVE"], D(300000 + 30000 - 40000 + 2000))
        self.assertEqual(st["ER"], D(100000 - 10000))
        self.assertEqual(st["Cash"], D(-50000 + 10000 + 5000))
        self.assertEqual(st["Float"], D(20000 + 10000 + 3000))
        self.assertEqual(ag.pos_owed(self.a1, self.p1), D(90000 - 30000 - 30000 - 10000))
        with self.assertRaises(BusinessError):  # paid more than supplied
            ag.record_supply(self.ua, client_kind="POS", pos_sim=self.p1.business_sim, uve=D(100),
                             paid_cash=D(60), paid_mtn=D(60))
        # through the page (fetch), with the boxes the form shows
        c = self.client_for("agent.ndive")
        d = c.post("/agent/supply/", {"idem": str(uuid.uuid4()), "client_kind": "POS", "pos_sim": self.p1.business_sim,
                                      "product": "CASH", "amount": "8000", "paid_uve": "8000"},
                   HTTP_X_REQUESTED_WITH="fetch").json()
        self.assertTrue(d["ok"])
        d = c.post("/agent/collect/", {"idem": str(uuid.uuid4()), "client_kind": "POS", "pos_sim": self.p1.business_sim,
                                       "cash": "1000", "mtn": "500"}, HTTP_X_REQUESTED_WITH="fetch").json()
        self.assertTrue(d["ok"], d)
        self.assertEqual(AgentClientEntry.objects.order_by("-pk").first().paid_mtn, D(500))

    def test_float_exchange(self):
        from core.models import AgentExchange

        ag.record_exchange(self.ua, direction="SEND_FLOAT_UVE", amount=D(10000))
        ag.record_exchange(self.ua, direction="SEND_FLOAT_CASH", amount=D(4000))
        x = ag.record_exchange(self.ua, direction="RECEIVE_FLOAT_UVE", amount=D(25000), other_phone="677 12 34 56")
        self.assertEqual(ag.exchange_code(x), "#149*5*677123456*25000#")
        with self.assertRaises(BusinessError):  # sending UVE needs the number
            ag.record_exchange(self.ua, direction="RECEIVE_FLOAT_UVE", amount=D(1))
        st = dict(ag.stock_estimate(self.a1))
        self.assertEqual((st["UVE"], st["Cash"], st["Float"]), (D(300000 + 10000 - 25000), D(4000), D(25000 - 14000)))
        ag.cancel_exchange(self.ua, AgentExchange.objects.get(direction="SEND_FLOAT_CASH"), "mistake")
        self.assertEqual(dict(ag.stock_estimate(self.a1))["Cash"], D(0))
        c = self.client_for("agent.ndive")
        self.assertEqual(c.get("/agent/exchange/").status_code, 200)
        d = c.post("/agent/exchange/", {"idem": str(uuid.uuid4()), "direction": "RECEIVE_FLOAT_UVE", "amount": "5000",
                                        "other_phone": "677123456"}, HTTP_X_REQUESTED_WITH="fetch").json()
        self.assertEqual(d["code"], "#149*5*677123456*5000#")
        rec = ag.daily_reconciliation(self.a1, AgentExchange.objects.first().business_date)
        flt = next(r for r in rec["stock"] if r["code"] == "MTN")
        self.assertEqual(flt["exchanged"], D(25000 + 5000 - 10000))

    def test_debtors(self):
        ag.record_supply(self.ua, client_kind="POS", pos_sim=self.p1.business_sim, uve=D(50000), paid_cash=D(10000))
        ag.record_supply(self.ua, client_kind="OTHER", client_phone="677 11 22 33", er=D(5000))
        ag.record_supply(self.ua, client_kind="SHOP", shop=self.shop, uve=D(3000), paid_cash=D(3000))  # paid
        c = self.client_for("agent.ndive")
        r = c.get("/agent/debtors/")
        self.assertContains(r, self.p1.name)
        self.assertContains(r, "40 000")
        self.assertContains(r, f"/agent/collect/?pos={self.p1.business_sim}")
        self.assertContains(r, "/agent/collect/?phone=677112233")
        self.assertContains(r, "sms:+237692000001?body=")
        self.assertNotContains(r, self.shop.name)
        self.assertContains(c.get(f"/agent/collect/?pos={self.p1.business_sim}"), f'value="{self.p1.business_sim}"')

    def test_clear_agent_and_user_records(self):
        from core.models import AgentExchange
        from core.services import admin as adm

        ag.record_supply(self.ua, client_kind="POS", pos_sim=self.p1.business_sim, uve=D(1000))
        ag.record_exchange(self.ua, direction="SEND_FLOAT_UVE", amount=D(10))
        ag.record_supply(self.agent_user2, client_kind="POS", pos_sim=self.p1.business_sim, uve=D(2000))
        p = ag.confirm_payback(self.c_f1, ag.request_payback(self.ua, desk=self.c_f1.subzone, cash=D(5000)))
        owed = self.cbal(self.a1)
        adm.clear_data(self.admin, scope="agents", password=PW, confirm_text="DELETE", agent=self.a1)
        self.assertFalse(AgentClientEntry.objects.filter(agent=self.a1).exists())
        self.assertFalse(AgentExchange.objects.exists() or AgentPayback.objects.exists())
        self.assertTrue(AgentClientEntry.objects.filter(agent=self.a2).exists())  # the other agent's records stay
        self.assertTrue(Transaction.objects.filter(pk=p.transaction_id).exists())  # the company payment stays
        self.assertEqual(self.cbal(self.a1), owed)
        adm.clear_data(self.admin, scope="agents", password=PW, confirm_text="DELETE")
        self.assertFalse(AgentClientEntry.objects.exists())
        # one user's records: cashier Fako 2's transactions only
        t2 = ops.record_supply(self.c_f2, product="UVE", customer=self.a2, amount=D(7000), source_account=self.uve_f2)
        n_f1 = Transaction.objects.filter(entered_by=self.c_f1).count()
        with self.assertRaises(BusinessError):
            adm.clear_data(self.admin, scope="user", password=PW, confirm_text="DELETE")
        adm.clear_data(self.admin, scope="user", password=PW, confirm_text="DELETE", target_user=self.c_f2)
        self.assertFalse(Transaction.objects.filter(pk=t2.pk).exists())
        self.assertEqual(Transaction.objects.filter(entered_by=self.c_f1).count(), n_f1)
        page = self.client_for("admin").get("/administration/clear/")
        self.assertContains(page, 'name="target_user"')
        self.assertContains(page, 'name="agent"')


class PosStock(OrgMixin, TestCase):
    """Stock imported from the Orange file: agents see their POS levels, out of stock when ER < threshold."""

    def setUp(self):
        self.make_org()

    def _import(self, rows, commit=True):
        from core.services import importer

        head = importer.TEMPLATES["stock"][0]
        return importer.run_import(self.admin, "stock", [dict(zip([importer._key(h) for h in head], r)) for r in rows],
                                   commit=commit)

    def test_import_threshold_views_and_report(self):
        from core.models import StockSnapshot
        from core.reports.defs import pos_stock
        from core.reports.engine import Filters

        p1, p2 = self.p1, self.p2
        results, ok = self._import([["AGENT", self.a1.business_sim, "Molyko", 10000, 5000, 5000],
                                    ["POS", p1.business_sim, "Molyko", 1500, 2000, 10000],
                                    ["POS", p2.business_sim, "", 6000, 2000, 100]])
        self.assertTrue(ok, results)
        self.assertIn("out of stock", results[1][2])
        p1.refresh_from_db()
        self.assertEqual((p1.er_stock, p1.uve_stock, p1.commission_balance), (D(1500), D(2000), D(10000)))
        self.assertTrue(p1.out_of_stock(D(2000)))
        self.assertEqual(StockSnapshot.objects.count(), 3)
        # wrong type, unknown SIM -> nothing imported
        results, ok = self._import([["AGENT", p1.business_sim, "", 1, 1, 1], ["POS", "699999999", "", 1, 1, 1]])
        self.assertFalse(ok)
        self.assertEqual(StockSnapshot.objects.count(), 3)
        # the agent's portal
        c = self.client_class()
        c.login(username=self.agent_user.username, password=PW)
        home = c.get("/")
        mine_out = 1 if p1.microzone and p1.microzone.agent_id == self.a1.pk else 0
        if mine_out:
            self.assertContains(home, "My POS out of stock")
        self.assertContains(home, "My Orange stock")
        r = c.get("/agent/pos/?out=1")
        self.assertContains(r, p1.name)
        self.assertNotContains(r, p2.name)
        self.assertContains(c.get(f"/agent/pos/{p1.pk}/"), "out of stock")
        # report
        rep = pos_stock(Filters(None, None, user=self.manager))
        self.assertEqual(rep.summary[0][1], "1")
        self.assertTrue(rep.tables[0].rows[0].get("_head"))
        self.assertEqual(rep.tables[0].totals()["er"], D(17500))
