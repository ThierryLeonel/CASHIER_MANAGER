"""Float in the cashiers' opening grid, float accounts shared by all cashiers, optional cash-in name, shops' UVE with
POS, and the cashiers' daily reports."""
import uuid

from django.test import TestCase

from core.models import Account, CompanySim, Transaction, TxType
from core.services import operations as ops
from core.services import shop_shifts as ss
from core.services import shops as sh
from core.services.days import douala_today
from core.services.errors import BusinessError

from .helpers import D, OrgMixin
from .test_web import PW

K = Account.Kind


class FloatPosReports(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def _shared_float(self):
        sim = CompanySim.objects.create(network="MTN", msisdn="680000777", label="Company float",
                                        master_zone=self.zones["fako"], subzone=self.zones["fako1"])
        acct = Account.objects.get(sim=sim, kind=K.MTN)
        acct.shared = True
        acct.save(update_fields=["shared"])
        return acct

    def test_opening_grid_has_float(self):
        r = self.client_for("admin").get("/setup/opening-balances/")
        self.assertContains(r, Account.Kind.MTN.label)
        self.assertNotContains(self.client_for("admin").get("/shops/opening-balances/"), f"<th class=\"money\">"
                                                                                         f"{Account.Kind.MTN.label}")

    def test_shared_float_used_by_every_cashier(self):
        shared = self._shared_float()
        # Fako 2 (another subzone) and Meme (another master zone) can receive into it
        for cashier in (self.c_f2, self.c_me):
            ops.record_payment(cashier, payer=self.a1, parts={"MTN": D(10000)}, accounts={"MTN": shared},
                               idempotency_key=uuid.uuid4())
        self.assertEqual(self.bal(shared), D(20000))
        # the cashier's own float account stays the default when he has one
        own = Account.objects.filter(kind=K.MTN, shared=False, zone=self.c_f2.subzone).first()
        if own is not None:
            t = ops.record_payment(self.c_f2, payer=self.a1, parts={"MTN": D(500)}, idempotency_key=uuid.uuid4())[0]
            self.assertEqual(t.dest_account, own)
        # the payment page offers the choice when there are two float accounts
        page = self.client_for("cashier.fako2").get("/collection/new/")
        self.assertContains(page, "Company float" if own is None else 'name="mtn_account"')

    def test_cash_in_name_optional(self):
        mgr = self.o["shop_mgr"]
        ss.open_shift(mgr, cash=D(0), uve=D(0), er=D(0))
        from core.services import capital as cap

        cap.inject(self.finance, account=self.shop.account(K.UVE), amount=D(10000), source="x", purpose="y")
        r = self.client_for("shop.molyko").post("/shops/do/cashin/", {"idem": str(uuid.uuid4()), "amount": "1 000",
                                                                      "account_name": "", "client_phone": ""})
        self.assertEqual(r.status_code, 302, r.content[:2000])  # no account name needed
        self.assertFalse(Transaction.objects.get(type=TxType.SHOP_CASHIN).account_name)

    def test_shop_uve_with_pos(self):
        mgr = self.o["shop_mgr"]
        ss.open_shift(mgr, cash=D(0), uve=D(0), er=D(0))
        from core.services import capital as cap

        cap.inject(self.finance, account=self.shop.account(K.UVE), amount=D(100000), source="x", purpose="y")
        cap.inject(self.finance, account=self.shop.account(K.CASH_DRAWER), amount=D(50000), source="x", purpose="y")
        pos_before = self.cbal(self.p1)
        sh.uve_to_pos(mgr, pos=self.p1, amount=D(30000), pay="CASH")
        sh.uve_to_pos(mgr, pos=self.p1, amount=D(20000), pay="CREDIT")
        self.assertEqual(self.bal(self.shop.account(K.UVE)), D(50000))
        self.assertEqual(self.bal(self.shop.account(K.CASH_DRAWER)), D(80000))
        self.assertEqual(self.cbal(self.p1), pos_before + 20000)  # the POS owes the credit
        sh.uve_from_pos(mgr, pos=self.p1, amount=D(5000), pay="CASH")
        sh.uve_from_pos(mgr, pos=self.p1, amount=D(20000), pay="CREDIT")
        self.assertEqual(self.bal(self.shop.account(K.UVE)), D(75000))
        self.assertEqual(self.bal(self.shop.account(K.CASH_DRAWER)), D(75000))
        self.assertEqual(self.cbal(self.p1), pos_before)
        # a commercial agent too: on credit it goes on his account with the company
        a_before = self.cbal(self.a1)
        sh.uve_to_pos(mgr, pos=self.a1, amount=D(4000), pay="CREDIT")
        sh.uve_from_pos(mgr, pos=self.a1, amount=D(1000), pay="CASH")
        self.assertEqual(self.cbal(self.a1), a_before + 4000)
        self.p2.active = False
        self.p2.save()
        with self.assertRaises(BusinessError):
            sh.uve_to_pos(mgr, pos=self.p2, amount=D(1))  # inactive
        # a staff member with only the Orange Money duty may do it too
        om = self.o["shop_om"]
        ss.open_shift(om)
        sh.uve_from_pos(om, pos=self.p1, amount=D(500), pay="CASH")
        c = self.client_for("shop.molyko")
        for op in ("pos_uve_out", "pos_uve_in"):
            self.assertEqual(c.get(f"/shops/do/{op}/").status_code, 200)
        r = c.post("/shops/do/pos_uve_out/", {"idem": str(uuid.uuid4()), "pos": self.p1.business_sim,
                                              "amount": "1 000", "pay": "CASH"})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        self.assertEqual(Transaction.objects.filter(type=TxType.SHOP_POS_UVE_OUT).count(), 4)
        self.assertEqual(self.client_for("shop.molyko.om").get("/shops/do/pos_uve_in/").status_code, 200)

    def test_daily_reports_for_cashiers(self):
        ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(70000), source_account=self.uve_f1)
        ops.record_supply(self.c_f1, product="CASH", customer=self.p1, amount=D(20000), source_account=self.d_f1)
        ops.record_payment(self.c_f1, payer=self.a1, parts={"CASH": D(30000), "UVE": D(10000)},
                           idempotency_key=uuid.uuid4())
        c = self.client_for("cashier.fako1")
        today = douala_today().isoformat()
        r = c.get(f"/reports/supply_payment/?preset=day&date_from={today}")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "90 000")  # supplied
        self.assertContains(r, "40 000")  # paid
        self.assertContains(r, "50 000")  # supplied - paid
        r = c.get("/reports/daily_reconciliation/?preset=week")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Counted at closing")
        for fmt_ in ("pdf", "xlsx"):
            self.assertEqual(c.get(f"/reports/supply_payment/?preset=week&format={fmt_}").status_code, 200)
            self.assertEqual(c.get(f"/reports/daily_reconciliation/?preset=week&format={fmt_}").status_code, 200)
        self.assertContains(c.get("/reports/"), "Daily supplies versus payments")
        self.assertEqual(self.client_for("manager").get("/reports/daily_reconciliation/?preset=month").status_code,
                         200)

    def _sp(self, user=None):
        from core.reports.defs import supply_payment
        from core.reports.engine import Filters

        u = user or self.manager
        return supply_payment(Filters(date_from=douala_today(), date_to=douala_today(), user=u,
                                      cashier=u if u.is_cashier else None))

    def test_supply_payment_pivot_by_subzone(self):
        from core.models import MicroZone
        from core.services import capital as cap

        # agent Ndive also runs a second microzone (distribution road)
        extra = MicroZone.objects.create(name="Mile 4 road", subzone=self.a1.microzone.subzone, agent=self.a1)
        ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(70000), source_account=self.uve_f1)
        ops.record_supply(self.c_f2, product="UVE", customer=self.a1, amount=D(30000), source_account=self.uve_f2)
        ops.record_destockage(self.c_f2, pos=self.p1, agent=self.a1, amount=D(5000), drawer=self.d_f2)
        ops.record_supply(self.c_f2, product="CASH", customer=self.a2, amount=D(8000), source_account=self.d_f2)
        cap.inject(self.finance, account=self.mtn_f1, amount=D(100000), source="x", purpose="y")
        cap.float_exchange(self.finance, source=self.mtn_f1, amount=D(60000), to="AGENT", agent=self.a1)
        ops.record_payment(self.c_f1, payer=self.a1, parts={"CASH": D(40000)}, idempotency_key=uuid.uuid4())
        ops.record_payment(self.c_f2, payer=self.a1, parts={"MTN": D(10000)}, idempotency_key=uuid.uuid4())
        rep = self._sp()
        summary = {str(k): v for k, v, _k in rep.summary}
        f1, f2 = self.c_f1.subzone.name, self.c_f2.subzone.name
        self.assertEqual((summary[f"Supplied · {f1}"], summary[f"Supplied · {f2}"],
                          summary["Float supplied (finance)"], summary["Total supplied"]),
                         (D(70000), D(43000), D(60000), D(173000)))
        self.assertEqual((summary[f"Paid · {f1}"], summary["Paid in float"], summary["Total paid"]),
                         (D(40000), D(10000), D(50000)))
        self.assertNotIn(f"Paid · {f2}", summary)  # Fako 2 only received float
        self.assertEqual(summary["Total balance today, all desks (+ owes, − credit)"],
                         self.cbal(self.a1) + self.cbal(self.a2) + self.cbal(self.p1))
        subs, table = rep.tables[0], rep.tables[1]
        rows = table.rows
        ndive = [r for r in rows if r["customer"].startswith(self.a1.name)]
        self.assertEqual(len(ndive), 1)  # once, even with two microzones
        r = ndive[0]
        self.assertIn("Mile 4 road", r["microzones"])
        self.assertIn(self.a1.microzone.name, r["microzones"])
        s1, s2, p1 = f"s_{self.c_f1.pk}", f"s_{self.c_f2.pk}", f"p_{self.c_f1.pk}"
        self.assertEqual((r[s1], r[s2], r["float_sup"], r["supplied"], r[p1], r["paid_float"], r["paid"], r["net"]),
                         (D(70000), D(35000), D(60000), D(165000), D(40000), D(10000), D(50000), D(115000)))
        cols = [c.key for c in table.columns]
        self.assertLess(cols.index(s2), cols.index("float_sup"))
        self.assertLess(cols.index("supplied"), cols.index(p1))
        self.assertLess(cols.index(p1), cols.index("paid_float"))
        heads = [x["customer"] for x in rows if x.get("_head")]
        self.assertIn(self.a1.microzone.subzone.name, heads)
        subtotal = next(x for x in rows if x.get("_sub") and self.a1.microzone.subzone.name in x["customer"])
        self.assertGreaterEqual(subtotal["supplied"], D(165000))
        self.assertEqual(table.totals()["supplied"], D(173000))  # subtotals not counted twice
        self.assertEqual(subs.totals()["supplied"], D(173000))
        desks = {x["cashier"]: x for x in rep.tables[3].rows}
        self.c_f2.refresh_from_db()
        self.assertEqual(desks[self.c_f2.subzone.name]["DESTOCK"], D(5000))
        self.assertEqual(desks[self.c_f2.subzone.name]["P_MTN"], D(10000))
        c = self.client_for("manager")
        for fmt_ in ("", "&format=pdf", "&format=xlsx"):
            self.assertEqual(c.get(f"/reports/supply_payment/?preset=day{fmt_}").status_code, 200)
        page = c.get("/reports/supply_payment/?preset=day")
        self.assertContains(page, "Summary by subzone")
        self.assertContains(page, "subtotal")
        # a cashier's own report includes the float supplied from his desk's float account
        mine = self._sp(self.c_f1)
        s1 = {str(k): v for k, v, _k in mine.summary}
        self.assertEqual(s1["Float supplied (finance)"], D(60000))
        self.assertEqual({str(k): v for k, v, _k in self._sp(self.c_f2).summary}["Float supplied (finance)"], D(0))

    def test_float_to_agent_in_other_reports(self):
        from core.reports.defs import _customer_rows, statement, supplies
        from core.reports.engine import Filters
        from core.services import capital as cap

        cap.inject(self.finance, account=self.mtn_f1, amount=D(200000), source="x", purpose="y")
        cap.float_exchange(self.finance, source=self.mtn_f1, amount=D(80000), to="AGENT", agent=self.a1)
        cap.float_exchange(self.finance, source=self.mtn_f1, amount=D(5000), to="DESK", uve_account=self.uve_f2)
        f = Filters(date_from=douala_today(), date_to=douala_today(), user=self.manager)
        groups = {r["group"]: r["amount"] for r in supplies(f).tables[0].rows}
        self.assertEqual(groups["Float (converted to UVE)"], D(80000))
        f.customer = self.a1
        self.assertEqual({str(k): v for k, v, _k in statement(f).summary}["Float supply"], D(80000))
        f.customer = None
        self.assertEqual(next(r for r in _customer_rows(f) if r["name"] == self.a1.name)["supplied"], D(80000))
