"""
Captain's Sidestick — Pure Python Virtual Joystick Bridge
==========================================================
Receives flight‑sim axis/button data from a phone via WebSocket
and feeds it into a virtual Xbox 360 controller via ViGEmBus.

Architecture
------------
  Main thread   : tkinter UI  +  vgamepad writes  +  root.after() tick
  Thread‑2      : asyncio event loop that runs the WebSocket server
  Shared state  : _AppState dataclass protected by threading.Lock

All gamepad writes happen inside the tkinter main‑thread tick so we
never touch vgamepad from two threads simultaneously.
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
    import vgamepad as vg
    VGAMEPAD_OK = True
except Exception as _vge:
    VGAMEPAD_OK = False
    print(f"[WARN] vgamepad unavailable: {_vge}")

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
                "server_running":   self.server_running,
                "client_connected": self.client_connected,
                "controller_ready": self.controller_ready,
                "last_rx": self.last_rx,
            }

# ── Virtual controller helper ────────────────────────────────────────────────
class VirtualController:
    """Wraps vgamepad.VX360Gamepad and provides safe axis/button writes."""

    def __init__(self) -> None:
        self.gamepad: Optional[vg.VX360Gamepad] = None
        self.ready = False
        self._prev_buttons: dict = {}

    def start(self) -> bool:
        if not VGAMEPAD_OK:
            return False
        try:
            self.gamepad = vg.VX360Gamepad()
            self.gamepad.reset()
            self.gamepad.update()
            self.ready = True
            print("[CTRL] Virtual Xbox 360 controller created.")
            return True
        except Exception as exc:
            print(f"[CTRL] Failed to create controller: {exc}")
            return False

    # ── Axis helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))

    @staticmethod
    def _axis_to_short(v: float) -> int:
        """Convert –1.0…+1.0 float to –32768…32767 int (XInput range)."""
        return int(VirtualController._clamp(v, -1.0, 1.0) * 32767)

    @staticmethod
    def _trigger_to_byte(v: float) -> int:
        """Convert 0.0…1.0 float to 0…255 trigger byte."""
        return int(VirtualController._clamp(v, 0.0, 1.0) * 255)

    # ── Button map ───────────────────────────────────────────────────────────
    #   (XUSB_BUTTON constants from vgamepad)
    _BTN_MAP = {
        "a":        "XUSB_GAMEPAD_A",
        "b":        "XUSB_GAMEPAD_B",
        "x":        "XUSB_GAMEPAD_X",
        "y":        "XUSB_GAMEPAD_Y",
        "toga":     "XUSB_GAMEPAD_B",      # TOGA → B
        "idle":     "XUSB_GAMEPAD_X",      # idle → X
        "reverse":  "XUSB_GAMEPAD_Y",      # reverse → Y
        "gear":     "XUSB_GAMEPAD_LEFT_SHOULDER",    # LB
        "flapsUp":  "XUSB_GAMEPAD_RIGHT_SHOULDER",   # RB
        "ptt":      "XUSB_GAMEPAD_START",
    }

    def apply(self, snap: dict) -> None:
        """Write axes and buttons to the virtual controller, then update()."""
        if not self.ready or self.gamepad is None:
            return

        try:
            # ── Right stick → pitch (Y) + roll (X) ──────────────────────────
            #   Phone pitch: nose-up positive → invert for Y axis convention
            self.gamepad.right_joystick(
                x_value_float= snap["roll"],
                y_value_float=-snap["pitch"],   # invert: pull back = positive
            )

            # ── Left stick X → rudder ────────────────────────────────────────
            self.gamepad.left_joystick(
                x_value_float=snap["rud"],
                y_value_float=0.0,
            )

            # ── Right trigger → throttle ─────────────────────────────────────
            self.gamepad.right_trigger_float(value_float=snap["thr"])

            # ── flapsDown → left trigger ─────────────────────────────────────
            lt_val = 1.0 if snap["buttons"].get("flapsDown", False) else 0.0
            self.gamepad.left_trigger_float(value_float=lt_val)

            # ── All other buttons ─────────────────────────────────────────────
            btns = snap["buttons"]
            for name, xusb_name in self._BTN_MAP.items():
                if name == "flapsDown":
                    continue   # handled as trigger above
                xusb_const = getattr(vg.XUSB_BUTTON, xusb_name)
                if btns.get(name, False):
                    self.gamepad.press_button(button=xusb_const)
                else:
                    self.gamepad.release_button(button=xusb_const)

            # ── Commit all changes in one call ────────────────────────────────
            self.gamepad.update()

        except Exception as exc:
            print(f"[CTRL] apply() error: {exc}")

# ── WebSocket server (runs in asyncio thread) ────────────────────────────────
class WSServer:
    """
    Async WebSocket server.  Runs entirely inside a dedicated thread that
    owns an asyncio event loop.  Updates _AppState via update_from_json().
    """

    def __init__(self, state: _AppState) -> None:
        self._state   = state
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
        """Handle one client connection."""
        peer = ws.remote_address
        print(f"[WS] Client connected: {peer}")
        with self._state._lock:
            self._state.client_connected = True
            self._state.last_rx = time.monotonic()  # prevent watchdog false-positive before first packet

        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                    self._state.update_from_json(data)
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
            hdr, text="Virtual Joystick Bridge  v1.0",
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
        self._dot_ctrl   = _status_dot(row, "CONTROLLER")

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
        """Read shared state, update gamepad, refresh UI."""
        snap = self._state.snapshot()

        # ── 1. Push data to virtual controller (safe: main thread only) ──────
        self._ctrl.apply(snap)

        # ── 2. Connection watchdog: mark disconnected if >3 s silent ─────────
        if snap["client_connected"]:
            if time.monotonic() - snap["last_rx"] > 3.0:
                with self._state._lock:
                    self._state.client_connected = False
                snap["client_connected"] = False

        # ── 3. Status dots ────────────────────────────────────────────────────
        self._dot_server.config(fg=GREEN if snap["server_running"]   else RED)
        self._dot_device.config(fg=GREEN if snap["client_connected"] else RED)
        self._dot_ctrl.config(  fg=GREEN if snap["controller_ready"] else RED)

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
    ok = ctrl.start()
    with state._lock:
        state.controller_ready = ok

    # 3. WebSocket server (background thread)
    ws = WSServer(state)
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
