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
                    "/reports/supplies/", "/agents/declarations/"):
            self.assertEqual(c.get(url).status_code, 200, url)
        # never the agents' / POS statements or what they owe
        for url in ("/reports/customers/", "/reports/supply_due/", "/reports/supply_payment/", "/reports/pos/",
                    f"/reports/statement/?preset=all&customer_sim={self.a1.business_sim}", f"/customers/{self.a1.pk}/"):
            self.assertEqual(c.get(url).status_code, 404, url)
        lst = c.get("/customers/")
        self.assertNotContains(lst, "owed to the company")
        self.assertNotContains(lst, ">Statement<")
        self.assertNotContains(r, "Owed to the company by agents")
        self.assertNotContains(c.get("/reports/"), "/reports/customers/")
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


class Requests4(OrgMixin, TestCase):
    """Update agent / POS info by sheet; market supervisor supplies with his own SIM; multi-select clear; app."""

    def setUp(self):
        self.make_org()
        self.fund_all()

    def login(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u.username, password=PW))
        return c

    def test_update_info_by_sheet_changes_only_filled_cells(self):
        from core.services import importer

        from .test_admin import xlsx_bytes

        p = self.p1
        old_name, old_om = p.name, p.om_account_name
        Counterparty.objects.filter(pk=p.pk).update(address="old street")
        head = importer.TEMPLATES["customers_update"][0]
        rows = [head, ["POS", "", "", "", "", p.business_sim, "", "677 99 98 88", "", "new street"],
                ["", "", "", "", "", self.a1.business_sim, "", "", "", ""],
                ["", "", "", "", "", "699999999", "Nobody", "", "", ""]]
        res, ok = importer.run_import(self.admin, "customers_update", importer.read_rows("u.xlsx", xlsx_bytes(rows)),
                                      commit=True)
        self.assertFalse(ok)  # an unknown SIM is an error: nobody is created
        self.assertFalse(Counterparty.objects.filter(business_sim="699999999").exists())
        res, ok = importer.run_import(self.admin, "customers_update",
                                      importer.read_rows("u.xlsx", xlsx_bytes(rows[:3])), commit=True)
        self.assertTrue(ok, res)
        p.refresh_from_db()
        self.assertEqual((p.name, p.om_account_name, p.whatsapp, p.address), (old_name, old_om, "677999888",
                                                                              "new street"))
        self.assertIn("unchanged", res[1][2])
        self.assertIn("customers_update", dict(__import__("core.forms", fromlist=["x"]).ImportForm.KINDS))

    def test_market_supervisor_with_his_sim_supplies_and_pays_back(self):
        from core.models import AgentPayback

        sub = self.p1.microzone.subzone
        own = Counterparty.objects.create(kind="AGENT", business_sim="655443322", name="Market Sup SIM",
                                          master_zone=sub.parent)
        mk = User.objects.create_user("mk", password=PW, role=User.Role.MARKET, subzone=sub, counterparty=own)
        mk.full_clean(exclude=["password"])
        c = self.login(mk)
        home = c.get("/")
        self.assertContains(home, "My agent account")
        self.assertContains(home, "/agent/supply/")
        r = c.post("/agent/supply/", {"client_kind": "POS", "pos_sim": self.p1.business_sim, "product": "UVE",
                                      "amount": "15000", "idem": str(uuid.uuid4())}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertIn(r.status_code, (200, 302))
        e = AgentClientEntry.objects.filter(agent=own).first()
        self.assertIsNotNone(e, r.content[:3000] if r.status_code == 200 else r)
        self.assertEqual(e.pos, self.p1)
        Counterparty.objects.filter(pk=self.p1.pk).update(uve_stock=D(5000))
        page = c.get("/agent/destocash/")
        self.assertContains(page, 'data-destock="CASH"')  # he records destocash too
        self.assertEqual(c.get("/agent/payback/").status_code, 200)
        self.assertEqual(c.get("/agent/debtors/").status_code, 200)
        # without his own agent account he only follows
        mk2 = User.objects.create_user("mk2", password=PW, role=User.Role.MARKET, subzone=sub)
        c2 = self.login(mk2)
        self.assertEqual(c2.get("/agent/supply/").status_code, 403)
        self.assertNotContains(c2.get("/agent/destocash/"), 'data-destock="CASH"')
        self.assertTrue(AgentPayback.objects.model)

    def test_clear_several_options_at_once(self):
        from core.services import admin as adm

        self.assertTrue(Counterparty.objects.filter(kind="POS").exists())
        c = self.login(self.admin)
        r = c.get("/administration/clear/")
        self.assertContains(r, 'type="checkbox" name="scope"')
        adm.clear_many(self.admin, scopes=["pos_list", "agent_list"], password=PW, confirm_text="DELETE")
        self.assertFalse(Counterparty.objects.filter(kind__in=["POS", "AGENT"]).exists())

    def test_installable_app(self):
        c = self.client_class()
        m = c.get("/manifest.webmanifest")
        self.assertEqual(m.status_code, 200)
        import json

        data = json.loads(m.content)
        self.assertEqual(data["display"], "standalone")
        self.assertTrue(any(i["sizes"] == "512x512" for i in data["icons"]))
        icon = c.get("/app-icon/512.png")
        self.assertEqual(icon["Content-Type"], "image/png")
        self.assertTrue(icon.content.startswith(b"\x89PNG"))
        self.assertTrue(c.get("/app-icon/192/maskable.png").content.startswith(b"\x89PNG"))
        sw = c.get("/sw.js")
        self.assertEqual(sw["Service-Worker-Allowed"], "/")
        self.assertIn(b"fetch", sw.content)
        self.assertEqual(c.get("/offline/").status_code, 200)
        self.assertContains(c.get("/login/"), 'rel="manifest"')

    def test_market_supervisor_starts_supplying_with_his_sim_in_the_app(self):
        sub = self.p1.microzone.subzone
        mk = User.objects.create_user("mk3", password=PW, role=User.Role.MARKET, subzone=sub)
        Counterparty.objects.filter(pk=self.p1.pk).update(er_stock=D(100), uve_stock=D(8000))
        c = self.login(mk)
        oos = c.get("/agent/oos/")
        self.assertContains(oos, "Start supplying")
        self.assertNotContains(oos, "/agent/supply/?pos=")
        # a POS SIM is refused
        c.post("/agent/my-sim/", {"sim": self.p1.business_sim, "next": "/agent/oos/"})
        mk.refresh_from_db()
        self.assertIsNone(mk.counterparty_id)
        r = c.post("/agent/my-sim/", {"sim": "655 12 34 56", "next": "/agent/oos/"})
        self.assertRedirects(r, "/agent/oos/", fetch_redirect_response=False)
        mk.refresh_from_db()
        self.assertEqual((mk.counterparty.kind, mk.counterparty.business_sim), ("AGENT", "655123456"))
        oos = c.get("/agent/oos/")
        self.assertNotContains(oos, "Start supplying")
        self.assertContains(oos, "/agent/supply/?pos=")  # supply the out-of-stock POS
        dest = c.get("/agent/destocash/")
        self.assertContains(dest, 'data-destock="CASH"')
        self.assertContains(dest, 'data-destock="FLOAT"')
        r = c.post("/agent/destocash/record/", {"pos": self.p1.pk, "how": "CASH", "amount": "5000",
                                                "idem": str(uuid.uuid4())})
        self.assertTrue(AgentClientEntry.objects.filter(agent=mk.counterparty, pos=self.p1).exists())


class Requests5(OrgMixin, TestCase):
    """Daily declaration (agents and market supervisors), records chips, phone-friendly lists."""

    def setUp(self):
        self.make_org()
        self.fund_all()

    def login(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u.username if hasattr(u, "username") else u, password=PW))
        return c

    def test_daily_declaration_and_opening_capital(self):
        from core.models import AgentDeclaration
        from core.services import agents as ag
        from core.services import operations as ops

        agent_user = User.objects.get(counterparty=self.a1)
        ops.record_supplies(self.c_f1, customer=self.a1, parts={"UVE": D(50000)}, idempotency_key=uuid.uuid4())
        c = self.login(agent_user)
        self.assertContains(c.get("/"), "/agent/declaration/")
        page = c.get("/agent/declaration/")
        self.assertContains(page, "Received from the office")
        self.assertContains(page, "Initial capital")
        r = c.post("/agent/declaration/", {"idem": str(uuid.uuid4()), "kind": "DAILY",
                                           "business_date": str(__import__("core.services.days", fromlist=["x"]).douala_today()),
                                           "recv_uve": "50 000", "paid_cash": "10 000"})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        d = AgentDeclaration.objects.get(kind="DAILY")
        self.assertEqual((d.recv_uve, d.paid_cash, d.agent), (D(50000), D(10000), self.a1))
        page = c.get("/agent/declaration/")
        self.assertContains(page, "Different from the office")  # 10 000 cash paid is not recorded by the office
        ag.declare(agent_user, kind="OPENING", recv_cash=D(500000))
        with self.assertRaises(Exception):
            ag.declare(agent_user, kind="OPENING", recv_cash=D(1))
        self.assertNotContains(c.get("/agent/declaration/"), 'value="OPENING"')
        # supervisors see the declarations
        m = self.login("manager")
        self.assertContains(m.get("/agents/declarations/"), self.a1.name)
        # a market supervisor with his own SIM declares too
        own = Counterparty.objects.create(kind="AGENT", business_sim="655778899", name="Sup SIM")
        mk = User.objects.create_user("mkd", password=PW, role=User.Role.MARKET, subzone=self.p1.microzone.subzone,
                                      counterparty=own)
        mc = self.login(mk)
        self.assertContains(mc.get("/"), "/agent/declaration/")
        self.assertEqual(mc.get("/agent/declaration/").status_code, 200)

    def test_records_chips(self):
        from core.services import operations as ops

        ops.record_entry(self.c_f1, source="OTHER", from_whom="Owner", product="CASH", amount=D(1000))
        c = self.login("cashier.fako1")
        r = c.get("/transactions/?kind=entries&preset=all")
        self.assertContains(r, "My records")
        self.assertContains(r, "Entry from outside")
        self.assertNotContains(c.get("/transactions/?kind=exchanges&preset=all"), "Entry from outside")
        self.assertContains(r, "data-collapse-phone")


    def test_market_supervisor_declares_his_own_account(self):
        from core.models import AgentDeclaration

        mk = User.objects.create_user("mkdecl", password=PW, role=User.Role.MARKET,
                                      subzone=self.p1.microzone.subzone)
        c = self.login(mk)
        self.assertContains(c.get("/"), "My daily declaration")
        page = c.get("/agent/declaration/")
        self.assertContains(page, "Start supplying")  # gives his SIM first
        c.post("/agent/my-sim/", {"sim": "655 99 88 77", "next": "/agent/declaration/"})
        mk.refresh_from_db()
        r = c.post("/agent/declaration/", {"idem": str(uuid.uuid4()), "kind": "DAILY",
                                           "business_date": str(__import__("core.services.days", fromlist=["x"]).douala_today()),
                                           "recv_cash": "20 000", "paid_float": "5 000"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(AgentDeclaration.objects.filter(agent=mk.counterparty, recv_cash=D(20000)).exists())
        # his own statement only
        self.assertEqual(c.get("/reports/statement/").status_code, 200)
        self.assertContains(c.get(f"/reports/statement/?customer_sim={self.a1.business_sim}"), mk.counterparty.name)
