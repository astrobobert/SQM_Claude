"""
Sky Quality Meter — Pico W2 + TSL2591
Connects to WiFi and serves Unihedron SQM-LE compatible readings over TCP,
plus a browser page and JSON API over HTTP.

SQM-LE: client connects to TCP_PORT, optionally sends "rx\n", receives:
  r, XX.XXm,0000000000Hz,NNNNNNNNNNN c,XXXXXXX.XXXlx,XXXXX.XC\r\n
HTTP:   http://<meter-ip>:HTTP_PORT/  serves /www/index.html; /api/rx and
        /api/offset expose the same readings and calibration as JSON.
"""

import json
import math
import os
import select
import socket
import time
import network
from machine import I2C, Pin

import sys
sys.path.insert(0, "/lib")

from tsl2591 import (
    TSL2591,
    GAIN_LOW, GAIN_MED, GAIN_HIGH, GAIN_MAX,
    INT_100MS, INT_200MS, INT_300MS, INT_400MS, INT_500MS, INT_600MS,
)
from tmp117 import TMP117
import secrets
from settings import (
    I2C_ID, I2C_SDA_PIN, I2C_SCL_PIN, I2C_FREQ,
    TSL2591_GAIN, TSL2591_INTEGRATION,
    CALIBRATION_OFFSET,
    TCP_PORT, TCP_TIMEOUT_S, CX_TIMEOUT_S,
    HTTP_PORT, HTTP_TIMEOUT_S, HOSTNAME,
    TMP117_ADDR,
    LED_PIN,
)

# ── Map config strings to driver constants ────────────────────────────────────
_GAIN_MAP = {
    "LOW": GAIN_LOW, "MED": GAIN_MED, "HIGH": GAIN_HIGH, "MAX": GAIN_MAX,
}
_INT_MAP = {
    "100MS": INT_100MS, "200MS": INT_200MS, "300MS": INT_300MS,
    "400MS": INT_400MS, "500MS": INT_500MS, "600MS": INT_600MS,
}


# ── LED helper ────────────────────────────────────────────────────────────────

def make_led():
    try:
        return Pin(LED_PIN, Pin.OUT)
    except Exception:
        return None


def blink(led, times: int = 1, on_ms: int = 100, off_ms: int = 100):
    if led is None:
        return
    for _ in range(times):
        led.on()
        time.sleep_ms(on_ms)
        led.off()
        time.sleep_ms(off_ms)


# ── WiFi ──────────────────────────────────────────────────────────────────────

def wifi_networks() -> list:
    """Credentials as [(ssid, password), ...]; accepts the old single-SSID form."""
    nets = getattr(secrets, "WIFI_NETWORKS", None)
    if nets:
        return list(nets)
    return [(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)]


_STATUS_NAMES = {
    0: "IDLE", 1: "CONNECTING", 2: "NO_IP", 3: "GOT_IP",
    -1: "CONNECT_FAIL", -2: "NO_AP_FOUND", -3: "WRONG_PASSWORD",
}


def _status_name(status) -> str:
    return _STATUS_NAMES.get(status, "UNKNOWN")


def _visible_ssids(wlan) -> set:
    """SSIDs the radio can see. An empty set means "scan unavailable", not "none"."""
    try:
        return {n[0].decode() for n in wlan.scan() if n[0]}
    except Exception as e:
        print(f"[WIFI] scan failed: {e}")
        return set()


def _try_connect(wlan, ssid: str, password: str, led, timeout_s: int) -> bool:
    print(f"Connecting to {ssid} ", end="")
    wlan.connect(ssid, password)

    deadline = time.ticks_add(time.ticks_ms(), timeout_s * 1000)
    # The radio can briefly report a failure while the join is still in flight,
    # so a negative status is only believed once it has had time to settle.
    settled  = time.ticks_add(time.ticks_ms(), 3000)
    while not wlan.isconnected():
        status  = wlan.status()
        expired = time.ticks_diff(deadline, time.ticks_ms()) <= 0
        # A settled negative status is terminal (no such AP, bad password,
        # association refused), so move on instead of waiting out the timeout.
        rejected = status < 0 and time.ticks_diff(settled, time.ticks_ms()) <= 0
        if expired or rejected:
            why = f"failed after {timeout_s}s" if expired else "rejected"
            print(f"\n[WIFI] {ssid} {why} (status={status} {_status_name(status)})")
            try:
                wlan.disconnect()
            except Exception:
                pass
            return False
        blink(led, on_ms=50, off_ms=50)
        print(".", end="")
        time.sleep_ms(500)
    return True


def wifi_connect(networks, led=None, timeout_s: int = 30, passes: int = 2) -> network.WLAN:
    """Try each (ssid, password) in turn until one associates."""
    try:
        network.hostname(HOSTNAME)   # DHCP name, so http://sqm/ may work too
    except Exception as e:
        print(f"[WIFI] hostname not set: {e}")
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    time.sleep_ms(500)   # radio needs a moment before the first scan is complete

    if wlan.isconnected():
        print(f"Already connected — IP: {wlan.ifconfig()[0]}")
        return wlan

    for attempt in range(passes):
        visible = _visible_ssids(wlan)
        # Networks in range first, in the order secrets.py lists them; hidden
        # SSIDs never appear in a scan, so try the rest afterwards anyway.
        seen   = [n for n in networks if n[0] in visible]
        unseen = [n for n in networks if n[0] not in visible]
        for ssid, password in seen + unseen:
            if _try_connect(wlan, ssid, password, led, timeout_s):
                print(f"\nConnected to {ssid} — IP: {wlan.ifconfig()[0]}")
                return wlan
        if attempt + 1 < passes:
            print("[WIFI] no network joined — retrying")

    raise RuntimeError(f"WiFi failed: none of {[n[0] for n in networks]} joined")


# ── SQM maths ─────────────────────────────────────────────────────────────────

_MPSAS_MIN  = 16.0   # heavily light-polluted urban sky
_MPSAS_MAX  = 23.0   # natural airglow sets the physical dark-sky limit
_cal_offset = CALIBRATION_OFFSET   # mutable; updated by cx command


def lux_to_mpsas(lux: float) -> float:
    if lux <= 0:
        return 99.99  # sensor below noise floor
    mpsas = -2.5 * math.log10(lux) + _cal_offset
    return max(_MPSAS_MIN, min(_MPSAS_MAX, mpsas))


# Thresholds from Sky & Telescope / Globe at Night SQM correlation table.
# Each tuple is (minimum MPSAS for this class, Bortle class).
_BORTLE_THRESHOLDS = (
    (21.99, 1),
    (21.89, 2),
    (21.69, 3),
    (20.49, 4),
    (19.50, 5),
    (18.94, 6),
    (18.38, 7),
    (17.83, 8),
)

def mpsas_to_bortle(mpsas: float) -> int:
    for threshold, cls in _BORTLE_THRESHOLDS:
        if mpsas >= threshold:
            return cls
    return 9


def update_calibration(new_val: float):
    global _cal_offset
    _cal_offset = new_val
    path = "/lib/settings.py"
    with open(path) as f:
        lines = f.readlines()
    with open(path, "w") as f:
        for line in lines:
            if line.startswith("CALIBRATION_OFFSET"):
                f.write(f"CALIBRATION_OFFSET = {new_val}\n")
            else:
                f.write(line)


# ── Unihedron SQM-LE response formatter ──────────────────────────────────────

def sqm_response(mpsas: float, chan0: int, lux: float, temp_c: float) -> bytes:
    """
    Format compatible with Unihedron SQM-LE ASCII protocol (Table 8.2):
      r, XX.XXm,0000000000Hz,0000000000c,0000000.000s, XXX.XC\r\n
    """
    no_signal = lux <= 0 or math.isinf(lux)
    mpsas_display = "----" if no_signal else f"{mpsas:5.2f}"
    # APT validates Hz > 0; derive a non-zero value from lux (scale ~50k matches
    # real SQM-LE output range). Zero only when sensor is below noise floor.
    hz = 0 if no_signal else int(lux * 50_000)
    # Period in seconds: SQM-LE defines this as counts / 460800 Hz reference clock.
    # TSL2591 chan0 is an ADC count, not a frequency period, but serves as a proxy.
    period_s = 0.0 if no_signal else chan0 / 460_800.0
    line = (
        f"r, {mpsas_display}m,"
        f"{hz:010d}Hz,"
        f"{chan0:010d}c,"
        f"{period_s:011.3f}s,"
        f"{temp_c:6.1f}C\r\n"
    )
    return line.encode()


# Reported when the TMP117 is absent or unreadable, so a temperature fault is
# obvious to the client instead of passing as a plausible reading.
TEMP_UNAVAILABLE = -999.0


def read_temperature(therm) -> float:
    """Die temperature in °C, or TEMP_UNAVAILABLE if the sensor is unusable."""
    if therm is None:
        return TEMP_UNAVAILABLE
    try:
        return therm.temperature
    except Exception as e:
        print(f"[TMP117] read failed: {e}")
        return TEMP_UNAVAILABLE



# ── Readings ──────────────────────────────────────────────────────────────────

def take_reading(sensor, therm, led=None) -> dict:
    """One measurement from both sensors; shared by the SQM and HTTP servers."""
    if led:
        led.off()                                   # dark while integrating
    c0, c1, _ = sensor.raw_auto()
    lx    = sensor.lux(c0, c1)
    mpsas = lux_to_mpsas(lx)
    temp  = read_temperature(therm)
    if led:
        led.on()
    return {"chan0": c0, "chan1": c1, "lux": lx, "mpsas": mpsas, "temp": temp}


def log_reading(client_id: str, r: dict):
    print(
        f"[READING]    {client_id}  "
        f"MPSAS={r['mpsas']:.2f}  lux={r['lux']:.6f}  "
        f"chan0={r['chan0']}  chan1={r['chan1']}  T={r['temp']:.1f}C"
    )


# ── SQM-LE client (port TCP_PORT) ─────────────────────────────────────────────

def handle_sqm_client(conn, client_id: str, sensor, therm, led=None):
    """One Unihedron-style session: rx, cx [value], cx exit."""
    persistent = False

    # Accept optional "rx" command; proceed on timeout too
    try:
        cmd = conn.recv(64).decode("ascii").strip().lower()
        if cmd:
            print(f"[CMD]        {client_id}  <- '{cmd}'")
    except OSError:
        cmd = "rx"

    while True:
        if cmd == "rx":
            r = take_reading(sensor, therm, led)
            if persistent:
                bortle = mpsas_to_bortle(r["mpsas"])
                response = (
                    f"MPSAS={r['mpsas']:.2f}  Bortle={bortle}  lux={r['lux']:.6f}  "
                    f"chan0={r['chan0']}  chan1={r['chan1']}  T={r['temp']:.1f}C\r\n"
                ).encode()
            else:
                response = sqm_response(r["mpsas"], r["chan0"], r["lux"], r["temp"])
            conn.sendall(response)
            log_reading(client_id, r)
            print(f"[SENT]       {client_id}  -> {response.decode().rstrip()}")

        elif cmd.startswith("cx"):
            parts = cmd.split()
            if len(parts) >= 2 and parts[1] == "exit":
                persistent = False
                conn.sendall(b"c, BYE\r\n")
                print(f"[CAL EXIT]   {client_id}")
                break
            if not persistent:
                persistent = True
                conn.settimeout(CX_TIMEOUT_S)
            if len(parts) >= 2:
                try:
                    update_calibration(float(parts[1]))
                except ValueError:
                    conn.sendall(b"c, ERROR\r\n")
                    print(f"[CAL ERROR]  {client_id}  bad value '{parts[1]}'")
                else:
                    conn.sendall(f"c, {_cal_offset:+.2f}\r\n".encode())
                    print(f"[CAL SET]    {client_id}  -> {_cal_offset:+.2f}")
            else:
                conn.sendall(f"c, {_cal_offset:+.2f}\r\n".encode())
                print(f"[CAL READ]   {client_id}  -> {_cal_offset:+.2f}")

        else:
            print(f"[IGNORED]    {client_id}  unknown command '{cmd}'")

        if not persistent:
            break

        try:
            raw = conn.recv(64)
            if not raw:  # b"" means TCP connection closed
                break
            cmd = raw.decode("ascii").strip().lower()
            if cmd:
                print(f"[CMD]        {client_id}  <- '{cmd}'")
        except OSError:
            break


# ── HTTP client (port HTTP_PORT): browser page + JSON API ─────────────────────
#
#   GET  /             the page, streamed from /www/index.html on flash
#   GET  /api/rx       one reading as JSON (plus the current offset)
#   GET  /api/offset   {"offset": 12.6}
#   POST /api/offset   body {"value": 12.8}  -> sets and persists the offset
#
# Requests are served one at a time, HTTP/1.0 with Connection: close, so the
# browser never waits on a keep-alive socket that this loop would not service.

_WWW_INDEX   = "/www/index.html"
_HTTP_REASON = {
    200: "OK", 204: "No Content", 400: "Bad Request",
    404: "Not Found", 405: "Method Not Allowed",
}


def _http_head(status: int, ctype: str, length: int) -> bytes:
    return (
        f"HTTP/1.0 {status} {_HTTP_REASON.get(status, '')}\r\n"
        f"Content-Type: {ctype}\r\n"
        f"Content-Length: {length}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    ).encode()


def _http_send(conn, status: int, body: bytes = b"", ctype: str = "text/plain"):
    conn.sendall(_http_head(status, ctype, len(body)))
    if body:
        conn.sendall(body)


def _http_send_json(conn, obj, status: int = 200):
    _http_send(conn, status, json.dumps(obj).encode(), "application/json")


def _http_send_file(conn, path: str, ctype: str):
    """Stream a flash file in small chunks so a 10 KB page never needs 10 KB of RAM."""
    conn.sendall(_http_head(200, ctype, os.stat(path)[6]))
    with open(path, "rb") as f:
        while True:
            chunk = f.read(512)
            if not chunk:
                break
            conn.sendall(chunk)


def _http_read_request(conn):
    """Return (method, path, body) or None if the client sent nothing usable."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(512)
        if not chunk or len(buf) + len(chunk) > 2048:
            return None
        buf += chunk
    head, body = buf.split(b"\r\n\r\n", 1)
    try:
        lines = head.decode("ascii").split("\r\n")
    except UnicodeError:
        return None
    request = lines[0].split()
    if len(request) < 2:
        return None
    method, target = request[0].upper(), request[1]

    length = 0
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            try:
                length = min(int(value.strip()), 1024)
            except ValueError:
                length = 0
    while len(body) < length:
        chunk = conn.recv(min(512, length - len(body)))
        if not chunk:
            break
        body += chunk

    path = target.partition("?")[0]
    return method, path, body


def reading_json(r: dict) -> dict:
    lux = r["lux"]
    no_signal = lux <= 0 or math.isinf(lux)
    return {
        "mpsas":  None if no_signal else round(r["mpsas"], 2),
        "bortle": None if no_signal else mpsas_to_bortle(r["mpsas"]),
        "lux":    None if math.isinf(lux) else lux,
        "hz":     0 if no_signal else int(lux * 50_000),
        "chan0":  r["chan0"],
        "chan1":  r["chan1"],
        "temp":   r["temp"],
        "offset": _cal_offset,
        "line":   sqm_response(r["mpsas"], r["chan0"], lux, r["temp"]).decode().strip(),
    }


def handle_http_client(conn, client_id: str, sensor, therm, led=None):
    req = _http_read_request(conn)
    if req is None:
        return                                      # idle pre-connect or garbage
    method, path, body = req
    print(f"[HTTP]       {client_id}  {method} {path}")

    if path == "/":
        if method != "GET":
            _http_send(conn, 405)
            return
        try:
            _http_send_file(conn, _WWW_INDEX, "text/html; charset=utf-8")
        except OSError:
            _http_send(conn, 404, b"page missing: upload www/index.html to /www/index.html on the meter\n")

    elif path == "/api/rx":
        if method != "GET":
            _http_send(conn, 405)
            return
        r = take_reading(sensor, therm, led)
        _http_send_json(conn, reading_json(r))
        log_reading(client_id, r)

    elif path == "/api/offset":
        if method == "POST":
            try:
                value = float(json.loads(body.decode())["value"])
            except (ValueError, KeyError, TypeError, UnicodeError):
                _http_send_json(conn, {"error": "offset must be a number"}, 400)
                print(f"[CAL ERROR]  {client_id}  bad body {repr(body)}")
                return
            update_calibration(value)
            print(f"[CAL SET]    {client_id}  -> {_cal_offset:+.2f}")
        elif method != "GET":
            _http_send(conn, 405)
            return
        else:
            print(f"[CAL READ]   {client_id}  -> {_cal_offset:+.2f}")
        _http_send_json(conn, {"offset": _cal_offset})

    elif path == "/favicon.ico":
        _http_send(conn, 204)

    else:
        _http_send(conn, 404, b"not found\n")


# ── Server loop: one SQM-LE socket and one HTTP socket, one client at a time ──

def _listen(port: int, backlog: int):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("", port))
    srv.listen(backlog)
    return srv


def run_server(sensor, therm, led=None):
    sqm = _listen(TCP_PORT, 1)
    web = _listen(HTTP_PORT, 2)
    print(f"AstroBobert_SQM listening on port {TCP_PORT} (SQM-LE) and {HTTP_PORT} (HTTP)")

    poller = select.poll()
    poller.register(sqm, select.POLLIN)
    poller.register(web, select.POLLIN)

    while True:
        try:
            for sock, _event in poller.poll(1000):
                conn, addr = sock.accept()
                is_http   = sock is web
                client_id = f"{addr[0]}:{addr[1]}"
                blink(led, times=2, on_ms=30, off_ms=30)
                if led:
                    led.on()
                kind = "http" if is_http else "sqm"
                print(f"[CONNECT]    {client_id}  {kind}")

                try:
                    if is_http:
                        conn.settimeout(HTTP_TIMEOUT_S)
                        handle_http_client(conn, client_id, sensor, therm, led)
                    else:
                        conn.settimeout(TCP_TIMEOUT_S)
                        handle_sqm_client(conn, client_id, sensor, therm, led)
                except Exception as e:
                    print(f"[ERROR]      {client_id}  {e}")
                finally:
                    conn.close()
                    print(f"[DISCONNECT] {client_id}")

        except Exception as e:
            print(f"[SERVER ERR] {e}")
            time.sleep_ms(500)


# ── Boot sequence ─────────────────────────────────────────────────────────────


def halt(led, reason: str):
    """Stop serving but keep WiFi up, so the board stays reachable to diagnose."""
    print(f"[FATAL] {reason}")
    print("[FATAL] halted; WiFi stays up. Fix the hardware and reset.")
    while True:                                       # SOS: ... --- ...
        blink(led, times=3, on_ms=120, off_ms=120)
        blink(led, times=3, on_ms=400, off_ms=120)
        blink(led, times=3, on_ms=120, off_ms=120)
        time.sleep_ms(1200)


def main():
    led = make_led()
    blink(led, times=3, on_ms=200, off_ms=100)

    # WiFi first. A sensor fault must not cost us the network, or the board
    # goes dark in the field with no way to see what went wrong.
    wlan = wifi_connect(wifi_networks(), led=led)
    url = f"http://{wlan.ifconfig()[0]}/"
    if HTTP_PORT != 80:
        url = url[:-1] + f":{HTTP_PORT}/"
    print(f"Web page: {url}")
    blink(led, times=5, on_ms=80, off_ms=80)
    if led:
        led.on()

    i2c = I2C(I2C_ID, sda=Pin(I2C_SDA_PIN), scl=Pin(I2C_SCL_PIN), freq=I2C_FREQ)
    print(f"I2C scan: {[hex(a) for a in i2c.scan()]}")

    # Light sensor is required; without it there is no genuine reading to serve.
    try:
        sensor = TSL2591(i2c)
        sensor.gain             = _GAIN_MAP[TSL2591_GAIN]
        sensor.integration_time = _INT_MAP[TSL2591_INTEGRATION]
        print("TSL2591 OK")
    except Exception as e:
        halt(led, f"TSL2591 unavailable: {e}")

    # Temperature sensor is optional; readings continue without it.
    try:
        therm = TMP117(i2c, TMP117_ADDR)
        print(f"TMP117 OK  ({therm.temperature:.2f} °C)")
    except Exception as e:
        therm = None
        print(f"[TMP117] unavailable: {e}; reporting {TEMP_UNAVAILABLE}")

    run_server(sensor, therm, led)


main()