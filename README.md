# SQM_Claude — DIY Sky Quality Meter

MicroPython firmware for a WiFi-enabled Sky Quality Meter built on the
**Raspberry Pi Pico W** (RP2040) or **Pico W2** (RP2350) with an **Adafruit TSL2591** light sensor.

Serves readings over TCP using the Unihedron SQM-LE ASCII protocol, so it works
out of the box with astronomy software like APT, SQM Reader, and Sky Quality Monitor.

Based on:
- [PiSQM by chvvkumar](https://github.com/chvvkumar/PiSQM)
- ["Measuring Sky Brightness with a Raspberry Pi", MNASSA Oct 2017](https://www.mnassa.org.za/html/Oct2017/2017MNASSA..76..Oct..215.pdf)

Protocol reference: [Unihedron SQM-LE product page and user manual](https://unihedron.com/projects/sqm-le/).

---

## Hardware

| Component | Detail |
|-----------|--------|
| Controller | Raspberry Pi Pico W (RP2040) or Pico W2 (RP2350) |
| Sensor | Adafruit TSL2591 (I²C address `0x29`) |
| SDA | GPIO 16 (pin 21) |
| SCL | GPIO 17 (pin 22) |
| Sensor VIN | Pico 3V3(OUT) — pin 36 |
| Sensor GND | Pico GND — pin 33 |

---

## Chassis

The `chassis/` folder contains 3D-printable STL files for an enclosure that houses the Pico and TSL2591:

| File | Part |
|------|------|
| `SQM Chassis Top.stl` | Top half of enclosure |
| `SQM Chassis Bottom.stl` | Bottom half of enclosure |

The model is designed for **2 mm screw inserts**.

A modified **PATIKIL 12 mm LED Convex Lens (20°)** is used to concentrate light onto the TSL2591.
The bottom skirt of the lens is trimmed down to clear the TSL2591 breakout board and allow the lens to sit directly over the sensor. The lens is secured with hot glue.

> Focal distance is not precisely measured — position the sensor as close to the optical centre of the lens as the breakout board allows. Size the chassis aperture to match the lens you use and verify the fit before finalising your print.

---

## Project Structure

```
SQM_Claude/
├── main.py              entry point (imports src/SQM_Claude)
├── src/
│   └── sqm_claude.py    WiFi connect + TCP server + SQM logic
├── lib/
│   ├── tsl2591.py           TSL2591 MicroPython driver
│   ├── settings.py          I²C pins, gain, calibration, port
│   ├── secrets.py           WiFi credentials (not committed)
│   └── secrets.example.py  credential template
├── chassis/
│   ├── SQM Chassis Top.stl
│   └── SQM Chassis Bottom.stl
├── requirements.txt
└── LICENSE
```

---

## Deployment

Code runs on **MicroPython** — not CPython.

### 1. Flash MicroPython

Download the `.uf2` for your board from [micropython.org/download](https://micropython.org/download/).
Hold BOOTSEL while plugging in the Pico, copy the `.uf2` to the Pico drive, and it will reboot automatically.

### 2. Create WiFi credentials

`lib/secrets.py` is git-ignored. Copy the example and fill in your details:

```bash
cp lib/secrets.example.py lib/secrets.py
```

```python
WIFI_SSID     = "your_network_name"
WIFI_PASSWORD = "your_wifi_password"
```

### 3. Upload the project

**Option A — MicroPico VS Code extension**

Open `SQM_Claude/` in VS Code with the MicroPico extension installed, then use
**"Upload project to Pico"** to sync all files. The Pico boots `main.py` automatically on power-up.

**Option B — mpremote**

```bash
pip install mpremote
mpremote cp lib/tsl2591.py     :/lib/tsl2591.py
mpremote cp lib/secrets.py     :/lib/secrets.py
mpremote cp lib/settings.py    :/lib/settings.py
mpremote cp src/sqm_claude.py  :/src/sqm_claude.py
mpremote cp main.py            :/main.py
```

Quick test without rebooting:

```bash
mpremote run src/sqm_claude.py
```

---

## Configuration

Edit `lib/settings.py` before uploading:

| Setting | Default | Notes |
|---------|---------|-------|
| `I2C_SDA_PIN` | `16` | GPIO for SDA |
| `I2C_SCL_PIN` | `17` | GPIO for SCL |
| `TSL2591_GAIN` | `"MAX"` | `LOW` / `MED` / `HIGH` / `MAX` |
| `TSL2591_INTEGRATION` | `"600MS"` | `100MS` – `600MS` |
| `CALIBRATION_OFFSET` | `12.6` | mag/arcsec² zero-point — adjust to match a reference meter |
| `TCP_PORT` | `10001` | Standard Unihedron SQM port |

---

## TCP Protocol (Unihedron SQM-LE compatible)

Connect to the Pico's IP on port **10001**. The device accepts one connection at a time (per the SQM-LE spec).

```bash
telnet <pico-ip> 10001
# or
nc <pico-ip> 10001
```

### Commands

| Command | Effect |
|---------|--------|
| `rx` | Take a reading; return one SQM-LE formatted line and close |
| `cx` | Enter calibration mode (persistent connection, 60 s timeout) |
| `cx <value>` | Set `CALIBRATION_OFFSET` to `value` and persist to `lib/settings.py` |
| `cx exit` | Exit calibration mode and close connection |

### SQM-LE response format

The `rx` response follows the [Unihedron SQM-LE ASCII protocol](https://unihedron.com/projects/sqm-le/) (§8.2.1 of the user manual):

```
r, XX.XXm,0000000000Hz,0000000000c,0000000.000s,  27.4C\r\n
```

| Field | Example | Meaning |
|-------|---------|---------|
| MPSAS | `21.45m` | Sky brightness (mag/arcsec²) |
| Hz | `0000005915Hz` | Derived from lux × 50 000 (TSL2591 is not a photon counter) |
| Counts | `0000000042c` | Raw TSL2591 channel-0 ADC value |
| Period | `0000000.000s` | Counts / 460 800 Hz (SQM-LE convention) |
| Temp | `27.4C` | RP2040/RP2350 die temperature |

Astronomy software parses columns by fixed position — do not change field widths.

**APT note:** APT connects and immediately sends `rx`, expecting only the SQM-LE string back. The device handles this correctly.

---

## Physics

```
MPSAS = −2.5 × log₁₀(lux) + CALIBRATION_OFFSET
```

MPSAS is a logarithmic scale — higher values mean darker sky. Each magnitude step is ~2.5× in brightness.

**Bortle scale reference:**

| Bortle | Min MPSAS | Description |
|--------|-----------|-------------|
| 1 | 21.99 | Excellent dark sky |
| 2 | 21.89 | Truly dark site |
| 3 | 21.69 | Rural sky |
| 4 | 20.49 | Rural/suburban transition |
| 5 | 19.50 | Suburban sky |
| 6 | 18.94 | Bright suburban sky |
| 7 | 18.38 | Suburban/urban transition |
| 8 | 17.83 | City sky |
| 9 | — | Inner-city sky |

Expected precision for a calibrated unit is **±0.10 mag/arcsec²**. This build is uncalibrated by default.

---

## Calibration Workflow

1. Connect via TCP.
2. Send `cx` to enter calibration mode.
3. Take a reading with a reference meter (or compare against a known Bortle-class site).
4. Send `cx <offset>` to update the zero-point, e.g. `cx 14.20`.
5. Verify with `rx`.
6. Send `cx exit` — the new offset is persisted to `lib/settings.py` on flash and survives reboots.

---

## LED Status

| Pattern | Meaning |
|---------|---------|
| 3 slow blinks | Booting |
| Rapid blinking | Connecting to WiFi |
| 5 fast blinks | WiFi connected, server starting |
| Solid on | Server idle, waiting for client |
| 2 quick blinks then on | Client connected, reading taken |
