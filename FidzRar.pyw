#!/usr/bin/env python3
"""
FidzRar - a small, friendly archive extractor for Windows.

Opens almost anything: zip (including split .z01+.zip / .part1+.part2 / .001+.002),
7z, rar/rar5, tar, tar.gz, tgz, tar.bz2, tar.xz, tar.zst, gz, bz2, xz, zst,
lzma, cab, iso, udf, wim, arj, lzh, chm, msi, rpm, deb, cpio, dmg, squashfs...

Built on 7-Zip (bundled) with a pure-Python fallback for zip + tar families.

Files: FidzRar.pyw (this), archive_engine.py, extract_span_zip.py, bin\\7z.exe,
bin\\7z.dll, bin\\License.txt (7-Zip is LGPL - see bin/License.txt).
"""

import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NAME = "FidzRar"
APP_SUB = ("zip  \u2022  7z  \u2022  rar  \u2022  tar/gz/bz2/xz/zst  \u2022  cab  \u2022  iso  \u2022  "
           "wim  \u2022  split volumes (.z01, .partN, .001)  \u2022  drag & drop")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    import shell_menu
except Exception:                                    # pragma: no cover
    shell_menu = None

# Drag & drop is optional: the app still works if tkinterdnd2 is absent.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _TK_BASE = TkinterDnD.Tk
    HAVE_DND = True
except Exception:                                    # pragma: no cover
    DND_FILES = None
    _TK_BASE = tk.Tk
    HAVE_DND = False

FILE_TYPES = [
    ("All archives", "*.zip *.zipx *.7z *.rar *.r00 *.tar *.gz *.tgz *.bz2 *.tbz *.tbz2 "
                     "*.xz *.txz *.zst *.tzst *.lzma *.cab *.iso *.img *.udf *.wim *.swm "
                     "*.esd *.arj *.lzh *.lha *.chm *.msi *.rpm *.deb *.cpio *.dmg *.xar "
                     "*.z *.apk *.jar *.epub *.docx *.xlsx *.odt "
                     "*.z01 *.z02 *.001 *.002 *.part1 *.part2"),
    ("Split parts only", "*.z01 *.z02 *.z03 *.001 *.002 *.003 *.part1 *.part2 *.part3"),
    ("All files", "*.*"),
]


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.2f %s" % (n, unit))
        n /= 1024.0


def setup_dpi():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def default_dest(path):
    """Output folder next to the archive, named after it, minus part numbers."""
    folder = os.path.dirname(os.path.abspath(path))
    name = os.path.basename(path)
    stem = re.sub(r"(\.part\d+|\.z\d{2}|\.\d{3})$", "", name, flags=re.IGNORECASE)
    stem = os.path.splitext(stem)[0]
    if stem.lower().endswith(".tar"):
        stem = stem[:-4]
    return os.path.join(folder, stem or "extracted")


class FidzRar(_TK_BASE):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.info = None
        self.part = None
        self.worker = None
        self.cancel_ev = threading.Event()
        self.q = queue.Queue()
        self._last_prog = 0.0
        self._last_dir = os.path.expanduser("~")
        self._last_drop = None
        self._last_drop_t = 0.0

        self.title(APP_NAME)
        self.geometry("940x700")
        self.minsize(820, 600)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.var_part = tk.StringVar()
        self.var_dest = tk.StringVar()
        self.var_pw = tk.StringVar()
        self.var_info = tk.StringVar(value="Pick any archive or part of a split set to begin.")
        self.var_status = tk.StringVar(value="Ready.")
        self.var_open = tk.BooleanVar(value=True)

        self._build()

    # ------------------------------------------------------------------ layout
    def _build(self):
        self._build_menu()
        self.bind("<Control-o>", lambda _ev: self.browse_archive())

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        outer.rowconfigure(7, weight=1)

        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(head, text=APP_NAME, font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Label(head, text=APP_SUB, foreground="#666").pack(side="left", padx=12, pady=(6, 0))

        box = ttk.LabelFrame(outer, text=" Archive ", padding=8)
        box.grid(row=1, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="File:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        e = ttk.Entry(box, textvariable=self.var_part)
        e.grid(row=0, column=1, sticky="ew")
        e.bind("<Return>", lambda _ev: self.select_archive(self.var_part.get().strip()))
        ttk.Button(box, text="Browse\u2026", command=self.browse_archive, width=12).grid(
            row=0, column=2, padx=(6, 0))
        ttk.Label(box, text="Password:").grid(row=1, column=0, sticky="w", pady=(6, 0), padx=(0, 6))
        pw = ttk.Entry(box, textvariable=self.var_pw, show="\u2022")
        pw.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        pw.bind("<Return>", lambda _ev: self.select_archive(self.var_part.get().strip()))
        ttk.Label(box, text="(only if encrypted)", foreground="#777").grid(
            row=1, column=2, sticky="w", padx=(6, 0), pady=(6, 0))
        ttk.Label(box, textvariable=self.var_info, foreground="#555", wraplength=880).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        if HAVE_DND:
            ttk.Label(box, text="Tip: drag an archive from Explorer and drop it on this window.",
                      foreground="#999").grid(row=3, column=0, columnspan=3,
                                              sticky="w", pady=(2, 0))

        cbox = ttk.LabelFrame(outer, text=" Contents ", padding=8)
        cbox.grid(row=2, column=0, sticky="nsew", pady=10)
        cbox.columnconfigure(0, weight=1)
        cbox.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(cbox, columns=("name", "size"), show="headings", height=9)
        self.tree.heading("name", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.column("name", width=660, anchor="w")
        self.tree.column("size", width=110, anchor="e", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(cbox, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.tag_configure("dir", foreground="#777")
        self.tree.tag_configure("enc", foreground="#b00020")

        dbox = ttk.LabelFrame(outer, text=" Extract to ", padding=8)
        dbox.grid(row=3, column=0, sticky="ew")
        dbox.columnconfigure(1, weight=1)
        ttk.Label(dbox, text="Folder:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(dbox, textvariable=self.var_dest).grid(row=0, column=1, sticky="ew")
        ttk.Button(dbox, text="Browse\u2026", command=self.browse_dest, width=12).grid(
            row=0, column=2, padx=(6, 0))
        ttk.Button(dbox, text="Reset", command=self.reset_dest, width=8).grid(
            row=0, column=3, padx=(6, 0))

        pbox = ttk.Frame(outer)
        pbox.grid(row=4, column=0, sticky="ew", pady=(10, 4))
        pbox.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(pbox, mode="determinate", maximum=1000)
        self.bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(pbox, textvariable=self.var_status).grid(row=1, column=0, sticky="w", pady=(4, 0))

        bbox = ttk.Frame(outer)
        bbox.grid(row=5, column=0, sticky="ew", pady=(6, 8))
        self.btn_extract = ttk.Button(bbox, text="Extract", command=lambda: self.start("extract"))
        self.btn_extract.pack(side="left")
        self.btn_verify = ttk.Button(bbox, text="Test archive", command=lambda: self.start("test"))
        self.btn_verify.pack(side="left", padx=6)
        self.btn_cancel = ttk.Button(bbox, text="Cancel", command=self.do_cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        self.btn_open = ttk.Button(bbox, text="Open folder", command=self.open_folder)
        self.btn_open.pack(side="left", padx=6)
        ttk.Checkbutton(bbox, text="Open folder when finished", variable=self.var_open).pack(side="right")

        lbox = ttk.LabelFrame(outer, text=" Log ", padding=6)
        lbox.grid(row=7, column=0, sticky="nsew")
        lbox.columnconfigure(0, weight=1)
        lbox.rowconfigure(0, weight=1)
        self.log = tk.Text(lbox, height=6, wrap="none", state="disabled",
                           background="#fbfbfb", relief="flat", font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(lbox, orient="vertical", command=self.log.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=lsb.set)
        self.log.tag_configure("err", foreground="#b00020")
        self.log.tag_configure("dim", foreground="#777")
        self.log.tag_configure("ok", foreground="#0a6b2d")

        if HAVE_DND:
            for widget in (self, outer, self.tree):
                try:
                    widget.drop_target_register(DND_FILES)
                    widget.dnd_bind("<<Drop>>", self._on_drop)
                except Exception:
                    pass

    def _build_menu(self):
        bar = tk.Menu(self)

        file_menu = tk.Menu(bar, tearoff=0)
        file_menu.add_command(label="Open archive\u2026", accelerator="Ctrl+O",
                              command=self.browse_archive)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        bar.add_cascade(label="File", menu=file_menu)

        tools = tk.Menu(bar, tearoff=0)
        tools.add_command(label="Add to right-click menu", command=self.install_menu)
        tools.add_command(label="Remove from right-click menu", command=self.uninstall_menu)
        tools.add_separator()
        tools.add_command(label="Supported formats\u2026", command=self.show_formats)
        bar.add_cascade(label="Tools", menu=tools)

        help_menu = tk.Menu(bar, tearoff=0)
        help_menu.add_command(label="About FidzRar", command=self.show_about)
        bar.add_cascade(label="Help", menu=help_menu)

        self.configure(menu=bar)

    # ------------------------------------------------------------------ helpers
    def _log(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _busy(self, busy):
        state = "disabled" if busy else "normal"
        for b in (self.btn_extract, self.btn_verify, self.btn_open):
            b.configure(state=state)
        self.btn_cancel.configure(state="normal" if busy else "disabled")

    # -------------------------------------------------------------- drag & drop
    def _on_drop(self, event):
        """Load the first archive dropped from Explorer."""
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "Wait for the current job to finish first.")
            return "break"
        try:
            paths = [p for p in self.tk.splitlist(event.data) if p]
        except Exception:
            paths = [event.data]
        if not paths:
            return "break"

        path = paths[0]
        now = time.time()
        # A drop can reach more than one registered widget; only handle it once.
        if path == self._last_drop and now - self._last_drop_t < 0.6:
            return "break"
        self._last_drop, self._last_drop_t = path, now

        if len(paths) > 1:
            self._log("Dropped %d items - opening the first: %s"
                      % (len(paths), os.path.basename(path)), "dim")
        self.select_archive(path)
        return "break"

    # ----------------------------------------------------------- Explorer menu
    def install_menu(self):
        if not shell_menu or not shell_menu.available():
            messagebox.showinfo(APP_NAME, "The right-click menu is only available on Windows.")
            return
        try:
            n = shell_menu.install()
        except Exception as ex:
            self._log("Could not add the right-click menu: %s" % ex, "err")
            messagebox.showerror(APP_NAME, "Could not add the right-click menu:\n\n%s" % ex)
            return
        self._log("Added 'Extract with FidzRar' for %d file types." % n, "ok")
        messagebox.showinfo(
            APP_NAME,
            "Added 'Extract with FidzRar' to the right-click menu for %d file types.\n\n"
            "Right-click an archive (or the first part of a split set) and choose it.\n\n"
            "On Windows 11 it may sit under 'Show more options'." % n)

    def uninstall_menu(self):
        if not shell_menu or not shell_menu.available():
            messagebox.showinfo(APP_NAME, "The right-click menu is only available on Windows.")
            return
        n = shell_menu.uninstall()
        self._log("Removed the right-click menu from %d file types." % n, "dim")
        messagebox.showinfo(APP_NAME, "Removed the right-click menu from %d file types." % n)

    def show_formats(self):
        messagebox.showinfo(APP_NAME, self.engine.formats_summary())

    def show_about(self):
        sevenz = self.engine.find_7z() or "not found"
        dnd = "yes" if HAVE_DND else "no (tkinterdnd2 not installed)"
        menu = "yes" if (shell_menu and shell_menu.is_installed()) else "no"
        messagebox.showinfo(
            APP_NAME,
            "%s - a small, friendly archive extractor.\n\n"
            "7-Zip engine: %s\n"
            "Drag & drop: %s\n"
            "Right-click menu: %s\n\n"
            "7-Zip is (c) Igor Pavlov, LGPL-2.1+.\n"
            "Its licence is in bin\\License.txt." % (APP_NAME, sevenz, dnd, menu))

    def open_and_extract(self, path):
        """Used by the Explorer verb: load a file, then start extracting."""
        self.select_archive(path)
        if self.info:
            self.start("extract")

    # -------------------------------------------------------------- selection
    def browse_archive(self):
        path = filedialog.askopenfilename(
            title="Select an archive, or any part of a split set",
            initialdir=self._last_dir, filetypes=FILE_TYPES)
        if path:
            self.select_archive(path)

    def select_archive(self, path, quiet=False):
        path = (path or "").strip().strip('"')
        if not path:
            return
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            info = self.engine.probe(path, password=self.var_pw.get())
        except Exception as ex:
            if not quiet:
                messagebox.showerror(APP_NAME, str(ex))
            self.info = None
            self.part = None
            self.var_info.set(str(ex))
            return
        finally:
            self.configure(cursor="")

        self.info = info
        self.part = path
        self._last_dir = os.path.dirname(os.path.abspath(path))
        self.var_part.set(path)
        self.var_dest.set(default_dest(path))
        self.var_info.set(self._describe(info))
        self.var_status.set("Ready.")
        self.bar.configure(value=0)
        self._fill_tree(info["entries"])
        self._clear_log()
        self._log("Loaded %s  [%s]" % (os.path.basename(path), info["type"]), "dim")
        if info["encrypted"] and not self.var_pw.get():
            self._log("This archive is encrypted - type the password above.", "err")

    def _describe(self, info):
        files = sum(1 for e in info["entries"] if not e["is_dir"])
        bits = [
            "format: %s" % info["type"],
            "%d file%s" % (files, "" if files == 1 else "s"),
            "%s unpacked" % human(info["unpacked"]),
        ]
        if info.get("parts", 1) > 1:
            bits.insert(1, "%d parts (%s)" % (info["parts"], info.get("style", "")))
        if info.get("nested"):
            bits.append("tar inside a compressor")
        if info["encrypted"]:
            bits.append("encrypted")
        engine_name = {"sevenzip": "7-Zip", "sevenzip_joined": "7-Zip",
                       "tar": "built-in tar", "python_zip": "built-in zip",
                       "python_zip_single": "built-in zip"}.get(info["engine"], info["engine"])
        bits.append("engine: %s" % engine_name)
        return "  \u2022  ".join(bits)

    def _fill_tree(self, entries):
        self.tree.delete(*self.tree.get_children())
        for e in entries:
            tags = []
            if e["is_dir"]:
                tags.append("dir")
            if e.get("encrypted"):
                tags.append("enc")
            self.tree.insert("", "end",
                             values=(e["name"] + ("/" if e["is_dir"] else ""),
                                     "" if e["is_dir"] else human(e["size"])),
                             tags=tuple(tags))

    def browse_dest(self):
        start = self.var_dest.get() or self._last_dir
        path = filedialog.askdirectory(title="Where should the files go?",
                                       initialdir=start if os.path.isdir(start) else self._last_dir)
        if path:
            self.var_dest.set(path)

    def reset_dest(self):
        if self.part:
            self.var_dest.set(default_dest(self.part))

    def open_folder(self):
        dest = self.var_dest.get().strip()
        if dest and os.path.isdir(dest):
            os.startfile(dest)
        else:
            messagebox.showinfo(APP_NAME, "That folder does not exist yet.")

    # ------------------------------------------------------------------- run
    def start(self, mode):
        if self.worker and self.worker.is_alive():
            return
        if not self.info or not self.part:
            messagebox.showinfo(APP_NAME, "Choose an archive first.")
            return
        if mode == "extract":
            dest = self.var_dest.get().strip()
            if not dest:
                messagebox.showinfo(APP_NAME, "Choose an output folder.")
                return
            if os.path.isdir(dest) and os.listdir(dest):
                if not messagebox.askyesno(APP_NAME,
                                           "This folder already has files in it:\n\n%s\n\n"
                                           "Matching files will be overwritten. Continue?" % dest):
                    return

        self._clear_log()
        self.bar.configure(value=0)
        self.var_status.set("Starting\u2026")
        self._busy(True)
        self.cancel_ev = threading.Event()
        self._last_prog = 0.0
        self.worker = threading.Thread(
            target=self._work,
            args=(self.part, self.var_dest.get().strip(), self.var_pw.get(), mode),
            daemon=True)
        self.worker.start()
        self.after(80, self._poll)

    def _work(self, part, dest, password, mode):
        try:
            info = self.engine.probe(part, password=password)
            ok, fail, errs = self.engine.extract(
                info, dest, password=password, mode=mode,
                on_progress=self._on_progress, on_file=self._on_file,
                cancel=self.cancel_ev)
            self.q.put(("done", ok, fail, errs, dest, mode))
        except self.engine.Cancelled:
            self.q.put(("cancelled",))
        except Exception as ex:
            self.q.put(("fatal", str(ex)))

    def _on_progress(self, done, total):
        now = time.time()
        if now - self._last_prog >= 0.08 or done >= total:
            self._last_prog = now
            self.q.put(("progress", done, total))

    def _on_file(self, name, size, good, err):
        self.q.put(("file", name, size, good, err))

    def do_cancel(self):
        if self.worker and self.worker.is_alive():
            self.cancel_ev.set()
            self.var_status.set("Cancelling\u2026")

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]

                if kind == "progress":
                    _, done, total = msg
                    pct = 0.0 if not total else done * 100.0 / total
                    self.bar.configure(value=int(pct * 10))
                    self.var_status.set("%.1f%%" % pct)

                elif kind == "file":
                    _, name, size, good, err = msg
                    short = name.split("/")[-1]
                    if good:
                        self._log("  ok   %-72s %10s" % (short[:72], human(size)), "ok")
                    else:
                        self._log("  FAIL %-72s %s" % (short[:72], err), "err")

                elif kind == "done":
                    _, ok, fail, errs, dest, mode = msg
                    self.bar.configure(value=1000)
                    self._busy(False)
                    verb = "Extracted" if mode == "extract" else "Tested"
                    self.var_status.set("%s %d file%s, %d failed." % (
                        verb, ok, "" if ok == 1 else "s", fail))
                    if errs:
                        self._log("")
                        for n, m in errs[:200]:
                            self._log("   %s -> %s" % (n, m), "err")
                        messagebox.showwarning(APP_NAME, "%s with %d problem(s).\n"
                                               "See the log." % (verb, len(errs)))
                    else:
                        self._log("")
                        self._log("Everything OK.", "ok")
                        messagebox.showinfo(APP_NAME, "%s %d file(s) successfully.%s" % (
                            verb, ok, "\n\n" + dest if mode == "extract" else ""))
                        if mode == "extract" and self.var_open.get():
                            os.startfile(dest)

                elif kind == "cancelled":
                    self._busy(False)
                    self.var_status.set("Cancelled.")
                    self._log("Cancelled by user.", "dim")

                elif kind == "fatal":
                    self._busy(False)
                    self.var_status.set("Failed.")
                    self._log(msg[1], "err")
                    messagebox.showerror(APP_NAME, msg[1])

        except queue.Empty:
            pass

        if self.worker and self.worker.is_alive():
            self.after(80, self._poll)

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, "A job is still running. Stop it and quit?"):
                return
            self.cancel_ev.set()
        self.destroy()


def main():
    setup_dpi()
    try:
        import archive_engine as engine
    except Exception as ex:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(APP_NAME, "Could not load archive_engine.py\n\n%s\n\n"
                                       "Keep all FidzRar files together." % ex)
        r.destroy()
        return 1

    # Explorer menu maintenance, without opening a window:
    #   FidzRar.exe --install-menu / --remove-menu
    if "--install-menu" in sys.argv or "--remove-menu" in sys.argv:
        text, code = "", 0
        try:
            if not shell_menu or not shell_menu.available():
                raise RuntimeError("the right-click menu is only available on Windows")
            if "--install-menu" in sys.argv:
                n = shell_menu.install()
                text = "Added the right-click menu for %d file types." % n
            else:
                n = shell_menu.uninstall()
                text = "Removed the right-click menu from %d file types." % n
        except Exception as ex:
            text, code = "ERROR: %s" % ex, 1
        if sys.stdout is not None:
            print(text)
        return code

    # headless mode, handy for scripting and for verifying a frozen build:
    #   FidzRar.exe --cli archive.rar -o "D:\out" [-p password] [-t] [--log result.txt]
    if "--cli" in sys.argv:
        args = sys.argv[1:]
        arc = dest = pw = log = None
        mode = "extract"
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-o", "--out"):
                i += 1
                dest = args[i]
            elif a in ("-p", "--password"):
                i += 1
                pw = args[i]
            elif a in ("-t", "--test"):
                mode = "test"
            elif a == "--log":
                i += 1
                log = args[i]
            elif not a.startswith("--"):
                arc = a
            i += 1

        lines, code = [], 0
        try:
            info = engine.probe(arc, password=pw)
            target = dest or default_dest(arc)
            ok, fail, errs = engine.extract(info, target, password=pw, mode=mode)
            lines.append("archive=%s" % arc)
            lines.append("type=%s engine=%s parts=%d" % (
                info["type"], info["engine"], info.get("parts", 1)))
            lines.append("dest=%s" % target)
            lines.append("ok=%d fail=%d" % (ok, fail))
            for n, m in errs:
                lines.append("FAIL %s -> %s" % (n, m))
            code = 0 if (fail == 0 and not errs) else 1
        except Exception as ex:
            lines.append("ERROR: %s" % ex)
            code = 2

        text = "\n".join(lines)
        if log:
            try:
                with open(log, "w", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            except Exception:
                pass
        if sys.stdout is not None:
            print(text)
        return code

    app = FidzRar(engine)

    # Explorer "Extract with FidzRar": load the file, then start extracting.
    menu_path = None
    if "--extract" in sys.argv:
        i = sys.argv.index("--extract")
        if i + 1 < len(sys.argv):
            menu_path = sys.argv[i + 1]
    if menu_path:
        app.after(50, lambda: app.open_and_extract(menu_path))

    if "--selftest" in sys.argv:
        app.withdraw()
        lines = []
        extra = [a for a in sys.argv[1:] if not a.startswith("--")]
        try:
            if extra:
                app.select_archive(extra[0])
                app.update_idletasks()
                lines.append("entries=%d" % len(app.tree.get_children()))
                lines.append("info=%s" % app.var_info.get())
            app.update_idletasks()
            lines.append("7z=%s" % (engine.find_7z() or "NOT FOUND"))
            lines.append("dnd=%s" % ("yes" if HAVE_DND else "no"))
            if shell_menu and shell_menu.available():
                lines.append("menu=%d/%d" % (shell_menu.installed_count(),
                                             len(shell_menu.ARCHIVE_EXTS)))
            else:
                lines.append("menu=unavailable")
            lines.append("SELFTEST OK")
        finally:
            app.destroy()
        # windowed exe has no stdout, so record the result to a file as well
        try:
            import tempfile
            path = os.path.join(tempfile.gettempdir(), "fidzrar_selftest.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
            if sys.stdout is not None:
                print("\n".join(lines))
        except Exception:
            pass
        return 0

    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
