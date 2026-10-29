from .base import dashboard, login_view, logout_view  # noqa: F401
from .control import (  # noqa: F401
    close_day, closing_detail, closings, conversion_rate, discrepancies, discrepancy_detail, opening_balances, report,
    reports_index,
)
from .entry import (  # noqa: F401
    collection_new, conversion_new, supply_agent, supply_pos, destockage_new, exchange_detail, exchange_new, exchanges, expense_new, supply_new,
    transfer_new,
)
from .records import (  # noqa: F401
    customer_detail, customer_edit, customer_search, customer_new, customers, receipt, receipt_pdf_view, txn_correct, txn_detail,
    txn_list, txn_transfer,
)
from .admin_tools import (  # noqa: F401,E402
    admin_home, audit_log, clear_data, company_logo, company_profile, favicon, import_data, import_template, txn_delete, txn_edit,
)
from .base import act_as, idle_logout, keepalive, set_lang  # noqa: F401,E402
from .accounts import my_password, user_detail, user_toggle, users  # noqa: F401
