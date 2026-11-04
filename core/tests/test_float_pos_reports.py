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
        with self.assertRaises(BusinessError):
            sh.uve_to_pos(mgr, pos=self.a1, amount=D(1))  # an agent is not a POS
        c = self.client_for("shop.molyko")
        for op in ("pos_uve_out", "pos_uve_in"):
            self.assertEqual(c.get(f"/shops/do/{op}/").status_code, 200)
        r = c.post("/shops/do/pos_uve_out/", {"idem": str(uuid.uuid4()), "pos": self.p1.business_sim,
                                              "amount": "1 000", "pay": "CASH"})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        self.assertEqual(Transaction.objects.filter(type=TxType.SHOP_POS_UVE_OUT).count(), 3)

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
