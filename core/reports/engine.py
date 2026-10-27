"""Report engine: one query function per report returns a Report made of
Tables; the same Report is rendered on screen, to .xlsx and to PDF, so
exports always carry the same filters and permission scoping."""
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django import forms
from django.db.models import Q
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from core.models import Counterparty, PaymentMethod, TxType, User, Zone
from core.services.days import douala_today
from core.simnumbers import SimNumberError, normalize_sim


@dataclass
class Col:
    key: str
    label: str
    kind: str = "text"  # text | money | date | datetime | int | rate
    total: bool = False


@dataclass
class Table:
    title: str
    columns: list
    rows: list = field(default_factory=list)
    note: str = ""

    def totals(self):
        if not any(c.total for c in self.columns):
            return None
        out = {}
        for c in self.columns:
            if c.total:
                out[c.key] = sum((r.get(c.key) or Decimal(0)) for r in self.rows)
        return out


@dataclass
class Report:
    key: str
    title: str
    filters_text: str
    tables: list = field(default_factory=list)
    summary: list = field(default_factory=list)  # [(label, value, kind)]


PRESETS = [("day", _l("Daily")), ("week", _l("Weekly")), ("month", _l("Monthly")), ("custom", _l("Custom dates")),
           ("all", _l("Overall (since launch)"))]


class FilterForm(forms.Form):
    preset = forms.ChoiceField(label=_l("Period"), choices=PRESETS, required=False, initial="day")
    date_from = forms.DateField(label=_l("From (business date)"), required=False,
                                widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(label=_l("To (business date)"), required=False,
                              widget=forms.DateInput(attrs={"type": "date"}))
    master_zone = forms.ModelChoiceField(label=_l("Master zone"), required=False,
                                         queryset=Zone.objects.filter(parent__isnull=True))
    subzone = forms.ModelChoiceField(label=_l("Subzone"), required=False,
                                     queryset=Zone.objects.filter(parent__isnull=False))
    cashier = forms.ModelChoiceField(label=_l("Cashier"), required=False,
                                     queryset=User.objects.filter(role=User.Role.CASHIER))
    customer_sim = forms.CharField(label=_l("Customer business SIM"), required=False,
                                   widget=forms.TextInput(attrs={"list": "customer-list", "inputmode": "tel"}))
    tx_type = forms.ChoiceField(label=_l("Transaction type"), required=False,
                                choices=[("", "—")] + list(TxType.choices))
    method = forms.ChoiceField(label=_l("Payment method"), required=False,
                               choices=[("", "—")] + list(PaymentMethod.choices))

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if not user.sees_all_zones:
            for f in ("master_zone", "subzone", "cashier"):
                self.fields.pop(f)

    def clean_customer_sim(self):
        v = self.cleaned_data.get("customer_sim")
        if not v:
            return None
        try:
            sim = normalize_sim(v.split("–")[0])
        except SimNumberError as exc:
            raise forms.ValidationError(_(str(exc)))
        cp = Counterparty.objects.filter(business_sim=sim).first()
        if not cp:
            raise forms.ValidationError(_("No agent or POS is registered with business SIM %(sim)s. Register it "
                                          "first.") % {"sim": sim})
        return cp


@dataclass
class Filters:
    date_from: date | None
    date_to: date | None
    master_zone: object = None
    subzone: object = None
    cashier: object = None
    customer: object = None
    tx_type: str = ""
    method: str = ""
    user: object = None
    preset: str = "day"
    cashier_forced: bool = False

    @classmethod
    def from_form(cls, form, user, force_customer=None):
        cd = form.cleaned_data if form.is_valid() else {}
        preset = cd.get("preset") or "day"
        today = douala_today()
        df, dt = cd.get("date_from"), cd.get("date_to")
        if preset == "day":
            df = dt = df or today
        elif preset == "week":
            ref = df or today
            df = ref - timedelta(days=ref.weekday())
            dt = df + timedelta(days=6)
        elif preset == "month":
            ref = df or today
            df = ref.replace(day=1)
            nxt = (df.replace(day=28) + timedelta(days=4)).replace(day=1)
            dt = nxt - timedelta(days=1)
        elif preset == "all":
            df, dt = None, dt or None
        else:
            df = df or today
            dt = dt or df
        f = cls(date_from=df, date_to=dt, master_zone=cd.get("master_zone"), subzone=cd.get("subzone"),
                cashier=cd.get("cashier"), customer=force_customer or cd.get("customer_sim"),
                tx_type=cd.get("tx_type") or "", method=cd.get("method") or "", user=user, preset=preset)
        if user.is_cashier:
            f.cashier, f.master_zone, f.subzone, f.cashier_forced = user, None, None, True
        return f

    def text(self):
        parts = []
        if self.date_from or self.date_to:
            parts.append(_("Business dates %(a)s → %(b)s") % {"a": self.date_from or "…", "b": self.date_to or "…"})
        else:
            parts.append(_("All dates"))
        for label, val in [(_("Master zone"), self.master_zone), (_("Subzone"), self.subzone),
                           (_("Cashier"), self.cashier), (_("Customer"), self.customer)]:
            if val:
                parts.append(f"{label}: {val}")
        if self.tx_type:
            parts.append(f"{_('Type')}: {TxType(self.tx_type).label}")
        if self.method:
            parts.append(f"{_('Method')}: {PaymentMethod(self.method).label}")
        return " · ".join(parts)

    # ---- query helpers
    def date_q(self, prefix="business_date"):
        q = Q()
        if self.date_from:
            q &= Q(**{f"{prefix}__gte": self.date_from})
        if self.date_to:
            q &= Q(**{f"{prefix}__lte": self.date_to})
        return q

    def station_q(self, prefix=""):
        """Owning cashier of a transaction = cashier_day.cashier, or the
        entering user when there is no cashier day (manager postings)."""
        p = prefix
        q = Q()
        if self.cashier:
            q &= Q(**{f"{p}cashier_day__cashier": self.cashier}) | Q(
                **{f"{p}cashier_day__isnull": True, f"{p}entered_by": self.cashier})
        if self.subzone:
            q &= Q(**{f"{p}cashier_day__cashier__subzone": self.subzone})
        if self.master_zone:
            q &= Q(**{f"{p}cashier_day__cashier__subzone__parent": self.master_zone})
        return q

    def txn_q(self, prefix=""):
        q = self.date_q(f"{prefix}business_date") & self.station_q(prefix)
        if self.tx_type:
            q &= Q(**{f"{prefix}type": self.tx_type})
        if self.method:
            q &= Q(**{f"{prefix}payment_method": self.method})
        if self.customer:
            q &= Q(**{f"{prefix}counterparty": self.customer}) | Q(**{f"{prefix}counterparty_2": self.customer})
        return q
