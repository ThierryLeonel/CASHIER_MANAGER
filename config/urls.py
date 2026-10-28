from django.contrib import admin
from django.urls import include, path

from core import views as v

urlpatterns = [
    path("", v.dashboard, name="dashboard"),
    path("login/", v.login_view, name="login"),
    path("logout/", v.logout_view, name="logout"),
    path("i18n/", include("django.conf.urls.i18n")),
    # entry
    path("supply/new/", v.supply_new, name="supply_new"),
    path("supply/agent/", v.supply_agent, name="supply_agent"),
    path("supply/pos/", v.supply_pos, name="supply_pos"),
    path("api/customers/", v.customer_search, name="customer_search"),
    path("destockage/new/", v.destockage_new, name="destockage_new"),
    path("collection/new/", v.collection_new, name="collection_new"),
    path("expense/new/", v.expense_new, name="expense_new"),
    path("transfer/new/", v.transfer_new, name="transfer_new"),
    path("conversion/new/", v.conversion_new, name="conversion_new"),
    path("cash-managers/", v.exchanges, name="exchanges"),
    path("cash-managers/exchange/new/", v.exchange_new, name="exchange_new"),
    path("cash-managers/exchange/<int:pk>/", v.exchange_detail, name="exchange_detail"),
    # records
    path("customers/", v.customers, name="customers"),
    path("customers/new/", v.customer_new, name="customer_new"),
    path("customers/<int:pk>/", v.customer_detail, name="customer_detail"),
    path("customers/<int:pk>/edit/", v.customer_edit, name="customer_edit"),
    path("transactions/", v.txn_list, name="txn_list"),
    path("transactions/<int:pk>/", v.txn_detail, name="txn_detail"),
    path("transactions/<int:pk>/receipt/", v.receipt, name="receipt"),
    path("transactions/<int:pk>/receipt.pdf", v.receipt_pdf_view, name="receipt_pdf"),
    path("transactions/<int:pk>/correct/", v.txn_correct, name="txn_correct"),
    path("transactions/<int:pk>/transfer-responsibility/", v.txn_transfer, name="txn_transfer"),
    # control
    path("closing/", v.close_day, name="close_day"),
    path("closings/", v.closings, name="closings"),
    path("closings/<int:pk>/", v.closing_detail, name="closing_detail"),
    path("discrepancies/", v.discrepancies, name="discrepancies"),
    path("discrepancies/<int:pk>/", v.discrepancy_detail, name="discrepancy_detail"),
    path("setup/opening-balances/", v.opening_balances, name="opening_balances"),
    path("setup/conversion-rate/", v.conversion_rate, name="conversion_rate"),
    path("reports/", v.reports_index, name="reports"),
    path("reports/<slug:key>/", v.report, name="report"),
    path("admin/", admin.site.urls),
]
