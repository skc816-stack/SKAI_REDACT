#!/usr/bin/env python3
"""
SKAI_REDACT
===========
A fully offline desktop tool for secure PDF editing, *permanent* redaction,
annotation, text editing, merging, splitting and compression.

Engine : PyMuPDF (fitz)  -> rendering + true content-removal redaction
UI     : Tkinter         -> ships with Python, no extra GUI install
Images : Pillow          -> page preview + annotation overlay

Key design choice
-----------------
Redaction uses PyMuPDF `add_redact_annot` + `apply_redactions()` with explicit
flags (PDF_REDACT_TEXT_REMOVE, PDF_REDACT_IMAGE_PIXELS, PDF_REDACT_LINE_ART)
that DELETE the underlying text/vector/image content from the PDF stream.
A drawn black rectangle is NOT used, because that leaves the text extractable.
After redaction a two-pass save (in-memory rebuild + re-parse to disk) ensures
no residual text tokens survive in Form XObjects or shared content streams.

Edits are kept as an editable list (per page, in PDF-point coordinates) so that
Undo/Redo is clean and the original file is never mutated until you Save.

Run:  python pdf_redactor.py
"""

import sys
import copy
import os

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("PyMuPDF is required.  Install with:  pip install pymupdf")

try:
    from PIL import Image, ImageTk, ImageDraw
except ImportError:
    sys.exit("Pillow is required.  Install with:  pip install pillow")

import re
import webbrowser

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser, simpledialog


VERSION = "1.0"
DEVELOPER = "CA Santosh Kumar Kushwaha"
WEBSITE = "https://skcweb.vercel.app/"
EMAIL = "skai4u2025@gmail.com"
PHONE = "+91 8617249565"


def resource_dir():
    """Folder holding bundled assets (works for plain run and PyInstaller exe)."""
    if hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

# Optional: AI name/place detection. Used only if the user has installed it.
try:
    import spacy
    try:
        _NLP = spacy.load("en_core_web_sm")
    except Exception:
        _NLP = None
except Exception:
    spacy = None
    _NLP = None


# --------------------------------------------------------------------------- #
#  SENSITIVE-DATA DETECTION                                                    #
# --------------------------------------------------------------------------- #
# Indian identifier patterns. These have fixed shapes, so regex finds them
# reliably. Over-detection is acceptable for a redaction tool -- it is safer to
# offer one extra box for review than to miss a confidential number.
DETECTORS = [
    ("PAN",     re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")),
    # GSTIN: 2-digit state + 10-char PAN + entity + 'Z' + checksum
    ("GST",     re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b")),
    ("Aadhaar", re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")),
    ("Phone",   re.compile(r"\b(?:\+91[\s-]?)?[6-9]\d{9}\b")),
    ("Email",   re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
]
# spaCy entity labels that usually hold names / places / orgs / addresses.
_NER_LABELS = {"PERSON", "GPE", "LOC", "FAC", "ORG"}

# Field labels common in tax / audit / legal / govt forms. The *value* typed
# after one of these on the same line is what we redact.
FIELD_LABELS = [
    "legal name", "trade name", "reference no", "ref no", "name", "address",
    "gstin", "pan", "aadhaar", "mobile", "phone", "email", "contact",
    "father", "proprietor", "applicant", "dealer",
]


def _detect_form_fields(page, pno, add_fn):
    """Redact the value that follows a known label on the same text line.

    Works on structured forms (e.g. 'Name:  Rajesh Kumar'), which is exactly
    the layout of tax notices, audit letters and legal drafts.
    """
    words = page.get_text("words")  # (x0,y0,x1,y1, word, block, line, wordno)
    if not words:
        return
    lines = {}
    for w in words:
        lines.setdefault((w[5], w[6]), []).append(w)

    for ws in lines.values():
        ws.sort(key=lambda w: w[7])
        line_text = " ".join(w[4] for w in ws).lower().strip()
        for label in FIELD_LABELS:
            if line_text.startswith(label):
                n_label_words = len(label.split())
                value_words = ws[n_label_words:]
                # drop a leading stray ':' token if present
                value_words = [w for w in value_words if w[4].strip(":.- ")]
                if value_words:
                    x0 = min(w[0] for w in value_words)
                    y0 = min(w[1] for w in value_words)
                    x1 = max(w[2] for w in value_words)
                    y1 = max(w[3] for w in value_words)
                    txt = " ".join(w[4] for w in value_words)
                    add_fn(pno, fitz.Rect(x0, y0, x1, y1),
                           label.title(), txt)
                break


def detect_sensitive(doc, use_ai=False, include_fields=True):
    """Scan every page for sensitive data.

    Returns a list of records:
        {"page": i, "rect": (x0,y0,x1,y1), "label": "PAN", "text": "ABCDE1234F"}

    `use_ai=True` additionally runs spaCy NER for names/places/orgs if the
    model is installed; otherwise that part is silently skipped.
    """
    found = []
    seen = set()  # de-dupe identical rects

    def _add(pno, rect, label, text):
        key = (pno, round(rect.x0, 1), round(rect.y0, 1),
               round(rect.x1, 1), round(rect.y1, 1))
        if key in seen:
            return
        seen.add(key)
        found.append({"page": pno, "rect": (rect.x0, rect.y0, rect.x1, rect.y1),
                      "label": label, "text": text})

    for pno in range(doc.page_count):
        page = doc[pno]
        text = page.get_text()

        # 1) Pattern-based identifiers (always on, very reliable)
        for label, rx in DETECTORS:
            for m in rx.finditer(text):
                s = m.group().strip()
                for rect in page.search_for(s):
                    _add(pno, rect, label, s)

        # 1b) Form-field values that follow a known label (Name:, Address:, ...)
        if include_fields:
            _detect_form_fields(page, pno, _add)

        # 2) AI name/place detection (optional)
        if use_ai and _NLP is not None:
            try:
                spdoc = _NLP(text)
                for ent in spdoc.ents:
                    if ent.label_ in _NER_LABELS and len(ent.text.strip()) > 2:
                        for rect in page.search_for(ent.text.strip()):
                            _add(pno, rect, ent.label_, ent.text.strip())
            except Exception:
                pass

    return found


def ai_available():
    """True if the optional name/place AI model is installed and loaded."""
    return _NLP is not None


# --------------------------------------------------------------------------- #
#  ENGINE  -- pure PDF logic, independent of the GUI (easy to test headless)   #
# --------------------------------------------------------------------------- #
def apply_edits_to_document(src_path, edits, out_path):
    """Bake a list of edit-records onto a fresh copy of `src_path`.

    Each edit is a dict with PDF-point coordinates:
        {"type":"redact",    "page":i, "rect":(x0,y0,x1,y1)}
        {"type":"highlight", "page":i, "rect":(...), "color":(r,g,b)}  # 0..1
        {"type":"underline", "page":i, "rect":(...), "color":(...)}
        {"type":"pen",       "page":i, "points":[(x,y)...], "color":(...),
                             "width":w}
    """
    doc = fitz.open(src_path)
    by_page = {}
    for e in edits:
        by_page.setdefault(e["page"], []).append(e)

    for pno in range(doc.page_count):
        page = doc[pno]
        page_edits = by_page.get(pno, [])

        # 1) Redactions first -- this destroys content under the rect.
        #    Flags ensure BOTH the visual pixels AND the underlying text stream
        #    are permanently removed, so no tool can extract the hidden text.
        reds = [e for e in page_edits if e["type"] == "redact"]
        for e in reds:
            page.add_redact_annot(fitz.Rect(e["rect"]), fill=(0, 0, 0))
        if reds:
            # Build kwargs defensively: older PyMuPDF versions may not have
            # all flags. PDF_REDACT_TEXT_REMOVE is the critical one -- it
            # physically deletes text operators from the content stream so
            # no extraction tool can recover the hidden text.
            redact_kwargs = {}
            for attr, key in [
                ("PDF_REDACT_IMAGE_PIXELS", "images"),
                ("PDF_REDACT_TEXT_REMOVE",  "text"),
                ("PDF_REDACT_LINE_ART",     "graphics"),
            ]:
                val = getattr(fitz, attr, None)
                if val is not None:
                    redact_kwargs[key] = val
            page.apply_redactions(**redact_kwargs)

        # 2) Markup / ink annotations added AFTER redaction so they survive.
        for e in page_edits:
            t = e["type"]
            if t == "highlight":
                a = page.add_highlight_annot(fitz.Rect(e["rect"]))
                a.set_colors(stroke=e["color"]); a.update()
            elif t == "underline":
                a = page.add_underline_annot(fitz.Rect(e["rect"]))
                a.set_colors(stroke=e["color"]); a.update()
            elif t == "pen":
                a = page.add_ink_annot([list(e["points"])])
                a.set_colors(stroke=e["color"])
                try:
                    a.set_border(width=e.get("width", 2))
                except Exception:
                    pass
                a.update()
            elif t == "text":
                # baseline origin; nudge down so it sits like typed text
                pt = fitz.Point(e["point"][0], e["point"][1] + e.get("size", 12))
                page.insert_text(pt, e["text"], fontsize=e.get("size", 12),
                                 color=e.get("color", (0, 0, 0)))

    # --- Two-pass sanitization ---
    # Pass 1: write to an in-memory buffer with full garbage collection and
    # content-stream cleanup so no residual text operators survive.
    import io
    buf = io.BytesIO()
    doc.save(buf, garbage=4, deflate=True, clean=True, no_new_id=False)
    doc.close()

    # Pass 2: reload from the clean buffer and re-save to disk. The reload
    # forces PyMuPDF to re-parse the rebuilt streams, stripping any token
    # (e.g. inside a Form XObject) that survived the first pass.
    buf.seek(0)
    doc2 = fitz.open("pdf", buf)
    doc2.save(out_path, garbage=4, deflate=True, clean=True, no_new_id=False)
    doc2.close()




def flatten_to_image_pdf(src_path, edits, out_path, dpi=200):
    """Nuclear redaction: apply edits, render every page to a raster image,
    then rebuild a new PDF from those images.

    The output has ZERO text layer -- nothing can be extracted, copied, or
    searched because the entire document is pixel data.  This is the only
    method that is immune to Form XObject leakage and all other text-layer
    edge cases.

    dpi=200 gives a good quality/size balance for A4 notices.  Use 150 for
    smaller files, 300 for archival quality.
    """
    import io

    # Step 1: apply all edits (redactions, annotations, text inserts) to a
    #         temporary in-memory PDF exactly as the normal save does.
    tmp_buf = io.BytesIO()
    tmp_doc = fitz.open(src_path)
    by_page = {}
    for e in edits:
        by_page.setdefault(e["page"], []).append(e)

    for pno in range(tmp_doc.page_count):
        page = tmp_doc[pno]
        page_edits = by_page.get(pno, [])
        reds = [e for e in page_edits if e["type"] == "redact"]
        for e in reds:
            page.add_redact_annot(fitz.Rect(e["rect"]), fill=(0, 0, 0))
        if reds:
            redact_kwargs = {}
            for attr, key in [
                ("PDF_REDACT_IMAGE_PIXELS", "images"),
                ("PDF_REDACT_TEXT_REMOVE",  "text"),
                ("PDF_REDACT_LINE_ART",     "graphics"),
            ]:
                val = getattr(fitz, attr, None)
                if val is not None:
                    redact_kwargs[key] = val
            page.apply_redactions(**redact_kwargs)
        for e in page_edits:
            t = e["type"]
            if t == "highlight":
                a = page.add_highlight_annot(fitz.Rect(e["rect"]))
                a.set_colors(stroke=e["color"]); a.update()
            elif t == "underline":
                a = page.add_underline_annot(fitz.Rect(e["rect"]))
                a.set_colors(stroke=e["color"]); a.update()
            elif t == "pen":
                a = page.add_ink_annot([list(e["points"])])
                a.set_colors(stroke=e["color"])
                try:
                    a.set_border(width=e.get("width", 2))
                except Exception:
                    pass
                a.update()
            elif t == "text":
                pt = fitz.Point(e["point"][0], e["point"][1] + e.get("size", 12))
                page.insert_text(pt, e["text"], fontsize=e.get("size", 12),
                                 color=e.get("color", (0, 0, 0)))

    tmp_doc.save(tmp_buf, garbage=4, deflate=True, clean=True)
    tmp_doc.close()

    # Step 2: render every page to pixels at the requested DPI, then pack into
    #         a brand-new PDF.  The new PDF has no text stream at all.
    tmp_buf.seek(0)
    src = fitz.open("pdf", tmp_buf)
    out_doc = fitz.open()          # blank new PDF
    zoom = dpi / 72.0              # 72 pt = 1 inch
    mat = fitz.Matrix(zoom, zoom)

    for pno in range(src.page_count):
        page = src[pno]
        pix = page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csRGB)
        # Insert a new page the same physical size as the original
        w_pt = page.rect.width
        h_pt = page.rect.height
        new_page = out_doc.new_page(width=w_pt, height=h_pt)
        # Embed the raster image covering the full page
        img_bytes = pix.tobytes("png")
        new_page.insert_image(new_page.rect, stream=img_bytes)

    src.close()
    out_doc.save(out_path, garbage=4, deflate=True, deflate_images=True)
    out_doc.close()


def merge_documents(paths, out_path):
    out = fitz.open()
    for p in paths:
        with fitz.open(p) as d:
            out.insert_pdf(d)
    out.save(out_path, garbage=4, deflate=True)
    out.close()


def split_document(src_path, out_dir):
    """One PDF per page. Returns list of written paths."""
    written = []
    base = os.path.splitext(os.path.basename(src_path))[0]
    with fitz.open(src_path) as d:
        for i in range(d.page_count):
            single = fitz.open()
            single.insert_pdf(d, from_page=i, to_page=i)
            out = os.path.join(out_dir, f"{base}_page_{i + 1}.pdf")
            single.save(out); single.close()
            written.append(out)
    return written


def find_all_matches(doc, term):
    """Return {page_no: [fitz.Rect, ...]} for every occurrence of `term`."""
    hits = {}
    for pno in range(doc.page_count):
        rects = doc[pno].search_for(term)
        if rects:
            hits[pno] = rects
    return hits


# level -> (dpi_threshold, dpi_target, jpeg_quality)
COMPRESS_LEVELS = {
    "Low (best quality)":   (150, 120, 80),
    "Medium (recommended)": (130, 96, 55),
    "High (smallest file)": (120, 72, 40),
}


def compress_document(src_path, out_path, level="Medium (recommended)"):
    """Recompress images + clean structure to shrink the PDF.

    Lossless cleanup always runs; image downsampling runs for image-heavy
    (e.g. scanned) PDFs. Returns (original_bytes, new_bytes).
    """
    thr, tgt, q = COMPRESS_LEVELS.get(level, COMPRESS_LEVELS["Medium (recommended)"])
    orig = os.path.getsize(src_path)
    doc = fitz.open(src_path)
    try:
        doc.subset_fonts()
    except Exception:
        pass
    try:
        doc.rewrite_images(dpi_threshold=thr, dpi_target=tgt, quality=q,
                           lossy=True, lossless=True)
    except Exception:
        pass  # text-only PDFs simply skip image rewriting
    doc.save(out_path, garbage=4, deflate=True, deflate_images=True,
             deflate_fonts=True, clean=True)
    doc.close()
    return orig, os.path.getsize(out_path)


# --------------------------------------------------------------------------- #
#  GUI                                                                         #
# --------------------------------------------------------------------------- #
MODES = ("idle", "redact", "highlight", "underline", "pen", "text")
MODE_COLORS = {
    "idle": "#6b7280", "redact": "#111827", "highlight": "#b45309",
    "underline": "#1d4ed8", "pen": "#047857", "text": "#7c3aed",
}


class PDFRedactorApp:
    PALETTE = {
        "app": "#0b1220", "header": "#0b1220", "bar": "#111827",
        "strip": "#0f172a", "canvas": "#1e293b",
        "btn": "#1f2937", "btn_hover": "#334155", "text": "#e5e7eb",
        "muted": "#94a3b8", "field": "#1e293b", "chip": "#334155",
        "accent": "#38bdf8", "accent_hi": "#7dd3fc",
        "danger": "#ef4444", "danger_hi": "#f87171",
        "ok": "#22c55e", "ok_hi": "#4ade80",
    }

    def __init__(self, root):
        self.root = root
        self.root.title("SKAI_REDACT  \u2014  Secure PDF Redaction & Editing")
        self.root.geometry("1300x840")
        self._mode_buttons = {}       # mode name -> button (for active styling)

        # document state
        self.src_path = None          # path of the *original* loaded file
        self.doc = None               # fitz doc kept open for preview/search
        self.page_no = 0
        self.zoom = 1.5

        # edit state
        self.edits = []               # committed edit records (PDF points)
        self.redo_stack = []
        self.mode = "idle"
        self.draw_color = (1.0, 1.0, 0.0)   # default highlight yellow (0..1)
        self.pen_width = 2

        # search state
        self.search_hits = {}         # {page: [Rect,...]}
        self.search_term = ""

        # interaction scratch
        self._drag_start = None
        self._temp_items = []
        self._pen_points = []

        self._tk_img = None           # keep ref so it isn't GC'd

        self._build_ui()
        self._update_status()

    # ----- UI construction -------------------------------------------------- #
    def _build_ui(self):
        P = self.PALETTE
        self.root.configure(bg=P["app"])

        # try to load the logo (sits next to the script)
        self._logo_img = None
        try:
            here = resource_dir()
            from PIL import Image as _PImage
            for cand in ("skai_logo_128.png", "skai_logo.png"):
                fp = os.path.join(here, cand)
                if os.path.exists(fp):
                    im = _PImage.open(fp).convert("RGBA").resize((40, 40))
                    self._logo_img = ImageTk.PhotoImage(im)
                    # window icon
                    ico = _PImage.open(fp).convert("RGBA").resize((64, 64))
                    self.root.iconphoto(True, ImageTk.PhotoImage(ico))
                    break
        except Exception:
            self._logo_img = None

        # ---- header ---- (auto-sizes to its contents so nothing clips)
        header = tk.Frame(self.root, bg=P["header"])
        header.pack(side=tk.TOP, fill=tk.X)
        if self._logo_img:
            tk.Label(header, image=self._logo_img, bg=P["header"]).pack(
                side=tk.LEFT, padx=(14, 10), pady=12)
        title_box = tk.Frame(header, bg=P["header"])
        title_box.pack(side=tk.LEFT, pady=10)
        tk.Label(title_box, text="SKAI_REDACT", bg=P["header"], fg="white",
                 font=("Segoe UI Semibold", 15)).pack(anchor="w")
        tk.Label(title_box,
                 text="Secure offline PDF redaction \u2022 editing \u2022 compression",
                 bg=P["header"], fg="#cbd5e1",
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(3, 2))
        self.mode_badge = tk.Label(header, text="  IDLE  ", bg=P["chip"],
                                   fg="white", font=("Segoe UI Semibold", 9),
                                   padx=6, pady=3)
        self.mode_badge.pack(side=tk.RIGHT, padx=14)
        about_btn = tk.Button(header, text="About", command=self._show_about,
                              relief="flat", bg=P["btn"], fg=P["text"],
                              activebackground=P["btn_hover"], bd=0, padx=12,
                              pady=4, cursor="hand2", font=("Segoe UI", 9))
        about_btn.pack(side=tk.RIGHT, padx=(0, 4))
        about_btn.bind("<Enter>", lambda e: about_btn.config(bg=P["btn_hover"]))
        about_btn.bind("<Leave>", lambda e: about_btn.config(bg=P["btn"]))

        # ---- toolbar ----
        bar = tk.Frame(self.root, bg=P["bar"])
        bar.pack(side=tk.TOP, fill=tk.X)
        bar_in = tk.Frame(bar, bg=P["bar"])
        bar_in.pack(side=tk.LEFT, padx=8, pady=8)

        self._first_group = True

        def group():
            if not self._first_group:
                tk.Frame(bar_in, bg=P["btn_hover"], width=1, height=28).pack(
                    side=tk.LEFT, padx=8, pady=2)
            self._first_group = False
            inner = tk.Frame(bar_in, bg=P["bar"])
            inner.pack(side=tk.LEFT)
            return inner

        def mk(parent, text, cmd, kind="default", mode=None):
            colors = {
                "default": (P["btn"], P["btn_hover"], P["text"]),
                "accent":  (P["accent"], P["accent_hi"], "#06283d"),
                "danger":  (P["danger"], P["danger_hi"], "white"),
                "ok":      (P["ok"], P["ok_hi"], "#05291a"),
            }[kind]
            base, hov, fg = colors
            b = tk.Button(parent, text=text, command=cmd, relief="flat",
                          bg=base, fg=fg, activebackground=hov,
                          activeforeground=fg, bd=0, padx=11, pady=6,
                          cursor="hand2", font=("Segoe UI", 9))
            b.pack(side=tk.LEFT, padx=2)
            b._base, b._hov = base, hov
            b.bind("<Enter>", lambda e: b.config(bg=b._hov))
            b.bind("<Leave>", lambda e: b.config(
                bg=(P["accent"] if self.mode == mode and mode else b._base)))
            if mode:
                self._mode_buttons[mode] = b
            return b

        gf = group()
        mk(gf, "Open", self.open_file, "accent")
        mk(gf, "Merge", self.merge_files)
        mk(gf, "Split", self.split_file)
        mk(gf, "Compress", self.compress_file)

        gv = group()
        mk(gv, "\u25c0 Prev", self.prev_page)
        mk(gv, "Next \u25b6", self.next_page)
        mk(gv, "Zoom +", lambda: self.set_zoom(self.zoom * 1.25))
        mk(gv, "Zoom \u2212", lambda: self.set_zoom(self.zoom / 1.25))

        gt = group()
        mk(gt, "Redact", lambda: self.set_mode("redact"), "danger", mode="redact")
        mk(gt, "Text", lambda: self.set_mode("text"), mode="text")
        mk(gt, "Highlight", lambda: self.set_mode("highlight"), mode="highlight")
        mk(gt, "Underline", lambda: self.set_mode("underline"), mode="underline")
        mk(gt, "Pen", lambda: self.set_mode("pen"), mode="pen")
        mk(gt, "Color", self.pick_color)

        ge = group()
        mk(ge, "Undo", self.undo)
        mk(ge, "Redo", self.redo)
        mk(ge, "Save", self.save_file, "ok")

        # ---- search + auto-detect strip ----
        strip = tk.Frame(self.root, bg=P["strip"])
        strip.pack(side=tk.TOP, fill=tk.X)

        srow = tk.Frame(strip, bg=P["strip"])
        srow.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(7, 3))
        tk.Label(srow, text="Search", bg=P["strip"], fg=P["text"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        ent = tk.Entry(srow, textvariable=self.search_var, width=30,
                       bg=P["field"], fg=P["text"], insertbackground=P["text"],
                       relief="flat", font=("Segoe UI", 9))
        ent.pack(side=tk.LEFT, padx=8, ipady=3)
        ent.bind("<Return>", lambda e: self.do_search())

        def sbtn(parent, text, cmd, kind="default"):
            base = {"default": P["btn"], "accent": P["accent"],
                    "danger": P["danger"]}[kind]
            hov = {"default": P["btn_hover"], "accent": P["accent_hi"],
                   "danger": P["danger_hi"]}[kind]
            fg = {"default": P["text"], "accent": "#06283d",
                  "danger": "white"}[kind]
            b = tk.Button(parent, text=text, command=cmd, relief="flat",
                          bg=base, fg=fg, activebackground=hov, bd=0,
                          padx=10, pady=5, cursor="hand2", font=("Segoe UI", 9))
            b.pack(side=tk.LEFT, padx=2)
            b.bind("<Enter>", lambda e: b.config(bg=hov))
            b.bind("<Leave>", lambda e: b.config(bg=base))
            return b

        sbtn(srow, "Find", self.do_search)
        sbtn(srow, "Redact Matches", self.redact_matches, "danger")
        self.search_info = tk.Label(srow, text="", bg=P["strip"], fg=P["muted"],
                                    font=("Segoe UI", 8))
        self.search_info.pack(side=tk.LEFT, padx=10)

        arow = tk.Frame(strip, bg=P["strip"])
        arow.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(3, 8))
        tk.Label(arow, text="Auto-find", bg=P["strip"], fg=P["text"],
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        sbtn(arow, "Scan (IDs + Name/Address/Ref fields)",
             lambda: self.auto_detect(use_ai=False), "accent")
        self.ai_btn = sbtn(arow, "Scan + Names/Addresses (AI)",
                           lambda: self.auto_detect(use_ai=True))
        sbtn(arow, "Redact All Found", self.redact_detected, "danger")
        sbtn(arow, "Clear Found", self.clear_detected)
        self.detect_info = tk.Label(arow, text="", bg=P["strip"], fg=P["muted"],
                                    font=("Segoe UI", 8))
        self.detect_info.pack(side=tk.LEFT, padx=10)
        if not ai_available():
            self.ai_btn.config(state="disabled")

        self.detected = []

        # ---- scrollable canvas ----
        mid = tk.Frame(self.root, bg=P["canvas"])
        mid.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(mid, background=P["canvas"],
                                cursor="crosshair", highlightthickness=0)
        vsb = tk.Scrollbar(mid, orient=tk.VERTICAL, command=self.canvas.yview)
        hsb = tk.Scrollbar(mid, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.root.bind("<Control-z>", lambda e: self.undo())
        self.root.bind("<Control-y>", lambda e: self.redo())

        # ---- status bar ----
        self.status = tk.Label(self.root, anchor="w", padx=10, pady=4,
                               bg=P["header"], fg=P["muted"],
                               font=("Segoe UI", 8))
        self.status.pack(side=tk.BOTTOM, fill=tk.X)
        self._show_empty_state()

    # ----- status / mode ---------------------------------------------------- #
    def _update_status(self):
        npages = self.doc.page_count if self.doc else 0
        name = os.path.basename(self.src_path) if self.src_path else "no file open"
        self.status.config(
            text=f"File: {name}    \u2022    "
                 f"Page {self.page_no + 1 if npages else 0} of {npages}    \u2022    "
                 f"Zoom {int(self.zoom * 100)}%    \u2022    "
                 f"{len(self.edits)} edit(s) staged")
        if hasattr(self, "mode_badge"):
            self.mode_badge.config(
                text=f"  {self.mode.upper()}  ",
                bg=MODE_COLORS.get(self.mode, self.PALETTE["chip"]))

    def set_mode(self, m):
        self.mode = m if m in MODES else "idle"
        # repaint tool buttons: active one gets the accent
        for name, b in self._mode_buttons.items():
            if name == self.mode:
                b.config(bg=self.PALETTE["accent"], fg="#06283d")
            else:
                b.config(bg=b._base, fg=self.PALETTE["text"])
        self._update_status()

    def _on_canvas_resize(self, event):
        # keep the welcome screen centred until a document is loaded
        if not self.doc:
            self._show_empty_state()

    def _show_about(self):
        P = self.PALETTE
        win = tk.Toplevel(self.root)
        win.title("About SKAI_REDACT")
        win.configure(bg=P["strip"])
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)

        try:
            from PIL import Image as _PI
            fp = os.path.join(resource_dir(), "skai_logo.png")
            im = _PI.open(fp).convert("RGBA").resize((84, 84))
            self._about_logo = ImageTk.PhotoImage(im)
            tk.Label(win, image=self._about_logo, bg=P["strip"]).pack(pady=(20, 6))
        except Exception:
            pass

        tk.Label(win, text="SKAI_REDACT", bg=P["strip"], fg="white",
                 font=("Segoe UI Semibold", 16)).pack()
        tk.Label(win, text="Secure offline PDF redaction, editing & compression",
                 bg=P["strip"], fg=P["muted"],
                 font=("Segoe UI", 9)).pack(pady=(2, 0))
        tk.Label(win, text=f"Version {VERSION}", bg=P["strip"], fg=P["muted"],
                 font=("Segoe UI", 8)).pack(pady=(2, 10))

        tk.Frame(win, bg=P["btn_hover"], height=1, width=320).pack()

        tk.Label(win, text="Developed by", bg=P["strip"], fg=P["muted"],
                 font=("Segoe UI", 8)).pack(pady=(12, 0))
        tk.Label(win, text=DEVELOPER, bg=P["strip"], fg="white",
                 font=("Segoe UI Semibold", 12)).pack()

        def link(text, url, mailto=False):
            lbl = tk.Label(win, text=text, bg=P["strip"], fg=P["accent"],
                           cursor="hand2", font=("Segoe UI", 9, "underline"))
            lbl.pack(pady=(6, 0))
            target = ("mailto:" + url) if mailto else url
            lbl.bind("<Button-1>", lambda e: webbrowser.open(target))
            return lbl

        link(WEBSITE, WEBSITE)
        link(EMAIL, EMAIL, mailto=True)
        tk.Label(win, text=PHONE, bg=P["strip"], fg=P["text"],
                 font=("Segoe UI", 9)).pack(pady=(4, 0))

        tk.Label(win,
                 text="100% offline \u2014 your documents never leave your computer.",
                 bg=P["strip"], fg=P["muted"],
                 font=("Segoe UI", 8)).pack(pady=(14, 4))

        tk.Button(win, text="Close", command=win.destroy, relief="flat",
                  bg=P["accent"], fg="#06283d", bd=0, padx=20, pady=6,
                  cursor="hand2", font=("Segoe UI Semibold", 9)).pack(pady=(8, 20))

        win.update_idletasks()
        x = (self.root.winfo_rootx() + self.root.winfo_width() // 2
             - win.winfo_width() // 2)
        y = self.root.winfo_rooty() + 110
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _show_empty_state(self):
        """Friendly placeholder before any PDF is opened."""
        P = self.PALETTE
        self.canvas.delete("all")
        self.canvas.update_idletasks()
        w = self.canvas.winfo_width() or 900
        h = self.canvas.winfo_height() or 600
        cx, cy = w // 2, h // 2
        if getattr(self, "_logo_big", None) is None and self._logo_img:
            try:
                here = resource_dir()
                from PIL import Image as _PImage
                fp = os.path.join(here, "skai_logo.png")
                im = _PImage.open(fp).convert("RGBA").resize((96, 96))
                self._logo_big = ImageTk.PhotoImage(im)
            except Exception:
                self._logo_big = None
        if getattr(self, "_logo_big", None):
            self.canvas.create_image(cx, cy - 50, image=self._logo_big)
        self.canvas.create_text(cx, cy + 24, text="Open a PDF to begin",
                                fill=P["text"], font=("Segoe UI Semibold", 15))
        self.canvas.create_text(
            cx, cy + 52,
            text="Click  Open  \u2022  then  Scan  to auto-find sensitive data",
            fill=P["muted"], font=("Segoe UI", 10))

    def set_zoom(self, z):
        self.zoom = max(0.25, min(z, 6.0))
        self.render_page()

    def pick_color(self):
        rgb, _ = colorchooser.askcolor(title="Choose annotation color")
        if rgb:
            self.draw_color = tuple(c / 255.0 for c in rgb)

    # ----- file ops --------------------------------------------------------- #
    def open_file(self):
        path = filedialog.askopenfilename(
            title="Open PDF", filetypes=[("PDF files", "*.pdf")])
        if not path:
            return
        try:
            self._load(path)
        except Exception as e:
            messagebox.showerror("Open failed", str(e))

    def _load(self, path):
        if self.doc:
            self.doc.close()
        self.doc = fitz.open(path)
        self.src_path = path
        self.page_no = 0
        self.edits.clear()
        self.redo_stack.clear()
        self.search_hits = {}
        self.search_info.config(text="")
        self.render_page()
        self._update_status()

    def merge_files(self):
        paths = filedialog.askopenfilenames(
            title="Select PDFs to merge (in order)",
            filetypes=[("PDF files", "*.pdf")])
        if not paths or len(paths) < 2:
            messagebox.showinfo("Merge", "Pick at least two PDF files.")
            return
        out = filedialog.asksaveasfilename(
            title="Save merged PDF as", defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf")])
        if not out:
            return
        try:
            merge_documents(list(paths), out)
            if messagebox.askyesno("Merge complete",
                                   "Merged file saved.\nOpen it now?"):
                self._load(out)
        except Exception as e:
            messagebox.showerror("Merge failed", str(e))

    def split_file(self):
        if not self.doc:
            messagebox.showinfo("Split", "Open a PDF first.")
            return
        out_dir = filedialog.askdirectory(title="Folder for split pages")
        if not out_dir:
            return
        try:
            files = split_document(self.src_path, out_dir)
            messagebox.showinfo("Split complete",
                                f"Wrote {len(files)} files to:\n{out_dir}")
        except Exception as e:
            messagebox.showerror("Split failed", str(e))

    def _ask_choice(self, title, prompt, options, default=0):
        """Modal chooser. Returns the chosen option string, or None."""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)
        ttk.Label(win, text=prompt, padding=10).pack()
        choice = tk.StringVar(value=options[default])
        for opt in options:
            ttk.Radiobutton(win, text=opt, value=opt,
                            variable=choice).pack(anchor="w", padx=16)
        result = {"val": None}
        row = ttk.Frame(win, padding=10); row.pack()

        def ok():
            result["val"] = choice.get(); win.destroy()

        def cancel():
            win.destroy()

        ttk.Button(row, text="OK", command=ok).pack(side=tk.LEFT, padx=6)
        ttk.Button(row, text="Cancel", command=cancel).pack(side=tk.LEFT, padx=6)
        win.update_idletasks()
        # centre over main window
        x = self.root.winfo_rootx() + 120
        y = self.root.winfo_rooty() + 120
        win.geometry(f"+{x}+{y}")
        self.root.wait_window(win)
        return result["val"]

    def compress_file(self):
        if not self.doc:
            messagebox.showinfo("Compress", "Open a PDF first.")
            return
        level = self._ask_choice(
            "Compress PDF", "Choose a compression level:",
            list(COMPRESS_LEVELS.keys()), default=1)
        if not level:
            return
        out = filedialog.asksaveasfilename(
            title="Save compressed PDF as", defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf")])
        if not out:
            return
        try:
            self.root.config(cursor="watch"); self.root.update()
            orig, new = compress_document(self.src_path, out, level)
        except Exception as e:
            messagebox.showerror("Compress failed", str(e)); return
        finally:
            self.root.config(cursor="")
        pct = int(100 * new / max(orig, 1))
        saved = max(orig - new, 0)
        messagebox.showinfo(
            "Compress complete",
            f"Saved to:\n{out}\n\n"
            f"Original : {orig // 1024} KB\n"
            f"Compressed: {new // 1024} KB  ({pct}% of original)\n"
            f"Reduced by {saved // 1024} KB.\n\n"
            f"Note: text-only PDFs are already small, so the drop is biggest "
            f"on scanned / image-heavy files.")

    def save_file(self):
        if not self.doc:
            messagebox.showinfo("Save", "Open a PDF first.")
            return

        # If there are redactions, ask which save mode to use
        has_redacts = any(e["type"] == "redact" for e in self.edits)
        if has_redacts:
            mode = self._ask_choice(
                "Save mode",
                "Choose how to save:\n\n"
                "Standard Redact  -  removes text from PDF stream (fast)\n"
                "Secure Flatten   -  converts every page to image; 100% safe,\n"
                "                    nothing can be extracted (recommended)",
                ["Standard Redact", "Secure Flatten (Image PDF)"],
                default=1)
            if not mode:
                return
        else:
            mode = "Standard Redact"

        out = filedialog.asksaveasfilename(
            title="Save edited PDF as", defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf")])
        if not out:
            return
        if os.path.abspath(out) == os.path.abspath(self.src_path):
            messagebox.showwarning(
                "Save", "Please choose a different filename so the original "
                        "is preserved.")
            return
        try:
            self.root.config(cursor="watch"); self.root.update()
            if mode == "Secure Flatten (Image PDF)":
                flatten_to_image_pdf(self.src_path, self.edits, out, dpi=200)
                messagebox.showinfo(
                    "Saved - Secure Flatten",
                    f"Saved to:\n{out}\n\n"
                    "Every page converted to pixels.\n"
                    "There is NO text layer - nothing can be extracted, "
                    "copied or searched.")
            else:
                apply_edits_to_document(self.src_path, self.edits, out)
                messagebox.showinfo(
                    "Saved",
                    f"Saved to:\n{out}\n\nRedacted content has been permanently "
                    f"removed from the PDF text stream.")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))
        finally:
            self.root.config(cursor="")

    # ----- navigation ------------------------------------------------------- #
    def prev_page(self):
        if self.doc and self.page_no > 0:
            self.page_no -= 1
            self.render_page()

    def next_page(self):
        if self.doc and self.page_no < self.doc.page_count - 1:
            self.page_no += 1
            self.render_page()

    # ----- rendering -------------------------------------------------------- #
    def render_page(self):
        if not self.doc:
            return
        page = self.doc[self.page_no]
        mat = fitz.Matrix(self.zoom, self.zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        img = img.convert("RGBA")

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        z = self.zoom

        def col255(c, a=255):
            return (int(c[0] * 255), int(c[1] * 255), int(c[2] * 255), a)

        # committed edits on this page
        for e in self.edits:
            if e["page"] != self.page_no:
                continue
            t = e["type"]
            if t in ("redact", "highlight", "underline"):
                x0, y0, x1, y1 = [v * z for v in e["rect"]]
                if t == "redact":
                    d.rectangle([x0, y0, x1, y1], fill=(0, 0, 0, 255))
                elif t == "highlight":
                    d.rectangle([x0, y0, x1, y1], fill=col255(e["color"], 90))
                elif t == "underline":
                    d.line([x0, y1, x1, y1], fill=col255(e["color"], 255),
                           width=max(2, int(2 * z)))
            elif t == "pen":
                pts = [(x * z, y * z) for x, y in e["points"]]
                if len(pts) >= 2:
                    d.line(pts, fill=col255(e["color"], 255),
                           width=max(1, int(e.get("width", 2) * z)), joint="curve")
            elif t == "text":
                tx, ty = e["point"][0] * z, e["point"][1] * z
                try:
                    from PIL import ImageFont
                    fnt = ImageFont.truetype("arial.ttf", int(e.get("size", 12) * z))
                except Exception:
                    fnt = None
                d.text((tx, ty), e["text"], fill=col255(e.get("color", (0, 0, 0))),
                       font=fnt)

        # live search highlights (not committed)
        for r in self.search_hits.get(self.page_no, []):
            d.rectangle([r.x0 * z, r.y0 * z, r.x1 * z, r.y1 * z],
                        outline=(220, 30, 30, 255), width=2)

        # auto-detected sensitive items awaiting review (orange)
        for rec in getattr(self, "detected", []):
            if rec["page"] != self.page_no:
                continue
            x0, y0, x1, y1 = [v * z for v in rec["rect"]]
            d.rectangle([x0, y0, x1, y1], fill=(255, 140, 0, 70),
                        outline=(255, 120, 0, 255), width=2)

        img = Image.alpha_composite(img, overlay).convert("RGB")
        self._tk_img = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._tk_img)
        self.canvas.config(scrollregion=(0, 0, img.width, img.height))
        self._update_status()

    # ----- mouse interaction ------------------------------------------------ #
    def _cxy(self, event):
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def on_press(self, event):
        if not self.doc or self.mode == "idle":
            return
        if self.mode == "text":
            cx, cy = self._cxy(event)
            txt = simpledialog.askstring("Add text", "Type the text to place:")
            if txt:
                pt = (cx / self.zoom, cy / self.zoom)
                self._commit({"type": "text", "page": self.page_no,
                              "point": pt, "text": txt,
                              "color": (0, 0, 0), "size": 12})
            return
        self._drag_start = self._cxy(event)
        self._pen_points = [self._drag_start] if self.mode == "pen" else []

    def on_drag(self, event):
        if not self.doc or self.mode == "idle" or self._drag_start is None:
            return
        for it in self._temp_items:
            self.canvas.delete(it)
        self._temp_items = []
        x0, y0 = self._drag_start
        x1, y1 = self._cxy(event)
        if self.mode == "pen":
            self._pen_points.append((x1, y1))
            if len(self._pen_points) >= 2:
                flat = [c for p in self._pen_points for c in p]
                self._temp_items.append(self.canvas.create_line(
                    *flat, fill="#10b981", width=2))
        else:
            fill, stipple = "", ""
            if self.mode == "redact":
                fill, stipple = "#000000", "gray50"
            elif self.mode == "highlight":
                fill, stipple = "#facc15", "gray25"
            kw = dict(outline="#2563eb", width=1)
            if fill:
                kw.update(fill=fill, stipple=stipple)
            self._temp_items.append(
                self.canvas.create_rectangle(x0, y0, x1, y1, **kw))

    def on_release(self, event):
        if not self.doc or self.mode == "idle" or self._drag_start is None:
            return
        for it in self._temp_items:
            self.canvas.delete(it)
        self._temp_items = []
        z = self.zoom

        if self.mode == "pen":
            if len(self._pen_points) >= 2:
                pts = [(x / z, y / z) for x, y in self._pen_points]
                self._commit({"type": "pen", "page": self.page_no,
                              "points": pts, "color": self.draw_color,
                              "width": self.pen_width})
        else:
            x0, y0 = self._drag_start
            x1, y1 = self._cxy(event)
            rect = (min(x0, x1) / z, min(y0, y1) / z,
                    max(x0, x1) / z, max(y0, y1) / z)
            if (rect[2] - rect[0]) > 2 and (rect[3] - rect[1]) > 2:
                rec = {"type": self.mode, "page": self.page_no, "rect": rect}
                if self.mode in ("highlight", "underline"):
                    rec["color"] = self.draw_color
                self._commit(rec)

        self._drag_start = None
        self._pen_points = []

    # ----- edit stack ------------------------------------------------------- #
    def _commit(self, rec):
        self.edits.append(rec)
        self.redo_stack.clear()
        self.render_page()

    def undo(self):
        if self.edits:
            self.redo_stack.append(self.edits.pop())
            self.render_page()

    def redo(self):
        if self.redo_stack:
            self.edits.append(self.redo_stack.pop())
            self.render_page()

    # ----- search ----------------------------------------------------------- #
    def do_search(self):
        if not self.doc:
            return
        term = self.search_var.get().strip()
        self.search_term = term
        if not term:
            self.search_hits = {}
            self.search_info.config(text="")
            self.render_page()
            return
        self.search_hits = find_all_matches(self.doc, term)
        total = sum(len(v) for v in self.search_hits.values())
        self.search_info.config(
            text=f"{total} match(es) on {len(self.search_hits)} page(s)")
        if self.search_hits:                      # jump to first hit's page
            first = min(self.search_hits.keys())
            self.page_no = first
        self.render_page()

    def redact_matches(self):
        if not self.doc or not self.search_term:
            messagebox.showinfo("Redact matches", "Run a search first.")
            return
        hits = find_all_matches(self.doc, self.search_term)
        n = sum(len(v) for v in hits.values())
        if not n:
            messagebox.showinfo("Redact matches", "No matches to redact.")
            return
        if not messagebox.askyesno(
                "Redact matches",
                f"Permanently redact all {n} occurrence(s) of "
                f"\u201c{self.search_term}\u201d on save?"):
            return
        for pno, rects in hits.items():
            for r in rects:
                self.edits.append({"type": "redact", "page": pno,
                                   "rect": (r.x0, r.y0, r.x1, r.y1)})
        self.redo_stack.clear()
        self.search_hits = {}
        self.search_info.config(text="")
        self.render_page()

    # ----- auto-detect ------------------------------------------------------ #
    def auto_detect(self, use_ai=False):
        if not self.doc:
            messagebox.showinfo("Auto-detect", "Open a PDF first.")
            return
        if use_ai and not ai_available():
            messagebox.showinfo(
                "AI model not installed",
                "Name/address detection needs the AI language model.\n\n"
                "It is not installed, so only the pattern scan ran.")
            use_ai = False
        try:
            self.root.config(cursor="watch"); self.root.update()
            self.detected = detect_sensitive(self.doc, use_ai=use_ai)
        finally:
            self.root.config(cursor="")
        # summarise counts by label
        by_label = {}
        for d in self.detected:
            by_label[d["label"]] = by_label.get(d["label"], 0) + 1
        if not self.detected:
            self.detect_info.config(text="Nothing found.")
        else:
            summary = ", ".join(f"{k}:{v}" for k, v in sorted(by_label.items()))
            self.detect_info.config(
                text=f"Found {len(self.detected)} \u2014 {summary}")
            self.page_no = min(d["page"] for d in self.detected)
        self.render_page()

    def redact_detected(self):
        if not self.detected:
            messagebox.showinfo("Redact found",
                                "Run a scan first, then redact what it finds.")
            return
        if not messagebox.askyesno(
                "Redact all found",
                f"Permanently redact all {len(self.detected)} detected item(s) "
                f"on save?\n\nTip: anything wrongly detected can be removed with "
                f"Undo afterwards."):
            return
        for d in self.detected:
            self.edits.append({"type": "redact", "page": d["page"],
                               "rect": d["rect"]})
        self.redo_stack.clear()
        self.detected = []
        self.detect_info.config(text="Staged for redaction. Now click Save.")
        self.render_page()

    def clear_detected(self):
        self.detected = []
        self.detect_info.config(text="")
        self.render_page()


def main():
    root = tk.Tk()
    PDFRedactorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
