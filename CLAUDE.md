# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

MicroPython firmware for a WiFi-enabled Sky Quality Meter running on a **Raspberry Pi Pico W** (RP2040) or **Pico W2** (RP2350 + CYW43439). It serves sky-brightness readings over TCP using the Unihedron SQM-LE ASCII protocol, compatible with apps like APT, SQM Reader, and Sky Quality Monitor.

Hardware:
- TSL2591 light sensor — I²C on SDA=GP16, SCL=GP17, address 0x29
- TMP117 temperature sensor — same I²C bus, address 0x48 (ADD0 → GND)

## Deploying to Device

`mpremote` is the preferred toolchain. Install it with:
```
pip install mpremote
```

Upload everything to the device:
```
mpremote cp lib/tsl2591.py     :/lib/tsl2591.py
mpremote cp lib/tmp117.py      :/lib/tmp117.py
mpremote cp lib/secrets.py     :/lib/secrets.py
mpremote cp lib/settings.py    :/lib/settings.py
mpremote cp src/sqm_claude.py  :/src/sqm_claude.py
mpremote mkdir :/www
mpremote cp www/index.html     :/www/index.html
mpremote cp main.py            :/main.py
```

Run without rebooting (for quick testing):
```
mpremote run src/sqm_claude.py
```

Monitor serial output:
```
mpremote
```

Any mpremote command interrupts the running firmware and leaves the board at the
REPL, so nothing listens on the TCP port afterwards. Reset when done:
```
mpremote reset
```

## Desktop Console

`tools/sqm_console.py` is a standard-library Python script that serves a web page
on `http://127.0.0.1:8080/` and relays `rx` / `cx` to the meter over TCP (one short
session per button press, commands sent one at a time because the firmware reads
each command with a single `recv`). Run with `python tools/sqm_console.py`
(`--sqm <ip>` to change the meter address, `--listen <port>` for the local port,
`--lan` to serve on all interfaces so phones and laptops on the WiFi can open it).

List files on device:
```
mpremote ls -la
```

## Credentials (not in git)

`lib/secrets.py` is git-ignored. Copy the example and fill in your details:
```python
WIFI_NETWORKS = [
    ("your_network_name",  "your_wifi_password"),
    ("phone_hotspot_name", "your_hotspot_password"),
]
```
Any number of networks may be listed. `wifi_connect()` scans first and tries the
in-range networks in list order, then the ones it could not see (hidden SSIDs),
repeating the whole sweep `passes` times before raising. The old single
`WIFI_SSID`/`WIFI_PASSWORD` form is still accepted as a fallback.

## Architecture

- `main.py` — trivial entry point: `import src.sqm_claude`
- `src/sqm_claude.py` — all application logic: WiFi connect, SQM-LE + HTTP server loop, SQM math, LED blink helpers
- `www/index.html` — browser page served by the device at `http://<ip>/` or `http://sqm.local/` (mDNS via `HOSTNAME`); deploy to `/www/index.html`
- `lib/tsl2591.py` — TSL2591 driver; `raw_auto()` steps gain down automatically when the sensor saturates
- `lib/tmp117.py` — TMP117 driver; `therm.temperature` returns °C from the sensor die
- `lib/settings.py` — I²C pins, gain, integration time, `CALIBRATION_OFFSET`, `TMP117_ADDR`, TCP port, `HTTP_PORT`, `HOSTNAME`

`run_server()` uses `select.poll` on two listening sockets and serves one client at
a time. `take_reading()` is the single measurement path shared by both handlers.

`handle_sqm_client()` (port 10001, SQM-LE) optionally receives a command and dispatches:
- `rx` — take a reading, send one SQM-LE formatted line, close
- `cx [value]` — enter persistent calibration mode; reads or updates `CALIBRATION_OFFSET`; writes changes back to `/lib/settings.py` on the device filesystem
- `cx exit` — leave calibration mode

`handle_http_client()` (port 80, HTTP/1.0, `Connection: close`) routes:
- `GET /` — streams `/www/index.html` from flash in 512-byte chunks
- `GET /api/rx` — one reading as JSON: `mpsas`, `bortle`, `lux`, `hz`, `chan0`, `chan1`, `temp`, `offset`, `line`
- `GET /api/offset` — `{"offset": 12.6}`
- `POST /api/offset` with body `{"value": 12.8}` — sets and persists the offset
- `/favicon.ico` → 204, anything else → 404

MicroPython gotchas: f-string expressions must not contain a `:` inside a string
literal (write the value to a variable first). A browser's idle pre-connect socket
blocks the loop for `HTTP_TIMEOUT_S`; keep that small.

## SQM Protocol Response Format

```
r, XX.XXm,0000000000Hz,0000000000c,00000000.000s,  XX.XC\r\n
```

Fields: MPSAS (mag/arcsec²), derived Hz (lux × 50 000), raw chan0 counts, period proxy (chan0 / 460 800), TMP117 sensor temperature.

## Calibration

`MPSAS = -2.5 × log10(lux) + CALIBRATION_OFFSET`

`CALIBRATION_OFFSET` defaults to `12.6` in `lib/settings.py`. Adjust after comparing against a reference meter. The `cx` TCP command updates the offset and persists it to `/lib/settings.py` on the device without a reboot.
