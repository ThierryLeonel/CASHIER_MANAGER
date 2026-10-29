"""Super-administrator powers, import, dashboards, cashier IDs and languages."""
import io
import uuid

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Sum
from django.test import TestCase
from openpyxl import Workbook

from core.models import AuditEvent, Counterparty, Entry, ExpenseCategory, Transaction, TxType, User
from core.services import admin as adm
from core.services import corrections as corr
from core.services import importer
from core.services import operations as ops
from core.services.errors import BusinessError, PermissionDenied

from .helpers import D, OrgMixin
from .test_web import PW, pdf_text


def xlsx_bytes(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def ledger_ok():
    return (Entry.objects.aggregate(s=Sum("amount"))["s"] or 0) == 0


class AdminPowers(OrgMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.make_org()
        self.fund_all()

    def test_delete_restores_balances_and_keeps_audit_copy(self):
        t = ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(70000), source_account=self.uve_f1)
        with self.assertRaises(PermissionDenied):
            adm.delete_transaction(self.manager, t, reason="x")
        adm.delete_transaction(self.admin, t, reason="test entry")
        self.assertFalse(Transaction.objects.filter(pk=t.pk).exists())
        self.assertEqual((self.cbal(self.a1), self.bal(self.uve_f1)), (D(0), D(1000000)))
        ev = AuditEvent.objects.get(action="transaction_deleted")
        self.assertEqual(ev.detail["transactions"][0]["reference"], t.reference)
        self.assertEqual(ev.detail["reason"], "test entry")
        self.assertTrue(ledger_ok())

    def test_delete_corrected_removes_whole_chain_and_reversal_alone_refused(self):
        t = ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(50000), source_account=self.uve_f1)
        c = corr.correct_transaction(self.manager, t, kind="REPLACE", reason="typo", new_values={"amount": D(40000)})
        with self.assertRaises(BusinessError):
            adm.delete_transaction(self.admin, c.reversal_txn, reason="x")
        adm.delete_transaction(self.admin, t, reason="remove all")
        self.assertEqual(Transaction.objects.filter(type__in=[TxType.SUPPLY, TxType.REVERSAL]).count(), 0)
        self.assertEqual(self.cbal(self.a1), D(0))
        self.assertTrue(ledger_ok())

    def test_delete_refused_when_it_would_make_a_drawer_negative(self):
        opening = Transaction.objects.get(opening_for_account=self.d_f1)
        ops.record_expense(self.c_f1, category=ExpenseCategory.objects.get(code="fuel"), amount=D(10000),
                           drawer=self.d_f1, description="x")
        with self.assertRaises(BusinessError) as cm:
            adm.delete_transaction(self.admin, opening, reason="x")
        self.assertIn("negative", cm.exception.message)
        self.assertTrue(Transaction.objects.filter(pk=opening.pk).exists())

    def test_edit_in_place(self):
        t = ops.record_collection(self.c_f1, payer=self.a1, method="CASH", amount=D(30000), account=self.d_f1)
        ref, receipt = t.reference, t.receipt_no
        t2 = adm.edit_transaction(self.admin, t, new_values={"amount": D(35000), "payer": self.a2}, reason="typo")
        self.assertEqual((t2.reference, t2.receipt_no, t2.amount, t2.counterparty), (ref, receipt, D(35000), self.a2))
        self.assertEqual((self.cbal(self.a1), self.cbal(self.a2)), (D(0), D(-35000)))
        self.assertEqual(self.bal(self.d_f1), D(1035000))
        ev = AuditEvent.objects.get(action="transaction_edited")
        self.assertEqual(ev.detail["before"]["amount"], "30000")
        self.assertEqual(ev.detail["after"]["amount"], "35000")
        with self.assertRaises(BusinessError):  # would overspend the UVE wallet
            s = ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(1000),
                                  source_account=self.uve_f1)
            adm.edit_transaction(self.admin, s, new_values={"amount": D(5000000)}, reason="x")
        self.assertTrue(ledger_ok())

    def test_clear_data(self):
        ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(1000), source_account=self.uve_f1)
        with self.assertRaises(BusinessError):
            adm.clear_data(self.admin, scope="transactions", password="wrong", confirm_text="DELETE")
        with self.assertRaises(BusinessError):
            adm.clear_data(self.admin, scope="transactions", password=PW, confirm_text="nope")
        adm.clear_data(self.admin, scope="transactions", password=PW, confirm_text="DELETE")
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertTrue(Counterparty.objects.exists())
        t = ops.record_opening_balance(self.admin, account=self.d_f1, amount=D(5))
        self.assertEqual(t.reference, "TX00000001")  # numbering restarted
        adm.clear_data(self.admin, scope="everything", password=PW, confirm_text="supprimer")
        self.assertFalse(Counterparty.objects.exists())
        self.assertFalse(User.objects.filter(role="AGENT").exists())
        self.assertTrue(User.objects.filter(role="CASHIER").exists())
        self.assertTrue(AuditEvent.objects.filter(action="data_cleared").count() == 2)

    def test_admin_records_for_a_cashier_over_http(self):
        c = self.client_class()
        c.login(username="admin", password=PW)
        r = c.get("/supply/agent/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/act-as/", r["Location"])
        c.post("/act-as/", {"cashier": self.c_f2.pk})
        r = c.post("/supply/agent/", {"idem": str(uuid.uuid4()), "product": "CASH", "agent": "691000001",
                                      "amount": "12 000", "source_account": self.d_f2.pk})
        self.assertEqual(r.status_code, 302, r.content[:500])
        t = Transaction.objects.get(type=TxType.SUPPLY)
        self.assertEqual((t.cashier, t.entered_by, t.cashier_day.cashier), (self.c_f2, self.admin, self.c_f2))
        pdf = pdf_text(c.get(f"/transactions/{t.pk}/receipt.pdf").content)
        self.assertIn(self.c_f2.staff_code, pdf)
        self.assertIn(self.admin.staff_code, pdf)
        # the admin can also correct, edit and delete through the web
        self.assertEqual(c.get(f"/transactions/{t.pk}/correct/").status_code, 200)
        self.assertEqual(c.get(f"/transactions/{t.pk}/edit/").status_code, 200)
        r = c.post(f"/transactions/{t.pk}/delete/", {"reason": "duplicate"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Transaction.objects.filter(type=TxType.SUPPLY).exists())
        m = self.client_class()
        m.login(username="manager", password=PW)
        self.assertEqual(m.get("/administration/").status_code, 403)
        self.assertEqual(m.get("/administration/import/").status_code, 403)


class Imports(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()

    def test_customers_csv_upsert(self):
        csv_text = "type;business_sim;name;master_zone\nAGENT;+237 699 11 22 33;New agent;fako\nPDV;692000001;Renamed POS;\n"
        rows = importer.read_rows("c.csv", csv_text.encode())
        results, ok = importer.run_import(self.admin, "customers", rows, commit=True)
        self.assertTrue(ok, results)
        self.assertEqual(Counterparty.objects.get(business_sim="699112233").name, "New agent")
        self.assertEqual(Counterparty.objects.get(business_sim="692000001").name, "Renamed POS")

    def test_transactions_dry_run_error_and_commit(self):
        head = importer.TEMPLATES["transactions"][0]
        good = [["2026-10-01", "cashier.fako1", "SUPPLY_AGENT", "691000001", "", "UVE", "690000001", 100000, "", "", ""],
                ["01/10/2026", "CSH-%04d" % self.c_f2.pk, "PAYMENT", "691000001", "", "CASH", "", "40 000", "", "", ""],
                ["2026-10-01", "cashier.meme", "EXPENSE", "", "", "", "", 5000, "fuel", "", "fuel"]]
        bad = good + [["2026-10-01", "cashier.fako1", "SUPPLY_AGENT", "692000001", "", "UVE", "", 1, "", "", ""]]
        rows = importer.read_rows("t.xlsx", xlsx_bytes([head] + bad))
        results, ok = importer.run_import(self.admin, "transactions", rows, commit=True)
        self.assertFalse(ok)
        self.assertFalse(results[-1][1])
        self.assertEqual(Transaction.objects.filter(type__in=["SUPPLY", "COLLECTION", "EXPENSE"]).count(), 0)
        rows = importer.read_rows("t.xlsx", xlsx_bytes([head] + good))
        results, ok = importer.run_import(self.admin, "transactions", rows, commit=False)
        self.assertTrue(ok, results)
        self.assertEqual(Transaction.objects.filter(type="SUPPLY").count(), 0)  # dry run rolled back
        results, ok = importer.run_import(self.admin, "transactions", rows, commit=True)
        self.assertTrue(ok, results)
        s = Transaction.objects.get(type="SUPPLY")
        self.assertEqual((s.cashier, s.entered_by, str(s.business_date)), (self.c_f1, self.admin, "2026-10-01"))
        self.assertEqual(Transaction.objects.get(type="COLLECTION").cashier, self.c_f2)
        self.assertEqual(self.cbal(self.a1), D(60000))
        self.assertTrue(ledger_ok())

    def test_opening_import_and_web_flow(self):
        rows = [["kind", "identifier", "amount"], ["CUSTOMER", "691000003", "-15000"], ["MTN", "670000001", "7000"],
                ["CASH_MANAGER", "650000001", "-50000"]]
        c = self.client_class()
        c.login(username="admin", password=PW)
        up = SimpleUploadedFile("o.xlsx", xlsx_bytes(rows))
        r = c.post("/administration/import/", {"kind": "opening", "file": up})
        self.assertContains(r, "Confirm import")
        self.assertEqual(self.bal(self.mtn_f1), D(0))
        r = c.post("/administration/import/", {"action": "confirm"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual((self.bal(self.mtn_f1), self.cbal(self.a3)), (D(7000), D(-15000)))
        self.assertEqual(c.get("/administration/import/template/transactions/").status_code, 200)


class DashboardsAndLanguage(OrgMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.make_org()
        self.fund(self.d_f1, 123456)
        self.fund(self.er_me, 654321)

    def test_balances_on_dashboards(self):
        c = self.client_class()
        c.login(username="cashier.fako1", password=PW)
        r = c.get("/")
        self.assertContains(r, "123 456")
        self.assertNotContains(r, "654 321")
        m = self.client_class()
        m.login(username="finance", password=PW)
        r = m.get("/")
        self.assertContains(r, "123 456")
        self.assertContains(r, "654 321")
        grand = {k: v for k, _l, v in r.context["grand"]}
        self.assertEqual((grand["CASH_DRAWER"], grand["ER"]), (D(123456), D(654321)))
        r = m.get(f"/?zone={self.zones['fako'].pk}")
        grand = {k: v for k, _l, v in r.context["grand"]}
        self.assertEqual((grand["CASH_DRAWER"], grand["ER"]), (D(123456), 0))
        self.assertContains(r, self.c_f1.staff_code)

    def test_language_follows_user(self):
        c = self.client_class()
        c.login(username="manager", password=PW)
        self.assertContains(c.get("/"), "Overview")
        c.post("/language/", {"language": "fr", "next": "/"})
        self.assertEqual(User.objects.get(username="manager").language, "fr")
        c2 = self.client_class()  # another device, no cookie
        c2.login(username="manager", password=PW)
        self.assertContains(c2.get("/"), "Vue d'ensemble")
        c2.post("/language/", {"language": "en", "next": "/"})
        self.assertContains(c2.get("/"), "Overview")


class InactivitySignOut(OrgMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.make_org()

    def test_session_expires_after_idle_limit_and_keepalive(self):
        from django.conf import settings

        self.assertEqual(settings.SESSION_COOKIE_AGE, 15 * 60)
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)
        c = self.client_class()
        c.login(username="cashier.fako1", password=PW)
        r = c.get("/")
        self.assertContains(r, 'id="idle"')
        self.assertEqual(c.session.get_expiry_age(), 900)
        self.assertEqual(c.post("/session/keepalive/").status_code, 200)
        r = c.post("/session/idle-logout/")
        self.assertEqual(r["Location"], "/login/?idle=1")
        self.assertEqual(c.post("/session/keepalive/").status_code, 401)
        self.assertEqual(c.get("/").status_code, 302)  # signed out
        self.assertContains(self.client_class().get("/login/?idle=1"), "15 minutes without activity")
        self.assertTrue(AuditEvent.objects.filter(action="auto_logout_idle").exists())

    def test_expired_session_redirects_to_login(self):
        c = self.client_class()
        c.login(username="manager", password=PW)
        s = c.session
        s.set_expiry(-1)  # simulate 15 minutes passing
        s.save()
        self.assertEqual(c.get("/").status_code, 302)

    def test_reports_page_cards(self):
        c = self.client_class()
        c.login(username="manager", password=PW)
        r = c.get("/reports/")
        self.assertContains(r, 'class="report-card"', count=15)
        self.assertNotContains(r, "daily/weekly")


class ZoneConfiguration(TestCase):
    def setUp(self):
        from core.models import User

        User.objects.create_user("boss", password="Sample-Pass-2026!", role="SUPERADMIN", is_staff=True)
        self.c = self.client_class()
        self.c.login(username="boss", password="Sample-Pass-2026!")

    def test_master_zones_and_subzones_are_separate(self):
        from core.models import Zone

        r = self.c.post("/admin/core/masterzone/add/", {
            "name": "Fako", "code": "fako", "active": "on",
            "subzones-TOTAL_FORMS": "2", "subzones-INITIAL_FORMS": "0", "subzones-MIN_NUM_FORMS": "0",
            "subzones-MAX_NUM_FORMS": "1000",
            "subzones-0-name": "Fako 1", "subzones-0-code": "fako-1", "subzones-0-active": "on",
            "subzones-1-name": "Fako 2", "subzones-1-code": "fako-2", "subzones-1-active": "on"})
        self.assertEqual(r.status_code, 302, [str(e) for e in r.context["errors"]] if r.context else r.status_code)
        fako = Zone.objects.get(code="fako")
        self.assertIsNone(fako.parent)
        self.assertContains(self.c.get(f"/admin/core/masterzone/{fako.pk}/change/"), "Subzones of this master zone")
        self.assertEqual(set(fako.subzones.values_list("name", flat=True)), {"Fako 1", "Fako 2"})
        masters = self.c.get("/admin/core/masterzone/")
        self.assertContains(masters, "Fako 1, Fako 2")
        subs = self.c.get("/admin/core/subzone/")
        self.assertContains(subs, "Fako 1")
        self.assertNotContains(subs, 'field-name"><a href="/admin/core/subzone/%d/' % fako.pk)
        # a subzone needs a master zone; only master zones are offered
        r = self.c.post("/admin/core/subzone/add/", {"name": "Lost", "code": "lost", "active": "on"})
        self.assertEqual(r.status_code, 200)
        opts = r.context["adminform"].form.fields["parent"].queryset
        self.assertEqual(list(opts), [fako])
        # an unused zone saved as a subzone by mistake can be turned into a master zone
        wrong = Zone.objects.create(code="meme", name="Meme", parent=fako)
        self.c.post("/admin/core/subzone/", {"action": "make_master", "_selected_action": [wrong.pk]})
        wrong.refresh_from_db()
        self.assertIsNone(wrong.parent)

    def test_fix_nested_zones_from_the_subzone_page(self):
        from core.models import Zone

        meme = Zone.objects.create(code="meme", name="MEME")
        ndian = Zone.objects.create(code="2000", name="MEME NDIAN", parent=meme)
        fako_bad = Zone.objects.create(code="1000", name="FAKO")
        Zone.objects.filter(pk=fako_bad.pk).update(parent=ndian)  # invalid nesting from earlier edits
        Zone.objects.filter(pk=ndian.pk).update(parent=fako_bad)  # and a loop
        lst = self.c.get("/admin/core/subzone/")
        self.assertContains(lst, "has zones under it")
        page = self.c.get(f"/admin/core/subzone/{ndian.pk}/change/")
        self.assertContains(page, "Make this zone a master zone")
        # MEME NDIAN under MEME is refused while FAKO sits under it, with the names given
        r = self.c.post(f"/admin/core/subzone/{ndian.pk}/change/", {"name": "MEME NDIAN", "code": "2000",
                                                                   "parent": meme.pk, "active": "on"})
        self.assertContains(r, "still has zones under it (FAKO)")
        # make FAKO (1000) a master zone, then MEME NDIAN can go under MEME
        r = self.c.post(f"/admin/core/subzone/{fako_bad.pk}/change/", {"name": "FAKO", "code": "1000",
                                                                      "make_master": "on", "active": "on"})
        self.assertEqual(r.status_code, 302)
        fako_bad.refresh_from_db()
        self.assertIsNone(fako_bad.parent)
        r = self.c.post(f"/admin/core/subzone/{ndian.pk}/change/", {"name": "MEME NDIAN", "code": "2000",
                                                                   "parent": meme.pk, "active": "on"})
        self.assertEqual(r.status_code, 302)
        ndian.refresh_from_db()
        self.assertEqual(ndian.parent, meme)

    def test_used_subzone_cannot_become_master(self):
        from core.models import User, Zone

        m = Zone.objects.create(code="m", name="M")
        s = Zone.objects.create(code="s", name="S", parent=m)
        User.objects.create_user("c1", password="x-Strong-Pass-1", role="CASHIER", subzone=s)
        r = self.c.post(f"/admin/core/subzone/{s.pk}/change/", {"name": "S", "code": "s", "make_master": "on",
                                                               "active": "on"})
        self.assertContains(r, "cannot become a master zone")
        self.assertContains(r, "1 cashier(s)")

    def test_admin_adds_updates_and_deletes_zones(self):
        from core.models import Account, MicroZone, Zone

        m = Zone.objects.create(code="ndian", name="NDIAN")
        s1 = Zone.objects.create(code="ndian-1", name="Ndian 1", parent=m)
        MicroZone.objects.create(subzone=s1, name="Mundemba")
        # update
        r = self.c.post(f"/admin/core/masterzone/{m.pk}/change/", {
            "name": "NDIAN DIVISION", "code": "ndian", "active": "on",
            "subzones-TOTAL_FORMS": "1", "subzones-INITIAL_FORMS": "1", "subzones-MIN_NUM_FORMS": "0",
            "subzones-MAX_NUM_FORMS": "1000", "subzones-0-id": s1.pk, "subzones-0-parent": m.pk,
            "subzones-0-name": "Ndian One", "subzones-0-code": "ndian-1", "subzones-0-active": "on"})
        self.assertEqual(r.status_code, 302)
        s1.refresh_from_db()
        self.assertEqual((Zone.objects.get(pk=m.pk).name, s1.name), ("NDIAN DIVISION", "Ndian One"))
        # a subzone in use cannot be deleted from the master zone page; the reason is given
        Account.objects.create(kind="CASH_DRAWER", name="Drawer Ndian", zone=s1)
        r = self.c.post(f"/admin/core/masterzone/{m.pk}/change/", {
            "name": "NDIAN DIVISION", "code": "ndian", "active": "on",
            "subzones-TOTAL_FORMS": "1", "subzones-INITIAL_FORMS": "1", "subzones-MIN_NUM_FORMS": "0",
            "subzones-MAX_NUM_FORMS": "1000", "subzones-0-id": s1.pk, "subzones-0-parent": m.pk,
            "subzones-0-name": "Ndian One", "subzones-0-code": "ndian-1", "subzones-0-active": "on",
            "subzones-0-DELETE": "on"})
        self.assertContains(r, "would require deleting the following protected related objects")
        self.assertContains(r, "Drawer Ndian")
        self.assertTrue(Zone.objects.filter(pk=s1.pk).exists())
        self.assertContains(self.c.get(f"/admin/core/masterzone/{m.pk}/delete/"), "Drawer Ndian")  # listed as blocking
        Account.objects.filter(zone=s1).delete()
        # delete the master zone: its unused subzones and microzones go with it
        r = self.c.post(f"/admin/core/masterzone/{m.pk}/delete/", {"post": "yes"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Zone.objects.filter(code__startswith="ndian").exists())
        self.assertFalse(MicroZone.objects.filter(name="Mundemba").exists())
        # delete a single subzone
        m2 = Zone.objects.create(code="m2", name="M2")
        s2 = Zone.objects.create(code="s2", name="S2", parent=m2)
        self.assertEqual(self.c.post(f"/admin/core/subzone/{s2.pk}/delete/", {"post": "yes"}).status_code, 302)
        self.assertTrue(Zone.objects.filter(pk=m2.pk).exists())
        self.assertFalse(Zone.objects.filter(pk=s2.pk).exists())
