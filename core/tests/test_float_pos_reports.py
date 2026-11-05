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

    def test_supply_payment_split_by_cashier(self):
        ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(70000), source_account=self.uve_f1)
        ops.record_supply(self.c_f2, product="UVE", customer=self.a1, amount=D(30000), source_account=self.uve_f2)
        ops.record_destockage(self.c_f2, pos=self.p1, agent=self.a1, amount=D(5000), drawer=self.d_f2)
        ops.record_payment(self.c_f1, payer=self.a1, parts={"CASH": D(40000)}, idempotency_key=uuid.uuid4())
        ops.record_payment(self.c_f2, payer=self.a1, parts={"UVE": D(10000)}, idempotency_key=uuid.uuid4())
        from core.reports.defs import supply_payment
        from core.reports.engine import Filters

        f = Filters(date_from=douala_today(), date_to=douala_today(), user=self.manager)
        rep = supply_payment(f)
        per_agent = rep.tables[1]
        keys = [c.key for c in per_agent.columns]
        s1, s2 = f"s_{self.c_f1.pk}", f"s_{self.c_f2.pk}"
        self.assertLess(keys.index(s1), keys.index("supplied"))
        self.assertLess(keys.index("supplied"), keys.index(f"p_{self.c_f1.pk}"))
        self.assertLess(keys.index(f"p_{self.c_f2.pk}"), keys.index("paid"))
        row = next(r for r in per_agent.rows if self.a1.name in r["customer"])
        self.assertEqual((row[s1], row[s2], row["supplied"]), (D(70000), D(35000), D(105000)))  # destockage included
        self.assertEqual((row[f"p_{self.c_f1.pk}"], row[f"p_{self.c_f2.pk}"], row["paid"]),
                         (D(40000), D(10000), D(50000)))
        self.assertEqual(row["net"], D(55000))
        desks = {r["cashier"]: r for r in rep.tables[2].rows}
        self.assertEqual(desks[str(self.c_f2)]["DESTOCK"], D(5000))
        c = self.client_for("manager")
        for fmt_ in ("", "&format=pdf", "&format=xlsx"):
            self.assertEqual(c.get(f"/reports/supply_payment/?preset=day{fmt_}").status_code, 200)
        # a cashier sees only his own desk: no per-cashier columns
        r = self.client_for("cashier.fako1").get("/reports/supply_payment/?preset=day")
        self.assertNotContains(r, "Supplied ·")

    def test_float_to_agent_is_a_supply_in_reports(self):
        from core.reports.defs import _customer_rows, statement, supplies, supply_payment
        from core.reports.engine import Filters
        from core.services import capital as cap

        sim = CompanySim.objects.create(network="MTN", msisdn="680000444", label="Finance float",
                                        master_zone=self.zones["fako"], subzone=self.zones["fako1"])
        flt = Account.objects.get(sim=sim, kind=K.MTN)
        cap.inject(self.finance, account=flt, amount=D(200000), source="x", purpose="y")
        cap.float_exchange(self.finance, source=flt, amount=D(80000), to="AGENT", agent=self.a1)
        cap.float_exchange(self.finance, source=flt, amount=D(5000), to="DESK", uve_account=self.uve_f2)  # not a supply
        ops.record_payment(self.c_f1, payer=self.a1, parts={"MTN": D(30000)}, accounts={"MTN": self.mtn_f1},
                           idempotency_key=uuid.uuid4())
        f = Filters(date_from=douala_today(), date_to=douala_today(), user=self.manager)
        groups = {r["group"]: r["amount"] for r in supplies(f).tables[0].rows}
        self.assertEqual(groups["Float (converted to UVE)"], D(80000))
        sp = supply_payment(f)
        row = next(r for r in sp.tables[1].rows if self.a1.name in r["customer"])
        self.assertEqual((row["supplied"], row["paid"]), (D(80000), D(30000)))
        by_desk = {r["cashier"]: r for r in sp.tables[2].rows}
        self.assertEqual(by_desk[str(self.finance)]["FLOAT"], D(80000))
        self.assertEqual(by_desk[str(self.c_f1)]["P_MTN"], D(30000))
        self.assertIn("Paid float (MTN)", [c.label for c in sp.tables[2].columns])
        f.customer = self.a1
        summ = {str(k): v for k, v, _kind in statement(f).summary}
        self.assertEqual(summ["Float supply"], D(80000))
        f.customer = None
        cust = next(r for r in _customer_rows(f) if r["name"] == self.a1.name)
        self.assertEqual(cust["supplied"], D(80000))
        self.assertEqual(cust["paid"], D(30000))

    def test_float_supply_in_the_desks_own_report(self):
        from core.services import capital as cap

        cap.inject(self.finance, account=self.mtn_f1, amount=D(100000), source="x", purpose="y")
        cap.float_exchange(self.finance, source=self.mtn_f1, amount=D(60000), to="AGENT", agent=self.a1)
        for u in ("cashier.fako1", "manager"):
            r = self.client_for(u).get("/reports/supply_payment/?preset=day")
            self.assertContains(r, "of which float supply", msg_prefix=u)
            self.assertContains(r, "60 000", msg_prefix=u)
        # another desk does not see it
        self.assertNotContains(self.client_for("cashier.fako2").get("/reports/supply_payment/?preset=day"),
                               "of which float supply")
