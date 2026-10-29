"""Financial scenario tests against the real PostgreSQL schema (triggers on)."""
from datetime import timedelta

from django.db.models import Sum
from django.test import TestCase

from core.models import (
    Account, CashierDay, ClosingLine, Correction, Discrepancy, Entry, ExpenseCategory, PaymentMethod,
    Product, Transaction, TxType,
)
from core.services import closing as cl
from core.services import corrections as corr
from core.services import operations as ops
from core.services.days import douala_today
from core.services.errors import BusinessError, DuplicateSubmission, InsufficientFunds, PermissionDenied

from .helpers import D, OrgMixin, key

K = Account.Kind


def ledger_is_balanced():
    return (Entry.objects.aggregate(s=Sum("amount"))["s"] or 0) == 0


class SuppliesAndPayments(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()

    def test_requirement_examples_and_mixed_methods(self):
        # Supply 100,000 UVE: debt +100,000; selected UVE wallet -100,000
        ops.record_supply(self.c_f1, product=Product.UVE, customer=self.a1, amount=D(100000),
                          source_account=self.uve_f1)
        self.assertEqual(self.cbal(self.a1), D(100000))
        self.assertEqual(self.bal(self.uve_f1), D(900000))
        # Collect 60,000 cash: debt -60,000; drawer +60,000
        ops.record_collection(self.c_f1, payer=self.a1, method=PaymentMethod.CASH, amount=D(60000),
                              account=self.d_f1)
        self.assertEqual(self.cbal(self.a1), D(40000))
        self.assertEqual(self.bal(self.d_f1), D(1060000))
        # Collect 40,000 MTN: debt -40,000; MTN wallet +40,000
        ops.record_collection(self.c_f1, payer=self.a1, method=PaymentMethod.MTN, amount=D(40000),
                              account=self.mtn_f1)
        self.assertEqual(self.cbal(self.a1), D(0))
        self.assertEqual(self.bal(self.mtn_f1), D(40000))
        # ER + cash supplies, paid by UVE and bank deposit -> one combined balance
        ops.record_supply(self.c_f1, product=Product.ER, customer=self.a1, amount=D(50000), source_account=self.er_f1)
        ops.record_supply(self.c_f1, product=Product.CASH, customer=self.a1, amount=D(25000), source_account=self.d_f1)
        self.assertEqual(self.cbal(self.a1), D(75000))
        ops.record_collection(self.c_f1, payer=self.a1, method=PaymentMethod.UVE, amount=D(30000),
                              account=self.uve_f1)
        ops.record_collection(self.c_f1, payer=self.a1, method=PaymentMethod.BANK, amount=D(45000),
                              account=self.bank, bank_reference="DEP-778")
        self.assertEqual(self.cbal(self.a1), D(0))
        self.assertEqual(self.bal(self.bank), D(45000))
        # supply totals by product are separate
        sup = Transaction.objects.filter(type=TxType.SUPPLY, counterparty=self.a1)
        self.assertEqual(sup.filter(product="UVE").aggregate(s=Sum("amount"))["s"], D(100000))
        self.assertEqual(sup.filter(product="ER").aggregate(s=Sum("amount"))["s"], D(50000))
        self.assertEqual(sup.filter(product="CASH").aggregate(s=Sum("amount"))["s"], D(25000))
        # receipts are uniquely numbered for supplies and payments
        rec = Transaction.objects.filter(type__in=[TxType.SUPPLY, TxType.COLLECTION]).values_list("receipt_no",
                                                                                               flat=True)
        self.assertEqual(len(set(rec)), 7)
        self.assertTrue(all(rec))
        self.assertTrue(ledger_is_balanced())

    def test_duplicate_bank_reference_rejected(self):
        ops.record_collection(self.c_f1, payer=self.a1, method="BANK", amount=D(1000), account=self.bank,
                              bank_reference="X1")
        with self.assertRaises(BusinessError):
            ops.record_collection(self.c_f2, payer=self.a2, method="BANK", amount=D(1000), account=self.bank,
                                  bank_reference="x1")

    def test_shared_agent_balance_across_cashiers(self):
        ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(100000), source_account=self.uve_f1)
        ops.record_supply(self.c_me, product="UVE", customer=self.a1, amount=D(20000), source_account=self.uve_me)
        ops.record_collection(self.c_f2, payer=self.a1, method="CASH", amount=D(30000), account=self.d_f2)
        ops.record_collection(self.c_me, payer=self.a1, method="MTN", amount=D(20000), account=self.mtn_me)
        self.assertEqual(self.cbal(self.a1), D(70000))
        cashiers = set(Transaction.objects.filter(counterparty=self.a1).values_list("entered_by__username", flat=True))
        self.assertEqual(cashiers, {"cashier.fako1", "cashier.fako2", "cashier.meme"})

    def test_excess_payment_then_supply(self):
        ops.record_supply(self.c_f1, product="UVE", customer=self.a2, amount=D(100000), source_account=self.uve_f1)
        ops.record_collection(self.c_f1, payer=self.a2, method="CASH", amount=D(120000), account=self.d_f1)
        self.assertEqual(self.cbal(self.a2), D(-20000))  # 20,000 supply due to customer
        pay = Transaction.objects.get(type=TxType.COLLECTION, counterparty=self.a2)
        self.assertEqual(pay.amount, D(120000))  # payment preserved, not capped
        self.assertEqual(Transaction.objects.filter(type=TxType.SUPPLY, counterparty=self.a2).count(), 1)
        ops.record_supply(self.c_f1, product="ER", customer=self.a2, amount=D(15000), source_account=self.er_f1)
        self.assertEqual(self.cbal(self.a2), D(-5000))
        ops.record_supply(self.c_f1, product="UVE", customer=self.a2, amount=D(30000), source_account=self.uve_f1)
        self.assertEqual(self.cbal(self.a2), D(25000))

    def test_payment_before_supply(self):
        ops.record_collection(self.c_f1, payer=self.a3, method="CASH", amount=D(50000), account=self.d_f1)
        self.assertEqual(self.cbal(self.a3), D(-50000))
        ops.record_supply(self.c_me, product="UVE", customer=self.a3, amount=D(50000), source_account=self.uve_me)
        self.assertEqual(self.cbal(self.a3), D(0))

    def test_validation_errors(self):
        with self.assertRaises(BusinessError):  # wrong account for product
            ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(100), source_account=self.d_f1)
        with self.assertRaises(BusinessError):
            ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(0), source_account=self.uve_f1)
        with self.assertRaises(BusinessError):  # fractional FCFA
            ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount="100.5", source_account=self.uve_f1)
        with self.assertRaises(InsufficientFunds):
            ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(2000000),
                              source_account=self.uve_f1)
        with self.assertRaises(BusinessError):  # MTN money must go to an MTN wallet
            ops.record_collection(self.c_f1, payer=self.a1, method="MTN", amount=D(10), account=self.uve_f1)
        with self.assertRaises(BusinessError):  # bank deposit needs a reference
            ops.record_collection(self.c_f1, payer=self.a1, method="BANK", amount=D(10), account=self.bank)


class Destockage(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()

    def test_destockage_then_uve_repayment_no_double_count(self):
        uve_before = self.bal(self.uve_f1)
        t = ops.record_destockage(self.c_f1, pos=self.p1, agent=self.a1, amount=D(100000), drawer=self.d_f1)
        self.assertEqual(self.bal(self.d_f1), D(900000))
        self.assertEqual(self.cbal(self.a1), D(100000))
        self.assertEqual(self.cbal(self.p1), D(0))  # no POS debt
        self.assertEqual(self.bal(self.uve_f1), uve_before)  # company UVE unchanged
        self.assertEqual(t.type, TxType.DESTOCKAGE)
        self.assertEqual((t.counterparty, t.counterparty_2), (self.a1, self.p1))
        self.assertTrue(t.receipt_no)
        ops.record_collection(self.c_f1, payer=self.a1, method="UVE", amount=D(100000), account=self.uve_f1)
        self.assertEqual(self.bal(self.uve_f1), uve_before + D(100000))
        self.assertEqual(self.cbal(self.a1), D(0))
        self.assertEqual(Transaction.objects.filter(type=TxType.DESTOCKAGE).count(), 1)
        self.assertEqual(Transaction.objects.filter(type=TxType.SUPPLY).count(), 0)
        self.assertTrue(ledger_is_balanced())

    def test_destockage_roles_enforced(self):
        with self.assertRaises(BusinessError):
            ops.record_destockage(self.c_f1, pos=self.a2, agent=self.a1, amount=D(1000), drawer=self.d_f1)
        with self.assertRaises(BusinessError):
            ops.record_destockage(self.c_f1, pos=self.p1, agent=self.p2, amount=D(1000), drawer=self.d_f1)
        with self.assertRaises(PermissionDenied):  # another cashier's drawer
            ops.record_destockage(self.c_f1, pos=self.p1, agent=self.a1, amount=D(1000), drawer=self.d_f2)


class PosAndResponsibility(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()

    def assets_snapshot(self):
        return {a.id: self.bal(a) for a in Account.objects.filter(kind__in=Account.ASSET_KINDS)}

    def test_direct_pos_supply_and_full_transfer(self):
        s = ops.record_supply(self.c_f2, product="UVE", customer=self.p1, amount=D(50000), source_account=self.uve_f2)
        self.assertEqual(self.cbal(self.p1), D(50000))
        self.assertEqual(self.cbal(self.a1), D(0))  # no automatic agent liability
        before = self.assets_snapshot()
        t = ops.transfer_responsibility(self.c_f1, supply=s, new_party=self.a1, reason="Agent collected POS money")
        self.assertEqual(self.cbal(self.p1), D(0))
        self.assertEqual(self.cbal(self.a1), D(50000))
        self.assertEqual(self.assets_snapshot(), before)  # company assets unchanged
        self.assertEqual((t.counterparty_2, t.counterparty, t.related_supply), (self.p1, self.a1, s))
        self.assertEqual(t.entered_by, self.c_f1)
        s.refresh_from_db()
        self.assertEqual(s.counterparty, self.p1)  # original supply preserved
        self.assertEqual(ops.current_responsible(s), self.a1)
        with self.assertRaises(BusinessError):  # same obligation cannot move twice
            ops.transfer_responsibility(self.manager, supply=s, new_party=self.a2, reason="again")

    def test_partly_paid_pos_supply_cannot_move_settled_debt(self):
        s = ops.record_supply(self.c_f1, product="UVE", customer=self.p2, amount=D(50000), source_account=self.uve_f1)
        ops.record_collection(self.c_f1, payer=self.p2, method="CASH", amount=D(20000), account=self.d_f1)
        with self.assertRaises(BusinessError) as cm:
            ops.transfer_responsibility(self.manager, supply=s, new_party=self.a1, reason="x")
        self.assertIn("settled", cm.exception.message)
        self.assertEqual(self.cbal(self.p2), D(30000))
        self.assertEqual(self.cbal(self.a1), D(0))

    def test_explicit_assignment_at_entry_and_roles(self):
        t = ops.record_supply(self.c_f1, product="UVE", customer=self.a2, recipient=self.p2, amount=D(40000),
                              source_account=self.uve_f1)
        self.assertEqual(self.cbal(self.a2), D(40000))
        self.assertEqual(self.cbal(self.p2), D(0))
        self.assertEqual(t.counterparty_2, self.p2)
        with self.assertRaises(PermissionDenied):
            ops.transfer_responsibility(self.agent_user, supply=t, new_party=self.a1, reason="x")
        ops.transfer_responsibility(self.finance, supply=t, new_party=self.a1, reason="Agent change")
        self.assertEqual(self.cbal(self.a1), D(40000))

    def test_cancelled_transfer_can_be_redone(self):
        s = ops.record_supply(self.c_f1, product="CASH", customer=self.p1, amount=D(10000), source_account=self.d_f1)
        t = ops.transfer_responsibility(self.c_f1, supply=s, new_party=self.a1, reason="wrong agent")
        with self.assertRaises(BusinessError):  # cannot correct supply while transferred
            corr.correct_transaction(self.manager, s, kind="VOID", reason="x")
        corr.correct_transaction(self.manager, t, kind="VOID", reason="Wrong agent selected")
        self.assertEqual(self.cbal(self.p1), D(10000))
        ops.transfer_responsibility(self.c_f1, supply=s, new_party=self.a2, reason="correct agent")
        self.assertEqual(self.cbal(self.a2), D(10000))
        self.assertEqual(self.cbal(self.a1), D(0))


class CashManagers(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()

    def test_legs_at_different_times(self):
        ex = ops.create_exchange(self.c_f1, cash_manager=self.cm1, amount=D(200000))
        cm_acct = self.cm1.account
        self.assertEqual(self.bal(cm_acct), D(0))  # agreement alone moves nothing
        self.assertEqual(ex.status, "AGREED")
        ops.record_exchange_leg(self.c_f1, exchange=ex, side=TxType.CM_GIVE, amount=D(200000), account=self.d_f1)
        self.assertEqual(self.bal(self.d_f1), D(800000))
        self.assertEqual(self.bal(cm_acct), D(200000))  # cash manager owes company
        self.assertEqual(ex.status, "PARTIAL")
        ops.record_exchange_leg(self.c_f1, exchange=ex, side=TxType.CM_RECEIVE, amount=D(120000),
                                account=self.uve_f1)
        self.assertEqual(self.bal(cm_acct), D(80000))
        with self.assertRaises(BusinessError):  # cannot exceed agreement
            ops.record_exchange_leg(self.c_f1, exchange=ex, side=TxType.CM_RECEIVE, amount=D(90000),
                                    account=self.uve_f1)
        with self.assertRaises(BusinessError):  # only cash drawers and UVE wallets
            ops.record_exchange_leg(self.c_f1, exchange=ex, side=TxType.CM_RECEIVE, amount=D(1000),
                                    account=self.mtn_f1)
        # no direction: the rest may come back as cash and UVE
        ops.record_exchange_leg(self.c_f1, exchange=ex, side=TxType.CM_RECEIVE, amount=D(30000),
                                account=self.d_f1)
        ops.record_exchange_leg(self.c_f1, exchange=ex, side=TxType.CM_RECEIVE, amount=D(50000),
                                account=self.uve_f1)
        self.assertEqual(self.bal(cm_acct), D(0))
        self.assertEqual(ex.status, "COMPLETE")
        self.assertEqual(self.bal(self.uve_f1), D(1170000))
        self.assertEqual(self.bal(self.d_f1), D(830000))
        # never revenue / expense / customer debt
        self.assertEqual(Entry.objects.filter(account__kind__in=[K.EXPENSE, K.CUSTOMER, K.CONVERSION_UPLIFT],
                                              transaction__exchange=ex).count(), 0)

    def test_receive_before_give(self):
        ex = ops.create_exchange(self.c_f2, cash_manager=self.cm2, amount=D(50000))
        ops.record_exchange_leg(self.c_f2, exchange=ex, side=TxType.CM_RECEIVE, amount=D(50000), account=self.d_f2)
        self.assertEqual(self.bal(self.cm2.account), D(-50000))  # company owes cash manager UVE
        ops.record_exchange_leg(self.c_f2, exchange=ex, side=TxType.CM_GIVE, amount=D(50000), account=self.uve_f2)
        self.assertEqual(self.bal(self.cm2.account), D(0))


class Conversion(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()

    def test_conversion_and_rate_history(self):
        t1 = ops.record_conversion(self.c_f1, uve_account=self.uve_f1, amount=D(100000))
        self.assertEqual(t1.amount_received, D(104000))
        self.assertEqual(t1.rate_used, D("0.04"))
        self.assertEqual(self.bal(self.uve_f1), D(900000))
        self.assertEqual(self.bal(self.er_f1), D(1104000))
        self.assertEqual(self.sysbal(K.CONVERSION_UPLIFT), D(-4000))
        with self.assertRaises(PermissionDenied):
            ops.set_conversion_rate(self.manager, percent="5")
        ops.set_conversion_rate(self.admin, percent="5")
        t2 = ops.record_conversion(self.c_f1, uve_account=self.uve_f1, amount=D(10000))
        self.assertEqual(t2.amount_received, D(10500))
        t1.refresh_from_db()
        self.assertEqual((t1.rate_used, t1.amount_received), (D("0.04"), D(104000)))  # history preserved
        # a correction of an old conversion keeps the rate it was made at
        c = corr.correct_transaction(self.manager, t1, kind="REPLACE", reason="Typo", new_values={"amount": D(50000)})
        self.assertEqual(c.replacement_txn.rate_used, D("0.04"))
        self.assertEqual(c.replacement_txn.amount_received, D(52000))

    def test_rounding_half_up(self):
        t = ops.record_conversion(self.c_f1, uve_account=self.uve_f1, amount=D(12345))
        self.assertEqual(t.amount_received, D(12839))  # 12838.80
        t = ops.record_conversion(self.c_f1, uve_account=self.uve_f1, amount=D(12))
        self.assertEqual(t.amount_received, D(12))  # 12.48 -> 12
        t = ops.record_conversion(self.c_f1, uve_account=self.uve_f1, amount=D(25))
        self.assertEqual(t.amount_received, D(26))  # 26.00

    def test_conversion_stays_on_same_sim_and_needs_permission(self):
        t = ops.record_conversion(self.c_f2, uve_account=self.uve_f2, amount=D(1000))
        self.assertEqual(t.dest_account, self.er_f2)
        with self.assertRaises(PermissionDenied):
            ops.record_conversion(self.c_f2, uve_account=self.uve_f1, amount=D(1000))


class ErMasterSim(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()
        self.fund(self.er_f2, 100000)

    def test_er_must_come_from_zone_master(self):
        with self.assertRaises(BusinessError):  # Fako 2's own SIM is not the master
            ops.record_supply(self.c_f2, product="ER", customer=self.a1, amount=D(1000), source_account=self.er_f2)
        with self.assertRaises(PermissionDenied):  # the master SIM is used only by its cashier in charge
            ops.record_supply(self.c_f2, product="ER", customer=self.a1, amount=D(1000), source_account=self.er_f1)
        ops.record_supply(self.c_f1, product="ER", customer=self.a1, amount=D(1000), source_account=self.er_f1)
        with self.assertRaises((BusinessError, PermissionDenied)):  # Meme cashier cannot use the Fako master
            ops.record_supply(self.c_me, product="ER", customer=self.a3, amount=D(1000), source_account=self.er_f1)
        ops.record_supply(self.c_me, product="ER", customer=self.a3, amount=D(1000), source_account=self.er_me)
        # UVE may go from the subzone's Orange SIM, but not from another zone's SIM
        ops.record_supply(self.c_f2, product="UVE", customer=self.a1, amount=D(1000), source_account=self.uve_f2)
        with self.assertRaises(PermissionDenied):
            ops.record_supply(self.c_f2, product="UVE", customer=self.a1, amount=D(1000), source_account=self.uve_f1)


class ExpensesAndTransfers(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()

    def test_expense(self):
        fuel = ExpenseCategory.objects.get(code="fuel")
        pre = ExpenseCategory.objects.get(code="orange-prefinance")
        ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(10000), source_account=self.uve_f1)
        ops.record_expense(self.c_f1, category=fuel, amount=D(5000), drawer=self.d_f1, description="Moto fuel")
        ops.record_expense(self.c_f1, category=pre, amount=D(20000), drawer=self.d_f1, description="Pre-finance")
        self.assertEqual(self.bal(self.d_f1), D(975000))
        self.assertEqual(self.bal(fuel.ledger_account), D(5000))
        self.assertEqual(self.cbal(self.a1), D(10000))  # expenses never touch agent debt
        with self.assertRaises(BusinessError):
            ops.record_expense(self.c_f1, category=fuel, amount=D(5000), drawer=self.d_f1, description="")
        with self.assertRaises(InsufficientFunds):
            ops.record_expense(self.c_f1, category=fuel, amount=D(2000000), drawer=self.d_f1, description="x")

    def test_internal_transfers(self):
        # only MTN float, UVE or bank, and the same kind on both sides
        ops.record_internal_transfer(self.c_f1, source=self.uve_f1, destination=self.uve_me, amount=D(10000))
        self.assertEqual(self.bal(self.uve_me), D(1010000))
        self.fund(self.mtn_f1, 20000)
        ops.record_internal_transfer(self.c_f1, source=self.mtn_f1, destination=self.mtn_f2, amount=D(20000))
        self.assertEqual(self.bal(self.mtn_f2), D(20000))
        for src, dst in ((self.d_f1, self.d_f2), (self.d_f1, self.bank), (self.er_f1, self.er_me),
                         (self.uve_f1, self.mtn_f1)):
            with self.assertRaises(BusinessError):
                ops.record_internal_transfer(self.c_f1, source=src, destination=dst, amount=D(1))
        with self.assertRaises(PermissionDenied):
            ops.record_internal_transfer(self.c_f2, source=self.uve_f1, destination=self.uve_f2, amount=D(1))
        with self.assertRaises(BusinessError):
            ops.record_internal_transfer(self.c_f1, source=self.uve_f1, destination=self.uve_f1, amount=D(1))
        self.assertFalse(Entry.objects.filter(transaction__type=TxType.INTERNAL_TRANSFER,
                                              account__kind__in=[K.CUSTOMER, K.EXPENSE]).exists())
        self.assertTrue(ledger_is_balanced())


class ClosingAndDiscrepancies(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()

    def actuals(self, cashier, overrides=None):
        vals = {a.id: self.bal(a) for a in cl.closing_accounts(cashier)}
        vals.update(overrides or {})
        return vals

    def test_close_carry_forward_and_next_business_day(self):
        t1 = ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(100000), source_account=self.uve_f1)
        day1 = t1.business_date
        self.assertEqual(day1, douala_today())
        closing = cl.close_day(self.c_f1, actuals=self.actuals(self.c_f1))
        self.assertEqual(closing.lines.count(), 4)  # drawer, UVE, ER, MTN separately
        self.assertFalse(Discrepancy.objects.exists())
        line = closing.lines.get(account=self.uve_f1)
        self.assertEqual((line.opening_book, line.movements, line.expected), (D(1000000), D(-100000), D(900000)))
        t2 = ops.record_collection(self.c_f1, payer=self.a1, method="CASH", amount=D(5000), account=self.d_f1)
        self.assertEqual(t2.business_date, day1 + timedelta(days=1))  # next business day
        self.assertGreaterEqual(t2.posted_at, t1.posted_at)  # actual timestamp kept separately
        # a second cashier of the same subzone uses the shared drawer and the same business day
        from core.models import User

        c_f1b = User(username="cashier.fako1b", role="CASHIER", subzone=self.c_f1.subzone)
        c_f1b.set_password("x")
        c_f1b.save()
        t3 = ops.record_expense(c_f1b, category=ExpenseCategory.objects.get(code="fuel"), amount=D(7000),
                                drawer=self.d_f1, description="shared drawer")
        self.assertEqual(t3.cashier_day, t2.cashier_day)
        self.assertEqual((t3.zone, t3.cashier), (self.c_f1.subzone, c_f1b))
        closing2 = cl.close_day(c_f1b, actuals=self.actuals(c_f1b))  # either cashier closes the subzone
        l2 = closing2.lines.get(account=self.d_f1)
        self.assertEqual(l2.opening_book, closing.lines.get(account=self.d_f1).expected)
        self.assertEqual(l2.movements, D(-2000))  # +5,000 payment, -7,000 expense
        self.assertEqual(closing2.lines.count(), 4)
        zone = self.c_f1.subzone
        self.assertEqual(CashierDay.objects.filter(zone=zone, status="CLOSED").count(), 2)
        self.assertEqual(CashierDay.objects.get(zone=zone, status="OPEN").business_date, day1 + timedelta(days=2))
        # other subzones are unaffected
        self.assertEqual(CashierDay.objects.filter(zone=self.c_me.subzone).count(), 0)
        # the agent account is cumulative across closings
        self.assertEqual(self.cbal(self.a1), D(95000))

    def test_discrepancy_investigation_and_resolution(self):
        acts = self.actuals(self.c_f1, {self.d_f1.id: D(995000)})  # 5,000 short
        c1 = cl.close_day(self.c_f1, actuals=acts)
        d = Discrepancy.objects.get()
        self.assertEqual((d.amount, d.status, d.account), (D(-5000), "OPEN", self.d_f1))
        self.assertEqual(self.bal(self.d_f1), D(1000000))  # book is NOT silently adjusted
        # next day still 5,000 short: carried, not duplicated
        c2 = cl.close_day(self.c_f1, actuals=acts)
        l2 = c2.lines.get(account=self.d_f1)
        self.assertEqual((l2.difference, l2.carried_unresolved, l2.new_difference), (D(-5000), D(-5000), D(0)))
        self.assertEqual(Discrepancy.objects.count(), 1)
        cl.add_discrepancy_note(self.c_f1, d, "Recounted twice, still short")
        with self.assertRaises(PermissionDenied):
            cl.resolve_discrepancy(self.c_f1, d, resolution="ADJUSTMENT", notes="x")
        cl.add_discrepancy_note(self.manager, d, "Cashier gave wrong change to a POS")
        d = cl.resolve_discrepancy(self.manager, d, resolution="ADJUSTMENT", notes="Write off shortage")
        self.assertEqual(d.status, "RESOLVED")
        self.assertEqual(d.adjustment_txn.type, TxType.RECON_ADJUSTMENT)
        self.assertEqual(d.adjustment_txn.cashier_day.cashier, self.c_f1)
        self.assertEqual(self.bal(self.d_f1), D(995000))
        self.assertEqual(self.sysbal(K.RECON_ADJUSTMENT), D(5000))
        self.assertEqual(d.notes.count(), 3)
        c3 = cl.close_day(self.c_f1, actuals=acts)
        l3 = c3.lines.get(account=self.d_f1)
        self.assertEqual((l3.difference, l3.carried_unresolved, l3.new_difference), (D(0), D(0), D(0)))
        c1.refresh_from_db()
        self.assertEqual(c1.lines.get(account=self.d_f1).difference, D(-5000))  # original kept
        with self.assertRaises(BusinessError):
            cl.resolve_discrepancy(self.finance, d, resolution="COUNT_ERROR", notes="again")

    def test_surplus_counting_error(self):
        cl.close_day(self.c_me, actuals=self.actuals(self.c_me, {self.mtn_me.id: D(1000)}))
        d = Discrepancy.objects.get()
        self.assertEqual(d.amount, D(1000))
        cl.resolve_discrepancy(self.finance, d, resolution="COUNT_ERROR", notes="Misread SIM balance")
        self.assertEqual(self.bal(self.mtn_me), D(0))  # no invented movement

    def test_closing_requires_every_account(self):
        acts = self.actuals(self.c_f1)
        acts.pop(self.mtn_f1.id)
        with self.assertRaises(BusinessError):
            cl.close_day(self.c_f1, actuals=acts)
        with self.assertRaises(PermissionDenied):
            cl.close_day(self.manager, actuals={})


class Corrections(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()

    def test_replace_supply_amount(self):
        s = ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(100000), source_account=self.uve_f1)
        with self.assertRaises(PermissionDenied):
            corr.correct_transaction(self.c_f1, s, kind="REPLACE", reason="x", new_values={"amount": D(80000)})
        c = corr.correct_transaction(self.manager, s, kind="REPLACE", reason="Wrong amount typed",
                                     new_values={"amount": D(80000)})
        s.refresh_from_db()
        self.assertTrue(s.is_corrected)
        self.assertEqual(s.amount, D(100000))  # original retained
        self.assertEqual(c.changes, {"amount": ["100000", "80000"]})
        self.assertEqual(c.reversal_txn.reverses, s)
        self.assertEqual(c.replacement_txn.replaces, s)
        self.assertTrue(c.replacement_txn.receipt_no)
        self.assertEqual(self.cbal(self.a1), D(80000))
        self.assertEqual(self.bal(self.uve_f1), D(920000))
        with self.assertRaises(BusinessError):
            corr.correct_transaction(self.finance, s, kind="VOID", reason="again")
        with self.assertRaises(BusinessError):
            corr.correct_transaction(self.finance, c.reversal_txn, kind="VOID", reason="no")
        self.assertTrue(ledger_is_balanced())

    def test_change_customer_and_void_payment(self):
        s = ops.record_supply(self.c_f1, product="ER", customer=self.a1, amount=D(30000), source_account=self.er_f1)
        corr.correct_transaction(self.finance, s, kind="REPLACE", reason="Wrong agent", new_values={"customer": self.a2})
        self.assertEqual((self.cbal(self.a1), self.cbal(self.a2)), (D(0), D(30000)))
        p = ops.record_collection(self.c_f2, payer=self.a2, method="CASH", amount=D(30000), account=self.d_f2)
        corr.correct_transaction(self.manager, p, kind="VOID", reason="Duplicate entry")
        self.assertEqual(self.cbal(self.a2), D(30000))
        self.assertEqual(self.bal(self.d_f2), D(1000000))

    def test_correction_of_closed_day_posts_in_current_day(self):
        e = ops.record_expense(self.c_f1, category=ExpenseCategory.objects.get(code="transport"), amount=D(10000),
                               drawer=self.d_f1, description="Taxi")
        closing = cl.close_day(self.c_f1, actuals={a.id: self.bal(a) for a in cl.closing_accounts(self.c_f1)})
        closed_lines = list(closing.lines.values_list("account_id", "expected", "actual"))
        c = corr.correct_transaction(self.manager, e, kind="REPLACE", reason="Was 8,000",
                                     new_values={"amount": D(8000)})
        self.assertEqual(c.reversal_txn.business_date, e.business_date + timedelta(days=1))
        self.assertEqual(c.reversal_txn.original_business_date, e.business_date)
        self.assertEqual(c.replacement_txn.cashier_day.cashier, self.c_f1)
        self.assertEqual(list(ClosingLine.objects.filter(closing=closing).values_list(
            "account_id", "expected", "actual")), closed_lines)  # established closing untouched
        self.assertEqual(self.bal(self.d_f1), D(992000))
        nxt = cl.close_day(self.c_f1, actuals={a.id: self.bal(a) for a in cl.closing_accounts(self.c_f1)})
        self.assertEqual(nxt.lines.get(account=self.d_f1).movements, D(2000))

    def test_correction_cannot_overspend(self):
        p = ops.record_collection(self.c_f1, payer=self.a1, method="MTN", amount=D(10000), account=self.mtn_f1)
        ops.record_internal_transfer(self.c_f1, source=self.mtn_f1, destination=self.mtn_f2, amount=D(10000))
        with self.assertRaises(InsufficientFunds):
            corr.correct_transaction(self.manager, p, kind="VOID", reason="no money left to reverse")
        self.assertFalse(Correction.objects.exists())

    def test_cash_manager_leg_correction_respects_agreement(self):
        ex = ops.create_exchange(self.c_f1, cash_manager=self.cm1, amount=D(100000))
        leg = ops.record_exchange_leg(self.c_f1, exchange=ex, side=TxType.CM_GIVE, amount=D(60000), account=self.d_f1)
        corr.correct_transaction(self.manager, leg, kind="REPLACE", reason="Gave all", new_values={"amount": D(100000)})
        self.assertEqual(ex.side_totals(), (D(100000), 0))
        self.assertEqual(self.bal(self.cm1.account), D(100000))


class OpeningBalances(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()

    def test_opening_rules(self):
        ops.record_opening_balance(self.admin, account=self.a1.account, amount=D(250000))
        ops.record_opening_balance(self.admin, account=self.p1.account, amount=D(-15000))  # supply due
        ops.record_opening_balance(self.admin, account=self.cm1.account, amount=D(-40000))
        ops.record_opening_balance(self.admin, account=self.d_f1, amount=D(300000))
        self.assertEqual((self.cbal(self.a1), self.cbal(self.p1)), (D(250000), D(-15000)))
        with self.assertRaises(BusinessError):  # no duplicate initialisation
            ops.record_opening_balance(self.admin, account=self.d_f1, amount=D(300000))
        with self.assertRaises(BusinessError):
            ops.record_opening_balance(self.admin, account=self.d_f2, amount=D(-1))
        with self.assertRaises(PermissionDenied):
            ops.record_opening_balance(self.manager, account=self.d_f2, amount=D(1))
        ob = Transaction.objects.get(opening_for_account=self.d_f1)
        c = corr.correct_transaction(self.finance, ob, kind="REPLACE", reason="Counted 320k",
                                     new_values={"amount": D(320000)})
        self.assertEqual(self.bal(self.d_f1), D(320000))
        self.assertIsNone(c.replacement_txn.opening_for_account)
        # an opening debt of an agent can be transferred like a supply
        t = Transaction.objects.get(opening_for_account=self.a1.account)
        ops.transfer_responsibility(self.manager, supply=t, new_party=self.a2, reason="Agent takeover")
        self.assertEqual((self.cbal(self.a1), self.cbal(self.a2)), (D(0), D(250000)))


class ServicePermissions(OrgMixin, TestCase):
    def setUp(self):
        self.make_org()
        self.fund_all()

    def test_non_cashiers_cannot_enter_operations(self):
        for u in (self.agent_user, self.manager, self.finance, self.admin):
            with self.assertRaises(PermissionDenied):
                ops.record_supply(u, product="UVE", customer=self.a1, amount=D(1), source_account=self.uve_f1)
            with self.assertRaises(PermissionDenied):
                ops.record_collection(u, payer=self.a1, method="CASH", amount=D(1), account=self.d_f1)

    def test_duplicate_submission_sequential(self):
        k = key()
        t = ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(100), source_account=self.uve_f1,
                              idempotency_key=k)
        with self.assertRaises(DuplicateSubmission) as cm:
            ops.record_supply(self.c_f1, product="UVE", customer=self.a1, amount=D(100),
                              source_account=self.uve_f1, idempotency_key=k)
        self.assertEqual(cm.exception.existing, t)
        self.assertEqual(self.cbal(self.a1), D(100))


class ExchangeServedLater(OrgMixin, TestCase):
    """We need 10M, give 10M, are served 8M now; the 2M balance is served later."""

    def setUp(self):
        self.make_org()
        self.fund(self.d_f1, 10_000_000)
        self.fund(self.uve_f1, 1_000_000)

    def test_give_10m_served_8m_then_balance(self):
        from datetime import timedelta

        from core.services.days import douala_today

        before_cash, before_uve = self.bal(self.d_f1), self.bal(self.uve_f1)
        ex = ops.record_exchange(self.c_f1, cash_manager=self.cm1, amount=D(10_000_000), give_kind="CASH",
                                 give_account=self.d_f1,
                                 amount_given=D(10_000_000), receive_account=self.uve_f1,
                                 amount_served=D(8_000_000), served_on=douala_today(), external_ref="BK-778",
                                 note="weekly float")
        self.assertEqual((ex.external_ref, ex.to_serve, ex.status), ("BK-778", D(2_000_000), "PARTIAL"))
        self.assertEqual(self.bal(self.d_f1), before_cash - 10_000_000)
        self.assertEqual(self.bal(self.uve_f1), before_uve + 8_000_000)
        self.assertEqual(self.bal(self.cm1.account), D(2_000_000))  # the cash manager owes the company 2M
        ops.record_exchange_leg(self.c_f1, exchange=ex, side=TxType.CM_RECEIVE, amount=D(2_000_000),
                                account=self.uve_f1)
        ex.refresh_from_db()
        self.assertEqual((ex.to_serve, ex.status), (D(0), "COMPLETE"))
        with self.assertRaises(BusinessError):
            ops.record_exchange(self.c_f1, cash_manager=self.cm1, amount=D(100), give_kind="CASH",
                                give_account=self.d_f1,
                                amount_given=D(100), receive_account=self.uve_f1, amount_served=D(50),
                                served_on=douala_today() + timedelta(days=1))

    def test_form_page(self):
        import uuid

        c = self.client_class()
        c.login(username="cashier.fako1", password="Sample-Pass-2026!")
        page = c.get("/cash-managers/exchange/new/")
        for txt in ("Ref number", "Amount served", "Date served", "Served into", "Comment"):
            self.assertContains(page, txt)
        r = c.post("/cash-managers/exchange/new/", {
            "idem": str(uuid.uuid4()), "cash_manager": self.cm2.pk, "external_ref": "CM-1", "amount": "10 000 000",
            "give_kind": "CASH", "amount_given": "", "served_on": "2026-10-28",
            "amount_served": "8 000 000", "note": ""})  # accounts filled automatically
        self.assertEqual(r.status_code, 302, r.content[:2000])
        detail = c.get(r["Location"])
        self.assertContains(detail, "Balance to be served")
        self.assertContains(detail, "2 000 000")

    def test_sides_follow_what_is_given(self):
        self.fund(self.mtn_f1, 500_000)
        # MTN float given -> served in UVE, accounts picked automatically for the subzone
        ex = ops.record_exchange(self.c_f1, cash_manager=self.cm1, amount=D(500_000), give_kind="MTN",
                                 amount_served=D(300_000))
        self.assertEqual((ex.direction, ex.give_kind(), ex.receive_kind()), ("MTN_FOR_UVE", "MTN", "UVE"))
        self.assertEqual(self.bal(self.mtn_f1), D(0))
        # the balance must come back in UVE, not cash
        with self.assertRaises(BusinessError):
            ops.record_exchange_leg(self.c_f1, exchange=ex, side=TxType.CM_RECEIVE, amount=D(200_000),
                                    account=self.d_f1)
        ops.record_exchange_leg(self.c_f1, exchange=ex, side=TxType.CM_RECEIVE, amount=D(200_000),
                                account=self.uve_f1)
        # UVE given -> served in cash
        ex2 = ops.record_exchange(self.c_f1, cash_manager=self.cm2, amount=D(100_000), give_kind="UVE",
                                  amount_served=D(100_000))
        self.assertEqual((ex2.direction, ex2.status), ("UVE_FOR_CASH", "COMPLETE"))
        with self.assertRaises(BusinessError):  # cash cannot be "given" on a UVE exchange
            ops.record_exchange(self.c_f1, cash_manager=self.cm2, amount=D(1), give_kind="UVE",
                                give_account=self.d_f1)
