"""Tiny stdlib HTTP server exposing teleop state for the operator UI.

Two endpoints:
    GET /          → static HTML/JS that polls /state every 250 ms
    GET /state     → JSON snapshot of the most recent sent RoveControl
                     and received RoveTelemetry, plus packet counters.

Also writes every sent/received frame to a per-session CSV log so a
runaway control session can be replayed offline.
"""
from __future__ import annotations

import csv
import json
import logging
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from .network.udp_telemetry_receiver import UdpTelemetryReceiver

log = logging.getLogger(__name__)


_INDEX_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8" />
<title>capra teleop</title>
<style>
 body { font-family: ui-monospace, monospace; background:#101418; color:#d6d6d6;
        margin:0; padding:1em; }
 h1 { color:#7fd1ff; margin:0 0 .4em; font-size:1.1em; }
 .grid { display:grid; grid-template-columns: 1fr 1fr; gap:1em; }
 .card { background:#1a1f25; border:1px solid #29303a; border-radius:6px; padding:.8em 1em; }
 .row { display:flex; justify-content:space-between; padding:2px 0; border-bottom:1px solid #222831; }
 .row:last-child { border-bottom:none; }
 .label { color:#8a98a8; }
 .val { color:#e8e8e8; }
 .small { color:#5d6878; font-size:.85em; }
 .bad { color:#ff7777; }
 .good { color:#7fe09a; }
 .joints { display:grid; grid-template-columns: repeat(6, 1fr); gap:4px; margin-top:.6em; }
 .joint { background:#101418; padding:.4em; border-radius:4px; text-align:center; }
 .joint .n { color:#7fd1ff; font-size:.8em; }
 .joint .v { font-variant-numeric: tabular-nums; }
</style></head><body>
<h1>capra teleop — <span id="status">connecting…</span></h1>
<div class="grid">
  <div class="card">
    <div style="font-weight:bold;margin-bottom:.4em;">Sent (RoveControl)</div>
    <div class="row"><span class="label">tracks L/R</span><span class="val" id="tracks">—</span></div>
    <div class="row"><span class="label">flippers fl/fr/rl/rr</span><span class="val" id="flippers">—</span></div>
    <div class="row"><span class="label">ovis pos xyz</span><span class="val" id="ovis_pos">—</span></div>
    <div class="row"><span class="label">ovis ori ypr</span><span class="val" id="ovis_ori">—</span></div>
    <div class="row"><span class="label">gripper</span><span class="val" id="gripper">—</span></div>
    <div class="row small"><span>sent #</span><span id="sent_count">0</span></div>
  </div>
  <div class="card">
    <div style="font-weight:bold;margin-bottom:.4em;">Received (RoveTelemetry)</div>
    <div class="row"><span class="label">timestamp_us</span><span class="val" id="rx_ts">—</span></div>
    <div class="row"><span class="label">machine_state</span><span class="val" id="rx_state">—</span></div>
    <div class="joints" id="joints"></div>
    <div class="row small" style="margin-top:.5em;"><span>received #</span><span id="rx_count">0</span></div>
  </div>
</div>
<script>
async function tick() {
  try {
    const r = await fetch('/state');
    if (!r.ok) throw new Error(r.status);
    const j = await r.json();
    document.getElementById('status').innerHTML = '<span class="good">live</span>';
    if (j.sent) {
      const s = j.sent;
      document.getElementById('tracks').textContent = `${fmt(s.tracks_left)} / ${fmt(s.tracks_right)}`;
      document.getElementById('flippers').textContent =
        `${s.flippers.fl} / ${s.flippers.fr} / ${s.flippers.rl} / ${s.flippers.rr}`;
      document.getElementById('ovis_pos').textContent =
        `${fmt(s.ovis.x)} / ${fmt(s.ovis.y)} / ${fmt(s.ovis.z)}`;
      document.getElementById('ovis_ori').textContent =
        `${fmt(s.ovis.yaw)} / ${fmt(s.ovis.pitch)} / ${fmt(s.ovis.roll)}`;
      document.getElementById('gripper').textContent = s.gripper_open ? 'OPEN' : 'CLOSED';
      document.getElementById('sent_count').textContent = j.sent_count;
    }
    document.getElementById('rx_count').textContent = j.rx_count;
    if (j.telemetry) {
      const t = j.telemetry;
      document.getElementById('rx_ts').textContent = t.timestamp_us;
      document.getElementById('rx_state').textContent = t.machine_state;
      const cells = t.joints.map((jt, i) =>
        `<div class="joint"><div class="n">J${i+1}</div>` +
        `<div class="v">${fmt(jt.pos)}°</div>` +
        `<div class="v small">${fmt(jt.amp)} A</div>` +
        `<div class="v small">${fmt(jt.temp)} °C</div></div>`
      ).join('');
      document.getElementById('joints').innerHTML = cells;
    }
  } catch (e) {
    document.getElementById('status').innerHTML = '<span class="bad">offline (' + e + ')</span>';
  }
}
function fmt(v) { return (v == null) ? '—' : (+v).toFixed(2); }
setInterval(tick, 250);
tick();
</script></body></html>
"""


class TeleopState:
    """Thread-safe snapshot the HTTP handler reads from."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sent: Optional[dict] = None
        self._sent_count = 0

    def on_sent(self, msg) -> None:
        snap = {
            "tracks_left": msg.tracks.left_vel,
            "tracks_right": msg.tracks.right_vel,
            "flippers": {
                "fl": msg.flippers.fl,
                "fr": msg.flippers.fr,
                "rl": msg.flippers.rl,
                "rr": msg.flippers.rr,
            },
            "ovis": {
                "x": msg.ovis.position.x,
                "y": msg.ovis.position.y,
                "z": msg.ovis.position.z,
                "yaw": msg.ovis.orientation.yaw,
                "pitch": msg.ovis.orientation.pitch,
                "roll": msg.ovis.orientation.roll,
            },
            "gripper_open": bool(msg.gripper.open_state),
            "timestamp_us": int(msg.timestamp_us),
        }
        with self._lock:
            self._sent = snap
            self._sent_count += 1

    def snapshot(self, receiver: Optional[UdpTelemetryReceiver]) -> dict:
        with self._lock:
            sent = dict(self._sent) if self._sent is not None else None
            sent_count = self._sent_count
        telemetry = None
        rx_count = 0
        if receiver is not None:
            t = receiver.latest()
            rx_count = receiver.packet_count()
            if t is not None:
                joints = []
                for i in range(1, 7):
                    a = getattr(t.ovis, f"act_{i}")
                    joints.append(
                        {
                            "pos": a.motor_pos,
                            "amp": a.motor_amp,
                            "temp": a.motor_temp_c,
                            "state": a.node_state,
                        }
                    )
                telemetry = {
                    "timestamp_us": int(t.timestamp_us),
                    "machine_state": int(t.machine_state),
                    "joints": joints,
                }
        return {
            "sent": sent,
            "sent_count": sent_count,
            "telemetry": telemetry,
            "rx_count": rx_count,
        }


class TeleopHttpServer:
    """ThreadingHTTPServer wrapper running the UI in a daemon thread."""

    def __init__(
        self,
        state: TeleopState,
        receiver: Optional[UdpTelemetryReceiver],
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self._state = state
        self._receiver = receiver
        self._host = host
        self._port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        state = self._state
        receiver = self._receiver

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args, **_kw):
                # Quiet — we already have our own logger.
                return

            def do_GET(self):
                if self.path == "/" or self.path.startswith("/index"):
                    body = _INDEX_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/state":
                    body = json.dumps(state.snapshot(receiver)).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

        self._server = ThreadingHTTPServer((self._host, self._port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="TeleopHttpServer",
            daemon=True,
        )
        self._thread.start()
        log.info("Teleop UI at http://%s:%d", self._host, self._port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None


class CsvLogger:
    """Append-only CSV writer for sent + received frames.

    Two files per session live in ``log_dir``: ``sent_<ts>.csv`` and
    ``recv_<ts>.csv``. Writes are buffered behind a lock so concurrent
    callers (sender thread, receiver thread) don't interleave rows.
    """

    SENT_COLS = [
        "timestamp_us", "tracks_left", "tracks_right",
        "flip_fl", "flip_fr", "flip_rl", "flip_rr",
        "ovis_x", "ovis_y", "ovis_z",
        "ovis_yaw", "ovis_pitch", "ovis_roll",
        "gripper_open",
    ]
    RECV_COLS = (
        ["timestamp_us", "machine_state"]
        + [f"j{i}_{f}" for i in range(1, 7) for f in ("pos", "vel", "amp", "temp")]
    )

    def __init__(self, log_dir: Path) -> None:
        self._dir = log_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._sent_path = self._dir / f"sent_{stamp}.csv"
        self._recv_path = self._dir / f"recv_{stamp}.csv"
        self._sent_lock = threading.Lock()
        self._recv_lock = threading.Lock()
        self._sent_f = self._sent_path.open("w", newline="")
        self._recv_f = self._recv_path.open("w", newline="")
        self._sent_w = csv.writer(self._sent_f)
        self._recv_w = csv.writer(self._recv_f)
        self._sent_w.writerow(self.SENT_COLS)
        self._recv_w.writerow(self.RECV_COLS)
        log.info("CSV logs: sent=%s recv=%s", self._sent_path, self._recv_path)

    def log_sent(self, msg) -> None:
        row = [
            int(msg.timestamp_us),
            f"{msg.tracks.left_vel:.4f}", f"{msg.tracks.right_vel:.4f}",
            msg.flippers.fl, msg.flippers.fr, msg.flippers.rl, msg.flippers.rr,
            f"{msg.ovis.position.x:.4f}", f"{msg.ovis.position.y:.4f}", f"{msg.ovis.position.z:.4f}",
            f"{msg.ovis.orientation.yaw:.4f}", f"{msg.ovis.orientation.pitch:.4f}",
            f"{msg.ovis.orientation.roll:.4f}",
            int(bool(msg.gripper.open_state)),
        ]
        with self._sent_lock:
            self._sent_w.writerow(row)
            self._sent_f.flush()

    def log_recv(self, telemetry) -> None:
        row = [int(telemetry.timestamp_us), int(telemetry.machine_state)]
        for i in range(1, 7):
            a = getattr(telemetry.ovis, f"act_{i}")
            row.extend([
                f"{a.motor_pos:.4f}",
                f"{0.0:.4f}",     # no velocity field on DriveNodeState
                f"{a.motor_amp:.4f}",
                f"{a.motor_temp_c:.4f}",
            ])
        with self._recv_lock:
            self._recv_w.writerow(row)
            self._recv_f.flush()

    def close(self) -> None:
        try:
            self._sent_f.close()
        finally:
            self._recv_f.close()
