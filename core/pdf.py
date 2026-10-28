"""A4 PDF output: receipts and report exports (ReportLab)."""
import io
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from core.money import fmt

FONT_DIR = Path(__file__).parent / "fonts"
_registered = False


def _fonts():
    global _registered
    if not _registered:
        pdfmetrics.registerFont(TTFont("DV", str(FONT_DIR / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DV-B", str(FONT_DIR / "DejaVuSans-Bold.ttf")))
        pdfmetrics.registerFont(TTFont("DVC", str(FONT_DIR / "DejaVuSansCondensed.ttf")))
        pdfmetrics.registerFont(TTFont("DVC-B", str(FONT_DIR / "DejaVuSansCondensed-Bold.ttf")))
        _registered = True


def _styles(condensed=False):
    f, fb = ("DVC", "DVC-B") if condensed else ("DV", "DV-B")
    return {
        "title": ParagraphStyle("t", fontName=fb, fontSize=15, leading=19, spaceAfter=4),
        "h": ParagraphStyle("h", fontName=fb, fontSize=10.5, leading=13, spaceBefore=8, spaceAfter=3),
        "n": ParagraphStyle("n", fontName=f, fontSize=9, leading=11.5),
        "s": ParagraphStyle("s", fontName=f, fontSize=7, leading=8.6),
        "sb": ParagraphStyle("sb", fontName=fb, fontSize=7, leading=8.6),
        "big": ParagraphStyle("big", fontName=fb, fontSize=20, leading=24),
    }


def _local(dt):
    return timezone.localtime(dt).strftime("%d/%m/%Y %H:%M:%S") if dt else ""


# ------------------------------------------------------------------ receipts
def receipt_pdf(txn, rows, balance_after=None):
    """rows: [(label, value)] already localised."""
    _fonts()
    st = _styles()
    buf = io.BytesIO()
    corrected = getattr(txn, "correction", None) if hasattr(txn, "correction") else None

    def stamp(canvas, doc):
        canvas.saveState()
        canvas.setFont("DV", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(18 * mm, 10 * mm, f"{settings.COMPANY_NAME} · {txn.reference} · "
                                            + _("Printed %(when)s") % {"when": _local(timezone.now())})
        if corrected:
            canvas.translate(105 * mm, 150 * mm)
            canvas.rotate(28)
            canvas.setStrokeColor(colors.HexColor("#c62828"))
            canvas.setFillColor(colors.HexColor("#c62828"))
            canvas.setLineWidth(3)
            canvas.roundRect(-78 * mm, -14 * mm, 156 * mm, 30 * mm, 4 * mm, stroke=1, fill=0)
            canvas.setFont("DV-B", 34)
            canvas.drawCentredString(0, 2 * mm, _("CORRECTED"))
            canvas.setFont("DV", 9)
            reason = corrected.reason[:80]
            canvas.drawCentredString(0, -8 * mm, f"{_('Reason')}: {reason}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title=f"{_('Receipt')} {txn.receipt_no or txn.reference}",
                            author=settings.COMPANY_NAME)
    story = [
        Paragraph(settings.COMPANY_NAME, st["title"]),
        Paragraph(settings.COMPANY_ADDRESS, st["n"]),
        Spacer(1, 6 * mm),
        Paragraph(f"{_('RECEIPT')} / {str(txn.display_type).upper()}", st["h"]),
        Paragraph(f"{_('Receipt no.')} <b>{txn.receipt_no or '—'}</b> &nbsp;&nbsp; {_('Reference')} "
                  f"<b>{txn.reference}</b>", st["n"]),
        Spacer(1, 4 * mm),
        Paragraph(f"{fmt(txn.amount)} FCFA", st["big"]),
        Spacer(1, 4 * mm),
    ]
    data = [[Paragraph(f"<b>{k}</b>", st["n"]), Paragraph(str(v), st["n"])] for k, v in rows]
    if balance_after is not None:
        lbl = _("Customer balance after this transaction")
        val = (f"{fmt(balance_after)} FCFA " + (_("owed to the company") if balance_after > 0 else
                                                  _("supply due to customer") if balance_after < 0 else ""))
        data.append([Paragraph(f"<b>{lbl}</b>", st["n"]), Paragraph(val, st["n"])])
    t = Table(data, colWidths=[62 * mm, 112 * mm])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f4f4f4")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [t, Spacer(1, 18 * mm)]
    sig = Table([[Paragraph(_("Cashier signature"), st["n"]), Paragraph(_("Customer signature"), st["n"])],
                 ["", ""]], colWidths=[87 * mm, 87 * mm], rowHeights=[6 * mm, 22 * mm])
    sig.setStyle(TableStyle([("BOX", (0, 1), (0, 1), 0.4, colors.grey), ("BOX", (1, 1), (1, 1), 0.4, colors.grey)]))
    story += [sig, Spacer(1, 6 * mm),
              Paragraph(_("This receipt confirms a completed transaction recorded in the company ledger. "
                          "Amounts in FCFA (XAF)."), st["s"])]
    doc.build(story, onFirstPage=stamp, onLaterPages=stamp)
    return buf.getvalue()


# ------------------------------------------------------------------ reports
def cell_text(value, kind):
    if value is None or value == "":
        return ""
    if kind == "money":
        return fmt(value)
    if kind == "date":
        return value.strftime("%d/%m/%Y")
    if kind == "datetime":
        return timezone.localtime(value).strftime("%d/%m/%Y %H:%M")
    if kind == "rate":
        return f"{(Decimal(value) * 100).normalize()}%"
    return str(value)


def report_pdf(report, generated_by):
    _fonts()
    st = _styles(condensed=True)
    buf = io.BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=10 * mm, rightMargin=10 * mm, topMargin=12 * mm,
                            bottomMargin=12 * mm, title=str(report.title), author=settings.COMPANY_NAME)
    width = page[0] - 20 * mm

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("DVC", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(10 * mm, 6 * mm, f"{settings.COMPANY_NAME} · {report.title} · "
                                           f"{_('Generated')} {_local(timezone.now())} · {generated_by}")
        canvas.drawRightString(page[0] - 10 * mm, 6 * mm, f"{_('Page')} {doc.page}")
        canvas.restoreState()

    story = [Paragraph(f"{settings.COMPANY_NAME} — {report.title}", st["title"]),
             Paragraph(report.filters_text, st["n"]), Spacer(1, 3 * mm)]
    if report.summary:
        data = [[Paragraph(str(lbl), st["s"]), Paragraph(cell_text(v, k), st["sb"])] for lbl, v, k in report.summary]
        t = Table(data, colWidths=[90 * mm, 70 * mm], hAlign="LEFT")
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
                               ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f5f5f5"))]))
        story += [t, Spacer(1, 3 * mm)]
    for table in report.tables:
        story.append(Paragraph(str(table.title), st["h"]))
        if not table.columns:
            continue
        head = [Paragraph(f"<b>{c.label}</b>", st["s"]) for c in table.columns]
        body = []
        for r in table.rows:
            line = []
            for c in table.columns:
                txt = cell_text(r.get(c.key), c.kind)
                if c.key == "reference" and r.get("corrected"):
                    txt += " ⓒ"
                line.append(Paragraph(txt, st["s"]))
            body.append(line)
        tot = table.totals()
        if tot is not None and table.rows:
            body.append([Paragraph(f"<b>{cell_text(tot[c.key], c.kind)}</b>" if c.key in tot else
                                   (f"<b>{_('Total')}</b>" if i == 0 else ""), st["s"])
                         for i, c in enumerate(table.columns)])
        if not body:
            body = [[Paragraph(_("No data for these filters."), st["s"])] + [""] * (len(table.columns) - 1)]
        weights = [3.2 if c.key in ("details", "account", "changes", "reason") else
                   1.3 if c.kind in ("money", "int", "rate") else 1.6 for c in table.columns]
        tw = sum(weights)
        t = Table([head] + body, colWidths=[width * w / tw for w in weights], repeatRows=1)
        style = [("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c8c8c8")),
                 ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                 ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]
        for i, c in enumerate(table.columns):
            if c.kind in ("money", "int", "rate"):
                style.append(("ALIGN", (i, 0), (i, -1), "RIGHT"))
        if tot is not None and table.rows:
            style.append(("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f7f7f7")))
        t.setStyle(TableStyle(style))
        story += [t, Spacer(1, 3 * mm)]
    story.append(Paragraph(_("ⓒ = transaction later corrected (see corrections report). Amounts in FCFA."), st["s"]))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
