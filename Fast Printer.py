"""Fast Printer: drag a PDF in, pick a label size, click Print. Talks TSPL straight to the chosen
label printer, so the settings come from this app, not the printer driver. Runs on Windows and macOS."""
import ctypes
import json
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from logging.handlers import RotatingFileHandler
from tkinter import filedialog, messagebox, ttk

import pymupdf
from PIL import Image, ImageDraw, ImageTk
from tkinterdnd2 import DND_FILES, TkinterDnD

WINDOWS = sys.platform == "win32"
if WINDOWS:
    import win32print

APP = "Fast Printer"
APP_VERSION = "1.2.2"  # must match the release tag (v1.2.2); the build checks this
REPO = "Gromit1999/fast-printer"
PLACEHOLDER = "Select printer"
DOTS_PER_MM = 8  # 203 dpi
SIZES = {"100 x 150 mm": (100, 150), "30 x 60 mm": (60, 30)}  # built-in defaults: width, height in mm
MIN_MM, MAX_MM = 10, 300  # sanity bounds for custom sizes
GAP_MM = 3  # labels are always gapped, 3 mm gap, no offset
DEFAULTS = {"speed": 1, "darkness": 15, "copies": 1}
APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~/Library/Application Support"), "FastPrinter")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
LOG_FILE = os.path.join(APP_DIR, "log.txt")
log = logging.getLogger(APP)

# Palette
BG, CARD, BORDER, TEXT, MUTED = "#f3f4f6", "#ffffff", "#d7dbe2", "#111827", "#6b7280"
HEADER, ACCENT, ACCENT_DARK, OK, BAD = "#111827", "#2563eb", "#1d4ed8", "#16a34a", "#dc2626"
FONT = "Segoe UI" if WINDOWS else "Helvetica Neue"


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
    label = canvas.point(lambda v: 255 if v > 160 else 0).convert("1")
    # For blank labels: 0% black means the app made an empty picture (the PDF, or too faint: darkest over 160);
    # anything above 0% means there was something to print and it got lost on the Mac or in the printer.
    log.info("Page %d: %.2f%% black, darkest %d (0=black, 255=white), black area %s of %s; PDF has %d text chars, "
             "%d images, %d drawings, rotation %d, crop %s, media %s", page.number + 1,
             100 * label.histogram()[0] / (w_dots * h_dots), canvas.getextrema()[0],
             label.point(lambda v: 255 - v).getbbox(), label.size, len(page.get_text()), len(page.get_images()),
             len(page.get_drawings()), page.rotation, tuple(page.cropbox), tuple(page.mediabox))
    return label


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
        pages = [(round(p.rect.width), round(p.rect.height)) for p in doc]
        images = [page_to_label(p, round(w_mm * DOTS_PER_MM), round(h_mm * DOTS_PER_MM)) for p in doc]
    data = build_tspl(images, w_mm, h_mm, s)
    log.info("PRINT %r to %r: label %gx%g mm, %s, PDF pages (pt) %s, %d bytes\n%s", os.path.basename(path), printer,
             w_mm, h_mm, s, pages, len(data), data.split(b"CLS")[0].decode().replace("\r", ""))
    if not WINDOWS:  # macOS: hand the raw TSPL to CUPS, bypassing the driver's own rendering
        r = run(["lp", "-d", printer, "-o", "raw", "-t", os.path.basename(path)], data)
        if r.returncode:
            raise RuntimeError(r.stderr.decode(errors="replace").strip() or "The printer refused the job.")
        threading.Thread(target=follow_job, args=(printer, r.stdout.decode(errors="replace")), daemon=True).start()
        return len(images)
    hp = win32print.OpenPrinter(printer)
    try:
        win32print.StartDocPrinter(hp, 1, (os.path.basename(path), None, "RAW"))
        win32print.StartPagePrinter(hp)
        win32print.WritePrinter(hp, data)
        win32print.EndPagePrinter(hp)
        win32print.EndDocPrinter(hp)
    finally:
        win32print.ClosePrinter(hp)
    log.info("Sent to Windows spooler OK")
    return len(images)


def run(cmd, data=None):
    """Run a command and log it with its output."""
    r = subprocess.run(cmd, input=data, capture_output=True)
    log.info("$ %s  -> exit %s\n%s%s", " ".join(cmd), r.returncode,
             r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace"))
    return r


def follow_job(printer, lp_out):
    """Mac debugging: log how long CUPS takes to finish the job, then what the queue and CUPS say. A job that
    leaves the queue long before the label could physically finish points at CUPS cutting the print off."""
    m = re.search(r"request id is (\S+)", lp_out)
    if not m:
        return
    job, start = m.group(1), time.time()
    run(["lpstat", "-l", "-p", printer])  # state while printing: paused, offline, filter errors
    while time.time() - start < 120 and job in subprocess.run(["lpstat", "-o", printer], capture_output=True,
                                                               text=True).stdout.split():
        time.sleep(0.25)
    log.info("Job %s left the queue after %.1f s", job, time.time() - start)
    run(["lpstat", "-W", "completed", "-l", "-o", printer])
    run(["lpstat", "-l", "-p", printer])
    run(["lpstat", "-v", printer])
    run(["lpoptions", "-p", printer])  # driver name and saved options
    try:
        with open("/var/log/cups/error_log", errors="replace") as f:
            log.info("CUPS error_log (last 40 lines):\n%s", "".join(f.readlines()[-40:]))
    except OSError as e:
        log.info("CUPS error_log not readable: %s", e)


def setup_log():
    os.makedirs(APP_DIR, exist_ok=True)
    h = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    log.info("==== %s %s on %s %s (%s), Python %s", APP, APP_VERSION, platform.system(),
             platform.mac_ver()[0] or platform.release(), platform.machine(), platform.python_version())


def build_dump(printer):
    """Everything useful for debugging as one text: printer state right now, settings, then the logs oldest first."""
    out = [f"{APP} {APP_VERSION} on {platform.platform()}\nSelected printer: {printer}\nPrinters: {list_printers()}\n"]
    if not WINDOWS:
        for cmd in (["lpstat", "-t"], ["lpstat", "-W", "completed", "-l", "-o"], ["lpoptions", "-p", printer, "-l"],
                    ["lpoptions", "-p", printer]):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=10)
                text = (r.stdout + r.stderr).decode(errors="replace")
            except (OSError, subprocess.SubprocessError) as e:  # CUPS stuck or missing: the rest is still useful
                text = str(e)
            out.append(f"$ {' '.join(cmd)}\n{text}")
    for path in (SETTINGS_FILE, LOG_FILE + ".1", LOG_FILE):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                out.append(f"===== {os.path.basename(path)}\n{f.read()}")
        except OSError as e:
            out.append(f"===== {os.path.basename(path)}: {e.strerror}")
    return "\n".join(out)


def version_tuple(v):
    return tuple(int(x) for x in v.lstrip("v").split("."))


def mac_asset():
    return "Fast-Printer-Mac-Apple.zip" if platform.machine() == "arm64" else "Fast-Printer-Mac-Intel.zip"


VIRTUAL_PRINTERS = ("microsoft print to pdf", "microsoft xps", "onenote", "fax", "send to ")  # can't take TSPL


def list_printers():
    """Every printer installed for this user (local, USB and network), minus virtual ones."""
    if not WINDOWS:
        try:
            out = subprocess.run(["lpstat", "-e"], capture_output=True, text=True).stdout
        except OSError:
            return []
        return sorted({n.strip() for n in out.splitlines() if n.strip()}, key=str.lower)
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


class MacButton(tk.Label):
    """macOS draws tk.Button natively and ignores its colours (white text on grey), so use a clickable label."""

    def __init__(self, master, command=None, **kw):
        super().__init__(master, **kw)
        self.bind("<Button-1>", lambda e: command())


Button = tk.Button if WINDOWS else MacButton


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
        self.root.title(f"{APP} {APP_VERSION}")
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

        self.btn = Button(body, text="Print", font=(FONT, 13, "bold"), bg=ACCENT, fg="white",
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
        log.info("Printers: %s", self.printers)
        if not WINDOWS and getattr(sys, "frozen", False):  # only the built Mac app updates itself
            threading.Thread(target=self.check_update, daemon=True).start()
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
        for text, command in (("Dump logs", self.dump_logs), ("View logs", self.view_logs),
                              ("Label sizes", self.open_sizes)):
            link = tk.Label(bar, text=text, bg=HEADER, fg="#9ca3af", font=(FONT, 9, "underline"), cursor="hand2")
            link.pack(side="right", padx=(12, 0))
            link.bind("<Button-1>", lambda e, c=command: c())
            link.bind("<Enter>", lambda e, l=link: l.config(fg="white"))
            link.bind("<Leave>", lambda e, l=link: l.config(fg="#9ca3af"))

    def view_logs(self):
        """Open log.txt in the computer's text editor."""
        if WINDOWS:
            os.startfile(LOG_FILE)
        else:
            subprocess.run(["open", "-t", LOG_FILE])

    def dump_logs(self):
        """Save the whole log, settings and printer state as one text file, then show it so it can be sent on."""
        path = filedialog.asksaveasfilename(title="Save logs", defaultextension=".txt", filetypes=[("Text", "*.txt")],
                                            initialfile=time.strftime("Fast Printer logs %Y-%m-%d %H%M.txt"))
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(build_dump(self.printer.get()))
        except OSError as e:
            log.exception("Dump logs failed")
            messagebox.showerror("Couldn't save logs", str(e))
            return
        self.set_status(f"Logs saved to {os.path.basename(path)}", OK)
        if WINDOWS:
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
        else:
            subprocess.run(["open", "-R", path])

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
            b = Button(self.size_row, text=name, font=(FONT, 11, "bold"), relief="flat", bd=0, cursor="hand2",
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
                    Button(row, text="Remove", font=(FONT, 9), fg=BAD, bg=CARD, activebackground=BG, relief="flat",
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

        Button(form, text="Add size", font=(FONT, 10, "bold"), bg=ACCENT, fg="white", activebackground=ACCENT_DARK,
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
            log.exception("Print failed")
            self.set_status("Print failed", BAD)
            messagebox.showerror("Print failed", str(e))

    # ---------- Mac auto-update ----------
    def check_update(self):
        """Background: ask GitHub for the newest release and offer it if it's newer than this one."""
        try:
            out = subprocess.run(["curl", "-fsSL", "--max-time", "10",
                                  f"https://api.github.com/repos/{REPO}/releases/latest"],
                                 capture_output=True, text=True, check=True).stdout
            latest = json.loads(out)["tag_name"]
            newer = version_tuple(latest) > version_tuple(APP_VERSION)
        except Exception as e:  # offline, rate-limited, odd tag: just skip this time
            log.info("Update check failed: %s", e)
            return
        log.info("Update check: running %s, latest %s", APP_VERSION, latest)
        if newer:
            self.root.after(0, lambda: self.offer_update(latest))

    def offer_update(self, latest):
        if not messagebox.askyesno("Update available", f"Fast Printer {latest.lstrip('v')} is available "
                                   f"(you have {APP_VERSION}).\n\nUpdate now? It takes a few seconds."):
            return
        # sys.executable is .../Fast Printer.app/Contents/MacOS/Fast Printer
        bundle = os.path.abspath(os.path.join(sys.executable, "..", "..", ".."))
        if not bundle.endswith(".app") or not os.access(os.path.dirname(bundle), os.W_OK):
            # e.g. still opened from Downloads (macOS runs it from a read-only copy), or no permission
            log.info("Can't replace %s, sending user to the download page", bundle)
            messagebox.showinfo("Update", "Fast Printer can't update itself from where it is. Move it into your "
                                "Applications folder first, or download the new version from the page that opens.")
            subprocess.run(["open", f"https://github.com/{REPO}/releases/latest"])
            return
        self.set_status("Downloading update…", MUTED)
        threading.Thread(target=self.install_update, args=(bundle,), daemon=True).start()

    def install_update(self, bundle):
        """Download and unzip the new app; a small script swaps it in once this one has quit, then reopens it.
        Downloaded with curl, so macOS doesn't quarantine it and there's no 'Open Anyway' step again."""
        tmp = tempfile.mkdtemp(prefix="fastprinter-update-")
        zip_path, new_app = os.path.join(tmp, "update.zip"), os.path.join(tmp, "Fast Printer.app")
        try:
            subprocess.run(["curl", "-fsSL", "--max-time", "300", "-o", zip_path,
                            f"https://github.com/{REPO}/releases/latest/download/{mac_asset()}"],
                           capture_output=True, check=True)
            subprocess.run(["ditto", "-x", "-k", zip_path, tmp], capture_output=True, check=True)
            if not os.path.isdir(new_app):
                raise RuntimeError("the download didn't contain Fast Printer.app")
        except Exception as e:
            log.exception("Update download failed")
            msg = str(e)
            self.root.after(0, lambda: (self.set_status("Update failed", BAD),
                                        messagebox.showerror("Update failed", msg)))
            return
        log.info("Update downloaded, swapping %s and relaunching", bundle)
        swap = 'while kill -0 "$1" 2>/dev/null; do sleep 0.3; done; rm -rf "$2" && mv "$3" "$2" && open "$2"'
        subprocess.Popen(["/bin/sh", "-c", swap, "swap", str(os.getpid()), bundle, new_app], start_new_session=True)
        self.root.after(0, self.close)

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
    for ink, black in ((None, False), ((0, 0, 0), True)):
        with pymupdf.open() as doc:
            page = doc.new_page(width=288, height=432)
            if ink:
                page.draw_rect(page.rect, fill=ink)
            assert (0 in page_to_label(page, 800, 1200).tobytes()) is black
    assert version_tuple("v1.10.0") > version_tuple("1.9.2") and not version_tuple("v1.2.0") > version_tuple("1.2.0")
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--write-icon" in sys.argv:  # used by the build: icon.ico (Windows) or icon.png (Mac)
        make_logo(512).save(sys.argv[-1], sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    else:
        setup_log()
        App().root.mainloop()
