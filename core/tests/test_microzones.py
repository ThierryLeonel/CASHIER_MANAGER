"""Microzones: each controlled by one commercial agent; POS belong to a microzone."""
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.models import Counterparty, MicroZone, Zone

from .helpers import OrgMixin
from .test_web import PW


class Microzones(OrgMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.make_org()
        self.c = self.client_class()
        self.assertTrue(self.c.login(username="cashier.fako1", password=PW))

    def test_sample_links(self):
        p1 = self.o["pos"]["p1"]
        self.assertEqual(p1.microzone.name, "Molyko")
        self.assertEqual(p1.agent_in_charge, self.a1)
        self.assertEqual(p1.master_zone, self.zones["fako"])  # filled from the microzone

    def test_register_pos_in_existing_microzone(self):
        mz = self.o["microzones"]["molyko"]
        r = self.c.post("/customers/new/", {"kind": "POS", "business_sim": "692111111", "name": "POS Check",
                                            "microzone": mz.pk})
        self.assertEqual(r.status_code, 302)
        cp = Counterparty.objects.get(business_sim="692111111")
        self.assertEqual((cp.microzone, cp.agent_in_charge), (mz, self.a1))

    def test_pos_needs_a_microzone(self):
        r = self.c.post("/customers/new/", {"kind": "POS", "business_sim": "692111112", "name": "POS X"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "A POS belongs to a microzone")

    def test_new_agent_creates_and_controls_new_microzone(self):
        r = self.c.post("/customers/new/", {"kind": "AGENT", "business_sim": "691222222", "name": "Agent New",
                                            "new_microzone": "Great Soppo",
                                            "new_microzone_subzone": self.c_f1.subzone.pk})
        self.assertEqual(r.status_code, 302)
        mz = MicroZone.objects.get(name="Great Soppo")
        self.assertEqual((mz.subzone, mz.agent.business_sim), (self.c_f1.subzone, "691222222"))

    def test_microzone_has_one_agent(self):
        r = self.c.post("/customers/new/", {"kind": "AGENT", "business_sim": "691333333", "name": "Agent Two",
                                            "microzone": self.o["microzones"]["molyko"].pk})
        self.assertContains(r, "already controlled by")

    def test_cashier_sees_only_his_master_zone_microzones(self):
        form = self.c.get("/customers/new/").context["form"]
        names = set(form.fields["microzone"].queryset.values_list("name", flat=True))
        self.assertEqual(names, {"Molyko", "Bonduma"})

    def test_list_shows_microzone_and_filters(self):
        r = self.c.get(f"/customers/?mz={self.o['microzones']['molyko'].pk}")
        self.assertContains(r, "POS Molyko")
        self.assertContains(r, "Agent Ndive")
        self.assertNotContains(r, "691 00 00 03")  # agent of another microzone

    def test_zone_cannot_be_its_own_master(self):
        z = self.zones["fako"]
        z.parent = z
        with self.assertRaises(ValidationError):
            z.full_clean()
        mz = MicroZone(subzone=self.zones["fako"], name="Wrong level")
        with self.assertRaises(ValidationError):
            mz.full_clean()


class EmptySetup(TestCase):
    """A brand-new site: pages explain what to create instead of showing empty lists."""

    def setUp(self):
        from core.models import User

        self.admin = User.objects.create_user("boss", password=PW, role="SUPERADMIN")
        self.c = self.client_class()
        self.c.login(username="boss", password=PW)

    def test_opening_balances_explains(self):
        self.assertContains(self.c.get("/setup/opening-balances/"), "No account is waiting for an opening balance")

    def test_agent_form_explains_missing_zones(self):
        Zone.objects.create(code="fako", name="Fako")
        r = self.c.get("/customers/new/")
        self.assertContains(r, "No subzone exists yet")
        self.assertContains(r, "No microzone yet")


class PickerAndExpenses(OrgMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.make_org()
        self.fund_all()
        self.c = self.client_class()
        self.assertTrue(self.c.login(username="cashier.fako1", password=PW))

    def test_search_shows_and_finds_microzone(self):
        res = self.c.get("/api/customers/?kind=AGENT&q=molyko").json()["results"]
        self.assertEqual([r["sim"] for r in res], ["691000001"])
        self.assertEqual((res[0]["microzone"], res[0]["subzone"]), ("Molyko", "Fako 1"))
        pos = self.c.get("/api/customers/?kind=POS&q=692000001").json()["results"][0]
        self.assertEqual((pos["microzone"], pos["agent_in_charge"]), ("Molyko", "[SAMPLE] Agent Ndive"))

    def test_expense_from_uve_or_mtn(self):
        from core.models import ExpenseCategory
        from core.services import operations as ops

        from .helpers import D

        fuel = ExpenseCategory.objects.get(code="fuel")
        ops.record_opening_balance(self.admin, account=self.mtn_f1, amount=D(10000))
        before_uve, before_mtn = self.bal(self.uve_f1), self.bal(self.mtn_f1)
        ops.record_expense(self.c_f1, category=fuel, amount=D(5000), drawer=self.uve_f1, description="paid in UVE")
        ops.record_expense(self.c_f1, category=fuel, amount=D(3000), drawer=self.mtn_f1, description="paid in float")
        self.assertEqual((self.bal(self.uve_f1), self.bal(self.mtn_f1)), (before_uve - 5000, before_mtn - 3000))
        form = self.c.get("/expense/new/").context["form"]
        kinds = set(form.fields["drawer"].queryset.values_list("kind", flat=True))
        self.assertEqual(kinds, {"CASH_DRAWER", "UVE", "MTN"})
