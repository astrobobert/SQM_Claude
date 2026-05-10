# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

MicroPython firmware for a WiFi-enabled Sky Quality Meter running on a **Raspberry Pi Pico W2** (RP2350 + CYW43439). It serves sky-brightness readings over TCP using the Unihedron SQM-LE ASCII protocol, compatible with apps like SQM Reader and Sky Quality Monitor.

Hardware: TSL2591 light sensor wired I²C on SDA=GP16, SCL=GP17.

## Deploying to Device

`mpremote` is the only toolchain. Install it with:
```
pip install mpremote
```

Upload everything to the device:
```
mpremote cp lib/tsl2591.py  :/lib/tsl2591.py
mpremote cp lib/secrets.py  :/lib/secrets.py
mpremote cp config/settings.py :/config/settings.py
mpremote cp src/SQM_Claude.py :/src/SQM_Claude.py
mpremote cp main.py :/main.py
```

Run without rebooting (for quick testing):
```
mpremote run src/SQM_Claude.py
```

Monitor serial output:
```
mpremote
```

List files on device:
```
mpremote ls -la
```

## Credentials (not in git)

`lib/secrets.py` is git-ignored. Create it locally before uploading:
```python
WIFI_SSID     = "your_network"
WIFI_PASSWORD = "your_password"
```

## Architecture

- `main.py` — trivial entry point: `import src.SQM_Claude`
- `src/SQM_Claude.py` — all application logic: WiFi connect, TCP server loop, SQM math, LED blink helpers
- `lib/tsl2591.py` — MicroPython driver for the TSL2591; `raw_auto()` steps gain down automatically when the sensor saturates (bright sky / daylight)
- `config/settings.py` — I²C pins, gain, integration time, `CALIBRATION_OFFSET`, TCP port

The TCP server is blocking and handles one client at a time. On connect it optionally receives a command and dispatches:
- `rx` — take a reading, send one SQM-LE formatted line, close
- `cx [value]` — enter persistent calibration mode; reads or updates `CALIBRATION_OFFSET`; writes changes back to `/lib/settings.py` on the device filesystem (`update_calibration()` in `src/SQM_Claude.py`)
- `cx exit` — leave calibration mode

## SQM Protocol Response Format

```
r, XX.XXm,0000000000Hz,0000000000c,00000000.000s,  XX.XC\r\n
```

Fields: MPSAS (mag/arcsec²), derived Hz (lux × 50 000), raw chan0 counts, period proxy (chan0 / 460 800), RP2350 die temperature.

## Calibration

`MPSAS = -2.5 × log10(lux) + CALIBRATION_OFFSET`

`CALIBRATION_OFFSET` defaults to `12.6` in `config/settings.py`. Adjust after comparing against a reference meter. The `cx` TCP command updates the offset and persists it to `/lib/settings.py` on the device without a reboot.
