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
import uuid
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


def cashier_ctx(user, acting_for=None):
    """Entries belong to a cashier's desk. The super administrator may record
    on behalf of a chosen cashier: the cashier's desk, accounts and business
    day apply, and the super administrator is kept as `entered_by`."""
    if acting_for is not None and user.is_authenticated and user.is_superadmin:
        if not acting_for.is_cashier or not acting_for.is_active:
            raise BusinessError(_("Choose an active cashier to record for."))
        day = current_day(acting_for)
        return Ctx(entered_by=user, cashier=acting_for, day=day, business_date=day.business_date)
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


def record_supply(user, idempotency_key=None, acting_for=None, **params):
    ctx = cashier_ctx(user, acting_for)
    return post([build_supply(ctx, **params)], ctx, idempotency_key)[0]


SUPPLY_PRODUCTS = (Product.CASH, Product.UVE, Product.ER)


def _supply_account(ctx, product):
    """The account a product leaves from: the subzone's drawer / UVE wallet, or the master SIM for ER."""
    kind = PRODUCT_ACCOUNT_KIND[product]
    qs = perm.usable_accounts(ctx.cashier, [kind])
    if product == Product.ER:
        mz = ctx.cashier.master_zone
        qs = qs.filter(sim__is_master=True, sim__master_zone=mz) if mz is not None else qs.none()
        if not qs.exists():
            raise BusinessError(_("ER is supplied from the master SIM of %(zone)s by its cashier in charge.")
                                % {"zone": mz}, "er")
    if qs.count() != 1:
        raise BusinessError(_("No single %(kind)s account is set up for your subzone.")
                            % {"kind": Account.Kind(kind).label}, product.lower())
    return qs.first()


def record_supplies(user, *, customer, parts, recipient=None, description="", idempotency_key=None,
                    acting_for=None):
    """One supply made of several products at once, e.g. {"CASH": 100000, "UVE": 500000, "ER": 120000}.

    Each product leaves from its account automatically and is posted as its own supply, all together (all or
    nothing), sharing one group and one receipt. Returns the supplies, first one first."""
    ctx = cashier_ctx(user, acting_for)
    todo = []
    for prod in SUPPLY_PRODUCTS:
        amt = _money(parts.get(prod) or 0)
        if amt < 0:
            raise BusinessError(_("Amounts cannot be negative."), prod.lower())
        if amt:
            todo.append((prod, amt))
    if not todo:
        raise BusinessError(_("Enter the amount supplied in cash, UVE and/or ER."), "cash")
    group = uuid.uuid5(idempotency_key, "supply") if idempotency_key else uuid.uuid4()
    specs = []
    for i, (prod, amt) in enumerate(todo):
        spec = build_supply(ctx, product=prod, customer=customer, amount=amt, recipient=recipient,
                            source_account=_supply_account(ctx, prod), description=description)
        spec.fields["payment_group"] = group if len(todo) > 1 else None
        spec.receipt = i == 0
        specs.append(spec)
    with transaction.atomic():
        return post(specs, ctx, idempotency_key)


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


def record_destockage(user, idempotency_key=None, acting_for=None, **params):
    ctx = cashier_ctx(user, acting_for)
    return post([build_destockage(ctx, **params)], ctx, idempotency_key)[0]


# ---------------------------------------------------------------- collections
METHOD_ACCOUNT_KIND = {PaymentMethod.CASH: K.CASH_DRAWER, PaymentMethod.UVE: K.UVE,
                       PaymentMethod.MTN: K.MTN, PaymentMethod.BANK: K.BANK}


def clean_split(split, total, *, allow_negative=False):
    """{"UVE": 1000, "CASH": 0, "ER": 500} -> {"UVE": 1000, "ER": 500} (sum must equal total), or None."""
    if not split:
        return None
    out = {}
    for k, v in split.items():
        if k not in Product.values:
            raise BusinessError(_("Unknown product %(p)s.") % {"p": k})
        if v in (None, ""):
            continue
        v = _money(v)
        if v < 0 and not allow_negative:
            raise BusinessError(_("Product amounts cannot be negative."), "amount")
        if v:
            out[k] = int(v)
    if not out:
        return None
    if sum(out.values()) != total:
        raise BusinessError(_("UVE + Cash + ER (%(sum)s FCFA) must equal the amount (%(amt)s FCFA).")
                            % {"sum": fmt(sum(out.values())), "amt": fmt(total)}, "amount")
    return out


def build_collection(ctx, *, payer, method, amount, account, bank_reference="", description="", split=None):
    amount = _positive(amount)
    split = clean_split(split, amount)
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
                    bank_reference=bank_reference, description=description or "", product_split=split),
        receipt=True,
    )


PAYMENT_METHODS = (PaymentMethod.CASH, PaymentMethod.UVE, PaymentMethod.MTN, PaymentMethod.BANK)


def record_payment(user, *, payer, parts, accounts=None, bank_reference="", description="", idempotency_key=None,
                   acting_for=None):
    """One payment made with one or several methods at once, e.g. {"CASH": 200000, "UVE": 150000, "BANK": 50000}.

    Each method is a collection into the subzone's account for that method; all are posted together (all or
    nothing), share one payment group and one receipt number. Returns the collections, first one first."""
    ctx = cashier_ctx(user, acting_for)
    accounts = accounts or {}
    todo = []
    for m in PAYMENT_METHODS:
        amt = _money(parts.get(m) or 0)
        if amt < 0:
            raise BusinessError(_("Amounts cannot be negative."), m.lower())
        if amt:
            todo.append((m, amt))
    if not todo:
        raise BusinessError(_("Enter the amount received in at least one payment method."), "cash")
    group = uuid.uuid5(idempotency_key, "payment") if idempotency_key else uuid.uuid4()
    specs = []
    for i, (m, amt) in enumerate(todo):
        acct = accounts.get(m) or _desk_account(ctx, METHOD_ACCOUNT_KIND[m], receiving=True)
        if acct is None:
            raise BusinessError(_("No single %(kind)s account is set up for your subzone.")
                                % {"kind": Account.Kind(METHOD_ACCOUNT_KIND[m]).label}, m.lower())
        spec = build_collection(ctx, payer=payer, method=m, amount=amt, account=acct,
                                bank_reference=bank_reference if m == PaymentMethod.BANK else "",
                                description=description)
        spec.fields["payment_group"] = group if len(todo) > 1 else None
        spec.receipt = i == 0  # one receipt for the whole payment
        specs.append(spec)
    with transaction.atomic():
        return post(specs, ctx, idempotency_key)


def record_collection(user, idempotency_key=None, acting_for=None, **params):
    ctx = cashier_ctx(user, acting_for)
    return post([build_collection(ctx, **params)], ctx, idempotency_key)[0]


# ---------------------------------------------------------------- expenses
def build_expense(ctx, *, category, amount, drawer, description):
    amount = _positive(amount)
    if not category or not category.active:
        raise BusinessError(_("Choose an active expense category."), "category")
    if not (description or "").strip():
        raise BusinessError(_("Describe the expense."), "description")
    if drawer.kind not in (K.CASH_DRAWER, K.UVE, K.MTN):  # settled in cash, UVE or MTN float
        raise BusinessError(_("Expenses are paid from a cash drawer, a UVE wallet or an MTN float wallet."), "drawer")
    _require_send(ctx, drawer)
    return TxSpec(
        type=TxType.EXPENSE,
        lines=[(category.ledger_account, amount), (drawer, -amount)],
        fields=dict(amount=amount, expense_category=category, source_account=drawer, description=description.strip()),
    )


def record_expense(user, idempotency_key=None, acting_for=None, **params):
    ctx = cashier_ctx(user, acting_for)
    return post([build_expense(ctx, **params)], ctx, idempotency_key)[0]


# ---------------------------------------------------------------- internal transfers
TRANSFER_KINDS = (K.MTN, K.UVE, K.BANK)  # internal transfers: MTN float, UVE or bank only


def _compatible(a, b):
    return a.kind == b.kind


def build_internal_transfer(ctx, *, source, destination, amount, description=""):
    amount = _positive(amount)
    if source.pk == destination.pk:
        raise BusinessError(_("Source and destination must be different."), "destination")
    if not (source.is_asset and destination.is_asset and destination.active):
        raise BusinessError(_("Transfers are only between active company cash, wallet or bank accounts."))
    if source.kind not in TRANSFER_KINDS or destination.kind not in TRANSFER_KINDS:
        raise BusinessError(_("Internal transfers are only for MTN float, UVE or bank accounts."), "source")
    if not _compatible(source, destination):
        raise BusinessError(_("Incompatible asset types: %(a)s cannot be transferred to %(b)s.")
                            % {"a": source.get_kind_display(), "b": destination.get_kind_display()}, "destination")
    _require_send(ctx, source)
    return TxSpec(
        type=TxType.INTERNAL_TRANSFER,
        lines=[(destination, amount), (source, -amount)],
        fields=dict(amount=amount, source_account=source, dest_account=destination, description=description or ""),
    )


def record_internal_transfer(user, idempotency_key=None, acting_for=None, **params):
    ctx = cashier_ctx(user, acting_for)
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


def record_conversion(user, idempotency_key=None, acting_for=None, **params):
    ctx = cashier_ctx(user, acting_for)
    return post([build_conversion(ctx, **params)], ctx, idempotency_key)[0]


# ---------------------------------------------------------------- cash managers
def create_exchange(user, *, cash_manager, amount, note="", idempotency_key=None, acting_for=None, direction=""):
    ctx = cashier_ctx(user, acting_for)
    amount = _positive(amount)
    if not cash_manager.active:
        raise BusinessError(_("%(cp)s is inactive.") % {"cp": cash_manager})
    if direction and direction not in CashManagerExchange.Direction.values:
        raise BusinessError(_("Choose the exchange direction."), "direction")
    if idempotency_key:
        existing = CashManagerExchange.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            raise DuplicateSubmission(existing)
    return CashManagerExchange.objects.create(
        reference=next_exchange_reference(), cash_manager=cash_manager, direction=direction, amount=amount,
        created_by=user, business_date=ctx.business_date, note=note, idempotency_key=idempotency_key,
    )


def _desk_account(ctx, kind, receiving=False):
    """The single account of this kind the cashier's subzone uses, or None."""
    qs = perm.usable_accounts(ctx.cashier, [kind], receiving=receiving)
    return qs.first() if qs.count() == 1 else None


def record_exchange(user, *, cash_manager, amount, give_kind, give_account=None, amount_given=None,
                    receive_account=None, amount_served=ZERO, served_on=None, external_ref="", note="",
                    idempotency_key=None, acting_for=None):
    """One step: the agreement, what the company gave and what it was served now. The rest is served later.

    Giving cash or MTN float means being served UVE; giving UVE means being served cash."""
    direction = CashManagerExchange.GIVE_DIRECTION.get(give_kind)
    if direction is None:
        raise BusinessError(_("Choose what the company gives: cash, MTN float or UVE."), "give_kind")
    gk, rk = CashManagerExchange.SIDES[direction]
    ctx0 = cashier_ctx(user, acting_for)
    give_account = give_account or _desk_account(ctx0, gk)
    receive_account = receive_account or _desk_account(ctx0, rk, receiving=True)
    if give_account is None or give_account.kind != gk:
        raise BusinessError(_("Choose the %(kind)s account the company gives from.")
                            % {"kind": Account.Kind(gk).label}, "give_account")
    if receive_account is not None and receive_account.kind != rk:
        raise BusinessError(_("What is served goes into a %(kind)s account.") % {"kind": Account.Kind(rk).label},
                            "receive_account")
    amount = _positive(amount)
    amount_given = amount if amount_given in (None, "") else amount_given
    amount_given = _money(amount_given or 0)
    amount_served = _money(amount_served or 0)
    if amount_given < 0 or amount_served < 0:
        raise BusinessError(_("Amounts cannot be negative."), "amount_served")
    if amount_given > amount:
        raise BusinessError(_("The company cannot give more than the agreed amount."), "amount_given")
    if amount_served > amount:
        raise BusinessError(_("The amount served cannot exceed the agreed amount."), "amount_served")
    if amount_served and receive_account is None:
        raise BusinessError(_("Choose the account that received what was served."), "receive_account")
    if served_on and served_on > douala_today():
        raise BusinessError(_("The date served cannot be in the future."), "served_on")
    with transaction.atomic():
        ex = create_exchange(user, cash_manager=cash_manager, amount=amount, note=note, direction=direction,
                             idempotency_key=idempotency_key, acting_for=acting_for)
        CashManagerExchange.objects.filter(pk=ex.pk).update(external_ref=(external_ref or "").strip()[:60],
                                                           served_on=served_on if amount_served else None)
        ref = (external_ref or "").strip()
        desc = " · ".join(x for x in (f"Ref {ref}" if ref else "", note) if x)
        key = idempotency_key or uuid.uuid4()
        if amount_given:
            record_exchange_leg(user, exchange=ex, side=TxType.CM_GIVE, amount=amount_given, account=give_account,
                                description=desc, idempotency_key=uuid.uuid5(key, "give"), acting_for=acting_for)
        if amount_served:
            served_txt = (_("served on %(d)s") % {"d": served_on.strftime("%d/%m/%Y")}) if served_on else ""
            record_exchange_leg(user, exchange=ex, side=TxType.CM_RECEIVE, amount=amount_served,
                                account=receive_account, idempotency_key=uuid.uuid5(key, "receive"),
                                description=" · ".join(x for x in (desc, served_txt) if x), acting_for=acting_for)
        ex.refresh_from_db()
        return ex


def build_exchange_leg(ctx, *, exchange, side, amount, account, description=""):
    amount = _positive(amount)
    exchange = CashManagerExchange.objects.select_for_update().get(pk=exchange.pk)
    if side not in (TxType.CM_GIVE, TxType.CM_RECEIVE):
        raise BusinessError(_("Choose which side of the exchange happened."), "side")
    want = exchange.give_kind() if side == TxType.CM_GIVE else exchange.receive_kind()
    if want is not None and account.kind != want:
        raise BusinessError(_("This side of the exchange must use a %(kind)s account.")
                            % {"kind": Account.Kind(want).label}, "account")
    if want is None and account.kind not in CashManagerExchange.LEG_KINDS:  # older exchanges without direction
        raise BusinessError(_("Cash-manager exchanges use a cash drawer or a UVE wallet."), "account")
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


def record_exchange_leg(user, idempotency_key=None, acting_for=None, **params):
    ctx = cashier_ctx(user, acting_for)
    with transaction.atomic():
        return post([build_exchange_leg(ctx, **params)], ctx, idempotency_key)[0]


# ---------------------------------------------------------------- opening balances
OPENING_KINDS = Account.ASSET_KINDS + (K.CUSTOMER, K.CASH_MANAGER, K.SHOP_PAYABLE)


def build_opening(ctx, *, account, amount, description="", split=None):
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
    split = clean_split(split, amount, allow_negative=True)
    if split and account.kind != K.CUSTOMER:
        raise BusinessError(_("Only agents and POS openings are split by product."), "account")
    equity = Account.system(K.OPENING_EQUITY)
    fields = dict(amount=abs(amount), description=description or "", product_split=split,
                  counterparty=account.counterparty if account.kind == K.CUSTOMER else None,
                  cash_manager=account.cash_manager if account.kind == K.CASH_MANAGER else None,
                  dest_account=account if account.is_asset else None,
                  shop=account.shop if account.shop_id else None)  # a shop sees its own openings
    if account.kind == K.SHOP_PAYABLE:
        fields["counterparty"] = account.shop_agent
    if ctx.exclude_txn_id is None:
        fields["opening_for_account"] = account
    return TxSpec(type=TxType.OPENING_BALANCE, lines=[(account, amount), (equity, -amount)], fields=fields)


def record_opening_balance(user, *, account, amount, business_date=None, description="", idempotency_key=None,
                          split=None):
    perm.require_role(user, perm.OPENING_ROLES)
    ctx = Ctx(entered_by=user, cashier=None, day=None, business_date=business_date or douala_today())
    with transaction.atomic():
        lock_accounts([account.id])  # serialise duplicate initialisation attempts
        spec = build_opening(ctx, account=account, amount=amount, description=description, split=split)
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
        perm.require_zone(user, supply.zone)
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

