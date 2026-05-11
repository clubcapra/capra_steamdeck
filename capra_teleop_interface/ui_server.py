"""Tiny stdlib HTTP server exposing teleop state for the operator UI.

Endpoints:
    GET  /          → static HTML/JS dashboard
    GET  /state     → JSON snapshot of the most recent sent RoveControl
                      and received RoveTelemetry, plus packet counters.
    POST /estop     → engage emergency stop: zeroes outbound commands and
                      asks the sensor api to erase queued trajectories.
    POST /resume    → clear the emergency stop flag.

Also writes every sent/received frame to a per-session CSV log so a
runaway control session can be replayed offline.
"""
from __future__ import annotations

import csv
import json
import logging
import threading
import time
import urllib.error
import urllib.request
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
 .estop {
   margin-top:1em; padding:1em; background:#1a1f25; border:1px solid #29303a;
   border-radius:6px; text-align:center;
 }
 .estop-btn {
   width:100%; padding:1.4em; font-size:2em; font-weight:900; letter-spacing:.18em;
   border:3px solid #5a1010; border-radius:8px; background:#b81818; color:#fff;
   cursor:pointer; transition: background .08s, transform .04s;
   text-shadow: 0 1px 0 #000;
 }
 .estop-btn:hover { background:#d62020; }
 .estop-btn:active { transform: translateY(1px); }
 .estop-btn:disabled { background:#454e58; border-color:#2a2f36; color:#9aa6b3; cursor:default; }
 .resume-btn {
   margin-top:.6em; padding:.7em 1.3em; font-size:1em; font-weight:700;
   border:1px solid #2a2f36; background:#1f3a26; color:#cfeed3;
   border-radius:6px; cursor:pointer;
 }
 .resume-btn:hover { background:#296b3a; color:#fff; }
 .estop-status { margin-top:.6em; font-size:.95em; }
 .banner-estop {
   position:sticky; top:0; margin: 0 -1em .8em -1em; padding:.6em 1em;
   background:#b81818; color:#fff; font-weight:bold; text-align:center;
   border-bottom:2px solid #5a1010; letter-spacing:.08em;
 }
 .hidden { display:none; }
</style></head><body>
<div id="estop_banner" class="banner-estop hidden">⛔ E-STOP ENGAGED — outbound commands zeroed</div>
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
<div class="estop">
  <button id="estop_btn" class="estop-btn" onclick="trigger_estop()">E-STOP</button>
  <button id="resume_btn" class="resume-btn hidden" onclick="trigger_resume()">RESUME</button>
  <div id="estop_status" class="estop-status small">click to halt all outbound commands</div>
</div>
<script>
async function tick() {
  try {
    const r = await fetch('/state');
    if (!r.ok) throw new Error(r.status);
    const j = await r.json();
    document.getElementById('status').innerHTML = '<span class="good">live</span>';
    apply_estop(j.estopped);
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
function apply_estop(is_estopped) {
  const banner = document.getElementById('estop_banner');
  const btn = document.getElementById('estop_btn');
  const resume = document.getElementById('resume_btn');
  const stat = document.getElementById('estop_status');
  if (is_estopped) {
    banner.classList.remove('hidden');
    btn.disabled = true;
    btn.textContent = 'STOPPED';
    resume.classList.remove('hidden');
    stat.textContent = 'outbound zeroed · sensor api asked to erase trajectories';
  } else {
    banner.classList.add('hidden');
    btn.disabled = false;
    btn.textContent = 'E-STOP';
    resume.classList.add('hidden');
    stat.textContent = 'click to halt all outbound commands';
  }
}
async function trigger_estop() {
  try {
    const r = await fetch('/estop', {method: 'POST'});
    const j = await r.json();
    apply_estop(true);
    document.getElementById('estop_status').textContent = j.api_status || 'engaged';
  } catch (e) {
    document.getElementById('estop_status').textContent = 'estop POST failed: ' + e;
  }
}
async function trigger_resume() {
  try {
    await fetch('/resume', {method: 'POST'});
    apply_estop(false);
  } catch (e) {
    document.getElementById('estop_status').textContent = 'resume POST failed: ' + e;
  }
}
function fmt(v) { return (v == null) ? '—' : (+v).toFixed(2); }
// Space-bar also triggers E-STOP for quick muscle memory.
document.addEventListener('keydown', (e) => {
  if (e.key === ' ' || e.code === 'Space') {
    e.preventDefault();
    trigger_estop();
  }
});
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
        self._estopped = False

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

    def set_estop(self, value: bool) -> None:
        with self._lock:
            self._estopped = bool(value)

    def is_estopped(self) -> bool:
        with self._lock:
            return self._estopped

    def snapshot(self, receiver: Optional[UdpTelemetryReceiver]) -> dict:
        with self._lock:
            sent = dict(self._sent) if self._sent is not None else None
            sent_count = self._sent_count
            estopped = self._estopped
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
            "estopped": estopped,
        }


def zero_rove_control(msg) -> None:
    """Mutate a RoveControl message in place to a no-op frame.

    Used by the E-stop path: we want to keep the heartbeat going so the
    rover never thinks the operator is gone, but every commanded motion
    has to be zero.
    """
    msg.tracks.left_vel = 0.0
    msg.tracks.right_vel = 0.0
    msg.flippers.fl = 0
    msg.flippers.fr = 0
    msg.flippers.rl = 0
    msg.flippers.rr = 0
    msg.ovis.position.x = 0.0
    msg.ovis.position.y = 0.0
    msg.ovis.position.z = 0.0
    msg.ovis.orientation.yaw = 0.0
    msg.ovis.orientation.pitch = 0.0
    msg.ovis.orientation.roll = 0.0
    # gripper.open_state is latched state, not a velocity — leave it.


def post_estop_to_api(api_base_url: str) -> str:
    """Tell the sensor api to halt the arm directly.

    We don't rely on the rover-side wrapper for this: the wrapper's
    velocity stream will already go to zero (the strategy is zeroed
    upstream), but erasing queued trajectories is the only way to drop
    motion that's already been latched into the firmware. Best effort —
    a failed POST is logged but doesn't gate the local zeroing.
    """
    if not api_base_url:
        return "no api_base_url configured"
    url = api_base_url.rstrip("/") + "/kinova_arm/command"
    body = {
        "erase_trajectories": True,
        "joint_1_vel": 0.0,
        "joint_2_vel": 0.0,
        "joint_3_vel": 0.0,
        "joint_4_vel": 0.0,
        "joint_5_vel": 0.0,
        "joint_6_vel": 0.0,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return f"api POST {resp.status}"
    except urllib.error.URLError as e:
        log.warning("E-stop api POST failed: %s", e)
        return f"api POST failed: {e.reason if hasattr(e, 'reason') else e}"
    except Exception as e:  # pragma: no cover
        log.warning("E-stop api POST raised: %s", e)
        return f"api POST raised: {e}"


class TeleopHttpServer:
    """ThreadingHTTPServer wrapper running the UI in a daemon thread."""

    def __init__(
        self,
        state: TeleopState,
        receiver: Optional[UdpTelemetryReceiver],
        host: str = "127.0.0.1",
        port: int = 8765,
        api_base_url: str = "",
    ) -> None:
        self._state = state
        self._receiver = receiver
        self._host = host
        self._port = port
        self._api_base_url = api_base_url
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        state = self._state
        receiver = self._receiver
        api_base_url = self._api_base_url

        def _json_response(handler, body: dict, status: int = 200) -> None:
            payload = json.dumps(body).encode("utf-8")
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)

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
                    _json_response(self, state.snapshot(receiver))
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                if self.path == "/estop":
                    state.set_estop(True)
                    api_status = post_estop_to_api(api_base_url)
                    log.warning("E-STOP engaged via UI (%s)", api_status)
                    _json_response(
                        self,
                        {"estopped": True, "api_status": api_status},
                    )
                elif self.path == "/resume":
                    state.set_estop(False)
                    log.warning("E-STOP cleared via UI")
                    _json_response(self, {"estopped": False})
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
