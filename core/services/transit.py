"""Transfers between parties (cashier desks, shops, finance) are recorded by each side for its own money only.

The sender's record takes the money out of the sender's account into "transfers in transit"; the receiver's Entry
takes it out of transit into the receiver's account and is linked to the sender's record. Nobody's record changes
anybody else's balance, so the same money is never counted twice; what is left in transit is what was sent and not
yet entered (or entered and not yet recorded by the sender)."""
from django.db.models import Q

from core.models import Account, Transaction, TxType

K = Account.Kind
TRANSFER_TYPES = (TxType.INTERNAL_TRANSFER, TxType.SHOP_TO_DESK, TxType.DESK_TO_SHOP, TxType.SHOP_TO_SHOP,
                  TxType.CAPITAL_MOVE, TxType.FLOAT_EXCHANGE)


def account():
    return Account.system(K.TRANSIT)


def held_by_other_party(acct):
    """An account of a cashier desk or a shop (its holder records its own entries); master SIMs, banks and finance
    accounts are moved directly by finance."""
    if acct is None:
        return False
    return bool(acct.shop_id or acct.custodian_id or (acct.zone_id and acct.zone.parent_id is not None)
                or acct.shared)


def sender_lines(dest, src, amount):
    """Lines of a transfer recorded by the sender: only the sender's account moves."""
    return [(account(), amount), (src, -amount)]


def pending(dest_accounts):
    """Transfers sent to these accounts and not entered yet by the receiver."""
    return (Transaction.objects.filter(in_transit=True, dest_account__in=dest_accounts, correction__isnull=True,
                                       transit_entries__isnull=True)
            .exclude(type=TxType.REVERSAL)
            .select_related("source_account", "dest_account", "shop", "entered_by", "zone")
            .order_by("posted_at"))


def find_match(dest, amount, *, source_account=None, shop=None, match=None):
    """The sender's record an entry answers: the one chosen, else the oldest pending one from the same sender, for
    the same account and amount."""
    qs = pending([dest]) if dest is not None else Transaction.objects.none()
    if match is not None:
        return qs.filter(pk=match.pk).first()
    qs = qs.filter(amount=amount)
    if source_account is not None:
        qs = qs.filter(Q(source_account=source_account) | Q(source_account__zone=source_account.zone,
                                                              source_account__kind=source_account.kind))
    if shop is not None:
        qs = qs.filter(source_account__shop=shop)
    return qs.first()
