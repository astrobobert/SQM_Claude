"""
TMP117 high-accuracy temperature sensor driver for MicroPython.
Datasheet: https://www.ti.com/product/TMP117

Default I²C address: 0x48 (ADD0 → GND).
Resolution: 0.0078125 °C per LSB (1/128 °C).
"""

from machine import I2C

_REG_TEMP = 0x00   # Temperature result (16-bit, read-only)
_REG_ID   = 0x0F   # Device ID

_DEVICE_ID = 0x0117
_LSB_C     = 7.8125e-3  # °C per LSB


class TMP117:
    def __init__(self, i2c: I2C, addr: int = 0x48):
        self._i2c = i2c
        self._addr = addr

        dev_id = self._read16(_REG_ID)
        if dev_id != _DEVICE_ID:
            raise RuntimeError(f"TMP117 not found (ID=0x{dev_id:04x}, expected 0x0117)")

    def _read16(self, reg: int) -> int:
        self._i2c.writeto(self._addr, bytes([reg]))
        d = self._i2c.readfrom(self._addr, 2)
        return (d[0] << 8) | d[1]

    @property
    def temperature(self) -> float:
        raw = self._read16(_REG_TEMP)
        if raw > 32767:
            raw -= 65536
        return raw * _LSB_C
