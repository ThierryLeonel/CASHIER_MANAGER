from .base import dashboard, login_view, logout_view  # noqa: F401
from .control import (  # noqa: F401
    close_day, closing_detail, closings, conversion_rate, discrepancies, discrepancy_detail, opening_balances, shop_opening_balances, report,
    reports_index,
)
from .entry import (  # noqa: F401
    collection_new, conversion_new, supply_agent, supply_pos, destockage_new, exchange_detail, exchange_new, exchanges, expense_new, supply_new,
    transfer_new, entry_new,
)
from .records import (  # noqa: F401
    customer_detail, customer_edit, customer_search, customers_move, stock_download, call_database, agent_roads, customer_new, customers, receipt, receipt_pdf_view, txn_correct, txn_detail,
    txn_list, txn_transfer,
)
from .admin_tools import (  # noqa: F401,E402
    admin_home, audit_log, clear_data, company_logo, company_profile, favicon, import_batch, import_data, import_history_clear, import_step, import_template, txn_delete, txn_edit,
)
from .base import act_as, agents_supervision, idle_logout, keepalive, set_lang  # noqa: F401,E402
from .accounts import my_password, user_detail, user_toggle, users  # noqa: F401
from .shops import (  # noqa: F401,E402
    shop_detail, shop_op, shop_retrievals, shop_retrieve, shop_service, shop_services_report, shop_setup, shop_manage, shop_shift, shop_reconciliations, shop_reconciliation, shop_attendance, shop_reports, shop_purchase,
    shop_stock, shop_transactions, shops, desk_to_shop, desk_exchange,
)
from .capital import capital, capital_op  # noqa: F401,E402
from .records import transfer_no_payback, transfer_pay  # noqa: F401,E402
from .agents import (  # noqa: F401,E402
    agent_activity, agent_collect, agent_entry_cancel, agent_payback, agent_payback_cancel, agent_pos, agent_pos_detail, agent_pos_search, agent_oos, agent_destock, agent_destock_record, market_sim, agent_coverage, agent_pos_edit,
    agent_daily, agent_debtors, agent_debtor_pay, agent_exchange, agent_exchange_cancel, agent_office, agent_supply, agent_transfer, payback_decide, paybacks,
)
