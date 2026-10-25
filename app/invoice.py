"""PDF-накладная по продаже. Агент получает её в чат и пересылает клиенту."""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import config

FONTS = Path(__file__).resolve().parent / "fonts"
pdfmetrics.registerFont(TTFont("DejaVu", str(FONTS / "DejaVuSans.ttf")))          # кириллица
pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(FONTS / "DejaVuSans-Bold.ttf")))
pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold", italic="DejaVu", boldItalic="DejaVu-Bold")
BISHKEK = timezone(timedelta(hours=6))

PAY = {"cash": "наличными", "transfer": "переводом", "debt": "в долг", "part": "частично"}


def num(x) -> str:
    v = float(x)
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return s[:-3] if s.endswith(",00") else s


def build(sale: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"Накладная №{sale['no']}", author=config.COMPANY_NAME)
    base = ParagraphStyle("b", fontName="DejaVu", fontSize=10, leading=14)
    small = ParagraphStyle("s", parent=base, fontSize=8.5, leading=11, textColor=colors.HexColor("#555555"))
    h1 = ParagraphStyle("h", parent=base, fontName="DejaVu-Bold", fontSize=15, leading=19)

    created = datetime.fromisoformat(sale["created_at"]).astimezone(BISHKEK)
    story = [
        Paragraph(config.COMPANY_NAME, small),
        Paragraph(f"Расходная накладная №{sale['no']} от {created:%d.%m.%Y %H:%M}", h1),
        Spacer(1, 4 * mm),
        Paragraph(f"<b>Покупатель:</b> {sale['client_name']}"
                  + (f", {sale['client_place']}" if sale.get("client_place") else "")
                  + (f", тел. {sale['client_phone']}" if sale.get("client_phone") else ""), base),
        Paragraph(f"<b>Торговый агент:</b> {sale['agent_name']}", base),
        Spacer(1, 5 * mm),
    ]

    head = ["№", "Товар", "Кол-во", "Ед.", "Цена", "Скидка", "Сумма"]
    rows = [head]
    for i, it in enumerate(sale["items"], 1):
        d = float(it["discount_pct"])
        rows.append([str(i), Paragraph(it["name"], base), str(it["qty"]), it["unit"], num(it["price"]),
                     (num(d) + "%") if d else "—", num(it["amount"])])
    t = Table(rows, colWidths=[8 * mm, 74 * mm, 16 * mm, 14 * mm, 20 * mm, 16 * mm, 30 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "DejaVu", 9.5),
        ("FONT", (0, 0), (-1, 0), "DejaVu-Bold", 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEEAE3")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#BBBBBB")),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("ALIGN", (3, 1), (3, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [t, Spacer(1, 4 * mm)]

    total, base_total, paid = float(sale["total"]), float(sale["base_total"]), float(sale["paid_now"])
    sums = []
    if base_total - total > 0.004:
        sums.append(["Сумма без скидки:", num(base_total)])
        sums.append(["Скидка:", num(base_total - total)])
    total_row = len(sums)
    sums.append(["Итого к оплате:", num(total) + " сом"])
    sums.append([f"Оплачено ({PAY[sale['pay_type']]}):", num(paid)])
    if total - paid > 0.004:
        sums.append(["В долг по этой накладной:", num(total - paid)])
    st = Table(sums, colWidths=[140 * mm, 38 * mm])
    st.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "DejaVu", 10),
        ("FONT", (0, total_row), (-1, total_row), "DejaVu-Bold", 11),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
    ]))
    story += [st, Spacer(1, 12 * mm)]

    sign = Table([["Отпустил: ____________________", "Получил: ____________________"],
                  [f"({sale['agent_name']})", "(подпись покупателя)"]], colWidths=[89 * mm, 89 * mm])
    sign.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), "DejaVu", 10),
                              ("FONT", (0, 1), (-1, 1), "DejaVu", 8),
                              ("TEXTCOLOR", (0, 1), (-1, 1), colors.HexColor("#666666"))]))
    story += [sign, Spacer(1, 8 * mm),
              Paragraph(f"Общий долг покупателя после этой накладной: {num(sale['debt_after'])} сом", small)]
    doc.build(story)
    return buf.getvalue()
