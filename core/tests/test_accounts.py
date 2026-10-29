"""Users & passwords: admin resets, activation, own password change."""
from django.test import TestCase

from core.models import AuditEvent, User

from .helpers import OrgMixin
from .test_web import PW


class AccountsAdmin(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.admin_c = self.client_class()
        self.assertTrue(self.admin_c.login(username="admin", password=PW))
        self.u = User.objects.get(username="cashier.fako2")

    def test_list_and_password_never_shown(self):
        r = self.admin_c.get("/administration/users/")
        self.assertContains(r, "cashier.fako2")
        d = self.admin_c.get(f"/administration/users/{self.u.pk}/")
        self.assertContains(d, "cannot be shown")
        self.assertNotContains(d, self.u.password)  # not even the hash

    def test_generated_reset_shown_once_and_forces_change(self):
        r = self.admin_c.post(f"/administration/users/{self.u.pk}/", {"mode": "generate", "must_change": "on"})
        pw = r.context["new_pw"]
        self.assertEqual(len(pw), 12)
        self.assertContains(r, pw)
        self.assertNotContains(self.admin_c.get(f"/administration/users/{self.u.pk}/"), pw)  # only once
        c = self.client_class()
        self.assertFalse(c.login(username="cashier.fako2", password=PW))  # old password gone
        self.assertTrue(c.login(username="cashier.fako2", password=pw))
        self.assertRedirects(c.get("/"), "/account/password/")  # must choose own password first
        r = c.post("/account/password/", {"old_password": pw, "new_password1": "Mbanga-Bakundu-77",
                                          "new_password2": "Mbanga-Bakundu-77"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertEqual(c.get("/").status_code, 200)  # still signed in, free to work
        self.u.refresh_from_db()
        self.assertFalse(self.u.must_change_password)
        self.assertTrue(AuditEvent.objects.filter(action="password_reset").exists())

    def test_admin_types_password_and_weak_rejected(self):
        r = self.admin_c.post(f"/administration/users/{self.u.pk}/", {"mode": "set", "new_password": "1234"})
        self.assertContains(r, "too short")
        self.admin_c.post(f"/administration/users/{self.u.pk}/", {"mode": "set", "new_password": "Limbe-Down-Beach-9"})
        self.assertTrue(self.client_class().login(username="cashier.fako2", password="Limbe-Down-Beach-9"))

    def test_deactivate_and_activate(self):
        c = self.client_class()
        self.assertTrue(c.login(username="cashier.fako2", password=PW))
        self.admin_c.post(f"/administration/users/{self.u.pk}/toggle/")
        self.u.refresh_from_db()
        self.assertFalse(self.u.is_active)
        self.assertNotEqual(c.get("/").status_code, 200)  # signed out at once
        self.assertFalse(self.client_class().login(username="cashier.fako2", password=PW))
        self.admin_c.post(f"/administration/users/{self.u.pk}/toggle/")
        self.assertTrue(self.client_class().login(username="cashier.fako2", password=PW))
        admin = User.objects.get(username="admin")
        self.admin_c.post(f"/administration/users/{admin.pk}/toggle/")
        admin.refresh_from_db()
        self.assertTrue(admin.is_active)  # cannot deactivate yourself

    def test_only_superadmin_and_everyone_changes_own(self):
        m = self.client_class()
        m.login(username="manager", password=PW)
        self.assertEqual(m.get("/administration/users/").status_code, 403)
        self.assertEqual(m.post(f"/administration/users/{self.u.pk}/toggle/").status_code, 403)
        for name in ("manager", "agent.ndive", "cashier.meme"):
            c = self.client_class()
            c.login(username=name, password=PW)
            self.assertContains(c.get("/"), "/account/password/")
            r = c.post("/account/password/", {"old_password": "wrong", "new_password1": "Tiko-Palms-2026x",
                                              "new_password2": "Tiko-Palms-2026x"})
            self.assertEqual(r.status_code, 200)  # wrong current password refused
