#!/usr/bin/env python3
"""Build the two Rebuy PDF manuals.

    python3 scripts/make_manuals.py

Writes:
    docs/Rebuy-Shop-Manual.pdf
    docs/Rebuy-Admin-Manual.pdf

Content is written to describe what the product actually does — pages, routes
and limits are taken from the code, not invented.
"""
from __future__ import annotations

import os
import sys

from reportlab.graphics.shapes import Circle, Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Flowable,
                                Frame, KeepTogether, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle)

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")

# ---------------------------------------------------------------- palette
INK = colors.HexColor("#141821")
BODY = colors.HexColor("#3d4553")
MUTED = colors.HexColor("#6b7484")
LINE = colors.HexColor("#e3e6ec")
BRAND = colors.HexColor("#5b3df5")
BRAND_D = colors.HexColor("#3a24ab")
BRAND_L = colors.HexColor("#f1eeff")
GREEN = colors.HexColor("#0d9f6e")
GREEN_L = colors.HexColor("#e8f8f1")
AMBER = colors.HexColor("#b7791f")
AMBER_L = colors.HexColor("#fdf6e7")
RED = colors.HexColor("#c0392b")
RED_L = colors.HexColor("#fdecea")
BLUE = colors.HexColor("#2563eb")
BLUE_L = colors.HexColor("#eaf1fe")
PANEL = colors.HexColor("#f7f8fa")
WHITE = colors.white

PAGE_W, PAGE_H = A4
M = 20 * mm


# ---------------------------------------------------------------- styles
def styles():
    ss = getSampleStyleSheet()
    s = {}
    s["body"] = ParagraphStyle("body", parent=ss["BodyText"], fontName="Helvetica",
                               fontSize=9.6, leading=14.4, textColor=BODY,
                               spaceAfter=7, alignment=TA_LEFT)
    s["body_b"] = ParagraphStyle("body_b", parent=s["body"], fontName="Helvetica-Bold",
                                 textColor=INK)
    s["small"] = ParagraphStyle("small", parent=s["body"], fontSize=8.4, leading=12.2,
                                textColor=MUTED, spaceAfter=5)
    s["h1"] = ParagraphStyle("h1", parent=ss["Heading1"], fontName="Helvetica-Bold",
                             fontSize=27, leading=31, textColor=INK, spaceAfter=0)
    s["h1sub"] = ParagraphStyle("h1sub", parent=s["body"], fontSize=12.5, leading=18,
                                textColor=BRAND, spaceBefore=6, spaceAfter=0)
    s["h2"] = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold",
                             fontSize=15.5, leading=19, textColor=INK,
                             spaceBefore=4, spaceAfter=2)
    s["h3"] = ParagraphStyle("h3", parent=ss["Heading3"], fontName="Helvetica-Bold",
                             fontSize=11, leading=14.5, textColor=INK,
                             spaceBefore=2, spaceAfter=2)
    s["num"] = ParagraphStyle("num", parent=s["body"], fontName="Helvetica-Bold",
                              fontSize=14, leading=17, textColor=WHITE, spaceAfter=0)
    s["kicker"] = ParagraphStyle("kicker", parent=s["body"], fontName="Helvetica-Bold",
                                 fontSize=7.6, leading=10, textColor=BRAND_D,
                                 spaceAfter=3)
    s["toc_t"] = ParagraphStyle("toc_t", parent=s["body"], fontName="Helvetica-Bold",
                                fontSize=10.2, textColor=INK, spaceAfter=1)
    s["toc_d"] = ParagraphStyle("toc_d", parent=s["body"], fontSize=8.6, leading=11.5,
                                textColor=MUTED, spaceAfter=0)
    s["cover_t"] = ParagraphStyle("cover_t", parent=s["h1"], fontSize=38, leading=42)
    s["cover_s"] = ParagraphStyle("cover_s", parent=s["body"], fontSize=13, leading=20,
                                  textColor=colors.HexColor("#d8d2ff"), spaceAfter=0)
    s["code"] = ParagraphStyle("code", parent=s["body"], fontName="Courier",
                               fontSize=8.1, leading=11.4, textColor=INK,
                               leftIndent=5, spaceAfter=2)
    s["cell"] = ParagraphStyle("cell", parent=s["body"], fontSize=8.6, leading=12,
                               spaceAfter=0)
    s["cell_b"] = ParagraphStyle("cell_b", parent=s["cell"], fontName="Helvetica-Bold",
                                 textColor=INK)
    s["cell_h"] = ParagraphStyle("cell_h", parent=s["cell"], fontName="Helvetica-Bold",
                                 fontSize=8.2, textColor=WHITE)
    return s


ST = styles()


# ---------------------------------------------------------------- helpers
def P(text, style="body"):
    return Paragraph(text, ST[style])


class Rule(Flowable):
    def __init__(self, width=None, color=LINE, thickness=0.9, pad=0):
        super().__init__()
        self._w, self._c, self._t, self.pad = width, color, thickness, pad

    def wrap(self, aw, ah):
        self.width = self._w or aw
        return (self.width, self._t + self.pad * 2)

    def draw(self):
        self.canv.setStrokeColor(self._c)
        self.canv.setLineWidth(self._t)
        self.canv.line(0, self.pad, self.width, self.pad)


def numbered(n, title, body, accent=BRAND):
    """A numbered step block: badge, heading, prose."""
    badge = Table([[P(str(n), "num")]], colWidths=[8.6 * mm], rowHeights=[8.6 * mm])
    badge.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), accent),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    txt = [P(title, "h3"), P(body, "body")]
    t = Table([[badge, txt]], colWidths=[12 * mm, None])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (0, 0), "TOP"),
        ("VALIGN", (1, 0), (1, 0), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (1, 0), (1, 0), 3 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def callout(kind, title, text):
    palette = {
        "tip":   (GREEN_L, GREEN, "✓"),
        "warn":  (AMBER_L, AMBER, "!"),
        "stop":  (RED_L, RED, "✕"),
        "note":  (BLUE_L, BLUE, "i"),
        "key":   (BRAND_L, BRAND, "★"),
    }[kind]
    bg, fg, glyph = palette
    head = Paragraph(f'<font color="{fg.hexval()}">{glyph}</font>&nbsp;&nbsp;'
                     f'<b>{title}</b>', ST["cell"])
    inner = [head, Spacer(1, 2.5), P(text, "small")]
    t = Table([[inner]], colWidths=[None])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 2.6, fg),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


def table(rows, widths, header=True, zebra=True, align=None):
    data = []
    for i, row in enumerate(rows):
        cells = []
        for c in row:
            if isinstance(c, Flowable):
                cells.append(c)
            else:
                cells.append(P(str(c), "cell_h" if (header and i == 0) else "cell"))
        data.append(cells)
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    st = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5.5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, LINE),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
    ]
    if header:
        st += [("BACKGROUND", (0, 0), (-1, 0), INK)]
    if zebra:
        for i in range(1 if header else 0, len(rows)):
            if (i % 2) == 0:
                st.append(("BACKGROUND", (0, i), (-1, i), PANEL))
    if align:
        st += align
    t.setStyle(TableStyle(st))
    return t


def code_block(lines):
    body = [Paragraph(l.replace("&", "&amp;").replace("<", "&lt;").replace(" ", "&nbsp;"),
                      ST["code"]) for l in lines]
    t = Table([[body]], colWidths=[None])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f4f4f7")),
        ("BOX", (0, 0), (-1, -1), 0.5, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return t


def toc(entries):
    rows = []
    for n, title, desc in entries:
        rows.append([
            Paragraph(f'<font color="{BRAND.hexval()}"><b>{n:02d}</b></font>', ST["cell"]),
            [P(title, "toc_t"), P(desc, "toc_d")],
        ])
    t = Table(rows, colWidths=[10 * mm, None])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, LINE),
    ]))
    return t


# ---------------------------------------------------------------- page furniture
class Doc(BaseDocTemplate):
    def __init__(self, path, title, subtitle, footer_label, accent=BRAND):
        super().__init__(path, pagesize=A4,
                         leftMargin=M, rightMargin=M, topMargin=M, bottomMargin=M,
                         title=title, author="Rebuy", subject=subtitle,
                         creator="Rebuy")
        self.title_txt = title
        self.subtitle_txt = subtitle
        self.footer_label = footer_label
        self.accent = accent
        self._cover_done = False
        cover = PageTemplate(id="cover", frames=[Frame(M, M, PAGE_W - 2 * M,
                                                       PAGE_H - 2 * M, id="c",
                                                       leftPadding=0, rightPadding=0,
                                                       topPadding=0, bottomPadding=0)],
                             onPage=self._cover)
        normal = PageTemplate(id="normal", frames=[Frame(M, M + 6 * mm, PAGE_W - 2 * M,
                                                         PAGE_H - 2 * M - 14 * mm, id="n",
                                                         leftPadding=0, rightPadding=0,
                                                         topPadding=0, bottomPadding=0)],
                              onPage=self._chrome)
        self.addPageTemplates([cover, normal])

    def _cover(self, canv, doc):
        canv.saveState()
        canv.setFillColor(INK)
        canv.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
        # accent geometry
        canv.setFillColor(self.accent)
        canv.rect(0, PAGE_H - 9 * mm, PAGE_W, 9 * mm, stroke=0, fill=1)
        canv.setFillColor(colors.HexColor("#241a5e"))
        canv.circle(PAGE_W - 18 * mm, 42 * mm, 62 * mm, stroke=0, fill=1)
        canv.setFillColor(self.accent)
        canv.setFillAlpha(0.16)
        canv.circle(PAGE_W - 18 * mm, 42 * mm, 44 * mm, stroke=0, fill=1)
        canv.setFillAlpha(1)
        canv.setStrokeColor(colors.HexColor("#4b4453"))
        canv.setLineWidth(0.6)
        canv.line(M, 30 * mm, M + 54 * mm, 30 * mm)
        canv.setFillColor(colors.HexColor("#8c86a8"))
        canv.setFont("Helvetica", 8.2)
        canv.drawString(M, 25 * mm, "REBUY  ·  RETENTION OS")
        canv.setFont("Helvetica", 7.6)
        canv.drawString(M, 20.5 * mm, "Kampala, Uganda")
        canv.setFillColor(colors.HexColor("#d8d2ff"))
        canv.setFont("Helvetica", 7.6)
        canv.drawRightString(PAGE_W - M, 25 * mm, self.subtitle_txt)
        canv.restoreState()

    def _chrome(self, canv, doc):
        canv.saveState()
        # header
        canv.setStrokeColor(LINE)
        canv.setLineWidth(0.7)
        canv.line(M, PAGE_H - M + 3 * mm, PAGE_W - M, PAGE_H - M + 3 * mm)
        canv.setFont("Helvetica-Bold", 7.4)
        canv.setFillColor(MUTED)
        canv.drawString(M, PAGE_H - M + 5.4 * mm, "REBUY")
        canv.setFont("Helvetica", 7.4)
        canv.drawRightString(PAGE_W - M, PAGE_H - M + 5.4 * mm, self.footer_label)
        # footer
        canv.line(M, M + 1 * mm, PAGE_W - M, M + 1 * mm)
        canv.setFont("Helvetica", 7.4)
        canv.drawString(M, M - 3.2 * mm, self.title_txt)
        pn = str(canv.getPageNumber())
        canv.setFont("Helvetica-Bold", 8)
        canv.setFillColor(INK)
        canv.drawRightString(PAGE_W - M, M - 3.4 * mm, pn)
        canv.restoreState()


def section_heading(n, title, blurb):
    return KeepTogether([
        Spacer(1, 1),
        Paragraph(f'<font color="{BRAND.hexval()}"><b>SECTION {n:02d}</b></font>',
                  ST["kicker"]),
        P(title, "h2"),
        Rule(color=BRAND, thickness=2.2),
        Spacer(1, 5),
        P(blurb, "small"),
        Spacer(1, 9),
    ])


# ══════════════════════════════════════════════════════════════════ SHOP MANUAL
def build_shop(path):
    d = Doc(path, "Rebuy — Shop Manual",
            "The complete guide for shop owners and their teams",
            "SHOP MANUAL · 2026")
    F = []
    A = F.append

    # ---------------- cover
    A(Spacer(1, 44 * mm))
    A(Paragraph('<font color="#a99cff">THE COMPLETE GUIDE</font>', ST["kicker"]))
    A(Spacer(1, 3 * mm))
    A(Paragraph('<font color="#ffffff">Rebuy</font><br/><font color="#a99cff">Shop Manual</font>',
                ST["cover_t"]))
    A(Spacer(1, 6 * mm))
    A(Paragraph("Run a shop whose customers come back —<br/>without remembering a single one of them.",
                ST["cover_s"]))
    A(Spacer(1, 62 * mm))
    A(Paragraph('<font color="#8c86a8">For owners, managers and staff · Version 1.0 · November 2026</font>',
                ST["small"]))
    from reportlab.platypus import NextPageTemplate, PageBreak
    A(NextPageTemplate("normal"))
    A(PageBreak())

    # ---------------- contents
    A(Spacer(1, 2))
    A(Paragraph("WHAT IS INSIDE", ST["kicker"]))
    A(P("Contents", "h1"))
    A(Rule(color=BRAND, thickness=2.2))
    A(Spacer(1, 9))
    A(P("Nine sections, in the order you will actually need them.", "small"))
    A(Spacer(1, 4))
    A(toc([
        (1, "What Rebuy does", "The one idea, in plain language."),
        (2, "Getting started", "Sign in, create your shop, six setup steps."),
        (3, "Products & services", "Tell Rebuy how long what you sell actually lasts."),
        (4, "Recording sales", "Every sale starts a clock."),
        (5, "Customers & segments", "Who to talk to, and who to leave alone."),
        (6, "Reminders & inbox", "What gets sent, when, and how to reply."),
        (7, "Importing your data", "Bring the spreadsheet you already have."),
        (8, "Client records & recalls", "For clinics, salons, dentists and hospitals."),
        (9, "Retention & reports", "Proving it worked."),
        (10, "Team, billing & troubleshooting", "Invites, plans, and how to fix things."),
    ]))
    A(Spacer(1, 10))
    A(callout("note", "How to read this manual",
              "Bold text is something you click or type. Anything in a grey box is an "
              "example you can copy. Where the app behaves differently for a clinic than "
              "for a shop, the clinic version is in Section 8 and clearly marked."))
    A(PageBreak())

    # ---------------- 1
    A(section_heading(1, "What Rebuy does",
                      "One idea, and everything else follows from it."))
    A(P("Most shops lose customers without noticing. Someone buys soap in March, you "
        "never hear from them again, and by August they are buying soap somewhere else. "
        "It rarely feels like a loss — it feels like a normal quiet week."))
    A(P("Rebuy fixes exactly that, and nothing else. You tell it <b>how long what you "
        "sell lasts</b>. When someone buys it, Rebuy starts that clock. When the clock "
        "runs out, it sends them a message — on WhatsApp, by SMS or by email — that reads "
        "like a person wrote it."))
    A(Spacer(1, 3))

    A(P("The three things Rebuy tracks", "h3"))
    A(table([
        ["What", "What it means", "Why it matters"],
        ["Restock clock", "Days until a customer runs out of what they bought.",
         "The single best moment to hear from you."],
        ["Customer health", "new · loyal · at risk · lapsed · lost",
         "Who needs attention this week, and who does not."],
        ["Recovered revenue", "Sales that happened after a Rebuy message.",
         "Whether this is working, in shillings."],
    ], [30 * mm, None, 52 * mm]))

    A(Spacer(1, 6))
    A(P("What Rebuy does not do", "h3"))
    A(P("Rebuy is not a full accounting system and not a stock-management system. It "
        "does not do payroll, tax, or supplier orders. If you already use something for "
        "those, keep using it — Rebuy sits beside it and handles the part those tools "
        "ignore: <b>the customer who stopped coming back</b>."))
    A(Spacer(1, 4))
    A(callout("tip", "The one rule",
              "If you get nothing else right: make sure the duration on each product is "
              "honest. A wrong duration sends a reminder on the wrong day, and a reminder "
              "that arrives at the wrong moment is worse than none at all."))

    A(CondPageBreak(60 * mm))
    A(section_heading(2, "Getting started",
                      "From nothing to your first automated reminder, in about ten minutes."))

    A(numbered(1, "Sign in", "Open Rebuy in your browser and choose <b>Continue with "
               "Google</b>, or set a password with an email address. If you were invited "
               "to a team, use the link in that invitation on your phone — it signs you "
               "straight in."))
    A(numbered(2, "Create your shop", "First-time sign-in takes you to a short setup "
               "screen: shop name, what you sell, currency and where you are. This "
               "becomes the name on your messages, so use something your customers "
               "recognise — <i>Nakato's Salon</i>, not <i>nakato_co_2024</i>."))
    A(numbered(3, "Add products and their durations", "This is the important one. See "
               "Section 3."))
    A(numbered(4, "Add or import your customers", "Type them in one at a time, or upload "
               "the spreadsheet you already keep. See Section 7."))
    A(numbered(5, "Connect a channel", "WhatsApp, SMS or email. See Section 6."))
    A(numbered(6, "Record a few sales", "Each sale started a clock. Once a clock runs "
               "out, the first reminders appear in your queue for approval."))

    A(Spacer(1, 5))
    A(callout("warn", "You do not have to do this in one sitting",
              "The dashboard shows a setup checklist and tells you the single next thing "
              "worth doing. Nothing breaks if you stop halfway — Rebuy simply does not "
              "send anything until a channel is connected."))

    A(CondPageBreak(70 * mm))
    A(section_heading(3, "Products & services",
                      "The duration is the whole engine. Get this right and the rest is automatic."))
    A(P("Open <b>Products &amp; services</b>. Every item has a <b>duration in days</b>: "
        "how long one purchase lasts your customer."))
    A(Spacer(1, 2))
    A(table([
        ["Product", "Duration", "Reminder arrives"],
        ["Loaf of bread", "2 days", "Day 2 — before they would normally rebuy"],
        ["Bar of soap", "30 days", "Day 30"],
        ["Cooking gas cylinder", "45 days", "Day 45"],
        ["Bag of cement", "60 days", "Day 60"],
        ["Dental check-up", "180 days", "Day 180"],
    ], [None, 30 * mm, None]))
    A(Spacer(1, 5))
    A(P("When in doubt, go slightly <b>shorter</b> than the real duration. A reminder that "
        "arrives a day early is a helpful nudge; one that arrives a week late reads like "
        "you have only just noticed them.", "body"))
    A(Spacer(1, 4))
    A(callout("tip", "Watch the first few weeks",
              "Look at your queue in week one and check the timing looks sane. If a "
              "reminder says someone is out of bread after two days, and they buy once a "
              "month, fix the duration then — it corrects every future reminder at once."))

    A(CondPageBreak(70 * mm))
    A(section_heading(4, "Recording sales",
                      "One action, four ways in — pick whichever fits the moment."))
    A(table([
        ["Method", "Best for", "Where"],
        ["Quick add", "One sale at the counter, several times an hour", "Sales → New sale"],
        ["Import a sales book", "Catching up on a week that is already written down", "Import data → Sales"],
        ["Receipt photo", "You have a pile of paper receipts", "Import data → Receipts"],
        ["Till export", "You already use a POS that exports CSV", "Import data → Sales"],
    ], [32 * mm, None, 44 * mm]))
    A(Spacer(1, 5))
    A(P("Every sale records four things: <b>who</b>, <b>what</b>, <b>how much</b>, and "
        "<b>when</b>. Rebuy uses the product's duration to work out the date the customer "
        "will run out, and that date is what drives everything else."))
    A(Spacer(1, 3))
    A(callout("note", "Record the sale against a real customer",
              "A sale with no customer attached still counts in your revenue, but it "
              "cannot start a reminder — Rebuy has nobody to talk to. At the counter, "
              "take the phone number. That number is the entire asset."))

    A(CondPageBreak(70 * mm))
    A(section_heading(5, "Customers & segments",
                      "Grouping people so you message the right ones — and leave the rest alone."))
    A(P("Rebuy sorts every customer into five health states, automatically:"))
    A(Spacer(1, 2))
    A(table([
        ["State", "What it means", "What usually helps"],
        ["New", "Bought once, recently.", "A second purchase. Do not over-message."],
        ["Loyal", "Buying on a regular rhythm.", "Protect them. Reward them, lightly."],
        ["At risk", "Overdue, but they have not vanished.", "One reminder, timed well."],
        ["Lapsed", "Gone quiet for a long stretch.", "A win-back offer with a deadline."],
        ["Lost", "Very overdue and unreachable.", "One last honest attempt, then stop."],
    ], [22 * mm, None, 58 * mm]))
    A(Spacer(1, 5))
    A(P("A <b>segment</b> is a saved group — for example <i>at risk, bought soap, Kampala</i>. "
        "Open <b>Segments &amp; campaigns</b> to build one and send to everyone in it at once. "
        "Campaigns are previewed before they send, so you always see the exact message and "
        "the exact number of people before anything leaves."))

    A(CondPageBreak(70 * mm))
    A(section_heading(6, "Reminders & the inbox",
                      "Approval, timing, and what to do when someone replies."))
    A(P("A reminder moves through four states:"))
    A(code_block([
        "queued  →  approved  →  sent  →  delivered  (or  failed )",
    ]))
    A(P("Open <b>Reminder queue</b> to see what is about to go out and what already went. "
        "If your shop is set to <b>approve first</b>, nothing leaves until a person says "
        "so — useful for the first fortnight while you build confidence."))
    A(Spacer(1, 3))
    A(P("Channels", "h3"))
    A(table([
        ["Channel", "Best for", "Setup"],
        ["WhatsApp", "Most Ugandan customers; supports replies", "Business number connected"],
        ["SMS", "Customers without smartphones; always lands", "Sender ID + credits"],
        ["Email", "Receipts, invoices, formal clients", "SMTP settings"],
    ], [24 * mm, None, 46 * mm]))
    A(Spacer(1, 5))
    A(P("The <b>Inbox</b> collects every reply in one place. When a customer replies to a "
        "WhatsApp reminder, it does not vanish into your phone — it appears in Rebuy, "
        "threaded by customer, so whoever is on shift can answer it. Quick replies are "
        "worth setting up: half of all replies are answering “how much?” or “is it open?”."))
    A(Spacer(1, 4))
    A(callout("warn", "Reply fast or do not ask",
              "A reminder invites a reply. If nobody answers for two days, you have spent "
              "goodwill and gained nothing. Decide before you switch a channel on who is "
              "watching the inbox."))

    A(CondPageBreak(70 * mm))
    A(section_heading(7, "Importing your data",
                      "Keep the spreadsheet. Rebuy will read it."))
    A(P("Open <b>Import data</b>. Rebuy accepts CSV and Excel files (.csv, .xlsx, .xls) "
        "up to a few thousand rows. You do not need to rename your columns first — it "
        "looks for common names and shows you what it matched <b>before</b> importing "
        "anything."))
    A(Spacer(1, 3))
    A(P("A typical customer sheet:"))
    A(code_block([
        "Name, Phone, Last purchase, Amount, Product",
        "Grace Nakato, 0772123456, 2026-03-02, 45000, Bar soap",
        "Peter Okello, +256701987654, 2026-02-11, 120000, Cooking gas",
    ]))
    A(Spacer(1, 4))
    A(P("Rules that save you a second attempt:", "h3"))
    A(table([
        ["Rule", "Why"],
        ["Phone numbers must be real", "123 is not a number. Rebuy rejects it and tells you which row."],
        ["One customer per row", "Two names in one cell becomes two customers with the same phone."],
        ["Dates as dates", "2026-03-02 or 02/03/2026 both work; “last month” does not."],
        ["The preview is not the import", "Nothing is saved until you confirm the dry run."],
    ], [46 * mm, None]))
    A(Spacer(1, 5))
    A(callout("tip", "Import in small batches first",
              "Import 50 rows, look at the result, then do the rest. It is much easier to "
              "spot a column that mapped wrongly in 50 rows than in 5,000."))

    A(CondPageBreak(60 * mm))
    # ---------------- 8 clinical
    A(section_heading(8, "Client records & recalls",
                      "For clinics, dental practices, opticians, physiotherapists — and hospitals."))
    A(P("A shop sells things that run out. A clinic provides <b>services that come due</b>. "
        "The clock is different, so Rebuy treats this differently — but the principle is "
        "identical: know what is due, and reach the person politely."))
    A(Spacer(1, 3))
    A(P("Set up a service with a recall interval", "h3"))
    A(table([
        ["Service", "Recall interval"],
        ["General consultation", "as needed"],
        ["Dental check-up & scaling", "180 days"],
        ["Antenatal review", "28 days"],
        ["Immunisation (child)", "30 days"],
        ["Annual health screening", "365 days"],
    ], [None, 40 * mm]))
    A(Spacer(1, 5))
    A(P("When you record a visit, Rebuy schedules the next recall by itself. When you mark "
        "a recall <b>seen</b>, it chains the following one. You never maintain the calendar "
        "by hand."))

    A(Spacer(1, 4))
    A(P("The three screens", "h3"))
    A(table([
        ["Screen", "What you do there"],
        ["Client records", "Search clients, open one to see their history, upload a register."],
        ["Recalls due", "Work through who is due: mark <b>booked</b> or <b>seen</b>."],
        ["Retention", "See the percentage who came back, by service and by branch."],
    ], [34 * mm, None]))

    A(Spacer(1, 6))
    A(P("Privacy — this part is not optional", "h3"))
    A(P("If you handle health information, Rebuy is strict by default, and you should be "
        "glad it is:"))
    A(Spacer(1, 2))
    A(table([
        ["Protection", "What it means in practice"],
        ["Encrypted at rest", "Date of birth, file number, condition, address and notes are stored encrypted. A database copy does not expose them."],
        ["No diagnoses in messages", "A reminder may say “you are due for your dental check-up”. It will never carry a diagnosis or a test result. Messages that would are held back, not sent."],
        ["Every read is logged", "Opening a client record is recorded: who, when, from where. Export is recorded as a bulk disclosure."],
    ], [40 * mm, None]))
    A(Spacer(1, 5))
    A(callout("stop", "Never put a diagnosis in a reminder",
              "A message is read on a shared phone, in a matatu, by whoever picks it up. "
              "“Your HIV result” or “your pregnancy review” can do real harm. Rebuy blocks "
              "these automatically — do not try to word around the block."))

    A(CondPageBreak(60 * mm))
    A(section_heading(9, "Retention & reports",
                      "Whether any of this is working, in numbers you can act on."))
    A(P("Open <b>Reports</b> for the retail view, or <b>Retention</b> in the clinic view. "
        "The headline number is the same idea in both: <b>of the customers who came due, "
        "how many came back?</b>"))
    A(Spacer(1, 2))
    A(table([
        ["Number", "What it tells you"],
        ["Retention rate", "The share of due customers who returned. Rising is good."],
        ["Recovered revenue", "Money from sales that followed a Rebuy message."],
        ["Response rate", "Share of messages that got a reply. Falling means the wording or timing is wrong."],
        ["By segment", "Which group is leaking. Usually one, not all."],
    ], [36 * mm, None]))
    A(Spacer(1, 5))
    A(callout("note", "Read the first month loosely",
              "Early numbers are noisy because your customers are not yet spread across "
              "the clock evenly. Give it three cycles of your longest product before "
              "drawing conclusions."))

    A(CondPageBreak(60 * mm))
    A(section_heading(10, "Team, billing & troubleshooting",
                      "The boring section that saves the phone call."))
    A(P("Inviting your team", "h3"))
    A(P("Open <b>Team</b>. Only the owner can invite. An invitation is a single-use link "
        "that expires in seven days. Two roles exist: <b>owner</b> (everything, including "
        "billing) and <b>staff</b> (day-to-day: sales, customers, inbox)."))
    A(Spacer(1, 4))
    A(P("Plans", "h3"))
    A(table([
        ["Plan", "Monthly", "Customers", "Messages / month"],
        ["Starter", "UGX 49,000", "500", "2,000"],
        ["Growth", "UGX 129,000", "3,000", "15,000"],
        ["Scale", "UGX 299,000", "20,000", "100,000"],
    ], [None, 30 * mm, 30 * mm, 42 * mm]))
    A(Spacer(1, 5))

    A(P("When something looks wrong", "h3"))
    A(table([
        ["Symptom", "Almost always"],
        ["No reminders are appearing", "No product has a duration set, or no sale has a customer attached."],
        ["A reminder arrived on the wrong day", "The product duration is wrong. Fix it once; every future reminder corrects."],
        ["Messages say “failed”", "Channel not connected, or the customer's number is not a real number."],
        ["A customer got two messages", "Two products with different durations were bought on different days — this is correct behaviour."],
        ["Import rejected rows", "The preview tells you the row number and the reason. Fix those rows and re-import; nothing already imported is duplicated."],
    ], [52 * mm, None]))
    A(Spacer(1, 6))
    A(callout("tip", "Before you call for help",
              "Note the shop name and the approximate time of the problem. Almost every "
              "issue is answerable from the audit trail in under a minute with those two "
              "facts."))
    A(Spacer(1, 8))
    A(Rule())
    A(Spacer(1, 3))
    A(P("End of the Rebuy Shop Manual.", "small"))

    d.multiBuild(F)
    return path


# ══════════════════════════════════════════════════════════════════ ADMIN MANUAL
def build_admin(path):
    d = Doc(path, "Rebuy — Platform Admin Manual",
            "Operating the Rebuy platform across every tenant",
            "ADMIN MANUAL · 2026", accent=colors.HexColor("#0d9f6e"))
    F = []
    A = F.append
    GREEN_D = colors.HexColor("#066b4a")

    A(Spacer(1, 44 * mm))
    A(Paragraph('<font color="#7fe3bd">INTERNAL OPERATIONS</font>', ST["kicker"]))
    A(Spacer(1, 3 * mm))
    A(Paragraph('<font color="#ffffff">Rebuy</font><br/><font color="#7fe3bd">Admin Manual</font>',
                ST["cover_t"]))
    A(Spacer(1, 6 * mm))
    A(Paragraph("The cockpit, the tenants, the money —<br/>and the things that must not break.",
                ST["cover_s"]))
    A(Spacer(1, 62 * mm))
    A(Paragraph('<font color="#8c86a8">Platform operators only · Version 1.0 · November 2026</font>',
                ST["small"]))
    from reportlab.platypus import NextPageTemplate, PageBreak
    A(NextPageTemplate("normal"))
    A(PageBreak())

    A(Spacer(1, 2))
    A(Paragraph("WHAT IS INSIDE", ST["kicker"]))
    A(P("Contents", "h1"))
    A(Rule(color=GREEN, thickness=2.2))
    A(Spacer(1, 9))
    A(P("Eleven sections. Sections 3–7 are the daily work; 8–11 are the things that only "
        "bite once.", "small"))
    A(Spacer(1, 4))
    A(toc([
        (1, "What admins are", "Administrative accounts. No shop, no shop dashboard."),
        (2, "The cockpit", "The nine screens and what each answers."),
        (3, "Onboarding a tenant", "Retail shop, or healthcare provider."),
        (4, "Supporting a shop", "Impersonation, and how to do it without looking careless."),
        (5, "Credentials & channels", "WhatsApp, SMS, SMTP. Platform-wide and per shop."),
        (6, "The queue & the worker", "What is stuck, and what to do at 2am."),
        (7, "Audit log", "Every action, by whom, on which shop."),
        (8, "Healthcare tenants", "Encryption, access logs, the clinical guard."),
        (9, "Plans, revenue & capacity", "MRR, plan mix, and the 100-shop evidence."),
        (10, "Runbook", "Deploy, rotate, restore, break-glass."),
        (11, "Hard limits", "What this platform will not do."),
    ]))
    A(Spacer(1, 10))
    A(callout("note", "Scope of this document",
              "This manual covers operating the platform. It assumes you can sign in as a "
              "platform admin and reach the cockpit at /admin."))
    A(PageBreak())

    # 1
    A(section_heading(1, "What admins are",
                      "An administrative account, not a super-shop."))
    A(P("Admins operate the platform. They do not sell, do not own stock, and <b>never get "
        "a shop of their own</b>. This is enforced, not merely conventional: signing in as "
        "an admin takes you straight to the cockpit, and there is no path from there into "
        "a shop dashboard except deliberate impersonation."))
    A(Spacer(1, 3))
    A(P("How an account becomes admin", "h3"))
    A(P("The email address must appear in the <b>REBUY_ADMIN_EMAILS</b> environment "
        "variable, comma-separated. Sign in with Google using that address, or provision a "
        "password account, and the role is granted. Remove the address and the role goes "
        "with it on next sign-in."))
    A(Spacer(1, 4))
    A(callout("warn", "Admin is powerful and quiet",
              "An admin can read every tenant's data, change prices, reset any password and "
              "impersonate any user. Every one of those actions is written to the audit log "
              "with your email against it. Treat the log as the thing that keeps this "
              "honest."))

    # 2
    A(CondPageBreak(80 * mm))
    A(section_heading(2, "The cockpit",
                      "Nine screens, each answering one question."))
    A(table([
        ["Screen", "The question it answers"],
        ["Platform overview", "How is the whole platform doing right now?"],
        ["Shops", "Who is on the platform, and what state are they in?"],
        ["Messaging", "Volume, delivery, failures and cost, by provider."],
        ["Retention engine", "Do reminders actually bring customers back?"],
        ["Revenue", "MRR, plan mix, trial conversion."],
        ["Queue & worker", "What is due, what is stuck, when the worker last ran."],
        ["Provider credentials", "Platform-wide WhatsApp, SMS and SMTP settings."],
        ["Capacity", "Evidence the platform carries 100+ shops."],
        ["Audit log", "Every action, by whom, on which shop."],
    ], [40 * mm, None]))
    A(Spacer(1, 5))
    A(P("The cockpit is a separate document at <b>/admin</b> — not a hash route inside the "
        "shop app. If you find yourself in the shop shell as an admin, something is wrong; "
        "say so rather than working around it, because that symptom has historically meant "
        "a redirect bug.", "body"))

    # 3
    A(CondPageBreak(90 * mm))
    A(section_heading(3, "Onboarding a tenant",
                      "Two shapes: retail shop, or healthcare provider. Same table, different data."))

    A(P("Retail shop", "h3"))
    A(numbered(1, "Create the shop", "Shops → New shop. Name, sector, city, currency, plan. "
               "The name appears on customer messages, so use the trading name."))
    A(numbered(2, "Create the owner login", "Either invite them by email, or issue a "
               "password directly with the demo-login endpoint (Section 10). Self-service "
               "signup works too if REBUY_ALLOW_SIGNUP is on."))
    A(numbered(3, "Add products with durations", "The tenant can do this themselves, but "
               "doing the first ten with them is the highest-value support you can give. "
               "Duration errors cause most early dissatisfaction."))
    A(numbered(4, "Connect a channel", "See Section 5. Without a channel, nothing sends."))
    A(numbered(5, "Import their customers", "Encourage a small first batch."))

    A(Spacer(1, 7))
    A(P("Healthcare provider (hospital, clinic, dental practice)", "h3"))
    A(P("Same shop record, with <b>sector = Healthcare</b>, plus four things:"))
    A(table([
        ["What", "How"],
        ["Branches", "One account, many branches — each with its own client list, shared reporting. Created as places on the shop."],
        ["Services with recall intervals", "The clinical equivalent of a product duration. Seed the common ones, then let them adjust."],
        ["Client records", "Uploaded from their register (CSV/XLSX). Preview first, always."],
        ["A stable encryption key", "REBUY_PRIVACY_KEY must be set before the first real record. See Section 8 — this is the one that cannot be undone."],
    ], [48 * mm, None]))
    A(Spacer(1, 5))
    A(callout("stop", "Set the encryption key first",
              "If records are written before REBUY_PRIVACY_KEY is set, they are encrypted "
              "with the fallback secret. Changing it afterwards makes every existing client "
              "field unreadable — there is no recovery. Set it, verify it, then import."))

    # 4
    A(CondPageBreak(80 * mm))
    A(section_heading(4, "Supporting a shop",
                      "Impersonation is the sharpest tool here."))
    A(P("Impersonation signs you in as a shop user to see what they see. Use it when a "
        "description of a screen problem is not converging, and stop using it the moment "
        "it is."))
    A(Spacer(1, 2))
    A(table([
        ["Do", "Do not"],
        ["Reproduce the exact complaint as the user", "Change settings, unless that is the fix they asked for"],
        ["Leave within minutes", "Read client records out of curiosity"],
        ["Tell the shop afterwards if you changed anything", "Impersonate without a support reason"],
    ], [None, None]))
    A(Spacer(1, 5))
    A(P("Every impersonation is audited against your email and the shop. That record is "
        "the answer to “who looked at our data?” — which, for a healthcare tenant, someone "
        "will eventually ask."))

    # 5
    A(CondPageBreak(80 * mm))
    A(section_heading(5, "Credentials & channels",
                      "Platform-wide defaults, overridable per shop."))
    A(P("Resolution order for any message: <b>shop-level credential</b> → "
        "<b>platform credential</b> → <b>environment variable</b> → <b>sandbox</b>. If you "
        "reach sandbox, the message is written to the database and <i>nothing is sent</i>. "
        "Sandbox is a valid dev mode and a silent failure in production — know which one "
        "you are in."))
    A(Spacer(1, 2))
    A(table([
        ["Channel", "Fields", "Notes"],
        ["WhatsApp (Meta)", "token, phone_number_id, api_version, from_number",
         "Template messages required outside the 24-hour window."],
        ["SMS (Twilio)", "sid, auth_token, from_number", "Sender ID registration is per-country."],
        ["Email (SMTP)", "host, port, user, password, from_address, security",
         "Without this, password-reset emails never arrive."],
    ], [30 * mm, None, 42 * mm]))
    A(Spacer(1, 5))
    A(callout("warn", "Test credentials, do not assume them",
              "Provider credentials → Test sends a real message to a number you supply. Do "
              "this after every change. A wrong token fails silently at send time and looks "
              "like a customer who did not reply."))

    # 6
    A(CondPageBreak(80 * mm))
    A(section_heading(6, "The queue & the worker",
                      "When reminders stop moving, start here."))
    A(P("The worker drains the queue. On Vercel it is driven by a cron hit to "
        "<b>/api/cron/tick</b>; REBUY_WORKER=0 disables the in-process worker."))
    A(Spacer(1, 2))
    A(table([
        ["Symptom", "Check first"],
        ["Nothing sends at all", "Worker last-run time in Queue & worker. If stale, the cron is not firing."],
        ["Messages stuck in queued", "Approval mode on that shop, or a credential that fails at send."],
        ["High failure rate on one provider", "Provider status, then the credential test."],
        ["Duplicates", "Two products, or a re-import. Check the audit log before assuming a bug."],
    ], [50 * mm, None]))
    A(Spacer(1, 5))
    A(P("POST /api/admin/tick forces a drain. It is safe to run more than once. Use it when "
        "you need to prove to a tenant that their reminders are queued and correct, without "
        "waiting for the next cron."))

    # 7
    A(CondPageBreak(70 * mm))
    A(section_heading(7, "Audit log",
                      "The platform's memory, and your alibi."))
    A(P("Every privileged action writes a row: actor email, action, shop, target, timestamp. "
        "When a tenant asks “who looked at this record”, when a message goes out that should "
        "not have, and when two operators disagree about what happened, this is the answer."))
    A(Spacer(1, 3))
    A(P("Practical habits", "h3"))
    A(table([
        ["Habit", "Why"],
        ["Search by shop before a support call", "You often find the answer already in the log."],
        ["Search by actor when investigating a change", "Distinguishes a bug from an action someone took."],
        ["Export nothing by default", "Bulk reads are recorded as disclosures, deliberately."],
    ], [56 * mm, None]))

    # 8
    A(CondPageBreak(90 * mm))
    A(section_heading(8, "Healthcare tenants",
                      "The rules that carry real-world consequences."))
    A(P("A healthcare tenant holds other people's medical information. Four mechanisms "
        "protect it, and all four must be working before a real patient record is loaded."))
    A(Spacer(1, 2))
    A(table([
        ["Mechanism", "What it does", "Where to verify"],
        ["Field encryption", "dob, file number, condition, address, notes encrypted at rest with Fernet.", "Privacy status on the care overview; or read a raw row and look for enc:v1:"],
        ["Access log", "Every client read recorded before data is returned: actor, action, ip, user agent.", "client records → each open writes an access_log row"],
        ["Clinical guard", "Blocks any outbound message containing a diagnosis or a test result. Held, not sent, logged to message_audit.", "Test with a body containing a clinical term before go-live"],
        ["Bulk disclosure marking", "CSV export recorded as a bulk disclosure.", "Care privacy endpoint"],
    ], [32 * mm, None, 40 * mm]))
    A(Spacer(1, 5))
    A(P("Demonstrating it works", "h3"))
    A(P("Two checks worth running whenever you deploy anything touching the care layer:"))
    A(code_block([
        "1. Read a raw row directly from the database.",
        "   Expect: dob and identifier start with  enc:v1:",
        "",
        "2. Open the same record through the app as the tenant.",
        "   Expect: readable date and file number, and a new access_log row.",
        "",
        "If step 2 shows  [encrypted — could not be opened with this key]",
        "the running deployment does not have the key that encrypted the row.",
    ]))
    A(Spacer(1, 4))
    A(callout("stop", "The one unrecoverable mistake",
              "REBUY_PRIVACY_KEY must never change once records exist. Rotating it makes "
              "every encrypted client field on every tenant permanently unreadable. Before "
              "the first real record: set it, deploy, verify. After that: treat it as "
              "immutable and store a copy in a safe place."))

    # 9
    A(CondPageBreak(80 * mm))
    A(section_heading(9, "Plans, revenue & capacity",
                      "The commercial view of the same platform."))
    A(table([
        ["Plan", "Monthly", "Customers", "Messages / month"],
        ["Starter", "UGX 49,000", "500", "2,000"],
        ["Growth", "UGX 129,000", "3,000", "15,000"],
        ["Scale", "UGX 299,000", "20,000", "100,000"],
    ], [None, 32 * mm, 32 * mm, 42 * mm]))
    A(Spacer(1, 5))
    A(P("<b>Revenue</b> shows MRR, plan mix and trial conversion. <b>Capacity</b> is the "
        "evidence screen: it exists so that “can this carry a hundred shops?” is answered "
        "with numbers rather than assurances. Check it before promising a large tenant a "
        "launch date."))

    # 10
    A(CondPageBreak(90 * mm))
    A(section_heading(10, "Runbook",
                      "The operations you will need under pressure."))

    A(P("Deploy", "h3"))
    A(code_block([
        "git push origin main",
        "vercel create_deployment  target=production",
        "curl -s https://<host>/api/health   # ok:true, warnings:[]",
        "curl -s -o /dev/null -w '%{http_code}' https://<host>/api/care/overview",
        "                                    # 401 = route live, 404 = not deployed",
    ]))
    A(P("A deploy can silently not happen — a deduplicated call, a queued build. Always "
        "confirm the new route or behaviour exists before telling anyone it shipped.", "small"))

    A(Spacer(1, 5))
    A(P("Issue or rotate a tenant's login", "h3"))
    A(code_block([
        "POST /api/admin/shops/<sid>/demo-login",
        'Body: {"email": "owner@example.com", "password": "<optional>"}',
        "",
        "Omitting password generates one and returns it once.",
        "Existing sessions for that user are killed.",
    ]))
    A(P("Use this whenever self-service reset is not reliable. Without SMTP configured, the "
        "“forgot password” link is written to the sandbox and never delivered — the account "
        "would otherwise be unrecoverable.", "small"))

    A(Spacer(1, 5))
    A(P("Rotate a secret", "h3"))
    A(table([
        ["Secret", "Rotating it costs you"],
        ["REBUY_SECRET_KEY", "All sessions signed out. Nobody loses data. Safe."],
        ["REBUY_PRIVACY_KEY", "Every encrypted health field becomes unreadable. Never do this casually."],
        ["Provider tokens", "Messages fail until the new token is tested. Rotate one channel at a time."],
        ["REBUY_DATABASE_URL", "Total outage if wrong. Change, deploy, immediately verify /api/health."],
    ], [42 * mm, None]))

    A(Spacer(1, 5))
    A(P("Break-glass: locked out of the cockpit", "h3"))
    A(P("Admin access comes from <b>REBUY_ADMIN_EMAILS</b>. If you are locked out, edit that "
        "variable in Vercel and redeploy — the role follows the address, so there is no "
        "separate password to recover. Confirm afterwards that the value contains the "
        "addresses you expect and no others."))

    # 11
    A(CondPageBreak(80 * mm))
    A(section_heading(11, "Hard limits",
                      "What this platform deliberately will not do."))
    A(table([
        ["Limit", "Why it exists"],
        ["No clinical content in messages", "A reminder is not a channel for results. Blocked at the send choke point."],
        ["No unencrypted health fields in production", "The privacy layer reports its mode; a build without the dependency runs in plaintext mode and says so loudly."],
        ["No bulk export without a log entry", "Reading everything is an event worth recording."],
        ["Admins never hold a shop", "Administrative and trading roles do not mix."],
        ["Imports are size-capped", "Under 10,000 records per file by design; larger loads need a different mechanism."],
    ], [56 * mm, None]))
    A(Spacer(1, 6))
    A(callout("note", "If a tenant asks for something on this list",
              "The answer is usually “not this way”, not “no”. The clinical guard exists "
              "because the alternative is a diagnosis on a shared phone. Offer the safer "
              "wording instead of removing the guard."))
    A(Spacer(1, 8))
    A(Rule())
    A(Spacer(1, 3))
    A(P("End of the Rebuy Platform Admin Manual.", "small"))

    d.multiBuild(F)
    return path


def main():
    os.makedirs(OUT, exist_ok=True)
    shop = os.path.join(OUT, "Rebuy-Shop-Manual.pdf")
    admin = os.path.join(OUT, "Rebuy-Admin-Manual.pdf")
    build_shop(shop)
    build_admin(admin)
    for p in (shop, admin):
        print(f"{os.path.basename(p):28} {os.path.getsize(p)//1024:>5} KB")


if __name__ == "__main__":
    main()
