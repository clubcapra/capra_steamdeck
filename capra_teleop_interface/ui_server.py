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
import queue
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
 * { box-sizing: border-box; }
 body { font-family: ui-monospace, monospace; background:#101418; color:#d6d6d6;
        margin:0; padding:.8em 1em; }
 h1 { color:#7fd1ff; margin:0 0 .6em; font-size:1.05em; }

 /* Strategy selector */
 .strategy-bar { display:flex; gap:.5em; margin-bottom:.9em; }
 .strat-btn {
   flex:1; padding:.65em .5em; font-size:.9em; font-weight:700;
   letter-spacing:.07em; text-transform:uppercase;
   border-radius:6px; border:2px solid #29303a;
   background:#1a1f25; color:#5d6878; cursor:pointer;
   transition: background .1s, color .1s, border-color .1s;
 }
 .strat-btn:hover { background:#222a33; color:#b0bec8; }
 .strat-btn.active { background:#102030; color:#7fd1ff; border-color:#3a7aaa; }

 /* Two-column layout */
 .grid { display:grid; grid-template-columns:1fr 1fr; gap:.9em; }
 .card { background:#1a1f25; border:1px solid #29303a; border-radius:6px;
         padding:.75em .95em; }
 .card-title { font-weight:700; font-size:.9em; letter-spacing:.04em;
               color:#c4ccd4; border-bottom:1px solid #29303a;
               padding-bottom:.4em; margin-bottom:.6em; }
 .sec-label { font-size:.72em; letter-spacing:.1em; text-transform:uppercase;
              color:#3d4e5e; margin:.6em 0 .25em; }
 .row { display:flex; justify-content:space-between; align-items:baseline;
        padding:2px 0; border-bottom:1px solid #16191e; }
 .row:last-child { border-bottom:none; }
 .label { color:#6b7a8a; font-size:.88em; }
 .val { color:#e2e2e2; font-variant-numeric:tabular-nums; }
 .small { color:#4a5668; font-size:.82em; }
 .dim { opacity:.28; }

 /* Joints grid */
 .joints { display:grid; grid-template-columns:repeat(6,1fr); gap:3px; margin-top:.5em; }
 .joint { background:#101418; padding:.35em .2em; border-radius:4px; text-align:center; }
 .joint .n { color:#5590b8; font-size:.75em; }
 .joint .v { font-variant-numeric:tabular-nums; font-size:.88em; }

 /* E-stop */
 .estop { margin-top:.9em; padding:.9em 1em; background:#1a1f25;
          border:1px solid #29303a; border-radius:6px; text-align:center; }
 .estop-btn {
   width:100%; padding:1.1em; font-size:1.7em; font-weight:900;
   letter-spacing:.18em; border:3px solid #5a1010; border-radius:8px;
   background:#b81818; color:#fff; cursor:pointer;
   transition:background .08s, transform .04s; text-shadow:0 1px 0 #000;
 }
 .estop-btn:hover { background:#d62020; }
 .estop-btn:active { transform:translateY(1px); }
 .estop-btn:disabled { background:#3a4148; border-color:#252b30;
                       color:#7a8a98; cursor:default; }
 .resume-btn { margin-top:.5em; padding:.6em 1.2em; font-size:.95em;
               font-weight:700; border:1px solid #2a2f36; background:#1a3322;
               color:#b8e8c4; border-radius:6px; cursor:pointer; }
 .resume-btn:hover { background:#225530; color:#fff; }
 .estop-status { margin-top:.5em; font-size:.85em; color:#4a5668; }

 /* Banners */
 .banner-estop { position:sticky; top:0; margin:0 -1em .7em -1em;
                 padding:.55em 1em; background:#9c1414; color:#fff;
                 font-weight:700; text-align:center;
                 border-bottom:2px solid #5a1010; letter-spacing:.07em; }
 .hidden { display:none; }
 .good { color:#7fe09a; } .bad { color:#ff7070; }
</style></head><body>

<div id="estop_banner" class="banner-estop hidden">E-STOP ENGAGED — outbound commands zeroed</div>
<h1>capra teleop &mdash; <span id="status">connecting&hellip;</span></h1>

<div class="strategy-bar">
  <button id="btn_base_control" class="strat-btn" onclick="switch_strategy('base_control')">
    Base Control
  </button>
  <button id="btn_arm_control" class="strat-btn" onclick="switch_strategy('arm_control')">
    Arm Control
  </button>
</div>

<div class="grid">

  <!-- LEFT: sent commands -->
  <div class="card">
    <div class="card-title">Sent Commands</div>

    <div id="sec_drive_label" class="sec-label">Drive</div>
    <div id="sec_drive">
      <div class="row">
        <span class="label">tracks L / R</span>
        <span class="val" id="tracks">—</span>
      </div>
      <div class="row">
        <span class="label">flippers fl / fr / rl / rr</span>
        <span class="val" id="flippers">—</span>
      </div>
    </div>

    <div id="sec_arm_label" class="sec-label">Arm</div>
    <div id="sec_arm">
      <div class="row">
        <span class="label">pos x / y / z</span>
        <span class="val" id="ovis_pos">—</span>
      </div>
      <div class="row">
        <span class="label">ori yaw / pitch / roll</span>
        <span class="val" id="ovis_ori">—</span>
      </div>
      <div class="row">
        <span class="label">gripper</span>
        <span class="val" id="gripper">—</span>
      </div>
    </div>

    <div class="row small" style="margin-top:.5em;">
      <span>sent #</span><span id="sent_count">0</span>
    </div>
  </div>

  <!-- RIGHT: telemetry -->
  <div class="card">
    <div class="card-title">Telemetry</div>
    <div class="row">
      <span class="label">timestamp</span>
      <span class="val small" id="rx_ts">—</span>
    </div>
    <div class="row">
      <span class="label">machine state</span>
      <span class="val" id="rx_state">—</span>
    </div>
    <div class="joints" id="joints"></div>
    <div class="row small" style="margin-top:.5em;">
      <span>received #</span><span id="rx_count">0</span>
    </div>
  </div>

</div>

<div class="estop">
  <button id="estop_btn" class="estop-btn" onclick="trigger_estop()">E-STOP</button>
  <button id="resume_btn" class="resume-btn hidden" onclick="trigger_resume()">RESUME</button>
  <div id="estop_status" class="estop-status">click to halt all outbound commands</div>
</div>

<script>
let _strategy = '';

function set_active_strategy(name) {
  if (name === _strategy) return;
  _strategy = name;
  document.querySelectorAll('.strat-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('btn_' + name);
  if (btn) btn.classList.add('active');
  const is_base = (name === 'base_control');
  ['sec_drive', 'sec_drive_label'].forEach(id => {
    document.getElementById(id).classList.toggle('dim', !is_base);
  });
  ['sec_arm', 'sec_arm_label'].forEach(id => {
    document.getElementById(id).classList.toggle('dim', is_base);
  });
}

async function switch_strategy(name) {
  try {
    const r = await fetch('/strategy', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name}),
    });
    if (!r.ok) { const j = await r.json(); throw new Error(j.error || r.status); }
    set_active_strategy(name);
  } catch(e) {
    console.error('strategy switch failed:', e);
  }
}

async function tick() {
  try {
    const r = await fetch('/state');
    if (!r.ok) throw new Error(r.status);
    const j = await r.json();
    document.getElementById('status').innerHTML = '<span class="good">live</span>';
    apply_estop(j.estopped);
    if (j.strategy) set_active_strategy(j.strategy);
    if (j.sent) {
      const s = j.sent;
      document.getElementById('tracks').textContent =
        fmt(s.tracks_left) + ' / ' + fmt(s.tracks_right);
      document.getElementById('flippers').textContent =
        s.flippers.fl + ' / ' + s.flippers.fr + ' / ' + s.flippers.rl + ' / ' + s.flippers.rr;
      document.getElementById('ovis_pos').textContent =
        fmt(s.ovis.x) + ' / ' + fmt(s.ovis.y) + ' / ' + fmt(s.ovis.z);
      document.getElementById('ovis_ori').textContent =
        fmt(s.ovis.yaw) + ' / ' + fmt(s.ovis.pitch) + ' / ' + fmt(s.ovis.roll);
      document.getElementById('gripper').textContent = s.gripper_open ? 'OPEN' : 'CLOSED';
      document.getElementById('sent_count').textContent = j.sent_count;
    }
    document.getElementById('rx_count').textContent = j.rx_count;
    if (j.telemetry) {
      const t = j.telemetry;
      document.getElementById('rx_ts').textContent = t.timestamp_us;
      const STATE_NAMES = {0:'idle', 1:'running', 2:'error'};
      document.getElementById('rx_state').textContent =
        STATE_NAMES[t.machine_state] !== undefined ? STATE_NAMES[t.machine_state] : t.machine_state;
      document.getElementById('joints').innerHTML = t.joints.map((jt, i) =>
        '<div class="joint">' +
        '<div class="n">J' + (i+1) + '</div>' +
        '<div class="v">' + fmt(jt.pos) + '&deg;</div>' +
        '<div class="v small">' + fmt(jt.amp) + ' A</div>' +
        '<div class="v small">' + fmt(jt.temp) + ' &deg;C</div>' +
        '</div>'
      ).join('');
    }
  } catch(e) {
    document.getElementById('status').innerHTML = '<span class="bad">offline (' + e + ')</span>';
  }
}

function apply_estop(is_estopped) {
  const banner = document.getElementById('estop_banner');
  const btn    = document.getElementById('estop_btn');
  const resume = document.getElementById('resume_btn');
  const stat   = document.getElementById('estop_status');
  if (is_estopped) {
    banner.classList.remove('hidden');
    btn.disabled = true; btn.textContent = 'STOPPED';
    resume.classList.remove('hidden');
    stat.textContent = 'outbound zeroed \xb7 ODrives & arm estopped';
  } else {
    banner.classList.add('hidden');
    btn.disabled = false; btn.textContent = 'E-STOP';
    resume.classList.add('hidden');
    stat.textContent = 'click to halt all outbound commands';
  }
}

async function trigger_estop() {
  try {
    const r = await fetch('/estop', {method:'POST'});
    const j = await r.json();
    apply_estop(true);
    document.getElementById('estop_status').textContent = j.api_status || 'engaged';
  } catch(e) {
    document.getElementById('estop_status').textContent = 'estop POST failed: ' + e;
  }
}

async function trigger_resume() {
  try {
    await fetch('/resume', {method:'POST'});
    apply_estop(false);
  } catch(e) {
    document.getElementById('estop_status').textContent = 'resume POST failed: ' + e;
  }
}

function fmt(v) { return (v == null) ? '—' : (+v).toFixed(2); }

// Space-bar triggers E-STOP for quick muscle memory.
document.addEventListener('keydown', e => {
  if (e.key === ' ' || e.code === 'Space') { e.preventDefault(); trigger_estop(); }
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
        self._strategy_name = "base_control"

    def set_strategy_name(self, name: str) -> None:
        with self._lock:
            self._strategy_name = name

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
            strategy_name = self._strategy_name
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
            "strategy": strategy_name,
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


def _post_one(url: str, body: Optional[bytes] = None) -> tuple[str, str]:
    """POST to *url* with an optional JSON *body*. Returns (url, status_str).

    Best-effort: never raises — all errors are returned as status strings so
    the caller can collect results without try/except soup.
    """
    headers: dict[str, str] = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return url, f"{resp.status}"
    except urllib.error.URLError as e:
        reason = e.reason if hasattr(e, "reason") else str(e)
        log.warning("E-stop POST %s failed: %s", url, reason)
        return url, f"failed: {reason}"
    except Exception as e:
        log.warning("E-stop POST %s raised: %s", url, e)
        return url, f"error: {e}"


def post_estop_to_api(api_base_url: str) -> str:
    """Trigger estop on every sensor that supports it, plus zero the Kinova arm.

    Steps:
      1. GET /discover → find all sensors with has_estop=true.
      2. POST /{id}/estop to each of them concurrently (one thread per sensor).
      3. Also POST the Kinova arm zero-velocity / erase-trajectories command
         (belt-and-suspenders for the arm, which may not be in /discover yet).

    All calls are best-effort with a 1.5 s per-request timeout.  A failed POST
    is logged but never gates the local outbound zeroing, which has already
    happened before this function is called.
    """
    if not api_base_url:
        return "no api_base_url configured"

    base = api_base_url.rstrip("/")
    results: list[tuple[str, str]] = []

    # --- 1. Discover sensors and collect estop URLs ---
    estop_urls: list[str] = []
    try:
        with urllib.request.urlopen(f"{base}/discover", timeout=1.5) as resp:
            data = json.loads(resp.read())
        sensor_list = data.get("sensors", data) if isinstance(data, dict) else data
        for s in sensor_list:
            # estop support indicated by endpoints.estop being non-null
            if s.get("endpoints", {}).get("estop"):
                estop_urls.append(f"{base}/{s['id']}/estop")
    except Exception as e:
        log.warning("E-stop /discover failed: %s — falling back to direct estop URLs", e)
        # Fall back: hit the ODrive estop endpoints directly using the known node IDs.
        for nid in (31, 32, 33, 34):
            estop_urls.append(f"{base}/odrive_{nid}/estop")

    # --- 2. Fire all estop POSTs in parallel ---
    threads: list[threading.Thread] = []
    lock = threading.Lock()

    def _fire(url: str) -> None:
        r = _post_one(url)
        with lock:
            results.append(r)

    for url in estop_urls:
        t = threading.Thread(target=_fire, args=(url,), daemon=True)
        t.start()
        threads.append(t)

    # --- 3. Also erase Kinova arm trajectories ---
    kinova_url = f"{base}/kinova_arm/command"
    kinova_body = json.dumps({
        "erase_trajectories": True,
        "joint_1_vel": 0.0, "joint_2_vel": 0.0, "joint_3_vel": 0.0,
        "joint_4_vel": 0.0, "joint_5_vel": 0.0, "joint_6_vel": 0.0,
    }).encode("utf-8")
    kt = threading.Thread(
        target=lambda: results.append(_post_one(kinova_url, kinova_body)),
        daemon=True,
    )
    kt.start()
    threads.append(kt)

    for t in threads:
        t.join(timeout=2.0)

    ok = sum(1 for _, s in results if s.isdigit() or s.startswith("2"))
    summary = f"{ok}/{len(results)} estop calls ok"
    log.warning("E-stop API results: %s", results)
    return summary


class TeleopHttpServer:
    """ThreadingHTTPServer wrapper running the UI in a daemon thread."""

    def __init__(
        self,
        state: TeleopState,
        receiver: Optional[UdpTelemetryReceiver],
        host: str = "127.0.0.1",
        port: int = 8765,
        api_base_url: str = "",
        strategy_switcher=None,
    ) -> None:
        self._state = state
        self._receiver = receiver
        self._host = host
        self._port = port
        self._api_base_url = api_base_url
        self._strategy_switcher = strategy_switcher
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        state = self._state
        receiver = self._receiver
        api_base_url = self._api_base_url
        strategy_switcher = self._strategy_switcher

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
                elif self.path == "/strategy":
                    length = int(self.headers.get("Content-Length", 0))
                    try:
                        body = json.loads(self.rfile.read(length))
                        name = str(body.get("name", ""))
                        if not name:
                            raise ValueError("missing 'name'")
                        if strategy_switcher is None:
                            raise RuntimeError("strategy switching not wired")
                        strategy_switcher(name)
                        log.info("Strategy switched to %r via UI", name)
                        _json_response(self, {"strategy": name})
                    except Exception as exc:
                        _json_response(self, {"error": str(exc)}, status=400)
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

    All disk I/O is handled by a single background thread via a queue so
    the control-loop thread (50 Hz) never blocks on file writes or flushes.
    Rows are dropped silently if the queue fills up rather than ever stalling
    the sender.
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

    _FLUSH_EVERY = 50   # flush to disk once every N rows per file

    def __init__(self, log_dir: Path) -> None:
        self._dir = log_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._sent_path = self._dir / f"sent_{stamp}.csv"
        self._recv_path = self._dir / f"recv_{stamp}.csv"

        self._sent_f = self._sent_path.open("w", newline="")
        self._recv_f = self._recv_path.open("w", newline="")
        self._sent_w = csv.writer(self._sent_f)
        self._recv_w = csv.writer(self._recv_f)
        self._sent_w.writerow(self.SENT_COLS)
        self._recv_w.writerow(self.RECV_COLS)

        # Background writer: items are ("sent"|"recv", row) or None (sentinel).
        self._q: queue.Queue = queue.Queue(maxsize=2000)
        self._writer = threading.Thread(
            target=self._write_loop, name="CsvWriter", daemon=True
        )
        self._writer.start()
        log.info("CSV logs: sent=%s recv=%s", self._sent_path, self._recv_path)

    def _write_loop(self) -> None:
        sent_n = recv_n = 0
        while True:
            item = self._q.get()
            if item is None:
                break
            kind, row = item
            if kind == "sent":
                self._sent_w.writerow(row)
                sent_n += 1
                if sent_n % self._FLUSH_EVERY == 0:
                    self._sent_f.flush()
            else:
                self._recv_w.writerow(row)
                recv_n += 1
                if recv_n % self._FLUSH_EVERY == 0:
                    self._recv_f.flush()

    def log_sent(self, msg) -> None:
        row = [
            int(msg.timestamp_us),
            f"{msg.tracks.left_vel:.4f}", f"{msg.tracks.right_vel:.4f}",
            msg.flippers.fl, msg.flippers.fr, msg.flippers.rl, msg.flippers.rr,
            f"{msg.ovis.position.x:.4f}", f"{msg.ovis.position.y:.4f}",
            f"{msg.ovis.position.z:.4f}",
            f"{msg.ovis.orientation.yaw:.4f}", f"{msg.ovis.orientation.pitch:.4f}",
            f"{msg.ovis.orientation.roll:.4f}",
            int(bool(msg.gripper.open_state)),
        ]
        try:
            self._q.put_nowait(("sent", row))
        except queue.Full:
            pass  # drop rather than block the control loop

    def log_recv(self, telemetry) -> None:
        row = [int(telemetry.timestamp_us), int(telemetry.machine_state)]
        for i in range(1, 7):
            a = getattr(telemetry.ovis, f"act_{i}")
            row.extend([
                f"{a.motor_pos:.4f}",
                f"{0.0:.4f}",
                f"{a.motor_amp:.4f}",
                f"{a.motor_temp_c:.4f}",
            ])
        try:
            self._q.put_nowait(("recv", row))
        except queue.Full:
            pass

    def close(self) -> None:
        self._q.put(None)          # signal writer to exit
        self._writer.join(timeout=2.0)
        try:
            self._sent_f.flush()
            self._sent_f.close()
        finally:
            self._recv_f.flush()
            self._recv_f.close()
