"""
TSL2591 high-dynamic-range light sensor driver for MicroPython.
Datasheet: https://ams.com/tsl25911

Lux formula coefficients and gain/integration-time constants derived from:
  Adafruit CircuitPython TSL2591 Library
  Author: Tony DiCola for Adafruit Industries
  License: MIT
  https://github.com/adafruit/Adafruit_CircuitPython_TSL2591

Remaining implementation (raw(), raw_auto(), machine.I2C interface) is
original code written for this project.
"""

import time
from machine import I2C

_ADDR = 0x29
_CMD = 0xA0        # Command register bit (must be set for all register access)
_WORD = 0x20       # Word (multi-byte) read mode bit

_REG_ENABLE = 0x00
_REG_CONFIG = 0x01
_REG_ID     = 0x12
_REG_CHAN0  = 0x14  # Full spectrum (visible + IR), 16-bit LE
_REG_CHAN1  = 0x16  # IR only, 16-bit LE

_ENABLE_OFF = 0x00
_ENABLE_ON  = 0x01  # Power on
_ENABLE_AEN = 0x02  # ALS enable
_ENABLE_RUN = 0x03  # Power on + ALS enable

# Gain constants (upper nibble of CONFIG register)
GAIN_LOW  = 0x00   # 1x
GAIN_MED  = 0x10   # 25x
GAIN_HIGH = 0x20   # 428x
GAIN_MAX  = 0x30   # 9876x

# Integration time constants (lower nibble of CONFIG register)
INT_100MS = 0x00
INT_200MS = 0x01
INT_300MS = 0x02
INT_400MS = 0x03
INT_500MS = 0x04
INT_600MS = 0x05

_INT_MS   = (100, 200, 300, 400, 500, 600)
_GAIN_VAL = (1, 25, 428, 9876)

# Lux formula coefficients (from TSL2591 datasheet / Adafruit)
_LUX_DF    = 408.0
_LUX_COEFB = 1.64
_LUX_COEFC = 0.59
_LUX_COEFD = 0.86

_SATURATION = 65535


class TSL2591:
    def __init__(self, i2c: I2C, addr: int = _ADDR):
        self._i2c = i2c
        self._addr = addr
        self._gain = GAIN_MAX
        self._integration = INT_600MS

        chip_id = self._read_reg(_REG_ID)
        if chip_id != 0x50:
            raise RuntimeError(f"TSL2591 not found (ID=0x{chip_id:02x}, expected 0x50)")

        self._apply_config()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    @property
    def gain(self) -> int:
        return self._gain

    @gain.setter
    def gain(self, value: int):
        if value not in (GAIN_LOW, GAIN_MED, GAIN_HIGH, GAIN_MAX):
            raise ValueError("Invalid gain")
        self._gain = value
        self._apply_config()

    @property
    def integration_time(self) -> int:
        return self._integration

    @integration_time.setter
    def integration_time(self, value: int):
        if value not in range(6):
            raise ValueError("Integration time must be INT_100MS … INT_600MS")
        self._integration = value
        self._apply_config()

    def raw(self) -> tuple:
        """Return (chan0, chan1) after one integration cycle.

        chan0 = full spectrum (visible + IR)
        chan1 = IR only
        Returns (65535, 65535) on saturation.
        """
        self._write_reg(_REG_ENABLE, _ENABLE_RUN)
        time.sleep_ms(_INT_MS[self._integration] + 20)
        chan0, chan1 = self._read_channels()
        self._write_reg(_REG_ENABLE, _ENABLE_OFF)
        return chan0, chan1

    def raw_auto(self) -> tuple:
        """Like raw(), but steps down gain automatically on saturation.

        Returns (chan0, chan1, gain_used).
        """
        gains = [GAIN_MAX, GAIN_HIGH, GAIN_MED, GAIN_LOW]
        for g in gains:
            self._gain = g
            self._apply_config()
            c0, c1 = self.raw()
            if c0 < 60000 and c1 < 60000:
                return c0, c1, g
        # Even at GAIN_LOW everything is saturated (full daylight)
        return c0, c1, GAIN_LOW

    def lux(self, chan0: int, chan1: int) -> float:
        """Convert raw channel counts to lux using the current gain/integration."""
        if chan0 == _SATURATION or chan1 == _SATURATION:
            return float("inf")
        if chan0 == 0:
            return 0.0

        atime = _INT_MS[self._integration]
        again = _GAIN_VAL[self._gain >> 4]

        cpl  = (atime * again) / _LUX_DF
        lux1 = (chan0 - _LUX_COEFB * chan1) / cpl
        lux2 = (_LUX_COEFC * chan0 - _LUX_COEFD * chan1) / cpl
        return max(0.0, max(lux1, lux2))

    def read_lux(self) -> float:
        """Convenience: auto-gain raw read + lux calculation."""
        c0, c1, _ = self.raw_auto()
        return self.lux(c0, c1)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _write_reg(self, reg: int, value: int):
        self._i2c.writeto(self._addr, bytes([_CMD | reg, value]))

    def _read_reg(self, reg: int) -> int:
        self._i2c.writeto(self._addr, bytes([_CMD | reg]))
        return self._i2c.readfrom(self._addr, 1)[0]

    def _read_channels(self) -> tuple:
        # WORD bit enables auto-increment: reads chan0_lo, chan0_hi, chan1_lo, chan1_hi
        self._i2c.writeto(self._addr, bytes([_CMD | _WORD | _REG_CHAN0]))
        d = self._i2c.readfrom(self._addr, 4)
        chan0 = d[0] | (d[1] << 8)
        chan1 = d[2] | (d[3] << 8)
        return chan0, chan1

    def _apply_config(self):
        self._write_reg(_REG_CONFIG, self._gain | self._integration)
