#!/usr/bin/env python3
"""
SQM desktop console.

Serves a small web page on localhost and relays ``rx`` / ``cx`` commands to
the meter over TCP (Unihedron SQM-LE protocol, port 10001).  Standard
library only - no packages to install.

    python tools/sqm_console.py                      # meter at 192.168.0.252:10001
    python tools/sqm_console.py --sqm 192.168.0.50   # different meter address
    python tools/sqm_console.py --listen 8090        # different local web port
    python tools/sqm_console.py --lan                # also reachable from phone / laptop

The browser opens automatically at http://127.0.0.1:8080/.  The meter
address can also be changed from the page itself.

With --lan the page is served on every network interface, so any device on
the same WiFi can open http://<this computer's IP>:8080/ (the URLs are printed
at start-up).  Windows Firewall must allow Python inbound on that port; the
first run usually shows the firewall prompt, tick "Private networks".

Each button press is one short TCP session, so the meter's single client slot
is free for APT / SQM Reader between requests.  Commands are sent one at a
time and each reply is awaited before the next command goes out, because the
firmware reads commands with a single recv() and would otherwise see
"cx\\nrx" as one garbled command.
"""

import argparse
import json
import re
import socket
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_SQM_HOST = "192.168.0.252"
DEFAULT_SQM_PORT = 10001
DEFAULT_LISTEN   = 8080
SQM_TIMEOUT_S    = 8.0

# ── Meter I/O ────────────────────────────────────────────────────────────────

def sqm_session(host, port, commands):
    """Open one TCP session, send each command in turn, return one reply line each."""
    replies = []
    with socket.create_connection((host, port), timeout=SQM_TIMEOUT_S) as sock:
        reader = sock.makefile("rb")
        for cmd in commands:
            sock.sendall(cmd.encode("ascii") + b"\n")
            line = reader.readline()
            if not line:
                raise ConnectionError(f"meter closed the connection after '{cmd}'")
            replies.append(line.decode("ascii", "replace").strip())
    return replies


# Mirrors _BORTLE_THRESHOLDS in src/sqm_claude.py
_BORTLE_THRESHOLDS = (
    (21.99, 1), (21.89, 2), (21.69, 3), (20.49, 4),
    (19.50, 5), (18.94, 6), (18.38, 7), (17.83, 8),
)

def mpsas_to_bortle(mpsas):
    if mpsas is None:
        return None
    for threshold, cls in _BORTLE_THRESHOLDS:
        if mpsas >= threshold:
            return cls
    return 9


_RX_RE = re.compile(
    r"^r,\s*(?P<mpsas>[-\d.]+)m,(?P<hz>\d+)Hz,(?P<counts>\d+)c,"
    r"(?P<period>[\d.]+)s,\s*(?P<temp>[-\d.]+)C$"
)

def parse_rx(line):
    """'r, 16.00m,0005311181Hz,0000006239c,0000000.014s,  29.0C' -> dict"""
    m = _RX_RE.match(line)
    if not m:
        raise ValueError(f"unrecognised reading: {line!r}")
    try:
        mpsas = float(m["mpsas"])      # "----" when below the noise floor
    except ValueError:
        mpsas = None
    hz = int(m["hz"])
    return {
        "raw":    line,
        "mpsas":  mpsas,
        "bortle": mpsas_to_bortle(mpsas),
        "lux":    hz / 50_000.0,       # firmware derives Hz = lux * 50 000
        "hz":     hz,
        "counts": int(m["counts"]),
        "period": float(m["period"]),
        "temp":   float(m["temp"]),
    }


def parse_cal_line(line):
    """'MPSAS=16.00  Bortle=4  lux=0.000123  chan0=6239  chan1=12  T=29.0C' -> dict"""
    fields = dict(re.findall(r"(\w+)=([-\w.]+)", line))
    if "MPSAS" not in fields:
        raise ValueError(f"unrecognised calibration reading: {line!r}")
    return {
        "raw":    line,
        "mpsas":  float(fields["MPSAS"]),
        "bortle": int(fields["Bortle"]),
        "lux":    float(fields["lux"]),
        "chan0":  int(fields["chan0"]),
        "chan1":  int(fields["chan1"]),
        "temp":   float(fields["T"].rstrip("C")),
    }


def parse_offset(line):
    """'c, +12.60' -> 12.6 ; 'c, ERROR' raises"""
    m = re.match(r"^c,\s*([-+]?\d+(?:\.\d+)?)$", line)
    if not m:
        raise ValueError(f"meter replied {line!r}")
    return float(m.group(1))


# ── HTTP layer ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "SQMConsole/1.0"

    def _meter(self, query):
        host = query.get("host", [self.server.sqm_host])[0].strip() or self.server.sqm_host
        try:
            port = int(query.get("port", [self.server.sqm_port])[0])
        except ValueError:
            port = self.server.sqm_port
        return host, port

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _relay(self, fn):
        try:
            self._send_json(fn())
        except (OSError, ValueError, ConnectionError) as e:
            self._send_json({"error": str(e)}, status=502)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        host, port = self._meter(query)

        if url.path == "/":
            self._send_html(PAGE.replace("__DEFAULT_HOST__", self.server.sqm_host)
                                .replace("__DEFAULT_PORT__", str(self.server.sqm_port)))

        elif url.path == "/api/rx":
            def rx():
                (line,) = sqm_session(host, port, ["rx"])
                return {"reading": parse_rx(line), "time": datetime.now().isoformat(timespec="seconds")}
            self._relay(rx)

        elif url.path == "/api/detail":
            def detail():
                cx, rx, _bye = sqm_session(host, port, ["cx", "rx", "cx exit"])
                return {"offset": parse_offset(cx), "reading": parse_cal_line(rx),
                        "time": datetime.now().isoformat(timespec="seconds")}
            self._relay(detail)

        elif url.path == "/api/offset":
            def read_offset():
                cx, _bye = sqm_session(host, port, ["cx", "cx exit"])
                return {"offset": parse_offset(cx)}
            self._relay(read_offset)

        else:
            self.send_error(404)

    def do_POST(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        host, port = self._meter(query)

        if url.path == "/api/offset":
            length = int(self.headers.get("Content-Length", 0))
            try:
                value = float(json.loads(self.rfile.read(length) or b"{}").get("value"))
            except (TypeError, ValueError):
                self._send_json({"error": "offset must be a number"}, status=400)
                return

            def set_offset():
                cx, _bye = sqm_session(host, port, [f"cx {value:.2f}", "cx exit"])
                return {"offset": parse_offset(cx)}
            self._relay(set_offset)
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        if "/api/" in self.path:
            print(f"{datetime.now():%H:%M:%S}  {self.command} {self.path}  ->  {args[1]}")


# ── Page ─────────────────────────────────────────────────────────────────────

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SQM Console</title>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --line: #2a313b;
    --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff; --ok: #3fb950;
    --warn: #d29922; --bad: #f85149; --big: #ffffff;
  }
  body.night {
    --bg: #000; --panel: #140000; --line: #3a0000;
    --text: #ff5a5a; --muted: #a03030; --accent: #ff7070; --ok: #ff7070;
    --warn: #ff9a3c; --bad: #ff2020; --big: #ff6a6a;
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 16px; background: var(--bg); color: var(--text);
         font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  header { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 16px; }
  header h1 { font-size: 18px; margin: 0 auto 0 0; letter-spacing: .04em; }
  .addr { display: flex; gap: 6px; align-items: center; }
  .addr input { width: 150px; }
  .addr input.port { width: 70px; }
  .status { display: flex; align-items: center; gap: 6px; color: var(--muted); }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--muted); }
  .dot.ok { background: var(--ok); } .dot.busy { background: var(--warn); } .dot.bad { background: var(--bad); }
  main { display: grid; gap: 16px; grid-template-columns: 1fr; max-width: 1100px; margin: 0 auto; }
  @media (min-width: 820px) { main { grid-template-columns: 3fr 2fr; } .wide { grid-column: 1 / -1; } }
  section { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px; }
  section h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .1em; color: var(--muted); margin: 0 0 12px; }
  .readout { display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }
  .mpsas { font-size: 64px; font-weight: 600; line-height: 1; color: var(--big); font-variant-numeric: tabular-nums; }
  .mpsas small { font-size: 18px; color: var(--muted); font-weight: 400; margin-left: 6px; }
  .bortle { font-size: 20px; padding: 4px 12px; border: 1px solid var(--line); border-radius: 999px; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin-top: 16px; }
  .stat { border-top: 1px solid var(--line); padding-top: 6px; }
  .stat .k { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .06em; }
  .stat .v { font-size: 18px; font-variant-numeric: tabular-nums; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 14px; }
  button, select, input { font: inherit; color: var(--text); background: var(--bg);
    border: 1px solid var(--line); border-radius: 6px; padding: 7px 12px; }
  button { cursor: pointer; } button:hover { border-color: var(--accent); }
  button.primary { background: var(--accent); color: var(--bg); border-color: var(--accent); font-weight: 600; }
  button:disabled { opacity: .5; cursor: default; }
  input[type=number] { width: 110px; font-variant-numeric: tabular-nums; }
  label { color: var(--muted); }
  .hint { color: var(--muted); font-size: 12px; margin-top: 10px; }
  .offset { font-size: 32px; font-variant-numeric: tabular-nums; }
  .raw { font-family: ui-monospace, Consolas, monospace; font-size: 12px; color: var(--muted);
         margin-top: 12px; word-break: break-all; }
  .err { color: var(--bad); margin-top: 10px; min-height: 1.4em; }
  table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  th, td { text-align: right; padding: 5px 8px; border-bottom: 1px solid var(--line); }
  th:first-child, td:first-child { text-align: left; }
  th { color: var(--muted); font-weight: 500; font-size: 12px; }
  .tablewrap { max-height: 320px; overflow: auto; margin-top: 8px; }
  .toggle { margin-left: 8px; }
</style>
</head>
<body>
<header>
  <h1>SQM Console</h1>
  <div class="addr">
    <label for="host">Meter</label>
    <input id="host" value="__DEFAULT_HOST__" spellcheck="false">
    <input id="port" class="port" value="__DEFAULT_PORT__" inputmode="numeric">
  </div>
  <div class="status"><span class="dot" id="dot"></span><span id="statusText">idle</span></div>
  <button class="toggle" id="nightBtn" title="Red night-vision palette">Night</button>
</header>

<main>
  <section>
    <h2>Sky brightness</h2>
    <div class="readout">
      <div class="mpsas"><span id="mpsas">--.--</span><small>mag/arcsec²</small></div>
      <div class="bortle">Bortle <span id="bortle">–</span></div>
    </div>
    <div class="stats">
      <div class="stat"><div class="k">Lux</div><div class="v" id="lux">–</div></div>
      <div class="stat"><div class="k">Temp</div><div class="v" id="temp">–</div></div>
      <div class="stat"><div class="k">Chan0 counts</div><div class="v" id="counts">–</div></div>
      <div class="stat"><div class="k">Hz (derived)</div><div class="v" id="hz">–</div></div>
      <div class="stat"><div class="k">Last read</div><div class="v" id="when">–</div></div>
    </div>
    <div class="row">
      <button class="primary" id="readBtn">Take reading (rx)</button>
      <label for="auto">Auto</label>
      <select id="auto">
        <option value="0">off</option>
        <option value="5">every 5 s</option>
        <option value="10">every 10 s</option>
        <option value="30">every 30 s</option>
        <option value="60">every 60 s</option>
      </select>
    </div>
    <div class="raw" id="rawRx">–</div>
    <div class="err" id="errRx"></div>
  </section>

  <section>
    <h2>Calibration (cx)</h2>
    <div>Current offset <span class="offset" id="offset">–</span></div>
    <div class="row">
      <button id="offsetReadBtn">Read offset</button>
      <button id="detailBtn" title="cx, rx, cx exit: reading with chan0 / chan1 raw counts">Detailed read</button>
    </div>
    <div class="row">
      <label for="newOffset">New offset</label>
      <input type="number" id="newOffset" step="0.01" placeholder="12.60">
      <button id="offsetSetBtn">Set offset</button>
    </div>
    <div class="row">
      <label for="ref">Reference meter reads</label>
      <input type="number" id="ref" step="0.01" placeholder="21.30">
      <button id="suggestBtn" title="new = current offset + (reference minus this meter's last MPSAS)">Suggest</button>
    </div>
    <div class="hint">Set writes CALIBRATION_OFFSET into /lib/settings.py on the meter immediately.
      MPSAS = -2.5 · log10(lux) + offset, so raising the offset by 0.3 raises every reading by 0.3.</div>
    <div class="raw" id="rawCx">–</div>
    <div class="err" id="errCx"></div>
  </section>

  <section class="wide">
    <h2>Log</h2>
    <div class="row" style="margin-top:0">
      <button id="csvBtn">Save CSV</button>
      <button id="clearBtn">Clear</button>
      <span class="hint" style="margin:0" id="logCount">0 readings</span>
    </div>
    <div class="tablewrap">
      <table>
        <thead><tr><th>Time</th><th>MPSAS</th><th>Bortle</th><th>Lux</th><th>Temp °C</th><th>Chan0</th><th>Chan1</th><th>Source</th></tr></thead>
        <tbody id="log"></tbody>
      </table>
    </div>
  </section>
</main>

<script>
(() => {
  const $ = id => document.getElementById(id);
  const log = [];
  let timer = null, busy = false, lastMpsas = null;

  // Remember meter address, poll interval and palette between visits.
  try {
    const s = JSON.parse(localStorage.getItem('sqm-console') || '{}');
    if (s.host) $('host').value = s.host;
    if (s.port) $('port').value = s.port;
    if (s.auto) $('auto').value = s.auto;
    if (s.night) document.body.classList.add('night');
  } catch (e) {}
  function remember() {
    try {
      localStorage.setItem('sqm-console', JSON.stringify({
        host: $('host').value, port: $('port').value, auto: $('auto').value,
        night: document.body.classList.contains('night'),
      }));
    } catch (e) {}
  }

  function meterQuery() {
    return `?host=${encodeURIComponent($('host').value.trim())}&port=${encodeURIComponent($('port').value.trim())}`;
  }
  function setStatus(cls, text) { $('dot').className = 'dot ' + cls; $('statusText').textContent = text; }

  async function call(path, opts) {
    if (busy) throw new Error('another request is still in progress');
    busy = true;
    setStatus('busy', 'talking to meter…');
    document.querySelectorAll('button').forEach(b => { if (b.id !== 'nightBtn') b.disabled = true; });
    try {
      const r = await fetch(path + meterQuery(), opts);
      const j = await r.json();
      if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
      setStatus('ok', 'ok');
      return j;
    } catch (e) {
      setStatus('bad', 'error');
      throw e;
    } finally {
      busy = false;
      document.querySelectorAll('button').forEach(b => b.disabled = false);
    }
  }

  const fmt = (v, d) => (v === null || v === undefined) ? '–' : Number(v).toFixed(d);
  const fmtLux = v => v < 0.01 ? Number(v).toExponential(3) : fmt(v, 4);
  const hhmmss = iso => iso.slice(11, 19);

  function showReading(rd, when) {
    lastMpsas = rd.mpsas;
    $('mpsas').textContent  = rd.mpsas === null ? '----' : fmt(rd.mpsas, 2);
    $('bortle').textContent = rd.bortle ?? '–';
    $('lux').textContent    = fmtLux(rd.lux);
    $('temp').textContent   = fmt(rd.temp, 1) + ' °C';
    $('counts').textContent = (rd.counts ?? rd.chan0 ?? '–').toLocaleString();
    $('hz').textContent     = rd.hz !== undefined ? rd.hz.toLocaleString() : '–';
    $('when').textContent   = hhmmss(when);
  }

  function addLog(rd, when, source) {
    const row = { time: when, mpsas: rd.mpsas, bortle: rd.bortle, lux: rd.lux, temp: rd.temp,
                  chan0: rd.counts ?? rd.chan0 ?? '', chan1: rd.chan1 ?? '', source };
    log.push(row);
    if (log.length > 1000) log.shift();
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${hhmmss(row.time)}</td><td>${fmt(row.mpsas, 2)}</td><td>${row.bortle ?? '–'}</td>` +
      `<td>${fmtLux(row.lux)}</td><td>${fmt(row.temp, 1)}</td>` +
      `<td>${row.chan0}</td><td>${row.chan1}</td><td>${source}</td>`;
    const body = $('log');
    body.insertBefore(tr, body.firstChild);
    while (body.children.length > 1000) body.removeChild(body.lastChild);
    $('logCount').textContent = `${log.length} reading${log.length === 1 ? '' : 's'}`;
  }

  async function takeReading() {
    $('errRx').textContent = '';
    try {
      const j = await call('/api/rx');
      showReading(j.reading, j.time);
      $('rawRx').textContent = j.reading.raw;
      addLog(j.reading, j.time, 'rx');
    } catch (e) { $('errRx').textContent = e.message; }
  }

  async function detailedReading() {
    $('errCx').textContent = '';
    try {
      const j = await call('/api/detail');
      showReading(j.reading, j.time);
      $('offset').textContent = fmt(j.offset, 2);
      $('rawCx').textContent = j.reading.raw;
      addLog(j.reading, j.time, 'cx');
    } catch (e) { $('errCx').textContent = e.message; }
  }

  async function readOffset() {
    $('errCx').textContent = '';
    try {
      const j = await call('/api/offset');
      $('offset').textContent = fmt(j.offset, 2);
      $('rawCx').textContent = `c, ${j.offset >= 0 ? '+' : ''}${fmt(j.offset, 2)}`;
      if (!$('newOffset').value) $('newOffset').value = fmt(j.offset, 2);
    } catch (e) { $('errCx').textContent = e.message; }
  }

  async function setOffset() {
    $('errCx').textContent = '';
    const value = parseFloat($('newOffset').value);
    if (!Number.isFinite(value)) { $('errCx').textContent = 'enter a numeric offset first'; return; }
    if (!confirm(`Write CALIBRATION_OFFSET = ${value.toFixed(2)} to the meter?`)) return;
    try {
      const j = await call('/api/offset', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value }) });
      $('offset').textContent = fmt(j.offset, 2);
      $('rawCx').textContent = `c, ${j.offset >= 0 ? '+' : ''}${fmt(j.offset, 2)}  (saved to /lib/settings.py)`;
    } catch (e) { $('errCx').textContent = e.message; }
  }

  function suggest() {
    const ref = parseFloat($('ref').value);
    const cur = parseFloat($('offset').textContent);
    if (!Number.isFinite(ref)) { $('errCx').textContent = 'enter the reference meter value'; return; }
    if (!Number.isFinite(cur)) { $('errCx').textContent = 'read the current offset first'; return; }
    if (lastMpsas === null) { $('errCx').textContent = 'take a reading first'; return; }
    $('newOffset').value = (cur + (ref - lastMpsas)).toFixed(2);
    $('errCx').textContent = '';
  }

  function setAuto() {
    remember();
    clearInterval(timer); timer = null;
    const s = parseInt($('auto').value, 10);
    if (s > 0) { timer = setInterval(() => { if (!busy) takeReading(); }, s * 1000); takeReading(); }
  }

  function saveCsv() {
    if (!log.length) return;
    const head = 'time,mpsas,bortle,lux,temp_c,chan0,chan1,source';
    const rows = log.map(r => [r.time, r.mpsas ?? '', r.bortle ?? '', r.lux, r.temp, r.chan0, r.chan1, r.source].join(','));
    const blob = new Blob([head + '\n' + rows.join('\n') + '\n'], { type: 'text/csv' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `sqm-log-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  $('readBtn').onclick       = takeReading;
  $('detailBtn').onclick     = detailedReading;
  $('offsetReadBtn').onclick = readOffset;
  $('offsetSetBtn').onclick  = setOffset;
  $('suggestBtn').onclick    = suggest;
  $('auto').onchange         = setAuto;
  $('csvBtn').onclick        = saveCsv;
  $('clearBtn').onclick      = () => { log.length = 0; $('log').innerHTML = ''; $('logCount').textContent = '0 readings'; };
  $('host').onchange = $('port').onchange = remember;
  $('nightBtn').onclick = () => { document.body.classList.toggle('night'); remember(); };

  if (parseInt($('auto').value, 10) > 0) setAuto();
})();
</script>
</body>
</html>
"""


# ── Entry point ──────────────────────────────────────────────────────────────

def lan_addresses():
    """IPv4 addresses of this computer, excluding loopback, for the start-up banner."""
    found = []
    try:
        # The interface that routes to the meter is the one the phone will use too.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect((DEFAULT_SQM_HOST, 9))
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except OSError:
        pass
    return found


def main():
    ap = argparse.ArgumentParser(description="Web console for the SQM (rx / cx over TCP).")
    ap.add_argument("--sqm", default=DEFAULT_SQM_HOST, help=f"meter IP or hostname (default {DEFAULT_SQM_HOST})")
    ap.add_argument("--sqm-port", type=int, default=DEFAULT_SQM_PORT, help=f"meter TCP port (default {DEFAULT_SQM_PORT})")
    ap.add_argument("--listen", type=int, default=DEFAULT_LISTEN, help=f"local web port (default {DEFAULT_LISTEN})")
    ap.add_argument("--lan", action="store_true",
                    help="serve on all interfaces so phones / laptops on the WiFi can open the page")
    ap.add_argument("--no-browser", action="store_true", help="do not open the page automatically")
    args = ap.parse_args()

    bind = "0.0.0.0" if args.lan else "127.0.0.1"
    server = ThreadingHTTPServer((bind, args.listen), Handler)
    server.sqm_host = args.sqm
    server.sqm_port = args.sqm_port
    url = f"http://127.0.0.1:{args.listen}/"
    print(f"SQM console at {url}  (meter {args.sqm}:{args.sqm_port})  Ctrl-C to stop")
    if args.lan:
        for ip in lan_addresses():
            print(f"  from other devices on the WiFi:  http://{ip}:{args.listen}/")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
