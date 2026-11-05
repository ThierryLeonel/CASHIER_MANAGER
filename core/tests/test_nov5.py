"""Admin edits any entry; supplies may go negative; agents and POS wording; paid-now on supplies; "No" to paying a
transfer back; payments into another desk's wallet; finance exchanges float for UVE."""
import uuid

from django.test import TestCase

from core.models import Account, CompanySim, Transaction, TxType
from core.services import admin as adm
from core.services import capital as cap
from core.services import operations as ops
from core.services import settlements as st
from core.services import shop_shifts as ss
from core.services import shops as sh
from core.services.errors import BusinessError, InsufficientFunds

from .helpers import D, OrgMixin
from .test_web import PW

K = Account.Kind


class Nov5(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.shop = self.o["shop"]

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def _mtn(self, msisdn, zone, label):
        sim = CompanySim.objects.create(network="MTN", msisdn=msisdn, label=label, master_zone=self.zones["fako"],
                                        subzone=zone)
        return Account.objects.get(sim=sim, kind=K.MTN)

    def test_supply_may_go_negative(self):
        before = self.bal(self.uve_f1)
        ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=before + 50000, source_account=self.uve_f1)
        self.assertEqual(self.bal(self.uve_f1), D(-50000))
        # multi-product supply with a paid-now part also passes
        ops.record_supplies(self.c_f1, customer=self.a1, parts={"UVE": D(10000)}, paid={"CASH": D(1000)},
                            idempotency_key=uuid.uuid4())
        self.assertEqual(self.bal(self.uve_f1), D(-60000))
        # any other outflow still needs the money
        with self.assertRaises(InsufficientFunds):
            ops.record_desk_transfer(self.c_f1, desk=self.c_f2.subzone, uve=D(1), idempotency_key=uuid.uuid4())
        # money in repairs it
        ops.record_payment(self.c_f1, payer=self.a1, parts={"UVE": D(70000)}, idempotency_key=uuid.uuid4())
        self.assertEqual(self.bal(self.uve_f1), D(10000))
        self.assertContains(self.client_for("cashier.fako1").get("/"), "UVE")

    def test_supply_paid_now(self):
        owed = self.cbal(self.a1)
        ts = ops.record_supplies(self.c_f1, customer=self.a1, parts={"UVE": D(100000), "CASH": D(20000)},
                                 paid={"CASH": D(30000), "MTN": D(5000)}, idempotency_key=uuid.uuid4())
        self.assertEqual(self.cbal(self.a1), owed + 120000 - 35000)
        types = sorted(t.type for t in ts)
        self.assertEqual(types, ["COLLECTION", "COLLECTION", "SUPPLY", "SUPPLY"])
        self.assertEqual(sum(1 for t in ts if t.receipt_no), 2)  # one receipt for the supply, one for the payment
        with self.assertRaises(BusinessError):
            ops.record_supplies(self.c_f1, customer=self.a1, parts={"UVE": D(100)}, paid={"CASH": D(200)})
        c = self.client_for("cashier.fako1")
        r = c.post("/supply/agent/", {"idem": str(uuid.uuid4()), "agent": self.a1.business_sim, "uve": "40 000",
                                      "paid_cash": "10 000"})
        self.assertEqual(r.status_code, 302, r.content[:3000])
        self.assertEqual(self.cbal(self.a1), owed + 85000 + 30000)

    def test_no_to_paying_a_transfer_back(self):
        t = ops.record_desk_transfer(self.c_f1, desk=self.c_f2.subzone, cash=D(15000), idempotency_key=uuid.uuid4())[0]
        self.assertEqual(len(st.unpaid_for_desk(self.c_f2.subzone)), 1)
        c2 = self.client_for("cashier.fako2")
        self.assertContains(c2.get("/"), f"/transactions/{t.pk}/no-payback/")
        r = c2.post(f"/transactions/{t.pk}/no-payback/", {"next": "/"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(st.unpaid_for_desk(self.c_f2.subzone), [])  # no longer prompted
        self.assertEqual(st.outstanding(t), D(15000))  # it stays as it is
        self.assertFalse(st.can_pay(self.c_f2, t))
        self.assertContains(c2.get(f"/transactions/{t.pk}/"), "Not paid back")
        with self.assertRaises(Exception):
            st.decline(self.c_f1, t)  # the sender cannot answer for the receiver

    def test_payment_into_another_desks_wallet(self):
        # the agent sent UVE to Fako 2's wallet; the cashier of Fako 1 records it
        f2_before = self.bal(self.uve_f2)
        owed = self.cbal(self.a1)
        ops.record_payment(self.c_f1, payer=self.a1, parts={"UVE": D(25000)}, accounts={"UVE": self.uve_f2},
                           idempotency_key=uuid.uuid4())
        self.assertEqual(self.bal(self.uve_f2), f2_before + 25000)
        self.assertEqual(self.cbal(self.a1), owed - 25000)
        c = self.client_for("cashier.fako1")
        page = c.get("/collection/new/")
        self.assertContains(page, 'name="uve_account"')
        r = c.post("/collection/new/", {"idem": str(uuid.uuid4()), "payer": self.a1.business_sim, "uve": "5 000",
                                        "uve_account": self.uve_f2.pk})
        self.assertEqual(r.status_code, 302, r.content[:3000])
        self.assertEqual(self.bal(self.uve_f2), f2_before + 30000)
        with self.assertRaises(BusinessError):  # cash still goes to your own drawer only
            ops.record_payment(self.c_f1, payer=self.a1, parts={"CASH": D(1)}, accounts={"CASH": self.d_f2})

    def test_finance_exchanges_float_for_uve(self):
        flt = self._mtn("680000555", self.zones["fako1"], "Finance float")
        cap.inject(self.finance, account=flt, amount=D(300000), source="x", purpose="y")
        uve_before, owed = self.bal(self.uve_f2), self.cbal(self.a2)
        cap.float_exchange(self.finance, source=flt, amount=D(100000), to="DESK", uve_account=self.uve_f2)
        cap.float_exchange(self.finance, source=flt, amount=D(50000), to="AGENT", agent=self.a2)
        self.assertEqual(self.bal(flt), D(150000))
        self.assertEqual(self.bal(self.uve_f2), uve_before + 100000)
        self.assertEqual(self.cbal(self.a2), owed + 50000)
        with self.assertRaises(BusinessError):
            cap.float_exchange(self.finance, source=self.uve_f1, amount=D(1), to="DESK", uve_account=self.uve_f2)
        with self.assertRaises(InsufficientFunds):
            cap.float_exchange(self.finance, source=flt, amount=D(10_000_000), to="DESK", uve_account=self.uve_f2)
        fin = self.client_for("finance")
        self.assertContains(fin.get("/capital/"), "/capital/float/")
        r = fin.post("/capital/float/", {"idem": str(uuid.uuid4()), "source": flt.pk, "amount": "10 000",
                                         "to": "AGENT", "agent": self.a1.business_sim})
        self.assertEqual(r.status_code, 302, r.content[:3000])
        self.assertEqual(self.client_for("cashier.fako1").get("/capital/float/").status_code, 403)
        rep = self.client_for("manager").get("/reports/reconciliation/?preset=all")
        self.assertNotContains(rep, "MISMATCH")
        from core.services import agents as ag

        self.assertIn(TxType.FLOAT_EXCHANGE, [t.type for t in ag.office_supplies(self.a1)])

    def test_admin_edits_any_entry(self):
        mgr = self.o["shop_mgr"]
        ss.open_shift(mgr, cash=D(0), uve=D(0), er=D(0))
        cap.inject(self.finance, account=self.shop.account(K.UVE), amount=D(50000), source="x", purpose="y")
        t = sh.cash_in(mgr, amount=D(10000), account_name="Ngwa")
        adm.edit_transaction_simple(self.admin, t, amount=D(12000), account_name="Ngwa Peter", reason="typo")
        t.refresh_from_db()
        self.assertEqual((t.amount, t.account_name), (D(12000), "Ngwa Peter"))
        self.assertEqual(self.bal(self.shop.account(K.CASH_DRAWER)), D(12000))
        with self.assertRaises(BusinessError):
            adm.edit_transaction_simple(self.admin, t, amount=D(1), reason="")
        a = self.client_for("admin")
        self.assertEqual(a.get(f"/transactions/{t.pk}/edit/").status_code, 200)
        self.assertContains(a.get(f"/transactions/{t.pk}/"), f"/transactions/{t.pk}/edit/")
        r = a.post(f"/transactions/{t.pk}/edit/", {"amount": "9 000", "business_date": t.business_date.isoformat(),
                                                    "description": "fixed", "account_name": "Ngwa Peter",
                                                    "client_phone": "", "reason": "count"})
        self.assertEqual(r.status_code, 302, r.content[:3000])
        t.refresh_from_db()
        self.assertEqual((t.amount, t.description), (D(9000), "fixed"))
        mv = cap.inject(self.finance, account=self.d_f1, amount=D(1000), source="x", purpose="y")
        self.assertEqual(a.get(f"/transactions/{mv.pk}/edit/").status_code, 200)
        adm.delete_transaction(self.admin, t, reason="test")
        self.assertFalse(Transaction.objects.filter(pk=t.pk).exists())
        for path in ("/admin/core/agentcliententry/", "/admin/core/agentpayback/", "/admin/core/agentexchange/"):
            self.assertEqual(a.get(path).status_code, 200, path)

    def test_wording_agents_and_pos(self):
        r = self.client_for("manager").get("/reports/")
        self.assertContains(r, "Agent and POS balances")
        self.assertNotContains(r, "Customer balances")
        self.assertNotContains(self.client_for("cashier.fako1").get("/collection/new/"), "Paying customer")


class TwoSims(OrgMixin, TestCase):
    """A desk with two SIMs of a kind: one to collect payments, one to supply (or one for both)."""

    def setUp(self):
        self.make_org()
        self.fund_all()
        sim = CompanySim.objects.create(network="ORANGE", msisdn="690000777", label="Fako 1 collection",
                                        master_zone=self.zones["fako"], subzone=self.zones["fako1"])
        self.collect = Account.objects.get(sim=sim, kind=K.UVE)
        self.collect.usage = Account.Usage.COLLECTION
        self.collect.save(update_fields=["usage"])
        # the desk's existing UVE wallet becomes the supply one
        self.supply = Account.objects.filter(kind=K.UVE, zone=self.zones["fako1"], shop__isnull=True) \
            .exclude(pk=self.collect.pk).first() or self.uve_f1
        self.supply.usage = Account.Usage.SUPPLY
        self.supply.save(update_fields=["usage"])

    def client_for(self, u):
        c = self.client_class()
        self.assertTrue(c.login(username=u, password=PW))
        return c

    def test_defaults_follow_the_use(self):
        ts = ops.record_supplies(self.c_f1, customer=self.a1, parts={"UVE": D(10000)}, idempotency_key=uuid.uuid4())
        self.assertEqual(ts[0].source_account, self.supply)  # supplies leave from the supply SIM
        p = ops.record_payment(self.c_f1, payer=self.a1, parts={"UVE": D(4000)}, idempotency_key=uuid.uuid4())[0]
        self.assertEqual(p.dest_account, self.collect)  # payments arrive on the collection SIM
        # the cashier can still choose
        ts = ops.record_supplies(self.c_f1, customer=self.a1, parts={"UVE": D(1000)}, accounts={"UVE": self.collect},
                                 idempotency_key=uuid.uuid4())
        self.assertEqual(ts[0].source_account, self.collect)
        # a transfer from another desk arrives on the collection SIM
        t = ops.record_desk_transfer(self.c_f2, desk=self.zones["fako1"], uve=D(2000), idempotency_key=uuid.uuid4())[0]
        self.assertEqual(t.dest_account, self.collect)
        c = self.client_for("cashier.fako1")
        page = c.get("/supply/agent/")
        self.assertContains(page, 'name="uve_from"')
        self.assertContains(page, "supply")
        r = c.post("/supply/agent/", {"idem": str(uuid.uuid4()), "agent": self.a1.business_sim, "uve": "3 000",
                                      "uve_from": self.collect.pk})
        self.assertEqual(r.status_code, 302, r.content[:3000])
        self.assertEqual(Transaction.objects.filter(type=TxType.SUPPLY).order_by("-pk").first().source_account,
                         self.collect)
        self.assertContains(c.get("/transfer/new/"), 'name="uve_from"')
        self.assertEqual(self.client_for("admin").get(f"/admin/core/account/{self.collect.pk}/change/").status_code,
                         200)

    def test_default_for_payments(self):
        # both SIMs used for payments and supply: no obvious default, the cashier must choose
        Account.objects.filter(pk__in=[self.collect.pk, self.supply.pk]).update(usage=Account.Usage.BOTH)
        with self.assertRaises(BusinessError):
            ops.record_payment(self.c_f1, payer=self.a1, parts={"UVE": D(1000)}, idempotency_key=uuid.uuid4())
        c = self.client_for("cashier.fako1")
        r = c.post("/collection/new/", {"idem": str(uuid.uuid4()), "payer": self.a1.business_sim, "uve": "1 000"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "No default account is set for payments")
        # set one as the default for payments: it is used without choosing
        a = self.client_for("admin")
        r = a.post(f"/admin/core/account/{self.collect.pk}/change/", {
            "name": self.collect.name, "zone": self.collect.zone_id, "custodian": "", "usage": "BOTH",
            "default_collect": "on", "active": "on"})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        self.collect.refresh_from_db()
        self.assertTrue(self.collect.default_collect)
        p = ops.record_payment(self.c_f1, payer=self.a1, parts={"UVE": D(1000)}, idempotency_key=uuid.uuid4())[0]
        self.assertEqual(p.dest_account, self.collect)
        page = c.get("/collection/new/")
        self.assertContains(page, f'value="{self.collect.pk}" selected')
        # only one default per subzone and kind
        self.supply.default_collect = True
        self.supply.save()
        from core.admin import AccountAdmin

        AccountAdmin._one_default(self.supply)
        self.collect.refresh_from_db()
        self.assertFalse(self.collect.default_collect)
