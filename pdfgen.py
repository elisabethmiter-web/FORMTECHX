"""Builds the signed PDFs.

Uploaded PDF  -> original pages (each stamped with a signing footer)
                 + answers page (if the form had extra fields)
                 + signature certificate page.
Fillable form -> generated document with the answers + signature certificate.
"""
import io
from datetime import datetime

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)
from xml.sax.saxutils import escape

INK = colors.HexColor("#1d2433")
MUTED = colors.HexColor("#5b6475")
RULE = colors.HexColor("#d5d9e2")
ACCENT = colors.HexColor("#24408e")

H1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=17, leading=21, textColor=INK, spaceAfter=4)
H2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11.5, leading=15, textColor=INK,
                    spaceBefore=14, spaceAfter=6)
BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=14.5, textColor=INK, alignment=TA_LEFT)
SMALL = ParagraphStyle("small", fontName="Helvetica", fontSize=8.5, leading=11.5, textColor=MUTED)
LABEL = ParagraphStyle("label", fontName="Helvetica", fontSize=8.5, leading=11, textColor=MUTED)
VALUE = ParagraphStyle("value", fontName="Helvetica", fontSize=10, leading=13.5, textColor=INK)
MONO = ParagraphStyle("mono", fontName="Courier", fontSize=8, leading=10.5, textColor=INK)


def is_readable_pdf(path):
    try:
        r = PdfReader(path)
        if r.is_encrypted:
            return False
        return len(r.pages) > 0
    except Exception:  # noqa: BLE001
        return False


def _when(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%B %d, %Y at %H:%M:%S UTC")
    except (TypeError, ValueError):
        return iso or ""


def _p(text, style=BODY):
    return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), style)


def _answer_text(field, answers):
    v = answers.get(field["id"])
    if field["type"] == "checkbox":
        return "Yes — checked" if v else "No — not checked"
    return v if v not in (None, "") else "—"


def _answers_table(fields, answers):
    rows = [[_p(f["label"], LABEL), _p(_answer_text(f, answers), VALUE)] for f in fields]
    t = Table(rows, colWidths=[2.1 * inch, 4.4 * inch])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _certificate(meta, sig_path):
    img = ImageReader(sig_path)
    iw, ih = img.getSize()
    w = 2.8 * inch
    h = min(w * ih / iw, 1.1 * inch)
    w = h * iw / ih
    sig = Image(sig_path, width=w, height=h)
    sig_box = Table([[sig], [_p(meta["signer_name"], VALUE)], [_p("Signature", LABEL)]],
                    colWidths=[3.2 * inch], hAlign="LEFT")
    sig_box.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("LINEBELOW", (0, 0), (0, 0), 0.8, INK),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    details = [
        ("Document", meta["form_name"]),
        ("Signed by", meta["signer_name"] if meta["signer_name"].strip().lower() == meta["client_name"].strip().lower()
         else f"{meta['signer_name']} (on behalf of {meta['client_name']})"),
        ("Client email", meta["client_email"]),
        ("Signed on", _when(meta["signed_at"])),
        ("IP address", meta.get("ip") or "—"),
        ("Device", meta.get("user_agent") or "—"),
        ("Confirmation", meta["packet_ref"]),
        ("Requested by", meta["business"]),
    ]
    rows = [[_p(k, LABEL), _p(v, VALUE if k != "Device" else SMALL)] for k, v in details]
    if meta.get("original_sha256"):
        rows.append([_p("Original file SHA-256", LABEL), _p(meta["original_sha256"], MONO)])
    t = Table(rows, colWidths=[1.7 * inch, 4.8 * inch])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    consent = _p("The signer agreed to sign electronically and confirmed that their electronic signature is "
                 "the legal equivalent of their handwritten signature for this document.", SMALL)
    return [Spacer(1, 6), KeepTogether([sig_box]), Spacer(1, 16), _p("Signing record", H2), t,
            Spacer(1, 10), consent]


def _page_frame(meta):
    def draw(c, doc):
        c.saveState()
        c.setFont("Helvetica", 7.5)
        c.setFillColor(MUTED)
        c.drawString(0.85 * inch, 0.5 * inch, f"{meta['business']} · Confirmation {meta['packet_ref']}")
        c.drawRightString(letter[0] - 0.85 * inch, 0.5 * inch, f"Page {doc.page}")
        c.restoreState()
    return draw


def _build(story, meta):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.85 * inch, rightMargin=0.85 * inch,
                            topMargin=0.85 * inch, bottomMargin=0.85 * inch,
                            title=meta["form_name"], author=meta["business"])
    frame = _page_frame(meta)
    doc.build(story, onFirstPage=frame, onLaterPages=frame)
    buf.seek(0)
    return buf


def _stamp(width, height, text):
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(width, height))
    c.setFont("Helvetica", 7)
    c.setFillColor(ACCENT)
    c.drawString(24, 14, text)
    c.save()
    buf.seek(0)
    return PdfReader(buf).pages[0]


def _spec_tables(specs):
    rooms = specs.get("rooms", [])
    n = sum(len(r["items"]) for r in rooms)
    out = [_p(f"{len(rooms)} room{'s' if len(rooms) != 1 else ''}, {n} item{'s' if n != 1 else ''}", SMALL)]
    room_h = ParagraphStyle("rh", parent=H2, textColor=ACCENT, spaceBefore=12)
    widths = [0.3 * inch, 0.8 * inch, 1.4 * inch, 1.05 * inch, 1.2 * inch, 0.8 * inch, 1.05 * inch]
    for i, room in enumerate(rooms, 1):
        rows = [[_p(h, LABEL) for h in ("#", "Category", "Item", "Make", "Model", "Finish", "Notes")]]
        for j, it in enumerate(room["items"], 1):
            item = it["type"] + (f" ×{it['qty']}" if it.get("qty", 1) > 1 else "")
            rows.append([_p(j, SMALL), _p(it["category"], SMALL), _p(item, VALUE), _p(it["make"], VALUE),
                         _p(it["model"], VALUE), _p(it.get("finish") or "", SMALL), _p(it.get("notes") or "", SMALL)])
        t = Table(rows, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.5, RULE),
                               ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK), ("LEFTPADDING", (0, 0), (-1, -1), 3),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 4),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        out += [_p(f"Room {i}: {room['name']}", room_h), t]
    if specs.get("notes"):
        out += [_p("General notes", H2), _p(specs["notes"])]
    out += [Spacer(1, 10), _p("I confirm these specifications are correct to the best of my knowledge.")]
    return out


def signed_document(out_path, source_pdf, body, fields, answers, sig_path, meta, specs=None):
    story = [_p(meta["form_name"], H1),
             _p(f"Prepared by {meta['business']} for {meta['client_name']}", SMALL), Spacer(1, 10)]
    if specs:
        story += _spec_tables(specs)
    elif source_pdf is None:
        if body:
            for para in body.split("\n\n"):
                story += [_p(para.strip()), Spacer(1, 6)]
        if fields:
            story += [_p("Information provided", H2), _answers_table(fields, answers)]
    else:
        story += [_p(f"This page records the electronic signature for the attached document "
                     f"“{meta['form_name']}”, which appears on the preceding pages.", BODY)]
        if fields:
            story += [_p("Information provided", H2), _answers_table(fields, answers)]
    story += _certificate(meta, sig_path)
    generated = PdfReader(_build(story, meta))

    writer = PdfWriter()
    if source_pdf:
        stamp_text = (f"Signed electronically by {meta['signer_name']} on "
                      f"{meta['signed_at'][:10]} · Confirmation {meta['packet_ref']}")
        for page in PdfReader(source_pdf).pages:
            w, h = float(page.mediabox.width), float(page.mediabox.height)
            page.merge_page(_stamp(w, h, stamp_text))
            writer.add_page(page)
    for page in generated.pages:
        writer.add_page(page)
    writer.add_metadata({"/Title": f"{meta['form_name']} (signed)", "/Author": meta["business"],
                         "/Subject": f"Confirmation {meta['packet_ref']}"})
    with open(out_path, "wb") as fh:
        writer.write(fh)


EVENT_LABELS = {
    "created": "Packet created", "sent": "Sent", "viewed": "Opened by client", "signed": "Signed",
    "completed": "All forms signed", "voided": "Cancelled", "extended": "Link extended",
    "reminder": "Reminder", "receipt_emailed": "Confirmation emailed", "email_failed": "Email failed",
}


def audit_trail(out_path, business, packet, items, events):
    ref = f"FS-{packet['id']:05d}-{packet['token'][:6].upper()}"
    meta = {"business": business, "packet_ref": ref, "form_name": f"Audit trail {ref}"}
    story = [_p("Audit trail", H1), _p(f"{business} · Confirmation {ref}", SMALL), Spacer(1, 10),
             _p("Packet", H2)]
    info = [("Client", f"{packet['client_name']} <{packet['client_email']}>"),
            ("Created", _when(packet["created_at"])), ("Sent", _when(packet["sent_at"])),
            ("First opened", _when(packet["first_viewed_at"])),
            ("Completed", _when(packet["completed_at"]) or "Not yet")]
    t = Table([[_p(k, LABEL), _p(v, VALUE)] for k, v in info], colWidths=[1.7 * inch, 4.8 * inch])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.5, RULE), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [t, _p("Documents", H2)]
    rows = [[_p("Document", LABEL), _p("Signed", LABEL), _p("Signed file SHA-256", LABEL)]]
    for i in items:
        rows.append([_p(i["template_name"], VALUE),
                     _p(f"{i['signer_name']}\n{_when(i['signed_at'])}" if i["signed_at"] else "Not signed", SMALL),
                     _p(i["signed_sha256"] or "—", MONO)])
    t = Table(rows, colWidths=[2.0 * inch, 1.9 * inch, 2.6 * inch], repeatRows=1)
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.5, RULE), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [t, _p("Activity", H2)]
    rows = [[_p("When", LABEL), _p("Event", LABEL), _p("IP", LABEL)]]
    for e in events:
        label = EVENT_LABELS.get(e["type"], e["type"])
        detail = f"{label}: {e['detail']}" if e["detail"] and e["type"] != "viewed" else label
        rows.append([_p(_when(e["at"]), SMALL), _p(detail, VALUE), _p(e["ip"] or "", SMALL)])
    t = Table(rows, colWidths=[2.0 * inch, 3.3 * inch, 1.2 * inch], repeatRows=1)
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.5, RULE), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(t)
    buf = _build(story, meta)
    with open(out_path, "wb") as fh:
        fh.write(buf.read())
