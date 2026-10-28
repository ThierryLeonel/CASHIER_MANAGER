"""Zones: accounts belong to zones, master SIM in charge, closing per subzone,
junior/senior managers, zone and company reconciliation."""
from django.core.cache import cache
from django.test import TestCase

from core.models import Account, Transaction
from core.reports.defs import reconciliation
from core.reports.engine import Filters
from core.services import operations as ops
from core.services import permissions as perm
from core.services.errors import PermissionDenied

from .helpers import D, OrgMixin
from .test_web import PW


class ZoneAccounts(OrgMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.make_org()
        self.fund_all()

    def test_accounts_belong_to_zones(self):
        self.assertEqual(self.d_f1.zone.name, "Fako 1")
        self.assertIsNone(self.d_f1.custodian)
        self.assertEqual((self.er_f1.zone.name, self.er_f1.custodian), ("Fako", self.c_f1))
        usable_f2 = set(perm.usable_accounts(self.c_f2, Account.ASSET_KINDS).values_list("id", flat=True))
        self.assertIn(self.d_f2.id, usable_f2)
        self.assertNotIn(self.d_f1.id, usable_f2)
        self.assertNotIn(self.er_f1.id, usable_f2)  # master SIM: cashier in charge only
        with self.assertRaises(PermissionDenied):
            ops.record_expense(self.c_f2, category=self._fuel(), amount=D(1), drawer=self.d_f1, description="x")

    def test_entries_record_their_zone(self):
        t = ops.record_supply(self.c_f2, product="UVE", customer=self.a1, amount=D(1000), source_account=self.uve_f2)
        self.assertEqual((t.zone.name, t.zone.parent.name, t.cashier), ("Fako 2", "Fako", self.c_f2))

    def _fuel(self):
        from core.models import ExpenseCategory

        return ExpenseCategory.objects.get(code="fuel")


class ManagersAndReconciliation(OrgMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.make_org()
        self.fund_all()
        self.t_fako = ops.record_supply(self.c_f2, product="UVE", customer=self.a1, amount=D(11111),
                                        source_account=self.uve_f2)
        self.t_meme = ops.record_supply(self.c_me, product="UVE", customer=self.a3, amount=D(22222),
                                        source_account=self.uve_me)

    def login(self, username):
        c = self.client_class()
        self.assertTrue(c.login(username=username, password=PW))
        return c

    def test_junior_manager_sees_only_his_zone(self):
        j = self.login("junior.fako")
        r = j.get("/transactions/?preset=all")
        self.assertContains(r, self.t_fako.reference)
        self.assertNotContains(r, self.t_meme.reference)
        self.assertEqual(j.get(f"/transactions/{self.t_meme.pk}/").status_code, 404)
        self.assertEqual(j.get(f"/transactions/{self.t_fako.pk}/").status_code, 200)
        # correcting inside the zone is allowed, outside is not
        self.assertEqual(j.get(f"/transactions/{self.t_fako.pk}/correct/").status_code, 200)
        self.assertEqual(j.get(f"/transactions/{self.t_meme.pk}/correct/").status_code, 404)
        r = j.get(f"/reports/supplies/?preset=all&master_zone={self.zones['meme'].pk}")
        self.assertContains(r, "11 111")
        self.assertNotContains(r, "22 222")
        home = j.get("/")
        grand = {k: v for k, _l, v in home.context["grand"]}
        self.assertEqual(grand["UVE"], self.bal(self.uve_f1) + self.bal(self.uve_f2))
        self.assertEqual(j.get("/administration/").status_code, 403)

    def test_senior_manager_and_finance_see_everything(self):
        for u in ("manager", "finance"):
            r = self.login(u).get("/transactions/?preset=all")
            self.assertContains(r, self.t_fako.reference)
            self.assertContains(r, self.t_meme.reference)

    def test_zone_and_company_reconciliation(self):
        rep = reconciliation(Filters(None, None, user=self.finance))
        zt = rep.tables[0]
        names = [r["zone"].strip() for r in zt.rows]
        for n in ("Fako", "Fako 2", "Meme", "COMPANY TOTAL"):
            self.assertIn(n, names)
        company = next(r for r in zt.rows if r["zone"] == "COMPANY TOTAL")
        self.assertEqual(company["UVE_c"], self.bal(self.uve_f1) + self.bal(self.uve_f2) + self.bal(self.uve_me))
        fako = next(r for r in zt.rows if r["zone"] == "Fako")
        self.assertEqual(fako["UVE_c"], self.bal(self.uve_f1) + self.bal(self.uve_f2))
        master_line = next(r for r in zt.rows if "Master SIM" in r["zone"] and "Fako 1" in r["zone"])
        self.assertEqual(master_line["ER_c"], self.bal(self.er_f1))
        # a junior manager's reconciliation is limited to his zone, without the company total
        from core.reports.engine import FilterForm

        form = FilterForm(self.junior, {"preset": "all"})
        jf = Filters.from_form(form, self.junior)
        jnames = [r["zone"].strip() for r in reconciliation(jf).tables[0].rows]
        self.assertIn("Fako", jnames)
        self.assertNotIn("Meme", jnames)
        self.assertNotIn("COMPANY TOTAL", jnames)
        self.assertContains(self.login("finance").get("/reports/reconciliation/?preset=all"), "COMPANY TOTAL")

    def test_closings_visible_by_zone(self):
        from core.services import closing as cl

        acts = {a.id: self.bal(a) for a in cl.closing_accounts(self.c_me)}
        c = cl.close_day(self.c_me, actuals=acts)
        self.assertEqual(c.cashier_day.zone, self.c_me.subzone)
        self.assertEqual(self.login("junior.fako").get(f"/closings/{c.pk}/").status_code, 404)
        self.assertEqual(self.login("cashier.meme").get(f"/closings/{c.pk}/").status_code, 200)
        self.assertEqual(self.login("cashier.fako1").get(f"/closings/{c.pk}/").status_code, 404)
        self.assertEqual(self.login("finance").get(f"/closings/{c.pk}/").status_code, 200)
        self.assertTrue(Transaction.objects.filter(zone=self.c_me.subzone).exists())
