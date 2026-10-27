from .base import dashboard, login_view, logout_view  # noqa: F401
from .control import (  # noqa: F401
    close_day, closing_detail, closings, conversion_rate, discrepancies, discrepancy_detail, opening_balances, report,
    reports_index,
)
from .entry import (  # noqa: F401
    collection_new, conversion_new, destockage_new, exchange_detail, exchange_new, exchanges, expense_new, supply_new,
    transfer_new,
)
from .records import (  # noqa: F401
    customer_detail, customer_edit, customer_new, customers, receipt, receipt_pdf_view, txn_correct, txn_detail,
    txn_list, txn_transfer,
)
