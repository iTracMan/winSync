"""winSync window: confirm Thetis is running, then start/stop the winSync server and watch its
output. Started by winSync.cmd (or the shortcuts it makes) as `.venv\\Scripts\\pythonw.exe -m
winSync.gui`. Uses only tkinter, which the python.org / winget Python includes.
"""
import atexit
import os
import queue
import re
import subprocess
import sys
import threading
import tomllib
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_ID = "M0AWS.winSync"      # Windows taskbar identity, so the window shows winSync's icon
DEFAULT_CAT_PORT = 13013
DEFAULT_WEB_PORT = 8090
WEBSDR_MAP_URL = "https://rx-tx.info/map-sdr-points#close"
# Two-way sync: [sync] control / --control value -> label in the window (and on the page)
CONTROL_CHOICES = {"radio": "Radio controls Kiwi", "kiwi": "Kiwi controls radio", "both": "Both"}
MAX_LOG_LINES = 5000
POLL_MS = 100
# "Address already in use" as aiohttp reports it on Windows (WSAEADDRINUSE) and elsewhere
PORT_BUSY_RE = re.compile(r"10048|address already in use|only one usage of each socket address", re.I)

# Same dark palette as the web page (static/style.css), which the icon (a dark logo) sits on well.
BG, PANEL, TEXT, MUTED = "#12161c", "#1b222c", "#e6edf3", "#8b97a5"
OK, BAD, WARN, ACCENT, BTN = "#3fb950", "#f85149", "#d29922", "#58a6ff", "#263041"


class SetupError(Exception):
    """winSync has not been set up (no virtual environment)."""


class ServerProcess:
    """Runs `<venv python> -u -m winSync` from the project root and streams its output.

    Events arrive on `self.events` as ("line", text) and finally ("exit", returncode).
    A fresh queue is used per start so a late event from a previous run can't leak into the next.
    The server's stdin is a pipe from this process: closing it (stop(), or this process ending in
    any way) makes the server exit, so no server is left holding the port.
    """

    def __init__(self, root=ROOT, argv=None, kill_after=5.0):
        self.root = Path(root)
        self.argv = argv               # override for tests
        self.kill_after = kill_after
        self.proc = None
        self.events = queue.Queue()

    def venv_python(self):
        py = self.root / ".venv" / "Scripts" / "python.exe"
        return py if py.exists() else None

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, follow=None, control=None):
        if self.running:
            return
        argv = self.argv
        if argv is None:
            py = self.venv_python()
            if py is None:
                raise SetupError("The winSync Python environment (.venv) was not found.\n\n"
                                 "Double-click winSync.cmd in the winSync folder to set it up.")
            # -u: unbuffered, so output shows live
            argv = [str(py), "-u", "-m", "winSync", "--exit-with-stdin"]
            if follow:
                argv += ["--follow", follow]
            if control:
                argv += ["--control", control]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # no console window for the server
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.events = q = queue.Queue()
        self.proc = proc = subprocess.Popen(
            argv, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=flags, env=env)
        threading.Thread(target=self._reader, args=(proc, q), daemon=True).start()

    @staticmethod
    def _reader(proc, q):
        for line in proc.stdout:
            q.put(("line", line.rstrip("\n")))
        q.put(("exit", proc.wait()))

    def stop(self, blocking=False):
        """Close the server's stdin (it exits at once), kill it after kill_after s if it hasn't."""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        t = threading.Thread(target=self._terminate, args=(proc,), daemon=True)
        t.start()
        if blocking:
            t.join(self.kill_after + 3)

    def _terminate(self, proc):
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(self.kill_after)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                pass


def read_config(root=ROOT):
    try:
        with open(Path(root) / "config.toml", "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def read_port(section, default, root=ROOT):
    """A port number from config.toml, or `default` if it can't be read."""
    try:
        return int(read_config(root).get(section, {}).get("port", default))
    except (TypeError, ValueError):
        return default


def read_control(root=ROOT):
    """[sync] control from config.toml: "radio", "kiwi" or "both" ("radio" if unset or invalid)."""
    control = read_config(root).get("sync", {}).get("control", "radio")
    return control if control in CONTROL_CHOICES else "radio"


def read_cat_port(root=ROOT):
    return read_port("cat", DEFAULT_CAT_PORT, root)


def page_url(root=ROOT):
    """Address of the winSync web page, from [server] in config.toml. A server listening on all
    addresses (0.0.0.0 / ::) is reached from this machine via 127.0.0.1."""
    host = str(read_config(root).get("server", {}).get("host", "127.0.0.1")).strip()
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):   # bare IPv6 literal
        host = f"[{host}]"
    return f"http://{host}:{read_port('server', DEFAULT_WEB_PORT, root)}/_winsync/"


_PLAIN_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)?)$", re.IGNORECASE)


def format_version(text):
    """"1", "0.3", "v0.3" -> "v0.30" (the "vx.xx" display format). Anything else (e.g. a
    three-part semver, or a git-describe string) is shown as-is, just with a leading 'v'."""
    text = text.strip()
    if not text:
        return None
    m = _PLAIN_VERSION_RE.match(text)
    if m:
        return f"v{float(m.group(1)):.2f}"
    return text if text.startswith(("v", "V")) else f"v{text}"


def read_version(root=ROOT):
    """The release version from VERSION, formatted for display, or None if there isn't one."""
    try:
        text = (Path(root) / "VERSION").read_text()
    except OSError:
        return None
    return format_version(text)


class DarkMessages:
    """Message boxes in winSync's dark colours (tkinter.messagebox always draws the light system
    box), offering the same calls (showinfo, showerror, askokcancel) using a small modal window.
    Enter presses the first button, Escape / closing the window the last."""

    KINDS = {"info": ("i", ACCENT), "error": ("!", BAD), "question": ("?", WARN)}

    def __init__(self, tk, parent):
        self.tk, self.parent = tk, parent

    def _show(self, title, message, kind, buttons):
        """buttons: [(label, result), ...]; the last one is also the Escape / close-window result."""
        tk = self.tk
        dlg = tk.Toplevel(self.parent, bg=BG)
        dlg.title(title)
        dlg.transient(self.parent)
        dlg.resizable(False, False)
        result = [buttons[-1][1]]

        def finish(value):
            result[0] = value
            dlg.destroy()

        body = tk.Frame(dlg, bg=BG)
        body.pack(fill="both", expand=True, padx=20, pady=(18, 8))
        glyph, colour = self.KINDS[kind]
        badge = tk.Canvas(body, width=40, height=40, bg=BG, highlightthickness=0)
        badge.create_oval(3, 3, 37, 37, fill=colour, outline="")
        badge.create_text(20, 20, text=glyph, fill=BG, font=("TkDefaultFont", 16, "bold"))
        badge.pack(side="left", anchor="n", padx=(0, 14))
        tk.Label(body, text=message, bg=BG, fg=TEXT, justify="left", anchor="w",
                 wraplength=420).pack(side="left", fill="both", expand=True)
        row = tk.Frame(dlg, bg=BG)
        row.pack(fill="x", padx=20, pady=(4, 16))
        first = None
        for label, value in reversed(buttons):
            b = tk.Button(row, text=label, width=9, command=lambda v=value: finish(v), bg=BTN,
                          fg=TEXT, activebackground="#2f3b50", activeforeground=TEXT,
                          relief="flat", padx=8, pady=4)
            b.pack(side="right", padx=(8, 0))
            first = b
        first.focus_set()
        dlg.bind("<Return>", lambda e: finish(buttons[0][1]))
        dlg.bind("<Escape>", lambda e: finish(buttons[-1][1]))
        dlg.protocol("WM_DELETE_WINDOW", lambda: finish(buttons[-1][1]))

        dlg.update_idletasks()                       # centre over the parent window
        x = self.parent.winfo_rootx() + (self.parent.winfo_width() - dlg.winfo_reqwidth()) // 2
        y = self.parent.winfo_rooty() + (self.parent.winfo_height() - dlg.winfo_reqheight()) // 2
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        try:
            dlg.wait_visibility()
            dlg.grab_set()
        except tk.TclError:
            pass                                     # window vanished / grab refused: still usable
        dlg.wait_window()
        return result[0]

    def showinfo(self, title, message, **_):
        self._show(title, message, "info", [("OK", True)])
        return "ok"

    def showerror(self, title, message, **_):
        self._show(title, message, "error", [("OK", True)])
        return "ok"

    def askokcancel(self, title, message, **_):
        return bool(self._show(title, message, "question", [("OK", True), ("Cancel", False)]))


def _set_app_id():
    """Without this, Windows groups the window under pythonw.exe and shows Python's icon."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


class App:
    def __init__(self):
        import tkinter as tk
        self.tk = tk
        self.server = ServerProcess()
        self.stopping = False
        self.port_busy = False

        _set_app_id()
        self.version = read_version()
        self.win = win = tk.Tk(className="winSync")
        win.title(f"winSync {self.version}" if self.version else "winSync")
        win.configure(bg=BG)
        self.messagebox = DarkMessages(tk, win)
        win.geometry("760x560")
        win.minsize(560, 380)
        self._load_icons()

        # Header: icon top-left, title beside it
        head = tk.Frame(win, bg=PANEL)
        head.pack(fill="x")
        if self.icon_header:
            tk.Label(head, image=self.icon_header, bg=PANEL).pack(side="left", padx=(12, 10), pady=8)
        titles = tk.Frame(head, bg=PANEL)
        titles.pack(side="left", pady=8)
        title_row = tk.Frame(titles, bg=PANEL)
        title_row.pack(anchor="w")
        tk.Label(title_row, text="winSync", bg=PANEL, fg=ACCENT, font=("TkDefaultFont", 18, "bold")
                 ).pack(side="left")
        if self.version:
            tk.Label(title_row, text=self.version, bg=PANEL, fg=MUTED,
                     font=("TkDefaultFont", 10)).pack(side="left", padx=(6, 0), pady=(6, 0))
        tk.Label(titles, text="Keeps a KiwiSDR or WebSDR tuned to Thetis", bg=PANEL, fg=MUTED
                 ).pack(anchor="w")

        # Step 1: the user confirms Thetis is running
        self.port = read_cat_port()
        self.confirmed = tk.BooleanVar(value=False)
        self.check = tk.Checkbutton(
            win, variable=self.confirmed, command=self._update_buttons, bg=BG, fg=TEXT,
            selectcolor=PANEL, activebackground=BG, activeforeground=TEXT, highlightthickness=0,
            anchor="w", justify="left",
            text="Thetis is running and its TCP/IP CAT server is enabled "
                 f"(Setup > Serial/Network/Midi CAT > Network, port {self.port})")
        self.check.pack(fill="x", padx=12, pady=(10, 4))

        # Step 2: start / stop
        bar = tk.Frame(win, bg=BG)
        bar.pack(fill="x", padx=12, pady=4)
        self.button = tk.Button(bar, text="Start winSync", width=14, command=self._toggle,
                                bg=BTN, fg=TEXT, activebackground="#2f3b50", activeforeground=TEXT,
                                disabledforeground=MUTED, relief="flat", padx=8, pady=4)
        self.button.pack(side="left")
        # Two-way sync, used when winSync starts (the page's Control selector changes it while running)
        ctl_row = tk.Frame(win, bg=BG)
        ctl_row.pack(fill="x", padx=12, pady=(2, 4), after=bar)
        self.control = tk.StringVar(value=CONTROL_CHOICES[read_control()])
        tk.Label(ctl_row, text="Control", bg=BG, fg=TEXT).pack(side="left", padx=(0, 6))
        self.control_menu = tk.OptionMenu(ctl_row, self.control, *CONTROL_CHOICES.values())
        self.control_menu.configure(bg=BTN, fg=TEXT, activebackground="#2f3b50", activeforeground=TEXT,
                                    disabledforeground=MUTED, relief="flat", highlightthickness=0,
                                    width=18, anchor="w")
        self.control_menu["menu"].configure(bg=PANEL, fg=TEXT, activebackground="#2f3b50",
                                            activeforeground=TEXT)
        self.control_menu.pack(side="left")
        tk.Label(ctl_row, text="used when winSync starts; change it on the page while running",
                 bg=BG, fg=MUTED).pack(side="left", padx=10)
        self.status = tk.Label(bar, text="Stopped", bg=BG, fg=MUTED, font=("TkDefaultFont", 11, "bold"))
        self.status.pack(side="left", padx=14)
        tk.Button(bar, text="Clear", command=self._clear, bg=BTN, fg=TEXT, activebackground="#2f3b50",
                  activeforeground=TEXT, relief="flat", padx=8, pady=4).pack(side="right")
        tk.Button(bar, text="Open websdr map", command=self._open_map, bg=BTN, fg=TEXT,
                  activebackground="#2f3b50", activeforeground=TEXT, relief="flat", padx=8,
                  pady=4).pack(side="right", padx=(0, 8))
        self.open_button = tk.Button(
            bar, text="Open winSync page", command=self._open_page, bg=BTN, fg=TEXT,
            activebackground="#2f3b50", activeforeground=TEXT, disabledforeground=MUTED,
            relief="flat", padx=8, pady=4)
        self.open_button.pack(side="right", padx=(0, 8))

        # Output field
        box = tk.Frame(win, bg=BG)
        box.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.log = tk.Text(box, bg="#0d1117", fg=TEXT, insertbackground=TEXT, font="TkFixedFont",
                           wrap="word", state="disabled", relief="flat", padx=8, pady=6,
                           highlightthickness=1, highlightbackground="#2d3748")
        bar_y = tk.Scrollbar(box, command=self.log.yview, bg=PANEL, troughcolor=BG, relief="flat")
        self.log.configure(yscrollcommand=bar_y.set)
        bar_y.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for tag, colour in (("err", BAD), ("warn", WARN), ("note", MUTED)):
            self.log.tag_configure(tag, foreground=colour)

        win.protocol("WM_DELETE_WINDOW", self._close)
        atexit.register(lambda: self.server.stop(blocking=True))
        self._update_buttons()
        self._append("Tick the box above once Thetis is running with its TCP/IP CAT server on, "
                     "then press Start winSync.", "note")
        win.after(POLL_MS, self._poll)

    def _load_icons(self):
        tk = self.tk
        self.icon_header = None
        self.icons = []
        assets = ROOT / "assets"
        for size in (256, 128, 48):
            f = assets / f"winsync-icon-{size}.png"
            if f.exists():
                self.icons.append(tk.PhotoImage(file=str(f)))
        head = assets / "winsync-icon-64.png"
        if head.exists():
            self.icon_header = tk.PhotoImage(file=str(head))
        if self.icons:
            self.win.iconphoto(True, *self.icons)

    # --- state -------------------------------------------------------------------------------
    def _update_buttons(self):
        running = self.server.running
        if self.stopping:
            self.button.configure(text="Stopping…", state="disabled")
        elif running:
            self.button.configure(text="Stop winSync", state="normal")
        else:
            self.button.configure(text="Start winSync",
                                  state="normal" if self.confirmed.get() else "disabled")
        self.check.configure(state="disabled" if running or self.stopping else "normal")
        self.control_menu.configure(state="disabled" if running or self.stopping else "normal")
        self.open_button.configure(state="normal" if running and not self.stopping else "disabled")

    def _open_page(self):
        self._open_url(page_url(), "Open the winSync page")

    def _open_map(self):
        self._open_url(WEBSDR_MAP_URL, "Open the WebSDR map")

    def _open_url(self, url, title):
        self._append(f"Opening {url}", "note")
        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
        if not opened:
            self.messagebox.showinfo(
                title,
                f"Could not start a web browser automatically.\n\nOpen this address in your browser:\n\n{url}")

    def _set_status(self, text, colour):
        self.status.configure(text=text, fg=colour)

    def _toggle(self):
        if self.server.running:
            self.stopping = True
            self._set_status("Stopping…", WARN)
            self.server.stop()
        else:
            control = next(k for k, v in CONTROL_CHOICES.items() if v == self.control.get())
            try:
                self.server.start(control=control)
            except SetupError as e:
                self.messagebox.showerror("winSync is not set up", str(e))
                return
            except OSError as e:
                self.messagebox.showerror("Could not start winSync", str(e))
                return
            self.port_busy = False
            self._append(f"--- winSync started (Control: {CONTROL_CHOICES[control]}) ---", "note")
            self._set_status("Running", OK)
        self._update_buttons()

    # --- output ------------------------------------------------------------------------------
    @staticmethod
    def _tag_for(line):
        low = line.lower()
        if "traceback" in low or "error" in low or "exception" in low:
            return "err"
        if "warning" in low:
            return "warn"
        return None

    def _append(self, text, tag=None):
        self._append_lines([(text, tag)])

    def _append_lines(self, items):
        log = self.log
        at_bottom = log.yview()[1] >= 0.999          # only follow the output if already at the end
        log.configure(state="normal")
        for text, tag in items:
            log.insert("end", text + "\n", tag or ())
        overflow = int(log.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if overflow > 0:
            log.delete("1.0", f"{overflow + 1}.0")
        log.configure(state="disabled")
        if at_bottom:
            log.see("end")

    def _clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _poll(self):
        items, exited = [], None
        q = self.server.events
        try:
            for _ in range(500):                      # bounded per tick so the UI stays responsive
                kind, value = q.get_nowait()
                if kind == "line":
                    if PORT_BUSY_RE.search(value):
                        self.port_busy = True
                    items.append((value, self._tag_for(value)))
                else:
                    exited = value
        except queue.Empty:
            pass
        if items:
            self._append_lines(items)
        if exited is not None:
            if self.stopping:
                self._append("--- winSync stopped ---", "note")
                self._set_status("Stopped", MUTED)
            else:
                if self.port_busy:
                    web = read_port("server", DEFAULT_WEB_PORT)
                    self._append(f"Port {web} is already in use. winSync may already be running "
                                 "(perhaps in another window), or another program is using that port. "
                                 "Close it and press Start again, or change [server] port in "
                                 "config.toml.", "warn")
                self._append(f"--- winSync exited unexpectedly (code {exited}) ---", "err")
                self._set_status(f"Exited (code {exited})", BAD)
            self.stopping = False
            self._update_buttons()
        self.win.after(POLL_MS, self._poll)

    def _close(self):
        self.server.stop(blocking=True)
        self.win.destroy()

    def run(self):
        self.win.mainloop()


def main():
    try:
        app = App()
    except Exception as e:                      # e.g. tkinter missing pieces
        msg = f"winSync: cannot open the window: {e}"
        if sys.stderr:
            print(msg, file=sys.stderr)
        else:                                   # pythonw.exe has no console to print to
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, msg, "winSync", 0x10)
            except Exception:
                pass
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
