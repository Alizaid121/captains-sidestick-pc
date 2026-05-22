"""
Captain's Sidestick — Pure Python Virtual Joystick Bridge
==========================================================
Receives flight‑sim axis/button data from a phone via WebSocket
and feeds it into a vJoy virtual joystick via pyvjoystick.

Architecture
------------
  Main thread   : tkinter UI  +  root.after() tick (UI only)
  Thread‑2      : asyncio event loop — WebSocket server + vJoy writes
                  vJoy.update() is called synchronously on every message,
                  in the same thread as receipt — zero queue, zero lag.
  Shared state  : _AppState dataclass protected by threading.Lock
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import font as tkfont
from typing import Optional

# ── Third-party (see requirements.txt) ──────────────────────────────────────
try:
    from pyvjoystick import vjoy as vj
    VJOY_OK = True
except Exception as _vje:
    VJOY_OK = False
    print(f"[WARN] pyvjoystick unavailable: {_vje}")

try:
    import websockets
    from websockets.server import WebSocketServerProtocol
    WEBSOCKETS_OK = True
except Exception as _wse:
    WEBSOCKETS_OK = False
    print(f"[WARN] websockets unavailable: {_wse}")

try:
    import qrcode
    from PIL import Image, ImageTk
    QR_OK = True
except Exception as _qre:
    QR_OK = False
    print(f"[WARN] qrcode/PIL unavailable: {_qre}")

try:
    import pystray
    from PIL import Image as PilImage
    TRAY_OK = True
except Exception as _traye:
    TRAY_OK = False
    print(f"[WARN] pystray unavailable: {_traye}")

# ── Constants ────────────────────────────────────────────────────────────────
WS_PORT       = 8888
UI_TICK_MS    = 100          # tkinter refresh interval
AXIS_DEADZONE = 0.01         # below this → treat as zero

# Colours — dark cockpit theme
BG_DARK   = "#1a1a2e"
BG_PANEL  = "#16213e"
BG_CARD   = "#0f3460"
AMBER     = "#e0a020"
AMBER_DIM = "#7a5510"
GREEN     = "#00e676"
RED       = "#ff1744"
GREY      = "#546e7a"
WHITE     = "#e8eaf6"

# ── Shared application state (thread-safe) ───────────────────────────────────
@dataclass
class _AppState:
    """All mutable state shared between threads.  Guard writes with _lock."""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # Axes  (float  –1.0 … +1.0 for sticks,  0.0 … 1.0 for triggers)
    pitch:    float = 0.0
    roll:     float = 0.0
    thr:      float = 0.0
    rud:      float = 0.0

    # Buttons — keyed exactly as the phone sends them
    buttons: dict = field(default_factory=lambda: {
        "toga": False, "idle": False, "reverse": False,
        "gear": False, "flapsUp": False, "flapsDown": False,
        "a": False, "b": False, "x": False, "y": False, "ptt": False,
    })

    # Status flags
    server_running:    bool = False
    client_connected:  bool = False
    controller_ready:  bool = False
    controller_error:  str  = ""   # human-readable vJoy error, empty when OK

    # Timestamp of last received packet (for connection watchdog)
    last_rx: float = 0.0

    def update_from_json(self, data: dict) -> None:
        """Parse one JSON packet and update axes/buttons under the lock."""
        with self._lock:
            self.pitch = float(data.get("pitch", 0.0))
            self.roll  = float(data.get("roll",  0.0))
            self.thr   = float(data.get("thr",   0.0))
            self.rud   = float(data.get("rud",   0.0))
            btns = data.get("buttons", {})
            for k in self.buttons:
                self.buttons[k] = bool(btns.get(k, False))
            self.last_rx = time.monotonic()

    def snapshot(self) -> dict:
        """Return a lock-free snapshot for the UI/gamepad thread."""
        with self._lock:
            return {
                "pitch":   self.pitch,
                "roll":    self.roll,
                "thr":     self.thr,
                "rud":     self.rud,
                "buttons": dict(self.buttons),
                "server_running":    self.server_running,
                "client_connected":  self.client_connected,
                "controller_ready":  self.controller_ready,
                "controller_error":  self.controller_error,
                "last_rx": self.last_rx,
            }

# ── vJoy error classifier ────────────────────────────────────────────────────
def get_vjoy_error_message(error_str: str) -> str:
    """Map a raw vJoy exception string to a specific, actionable UI message."""
    e = error_str.lower()
    if "dll" in e or "not found" in e or "module" in e:
        return "vJoy driver not installed. Download from github.com/jshafer817/vJoy/releases"
    elif "acquired" in e or "busy" in e:
        return "vJoy device busy. Open vJoy Monitor and check Device 1 is free"
    elif "enabled" in e or "disabled" in e:
        return "vJoy device disabled. Open Configure vJoy and enable Device 1"
    elif "configured" in e or "axes" in e:
        return "vJoy not configured. Open Configure vJoy, enable X Y Z Rx axes on Device 1"
    else:
        return f"vJoy error: {error_str}. Try restarting the app as Administrator"


# ── Virtual controller helper ────────────────────────────────────────────────
class VirtualController:
    """Wraps pyvjoystick.VJoyDevice(1) and provides safe axis/button writes.

    On acquisition failure the specific error is stored in _AppState so the
    UI can show it.  A background retry thread attempts re-acquisition every
    5 seconds — when vJoy becomes available the dot turns green automatically.
    """

    def __init__(self) -> None:
        self.joystick: Optional[vj.VJoyDevice] = None
        self.ready = False
        self._state: Optional["_AppState"] = None   # set by start()

    # ── Axis conversion helpers ───────────────────────────────────────────────
    @staticmethod
    def _to_vjoy_axis(val: float) -> int:
        """Convert –1.0…+1.0 float to vJoy range 1…32768 (centre = 16384)."""
        return int((val + 1.0) * 0.5 * 32767) + 1

    @staticmethod
    def _to_vjoy_throttle(val: float) -> int:
        """Convert 0.0…1.0 float to vJoy range 1…32768."""
        return int(val * 32767) + 1

    def start(self, state: "_AppState") -> bool:
        """Attempt vJoy acquisition.  On failure, store a helpful message in
        state and launch a background retry thread."""
        self._state = state
        success = self._try_acquire()
        if not success:
            # Retry every 5 s in a daemon thread so the UI stays live
            t = threading.Thread(target=self._retry_loop, name="vjoy-retry", daemon=True)
            t.start()
        return success

    def _try_acquire(self) -> bool:
        """Single acquisition attempt.  Returns True on success."""
        if not VJOY_OK:
            msg = get_vjoy_error_message("dll not found")
            self._set_error(msg)
            return False
        try:
            self.joystick = vj.VJoyDevice(1)

            # ── Wake-up sequence ──────────────────────────────────────────────
            # vJoy leaves the device uninitialised after acquisition.
            # Write all axes to neutral and pulse button 1 so X-Plane's
            # input scanner promotes the device from idle → active.
            CENTER       = self._to_vjoy_axis(0.0)     # 16384 — true centre
            THROTTLE_MIN = self._to_vjoy_throttle(0.0)  # 1 — closed

            self.joystick._data.wAxisX    = CENTER
            self.joystick._data.wAxisY    = CENTER
            self.joystick._data.wAxisZ    = THROTTLE_MIN
            self.joystick._data.wAxisXRot = CENTER
            self.joystick._data.lButtons  = 1    # button 1 pressed (bit 0)
            self.joystick.update()
            time.sleep(0.5)
            self.joystick._data.lButtons  = 0    # button 1 released
            self.joystick.update()

            self.ready = True
            self._set_ok()
            print("[CTRL] vJoy device 1 acquired and activated.")
            return True

        except Exception as exc:
            self.joystick = None
            self.ready    = False
            msg = get_vjoy_error_message(str(exc))
            self._set_error(msg)
            print(f"[CTRL] Acquisition failed: {msg}")
            return False

    def _retry_loop(self) -> None:
        """Retry acquisition every 5 seconds until successful."""
        while not self.ready:
            time.sleep(5)
            if self.ready:
                break
            print("[CTRL] Retrying vJoy acquisition…")
            self._try_acquire()

    # ── State helpers (thread-safe) ───────────────────────────────────────────
    def _set_ok(self) -> None:
        if self._state:
            with self._state._lock:
                self._state.controller_ready = True
                self._state.controller_error = ""

    def _set_error(self, msg: str) -> None:
        if self._state:
            with self._state._lock:
                self._state.controller_ready = False
                self._state.controller_error = msg

    def apply(self, snap: dict) -> None:
        """Write axes and buttons to the vJoy device via _data batch, then update()."""
        if not self.ready or self.joystick is None:
            return

        try:
            # ── Build button bitmask ──────────────────────────────────────────
            # Each bit corresponds to one button (bit 0 = button 1, etc.).
            # Using a single lButtons integer avoids the set_button/update
            # stale-data bug entirely — one _data write, one update() call.
            _BTN_BITS = {
                "toga": 0, "idle": 1, "reverse": 2,
                "gear": 3, "flapsUp": 4, "flapsDown": 5,
                "ptt": 6, "a": 7, "b": 8, "x": 9, "y": 10,
            }
            btns = snap["buttons"]
            mask = 0
            for name, bit in _BTN_BITS.items():
                if btns.get(name, False):
                    mask |= (1 << bit)

            # ── Write all axes + button mask directly to _data ────────────────
            # Never call set_axis() — it stages values in a secondary buffer
            # that update() overwrites with the _data struct, losing the write.
            # Writing to _data directly means update() sends exactly what we set.
            #
            # X  → roll          (–1.0…+1.0)
            # Y  → pitch inverted (pull-back = positive in sim)
            # Z  → throttle      (0.0…1.0)
            # RX → rudder        (–1.0…+1.0)
            self.joystick._data.wAxisX    = self._to_vjoy_axis(snap["roll"])
            self.joystick._data.wAxisY    = self._to_vjoy_axis(-snap["pitch"])
            self.joystick._data.wAxisZ    = self._to_vjoy_throttle(snap["thr"])
            self.joystick._data.wAxisXRot = self._to_vjoy_axis(snap["rud"])
            self.joystick._data.lButtons  = mask

            # ── Single update() flushes the complete _data struct to the driver ─
            self.joystick.update()

        except Exception as exc:
            print(f"[CTRL] apply() error: {exc}")

# ── WebSocket server (runs in asyncio thread) ────────────────────────────────
class WSServer:
    """
    Async WebSocket server.  Runs entirely inside a dedicated thread that
    owns an asyncio event loop.

    vJoy is updated synchronously inside _handler on every received message —
    same thread, no queue, no timer delay.
    """

    def __init__(self, state: _AppState, controller: "VirtualController") -> None:
        self._state      = state
        self._controller = controller
        self._loop:   Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server  = None   # websockets server object

    # ── Thread entry point ────────────────────────────────────────────────────
    def start_thread(self) -> None:
        self._thread = threading.Thread(
            target=self._run_loop, name="ws-loop", daemon=True
        )
        self._thread.start()

    def _run_loop(self) -> None:
        """Create a new event loop for this thread and run forever."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as exc:
            print(f"[WS] Event loop crashed: {exc}")
        finally:
            with self._state._lock:
                self._state.server_running = False

    # ── Async WebSocket server ────────────────────────────────────────────────
    async def _serve(self) -> None:
        if not WEBSOCKETS_OK:
            print("[WS] websockets library not available — server disabled.")
            return

        with self._state._lock:
            self._state.server_running = True

        print(f"[WS] Listening on ws://0.0.0.0:{WS_PORT}")

        # websockets ≥ 12 uses websockets.serve(); gracefully handle both APIs
        try:
            async with websockets.serve(
                self._handler,
                "0.0.0.0",
                WS_PORT,
                ping_interval=20,
                ping_timeout=30,
            ) as server:
                self._server = server
                await asyncio.Future()   # run until cancelled
        except OSError as exc:
            print(f"[WS] Could not bind port {WS_PORT}: {exc}")

    async def _handler(self, ws: "WebSocketServerProtocol") -> None:
        """Handle one client connection.

        vJoy is written synchronously on every packet — apply() is called
        in this thread immediately after parsing, with no intermediate queue
        or rate limit.  The 100 ms UI tick reads the same _AppState for
        display only and never touches vJoy.
        """
        peer = ws.remote_address
        print(f"[WS] Client connected: {peer}")
        with self._state._lock:
            self._state.client_connected = True
            self._state.last_rx = time.monotonic()

        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                    # 1. Update shared state (used by UI tick for display)
                    self._state.update_from_json(data)
                    # 2. Write to vJoy immediately — same thread, zero buffering
                    self._controller.apply(self._state.snapshot())
                except json.JSONDecodeError:
                    pass   # silently discard malformed packets

        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as exc:
            print(f"[WS] Handler error for {peer}: {exc}")
        finally:
            print(f"[WS] Client disconnected: {peer}")
            with self._state._lock:
                self._state.client_connected = False

# ── IP helpers ───────────────────────────────────────────────────────────────
def get_local_ip() -> str:
    """Return the machine's LAN IP (not 127.0.0.1)."""
    # Primary method: connect to an external address (no packet sent)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        pass
    # Fallback: gethostbyname
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "127.0.0.1"


def make_qr_image(text: str, size: int = 200) -> Optional["ImageTk.PhotoImage"]:
    """Generate a QR code and return a tkinter-compatible PhotoImage."""
    if not QR_OK:
        return None
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=6,
            border=2,
        )
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#e0a020", back_color="#0f3460")
        img = img.resize((size, size), Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except Exception as exc:
        print(f"[QR] Error generating QR: {exc}")
        return None

# ── Tkinter UI ───────────────────────────────────────────────────────────────
class AppUI:
    """
    Main window built with tkinter.
    All widget updates happen exclusively inside the main (tkinter) thread
    via root.after() callbacks.
    """

    def __init__(
        self,
        root: tk.Tk,
        state: _AppState,
        controller: VirtualController,
        ws_server: WSServer,
    ) -> None:
        self._root       = root
        self._state      = state
        self._ctrl       = controller
        self._ws         = ws_server
        self._tray_icon  = None
        self._hidden     = False
        self._qr_ref     = None   # keep PhotoImage alive (GC protection)

        self._build_window()
        self._build_widgets()
        self._schedule_tick()

        # Override close button → minimise to tray (or quit if tray unavail)
        root.protocol("WM_DELETE_WINDOW", self._on_close_button)

    # ── Window setup ─────────────────────────────────────────────────────────
    def _build_window(self) -> None:
        r = self._root
        r.title("Captain's Sidestick")
        r.configure(bg=BG_DARK)
        r.resizable(False, False)
        # Centre on screen
        r.update_idletasks()
        w, h = 540, 720
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        r.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    # ── Widget tree ──────────────────────────────────────────────────────────
    def _build_widgets(self) -> None:
        r = self._root
        pad = {"padx": 12, "pady": 4}

        # ── Title bar ────────────────────────────────────────────────────────
        hdr = tk.Frame(r, bg=BG_CARD, pady=10)
        hdr.pack(fill="x")
        tk.Label(
            hdr, text="✈  CAPTAIN'S SIDESTICK",
            font=("Consolas", 18, "bold"),
            bg=BG_CARD, fg=AMBER
        ).pack()
        tk.Label(
            hdr, text="Virtual Joystick Bridge  v2.2.1",
            font=("Consolas", 9),
            bg=BG_CARD, fg=AMBER_DIM
        ).pack()

        # ── IP / QR section ──────────────────────────────────────────────────
        conn_frame = tk.Frame(r, bg=BG_PANEL, pady=10)
        conn_frame.pack(fill="x", padx=10, pady=(10, 4))

        ip = get_local_ip()
        ws_url = f"ws://{ip}:{WS_PORT}"

        tk.Label(
            conn_frame, text="CONNECT YOUR PHONE",
            font=("Consolas", 10, "bold"), bg=BG_PANEL, fg=AMBER
        ).pack()
        self._ip_lbl = tk.Label(
            conn_frame, text=ws_url,
            font=("Consolas", 13, "bold"), bg=BG_PANEL, fg=WHITE
        )
        self._ip_lbl.pack(pady=(4, 8))

        # QR code
        qr_frame = tk.Frame(conn_frame, bg=BG_CARD, bd=2, relief="flat")
        qr_frame.pack()
        self._qr_label = tk.Label(qr_frame, bg=BG_CARD)
        self._qr_label.pack(padx=6, pady=6)
        self._update_qr(ws_url)

        # ── Status row ───────────────────────────────────────────────────────
        status_frame = tk.Frame(r, bg=BG_PANEL, pady=8)
        status_frame.pack(fill="x", padx=10, pady=4)

        tk.Label(
            status_frame, text="STATUS",
            font=("Consolas", 10, "bold"), bg=BG_PANEL, fg=AMBER
        ).pack()

        row = tk.Frame(status_frame, bg=BG_PANEL)
        row.pack()

        def _status_dot(parent, label_text: str) -> tk.Label:
            f = tk.Frame(parent, bg=BG_PANEL)
            f.pack(side="left", padx=16, pady=4)
            dot = tk.Label(f, text="●", font=("Consolas", 18), bg=BG_PANEL, fg=RED)
            dot.pack()
            tk.Label(
                f, text=label_text,
                font=("Consolas", 9), bg=BG_PANEL, fg=GREY
            ).pack()
            return dot

        self._dot_server = _status_dot(row, "SERVER")
        self._dot_device = _status_dot(row, "DEVICE")
        self._dot_ctrl   = _status_dot(row, "vJoy")

        # Error message shown in red below the dots when vJoy fails
        self._ctrl_error_lbl = tk.Label(
            status_frame, text="",
            font=("Consolas", 8), bg=BG_PANEL, fg=RED,
            wraplength=480, justify="center",
        )
        self._ctrl_error_lbl.pack(pady=(2, 0))

        # ── Axis display ─────────────────────────────────────────────────────
        axis_frame = tk.Frame(r, bg=BG_PANEL, pady=8)
        axis_frame.pack(fill="x", padx=10, pady=4)

        tk.Label(
            axis_frame, text="LIVE AXES",
            font=("Consolas", 10, "bold"), bg=BG_PANEL, fg=AMBER
        ).pack(pady=(0, 6))

        axes_grid = tk.Frame(axis_frame, bg=BG_PANEL)
        axes_grid.pack()

        def _axis_row(parent, row_idx: int, label: str) -> tuple:
            """Returns (value_label, bar_canvas)."""
            tk.Label(
                parent, text=f"{label:<10}",
                font=("Consolas", 11), bg=BG_PANEL, fg=GREY, anchor="w", width=10
            ).grid(row=row_idx, column=0, padx=(8, 4), pady=2, sticky="w")

            val_lbl = tk.Label(
                parent, text="  0.000",
                font=("Consolas", 11, "bold"), bg=BG_PANEL, fg=WHITE, width=8
            )
            val_lbl.grid(row=row_idx, column=1, padx=4)

            bar = tk.Canvas(
                parent, width=200, height=16,
                bg=BG_CARD, highlightthickness=0
            )
            bar.grid(row=row_idx, column=2, padx=(4, 8), pady=2)
            return val_lbl, bar

        self._pitch_val, self._pitch_bar = _axis_row(axes_grid, 0, "PITCH")
        self._roll_val,  self._roll_bar  = _axis_row(axes_grid, 1, "ROLL")
        self._thr_val,   self._thr_bar   = _axis_row(axes_grid, 2, "THROTTLE")
        self._rud_val,   self._rud_bar   = _axis_row(axes_grid, 3, "RUDDER")

        # ── Button display ────────────────────────────────────────────────────
        btn_frame = tk.Frame(r, bg=BG_PANEL, pady=8)
        btn_frame.pack(fill="x", padx=10, pady=4)

        tk.Label(
            btn_frame, text="BUTTON STATE",
            font=("Consolas", 10, "bold"), bg=BG_PANEL, fg=AMBER
        ).pack(pady=(0, 6))

        btn_grid = tk.Frame(btn_frame, bg=BG_PANEL)
        btn_grid.pack()

        self._btn_labels: dict[str, tk.Label] = {}
        btn_names = ["toga","idle","reverse","gear","flapsUp","flapsDown",
                     "a","b","x","y","ptt"]
        cols = 4
        for i, name in enumerate(btn_names):
            r_i, c_i = divmod(i, cols)
            lbl = tk.Label(
                btn_grid, text=name.upper(),
                font=("Consolas", 9, "bold"),
                bg=BG_CARD, fg=GREY,
                width=9, pady=4, relief="flat"
            )
            lbl.grid(row=r_i, column=c_i, padx=4, pady=2)
            self._btn_labels[name] = lbl

        # ── Footer ────────────────────────────────────────────────────────────
        footer = tk.Frame(r, bg=BG_DARK, pady=6)
        footer.pack(fill="x", side="bottom")
        tk.Label(
            footer,
            text="Minimise to tray · Right-click tray icon for options",
            font=("Consolas", 8), bg=BG_DARK, fg=AMBER_DIM
        ).pack()

    # ── QR code ──────────────────────────────────────────────────────────────
    def _update_qr(self, url: str) -> None:
        photo = make_qr_image(url, size=180)
        if photo:
            self._qr_ref = photo          # prevent GC
            self._qr_label.config(image=photo)
        else:
            # Fallback: just show URL text if PIL not available
            self._qr_label.config(
                text=url, font=("Consolas", 10), fg=AMBER, bg=BG_CARD, padx=10, pady=10
            )

    # ── Bar drawing helpers ───────────────────────────────────────────────────
    @staticmethod
    def _draw_bar_bipolar(canvas: tk.Canvas, value: float) -> None:
        """Draw a centred bar for values in –1…+1."""
        canvas.delete("all")
        W, H = 200, 16
        mid = W // 2
        v = max(-1.0, min(1.0, value))
        fill_w = int(abs(v) * (W // 2))
        if v >= 0:
            x0, x1 = mid, mid + fill_w
        else:
            x0, x1 = mid - fill_w, mid
        colour = GREEN if abs(v) > AXIS_DEADZONE else GREY
        canvas.create_rectangle(0, 0, W, H, fill=BG_DARK, outline="")
        canvas.create_line(mid, 0, mid, H, fill=AMBER_DIM, width=1)
        if fill_w > 0:
            canvas.create_rectangle(x0, 2, x1, H - 2, fill=colour, outline="")

    @staticmethod
    def _draw_bar_unipolar(canvas: tk.Canvas, value: float) -> None:
        """Draw a left-to-right bar for values in 0…1."""
        canvas.delete("all")
        W, H = 200, 16
        v = max(0.0, min(1.0, value))
        fill_w = int(v * W)
        colour = GREEN if v > AXIS_DEADZONE else GREY
        canvas.create_rectangle(0, 0, W, H, fill=BG_DARK, outline="")
        if fill_w > 0:
            canvas.create_rectangle(0, 2, fill_w, H - 2, fill=colour, outline="")

    # ── Main tick — called every UI_TICK_MS milliseconds ─────────────────────
    def _tick(self) -> None:
        """Refresh UI from shared state snapshot.

        vJoy writes happen in the WebSocket thread on every received packet,
        so _tick is UI-only — no controller writes here.
        """
        snap = self._state.snapshot()

        # ── 1. Connection watchdog: mark disconnected if >3 s silent ─────────
        if snap["client_connected"]:
            if time.monotonic() - snap["last_rx"] > 3.0:
                with self._state._lock:
                    self._state.client_connected = False
                snap["client_connected"] = False

        # ── 2. Status dots ────────────────────────────────────────────────────
        self._dot_server.config(fg=GREEN if snap["server_running"]   else RED)
        self._dot_device.config(fg=GREEN if snap["client_connected"] else RED)
        self._dot_ctrl.config(  fg=GREEN if snap["controller_ready"] else RED)

        # ── 3. vJoy error message — show when failed, clear when OK ──────────
        err = snap["controller_error"]
        self._ctrl_error_lbl.config(text=err)

        # ── 4. Axis labels + bars ─────────────────────────────────────────────
        def _fmt(v: float) -> str:
            return f"{v:+.3f}"

        self._pitch_val.config(text=_fmt(snap["pitch"]))
        self._roll_val.config( text=_fmt(snap["roll"]))
        self._thr_val.config(  text=f" {snap['thr']:.1%}")
        self._rud_val.config(  text=_fmt(snap["rud"]))

        self._draw_bar_bipolar( self._pitch_bar, snap["pitch"])
        self._draw_bar_bipolar( self._roll_bar,  snap["roll"])
        self._draw_bar_unipolar(self._thr_bar,   snap["thr"])
        self._draw_bar_bipolar( self._rud_bar,   snap["rud"])

        # ── 5. Button indicators ──────────────────────────────────────────────
        for name, lbl in self._btn_labels.items():
            pressed = snap["buttons"].get(name, False)
            lbl.config(
                bg=AMBER  if pressed else BG_CARD,
                fg=BG_DARK if pressed else GREY,
            )

        # ── Schedule next tick ────────────────────────────────────────────────
        self._schedule_tick()

    def _schedule_tick(self) -> None:
        self._root.after(UI_TICK_MS, self._tick)

    # ── System tray ──────────────────────────────────────────────────────────
    def _build_tray(self) -> None:
        if not TRAY_OK:
            return
        try:
            # Minimal 64×64 amber icon
            icon_img = PilImage.new("RGB", (64, 64), color="#1a1a2e")
            # Draw a simple plane silhouette using pixels
            for y in range(28, 36):
                for x in range(8, 56):
                    icon_img.putpixel((x, y), (224, 160, 32))
            for y in range(20, 44):
                for x in range(30, 34):
                    icon_img.putpixel((x, y), (224, 160, 32))

            menu = pystray.Menu(
                pystray.MenuItem("Show", self._tray_show, default=True),
                pystray.MenuItem("Exit", self._tray_exit),
            )
            self._tray_icon = pystray.Icon(
                "captains_sidestick",
                icon_img,
                "Captain's Sidestick",
                menu,
            )
            tray_thread = threading.Thread(
                target=self._tray_icon.run,
                name="tray-thread",
                daemon=True,
            )
            tray_thread.start()
        except Exception as exc:
            print(f"[TRAY] Could not create tray icon: {exc}")
            self._tray_icon = None

    def _tray_show(self, icon=None, item=None) -> None:
        """Restore window from tray."""
        self._root.after(0, self._restore_window)

    def _restore_window(self) -> None:
        self._root.deiconify()
        self._root.lift()
        self._root.focus_force()
        self._hidden = False

    def _tray_exit(self, icon=None, item=None) -> None:
        """Quit from tray."""
        self._root.after(0, self._quit)

    def _on_close_button(self) -> None:
        """Intercept the window X button — minimise to tray if available."""
        if TRAY_OK and self._tray_icon is None:
            self._build_tray()
        if self._tray_icon is not None:
            self._root.withdraw()
            self._hidden = True
        else:
            self._quit()

    def _quit(self) -> None:
        """Clean shutdown."""
        print("[APP] Shutting down…")
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self._root.destroy()

    # ── Public entry point ────────────────────────────────────────────────────
    def run(self) -> None:
        self._root.mainloop()


# ── Application bootstrap ─────────────────────────────────────────────────────
def main() -> None:
    print("=" * 60)
    print("  Captain's Sidestick  — starting up")
    print("=" * 60)

    # 1. Shared state
    state = _AppState()

    # 2. Virtual controller (create before UI so status shows correctly)
    ctrl = VirtualController()
    ctrl.start(state)   # state is passed so errors and retry can update it

    # 3. WebSocket server (background thread) — pass controller for direct apply()
    ws = WSServer(state, ctrl)
    if WEBSOCKETS_OK:
        ws.start_thread()
    else:
        print("[WS] websockets not installed — server will not start.")

    # 4. tkinter root
    root = tk.Tk()

    # Set DPI-aware on Windows so it doesn't look blurry
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    # 5. Build & run UI
    ui = AppUI(root, state, ctrl, ws)
    ui.run()

    print("[APP] Goodbye.")


if __name__ == "__main__":
    main()
