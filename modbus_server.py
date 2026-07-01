import socket
import struct
import threading
import time
import tkinter as tk

# ---------------------------------------------------------------------------
# Modo SERVIDOR
#
# A diferencia de modbus_ui.py (cliente que marca al dispositivo), aquí el
# programa ESCUCHA. El USR-TCP232-410s se configura en modo "TCP Client" y
# marca hacia nosotros (o hacia el VPS). Una vez abierta la conexión, seguimos
# siendo el MASTER Modbus: escribimos comandos (FC06) y leemos estado (FC03)
# por el socket que el USR abrió. El USR solo tuneliza los bytes al bus RS485
# donde vive la placa N4D8B08.
# ---------------------------------------------------------------------------

LISTEN_HOST = "0.0.0.0"    # escucha en todas las interfaces
LISTEN_PORT = 5020         # el USR marca aquí. 5020 = puerto alto, no requiere root.
                           # 502 es el estándar Modbus pero necesita privilegios y es
                           # el primero que escanean los bots — evitar en internet.
ALLOWED_PEER = None        # ej. "203.0.113.5" para aceptar solo la IP del sitio USR.
                           # None = cualquiera. NO es seguridad real (ver notas), solo higiene.

SLAVE_ID = 0x01
NUM_OUTPUTS = 8
CMD_VALUE = 0x0500         # Momentary (pulso) — FC06, igual que modbus_ui.py


# --- Modbus helpers (idénticos a modbus_ui.py) ---

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


# --- Servidor Modbus ---

class ModbusServer:
    """Escucha; el USR marca hacia nosotros. Nosotros seguimos siendo master."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._srv: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._peer = None
        self._lock = threading.Lock()
        self._running = True
        # callback(connected: bool, peer) — invocado al conectar/desconectar USR
        self.on_state = None

    def start(self) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(1)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, addr = self._srv.accept()
            except OSError:
                break

            peer_ip = addr[0]
            if ALLOWED_PEER and peer_ip != ALLOWED_PEER:
                # Origen no permitido → cerrar. NOTA: filtrar por IP NO detiene
                # un MITM (forja la IP), solo bloquea conexiones directas ajenas.
                try:
                    conn.close()
                except Exception:
                    pass
                continue

            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            conn.settimeout(3)

            with self._lock:
                # Solo un slave esperado. Si había una conexión vieja, la soltamos.
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                self._conn = conn
                self._peer = addr

            if self.on_state:
                self.on_state(True, addr)

    def _drop(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = None
            self._peer = None
        if self.on_state:
            self.on_state(False, None)

    def _txn(self, request: bytes) -> bytes | None:
        """Una transacción RTU: manda request, recibe respuesta. Serializada por lock."""
        dead = False
        with self._lock:
            if self._conn is None:
                return None
            try:
                self._conn.sendall(request)
                time.sleep(0.1)
                resp = self._conn.recv(1024)
                if not resp:
                    dead = True
                else:
                    return resp
            except Exception:
                dead = True
        if dead:
            self._drop()
        return None

    def read_outputs(self) -> list[int] | None:
        resp = self._txn(build_read_outputs())
        return parse_read_response(resp) if resp is not None else None

    def write_command(self, output: int, value: int) -> bool:
        return self._txn(build_write_cmd(output, value)) is not None

    @property
    def peer(self):
        return self._peer

    def close(self) -> None:
        self._running = False
        self._drop()
        if self._srv is not None:
            try:
                self._srv.close()
            except Exception:
                pass


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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Servidor Modbus — Ocho Salidas Tipo Pulso")
        self.resizable(True, True)
        self.geometry("1280x720")
        self.configure(bg=BG)

        self._server = ModbusServer(LISTEN_HOST, LISTEN_PORT)
        self._server.on_state = self._on_conn_state
        self._running = True

        self._leds: list[tk.Canvas] = []
        self._led_ovals: list[int] = []
        self._status_var = tk.StringVar(value="Iniciando servidor...")
        self._status_lbl: tk.Label

        self._build_ui()
        self._start_server()

        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- UI ----

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=HDR_BG, pady=18)
        hdr.pack(fill="x")
        tk.Label(hdr, text="SERVIDOR — OCHO SALIDAS TIPO PULSO",
                 bg=HDR_BG, fg=ACCENT,
                 font=("Helvetica", 22, "bold")).pack()
        tk.Label(hdr, text=f"Escuchando en {LISTEN_HOST}:{LISTEN_PORT}   ·   Slave ID {SLAVE_ID}",
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

    # ---- Servidor ----

    def _start_server(self):
        try:
            self._server.start()
            self._set_status(f"Esperando conexión del USR en {LISTEN_HOST}:{LISTEN_PORT}...", None)
        except Exception as e:
            self._set_status(f"Error al abrir servidor: {e}", False)

    def _on_conn_state(self, connected: bool, peer):
        """Callback desde el thread de accept — marshal a hilo Tk."""
        if connected:
            ts = time.strftime("%H:%M:%S")
            self.after(0, self._set_status, f"USR conectado  ·  {peer[0]}:{peer[1]}  ·  {ts}", True)
        else:
            self.after(0, self._reset_leds)
            self.after(0, self._set_status,
                       f"Esperando conexión del USR en {LISTEN_HOST}:{LISTEN_PORT}...", None)

    # ---- Polling thread ----

    def _poll_loop(self):
        while self._running:
            if self._server.peer is not None:
                regs = self._server.read_outputs()
                if regs is not None:
                    self.after(0, self._update_leds, regs)
                    ts = time.strftime("%H:%M:%S")
                    peer = self._server.peer
                    if peer:
                        self.after(0, self._set_status,
                                   f"USR conectado  ·  {peer[0]}:{peer[1]}  ·  {ts}", True)
            time.sleep(0.5)

    # ---- UI updates (main thread) ----

    def _update_leds(self, regs: list[int]):
        for i, val in enumerate(regs[:NUM_OUTPUTS]):
            color = LED_ON if val == 0x0001 else LED_OFF
            self._leds[i].itemconfig(self._led_ovals[i], fill=color)

    def _reset_leds(self):
        for i in range(NUM_OUTPUTS):
            self._leds[i].itemconfig(self._led_ovals[i], fill=LED_UNK)

    def _set_status(self, msg: str, ok):
        self._status_var.set(msg)
        if ok is True:
            self._status_lbl.config(fg="#00ff44")
        elif ok is False:
            self._status_lbl.config(fg="#ff4444")
        else:
            self._status_lbl.config(fg=LED_UNK)  # esperando/neutro

    # ---- Command ----

    def _send_cmd(self, output: int):
        if self._server.peer is None:
            self._set_status(f"Sin conexión USR → Salida {output} no enviada", False)
            return
        ok = self._server.write_command(output, CMD_VALUE)
        ts = time.strftime("%H:%M:%S")
        if ok:
            frame = build_write_cmd(output, CMD_VALUE)
            self._set_status(
                f"Salida {output}  [{frame.hex(' ').upper()}]  {ts}", True)
        else:
            self._set_status(f"Error → Salida {output}", False)

    # ---- Cleanup ----

    def _on_close(self):
        self._running = False
        self._server.close()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
