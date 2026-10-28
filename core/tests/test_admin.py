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
