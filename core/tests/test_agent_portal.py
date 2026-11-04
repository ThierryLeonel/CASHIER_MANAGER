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
        ag.record_supply(self.ua, client_kind="OTHER", client_name="Mama Rose", client_phone="677 11 22 33",
                         er=D(5000))
        ag.record_client_payment(self.ua, client_kind="POS", pos_sim=self.p1.business_sim, amount=D(15000))
        self.assertEqual(ag.pos_owed(self.a1, self.p1), D(25000))
        owing = {g["label"]: g["owed"] for g in ag.clients_owing(self.a1)}
        self.assertEqual(owing[self.p1.name], D(25000))
        self.assertEqual(owing[self.p3.name], D(10000))
        self.assertEqual(owing["Mama Rose"], D(5000))
        self.assertNotIn(self.shop.name, owing)  # paid in full
        stock = {lbl: left for lbl, _r, _o, left in ag.stock_estimate(self.a1)}
        self.assertEqual((stock["UVE"], stock["ER"]), (D(300000 - 75000), D(100000 - 25000)))
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
        x = ag.record_supply(self.ua, client_kind="OTHER", client_name="X", cash=D(1000), idempotency_key=k)
        self.assertEqual(ag.record_supply(self.ua, client_kind="OTHER", client_name="X", cash=D(1000),
                                          idempotency_key=k).pk, x.pk)
        ag.cancel_entry(self.ua, x, "typo")
        self.assertNotIn("X", {g["label"] for g in ag.clients_owing(self.a1)})
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
        r = c.post("/agent/supply/", {"idem": str(uuid.uuid4()), "client_kind": "POS",
                                      "pos_sim": self.p3.business_sim, "product": "UVE", "amount": "12 000",
                                      "paid": ""})
        self.assertEqual(r.status_code, 302, r.content[:3000])
        e = AgentClientEntry.objects.get()
        self.assertEqual((e.pos, e.uve, e.er), (self.p3, D(12000), D(0)))
        self.assertEqual(r["Location"], f"/agent/supply/{e.pk}/done/")
        done = c.get(r["Location"])
        self.assertContains(done, "#149*5*692000003*12000#")
        self.assertContains(done, 'href="tel:%23149*5*692000003*12000%23"')
        self.assertContains(c.get("/agent/supply/"), ">Supply</button>")
        r = c.post("/agent/supply/", {"idem": str(uuid.uuid4()), "client_kind": "POS",
                                      "pos_sim": self.p1.business_sim, "product": "ER", "amount": "5000"})
        self.assertContains(c.get(r["Location"]), "#189*2*692000001*5000#")
        r = c.post("/agent/supply/", {"idem": str(uuid.uuid4()), "client_kind": "POS",
                                      "pos_sim": self.p1.business_sim, "product": "CASH", "amount": "3000"})
        self.assertNotContains(c.get(r["Location"]), "tel:")
        AgentClientEntry.objects.exclude(pk=e.pk).delete()
        act = c.get("/agent/activity/")
        self.assertContains(act, self.p3.name)  # its name only
        self.assertNotContains(act, f"/agent/pos/{self.p3.pk}/")
        r = c.post("/agent/payback/", {"idem": str(uuid.uuid4()), "desk": self.c_f1.subzone.pk, "cash": "40 000"})
        self.assertEqual(r.status_code, 302, r.content[:3000])
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
        e = ag.record_supply(self.ua, client_kind="OTHER", client_name="Z", client_phone="+237 677 11 22 33",
                             er=D(2500))
        self.assertEqual(ag.ussd_code(e), "#189*2*677112233*2500#")
        e = ag.record_supply(self.ua, client_kind="OTHER", client_name="Z", uve=D(100))
        self.assertEqual(ag.ussd_code(e), "")  # no number

    def test_admin_delete_and_clear(self):
        from core.services import admin as adm

        p = ag.request_payback(self.ua, desk=self.c_f1.subzone, cash=D(10000))
        p = ag.confirm_payback(self.c_f1, p)
        adm.delete_transaction(self.admin, p.transaction, reason="wrong")
        p.refresh_from_db()
        self.assertEqual((p.status, p.transaction), (AgentPayback.Status.REJECTED, None))
        ag.record_supply(self.ua, client_kind="OTHER", client_name="Y", cash=D(1000))
        q = ag.confirm_payback(self.c_f1, ag.request_payback(self.ua, desk=self.c_f1.subzone, cash=D(5000)))
        self.assertIsNotNone(q.transaction)
        adm.clear_data(self.admin, scope="transactions", password=PW, confirm_text="DELETE")
        self.assertFalse(AgentPayback.objects.exists() or AgentClientEntry.objects.exists())
