"""Business operations. Each operation has a `build_*` function (validation
+ ledger lines, reused by corrections) and a public `record_*` function
(role check, business day, idempotent posting).

Posting matrix (Dr = +, Cr = -):
  SUPPLY UVE      Dr customer(responsible)   Cr UVE wallet (any permitted Orange SIM)
  SUPPLY ER       Dr customer(responsible)   Cr ER wallet of the cashier's master-zone master SIM
  SUPPLY CASH     Dr customer(responsible)   Cr cash drawer
  DESTOCKAGE      Dr customer(AGENT)         Cr cash drawer           (POS recorded, no POS debt)
  COLLECTION      Dr drawer/UVE/MTN/bank     Cr customer(payer)
  RESP_TRANSFER   Dr customer(new party)     Cr customer(old party)   (no asset movement)
  CM_GIVE         Dr cash-manager position   Cr drawer or UVE wallet
  CM_RECEIVE      Dr drawer or UVE wallet    Cr cash-manager position
  CONVERSION      Dr ER wallet (UVE*(1+r))   Cr UVE wallet (UVE), Cr conversion uplift (UVE*r)
  EXPENSE         Dr expense category        Cr cash drawer
  INT_TRANSFER    Dr destination asset       Cr source asset
  OPENING         Dr/Cr target account       Cr/Dr opening equity
  RECON_ADJ       shortage: Dr recon adj, Cr asset; surplus: Dr asset, Cr recon adj
  REVERSAL        exact negation of the original entries
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils.translation import gettext as _

from core.models import (
    Account, AuditEvent, CashManagerExchange, ConversionRate, Counterparty, PaymentMethod, Product, Transaction, TxType,
)
from core.money import ZERO, MoneyError, conversion_er_amount, fmt, to_money

from . import permissions as perm
from .days import current_day, douala_today
from .errors import BusinessError, DuplicateSubmission, PermissionDenied
from .ledger import Ctx, TxSpec, balance, lock_accounts, next_exchange_reference, post

K = Account.Kind


def _money(amount, field="amount"):
    try:
        return to_money(amount)
    except MoneyError as exc:
        raise BusinessError(_(str(exc)), field) from exc


def _positive(amount):
    amount = _money(amount)
    if amount <= 0:
        raise BusinessError(_("Amount must be greater than zero."), "amount")
    return amount


def _active_party(cp, label):
    if cp is None:
        raise BusinessError(_("%(label)s is required.") % {"label": label})
    if not cp.active:
        raise BusinessError(_("%(cp)s is inactive.") % {"cp": cp})
    return cp


def _require_send(ctx, account):
    if not perm.can_send_from(ctx.cashier, account):
        raise PermissionDenied(_("You are not authorised to send from %(acct)s.") % {"acct": account})


def _require_receive(ctx, account):
    if not perm.can_receive_into(ctx.cashier, account):
        raise PermissionDenied(_("You are not authorised to receive into %(acct)s.") % {"acct": account})


def cashier_ctx(user):
    perm.require_role(user, perm.ENTRY_ROLES)
    day = current_day(user)
    return Ctx(entered_by=user, cashier=user, day=day, business_date=day.business_date)


# ---------------------------------------------------------------- supplies
PRODUCT_ACCOUNT_KIND = {Product.UVE: K.UVE, Product.ER: K.ER, Product.CASH: K.CASH_DRAWER}


def build_supply(ctx, *, product, customer, amount, source_account, recipient=None, description=""):
    amount = _positive(amount)
    _active_party(customer, _("Responsible customer"))
    if product not in PRODUCT_ACCOUNT_KIND:
        raise BusinessError(_("Choose UVE, ER or cash."), "product")
    if recipient is not None and recipient.pk == customer.pk:
        recipient = None
    if recipient is not None:
        _active_party(recipient, _("Recipient POS"))
        if recipient.kind != Counterparty.Kind.POS or customer.kind != Counterparty.Kind.AGENT:
            raise BusinessError(_("A different responsible party is only allowed when a POS receives the supply "
                                  "and it is explicitly assigned to a commercial agent."))
    if source_account.kind != PRODUCT_ACCOUNT_KIND[product]:
        raise BusinessError(_("The source account does not hold %(p)s.") % {"p": product}, "source_account")
    if product == Product.ER:
        sim = source_account.sim
        mz = ctx.cashier.master_zone
        if not (sim.is_master and mz is not None and sim.master_zone_id == mz.id):
            raise BusinessError(
                _("ER supplies must be sent from the master SIM of your master zone (%(zone)s).")
                % {"zone": mz}, "source_account")
    _require_send(ctx, source_account)
    return TxSpec(
        type=TxType.SUPPLY,
        lines=[(customer.account, amount), (source_account, -amount)],
        fields=dict(amount=amount, product=product, counterparty=customer, counterparty_2=recipient,
                    source_account=source_account, description=description or ""),
        receipt=True,
    )


def record_supply(user, idempotency_key=None, **params):
    ctx = cashier_ctx(user)
    return post([build_supply(ctx, **params)], ctx, idempotency_key)[0]


def build_destockage(ctx, *, pos, agent, amount, drawer, description=""):
    amount = _positive(amount)
    _active_party(pos, _("POS"))
    _active_party(agent, _("Commercial agent"))
    if pos.kind != Counterparty.Kind.POS:
        raise BusinessError(_("The cash recipient of a destockage must be a POS."), "pos")
    if agent.kind != Counterparty.Kind.AGENT:
        raise BusinessError(_("The responsible party of a destockage must be a commercial agent."), "agent")
    if drawer.kind != K.CASH_DRAWER:
        raise BusinessError(_("Destockage is paid from a cash drawer."), "drawer")
    _require_send(ctx, drawer)
    return TxSpec(
        type=TxType.DESTOCKAGE,
        lines=[(agent.account, amount), (drawer, -amount)],
        fields=dict(amount=amount, product=Product.CASH, counterparty=agent, counterparty_2=pos,
                    source_account=drawer, description=description or ""),
        receipt=True,
    )


def record_destockage(user, idempotency_key=None, **params):
    ctx = cashier_ctx(user)
    return post([build_destockage(ctx, **params)], ctx, idempotency_key)[0]


# ---------------------------------------------------------------- collections
METHOD_ACCOUNT_KIND = {PaymentMethod.CASH: K.CASH_DRAWER, PaymentMethod.UVE: K.UVE,
                       PaymentMethod.MTN: K.MTN, PaymentMethod.BANK: K.BANK}


def build_collection(ctx, *, payer, method, amount, account, bank_reference="", description=""):
    amount = _positive(amount)
    _active_party(payer, _("Paying customer"))
    if method not in METHOD_ACCOUNT_KIND:
        raise BusinessError(_("Choose a payment method."), "method")
    if account.kind != METHOD_ACCOUNT_KIND[method]:
        raise BusinessError(_("The receiving account does not match the payment method."), "account")
    bank_reference = (bank_reference or "").strip()
    if method == PaymentMethod.BANK:
        if not bank_reference:
            raise BusinessError(_("Enter the bank deposit reference."), "bank_reference")
        dup = Transaction.objects.filter(type=TxType.COLLECTION, payment_method=PaymentMethod.BANK,
                                         dest_account=account, bank_reference__iexact=bank_reference,
                                         correction__isnull=True)
        if ctx.exclude_txn_id:
            dup = dup.exclude(pk=ctx.exclude_txn_id)
        if dup.exists():
            raise BusinessError(_("Bank reference %(ref)s is already recorded for this bank account.")
                                % {"ref": bank_reference}, "bank_reference")
    else:
        bank_reference = ""
    _require_receive(ctx, account)
    return TxSpec(
        type=TxType.COLLECTION,
        lines=[(account, amount), (payer.account, -amount)],
        fields=dict(amount=amount, payment_method=method, counterparty=payer, dest_account=account,
                    bank_reference=bank_reference, description=description or ""),
        receipt=True,
    )


def record_collection(user, idempotency_key=None, **params):
    ctx = cashier_ctx(user)
    return post([build_collection(ctx, **params)], ctx, idempotency_key)[0]


# ---------------------------------------------------------------- expenses
def build_expense(ctx, *, category, amount, drawer, description):
    amount = _positive(amount)
    if not category or not category.active:
        raise BusinessError(_("Choose an active expense category."), "category")
    if not (description or "").strip():
        raise BusinessError(_("Describe the expense."), "description")
    if drawer.kind != K.CASH_DRAWER:
        raise BusinessError(_("Expenses are paid from a cash drawer."), "drawer")
    _require_send(ctx, drawer)
    return TxSpec(
        type=TxType.EXPENSE,
        lines=[(category.ledger_account, amount), (drawer, -amount)],
        fields=dict(amount=amount, expense_category=category, source_account=drawer, description=description.strip()),
    )


def record_expense(user, idempotency_key=None, **params):
    ctx = cashier_ctx(user)
    return post([build_expense(ctx, **params)], ctx, idempotency_key)[0]


# ---------------------------------------------------------------- internal transfers
def _compatible(a, b):
    if a.kind == b.kind:
        return True
    return {a.kind, b.kind} == {K.CASH_DRAWER, K.BANK}


def build_internal_transfer(ctx, *, source, destination, amount, description=""):
    amount = _positive(amount)
    if source.pk == destination.pk:
        raise BusinessError(_("Source and destination must be different."), "destination")
    if not (source.is_asset and destination.is_asset and destination.active):
        raise BusinessError(_("Transfers are only between active company cash, wallet or bank accounts."))
    if not _compatible(source, destination):
        raise BusinessError(_("Incompatible asset types: %(a)s cannot be transferred to %(b)s.")
                            % {"a": source.get_kind_display(), "b": destination.get_kind_display()}, "destination")
    if source.kind == K.ER and source.sim.master_zone_id != destination.sim.master_zone_id:
        raise BusinessError(_("ER can only be transferred between SIMs of the same master zone."), "destination")
    _require_send(ctx, source)
    return TxSpec(
        type=TxType.INTERNAL_TRANSFER,
        lines=[(destination, amount), (source, -amount)],
        fields=dict(amount=amount, source_account=source, dest_account=destination, description=description or ""),
    )


def record_internal_transfer(user, idempotency_key=None, **params):
    ctx = cashier_ctx(user)
    return post([build_internal_transfer(ctx, **params)], ctx, idempotency_key)[0]


# ---------------------------------------------------------------- conversion
def build_conversion(ctx, *, uve_account, amount, description=""):
    amount = _positive(amount)
    if uve_account.kind != K.UVE:
        raise BusinessError(_("Choose a UVE wallet to convert from."), "uve_account")
    er_account = Account.objects.get(kind=K.ER, sim=uve_account.sim)  # same Orange SIM (decision #5)
    _require_send(ctx, uve_account)
    if ctx.rate_override is not None:
        rate = ctx.rate_override
    else:
        current = ConversionRate.current()
        if current is None:
            raise BusinessError(_("No UVE→ER conversion rate is configured. Ask the super administrator."))
        rate = current.uplift_rate
    er_amount = conversion_er_amount(amount, rate)
    uplift = er_amount - amount
    lines = [(uve_account, -amount), (er_account, er_amount)]
    if uplift:
        lines.append((Account.system(K.CONVERSION_UPLIFT), -uplift))
    return TxSpec(
        type=TxType.CONVERSION, lines=lines,
        fields=dict(amount=amount, rate_used=rate, amount_received=er_amount, source_account=uve_account,
                    dest_account=er_account, description=description or ""),
    )


def record_conversion(user, idempotency_key=None, **params):
    ctx = cashier_ctx(user)
    return post([build_conversion(ctx, **params)], ctx, idempotency_key)[0]


# ---------------------------------------------------------------- cash managers
def create_exchange(user, *, cash_manager, direction, amount, note="", idempotency_key=None):
    ctx = cashier_ctx(user)
    amount = _positive(amount)
    if not cash_manager.active:
        raise BusinessError(_("%(cp)s is inactive.") % {"cp": cash_manager})
    if direction not in CashManagerExchange.Direction.values:
        raise BusinessError(_("Choose the exchange direction."), "direction")
    if idempotency_key:
        existing = CashManagerExchange.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            raise DuplicateSubmission(existing)
    return CashManagerExchange.objects.create(
        reference=next_exchange_reference(), cash_manager=cash_manager, direction=direction, amount=amount,
        created_by=user, business_date=ctx.business_date, note=note, idempotency_key=idempotency_key,
    )


def build_exchange_leg(ctx, *, exchange, side, amount, account, description=""):
    amount = _positive(amount)
    exchange = CashManagerExchange.objects.select_for_update().get(pk=exchange.pk)
    if side not in (TxType.CM_GIVE, TxType.CM_RECEIVE):
        raise BusinessError(_("Choose which side of the exchange happened."), "side")
    want_kind = exchange.give_kind() if side == TxType.CM_GIVE else exchange.receive_kind()
    if account.kind != want_kind:
        raise BusinessError(_("This side of the exchange must use a %(kind)s account.")
                            % {"kind": Account.Kind(want_kind).label}, "account")
    legs = exchange.legs.filter(type=side, correction__isnull=True)
    if ctx.exclude_txn_id:
        legs = legs.exclude(pk=ctx.exclude_txn_id)
    done = legs.aggregate(s=Sum("amount"))["s"] or ZERO
    remaining = exchange.amount - done
    if amount > remaining:
        raise BusinessError(_("Only %(rem)s FCFA remain to be delivered on this side of exchange %(ref)s.")
                            % {"rem": fmt(remaining), "ref": exchange.reference}, "amount")
    cm_acct = exchange.cash_manager.account
    if side == TxType.CM_GIVE:
        _require_send(ctx, account)
        lines = [(cm_acct, amount), (account, -amount)]
        fields = dict(source_account=account)
    else:
        _require_receive(ctx, account)
        lines = [(account, amount), (cm_acct, -amount)]
        fields = dict(dest_account=account)
    return TxSpec(type=side, lines=lines,
                  fields=dict(amount=amount, cash_manager=exchange.cash_manager, exchange=exchange,
                              description=description or "", **fields))


def record_exchange_leg(user, idempotency_key=None, **params):
    ctx = cashier_ctx(user)
    with transaction.atomic():
        return post([build_exchange_leg(ctx, **params)], ctx, idempotency_key)[0]


# ---------------------------------------------------------------- opening balances
OPENING_KINDS = Account.ASSET_KINDS + (K.CUSTOMER, K.CASH_MANAGER)


def build_opening(ctx, *, account, amount, description=""):
    """amount is signed: for customers + = owes company, - = supply due to
    customer; for cash managers + = owes company, - = company owes them;
    for company assets it must be >= 0."""
    amount = _money(amount)
    if amount == 0:
        raise BusinessError(_("Opening amount cannot be zero; leave the account uninitialised instead."), "amount")
    if account.kind not in OPENING_KINDS:
        raise BusinessError(_("Opening balances are only for customers, cash managers, drawers, wallets and banks."))
    if account.kind in Account.ASSET_KINDS and amount < 0:
        raise BusinessError(_("A company drawer/wallet/bank cannot open negative."), "amount")
    existing = Transaction.objects.filter(opening_for_account=account)
    if ctx.exclude_txn_id is None and existing.exists():
        raise BusinessError(_("%(acct)s already has an opening balance (%(ref)s). Use a correction to change it.")
                            % {"acct": account, "ref": existing.first().reference})
    equity = Account.system(K.OPENING_EQUITY)
    fields = dict(amount=abs(amount), description=description or "",
                  counterparty=account.counterparty if account.kind == K.CUSTOMER else None,
                  cash_manager=account.cash_manager if account.kind == K.CASH_MANAGER else None,
                  dest_account=account if account.is_asset else None)
    if ctx.exclude_txn_id is None:
        fields["opening_for_account"] = account
    return TxSpec(type=TxType.OPENING_BALANCE, lines=[(account, amount), (equity, -amount)], fields=fields)


def record_opening_balance(user, *, account, amount, business_date=None, description="", idempotency_key=None):
    perm.require_role(user, perm.OPENING_ROLES)
    ctx = Ctx(entered_by=user, cashier=None, day=None, business_date=business_date or douala_today())
    with transaction.atomic():
        lock_accounts([account.id])  # serialise duplicate initialisation attempts
        spec = build_opening(ctx, account=account, amount=amount, description=description)
        return post([spec], ctx, idempotency_key)[0]


# ---------------------------------------------------------------- responsibility transfers
TRANSFERABLE_TYPES = (TxType.SUPPLY, TxType.DESTOCKAGE, TxType.OPENING_BALANCE)


def responsible_party(txn):
    if txn.type == TxType.OPENING_BALANCE:
        return txn.opening_for_account.counterparty if txn.opening_for_account_id else txn.counterparty
    return txn.counterparty


def active_transfer(txn):
    return txn.responsibility_transfers.filter(type=TxType.RESPONSIBILITY_TRANSFER, correction__isnull=True).first()


def current_responsible(txn):
    t = active_transfer(txn)
    return t.counterparty if t else responsible_party(txn)


def transfer_responsibility(user, *, supply, new_party, reason, idempotency_key=None):
    perm.require_role(user, perm.TRANSFER_RESPONSIBILITY_ROLES)
    if not (reason or "").strip():
        raise BusinessError(_("Give the reason for the responsibility change."), "reason")
    with transaction.atomic():
        supply = Transaction.objects.select_for_update().get(pk=supply.pk)
        if supply.type not in TRANSFERABLE_TYPES:
            raise BusinessError(_("Only supplies, destockages and opening debts can change responsibility."))
        if supply.is_corrected:
            raise BusinessError(_("This transaction has been corrected; transfer its replacement instead."))
        old_party = responsible_party(supply)
        if old_party is None:
            raise BusinessError(_("This transaction has no customer obligation."))
        if supply.type == TxType.OPENING_BALANCE and supply.entries.filter(account=old_party.account,
                                                                           amount__lt=0).exists():
            raise BusinessError(_("Only an opening amount owed by the customer can be transferred."))
        if active_transfer(supply):
            raise BusinessError(_("Responsibility for %(ref)s has already been transferred. Cancel that transfer "
                                  "first if it was wrong.") % {"ref": supply.reference})
        _active_party(new_party, _("New responsible party"))
        if new_party.pk == old_party.pk:
            raise BusinessError(_("The new responsible party must be different."), "new_party")
        if user.is_cashier:
            day = current_day(user)
            ctx = Ctx(entered_by=user, cashier=user, day=day, business_date=day.business_date)
        else:
            ctx = Ctx(entered_by=user, business_date=douala_today())
        old_acct = old_party.account
        lock_accounts([old_acct.id, new_party.account.id])
        outstanding = balance(old_acct)
        if outstanding < supply.amount:
            # Decision #1 (default): never move debt that has already been settled.
            raise BusinessError(
                _("%(party)s currently owes %(owed)s FCFA, less than the %(amt)s FCFA of %(ref)s. Part of it is "
                  "already settled, so the full supply cannot be transferred without moving settled debt. "
                  "A manager must decide how to handle this case.")
                % {"party": old_party, "owed": fmt(outstanding), "amt": fmt(supply.amount), "ref": supply.reference})
        spec = TxSpec(
            type=TxType.RESPONSIBILITY_TRANSFER,
            lines=[(new_party.account, supply.amount), (old_acct, -supply.amount)],
            fields=dict(amount=supply.amount, counterparty=new_party, counterparty_2=old_party,
                        related_supply=supply, product=supply.product, description=reason.strip()),
        )
        return post([spec], ctx, idempotency_key)[0]


def log_event(user, action, **detail):
    AuditEvent.objects.create(user=user if user and user.is_authenticated else None, action=action, detail=detail)


def set_conversion_rate(user, *, percent, note=""):
    perm.require_role(user, perm.CONFIG_ROLES)
    try:
        pct = Decimal(str(percent).replace(",", "."))
    except Exception:
        raise BusinessError(_("Enter the uplift as a percentage, e.g. 4 or 4.5."), "percent")
    if pct < 0 or pct >= 100:
        raise BusinessError(_("The uplift must be between 0 and 100%."), "percent")
    rate = (pct / 100).quantize(Decimal("0.000001"))
    old = ConversionRate.current()
    new = ConversionRate.objects.create(uplift_rate=rate, created_by=user, note=note)
    log_event(user, "conversion_rate_changed", old=str(old.uplift_rate) if old else None, new=str(rate))
    return new


def customer_balance(cp):
    return balance(cp.account)

