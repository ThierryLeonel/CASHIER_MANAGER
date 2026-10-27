"""Concurrency, idempotency and database-level integrity (real commits)."""
import threading
from decimal import Decimal

from django.db import IntegrityError, InternalError, connection, connections, transaction
from django.db.models import Sum
from django.test import TransactionTestCase

from core.models import Entry, ExpenseCategory, Transaction
from core.services import operations as ops
from core.services.errors import DuplicateSubmission, InsufficientFunds

from .helpers import D, OrgMixin, key


def run_threads(n, fn):
    results, errors = [], []
    barrier = threading.Barrier(n)

    def worker(i):
        try:
            barrier.wait()
            results.append(fn(i))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results, errors


class Concurrency(OrgMixin, TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        self.make_org()
        self.fund(self.d_f1, 100000)
        self.fuel, _ = ExpenseCategory.objects.get_or_create(code="fuel", defaults=dict(name_en="Fuel", name_fr="Carburant"))

    def test_no_overspending_same_drawer(self):
        def spend(i):
            return ops.record_expense(self.c_f1, category=self.fuel, amount=D(30000), drawer=self.d_f1,
                                      description=f"parallel {i}")

        results, errors = run_threads(6, spend)
        self.assertEqual(len(results), 3)
        self.assertEqual(len(errors), 3)
        self.assertTrue(all(isinstance(e, InsufficientFunds) for e in errors), errors)
        self.assertEqual(self.bal(self.d_f1), D(10000))

    def test_concurrent_duplicate_submission(self):
        k = key()

        def submit(i):
            return ops.record_supply(self.c_f1, product="CASH", customer=self.a1, amount=D(20000),
                                     source_account=self.d_f1, idempotency_key=k)

        results, errors = run_threads(5, submit)
        self.assertEqual(len(results), 1, errors)
        self.assertTrue(all(isinstance(e, DuplicateSubmission) for e in errors), errors)
        self.assertEqual(Transaction.objects.filter(idempotency_key=k).count(), 1)
        self.assertEqual(self.cbal(self.a1), D(20000))
        self.assertEqual(self.bal(self.d_f1), D(80000))

    def test_parallel_cashiers_shared_agent(self):
        self.fund(self.d_f2, 100000)
        self.fund(self.d_me, 100000)
        cashiers = [(self.c_f1, self.d_f1), (self.c_f2, self.d_f2), (self.c_me, self.d_me)] * 4

        def pay(i):
            c, d = cashiers[i]
            return ops.record_collection(c, payer=self.a1, method="CASH", amount=D(1000), account=d)

        results, errors = run_threads(len(cashiers), pay)
        self.assertEqual(errors, [])
        self.assertEqual(self.cbal(self.a1), D(-12000))


class DatabaseIntegrity(OrgMixin, TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        self.make_org()
        self.fund(self.d_f1, 1000)

    def test_entries_are_append_only(self):
        e = Entry.objects.first()
        with self.assertRaises((IntegrityError, InternalError)):
            with transaction.atomic():
                Entry.objects.filter(pk=e.pk).update(amount=D(5))
        with self.assertRaises((IntegrityError, InternalError)):
            with transaction.atomic():
                Transaction.objects.filter(pk=e.transaction_id).delete()

    def test_unbalanced_insert_rejected_at_commit(self):
        t = Transaction.objects.first()
        with self.assertRaises((IntegrityError, InternalError)):
            with transaction.atomic():
                Entry.objects.create(transaction=t, account=self.d_f1, amount=D(7), business_date=t.business_date)
        self.assertEqual(Entry.objects.aggregate(s=Sum("amount"))["s"], 0)

    def test_negative_asset_rejected_by_database(self):
        t = Transaction.objects.first()
        with self.assertRaises((IntegrityError, InternalError)):
            with transaction.atomic():
                Entry.objects.create(transaction=t, account=self.d_f1, amount=D(-5000), business_date=t.business_date)
                Entry.objects.create(transaction=t, account=self.a1.account, amount=D(5000),
                                     business_date=t.business_date)
        with connection.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(amount),0) FROM core_entry WHERE account_id=%s", [self.d_f1.id])
            self.assertEqual(cur.fetchone()[0], Decimal(1000))
