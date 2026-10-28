"""Fast Printer: drag a PDF in, pick a label size, click Print. Talks TSPL straight to the chosen
label printer, so the settings come from this app, not the Windows driver."""
import ctypes
import json
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pymupdf
import win32print
from PIL import Image, ImageDraw, ImageTk
from tkinterdnd2 import DND_FILES, TkinterDnD

APP = "Fast Printer"
PLACEHOLDER = "Select printer"
DOTS_PER_MM = 8  # 203 dpi
SIZES = {"100 x 150 mm": (100, 150), "30 x 60 mm": (60, 30)}  # built-in defaults: width, height in mm
MIN_MM, MAX_MM = 10, 300  # sanity bounds for custom sizes
GAP_MM = 3  # labels are always gapped, 3 mm gap, no offset
DEFAULTS = {"speed": 1, "darkness": 15, "copies": 1}
SETTINGS_FILE = os.path.join(os.environ.get("APPDATA", "."), "FastPrinter", "settings.json")

# Palette
BG, CARD, BORDER, TEXT, MUTED = "#f3f4f6", "#ffffff", "#d7dbe2", "#111827", "#6b7280"
HEADER, ACCENT, ACCENT_DARK, OK, BAD = "#111827", "#2563eb", "#1d4ed8", "#16a34a", "#dc2626"
FONT = "Segoe UI"


def make_logo(px):
    """Blue rounded tile with a white label and a lightning bolt. Drawn at 4x then shrunk for smooth edges."""
    s = 1024
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((32, 32, s - 32, s - 32), radius=220, fill=ACCENT)
    d.rounded_rectangle((292, 190, 732, 834), radius=60, fill="white")
    d.polygon([(560, 270), (380, 560), (500, 560), (450, 760), (650, 460), (530, 460), (600, 270)], fill=ACCENT)
    return img.resize((px, px), Image.LANCZOS)


def parse_mm(raw, label):
    """Parse a millimetre value typed by the user. Raises ValueError with a readable message."""
    try:
        val = float(raw.strip())
    except ValueError:
        raise ValueError(f"'{raw.strip()}' isn't a number for {label}.")
    if not MIN_MM <= val <= MAX_MM:
        raise ValueError(f"{label} must be between {MIN_MM} and {MAX_MM} mm.")
    return round(val, 1)


def size_name(w, h):
    return f"{w:g} x {h:g} mm"


def load_settings():
    """Returns (label sizes, per-size settings, last used size, last used printer)."""
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    saved_sizes = saved.get("sizes")
    sizes = {}
    for name, dims in (saved_sizes.items() if isinstance(saved_sizes, dict) else ()):
        try:
            sizes[name] = (parse_mm(str(dims[0]), "Width"), parse_mm(str(dims[1]), "Height"))
        except (ValueError, TypeError, IndexError, KeyError):
            pass  # skip a hand-edited bad entry rather than refuse to start
    sizes = sizes or dict(SIZES)
    last = saved.get("last_size") if saved.get("last_size") in sizes else next(iter(sizes))
    per_size = {size: saved[size] if isinstance(saved.get(size), dict) else {} for size in sizes}
    return (sizes, {size: {**DEFAULTS, **{k: v for k, v in s.items() if k in DEFAULTS}}
                    for size, s in per_size.items()}, last, str(saved.get("printer", "")))


def save_settings(sizes, all_settings, last_size, printer):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump({**all_settings, "sizes": {n: list(d) for n, d in sizes.items()},
                   "last_size": last_size, "printer": printer}, f, indent=2)


def page_to_label(page, w_dots, h_dots):
    """Render a PDF page to a 1-bit image exactly the label's size, fitted and centred."""
    pw, ph = page.rect.width, page.rect.height
    rotate = 90 if (pw > ph) != (w_dots > h_dots) else 0
    if rotate:
        pw, ph = ph, pw
    zoom = min(w_dots / pw, h_dots / ph)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom).prerotate(rotate), colorspace=pymupdf.csGRAY)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    canvas = Image.new("L", (w_dots, h_dots), 255)
    canvas.paste(img, ((w_dots - img.width) // 2, (h_dots - img.height) // 2))
    # Hard threshold, no dithering: keeps barcodes and text crisp.
    return canvas.point(lambda v: 255 if v > 160 else 0).convert("1")


def build_tspl(images, w_mm, h_mm, s):
    head = (f"SIZE {w_mm:g} mm,{h_mm:g} mm\r\nGAP {GAP_MM} mm,0 mm\r\nSPEED {s['speed']}\r\n"
            f"DENSITY {s['darkness']}\r\nDIRECTION 0\r\nREFERENCE 0,0\r\n").encode()
    out = [head]
    for img in images:
        # PIL "1" packs white as 1 and black as 0, which is exactly TSPL's BITMAP convention.
        out.append(b"CLS\r\n" + f"BITMAP 0,0,{(img.width + 7) // 8},{img.height},0,".encode()
                   + img.tobytes() + f"\r\nPRINT 1,{s['copies']}\r\n".encode())
    return b"".join(out)


def print_pdf(path, dims, s, printer):
    w_mm, h_mm = dims
    with pymupdf.open(path) as doc:
        images = [page_to_label(p, round(w_mm * DOTS_PER_MM), round(h_mm * DOTS_PER_MM)) for p in doc]
    data = build_tspl(images, w_mm, h_mm, s)
    hp = win32print.OpenPrinter(printer)
    try:
        win32print.StartDocPrinter(hp, 1, (os.path.basename(path), None, "RAW"))
        win32print.StartPagePrinter(hp)
        win32print.WritePrinter(hp, data)
        win32print.EndPagePrinter(hp)
        win32print.EndDocPrinter(hp)
    finally:
        win32print.ClosePrinter(hp)
    return len(images)


VIRTUAL_PRINTERS = ("microsoft print to pdf", "microsoft xps", "onenote", "fax", "send to ")  # can't take TSPL


def list_printers():
    """Every printer installed for this Windows user (local, USB and network), minus virtual ones."""
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    try:
        names = {p[2] for p in win32print.EnumPrinters(flags)}
    except Exception:  # spooler service stopped: show an empty list rather than crash
        return []
    return sorted((n for n in names if not any(v in n.lower() for v in VIRTUAL_PRINTERS)), key=str.lower)


def default_printer(names, saved):
    """Saved choice if still installed, else a Munbyn, else the only printer, else nothing."""
    if saved in names:
        return saved
    munbyn = [n for n in names if "munbyn" in n.lower()]
    if munbyn:
        return munbyn[0]
    return names[0] if len(names) == 1 else ""


def style_title_bar(root):
    """Colour the Windows title bar to match the header (Windows 11; silently ignored elsewhere)."""
    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    bgr = lambda hexc: ctypes.c_int(int(hexc[5:7] + hexc[3:5] + hexc[1:3], 16))
    for attr, val in ((20, ctypes.c_int(1)), (35, bgr(HEADER)), (36, bgr("#ffffff"))):
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))


class App:
    FIELDS = [("speed", "Speed", [1, 2, 3, 4, 5, 6]),
              ("darkness", "Darkness", list(range(16))),
              ("copies", "Copies", (1, 999, 1))]

    def __init__(self):
        try:  # own taskbar icon instead of Python's
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FastPrinter.App")
        except Exception:
            pass
        self.root = TkinterDnD.Tk()
        self.root.title(APP)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.icons = [ImageTk.PhotoImage(make_logo(n)) for n in (64, 32, 16)]
        self.root.iconphoto(True, *self.icons)
        self.path = None
        self.sizes, self.settings, last, saved_printer = load_settings()
        self.size = tk.StringVar(value=last)
        self.printers = list_printers()
        self.printer = tk.StringVar(value=default_printer(self.printers, saved_printer) or PLACEHOLDER)
        self.style()

        self.header()
        body = tk.Frame(self.root, bg=BG, padx=20, pady=18)
        body.pack(fill="both")
        self.printer_picker(body)
        self.drop_zone(body)
        self.size_picker(body)
        self.settings_card(body)

        self.btn = tk.Button(body, text="Print", font=(FONT, 13, "bold"), bg=ACCENT, fg="white",
                             activebackground=ACCENT_DARK, activeforeground="white", relief="flat",
                             bd=0, cursor="hand2", pady=11, command=self.go)
        self.btn.pack(fill="x", pady=(18, 0))
        self.btn.bind("<Enter>", lambda e: self.btn.config(bg=ACCENT_DARK))
        self.btn.bind("<Leave>", lambda e: self.btn.config(bg=ACCENT))

        self.status = tk.Label(self.root, text="Ready", bg=CARD, fg=MUTED, font=(FONT, 9),
                               anchor="w", padx=20, pady=6, highlightthickness=1, highlightbackground=BORDER)
        self.status.pack(fill="x", side="bottom")

        self.load_into_form()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        try:
            style_title_bar(self.root)
        except Exception:
            pass

    # ---------- layout ----------
    def style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        for w in ("TCombobox", "TSpinbox"):
            st.configure(w, fieldbackground=CARD, background=CARD, bordercolor=BORDER, lightcolor=CARD,
                         darkcolor=CARD, arrowcolor=MUTED, padding=5, font=(FONT, 10))
            st.map(w, bordercolor=[("focus", ACCENT)], fieldbackground=[("readonly", CARD)])
        # clam turns readonly+focused combobox text white (meant for its blue field); our field is white,
        # so the chosen name vanished whenever the box was clicked. Keep it dark.
        st.map("TCombobox", foreground=[("disabled", MUTED), ("readonly", "focus", TEXT)],
               selectbackground=[("readonly", CARD)], selectforeground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 10))

    def header(self):
        bar = tk.Frame(self.root, bg=HEADER, padx=20, pady=14)
        bar.pack(fill="x")
        self.header_logo = ImageTk.PhotoImage(make_logo(36))
        tk.Label(bar, image=self.header_logo, bg=HEADER).pack(side="left")
        titles = tk.Frame(bar, bg=HEADER)
        titles.pack(side="left", padx=(10, 0))
        tk.Label(titles, text=APP, bg=HEADER, fg="white", font=(FONT, 14, "bold")).pack(anchor="w")
        status = tk.Frame(titles, bg=HEADER)
        status.pack(anchor="w")
        self.dot = tk.Label(status, text="●", bg=HEADER, font=(FONT, 7))
        self.dot.pack(side="left")
        self.dot_text = tk.Label(status, bg=HEADER, fg="#9ca3af", font=(FONT, 8))
        self.dot_text.pack(side="left", padx=(3, 0))
        self.paint_printer_status()
        link = tk.Label(bar, text="Label sizes", bg=HEADER, fg="#9ca3af", font=(FONT, 9, "underline"), cursor="hand2")
        link.pack(side="right")
        link.bind("<Button-1>", lambda e: self.open_sizes())
        link.bind("<Enter>", lambda e: link.config(fg="white"))
        link.bind("<Leave>", lambda e: link.config(fg="#9ca3af"))

    def printer_picker(self, parent):
        tk.Label(parent, text="SELECT PRINTER", bg=BG, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w", pady=(0, 6))
        self.printer_box = ttk.Combobox(parent, textvariable=self.printer, values=self.printers,
                                        state="readonly", font=(FONT, 10), postcommand=self.refresh_printers)
        self.printer_box.pack(fill="x", pady=(0, 16))
        self.printer_box.bind("<<ComboboxSelected>>", lambda e: self.on_printer())

    def refresh_printers(self):
        """Re-read installed printers each time the dropdown opens, so newly added ones appear."""
        self.printers = list_printers()
        self.printer_box["values"] = self.printers
        self.paint_printer_status()

    def on_printer(self):
        self.paint_printer_status()
        try:
            self.remember()
        except ValueError:
            pass  # half-typed setting; printer choice still saved on print/close

    def paint_printer_status(self):
        name = self.printer.get()
        ok = name in self.printers
        self.dot.config(fg=OK if ok else BAD)
        self.dot_text.config(text=f"{name} connected" if ok else "No printer selected")

    def drop_zone(self, parent):
        self.zone = tk.Frame(parent, bg=CARD, highlightthickness=2, highlightbackground=BORDER,
                             cursor="hand2", height=110)
        self.zone.pack(fill="x")
        self.zone.pack_propagate(False)
        self.zone_title = tk.Label(self.zone, text="Drop a PDF here", bg=CARD, fg=TEXT, font=(FONT, 13, "bold"))
        self.zone_sub = tk.Label(self.zone, text="or click to browse", bg=CARD, fg=MUTED, font=(FONT, 9))
        self.zone_title.pack(expand=True, anchor="s")
        self.zone_sub.pack(expand=True, anchor="n")
        for w in (self.zone, self.zone_title, self.zone_sub):
            w.bind("<Button-1>", lambda e: self.set_file(filedialog.askopenfilename(filetypes=[("PDF", "*.pdf")])))
        self.zone.drop_target_register(DND_FILES)
        self.zone.dnd_bind("<<DropEnter>>", lambda e: self.zone.config(highlightbackground=ACCENT) or e.action)
        self.zone.dnd_bind("<<DropLeave>>", lambda e: self.zone.config(highlightbackground=self.zone_border()) or e.action)
        self.zone.dnd_bind("<<Drop>>", self.on_drop)

    def section_title(self, parent, text):
        tk.Label(parent, text=text, bg=BG, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w", pady=(18, 6))

    def size_picker(self, parent):
        self.section_title(parent, "LABEL SIZE")
        self.size_row = tk.Frame(parent, bg=BG)
        self.size_row.pack(fill="x")
        for c in (0, 1):
            self.size_row.columnconfigure(c, weight=1, uniform="s")
        self.build_size_buttons()

    def build_size_buttons(self):
        """Two buttons per row; an odd last one spans the full width."""
        for w in self.size_row.winfo_children():
            w.destroy()
        self.size_btns = {}
        names = list(self.sizes)
        for i, name in enumerate(names):
            b = tk.Button(self.size_row, text=name, font=(FONT, 11, "bold"), relief="flat", bd=0, cursor="hand2",
                          pady=9, highlightthickness=1, command=lambda n=name: self.pick_size(n))
            span = 2 if i == len(names) - 1 and i % 2 == 0 else 1
            b.grid(row=i // 2, column=i % 2, columnspan=span, sticky="ew",
                   padx=(0, 8) if i % 2 == 0 and span == 1 else 0, pady=(8 if i >= 2 else 0, 0))
            self.size_btns[name] = b
        self.paint_sizes()

    def open_sizes(self):
        """Small window to add or remove label sizes."""
        win = tk.Toplevel(self.root, bg=BG, padx=20, pady=16)
        win.title("Label sizes")
        win.resizable(False, False)
        win.transient(self.root)

        tk.Label(win, text="YOUR SIZES", bg=BG, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w", pady=(0, 6))
        listing = tk.Frame(win, bg=CARD, highlightthickness=1, highlightbackground=BORDER, padx=12, pady=8)
        listing.pack(fill="x")

        def redraw():
            for w in listing.winfo_children():
                w.destroy()
            for name, (w_mm, h_mm) in self.sizes.items():
                row = tk.Frame(listing, bg=CARD)
                row.pack(fill="x", pady=2)
                tk.Label(row, text=name, bg=CARD, fg=TEXT, font=(FONT, 10, "bold")).pack(side="left")
                tk.Label(row, text=f"  {w_mm:g} wide, {h_mm:g} tall", bg=CARD, fg=MUTED, font=(FONT, 9)).pack(side="left")
                if len(self.sizes) > 1:
                    tk.Button(row, text="Remove", font=(FONT, 9), fg=BAD, bg=CARD, activebackground=BG, relief="flat",
                              bd=0, cursor="hand2", command=lambda n=name: remove(n)).pack(side="right")

        def remove(name):
            if not messagebox.askyesno("Remove size", f"Remove {name}?", parent=win):
                return
            if name == self.size.get():
                self.size.set(next(n for n in self.sizes if n != name))
                self.load_into_form()
            del self.sizes[name]
            self.settings.pop(name, None)
            save_settings(self.sizes, self.settings, self.size.get(), self.printer.get())
            self.build_size_buttons()
            redraw()

        tk.Label(win, text="ADD A SIZE", bg=BG, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w", pady=(16, 6))
        form = tk.Frame(win, bg=BG)
        form.pack(fill="x")
        w_var, h_var = tk.StringVar(), tk.StringVar()
        for c, (label, var) in enumerate((("Width (mm)", w_var), ("Height (mm)", h_var))):
            cell = tk.Frame(form, bg=BG)
            cell.grid(row=0, column=c, sticky="ew", padx=(0, 8))
            tk.Label(cell, text=label, bg=BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(0, 3))
            ttk.Entry(cell, textvariable=var, width=10, font=(FONT, 10)).pack(fill="x")

        def add():
            try:
                dims = (parse_mm(w_var.get(), "Width"), parse_mm(h_var.get(), "Height"))
            except ValueError as e:
                messagebox.showerror("Check size", str(e), parent=win)
                return
            same = [n for n, d in self.sizes.items() if d == dims]
            if same:
                messagebox.showinfo("Already there", f"You already have this size: {same[0]}", parent=win)
                return
            name = size_name(*dims)
            if name in self.sizes:  # e.g. built-in "30 x 60 mm" is 60 wide, so 30 wide x 60 tall clashes by name
                name = f"{dims[0]:g} wide x {dims[1]:g} tall mm"
            self.sizes[name] = dims
            self.settings[name] = dict(DEFAULTS)
            save_settings(self.sizes, self.settings, self.size.get(), self.printer.get())
            self.build_size_buttons()
            self.pick_size(name)
            w_var.set("")
            h_var.set("")
            redraw()

        tk.Button(form, text="Add size", font=(FONT, 10, "bold"), bg=ACCENT, fg="white", activebackground=ACCENT_DARK,
                  activeforeground="white", relief="flat", bd=0, cursor="hand2", padx=14, pady=5,
                  command=add).grid(row=0, column=2, sticky="sw")
        tk.Label(win, text="Width is across the printer, height is along the feed.", bg=BG, fg=MUTED,
                 font=(FONT, 8)).pack(anchor="w", pady=(10, 0))
        redraw()
        win.bind("<Return>", lambda e: add())
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + 60
        win.geometry(f"+{x}+{y}")
        try:  # modal; grabbing before the window is visible can fail on some systems
            win.wait_visibility()
            win.grab_set()
        except tk.TclError:
            pass

    def settings_card(self, parent):
        self.section_title(parent, "SETTINGS")
        card = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER, padx=16, pady=14)
        card.pack(fill="x")
        self.vars = {}
        for c, (key, label, opts) in enumerate(self.FIELDS):
            cell = tk.Frame(card, bg=CARD)
            cell.grid(row=0, column=c, sticky="ew", padx=(0, 12) if c < len(self.FIELDS) - 1 else 0)
            tk.Label(cell, text=label, bg=CARD, fg=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(0, 3))
            var = tk.StringVar()
            if isinstance(opts, list):
                w = ttk.Combobox(cell, textvariable=var, values=opts, state="readonly", width=8)
            else:
                w = ttk.Spinbox(cell, textvariable=var, from_=opts[0], to=opts[1], increment=opts[2], width=7)
            w.pack(fill="x")
            card.columnconfigure(c, weight=1, uniform="c")
            self.vars[key] = var

    # ---------- behaviour ----------
    def zone_border(self):
        return OK if self.path else BORDER

    def paint_sizes(self):
        for name, b in self.size_btns.items():
            on = name == self.size.get()
            b.config(bg=ACCENT if on else CARD, fg="white" if on else TEXT,
                     activebackground=ACCENT_DARK if on else BG, activeforeground="white" if on else TEXT,
                     highlightbackground=ACCENT if on else BORDER)

    def load_into_form(self):
        s = self.settings[self.size.get()]
        for key, var in self.vars.items():
            var.set(s[key])

    def read_form(self):
        s = {}
        for key, label, opts in self.FIELDS:
            raw = self.vars[key].get().strip()
            try:
                val = int(raw)
            except ValueError:
                raise ValueError(f"'{raw}' isn't a valid whole number for {label}.")
            lo, hi = (min(opts), max(opts)) if isinstance(opts, list) else opts[:2]
            if not lo <= val <= hi:
                raise ValueError(f"{label} must be between {lo} and {hi}.")
            s[key] = val
        return s

    def remember(self):
        """Store what's in the form for the current size. Returns the settings, raises on bad input."""
        s = self.read_form()
        self.settings[self.size.get()] = s
        save_settings(self.sizes, self.settings, self.size.get(), self.printer.get())
        return s

    def pick_size(self, name):
        if name == self.size.get():
            return
        try:
            self.remember()
        except ValueError as e:
            messagebox.showerror("Check settings", str(e))
            return
        self.size.set(name)
        save_settings(self.sizes, self.settings, name, self.printer.get())
        self.paint_sizes()
        self.load_into_form()

    def on_drop(self, e):
        self.set_file(self.root.tk.splitlist(e.data)[0])
        self.zone.config(highlightbackground=self.zone_border())
        return e.action

    def set_file(self, path):
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            messagebox.showerror("Not a PDF", "Please drop a .pdf file.")
            return
        try:
            with pymupdf.open(path) as doc:
                pages = len(doc)
        except Exception as e:
            messagebox.showerror("Can't open PDF", str(e))
            return
        self.path = path
        self.zone_title.config(text=os.path.basename(path), fg=TEXT)
        self.zone_sub.config(text=f"{pages} page{'s' if pages != 1 else ''}  ·  click to change", fg=OK)
        self.zone.config(highlightbackground=OK)
        self.set_status("PDF loaded. Ready to print.", MUTED)

    def set_status(self, text, color):
        self.status.config(text=text, fg=color)

    def go(self):
        if not self.path:
            messagebox.showwarning("No file", "Drop a PDF in first.")
            return
        if self.printer.get() not in self.printers:
            messagebox.showwarning("No printer", "Choose a printer from the dropdown first.")
            return
        try:
            s = self.remember()
            self.set_status("Printing…", MUTED)
            self.root.update_idletasks()
            n = print_pdf(self.path, self.sizes[self.size.get()], s, self.printer.get())
            total = n * s["copies"]
            self.set_status(f"Sent {total} label{'s' if total != 1 else ''} at {self.size.get()}", OK)
        except Exception as e:
            self.set_status("Print failed", BAD)
            messagebox.showerror("Print failed", str(e))

    def close(self):
        try:
            self.remember()
        except ValueError:
            pass  # don't block closing over a half-typed number; last good settings stay saved
        self.root.destroy()


def selftest():
    img = Image.new("1", (480, 240), 1)
    data = build_tspl([img, img], 60, 30, {**DEFAULTS, "copies": 2})
    assert data.startswith(b"SIZE 60 mm,30 mm\r\nGAP 3 mm,0 mm\r\nSPEED 1\r\nDENSITY 15\r\n")
    assert data.count(b"BITMAP 0,0,60,240,0,") == 2 and data.count(b"PRINT 1,2") == 2
    assert b"\xff" * (60 * 240) in data  # blank label = all-white bits
    assert default_printer(["HP", "Munbyn ITPP130"], "") == "Munbyn ITPP130"
    assert default_printer(["HP", "Canon"], "") == "" and default_printer(["HP"], "") == "HP"
    assert default_printer(["HP", "Canon"], "Canon") == "Canon"
    assert parse_mm(" 50.25 ", "W") == 50.2 and size_name(50.2, 25) == "50.2 x 25 mm"
    for bad in ("abc", "5", "400"):
        try:
            parse_mm(bad, "W")
            raise AssertionError(bad)
        except ValueError:
            pass
    assert build_tspl([], 50.5, 25, DEFAULTS).startswith(b"SIZE 50.5 mm,25 mm\r\n")
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--write-icon" in sys.argv:  # used when building the .exe
        make_logo(256).save(sys.argv[-1], sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    else:
        App().root.mainloop()
