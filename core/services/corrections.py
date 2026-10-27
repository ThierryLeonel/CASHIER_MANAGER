"""Corrections (managers / finance managers only).

A correction never edits or deletes posted history. It posts, atomically:
  1. a REVERSAL transaction: the exact negation of every original entry;
  2. for REPLACE, a new transaction of the same type built with the
     corrected values and re-validated with the original cashier's
     permissions and zone (conversions keep the original rate);
and records a Correction row (reason, user, time, field changes) that
stamps the original "CORRECTED".

Closed days are never rewritten: the reversal and replacement are posted
on the CURRENT open business day of the cashier who owned the original
(original_business_date keeps the link). The established closing still
shows what was counted that day; the next closing absorbs the correction
as a normal movement.
"""
from django.db import transaction
from django.utils.translation import gettext as _

from core.models import Account, Correction, TxType

from . import operations as ops
from . import permissions as perm
from .days import current_day, douala_today
from .errors import BusinessError
from .ledger import Ctx, TxSpec, post

VOID_ONLY = (TxType.RESPONSIBILITY_TRANSFER,)
NOT_CORRECTABLE = (TxType.REVERSAL, TxType.RECON_ADJUSTMENT)


def params_from_txn(t):
    T = TxType
    if t.type == T.SUPPLY:
        return dict(product=t.product, customer=t.counterparty, amount=t.amount, source_account=t.source_account,
                    recipient=t.counterparty_2, description=t.description)
    if t.type == T.DESTOCKAGE:
        return dict(pos=t.counterparty_2, agent=t.counterparty, amount=t.amount, drawer=t.source_account,
                    description=t.description)
    if t.type == T.COLLECTION:
        return dict(payer=t.counterparty, method=t.payment_method, amount=t.amount, account=t.dest_account,
                    bank_reference=t.bank_reference, description=t.description)
    if t.type == T.EXPENSE:
        return dict(category=t.expense_category, amount=t.amount, drawer=t.source_account, description=t.description)
    if t.type == T.INTERNAL_TRANSFER:
        return dict(source=t.source_account, destination=t.dest_account, amount=t.amount, description=t.description)
    if t.type == T.CONVERSION:
        return dict(uve_account=t.source_account, amount=t.amount, description=t.description)
    if t.type in (T.CM_GIVE, T.CM_RECEIVE):
        return dict(exchange=t.exchange, side=t.type, amount=t.amount,
                    account=t.source_account if t.type == T.CM_GIVE else t.dest_account, description=t.description)
    if t.type == T.OPENING_BALANCE:
        e = t.entries.exclude(account__kind=Account.Kind.OPENING_EQUITY).get()
        return dict(account=e.account, amount=e.amount, description=t.description)
    return {}


BUILDERS = {
    TxType.SUPPLY: ops.build_supply,
    TxType.DESTOCKAGE: ops.build_destockage,
    TxType.COLLECTION: ops.build_collection,
    TxType.EXPENSE: ops.build_expense,
    TxType.INTERNAL_TRANSFER: ops.build_internal_transfer,
    TxType.CONVERSION: ops.build_conversion,
    TxType.CM_GIVE: ops.build_exchange_leg,
    TxType.CM_RECEIVE: ops.build_exchange_leg,
    TxType.OPENING_BALANCE: ops.build_opening,
}

EDITABLE = {
    TxType.SUPPLY: ["product", "customer", "amount", "source_account", "recipient", "description"],
    TxType.DESTOCKAGE: ["pos", "agent", "amount", "drawer", "description"],
    TxType.COLLECTION: ["payer", "method", "amount", "account", "bank_reference", "description"],
    TxType.EXPENSE: ["category", "amount", "drawer", "description"],
    TxType.INTERNAL_TRANSFER: ["source", "destination", "amount", "description"],
    TxType.CONVERSION: ["uve_account", "amount", "description"],
    TxType.CM_GIVE: ["amount", "account", "description"],
    TxType.CM_RECEIVE: ["amount", "account", "description"],
    TxType.OPENING_BALANCE: ["amount", "description"],
}


def _str(v):
    return str(v) if v is not None else ""


def correct_transaction(user, original, *, kind, reason, new_values=None, idempotency_key=None):
    perm.require_role(user, perm.CORRECTION_ROLES, "correction")
    reason = (reason or "").strip()
    if not reason:
        raise BusinessError(_("A correction reason is required."), "reason")
    new_values = new_values or {}
    with transaction.atomic():
        from core.models import Transaction

        original = Transaction.objects.select_for_update().get(pk=original.pk)
        if original.type in NOT_CORRECTABLE:
            raise BusinessError(_("This type of entry cannot be corrected directly."))
        if Correction.objects.filter(original=original).exists():
            raise BusinessError(_("%(ref)s has already been corrected.") % {"ref": original.reference})
        if kind == Correction.Kind.REPLACE and original.type in VOID_ONLY:
            raise BusinessError(_("A responsibility transfer can only be cancelled, then recorded again."))
        if kind not in Correction.Kind.values:
            raise BusinessError(_("Choose the correction type."), "kind")
        if original.type in ops.TRANSFERABLE_TYPES and ops.active_transfer(original):
            raise BusinessError(_("Cancel the responsibility transfer of %(ref)s before correcting it.")
                                % {"ref": original.reference})

        owner = original.cashier_day.cashier if original.cashier_day_id else None
        if owner is not None:
            day = current_day(owner)
            ctx = Ctx(entered_by=user, cashier=owner, day=day, business_date=day.business_date,
                      exclude_txn_id=original.pk, rate_override=original.rate_used)
        else:
            ctx = Ctx(entered_by=user, cashier=original.entered_by if original.entered_by.is_cashier else None,
                      business_date=douala_today(), exclude_txn_id=original.pk, rate_override=original.rate_used)

        reversal = TxSpec(
            type=TxType.REVERSAL,
            lines=[(e.account, -e.amount) for e in original.entries.select_related("account")],
            fields=dict(amount=original.amount, reverses=original, product=original.product,
                        payment_method=original.payment_method, counterparty=original.counterparty,
                        counterparty_2=original.counterparty_2, source_account=original.source_account,
                        dest_account=original.dest_account, cash_manager=original.cash_manager,
                        exchange=original.exchange, expense_category=original.expense_category,
                        original_business_date=original.business_date,
                        description=(_("Reversal of %(ref)s: %(reason)s") % {"ref": original.reference,
                                                                            "reason": reason})[:255]),
        )
        specs = [reversal]
        changes = {}
        if kind == Correction.Kind.REPLACE:
            params = params_from_txn(original)
            allowed = EDITABLE[original.type]
            for key, val in new_values.items():
                if key not in allowed:
                    raise BusinessError(f"Field {key} cannot be changed for this type")
                if _str(params.get(key)) != _str(val):
                    changes[key] = [_str(params.get(key)), _str(val)]
                params[key] = val
            if not changes:
                raise BusinessError(_("No value was changed. Use 'Cancel' to reverse without replacement."))
            if original.type != TxType.OPENING_BALANCE and (ctx.cashier is None or not ctx.cashier.is_cashier):
                raise BusinessError(_("The original cashier is unknown; cancel and re-enter instead."))
            replacement = BUILDERS[original.type](ctx, **params)
            replacement.fields["replaces"] = original
            replacement.fields["original_business_date"] = original.business_date
            specs.append(replacement)
        posted = post(specs, ctx, idempotency_key)
        return Correction.objects.create(
            original=original, kind=kind, reason=reason, corrected_by=user,
            reversal_txn=posted[0], replacement_txn=posted[1] if len(posted) > 1 else None, changes=changes,
        )
