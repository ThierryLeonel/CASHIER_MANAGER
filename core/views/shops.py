"""Shop pages: the shop manager's desk, and the shops overview for the senior shop manager, finance and the
senior manager."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _

from core import forms_shop as SF
from core.models import (
    ShopShift, User, Account, ItemCategory, ServiceCount, Shop, ShopConversionRate, ShopItem, ShopService, SimRetrieval,
    StockUnit, Transaction, TxType,
)
from core.money import fmt
from core.services import shop_admin as sa
from core.services import shop_shifts as ss
from core.services import shops as sh
from core.services.days import douala_today
from core.services.errors import BusinessError

from .base import R, roles_required, run_service

SHOP_ROLES = (R.SHOP_MANAGER, R.SHOP_SENIOR, R.MANAGER, R.FINANCE, R.SUPERADMIN)
SHOP_DESK = (R.SHOP_MANAGER, R.SHOP_SENIOR, R.SUPERADMIN)  # who records shop operations
SHOP_CONFIG = (R.SHOP_SENIOR, R.SUPERADMIN)  # who manages services, items, stock and the shop rate
K = Account.Kind
SHOP_TYPES = [t for t in TxType.values if t.startswith("SHOP_")] + [TxType.DESK_TO_SHOP, TxType.CAPITAL_IN,
                                                                   TxType.CAPITAL_OUT, TxType.CAPITAL_MOVE]

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
                   _("The shop sends UVE to the agent and receives cash — or the agent owes the shop if on credit."),
                   "destock"),
    "pay_agent": (SF.AgentDealForm, sh.pay_agent, _("Pay an agent (supplies taken on credit)"),
                  _("Settles what the shop owes an agent."), "payment"),
    "agent_repay": (SF.AgentDealForm, sh.agent_repay, _("An agent pays the shop"),
                    _("The agent pays back what he owes the shop (e.g. a destockage taken on credit)."), "payment"),
    "to_cashier": (SF.ToCashierForm, sh.to_cashier, _("Transfer UVE to a cashier"),
                   _("UVE sent from the shop to a cashier's UVE wallet. It is recorded on the cashier side too."),
                   "transfer"),
    "sale": (SF.SaleForm, sh.sell, _("Sale"), "", "store"),
    "expense": (SF.ShopExpenseForm, sh.expense, _("Shop expense"),
                _("An expense of the shop (rent, electricity, transport…), paid from its cash drawer or UVE."),
                "expense"),
}


def _need_duty(user, key):
    """403 when a shop staff member opens a page outside their duties (segmented shops)."""
    duty = sh.OP_DUTY.get(key)
    if duty and not user.has_shop_duty(duty):
        raise DjangoPermissionDenied


def _shift_redirect(request):
    """Shop staff must open their shift before recording: send them to the shift page."""
    if request.user.is_shop_manager and ss.current_shift(request.user) is None:
        messages.info(request, _("Open your shift first: count the till, then you can record."))
        return redirect(f"/shops/shift/?next={request.path}")
    return None


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
    rows, tot = [], {"cash": Decimal(0), "uve": Decimal(0), "er": Decimal(0), "owed": Decimal(0),
                     "owed_by": Decimal(0), "com": Decimal(0),
                     "cashin": Decimal(0), "cashout": Decimal(0), "airtime": Decimal(0), "sales": Decimal(0)}
    for s in qs:
        b = sh.shop_balances(s)
        a = by.get(s.pk, {})

        def g(t, key="amt"):
            return (a.get(t) or {}).get(key) or Decimal(0)

        com = g(TxType.SHOP_CASHIN, "com") + g(TxType.SHOP_CASHOUT, "com")
        r = dict(shop=s, cash=b[K.CASH_DRAWER], uve=b[K.UVE], er=b[K.ER], owed=b["owed_agents"],
                 owed_by=b["owed_by_agents"], com=com,
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
    user = request.user
    types = sh.visible_types(user)  # segmented shop: a staff member sees only the operations of their duties
    restricted = types is not None
    summary = []
    for t in SHOP_TYPES:
        row = act.get(t)
        if row and (types is None or t in types):
            summary.append(dict(label=TxType(t).label, n=row["n"], amt=row["amt"], com=row["com"]))
    sees_services = user.has_shop_duty("SERVICES")
    svc_rows = ServiceCount.objects.filter(shop=shop, business_date__gte=d_from, business_date__lte=d_to) \
        .values("service__name").annotate(n=Sum("count")).order_by("service__name") if sees_services else []
    recent = Transaction.objects.filter(shop=shop).select_related("counterparty", "entered_by", "correction") \
        .order_by("-posted_at")
    if restricted:
        recent = recent.filter(type__in=types)
    recent = recent[:25]
    positions = [(a.shop_agent, sum((e.amount for e in a.entries.all()), Decimal(0)))
                 for a in Account.objects.filter(kind=K.SHOP_PAYABLE, shop=shop).select_related("shop_agent")
                 .prefetch_related("entries")] if user.has_shop_duty("AGENTS") else []
    staff_rows = []
    if not restricted:
        per_user = {r["entered_by_id"]: r for r in Transaction.objects.filter(
            shop=shop, business_date__gte=d_from, business_date__lte=d_to, correction__isnull=True)
            .exclude(type=TxType.REVERSAL).values("entered_by_id").annotate(n=Count("id"), amt=Sum("amount"),
                                                                             com=Sum("commission"))}
        staff_rows = [dict(user=u, n=(per_user.get(u.pk) or {}).get("n") or 0,
                           amt=(per_user.get(u.pk) or {}).get("amt") or 0,
                           com=(per_user.get(u.pk) or {}).get("com"))
                      for u in User.objects.filter(shop=shop).order_by("-is_active", "username")]
    owed = [(ag, -v) for ag, v in positions if v < 0]
    owed_by = [(ag, v) for ag, v in positions if v > 0]
    return render(request, "core/shop/detail.html", {
        "shop": shop, "bal": b, "summary": summary, "svc_rows": svc_rows,
        "retrievals": ret.get(shop.pk, 0) if sees_services else 0, "restricted": restricted,
        "sees_agents": user.has_shop_duty("AGENTS"), "sees_services": sees_services,
        "recent": recent, "staff_rows": staff_rows, "owed": owed, "owed_by": owed_by, "d_from": d_from, "d_to": d_to,
        "can_record": user.role in SHOP_DESK and (not user.is_shop_manager or user.shop_id == shop.pk),
        "rate": ShopConversionRate.current(), "my_shift": ss.current_shift(request.user)})


@roles_required(*SHOP_DESK)
def shop_op(request, op):
    if op not in OPS:
        raise Http404
    _need_duty(request.user, op)
    if (r := _shift_redirect(request)):
        return r
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
    if op == "sale":
        return render(request, "core/shop/sale.html", {"form": form})
    return render(request, "core/entry_form.html", {"form": form, "title": title, "intro": intro, "icon": icon})


@roles_required(R.CASHIER, R.SUPERADMIN)
def desk_to_shop(request):
    """Cashier side: send UVE from the subzone's UVE wallet to a shop."""
    from .base import acting_cashier

    acting = acting_cashier(request) if request.user.is_superadmin else None
    if request.user.is_superadmin and acting is None:
        return redirect(f"/act-as/?next={request.path}")
    form = SF.DeskToShopForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        resp = run_service(
            request, form, lambda: sh.desk_to_shop(request.user, idempotency_key=form.cleaned_data["idem"],
                                                   acting_for=acting, **form.params()),
            success=lambda t: _("%(type)s %(ref)s recorded: %(amt)s FCFA.") % {
                "type": t.get_type_display(), "ref": t.reference, "amt": fmt(t.amount)},
            on_success=lambda t: redirect("txn_detail", pk=t.pk))
        if resp:
            return resp
    return render(request, "core/entry_form.html", {
        "form": form, "title": _("Transfer UVE to a shop"), "icon": "transfer",
        "intro": _("UVE sent from your subzone's UVE wallet to a shop. The shop sees it in its entries.")})


@roles_required(*SHOP_DESK)
def shop_service(request):
    _need_duty(request.user, "service")
    if (r := _shift_redirect(request)):
        return r
    form = SF.ServiceCountForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            done = sh.count_services(request.user, **form.params())
        except BusinessError as e:
            form.add_error(e.field if e.field in form.fields else None, e.message)
        else:
            messages.success(request, _("Services recorded: %(list)s.") % {
                "list": ", ".join(f"{c.service.name} × {c.count}" for c in done)})
            return redirect("shop_service")
    shop = request.user.shop if request.user.is_shop_manager else None
    today = (ServiceCount.objects.filter(shop=shop, business_date=douala_today()).values("service__name")
             .annotate(n=Sum("count")).order_by("service__name")) if shop else []
    return render(request, "core/shop/services_new.html", {"form": form, "today": today})


@roles_required(*SHOP_DESK)
def shop_retrieve(request):
    _need_duty(request.user, "retrieve")
    if (r := _shift_redirect(request)):
        return r
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
    allowed = sh.visible_types(user)
    types = [t for t in SHOP_TYPES if allowed is None or t in allowed]
    if allowed is not None:
        qs = qs.filter(type__in=allowed)
    if shop_id.isdigit():
        qs = qs.filter(shop_id=shop_id)
    if typ in types:
        qs = qs.filter(type=typ)
    totals = qs.exclude(type=TxType.REVERSAL).aggregate(amt=Sum("amount"), com=Sum("commission"))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "core/shop/transactions.html", {
        "page": page, "shops": shops_qs, "types": [(t, TxType(t).label) for t in types], "shop_id": shop_id,
        "typ": typ, "d_from": d_from, "d_to": d_to, "totals": totals})


@roles_required(*SHOP_ROLES)
def shop_retrievals(request):
    _need_duty(request.user, "retrieve")
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
    _need_duty(request.user, "service")
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
    _need_duty(user, "stock")
    shops_qs = list(sh.visible_shops(user).filter(active=True))
    items = list(ShopItem.objects.filter(active=True).select_related("category"))
    levels = {(m["shop_id"], m["item_id"]): m["n"] for m in
              StockUnit.objects.filter(shop__in=shops_qs, status=StockUnit.Status.IN_STOCK)
              .values("shop_id", "item_id").annotate(n=Count("id"))}
    table = [dict(item=i, cells=[levels.get((s.pk, i.pk), 0) for s in shops_qs]) for i in items]
    can_edit = user.role in SHOP_CONFIG or (user.is_shop_manager and user.has_shop_duty("SALES"))
    form = SF.StockForm(user, request.POST if request.POST.get("action") == "receive" else None) if can_edit else None
    rm_form = SF.RemoveUnitForm(request.POST if request.POST.get("action") == "remove" else None) if can_edit else None
    if request.method == "POST" and can_edit:
        try:
            if request.POST.get("action") == "receive" and form.is_valid():
                cd = form.cleaned_data
                if cd["item"].category_id != cd["category"].pk:
                    raise BusinessError(_("This item is not in the chosen category."), "item")
                units = sh.receive_stock(user, shop=cd["shop"], item=cd["item"], units=cd["units"],
                                         note=cd["note"])
                messages.success(request, _("%(n)s unit(s) received into stock.") % {"n": len(units)})
                return redirect("shop_stock")
            if request.POST.get("action") == "remove" and rm_form.is_valid():
                u = sh.remove_unit(user, **rm_form.cleaned_data)
                messages.success(request, _("%(s)s removed from stock.") % {"s": u.serial})
                return redirect("shop_stock")
        except BusinessError as e:
            target = form if request.POST.get("action") == "receive" else rm_form
            target.add_error(e.field if e.field in target.fields else None, e.message)
    q = (request.GET.get("q") or "").strip()
    units = StockUnit.objects.filter(shop__in=shops_qs).select_related("shop", "item", "sale")
    if q:
        units = units.filter(Q(serial__icontains=q) | Q(imei__icontains=q.replace(" ", "")) | Q(item__name__icontains=q)
                             | Q(sale__client_name__icontains=q))
    else:
        units = units.filter(status=StockUnit.Status.IN_STOCK)
    return render(request, "core/shop/stock.html", {"shops": shops_qs, "table": table, "form": form,
                                                     "rm_form": rm_form, "units": units[:200], "q": q})


@roles_required(*SHOP_CONFIG)
def shop_setup(request):
    """Services, items and the shop conversion rate (senior shop manager and super administrator)."""
    what = request.GET.get("edit") or ""
    svc_form = SF.ServiceDefForm(request.POST or None, prefix="svc",
                                 instance=ShopService.objects.filter(pk=request.GET.get("svc") or 0).first())
    item_form = SF.ItemForm(request.POST or None, prefix="item",
                            instance=ShopItem.objects.filter(pk=request.GET.get("item") or 0).first())
    rate_form = SF.ShopRateForm(request.POST or None, prefix="rate")
    cat_form = SF.CategoryForm(request.POST or None, prefix="cat",
                               instance=ItemCategory.objects.filter(pk=request.GET.get("cat") or 0).first())
    if request.method == "POST":
        action = request.POST.get("action")
        models_by = {"delete_service": ShopService, "delete_item": ShopItem, "delete_category": ItemCategory}
        if action in models_by:
            obj = get_object_or_404(models_by[action], pk=request.POST.get("pk"))
            try:
                label = sa.delete_definition(request.user, obj)
            except BusinessError as e:
                messages.error(request, e.message)
            else:
                messages.success(request, _("%(x)s deleted.") % {"x": label})
            return redirect("shop_setup")
        if action == "category" and cat_form.is_valid():
            cat_form.save()
            messages.success(request, _("Category saved."))
            return redirect("shop_setup")
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
        "services": ShopService.objects.all(), "items": ShopItem.objects.select_related("category"),
        "categories": ItemCategory.objects.all(), "cat_form": cat_form, "svc_form": svc_form,
        "item_form": item_form, "rate_form": rate_form, "rate": ShopConversionRate.current(), "what": what,
        "rates": ShopConversionRate.objects.select_related("set_by")[:10]})


@roles_required(*SHOP_CONFIG)
def shop_manage(request):
    """Shops and shop staff access (senior shop manager, super administrator): add, edit, transfer, delete."""
    user = request.user
    shop_inst = Shop.objects.filter(pk=request.GET.get("shop") or 0).first()
    staff_inst = User.objects.filter(pk=request.GET.get("staff") or 0, role=User.Role.SHOP_MANAGER).first()
    action = request.POST.get("action") if request.method == "POST" else None
    shop_form = SF.ShopDefForm(request.POST if action == "shop_save" else None, prefix="shop", instance=shop_inst)
    staff_form = SF.StaffForm(request.POST if action == "staff_save" else None, prefix="staff", instance=staff_inst)
    new_pw = None
    try:
        if action == "shop_save" and shop_form.is_valid():
            s = sa.save_shop(user, shop_form)
            messages.success(request, _("Shop %(s)s saved.") % {"s": s.name})
            return redirect("shop_manage")
        if action == "shop_delete":
            shop = get_object_or_404(Shop, pk=request.POST.get("pk"))
            name = shop.name
            sa.delete_shop(user, shop)
            messages.success(request, _("Shop %(s)s deleted.") % {"s": name})
            return redirect("shop_manage")
        if action == "staff_save" and staff_form.is_valid():
            cd = staff_form.cleaned_data
            if staff_inst:
                sa.update_staff(user, staff_inst, first_name=cd["first_name"], last_name=cd["last_name"],
                                shop=cd["shop"], duties=cd["duties"])
                messages.success(request, _("Access of %(u)s saved.") % {"u": staff_inst.username})
                return redirect("shop_manage")
            u, new_pw = sa.create_staff(user, username=cd["username"], first_name=cd["first_name"],
                                        last_name=cd["last_name"], shop=cd["shop"], duties=cd["duties"])
            messages.success(request, _("Account %(u)s created. Give the temporary password below; it must be "
                                        "changed at the first sign-in.") % {"u": u.username})
            staff_form = SF.StaffForm(prefix="staff")
            new_pw = (u, new_pw)
        if action in ("staff_toggle", "staff_reset", "staff_delete"):
            target = get_object_or_404(User, pk=request.POST.get("pk"))
            if action == "staff_toggle":
                sa.set_staff_active(user, target, not target.is_active)
                messages.success(request, (_("%(u)s can sign in again.") if target.is_active else
                                           _("%(u)s is deactivated and can no longer sign in."))
                                 % {"u": target.username})
                return redirect("shop_manage")
            if action == "staff_reset":
                new_pw = (target, sa.reset_staff_password(user, target))
                messages.success(request, _("Password of %(u)s replaced.") % {"u": target.username})
            if action == "staff_delete":
                name = target.username
                sa.delete_staff(user, target)
                messages.success(request, _("Account %(u)s deleted.") % {"u": name})
                return redirect("shop_manage")
    except BusinessError as e:
        form = staff_form if action == "staff_save" else shop_form if action == "shop_save" else None
        if form is not None:
            form.add_error(e.field if e.field in form.fields else None, e.message)
        else:
            messages.error(request, e.message)
    shops_qs = Shop.objects.select_related("zone").annotate(n_staff=Count("managers", distinct=True))
    staff = User.objects.filter(role=User.Role.SHOP_MANAGER).select_related("shop").order_by("shop__name", "username")
    return render(request, "core/shop/manage.html", {
        "shops": shops_qs, "staff": staff, "shop_form": shop_form, "staff_form": staff_form, "new_pw": new_pw,
        "seniors": User.objects.filter(role=User.Role.SHOP_SENIOR)})


# ---------------------------------------------------------------- shifts, attendance, daily reconciliation
def _day(request):
    try:
        return date.fromisoformat(request.GET.get("date") or "")
    except ValueError:
        return douala_today()


@roles_required(R.SHOP_MANAGER)
def shop_shift(request):
    """Open or close my shift (work attendance and till counts)."""
    user = request.user
    shift = ss.current_shift(user)
    nxt = request.GET.get("next") or ""
    nxt = nxt if nxt.startswith("/shops/") and not nxt.startswith("//") else ""
    if shift is None:
        required = ss.counts_required_to_open(user.shop) if user.shop_id else True
    else:
        required = not ss.others_open(shift.shop, exclude=shift).exists()
    form = SF.ShiftForm(request.POST or None, required=required)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        try:
            if shift is None:
                ss.open_shift(user, **cd)
                messages.success(request, _("Shift opened at %(t)s. Have a good day.")
                                 % {"t": timezone.localtime().strftime("%H:%M")})
                return redirect(nxt or "shop_detail", **({} if nxt else {"pk": user.shop_id}))
            done = ss.close_shift(user, **cd)
            msg = _("Shift closed at %(t)s (%(h)s h).") % {"t": timezone.localtime(done.closed_at).strftime("%H:%M"),
                                                          "h": done.hours}
            if done.has_difference:
                messages.warning(request, msg + " " + _("The counts differ from the system balances; the senior "
                                                        "shop manager will review them."))
            else:
                messages.success(request, msg)
            return redirect("shop_shift")
        except BusinessError as e:
            form.add_error(e.field if e.field in form.fields else None, e.message)
    recent = ShopShift.objects.filter(user=user).select_related("shop")[:15]
    return render(request, "core/shop/shift.html", {"shift": shift, "form": form, "required": required,
                                                     "recent": recent, "others": ss.others_open(user.shop, exclude=shift)
                                                     .select_related("user") if user.shop_id else []})


@roles_required(*SHOP_ROLES)
def shop_reconciliations(request):
    """All visible shops for one day: status of the daily reconciliation."""
    day = _day(request)
    rows = []
    for s in sh.visible_shops(request.user).filter(active=True):
        rec = ss.day_reconciliation(s, day)
        rows.append(dict(shop=s, rec=rec, status_label=ss.STATUS_LABELS[rec["status"]]))
    return render(request, "core/shop/recon_list.html", {"rows": rows, "day": day,
                                                          "prev": day - timedelta(days=1),
                                                          "next": day + timedelta(days=1)})


@roles_required(*SHOP_ROLES)
def shop_reconciliation(request, pk):
    shop = _shop_or_404(request.user, pk)
    day = _day(request)
    can_review = request.user.is_shop_senior or request.user.is_superadmin
    review_form = SF.DayReviewForm(request.POST if request.POST.get("action") == "review" else None)
    force_form = SF.ForceCloseForm(request.POST if request.POST.get("action") == "force" else None)
    if request.method == "POST" and can_review:
        try:
            if request.POST.get("action") == "review" and review_form.is_valid():
                ss.review_day(request.user, shop=shop, day=day, note=review_form.cleaned_data["note"])
                messages.success(request, _("Day signed off."))
                return redirect(f"{request.path}?date={day.isoformat()}")
            if request.POST.get("action") == "force" and force_form.is_valid():
                shift = get_object_or_404(ShopShift, pk=request.POST.get("pk"), shop=shop)
                ss.close_shift(request.user, shift=shift, note=force_form.cleaned_data["note"])
                messages.success(request, _("Shift of %(u)s closed.") % {"u": shift.user})
                return redirect(f"{request.path}?date={day.isoformat()}")
        except BusinessError as e:
            target = review_form if request.POST.get("action") == "review" else force_form
            target.add_error(e.field if e.field in target.fields else None, e.message)
    rec = ss.day_reconciliation(shop, day, viewer=request.user)
    return render(request, "core/shop/recon.html", {
        "rec": rec, "shop": shop, "day": day, "status_label": ss.STATUS_LABELS[rec["status"]],
        "can_review": can_review, "review_form": review_form, "force_form": force_form,
        "prev": day - timedelta(days=1), "next": day + timedelta(days=1)})


@roles_required(*SHOP_ROLES)
def shop_attendance(request):
    d_from, d_to = _period(request, default_days=6)
    shops_qs = sh.visible_shops(request.user)
    shop_id = request.GET.get("shop") or ""
    if shop_id.isdigit():
        shops_qs = shops_qs.filter(pk=shop_id)
    only = request.user if request.user.is_shop_manager else None
    shifts, people = ss.attendance(shops_qs, d_from, d_to, user=only)
    return render(request, "core/shop/attendance.html", {
        "shifts": shifts[:300], "people": people, "d_from": d_from, "d_to": d_to, "shop_id": shop_id,
        "shops": sh.visible_shops(request.user)})


@roles_required(*SHOP_ROLES)
def shop_reports(request):
    """Daily / weekly / monthly shop summary, on screen, in Excel and in PDF."""
    from django.http import HttpResponse

    from core.reports.engine import only_table
    from core.reports.shops import ShopReportForm, income_statement, shop_summary
    from core.pdf import report_pdf
    from core.reports.xlsx import report_xlsx
    from core.services import operations as ops

    visible = sh.visible_shops(request.user).filter(active=True)
    data = request.GET.copy()
    fmt_ = data.pop("format", [None])[0]
    data.pop("table", None)
    form = ShopReportForm(visible, data or None)
    kind = data.get("kind") or "summary"
    if kind == "income":
        if sh.is_restricted(request.user):  # the income statement needs every duty
            raise DjangoPermissionDenied
        rep, period, d_from, d_to = income_statement(form, visible)
    else:
        rep, period, d_from, d_to = shop_summary(form, visible, viewer=request.user)
    if fmt_ in ("xlsx", "pdf"):
        rep = only_table(rep, request)
    who = f"{request.user} ({request.user.get_role_display()})"
    if fmt_ in ("xlsx", "pdf"):
        ops.log_event(request.user, "report_export", report="shops", format=fmt_, filters=rep.title)
        name = f"shop-{'income-statement' if kind == 'income' else 'report'}-{period}-{d_from}"
        if fmt_ == "xlsx":
            resp = HttpResponse(report_xlsx(rep, who),
                                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            resp["Content-Disposition"] = f'attachment; filename="{name}.xlsx"'
        else:
            resp = HttpResponse(report_pdf(rep, who), content_type="application/pdf")
            resp["Content-Disposition"] = f'attachment; filename="{name}.pdf"'
        return resp
    return render(request, "core/report.html", {"rep": rep, "form": form, "query": data.urlencode(),
                                                       "period": period})


@roles_required(*SHOP_DESK)
def shop_purchase(request):
    """Items bought for resale: units enter the stock at cost; the shop pays from cash or UVE."""
    _need_duty(request.user, "purchase")
    if (r := _shift_redirect(request)):
        return r
    form = SF.PurchaseForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        resp = run_service(
            request, form, lambda: sh.purchase(request.user, idempotency_key=form.cleaned_data["idem"],
                                               **form.params()),
            success=lambda t: _("%(type)s %(ref)s recorded: %(amt)s FCFA.") % {
                "type": t.get_type_display(), "ref": t.reference, "amt": fmt(t.amount)},
            on_success=lambda t: redirect("txn_detail", pk=t.pk))
        if resp:
            return resp
    return render(request, "core/shop/purchase.html", {"form": form})
