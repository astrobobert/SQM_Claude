<!-- Use this file to provide workspace-specific custom instructions to Copilot. -->

# SQM_Claude MicroPython Workspace

## Project Overview

MicroPython firmware for a WiFi-enabled Sky Quality Meter running on a
Raspberry Pi Pico W (RP2040) or Pico W2 (RP2350) with an Adafruit TSL2591
light sensor. Serves sky-brightness readings over TCP using the Unihedron
SQM-LE ASCII protocol.

## Target Platform

- **Hardware:** Raspberry Pi Pico W / Pico W2
- **Firmware:** MicroPython (not CPython, not CircuitPython)
- **Toolchain:** mpremote or MicroPico VS Code extension

## Development Guidelines

- All code must run on MicroPython — avoid CPython-only stdlib modules
- Deploy with `mpremote` or the MicroPico VS Code extension
- `lib/secrets.py` is git-ignored; use `lib/secrets.example.py` as a template
- Calibration offset is persisted by rewriting `lib/settings.py` on device flash
- The TCP server handles one client at a time (per Unihedron SQM-LE spec)
