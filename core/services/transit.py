"""Transfers between parties (cashier desks, shops, finance) are recorded by each side for its own money only.

The sender's record takes the money out of the sender's account; the receiver's Entry puts it into the receiver's
account. The two records are independent: neither touches the other's balance. The ledger still needs a counterpart
for each one-sided record: the system account "Transfers between parties" (sent minus entered)."""
from core.models import Account

K = Account.Kind


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
