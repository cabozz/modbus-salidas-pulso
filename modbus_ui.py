import socket
import struct
import threading
import time
import tkinter as tk
from tkinter import font

HOST = "192.168.100.62"
PORT = 433
SLAVE_ID = 0x01
NUM_OUTPUTS = 8


# --- Modbus helpers ---

def modbus_crc(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return struct.pack('<H', crc)


def build_read_outputs() -> bytes:
    frame = struct.pack(">B B H H", SLAVE_ID, 0x03, 0x0001, NUM_OUTPUTS)
    return frame + modbus_crc(frame)


def build_write_cmd(output: int, value: int) -> bytes:
    frame = struct.pack(">B B H H", SLAVE_ID, 0x06, output, value)
    return frame + modbus_crc(frame)


def parse_read_response(resp: bytes) -> list[int] | None:
    if len(resp) < 5:
        return None
    count = resp[2]
    data = resp[3:3 + count]
    if len(data) < count:
        return None
    return [struct.unpack(">H", data[i:i + 2])[0] for i in range(0, len(data), 2)]


# --- Modbus client ---

class ModbusClient:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    def connect(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect((self.host, self.port))
            self._sock = s
            return True
        except Exception:
            self._sock = None
            return False

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def read_outputs(self) -> list[int] | None:
        with self._lock:
            if not self._sock:
                return None
            try:
                self._sock.sendall(build_read_outputs())
                time.sleep(0.1)
                resp = self._sock.recv(1024)
                return parse_read_response(resp)
            except Exception:
                return None

    def write_command(self, output: int, value: int) -> bool:
        with self._lock:
            if not self._sock:
                return False
            try:
                self._sock.sendall(build_write_cmd(output, value))
                time.sleep(0.1)
                self._sock.recv(1024)
                return True
            except Exception:
                return False


# --- GUI ---

BG      = "#0d0d0d"
HDR_BG  = "#1a1a1a"
CARD_BG = "#1f1f1f"
ACCENT  = "#00aaff"
LED_ON  = "#00ff44"
LED_OFF = "#222222"
LED_UNK = "#ffaa00"
FG      = "#ffffff"
FG_DIM  = "#666666"

CMD_VALUE = 0x0500


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Ocho Salidas Tipo Pulso")
        self.resizable(True, True)
        self.geometry("1280x720")
        self.configure(bg=BG)

        self._client = ModbusClient(HOST, PORT)
        self._poll_trigger = threading.Event()
        self._running = True

        self._leds: list[tk.Canvas] = []
        self._led_ovals: list[int] = []
        self._status_var = tk.StringVar(value="Conectando...")
        self._status_lbl: tk.Label

        self._build_ui()
        self._connect()

        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- UI ----

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=HDR_BG, pady=18)
        hdr.pack(fill="x")
        tk.Label(hdr, text="OCHO SALIDAS TIPO PULSO",
                 bg=HDR_BG, fg=ACCENT,
                 font=("Helvetica", 22, "bold")).pack()
        tk.Label(hdr, text=f"{HOST}:{PORT}   ·   Slave ID {SLAVE_ID}",
                 bg=HDR_BG, fg=FG_DIM,
                 font=("Helvetica", 12)).pack(pady=(4, 0))

        # Body — centered, fills remaining height
        body = tk.Frame(self, bg=BG)
        body.pack(expand=True, fill="both")

        inner = tk.Frame(body, bg=BG)
        inner.place(relx=0.5, rely=0.5, anchor="center")

        FONT      = "Helvetica"
        LED_SIZE  = 80
        LED_PAD   = 7
        COL_PAD   = 8

        for i in range(NUM_OUTPUTS):
            col = tk.Frame(inner, bg=CARD_BG, padx=18, pady=24,
                           highlightbackground="#333333", highlightthickness=1)
            col.grid(row=0, column=i, padx=COL_PAD)

            # Output label
            tk.Label(col, text=f"SALIDA {i+1}",
                     bg=CARD_BG, fg=FG_DIM,
                     font=(FONT, 11, "bold")).pack(pady=(0, 12))

            # LED
            cv = tk.Canvas(col, width=LED_SIZE, height=LED_SIZE,
                           bg=CARD_BG, highlightthickness=0)
            cv.pack()
            oval = cv.create_oval(
                LED_PAD, LED_PAD,
                LED_SIZE - LED_PAD, LED_SIZE - LED_PAD,
                fill=LED_UNK, outline="#444444", width=3)
            self._leds.append(cv)
            self._led_ovals.append(oval)

            # Button
            n = i + 1
            tk.Button(
                col, text=str(n),
                font=(FONT, 28, "bold"),
                bg="#2a2a2a", fg="#000000",
                activebackground="#3a3a3a", activeforeground="#000000",
                relief="flat", cursor="hand2",
                bd=0, padx=22, pady=14,
                command=lambda out=n: self._send_cmd(out),
            ).pack(pady=(16, 0), fill="x")

        # Status bar
        status_bar = tk.Frame(self, bg=HDR_BG, pady=30)
        status_bar.pack(fill="x", side="bottom")
        self._status_lbl = tk.Label(
            status_bar, textvariable=self._status_var,
            bg=HDR_BG, fg=FG_DIM,
            font=("Helvetica", 20, "bold"))
        self._status_lbl.pack()

    # ---- Connection ----

    def _connect(self):
        ok = self._client.connect()
        self._set_status(
            "Conectado" if ok else f"Error: no se pudo conectar a {HOST}:{PORT}", ok)

    # ---- Polling thread ----

    def _poll_loop(self):
        while self._running:
            regs = self._client.read_outputs()
            if regs is not None:
                self.after(0, self._update_leds, regs)
                ts = time.strftime("%H:%M:%S")
                self.after(0, self._set_status, f"Conectado  ·  {ts}", True)
            else:
                self.after(0, self._set_status, "Sin respuesta del dispositivo", False)
            self._poll_trigger.wait(timeout=0.2)
            self._poll_trigger.clear()

    # ---- UI updates (main thread) ----

    def _update_leds(self, regs: list[int]):
        for i, val in enumerate(regs[:NUM_OUTPUTS]):
            color = LED_ON if val == 0x0001 else LED_OFF
            self._leds[i].itemconfig(self._led_ovals[i], fill=color)

    def _set_status(self, msg: str, ok: bool = True):
        self._status_var.set(msg)
        self._status_lbl.config(fg="#00ff44" if ok else "#ff4444")

    # ---- Command ----

    def _send_cmd(self, output: int):
        ok = self._client.write_command(output, CMD_VALUE)
        ts = time.strftime("%H:%M:%S")
        if ok:
            frame = build_write_cmd(output, CMD_VALUE)
            self._set_status(
                f"Salida {output}  [{frame.hex(' ').upper()}]  {ts}", True)
        else:
            self._set_status(f"Error → Salida {output}", False)
        self._poll_trigger.set()

    # ---- Cleanup ----

    def _on_close(self):
        self._running = False
        self._poll_trigger.set()
        self._client.close()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
