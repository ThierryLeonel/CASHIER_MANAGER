"""Configuration screens for the super administrator (Django admin).

Only users with role SUPERADMIN can open /admin/. Ledger tables
(transactions, entries) are not editable here; nothing financial can be
deleted. Every admin change is logged by Django's admin log."""
from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from core.models import (
    Account, AccountAccess, AuditEvent, BankAccount, CashManager, CompanySim, Counterparty, ExpenseCategory, User,
    Zone,
)


def _is_superadmin(request):
    u = request.user
    return u.is_active and u.is_authenticated and u.role == User.Role.SUPERADMIN


admin.site.has_permission = _is_superadmin
admin.site.site_header = "Cashier Manager – configuration"
admin.site.site_title = "Cashier Manager"


class SuperAdminOnly(admin.ModelAdmin):
    def has_module_permission(self, request):
        return _is_superadmin(request)

    def has_view_permission(self, request, obj=None):
        return _is_superadmin(request)

    def has_add_permission(self, request):
        return _is_superadmin(request)

    def has_change_permission(self, request, obj=None):
        return _is_superadmin(request)

    def has_delete_permission(self, request, obj=None):
        # Records still in use by the ledger are protected by the database
        # (Django shows which rows block the deletion).
        return _is_superadmin(request)


@admin.register(Zone)
class ZoneAdmin(SuperAdminOnly):
    list_display = ("code", "name", "parent", "active")


@admin.register(User)
class UserAdmin(BaseUserAdmin, SuperAdminOnly):
    list_display = ("username", "first_name", "last_name", "role", "subzone", "counterparty", "is_active")
    list_filter = ("role", "is_active", "subzone")
    fieldsets = BaseUserAdmin.fieldsets[:2] + (
        ("Role", {"fields": ("role", "subzone", "counterparty", "is_active", "is_staff")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (("Role", {"fields": ("role", "subzone", "counterparty",
                                                                        "first_name", "last_name")}),)
    autocomplete_fields = ("counterparty",)

    def save_model(self, request, obj, form, change):
        obj.is_superuser = obj.role == User.Role.SUPERADMIN
        obj.is_staff = obj.role == User.Role.SUPERADMIN
        super().save_model(request, obj, form, change)


@admin.register(CompanySim)
class CompanySimAdmin(SuperAdminOnly):
    list_display = ("label", "network", "msisdn", "master_zone", "subzone", "is_master", "custodian", "active")
    list_filter = ("network", "master_zone", "is_master")

    def get_readonly_fields(self, request, obj=None):
        return ("network", "msisdn") if obj else ()


class AccountForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = ["kind", "name", "zone", "custodian", "active"]

    def clean(self):
        cd = super().clean()
        if cd.get("kind") == Account.Kind.CASH_DRAWER and not cd.get("zone"):
            raise forms.ValidationError("A cash drawer belongs to a zone: choose its subzone.")
        return cd

    def clean_kind(self):
        kind = self.cleaned_data["kind"]
        if not self.instance.pk and kind != Account.Kind.CASH_DRAWER:
            raise forms.ValidationError("Only cash drawers are created here. Wallets, customer, bank, cash-manager "
                                        "and expense accounts are created automatically with their SIM/party.")
        return kind


@admin.register(Account)
class AccountAdmin(SuperAdminOnly):
    form = AccountForm
    list_display = ("name", "kind", "zone", "custodian", "sim", "active")
    list_select_related = ("zone", "custodian", "sim")
    list_filter = ("kind", "zone", "active")
    search_fields = ("name", "sim__msisdn", "counterparty__business_sim")

    def get_readonly_fields(self, request, obj=None):
        return ("kind",) if obj else ()


@admin.register(AccountAccess)
class AccountAccessAdmin(SuperAdminOnly):
    list_display = ("user", "account", "granted_by", "granted_at")
    autocomplete_fields = ("account",)

    def has_delete_permission(self, request, obj=None):
        return _is_superadmin(request)  # revoking a grant does not touch money

    def save_model(self, request, obj, form, change):
        obj.granted_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Counterparty)
class CounterpartyAdmin(SuperAdminOnly):
    list_display = ("business_sim", "name", "kind", "master_zone", "active")
    list_filter = ("kind", "active", "master_zone")
    search_fields = ("business_sim", "name")

    def get_readonly_fields(self, request, obj=None):
        return ("kind", "business_sim", "created_by", "created_at") if obj else ("created_by", "created_at")


@admin.register(CashManager)
class CashManagerAdmin(SuperAdminOnly):
    list_display = ("name", "phone", "active")


@admin.register(BankAccount)
class BankAccountAdmin(SuperAdminOnly):
    list_display = ("label", "bank_name", "account_number", "active")


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(SuperAdminOnly):
    list_display = ("code", "name_en", "name_fr", "active")


@admin.register(AuditEvent)
class AuditEventAdmin(SuperAdminOnly):
    list_display = ("at", "user", "action", "detail")
    list_filter = ("action",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
