"""Configuration screens for the super administrator (Django admin).

Only users with role SUPERADMIN can open /admin/. Ledger tables
(transactions, entries) are not editable here; nothing financial can be
deleted. Every admin change is logged by Django's admin log."""
from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from core.models import (
    Account, AccountAccess, AuditEvent, BankAccount, CashManager, CompanySim, Counterparty, ExpenseCategory, MasterZone,
    MicroZone, Shop, ShopItem, ShopService, SubZone, User, Zone,
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


class SubZoneInline(admin.TabularInline):
    model = SubZone
    fk_name = "parent"
    fields = ("name", "code", "active")
    extra = 1
    verbose_name = "subzone"
    verbose_name_plural = "subzones of this master zone"

    def has_view_permission(self, request, obj=None):
        return _is_superadmin(request)

    def has_add_permission(self, request, obj=None):
        return _is_superadmin(request)

    def has_change_permission(self, request, obj=None):
        return _is_superadmin(request)

    def has_delete_permission(self, request, obj=None):
        return _is_superadmin(request)  # Django refuses deleting a subzone still in use and names what uses it


@admin.register(MasterZone)
class MasterZoneAdmin(SuperAdminOnly):
    fields = ("name", "code", "active")
    list_display = ("name", "code", "subzone_list", "active")
    search_fields = ("name", "code")
    ordering = ("name",)
    inlines = [SubZoneInline]

    @admin.display(description="subzones")
    def subzone_list(self, obj):
        return ", ".join(obj.subzones.order_by("name").values_list("name", flat=True)) or "—"

    def has_delete_permission(self, request, obj=None):
        # deleting a master zone also deletes its unused subzones; anything still in use is listed and blocks it
        return _is_superadmin(request)


class SubZoneForm(forms.ModelForm):
    make_master = forms.BooleanField(
        label="Make this zone a master zone", required=False,
        help_text="Tick to turn this subzone into a master zone (leave Master zone empty). Not possible while "
                  "cashiers, drawers, SIMs, microzones or entries use it as a subzone.")

    class Meta:
        model = SubZone
        fields = ["name", "code", "parent", "make_master", "active"]
        labels = {"parent": "Master zone"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["parent"].queryset = Zone.objects.filter(parent__isnull=True).exclude(
            pk=self.instance.pk or 0).order_by("name")
        self.fields["parent"].required = False
        help_ = "The master zone this subzone depends on (e.g. Fako 1 → Fako)."
        inst = self.instance
        if inst.pk and inst.parent_id and inst.parent.parent_id is not None:
            help_ += (f" ⚠ It is currently placed under {inst.parent.name}, which is not a master zone: choose a "
                      f"master zone, or make this zone a master zone.")
        if inst.pk and inst.subzones.exists():
            names = ", ".join(inst.subzones.order_by("name").values_list("name", flat=True))
            help_ += f" ⚠ Zones are placed under it ({names}): either make it a master zone, or move them first."
        self.fields["parent"].help_text = help_
        if not inst.pk:
            self.fields.pop("make_master")

    def clean(self):
        cd = super().clean()
        inst = self.instance
        if cd.get("make_master"):
            usage = inst.subzone_usage() if inst.pk else []
            if usage:
                raise forms.ValidationError(
                    f"{inst.name} cannot become a master zone: it is used as a subzone by " + ", ".join(usage)
                    + ". Move them to another subzone first, or keep it as a subzone.")
            cd["parent"] = None
            self.instance.parent = None
        elif not cd.get("parent"):
            self.add_error("parent", "Choose the master zone, or tick 'Make this zone a master zone'.")
        return cd

    def _post_clean(self):
        if self.cleaned_data.get("make_master"):
            self.instance.parent = None
        super()._post_clean()


@admin.register(SubZone)
class SubZoneAdmin(SuperAdminOnly):
    form = SubZoneForm
    list_display = ("name", "code", "parent", "zone_check", "active")
    list_filter = ("parent",)
    search_fields = ("name", "code")
    ordering = ("parent__name", "name")
    actions = ["make_master"]

    def get_fields(self, request, obj=None):
        return ["name", "code", "parent", "make_master", "active"] if obj else ["name", "code", "parent", "active"]

    @admin.display(description="check")
    def zone_check(self, obj):
        problems = []
        if obj.parent_id and obj.parent.parent_id is not None:
            problems.append(f"⚠ placed under {obj.parent.name}, which is not a master zone")
        names = list(obj.subzones.values_list("name", flat=True))
        if names:
            problems.append("⚠ has zones under it: " + ", ".join(names))
        return "; ".join(problems) or "OK"

    def response_change(self, request, obj):
        if obj.parent_id is None:  # just became a master zone
            from django.http import HttpResponseRedirect
            from django.urls import reverse

            self.message_user(request, f"{obj.name} is now a master zone.")
            return HttpResponseRedirect(reverse("admin:core_masterzone_change", args=[obj.pk]))
        return super().response_change(request, obj)

    @admin.action(description="Make the selected zones master zones (only zones saved as subzones by mistake)")
    def make_master(self, request, queryset):
        done = []
        for z in queryset:
            usage = z.subzone_usage()
            if usage:
                self.message_user(request, f"{z.name} was left unchanged: it is used as a subzone by "
                                           + ", ".join(usage) + ".", level="warning")
                continue
            Zone.objects.filter(pk=z.pk).update(parent=None)
            done.append(z.name)
        if done:
            self.message_user(request, "Now master zones: " + ", ".join(done))


@admin.register(MicroZone)
class MicroZoneAdmin(SuperAdminOnly):
    list_display = ("name", "subzone", "agent", "active")
    list_filter = ("subzone",)
    search_fields = ("name", "agent__name", "agent__business_sim")
    autocomplete_fields = ("agent",)

    def has_delete_permission(self, request, obj=None):
        return _is_superadmin(request) and (obj is None or not obj.members.exists())


@admin.register(User)
class UserAdmin(BaseUserAdmin, SuperAdminOnly):
    list_display = ("username", "first_name", "last_name", "role", "subzone", "shop", "counterparty", "is_active")
    list_filter = ("role", "is_active", "subzone")
    fieldsets = BaseUserAdmin.fieldsets[:2] + (
        ("Role", {"fields": ("role", "subzone", "shop", "counterparty", "is_active", "is_staff")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (("Role", {"fields": ("role", "subzone", "shop", "counterparty",
                                                                        "first_name", "last_name")}),)
    autocomplete_fields = ("counterparty",)

    def save_model(self, request, obj, form, change):
        obj.is_superuser = obj.role == User.Role.SUPERADMIN
        obj.is_staff = obj.role == User.Role.SUPERADMIN
        super().save_model(request, obj, form, change)


class CompanySimForm(forms.ModelForm):
    class Meta:
        model = CompanySim
        fields = ["network", "msisdn", "label", "master_zone", "is_master", "subzone", "custodian", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "master_zone" in self.fields:
            self.fields["master_zone"].queryset = Zone.objects.filter(parent__isnull=True)
            self.fields["master_zone"].help_text = (
                "Only master zones are listed. If your zone is missing, open Zones and clear its "
                "'master zone' field (a master zone has none).")
        if "subzone" in self.fields:
            self.fields["subzone"].queryset = Zone.objects.filter(parent__isnull=False).select_related("parent")
            self.fields["subzone"].label_from_instance = lambda z: f"{z.parent.name} › {z.name}"
        if "custodian" in self.fields:
            self.fields["custodian"].queryset = User.objects.filter(role=User.Role.CASHIER, is_active=True)
        if "is_master" in self.fields:
            self.fields["is_master"].help_text = (
                "Tick for the zone's master SIM: it serves all the subzones of the master zone (e.g. Fako 1 and "
                "Fako 2) and is operated by its cashier in charge. Leave the subzone empty.")


@admin.register(CompanySim)
class CompanySimAdmin(SuperAdminOnly):
    form = CompanySimForm
    list_display = ("label", "network", "msisdn", "master_zone", "subzone", "is_master", "custodian", "active")
    list_filter = ("network", "master_zone", "is_master")

    def get_readonly_fields(self, request, obj=None):
        return ("network", "msisdn") if obj else ()


WALLET_NETWORK = {Account.Kind.UVE: CompanySim.Network.ORANGE, Account.Kind.MTN: CompanySim.Network.MTN}


class AccountForm(forms.ModelForm):
    sim_number = forms.CharField(label="SIM number", required=False, max_length=20,
                                 help_text="UVE and MTN float accounts: the number of the SIM that holds the value.")

    class Meta:
        model = Account
        fields = ["kind", "name", "zone", "custodian", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:  # cash drawers, UVE wallets and MTN float wallets are added here
            self.fields["kind"].choices = [(k, Account.Kind(k).label) for k in
                                           (Account.Kind.CASH_DRAWER, Account.Kind.UVE, Account.Kind.MTN)]
            self.fields["kind"].initial = Account.Kind.CASH_DRAWER
            self.fields["kind"].help_text = (
                "Cash drawer, UVE wallet (Orange SIM; its ER wallet is created with it) or MTN float wallet "
                "(MTN SIM). The master SIM (ER) is added in Company sims. Expense accounts come from Expense "
                "categories, banks from Bank accounts.")
            self.fields["zone"].queryset = Zone.objects.filter(parent__isnull=False).select_related("parent")
            self.fields["zone"].label_from_instance = lambda z: f"{z.parent.name} › {z.name}"
            self.fields["zone"].help_text = "The subzone whose cashiers use this account."
            self.fields["zone"].required = True

    def clean_kind(self):
        kind = self.cleaned_data["kind"]
        if not self.instance.pk and kind not in (Account.Kind.CASH_DRAWER, Account.Kind.UVE, Account.Kind.MTN):
            raise forms.ValidationError("Only cash drawers, UVE wallets and MTN float wallets are added here.")
        return kind

    def clean(self):
        from core.simnumbers import SimNumberError, normalize_sim

        cd = super().clean()
        kind = cd.get("kind")
        if not self.instance.pk and not cd.get("zone"):
            self.add_error("zone", "Choose the subzone of this account.")
        if not self.instance.pk and kind in WALLET_NETWORK:
            try:
                cd["sim_number"] = normalize_sim(cd.get("sim_number") or "")
            except SimNumberError as e:
                self.add_error("sim_number", str(e))
            else:
                if CompanySim.objects.filter(msisdn=cd["sim_number"]).exists():
                    self.add_error("sim_number", "This SIM number is already registered (see Company sims).")
        return cd


@admin.register(Account)
class AccountAdmin(SuperAdminOnly):
    form = AccountForm
    list_display = ("name", "kind", "zone", "custodian", "sim", "active")
    list_select_related = ("zone", "custodian", "sim")
    list_filter = ("kind", "zone", "active")
    search_fields = ("name", "sim__msisdn", "counterparty__business_sim")

    def get_readonly_fields(self, request, obj=None):
        return ("kind",) if obj else ()

    def get_fields(self, request, obj=None):
        # "cashier in charge" only applies to master-level accounts (the master SIM)
        return ["kind", "name", "zone", "custodian", "active"] if obj else ["kind", "name", "zone", "sim_number",
                                                                           "active"]

    def save_model(self, request, obj, form, change):
        if change or obj.kind not in WALLET_NETWORK:
            return super().save_model(request, obj, form, change)
        # a wallet is created through its SIM, which keeps the SIM/zone rules in one place
        zone = form.cleaned_data["zone"]
        sim = CompanySim.objects.create(network=WALLET_NETWORK[obj.kind], msisdn=form.cleaned_data["sim_number"],
                                        label=obj.name, master_zone=zone.master, subzone=zone, active=obj.active)
        acct = Account.objects.get(sim=sim, kind=obj.kind)
        obj.pk = obj.id = acct.pk
        obj.name, obj.sim, obj._state.adding = acct.name, sim, False


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
    list_display = ("business_sim", "name", "kind", "microzone", "master_zone", "active")
    list_filter = ("kind", "active", "master_zone", "microzone")
    search_fields = ("business_sim", "name")

    def get_readonly_fields(self, request, obj=None):
        return ("kind", "business_sim", "master_zone", "created_by", "created_at") if obj \
            else ("master_zone", "created_by", "created_at")


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


@admin.register(Shop)
class ShopAdmin(SuperAdminOnly):
    list_display = ("name", "code", "zone", "sim_number", "managers_list", "active")
    search_fields = ("name", "code", "sim_number")
    fields = ("name", "code", "zone", "sim_number", "address", "active")

    @admin.display(description="shop managers")
    def managers_list(self, obj):
        return ", ".join(str(u) for u in obj.managers.all()) or "—"


@admin.register(ShopService)
class ShopServiceAdmin(SuperAdminOnly):
    list_display = ("name", "kind", "sort", "active")
    list_editable = ("sort", "active")


@admin.register(ShopItem)
class ShopItemAdmin(SuperAdminOnly):
    list_display = ("name", "category", "price", "active")
    list_filter = ("category", "active")
