"""Company profile: any distribution partner can set its own name, details and logo."""
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image
from pypdf import PdfReader

from core.models import CompanyProfile
from core.services import branding

from .helpers import OrgMixin
from .test_web import PW


def png(w=800, h=400, color=(230, 90, 20, 255)):
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), color).save(buf, format="PNG")
    return SimpleUploadedFile("logo.png", buf.getvalue(), content_type="image/png")


class Branding(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.c = self.client_class()
        self.assertTrue(self.c.login(username="admin", password=PW))

    def save(self, **extra):
        data = {"name": "Acme Distribution SARL", "tagline": "", "address": "Rue 1", "city": "Buea",
                "phone": "+237 600 00 00 00", "email": "info@acme.test", "tax_id": "M0123456789",
                "trade_register": "RC/BUE/2020/B/1", "receipt_footer": "Thank you for your business"}
        data.update(extra)
        return self.c.post("/administration/company/", data)

    def test_no_partner_name_hardcoded(self):
        r = self.c.get("/")
        self.assertNotContains(r, "Orange Distribution Partner")

    def test_edit_profile_and_logo(self):
        r = self.save(logo_file=png())
        self.assertEqual(r.status_code, 302)
        prof = CompanyProfile.objects.get(pk=1)
        self.assertEqual(prof.name, "Acme Distribution SARL")
        self.assertTrue(prof.has_logo)
        img = Image.open(io.BytesIO(bytes(prof.logo)))
        self.assertLessEqual(max(img.size), branding.LOGO_MAX_PX)  # resized
        home = self.c.get("/")
        self.assertContains(home, "Acme Distribution SARL")
        self.assertContains(home, "/branding/logo.png?v=")
        anon = self.client_class()
        self.assertContains(anon.get("/login/"), "Acme Distribution SARL")
        logo = anon.get("/branding/logo.png?v=1")
        self.assertEqual((logo.status_code, logo["Content-Type"]), (200, "image/png"))

    def test_receipt_and_reports_use_profile(self):
        self.save(logo_file=png())
        from core.services import operations as ops

        from .helpers import D

        t = ops.record_supply(self.c_f2, product="UVE", customer=self.a1, amount=D(1000), source_account=self.uve_f2)
        self.assertTrue(t.receipt_no)
        html = self.c.get(f"/transactions/{t.pk}/receipt/")
        for txt in ("Acme Distribution SARL", "NIU M0123456789", "RCCM RC/BUE/2020/B/1", "Thank you for your business"):
            self.assertContains(html, txt)
        pdf = self.c.get(f"/transactions/{t.pk}/receipt.pdf")
        text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
        self.assertIn("Acme Distribution SARL", text)
        self.assertIn("Thank you for your business", text)
        rep = self.c.get("/reports/supplies/?preset=all&format=pdf")
        self.assertIn("Acme Distribution SARL", PdfReader(io.BytesIO(rep.content)).pages[0].extract_text())
        from openpyxl import load_workbook

        x = self.c.get("/reports/supplies/?preset=all&format=xlsx")
        self.assertTrue(load_workbook(io.BytesIO(x.content)).active["A1"].value.startswith("Acme Distribution SARL"))

    def test_remove_logo_and_reject_non_images(self):
        self.save(logo_file=png())
        r = self.save(logo_file=SimpleUploadedFile("x.png", b"not an image", content_type="image/png"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(CompanyProfile.objects.get(pk=1).has_logo)  # unchanged
        self.save(remove_logo="on")
        self.assertFalse(CompanyProfile.objects.get(pk=1).has_logo)
        self.assertEqual(self.c.get("/branding/logo.png").status_code, 404)
        self.assertContains(self.c.get("/"), '<span class="logo">A</span>')  # first letter

    def test_only_superadmin_edits(self):
        m = self.client_class()
        m.login(username="manager", password=PW)
        self.assertEqual(m.get("/administration/company/").status_code, 403)
