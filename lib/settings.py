"""
SQM configuration — edit to match your hardware and calibration.
WiFi credentials live in lib/secrets.py (not committed to git).
"""

# ── I2C ──────────────────────────────────────────────────────────────────────
I2C_ID      = 0      # I2C bus: 0 or 1
I2C_SDA_PIN = 16     # GP16
I2C_SCL_PIN = 17     # GP17
I2C_FREQ    = 400_000

# ── TSL2591 ──────────────────────────────────────────────────────────────────
# Default to maximum sensitivity for dark-sky work.
# raw_auto() will step the gain down automatically if the sensor saturates.
TSL2591_GAIN        = "MAX"   # LOW | MED | HIGH | MAX
TSL2591_INTEGRATION = "600MS" # 100MS | 200MS | 300MS | 400MS | 500MS | 600MS

# ── SQM calculation ──────────────────────────────────────────────────────────
# MPSAS = -2.5 * log10(lux) + CALIBRATION_OFFSET
# Starting value is theoretical; adjust after comparing to a reference meter.
CALIBRATION_OFFSET = 12.6

# ── TCP server ───────────────────────────────────────────────────────────────
TCP_PORT      = 10001   # Standard Unihedron SQM port
TCP_TIMEOUT_S = 10      # Client socket read timeout (rx sessions)
CX_TIMEOUT_S  = 60      # Socket read timeout during a cx persistent session

# ── HTTP server (browser page + JSON API) ────────────────────────────────────
HTTP_PORT      = 80     # http://<meter-ip>/  from any phone or laptop on the WiFi
HTTP_TIMEOUT_S = 3      # Give up on a browser connection that sends nothing
HOSTNAME       = "sqm"  # DHCP hostname; many routers then resolve http://sqm/

# ── TMP117 temperature sensor ────────────────────────────────────────────────
TMP117_ADDR = 0x48        # default (ADD0 → GND); range 0x48–0x4B

# ── Status LED ───────────────────────────────────────────────────────────────
LED_PIN = "LED"           # Pico W2 onboard LED (CYW43 GPIO)
