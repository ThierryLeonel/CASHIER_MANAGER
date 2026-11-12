"""Market supervisor (read-only over shops, agents / POS), call buttons, destocash in cash / float, call database."""
import io
import uuid

from django.test import TestCase
from openpyxl import load_workbook

from core.models import AgentClientEntry, AgentExchange, Counterparty, MicroZone, User

from .helpers import D, OrgMixin
from .test_web import PW


class Market(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.mk = User.objects.create_user("market", password=PW, role=User.Role.MARKET,
                                           subzone=self.p1.microzone.subzone)

    def login(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u.username, password=PW))
        return c

    def test_market_supervisor_sees_but_records_nothing(self):
        c = self.login(self.mk)
        r = c.get("/")
        self.assertContains(r, "Market supervision")
        self.assertContains(r, "Download call database")
        self.assertNotContains(r, "Cash managers")
        for url in ("/customers/", "/transactions/", "/reports/", "/shops/", "/reports/pos_stock/",
                    "/reports/customers/", f"/reports/statement/?preset=all&customer_sim={self.p1.business_sim}"):
            self.assertEqual(c.get(url).status_code, 200, url)
        self.assertNotContains(c.get("/customers/"), "/customers/new/")
        for url in ("/customers/new/", f"/customers/{self.p1.pk}/edit/", "/exchanges/", "/closings/"):
            self.assertIn(c.get(url).status_code, (403, 302, 404), url)
        self.assertEqual(c.get("/reports/reconciliation/").status_code, 404)  # not a market report
        x = c.get("/customers/call-database/")
        self.assertEqual(x.status_code, 200)
        ws = load_workbook(io.BytesIO(x.content)).active
        head = [v.value for v in ws[1]]
        self.assertEqual(head[:3], ["TYPE", "ZONE", "SUB_ZONE"])
        sims = {str(r[5]) for r in ws.iter_rows(min_row=2, values_only=True)}
        self.assertIn(self.p1.business_sim, sims)
        self.assertIn(self.a1.business_sim, sims)

    def test_cards_have_call_and_whatsapp_icon(self):
        Counterparty.objects.filter(pk=self.p1.pk).update(whatsapp="677111222")
        r = self.login(self.admin).get("/customers/?kind=POS")
        self.assertContains(r, 'data-call')
        self.assertContains(r, 'data-personal="677111222"')
        self.assertContains(r, "STKB (business SIM)")
        self.assertNotContains(r, "</svg> WhatsApp</a>")

    def test_destocash_cash_and_float(self):
        p = self.p1
        Counterparty.objects.filter(pk=p.pk).update(uve_stock=D(50000))
        agent_user = User.objects.get(counterparty=p.microzone.agent)
        c = self.login(agent_user)
        page = c.get("/agent/destocash/")
        self.assertContains(page, 'data-destock="CASH"')
        self.assertContains(page, 'data-destock="FLOAT"')
        k = str(uuid.uuid4())
        r = c.post("/agent/destocash/record/", {"pos": p.pk, "how": "CASH", "amount": "20 000", "idem": k,
                                                "next": f"/agent/destocash/#pos-{p.pk}"})
        self.assertRedirects(r, f"/agent/destocash/#pos-{p.pk}", fetch_redirect_response=False)
        e = AgentClientEntry.objects.get(pos=p)
        self.assertEqual((e.cash, e.paid_uve, e.balance_effect), (D(20000), D(20000), D(0)))
        c.post("/agent/destocash/record/", {"pos": p.pk, "how": "CASH", "amount": "20000", "idem": k})  # twice
        self.assertEqual(AgentClientEntry.objects.filter(pos=p).count(), 1)
        c.post("/agent/destocash/record/", {"pos": p.pk, "how": "FLOAT", "amount": "10000", "idem": str(uuid.uuid4())})
        x = AgentExchange.objects.get()
        self.assertEqual((x.direction, x.amount, x.other_phone), ("SEND_FLOAT_UVE", D(10000), p.business_sim))
        p.refresh_from_db()
        self.assertEqual(p.uve_stock, D(20000))
        # another agent's POS is refused
        other = Counterparty.objects.filter(kind="POS").exclude(microzone__agent=p.microzone.agent).first()
        if other:
            c.post("/agent/destocash/record/", {"pos": other.pk, "how": "CASH", "amount": "1000",
                                                "idem": str(uuid.uuid4())})
            self.assertFalse(AgentClientEntry.objects.filter(pos=other).exists())

    def test_senior_manager_and_finance_see_agents_supervision(self):
        for username in ("manager", "finance", self.admin.username):
            u = User.objects.get(username=username)
            c = self.login(u)
            r = c.get("/supervision/agents/")
            self.assertEqual(r.status_code, 200, username)
            self.assertContains(r, "Commercial agents supervision")
            self.assertContains(r, 'href="/supervision/agents/"')  # the Agents workspace tab
            self.assertContains(r, "Download call database")
            home = c.get("/")
            self.assertContains(home, "Cashiers")  # cashiers and shops spaces stay
            self.assertContains(home, 'href="/shops/"')
            self.assertEqual(c.get("/shops/").status_code, 200)
        # a cashier has no agents workspace
        cashier = User.objects.filter(role=User.Role.CASHIER).first()
        self.assertEqual(self.login(cashier).get("/supervision/agents/").status_code, 403)
        # the market supervisor works in the agents and shops spaces only
        r = self.login(self.mk).get("/")
        self.assertContains(r, 'href="/supervision/agents/"')
        self.assertNotContains(r, ">Cashiers<")

    def test_market_supervisor_follows_his_subzone_with_oos_and_destocash(self):
        sub = self.p1.microzone.subzone
        other_mz = MicroZone.objects.exclude(subzone=sub).filter(subzone__isnull=False).first()
        far = Counterparty.objects.create(kind="POS", business_sim="699000111", name="Far POS", microzone=other_mz,
                                          er_stock=D(100), uve_stock=D(9000))
        Counterparty.objects.filter(pk=self.p1.pk).update(er_stock=D(500), uve_stock=D(7000))
        c = self.login(self.mk)
        for url in ("/agent/oos/", "/agent/destocash/"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertContains(r, self.p1.name)
            self.assertNotContains(r, "Far POS")
            self.assertContains(r, sub.name)
            self.assertNotContains(r, 'data-destock="CASH"')  # he follows, he does not record
            self.assertNotContains(r, "/agent/supply/?pos=")
        self.assertNotContains(c.get("/customers/?kind=POS"), "Far POS")
        ws = load_workbook(io.BytesIO(c.get("/customers/call-database/").content)).active
        sims = {str(r[5]) for r in ws.iter_rows(min_row=2, values_only=True)}
        self.assertIn(self.p1.business_sim, sims)
        self.assertNotIn(far.business_sim, sims)
        self.assertContains(c.get("/"), 'href="/agent/oos/"')
        # the senior manager sees every POS in OOS; the super admin downloads agents and POS from the import page
        m = self.login(User.objects.get(username="manager"))
        self.assertContains(m.get("/agent/oos/"), "Far POS")
        a = self.login(self.admin)
        self.assertContains(a.get("/administration/import/"), "Download agents & POS")
        self.assertContains(a.get("/administration/"), "Download agents & POS")
        x = a.get("/customers/call-database/?kind=POS")
        rows = list(load_workbook(io.BytesIO(x.content)).active.iter_rows(min_row=2, values_only=True))
        self.assertTrue(rows and all(r[0] == "POS" for r in rows))
        self.assertIn(far.business_sim, {str(r[5]) for r in rows})

    def test_market_supervisor_needs_a_zone(self):
        from django.core.exceptions import ValidationError

        u = User(username="mk2", role=User.Role.MARKET)
        with self.assertRaises(ValidationError):
            u.clean()
