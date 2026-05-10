"""
Sky Quality Meter — Pico W2 + TSL2591
Connects to WiFi and serves Unihedron SQM-LE compatible readings over TCP.

Protocol: client connects to TCP_PORT, optionally sends "rx\n", receives:
  r, XX.XXm,0000000000Hz,NNNNNNNNNNN c,XXXXXXX.XXXlx,XXXXX.XC\r\n
"""

import math
import socket
import time
import network
from machine import I2C, Pin, ADC

import sys
sys.path.insert(0, "/lib")

from tsl2591 import (
    TSL2591,
    GAIN_LOW, GAIN_MED, GAIN_HIGH, GAIN_MAX,
    INT_100MS, INT_200MS, INT_300MS, INT_400MS, INT_500MS, INT_600MS,
)
import secrets
from settings import (
    I2C_ID, I2C_SDA_PIN, I2C_SCL_PIN, I2C_FREQ,
    TSL2591_GAIN, TSL2591_INTEGRATION,
    CALIBRATION_OFFSET,
    TCP_PORT, TCP_TIMEOUT_S, CX_TIMEOUT_S,
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

def wifi_connect(ssid: str, password: str, led=None, timeout_s: int = 30) -> network.WLAN:
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if wlan.isconnected():
        return wlan

    print(f"Connecting to {ssid} ", end="")
    wlan.connect(ssid, password)

    deadline = time.ticks_add(time.ticks_ms(), timeout_s * 1000)
    while not wlan.isconnected():
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            raise RuntimeError(f"WiFi timeout after {timeout_s}s (status={wlan.status()})")
        blink(led, on_ms=50, off_ms=50)
        print(".", end="")
        time.sleep_ms(500)

    ip = wlan.ifconfig()[0]
    print(f"\nConnected — IP: {ip}")
    return wlan


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


# ── Internal temperature sensor ───────────────────────────────────────────────

def cpu_temp_c() -> float:
    """Read Pico W2 RP2350 internal temperature sensor (ADC channel 4)."""
    adc = ADC(4)
    v = adc.read_u16() * 3.3 / 65535
    return 27.0 - (v - 0.706) / 0.001721


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


# ── TCP server (blocking, one client at a time) ───────────────────────────────

def run_server(sensor: TSL2591, led=None):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("", TCP_PORT))
    srv.listen(1)
    print(f"AstroBobert_SQM listening on port {TCP_PORT}")

    while True:
        try:
            conn, addr = srv.accept()
            client_id = f"{addr[0]}:{addr[1]}"
            blink(led, times=2, on_ms=30, off_ms=30)
            if led:
                led.on()
            print(f"[CONNECT]    {client_id}")
            conn.settimeout(TCP_TIMEOUT_S)

            try:
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
                        if led:
                            led.off()
                        c0, c1, _ = sensor.raw_auto()
                        lx        = sensor.lux(c0, c1)
                        mpsas     = lux_to_mpsas(lx)
                        temp      = cpu_temp_c()
                        if led:
                            led.on()

                        if persistent:
                            bortle = mpsas_to_bortle(mpsas)
                            cal_line = (
                                f"MPSAS={mpsas:.2f}  Bortle={bortle}  lux={lx:.6f}  "
                                f"chan0={c0}  chan1={c1}  T={temp:.1f}C\r\n"
                            )
                            conn.sendall(cal_line.encode())
                        else:
                            response = sqm_response(mpsas, c0, lx, temp)
                            conn.sendall(response)

                        print(
                            f"[READING]    {client_id}  "
                            f"MPSAS={mpsas:.2f}  lux={lx:.6f}  "
                            f"chan0={c0}  chan1={c1}  T={temp:.1f}C"
                        )
                        sent = cal_line.rstrip() if persistent else response.decode().rstrip()
                        print(f"[SENT]       {client_id}  -> {sent}")

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

            except Exception as e:
                print(f"[ERROR]      {client_id}  {e}")
            finally:
                conn.close()
                print(f"[DISCONNECT] {client_id}")

        except Exception as e:
            print(f"[SERVER ERR] {e}")
            time.sleep_ms(500)


# ── Boot sequence ─────────────────────────────────────────────────────────────

def main():
    led = make_led()
    blink(led, times=3, on_ms=200, off_ms=100)

    # Initialise I2C and sensor
    i2c = I2C(I2C_ID, sda=Pin(I2C_SDA_PIN), scl=Pin(I2C_SCL_PIN), freq=I2C_FREQ)
    print(f"I2C scan: {[hex(a) for a in i2c.scan()]}")

    sensor = TSL2591(i2c)
    sensor.gain             = _GAIN_MAP[TSL2591_GAIN]
    sensor.integration_time = _INT_MAP[TSL2591_INTEGRATION]
    print("TSL2591 OK")

    # Connect to WiFi (fast blink during connection)
    wifi_connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD, led=led)
    blink(led, times=5, on_ms=80, off_ms=80)
    if led:
        led.on()

    # Start serving
    run_server(sensor, led)


main()
