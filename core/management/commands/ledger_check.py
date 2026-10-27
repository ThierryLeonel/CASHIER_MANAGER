"""Independent integrity check of the ledger. Exit code 1 on any problem.

    python manage.py ledger_check
"""
from django.core.management.base import BaseCommand
from django.db import connection


CHECKS = [
    ("Unbalanced transactions",
     "SELECT transaction_id, SUM(amount) FROM core_entry GROUP BY transaction_id HAVING SUM(amount) <> 0"),
    ("Transactions without entries",
     "SELECT t.id, t.reference FROM core_transaction t LEFT JOIN core_entry e ON e.transaction_id = t.id "
     "WHERE e.id IS NULL"),
    ("Negative company asset balances",
     "SELECT a.id, a.name, SUM(e.amount) FROM core_account a JOIN core_entry e ON e.account_id = a.id "
     "WHERE a.kind IN ('CASH_DRAWER','UVE','ER','MTN','BANK') GROUP BY a.id, a.name HAVING SUM(e.amount) < 0"),
    ("Entry business date differs from its transaction",
     "SELECT e.id FROM core_entry e JOIN core_transaction t ON t.id = e.transaction_id "
     "WHERE e.business_date <> t.business_date"),
    ("Reversal that does not exactly negate its original",
     "SELECT r.id FROM core_transaction r WHERE r.type = 'REVERSAL' AND EXISTS ("
     " SELECT account_id FROM (SELECT account_id, amount FROM core_entry WHERE transaction_id = r.id UNION ALL "
     " SELECT account_id, amount FROM core_entry WHERE transaction_id = r.reverses_id) x "
     " GROUP BY account_id HAVING SUM(amount) <> 0)"),
    ("Closing line arithmetic",
     "SELECT id FROM core_closingline WHERE expected <> opening_book + movements OR difference <> actual - expected "
     "OR new_difference <> difference - carried_unresolved"),
    ("Whole ledger total (must be zero)",
     "SELECT SUM(amount) FROM core_entry HAVING SUM(amount) <> 0"),
]


class Command(BaseCommand):
    help = "Verify ledger invariants directly in SQL."

    def handle(self, *args, **opts):
        bad = 0
        with connection.cursor() as cur:
            for name, sql in CHECKS:
                cur.execute(sql)
                rows = cur.fetchall()
                if rows:
                    bad += 1
                    self.stdout.write(self.style.ERROR(f"FAIL {name}: {rows[:10]}"))
                else:
                    self.stdout.write(self.style.SUCCESS(f"ok   {name}"))
        if bad:
            raise SystemExit(1)
