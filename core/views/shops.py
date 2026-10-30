"""Shop pages: the shop manager's desk, and the shops overview for the senior shop manager, finance and the
senior manager."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from core import forms_shop as SF
from core.models import (
    Account, ServiceCount, Shop, ShopConversionRate, ShopItem, ShopService, SimRetrieval, StockMove, Transaction,
    TxType,
)
from core.money import fmt
from core.services import shops as sh
from core.services.days import douala_today
from core.services.errors import BusinessError

from .base import R, roles_required, run_service

SHOP_ROLES = (R.SHOP_MANAGER, R.SHOP_SENIOR, R.MANAGER, R.FINANCE, R.SUPERADMIN)
SHOP_DESK = (R.SHOP_MANAGER, R.SHOP_SENIOR, R.SUPERADMIN)  # who records shop operations
SHOP_CONFIG = (R.SHOP_SENIOR, R.SUPERADMIN)  # who manages services, items, stock and the shop rate
K = Account.Kind
SHOP_TYPES = [t for t in TxType.values if t.startswith("SHOP_")]

# op -> (form class, service, title, intro, icon)
OPS = {
    "cashin": (SF.CashInOutForm, sh.cash_in, _("Orange Money cash-in"),
               _("The client gives cash and receives Orange Money: UVE goes down, the cash drawer goes up."), "payment"),
    "cashout": (SF.CashInOutForm, sh.cash_out, _("Orange Money cash-out"),
                _("The client withdraws cash: UVE goes up, the cash drawer goes down."), "supply"),
    "airtime": (SF.AirtimeForm, sh.airtime, _("Airtime sale"),
                _("Airtime sold to a client: ER goes down, the cash drawer goes up."), "convert"),
    "convert": (SF.ConvertForm, sh.convert, _("UVE → ER conversion (shop rate)"),
                _("UVE converted to ER at the shops' conversion rate."), "convert"),
    "appro": (SF.AgentDealForm, sh.appro, _("UVE supply from an agent (appro)"),
              _("The agent supplies UVE: the shop's UVE goes up and the shop pays cash, or owes the agent if on "
                "credit."), "supply"),
    "er_supply": (SF.AgentDealForm, sh.er_supply, _("ER supply from an agent"),
                  _("The agent supplies ER: the shop's ER goes up, paid in cash or UVE, or owed to the agent if on "
                    "credit."), "supply"),
    "destockage": (SF.AgentDealForm, sh.destockage, _("Destockage with an agent (cash supply)"),
                   _("The shop sends UVE to the agent and receives cash: UVE goes down, cash goes up."), "destock"),
    "pay_agent": (SF.AgentDealForm, sh.pay_agent, _("Pay an agent (supplies taken on credit)"),
                  _("Settles what the shop owes an agent."), "payment"),
    "to_cashier": (SF.ToCashierForm, sh.to_cashier, _("Transfer to a cashier"),
                   _("Cash or UVE sent from the shop to a cashier's drawer or UVE wallet."), "transfer"),
    "sale": (SF.SaleForm, sh.sell, _("Sale of an item"),
             _("Gadget or Orange energy kit sold: stock goes down, the cash drawer goes up."), "store"),
}


def _period(request, default_days=0):
    today = douala_today()
    try:
        d_from = date.fromisoformat(request.GET.get("from") or "")
    except ValueError:
        d_from = today - timedelta(days=default_days)
    try:
        d_to = date.fromisoformat(request.GET.get("to") or "")
    except ValueError:
        d_to = today
    return d_from, d_to


def _shop_or_404(user, pk):
    shop = get_object_or_404(Shop, pk=pk)
    if not sh.visible_shops(user).filter(pk=shop.pk).exists():
        raise Http404
    return shop


def _activity(shops, d_from, d_to):
    """Per shop: totals of the period by operation, commissions, counted services, retrievals and sales."""
    txq = Transaction.objects.filter(shop__in=shops, business_date__gte=d_from, business_date__lte=d_to,
                                     correction__isnull=True).exclude(type=TxType.REVERSAL)
    by = {}
    for row in txq.values("shop_id", "type").annotate(n=Count("id"), amt=Sum("amount"), com=Sum("commission")):
        d = by.setdefault(row["shop_id"], {})
        d[row["type"]] = row
    svc = {}
    for row in ServiceCount.objects.filter(shop__in=shops, business_date__gte=d_from, business_date__lte=d_to) \
            .values("shop_id").annotate(n=Sum("count")):
        svc[row["shop_id"]] = row["n"]
    ret = {}
    for row in SimRetrieval.objects.filter(shop__in=shops, business_date__gte=d_from, business_date__lte=d_to) \
            .values("shop_id").annotate(n=Count("id")):
        ret[row["shop_id"]] = row["n"]
    return by, svc, ret


@roles_required(*SHOP_ROLES)
def shops(request):
    """Shop manager: their shop's desk. Others: all shops."""
    user = request.user
    if user.is_shop_manager:
        return redirect("shop_detail", pk=user.shop_id) if user.shop_id else render(request, "core/shop/no_shop.html")
    d_from, d_to = _period(request)
    qs = list(Shop.objects.filter(active=True).select_related("zone"))
    by, svc, ret = _activity(qs, d_from, d_to)
    rows, tot = [], {"cash": Decimal(0), "uve": Decimal(0), "er": Decimal(0), "owed": Decimal(0), "com": Decimal(0),
                     "cashin": Decimal(0), "cashout": Decimal(0), "airtime": Decimal(0), "sales": Decimal(0)}
    for s in qs:
        b = sh.shop_balances(s)
        a = by.get(s.pk, {})

        def g(t, key="amt"):
            return (a.get(t) or {}).get(key) or Decimal(0)

        com = g(TxType.SHOP_CASHIN, "com") + g(TxType.SHOP_CASHOUT, "com")
        r = dict(shop=s, cash=b[K.CASH_DRAWER], uve=b[K.UVE], er=b[K.ER], owed=b["owed_agents"], com=com,
                 cashin=g(TxType.SHOP_CASHIN), cashout=g(TxType.SHOP_CASHOUT), airtime=g(TxType.SHOP_AIRTIME),
                 sales=g(TxType.SHOP_SALE), services=svc.get(s.pk, 0), retrievals=ret.get(s.pk, 0))
        rows.append(r)
        for k in tot:
            tot[k] += r[k]
    return render(request, "core/shop/overview.html", {"rows": rows, "tot": tot, "d_from": d_from, "d_to": d_to})


@roles_required(*SHOP_ROLES)
def shop_detail(request, pk):
    shop = _shop_or_404(request.user, pk)
    d_from, d_to = _period(request)
    b = sh.shop_balances(shop)
    by, svc, ret = _activity([shop], d_from, d_to)
    act = by.get(shop.pk, {})
    summary = []
    for t in SHOP_TYPES:
        row = act.get(t)
        if row:
            summary.append(dict(label=TxType(t).label, n=row["n"], amt=row["amt"], com=row["com"]))
    svc_rows = ServiceCount.objects.filter(shop=shop, business_date__gte=d_from, business_date__lte=d_to) \
        .values("service__name").annotate(n=Sum("count")).order_by("service__name")
    recent = Transaction.objects.filter(shop=shop).select_related("counterparty", "entered_by", "correction") \
        .order_by("-posted_at")[:25]
    owed = [(a.shop_agent, -sum((e.amount for e in a.entries.all()), Decimal(0)))
            for a in Account.objects.filter(kind=K.SHOP_PAYABLE, shop=shop).select_related("shop_agent")
            .prefetch_related("entries")]
    owed = [(ag, v) for ag, v in owed if v]
    return render(request, "core/shop/detail.html", {
        "shop": shop, "bal": b, "summary": summary, "svc_rows": svc_rows, "retrievals": ret.get(shop.pk, 0),
        "recent": recent, "owed": owed, "d_from": d_from, "d_to": d_to,
        "can_record": request.user.role in SHOP_DESK and (not request.user.is_shop_manager
                                                          or request.user.shop_id == shop.pk),
        "rate": ShopConversionRate.current()})


@roles_required(*SHOP_DESK)
def shop_op(request, op):
    if op not in OPS:
        raise Http404
    form_class, fn, title, intro, icon = OPS[op]
    kwargs = {"op": op} if form_class is SF.AgentDealForm else {}
    form = form_class(request.user, request.POST or None, **kwargs)
    if request.method == "POST" and form.is_valid():
        params = form.params()
        resp = run_service(
            request, form, lambda: fn(request.user, idempotency_key=form.cleaned_data["idem"], **params),
            success=lambda t: _("%(type)s %(ref)s recorded: %(amt)s FCFA.") % {
                "type": t.get_type_display(), "ref": t.reference, "amt": fmt(t.amount)},
            on_success=lambda t: redirect("shop_detail", pk=t.shop_id))
        if resp:
            return resp
    if op == "convert":
        r = ShopConversionRate.current()
        intro = f"{intro} " + (_("Current shop rate: %(p)s %%.") % {"p": r.percent} if r else
                                _("The shop rate is not set yet."))
    return render(request, "core/entry_form.html", {"form": form, "title": title, "intro": intro, "icon": icon})


@roles_required(*SHOP_DESK)
def shop_service(request):
    form = SF.ServiceCountForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            sh.count_service(request.user, **{k: v for k, v in form.params().items()})
        except BusinessError as e:
            form.add_error(e.field if e.field in form.fields else None, e.message)
        else:
            messages.success(request, _("Service recorded."))
            return redirect("shop_service")
    return render(request, "core/entry_form.html", {"form": form, "title": _("Record services done"),
                                                 "intro": _("Only the number of services is recorded (e.g. OTT, MAX IT, "
                                                            "identification updates, tickets)."), "icon": "list"})


@roles_required(*SHOP_DESK)
def shop_retrieve(request):
    form = SF.RetrievalForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            svc = ShopService.objects.filter(kind=ShopService.Kind.RETRIEVE, active=True).first()
            sh.retrieve_sim(request.user, service=svc, **form.params())
        except BusinessError as e:
            form.add_error(e.field if e.field in form.fields else None, e.message)
        else:
            messages.success(request, _("SIM retrieval registered."))
            return redirect("shop_retrieve")
    return render(request, "core/entry_form.html", {"form": form, "title": _("SIM retrieval"),
                                                 "intro": _("Register the person retrieving the SIM and the SIM."),
                                                 "icon": "users"})


@roles_required(*SHOP_ROLES)
def shop_transactions(request):
    user = request.user
    shops_qs = sh.visible_shops(user)
    d_from, d_to = _period(request, default_days=30)
    qs = Transaction.objects.filter(shop__in=shops_qs, business_date__gte=d_from, business_date__lte=d_to) \
        .select_related("shop", "counterparty", "entered_by", "correction").order_by("-posted_at")
    shop_id, typ = request.GET.get("shop") or "", request.GET.get("type") or ""
    if shop_id.isdigit():
        qs = qs.filter(shop_id=shop_id)
    if typ in SHOP_TYPES:
        qs = qs.filter(type=typ)
    totals = qs.exclude(type=TxType.REVERSAL).aggregate(amt=Sum("amount"), com=Sum("commission"))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "core/shop/transactions.html", {
        "page": page, "shops": shops_qs, "types": [(t, TxType(t).label) for t in SHOP_TYPES], "shop_id": shop_id,
        "typ": typ, "d_from": d_from, "d_to": d_to, "totals": totals})


@roles_required(*SHOP_ROLES)
def shop_retrievals(request):
    d_from, d_to = _period(request, default_days=30)
    q = (request.GET.get("q") or "").strip()
    qs = SimRetrieval.objects.filter(shop__in=sh.visible_shops(request.user), business_date__gte=d_from,
                                     business_date__lte=d_to).select_related("shop", "recorded_by")
    if q:
        qs = qs.filter(Q(sim_number__icontains=q) | Q(holder_name__icontains=q) | Q(id_number__icontains=q))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "core/shop/retrievals.html", {"page": page, "q": q, "d_from": d_from, "d_to": d_to})


@roles_required(*SHOP_ROLES)
def shop_services_report(request):
    d_from, d_to = _period(request, default_days=30)
    shops_qs = sh.visible_shops(request.user)
    rows = ServiceCount.objects.filter(shop__in=shops_qs, business_date__gte=d_from, business_date__lte=d_to) \
        .values("shop__name", "service__name").annotate(n=Sum("count")).order_by("shop__name", "service__name")
    recent = ServiceCount.objects.filter(shop__in=shops_qs).select_related("shop", "service", "recorded_by")[:30]
    return render(request, "core/shop/services_report.html", {"rows": rows, "recent": recent, "d_from": d_from,
                                                              "d_to": d_to})


@roles_required(*SHOP_ROLES)
def shop_stock(request):
    user = request.user
    shops_qs = list(sh.visible_shops(user).filter(active=True))
    items = list(ShopItem.objects.filter(active=True))
    levels = {(m["shop_id"], m["item_id"]): m["q"] for m in
              StockMove.objects.filter(shop__in=shops_qs).values("shop_id", "item_id").annotate(q=Sum("quantity"))}
    table = [dict(item=i, cells=[levels.get((s.pk, i.pk), 0) for s in shops_qs]) for i in items]
    form = None
    if user.role in SHOP_CONFIG or user.is_shop_manager:
        form = SF.StockForm(user, request.POST or None)
        if request.method == "POST" and form.is_valid():
            cd = form.cleaned_data
            try:
                sh.receive_stock(user, shop=cd["shop"], item=cd["item"], quantity=cd["quantity"], note=cd["note"])
            except BusinessError as e:
                form.add_error("quantity", e.message)
            else:
                messages.success(request, _("Stock updated."))
                return redirect("shop_stock")
    moves = StockMove.objects.filter(shop__in=shops_qs).select_related("shop", "item", "recorded_by")[:30]
    return render(request, "core/shop/stock.html", {"shops": shops_qs, "table": table, "form": form, "moves": moves})


@roles_required(*SHOP_CONFIG)
def shop_setup(request):
    """Services, items and the shop conversion rate (senior shop manager and super administrator)."""
    what = request.GET.get("edit") or ""
    svc_form = SF.ServiceDefForm(request.POST or None, prefix="svc",
                                 instance=ShopService.objects.filter(pk=request.GET.get("svc") or 0).first())
    item_form = SF.ItemForm(request.POST or None, prefix="item",
                            instance=ShopItem.objects.filter(pk=request.GET.get("item") or 0).first())
    rate_form = SF.ShopRateForm(request.POST or None, prefix="rate")
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "service" and svc_form.is_valid():
            svc_form.save()
            messages.success(request, _("Service saved."))
            return redirect("shop_setup")
        if action == "item" and item_form.is_valid():
            item_form.save()
            messages.success(request, _("Item saved."))
            return redirect("shop_setup")
        if action == "rate" and rate_form.is_valid():
            try:
                sh.set_shop_rate(request.user, **rate_form.cleaned_data)
            except BusinessError as e:
                rate_form.add_error("percent", e.message)
            else:
                messages.success(request, _("Shop conversion rate saved."))
                return redirect("shop_setup")
    return render(request, "core/shop/setup.html", {
        "services": ShopService.objects.all(), "items": ShopItem.objects.all(), "svc_form": svc_form,
        "item_form": item_form, "rate_form": rate_form, "rate": ShopConversionRate.current(), "what": what,
        "rates": ShopConversionRate.objects.select_related("set_by")[:10]})
