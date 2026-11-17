"""Several records in one form: supplies to several commercial agents, capital to several agents. Rows are plain forms
prefixed r0 … rN-1 (rendered all at once, the empty ones folded away); an empty row is skipped."""
import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from core.forms import BaseEntryForm, CustomerField, MoneyField, K
from core.models import Counterparty, Product

ROWS = 20


def row_forms(row_class, data, n=ROWS):
    return [row_class(data, prefix=f"r{i}") for i in range(n)]


def shown_rows(rows, minimum=3):
    """How many rows to show: the filled ones (after a refused post), and at least `minimum`."""
    last = max((i for i, r in enumerate(rows) if r.is_bound and not r.blank()), default=-1)
    return max(minimum, last + 1)


class _Row(forms.Form):
    def blank(self):
        return not any((self.data.get(self.add_prefix(n)) or "").strip() for n in self.fields)


class SupplyRowForm(_Row):
    agent = CustomerField(label=_("Commercial agent"), kind=Counterparty.Kind.AGENT, required=False, help_text="")
    cash = MoneyField(label=_("Cash"), required=False)
    uve = MoneyField(label=_("UVE"), required=False)
    er = MoneyField(label=_("ER"), required=False)

    PRODUCT_FIELDS = (("cash", Product.CASH), ("uve", Product.UVE), ("er", Product.ER))


class MultiSupplyForm(BaseEntryForm):
    """The common part: the same amounts for every agent (a row's own amounts replace them), where the stock
    leaves from, and a note."""

    cash = MoneyField(label=_("Cash for each agent"), required=False)
    uve = MoneyField(label=_("UVE for each agent"), required=False)
    er = MoneyField(label=_("ER for each agent"), required=False)
    description = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, user, *args, **kwargs):
        super().__init__(user, *args, **kwargs)
        self.fields["cash"].help_text = _("Same amount for every agent below. Fill a row's own amounts to give that "
                                          "agent something else.")
        self.account_choice("uve_from", K.UVE, ("BOTH", "SUPPLY"), _("UVE leaves from"))
        self.account_choice("cash_from", K.CASH_DRAWER, ("BOTH", "SUPPLY"), _("Cash leaves from"))

    def supply_accounts(self):
        cd = self.cleaned_data
        return {p: cd[n] for n, p in (("uve_from", Product.UVE), ("cash_from", Product.CASH)) if cd.get(n)}

    def items(self, rows):
        """[(agent, parts)] from the filled rows; errors are put on the rows (returns None when any)."""
        common = {p: self.cleaned_data.get(n) for n, p in SupplyRowForm.PRODUCT_FIELDS if self.cleaned_data.get(n)}
        out, bad = [], False
        for r in rows:
            if r.blank():
                continue
            own = {p: r.cleaned_data.get(n) for n, p in SupplyRowForm.PRODUCT_FIELDS if r.cleaned_data.get(n)}
            parts = own or common
            if not r.cleaned_data.get("agent"):
                r.add_error("agent", _("Choose the commercial agent."))
                bad = True
            elif not parts:
                r.add_error("cash", _("Enter the amount for this agent, or the same amount for every agent above."))
                bad = True
            else:
                out.append((r.cleaned_data["agent"], parts))
        if not out and not bad:
            self.add_error(None, _("Add at least one commercial agent."))
            bad = True
        return None if bad else out


class CapitalRowForm(_Row):
    agent = CustomerField(label=_("Commercial agent"), kind=Counterparty.Kind.AGENT, required=False, help_text="")
    amount = MoneyField(label=_("Amount (FCFA)"), required=False)


class CapitalBatchForm(forms.Form):
    idem = forms.UUIDField(widget=forms.HiddenInput)
    movement = forms.ChoiceField(label=_("Movement"), widget=forms.RadioSelect, initial="GIVE",
                                 choices=[("GIVE", _("Capital given to the agent")),
                                          ("RETURN", _("Capital paid back by the agent"))])
    amount = MoneyField(label=_("Same amount for every agent (FCFA)"), required=False,
                        help_text=_("Used for each agent below whose own amount is empty."))
    business_date = forms.DateField(label=_("Business date"), required=False,
                                    widget=forms.DateInput(attrs={"type": "date"}))
    note = forms.CharField(label=_("Note"), required=False, max_length=255)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("idem", uuid.uuid4())

    def items(self, rows):
        out, bad = [], False
        for r in rows:
            if r.blank():
                continue
            amt = r.cleaned_data.get("amount") or self.cleaned_data.get("amount")
            if not r.cleaned_data.get("agent"):
                r.add_error("agent", _("Choose the commercial agent."))
                bad = True
            elif not amt:
                r.add_error("amount", _("Enter the amount for this agent, or the same amount for every agent above."))
                bad = True
            else:
                out.append((r.cleaned_data["agent"], amt))
        if not out and not bad:
            self.add_error(None, _("Add at least one commercial agent."))
            bad = True
        return None if bad else out
