"""Super-administrator powers: hard delete, in-place edit, clear data.

Safety rules kept even here:
* a transaction is always deleted/edited as a whole (all its entries), so
  the ledger stays balanced (the deferred balance trigger still runs);
* company cash/wallet/bank balances may not become negative;
* the full before (and after) state is written to the audit log first;
* the database lock is lifted only inside these functions, only for the
  current database transaction (SET LOCAL), and only for SUPERADMIN.
"""
from collections import defaultdict
from contextlib import contextmanager

from django.contrib.auth import authenticate
from django.db import connection, transaction
from django.db.models import Q
from django.utils.translation import gettext as _

from core.models import (
    ItemSale, ServiceCount, ShopDayReview, ShopShift, SimRetrieval, StockMove, StockUnit,
    Account, AuditEvent, CashierDay, CashManager, CashManagerExchange, Closing, ClosingLine, Correction, MicroZone,
    Counterparty, Discrepancy, DiscrepancyNote, Entry, Transaction, TxType, User,
)
from core.money import fmt

from . import corrections as corr
from . import permissions as perm
from .errors import BusinessError
from .ledger import Ctx, balance, lock_accounts


@contextmanager
def admin_override():
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET LOCAL core.admin_override = 'on'")
        try:
            yield
        finally:
            with connection.cursor() as cur:
                cur.execute("SET LOCAL core.admin_override = 'off'")


def snapshot(t):
    return {
        "id": t.id, "reference": t.reference, "receipt_no": t.receipt_no, "type": t.type,
        "business_date": str(t.business_date), "posted_at": t.posted_at.isoformat(), "amount": str(t.amount),
        "entered_by": t.entered_by.username, "cashier": t.cashier.username if t.cashier_id else None,
        "product": t.product, "payment_method": t.payment_method,
        "counterparty": t.counterparty.business_sim if t.counterparty_id else None,
        "counterparty_2": t.counterparty_2.business_sim if t.counterparty_2_id else None,
        "source_account": str(t.source_account) if t.source_account_id else None,
        "dest_account": str(t.dest_account) if t.dest_account_id else None,
        "description": t.description, "bank_reference": t.bank_reference,
        "rate_used": str(t.rate_used) if t.rate_used is not None else None,
        "entries": [{"account": str(e.account), "account_id": e.account_id, "amount": str(e.amount)}
                    for e in t.entries.select_related("account")],
    }


def _check_assets(account_ids):
    for acct in Account.objects.filter(id__in=account_ids, kind__in=Account.ASSET_KINDS):
        b = balance(acct)
        if b < 0:
            raise BusinessError(_("This change would make %(acct)s negative (%(bal)s FCFA). Nothing was changed.")
                                % {"acct": acct, "bal": fmt(b)})


def _sql_delete_transactions(where, params):
    """Raw delete: the rows reference each other (reversal -> original), and
    PostgreSQL checks those foreign keys at commit, so order does not matter."""
    with connection.cursor() as cur:
        cur.execute(f"DELETE FROM core_transaction WHERE {where}", params)


def _deletion_group(t):
    """The transactions that must go together with t."""
    if t.type == TxType.REVERSAL or t.replaces_id:
        raise BusinessError(_("This entry belongs to a correction of %(ref)s. Delete %(ref)s instead: its "
                              "correction entries are removed with it.")
                            % {"ref": (t.reverses or t.replaces).reference})
    group = [t]
    c = Correction.objects.filter(original=t).first()
    if c:
        group.append(c.reversal_txn)
        if c.replacement_txn_id:
            group.append(c.replacement_txn)
    for g in list(group):
        if g.responsibility_transfers.exists():
            refs = ", ".join(g.responsibility_transfers.values_list("reference", flat=True))
            raise BusinessError(_("Delete the responsibility transfer(s) %(refs)s first.") % {"refs": refs})
        if hasattr(g, "resolved_discrepancy"):
            raise BusinessError(_("%(ref)s resolves a closing difference. Reopen or keep it; it cannot be deleted.")
                                % {"ref": g.reference})
    return group, c


def delete_transaction(user, t, *, reason):
    perm.require_role(user, perm.CONFIG_ROLES, "delete")
    if not (reason or "").strip():
        raise BusinessError(_("Give the reason for deleting."), "reason")
    with admin_override():
        t = Transaction.objects.select_for_update().get(pk=t.pk)
        group, corr_row = _deletion_group(t)
        ids = [g.id for g in group]
        acct_ids = list(Entry.objects.filter(transaction_id__in=ids).values_list("account_id", flat=True))
        lock_accounts(acct_ids)
        snaps = [snapshot(g) for g in group]
        AuditEvent.objects.create(user=user, action="transaction_deleted",
                                  detail={"reason": reason.strip(), "transactions": snaps})
        if corr_row:
            Discrepancy.objects.filter(correction=corr_row).update(correction=None)
            corr_row.delete()
        _release_shop_rows(ids)
        Entry.objects.filter(transaction_id__in=ids).delete()
        _sql_delete_transactions("id = ANY(%s)", [ids])
        _check_assets(acct_ids)
    return snaps


def _release_shop_rows(txn_ids):
    """Shop rows hanging on transactions about to be deleted: a deleted sale puts its units back in stock."""
    sales = ItemSale.objects.filter(transaction_id__in=txn_ids)
    StockUnit.objects.filter(sale__in=sales).update(status=StockUnit.Status.IN_STOCK, sale=None)
    # a deleted purchase takes its units out of the stock again (refused when one was already sold)
    bought = StockUnit.objects.filter(moves__transaction_id__in=txn_ids, moves__kind=StockMove.Kind.RECEIVED)
    if bought.exclude(status=StockUnit.Status.IN_STOCK).exists():
        raise BusinessError(_("Some units of this purchase were already sold or removed. Delete those first."))
    bought_ids = list(bought.values_list("id", flat=True))
    StockMove.objects.filter(Q(transaction_id__in=txn_ids) | Q(unit_id__in=bought_ids)).delete()
    StockUnit.objects.filter(id__in=bought_ids).delete()
    sales.delete()


EDITABLE_EXTRA = ["business_date"]


def edit_transaction(user, t, *, new_values, reason):
    """Rewrite a transaction in place (same reference and receipt number)."""
    perm.require_role(user, perm.CONFIG_ROLES, "edit")
    if not (reason or "").strip():
        raise BusinessError(_("Give the reason for the edit."), "reason")
    with admin_override():
        t = Transaction.objects.select_for_update().get(pk=t.pk)
        if t.type not in corr.BUILDERS:
            raise BusinessError(_("This type of entry cannot be edited."))
        if t.type == TxType.REVERSAL or t.replaces_id or Correction.objects.filter(original=t).exists():
            raise BusinessError(_("This entry is part of a correction; delete it and record it again instead."))
        if t.responsibility_transfers.exists():
            raise BusinessError(_("Delete the responsibility transfer of this entry before editing it."))
        before = snapshot(t)
        params = corr.params_from_txn(t)
        new_date = new_values.pop("business_date", None) or t.business_date
        changes = {}
        for k, v in new_values.items():
            if k not in corr.EDITABLE.get(t.type, []):
                raise BusinessError(f"Field {k} cannot be changed for this type")
            if str(params.get(k) if params.get(k) is not None else "") != str(v if v is not None else ""):
                changes[k] = [str(params.get(k)), str(v)]
            params[k] = v
        if new_date != t.business_date:
            changes["business_date"] = [str(t.business_date), str(new_date)]
        if not changes:
            raise BusinessError(_("No value was changed."))
        owner = t.cashier or (t.cashier_day.cashier if t.cashier_day_id else None)
        if owner is not None and not owner.is_cashier:
            owner = None
        ctx = Ctx(entered_by=t.entered_by, cashier=owner, day=t.cashier_day, business_date=new_date,
                  exclude_txn_id=t.pk, rate_override=t.rate_used)
        spec = corr.BUILDERS[t.type](ctx, **params)
        old_ids = list(t.entries.values_list("account_id", flat=True))
        new_ids = [a.id for a, _amt in spec.lines]
        lock_accounts(old_ids + new_ids)
        Entry.objects.filter(transaction=t).delete()
        fields = {k: v for k, v in spec.fields.items() if k not in ("opening_for_account",)}
        fields["business_date"] = new_date
        Transaction.objects.filter(pk=t.pk).update(**fields)
        Entry.objects.bulk_create([Entry(transaction=t, account_id=a.id, amount=amt, business_date=new_date,
                                         posted_at=t.posted_at) for a, amt in spec.lines])
        _check_assets(old_ids + new_ids)
        t.refresh_from_db()
        AuditEvent.objects.create(user=user, action="transaction_edited",
                                  detail={"reason": reason.strip(), "changes": changes, "before": before,
                                          "after": snapshot(t)})
        return t


CLEAR_SCOPES = ("transactions", "everything", "shops")


def _clear_shop_operations():
    """Shop operations that are not ledger rows: shifts, day sign-offs, services, retrievals, stock and sales."""
    counts = {"shop_shifts": ShopShift.objects.count(), "stock_units": StockUnit.objects.count(),
              "service_counts": ServiceCount.objects.count(), "sim_retrievals": SimRetrieval.objects.count()}
    ShopDayReview.objects.all().delete()
    ShopShift.objects.all().delete()
    ServiceCount.objects.all().delete()
    SimRetrieval.objects.all().delete()
    StockMove.objects.all().delete()
    StockUnit.objects.update(sale=None)
    ItemSale.objects.all().delete()
    StockUnit.objects.all().delete()
    return counts


def clear_data(user, *, scope, password, confirm_text):
    perm.require_role(user, perm.CONFIG_ROLES, "clear")
    if scope not in CLEAR_SCOPES:
        raise BusinessError(_("Choose what to clear."), "scope")
    if (confirm_text or "").strip().upper() not in ("DELETE", "SUPPRIMER"):
        raise BusinessError(_("Type DELETE to confirm."), "confirm_text")
    if authenticate(username=user.username, password=password or "") is None:
        raise BusinessError(_("Your password is incorrect."), "password")
    counts = defaultdict(int)
    if scope == "shops":
        with admin_override():
            counts.update(_clear_shop_operations())
            # every entry with a shop: shop operations, transfers between shops and cashiers, capital on shops
            ids = set(Transaction.objects.filter(shop__isnull=False).values_list("id", flat=True))
            ids |= set(Entry.objects.filter(account__shop__isnull=False).values_list("transaction_id", flat=True))
            corrs = Correction.objects.filter(Q(original_id__in=ids) | Q(reversal_txn_id__in=ids)
                                              | Q(replacement_txn_id__in=ids))
            for c in corrs:  # a correction goes with its original, reversal and replacement
                ids |= {c.original_id, c.reversal_txn_id} | ({c.replacement_txn_id} if c.replacement_txn_id else set())
            ids = list(ids)
            acct_ids = list(Entry.objects.filter(transaction_id__in=ids).values_list("account_id", flat=True)
                            .distinct())
            counts["transactions"] = len(ids)
            Discrepancy.objects.filter(correction__in=corrs).update(correction=None)
            corrs.delete()
            Entry.objects.filter(transaction_id__in=ids).delete()
            _sql_delete_transactions("id = ANY(%s)", [ids])
            _check_assets(acct_ids)
            AuditEvent.objects.create(user=user, action="data_cleared", detail={"scope": scope, **counts})
        return dict(counts)
    with admin_override():
        counts.update(_clear_shop_operations())
        counts["transactions"] = Transaction.objects.count()
        counts["closings"] = Closing.objects.count()
        DiscrepancyNote.objects.all().delete()
        Discrepancy.objects.all().delete()
        ClosingLine.objects.all().delete()
        Closing.objects.all().delete()
        Correction.objects.all().delete()
        Entry.objects.all().delete()
        _sql_delete_transactions("TRUE", [])
        CashManagerExchange.objects.all().delete()
        CashierDay.objects.all().delete()
        with connection.cursor() as cur:
            for seq in ("core_txn_ref_seq", "core_receipt_seq", "core_exchange_ref_seq"):
                cur.execute(f"ALTER SEQUENCE {seq} RESTART WITH 1")
        if scope == "everything":
            counts["customers"] = Counterparty.objects.count()
            counts["cash_managers"] = CashManager.objects.count()
            AuditEvent.objects.filter(user__role=User.Role.AGENT).update(user=None)
            User.objects.filter(role=User.Role.AGENT).delete()
            Account.objects.filter(kind__in=[Account.Kind.CUSTOMER, Account.Kind.CASH_MANAGER]).delete()
            Account.objects.filter(kind=Account.Kind.SHOP_PAYABLE).delete()  # shops' positions with agents
            Counterparty.objects.update(microzone=None)
            MicroZone.objects.all().delete()  # microzones are defined with their agents
            Counterparty.objects.all().delete()
            CashManager.objects.all().delete()
        AuditEvent.objects.create(user=user, action="data_cleared", detail={"scope": scope, **counts})
    return dict(counts)
