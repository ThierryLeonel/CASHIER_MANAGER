"""Closings, discrepancies, corrections and audit events."""
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .ledger import Account, CashierDay, Transaction
from .org import MONEY, User


class Closing(models.Model):
    cashier_day = models.OneToOneField(CashierDay, on_delete=models.PROTECT, related_name="closing")
    closed_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    closed_at = models.DateTimeField(default=timezone.now)
    last_entry_id = models.BigIntegerField(help_text="Highest ledger entry id included in expected balances.")
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-closed_at"]

    def __str__(self):
        return f"Closing {self.cashier_day}"


class ClosingLine(models.Model):
    """Per-account comparison. Book (expected) balances carry forward;
    differences remain open Discrepancy records until resolved."""

    closing = models.ForeignKey(Closing, on_delete=models.PROTECT, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="closing_lines")
    opening_book = models.DecimalField(**MONEY)
    movements = models.DecimalField(**MONEY)
    expected = models.DecimalField(**MONEY)
    actual = models.DecimalField(**MONEY)
    difference = models.DecimalField(**MONEY)  # actual - expected
    carried_unresolved = models.DecimalField(**MONEY)  # open differences from earlier closings
    new_difference = models.DecimalField(**MONEY)  # difference - carried_unresolved

    class Meta:
        ordering = ["account__kind", "account__name"]
        constraints = [models.UniqueConstraint(fields=["closing", "account"], name="one_line_per_account_per_closing")]


class Discrepancy(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", _("Open – under investigation")
        RESOLVED = "RESOLVED", _("Resolved")

    class Resolution(models.TextChoices):
        ADJUSTMENT = "ADJUSTMENT", _("Post reconciliation adjustment (write-off / recognise)")
        CORRECTION = "CORRECTION", _("Explained by a transaction correction")
        COUNT_ERROR = "COUNT_ERROR", _("Counting/reading error – no posting")

    closing_line = models.OneToOneField(ClosingLine, on_delete=models.PROTECT, related_name="discrepancy")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="discrepancies")
    amount = models.DecimalField(**MONEY, help_text="Positive = surplus, negative = shortage")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(default=timezone.now)
    resolution = models.CharField(max_length=12, choices=Resolution.choices, blank=True)
    resolution_notes = models.TextField(blank=True)
    resolved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    adjustment_txn = models.OneToOneField(Transaction, null=True, blank=True, on_delete=models.PROTECT,
                                          related_name="resolved_discrepancy")
    correction = models.ForeignKey("Correction", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="resolved_discrepancies")

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "discrepancies"
        constraints = [
            models.CheckConstraint(name="discrepancy_nonzero", condition=~Q(amount=0)),
            models.CheckConstraint(
                name="resolved_has_details",
                condition=Q(status="OPEN") | (Q(resolved_by__isnull=False) & Q(resolved_at__isnull=False) & ~Q(resolution="")),
            ),
        ]

    @property
    def kind_label(self):
        return _("Surplus") if self.amount > 0 else _("Shortage")


class DiscrepancyNote(models.Model):
    discrepancy = models.ForeignKey(Discrepancy, on_delete=models.PROTECT, related_name="notes")
    author = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    text = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at"]


class Correction(models.Model):
    class Kind(models.TextChoices):
        VOID = "VOID", _("Cancel (reverse only)")
        REPLACE = "REPLACE", _("Reverse and replace with corrected values")

    original = models.OneToOneField(Transaction, on_delete=models.PROTECT, related_name="correction")
    kind = models.CharField(max_length=7, choices=Kind.choices)
    reason = models.CharField(max_length=255)
    corrected_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    reversal_txn = models.OneToOneField(Transaction, on_delete=models.PROTECT, related_name="reversal_of_correction")
    replacement_txn = models.OneToOneField(Transaction, null=True, blank=True, on_delete=models.PROTECT,
                                           related_name="replacement_of_correction")
    changes = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]


class AuditEvent(models.Model):
    """Non-ledger events: configuration and rate changes, logins, access."""

    at = models.DateTimeField(default=timezone.now, db_index=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=60)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-at"]
