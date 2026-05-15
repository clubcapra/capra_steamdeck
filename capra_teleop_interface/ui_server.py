"""Stdlib HTTP server backing the operator UI (Blueprint.js SPA in ui/).

Endpoints:
    GET  /            → static index.html from ui/dist/
    GET  /assets/*    → static asset (built by Vite)
    GET  /state       → JSON snapshot: sent RoveControl, received RoveTelemetry,
                        packet counters, strategy, estop, control_active.
    POST /estop       → engage emergency stop (zeroes outbound + erases arm trajectories).
    POST /resume      → clear estop.
    POST /strategy    → {"name": "..."} switch the live ConversionStrategy.

    POST /api/control/active     → {"active": bool} flip the send-gate
    GET  /api/sensors/discover   → proxy rove_sensor_api /discover
    GET  /api/sensors/<id>/info  → proxy rove_sensor_api /<id>/info
    POST /api/sensors/subscribe  → {"ids": [...]} start/stop UDP subscribers
    GET  /api/sensors/state      → latest values per active subscriber
    GET  /api/ik/collision       → proxy rove_ik_engine GET /api/v1/ik/collision
    POST /api/ik/collision       → proxy rove_ik_engine POST /api/v1/ik/collision

The sensor proxy keeps one UDP subscriber thread per sensor the Data tab
has asked for, mirroring rove_sensor_api/tools/sensor_dashboard.py. On
tab leave the React app POSTs an empty subscription set and all threads
exit.

Also writes every sent/received frame to a per-session CSV log so a
runaway control session can be replayed offline.
"""
from __future__ import annotations

import collections
import csv
import json
import logging
import queue
import socket
import struct
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



class TeleopState:
    """Thread-safe snapshot the HTTP handler reads from."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sent: Optional[dict] = None
        self._sent_count = 0
        self._estopped = False
        self._strategy_name = "arcade_drive"
        # Sends are gated: only the Control tab in the UI flips this to True.
        # Default False so a freshly-launched UI on the Settings tab can't
        # accidentally fire commands. The controller loop checks this each tick.
        self._control_active = False
        # Stuck-detection haptic: vectornav-driven rumble when tracks are
        # commanded but the IMU sees no movement (linear OR rotational).
        # Default True so a fresh boot picks it up; flip from Settings tab.
        self._stuck_haptic_enabled = True

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
            "gripper_position": msg.gripper.position,
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

    def set_control_active(self, value: bool) -> None:
        with self._lock:
            self._control_active = bool(value)

    def is_control_active(self) -> bool:
        with self._lock:
            return self._control_active

    def set_stuck_haptic_enabled(self, value: bool) -> None:
        with self._lock:
            self._stuck_haptic_enabled = bool(value)

    def is_stuck_haptic_enabled(self) -> bool:
        with self._lock:
            return self._stuck_haptic_enabled

    def latest_commanded_tracks(self) -> tuple[float, float]:
        """Read-only view of the last commanded (left, right) track velocities.

        Used by the stuck detector to decide whether the operator is asking
        the robot to move. Returns (0.0, 0.0) before the first frame.
        """
        with self._lock:
            if self._sent is None:
                return 0.0, 0.0
            return (
                float(self._sent.get("tracks_left", 0.0)),
                float(self._sent.get("tracks_right", 0.0)),
            )

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
        with self._lock:
            control_active = self._control_active
            stuck_haptic = self._stuck_haptic_enabled
        return {
            "sent": sent,
            "sent_count": sent_count,
            "telemetry": telemetry,
            "rx_count": rx_count,
            "estopped": estopped,
            "strategy": strategy_name,
            "control_active": control_active,
            "stuck_haptic_enabled": stuck_haptic,
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
    # gripper.position is latched state, not a velocity — leave it.


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


# ----------------------------------------------------------------------------
# Sensor proxy pool (Data tab)
# ----------------------------------------------------------------------------

# Mirrors rove_sensor_api's wire format: 4-byte header + JSON body.
_SP_PROTOCOL_VERSION = 0x01
_SP_MSG_SUBSCRIBE = 0x01
_SP_MSG_UNSUBSCRIBE = 0x02
_SP_MSG_DATA = 0x03
_SP_HEADER_FMT = "<BBH"


def _sp_encode(mt: int, seq: int, payload: Optional[dict]) -> bytes:
    body = json.dumps(payload).encode() if payload is not None else b""
    return struct.pack(_SP_HEADER_FMT, _SP_PROTOCOL_VERSION, mt, seq & 0xFFFF) + body


def _sp_decode(data: bytes) -> tuple[int, int, Optional[dict]]:
    if len(data) < 4:
        raise ValueError("short")
    ver, mt, seq = struct.unpack(_SP_HEADER_FMT, data[:4])
    if ver != _SP_PROTOCOL_VERSION:
        raise ValueError(f"bad protocol version {ver}")
    return mt, seq, (json.loads(data[4:]) if data[4:] else None)


class _SensorSubscriber:
    """One UDP socket per sensor: SUBSCRIBE, drain DATA frames, UNSUBSCRIBE.

    Lifecycle is owned by ``SensorProxyPool``; the React app's tab effects
    flip subscriptions on enter / leave so we're not holding open sockets
    in the background while the operator is on Control or Settings.
    """

    def __init__(self, host: str, summary: dict, interval_ms: int = 100) -> None:
        self.id = summary["id"]
        self.display_name = summary.get("display_name", self.id)
        self.host = host
        self.data_port = int(summary["data_port"])
        self.interval_ms = interval_ms
        self.lock = threading.Lock()
        self.latest: dict = {}
        self.packets = 0
        self.last_packet_mono: Optional[float] = None
        self.last_error: Optional[str] = None
        self.recv_times: collections.deque = collections.deque(maxlen=200)
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"SensorSub-{self.id}", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.sendto(
                    _sp_encode(_SP_MSG_UNSUBSCRIBE, 0, None),
                    (self.host, self.data_port),
                )
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _run(self) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.settimeout(0.5)
            self._sock.sendto(
                _sp_encode(_SP_MSG_SUBSCRIBE, 0, {"interval_ms": self.interval_ms}),
                (self.host, self.data_port),
            )
        except Exception as e:
            with self.lock:
                self.last_error = f"subscribe failed: {e}"
            return

        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(8192)
            except socket.timeout:
                continue
            except Exception as e:
                with self.lock:
                    self.last_error = f"recv failed: {e}"
                break
            try:
                mt, _seq, body = _sp_decode(data)
            except Exception as e:
                with self.lock:
                    self.last_error = f"decode failed: {e}"
                continue
            if mt != _SP_MSG_DATA or not isinstance(body, dict):
                continue
            now = time.monotonic()
            with self.lock:
                self.latest = body
                self.packets += 1
                self.last_packet_mono = now
                self.recv_times.append(now)

    def snapshot(self) -> dict:
        with self.lock:
            age = (
                None if self.last_packet_mono is None
                else max(0.0, time.monotonic() - self.last_packet_mono)
            )
            # Packet rate = packets in the last second window from the deque.
            now = time.monotonic()
            recent = [t for t in self.recv_times if now - t <= 1.0]
            rate_hz = float(len(recent)) if recent else None
            return {
                "id": self.id,
                "display_name": self.display_name,
                "packets": self.packets,
                "last_packet_age_s": age,
                "rate_hz": rate_hz,
                "last_error": self.last_error,
                "latest": dict(self.latest),
            }


class SensorProxyPool:
    """Tracks the set of currently-subscribed sensors and serves snapshots.

    Single-threaded API (called from HTTP handler threads, guarded by lock).
    """

    def __init__(self, api_base_url: str, sensor_api_host_override: str = "") -> None:
        self._api_base_url = api_base_url.rstrip("/")
        # The sensor_api HTTP URL might be a hostname the browser can reach,
        # but the UDP data port lives on the rover. If the operator wants to
        # override the UDP host (e.g. behind a tunnel), this is the lever.
        self._udp_host = sensor_api_host_override or _hostname_from_url(api_base_url)
        self._lock = threading.Lock()
        self._subs: dict[str, _SensorSubscriber] = {}
        self._summaries: dict[str, dict] = {}

    def discover(self) -> list[dict]:
        """Hit sensor_api /discover and cache summaries keyed by id."""
        if not self._api_base_url:
            return []
        url = f"{self._api_base_url}/discover"
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            data = json.loads(resp.read())
        sensors = data.get("sensors", data) if isinstance(data, dict) else data
        with self._lock:
            self._summaries = {s["id"]: s for s in sensors if "id" in s}
        return sensors

    def get_info(self, sensor_id: str) -> dict:
        url = f"{self._api_base_url}/{sensor_id}/info"
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            return json.loads(resp.read())

    def set_subscriptions(self, ids: list[str]) -> list[str]:
        """Diff the requested set against the live one; start/stop subscribers
        so only the requested ids are subscribed. Returns the resulting set."""
        want = set(ids)
        with self._lock:
            have = set(self._subs)
            to_stop = have - want
            to_start = want - have
            for sid in to_stop:
                self._subs[sid].stop()
                self._subs.pop(sid, None)
            for sid in to_start:
                summary = self._summaries.get(sid)
                if summary is None:
                    # Caller asked for an id we haven't discovered yet.
                    continue
                sub = _SensorSubscriber(self._udp_host, summary)
                sub.start()
                self._subs[sid] = sub
            return sorted(self._subs)

    def snapshot(self) -> list[dict]:
        with self._lock:
            subs = list(self._subs.values())
        return [s.snapshot() for s in subs]

    def stop_all(self) -> None:
        with self._lock:
            subs = list(self._subs.values())
            self._subs.clear()
        for s in subs:
            s.stop()


def _hostname_from_url(url: str) -> str:
    """Extract the hostname from http://host:port — used for the UDP target."""
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname or "127.0.0.1"
    except Exception:
        return "127.0.0.1"


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
        ui_dir: Optional[Path] = None,
        ik_engine_url: str = "",
        sensor_pool: Optional[SensorProxyPool] = None,
        stuck_haptic_setter=None,
    ) -> None:
        self._state = state
        self._receiver = receiver
        self._host = host
        self._port = port
        self._api_base_url = api_base_url
        self._strategy_switcher = strategy_switcher
        self._ui_dir = ui_dir
        self._ik_engine_url = ik_engine_url.rstrip("/")
        self._sensor_pool = sensor_pool
        # Called when the Settings tab flips the stuck-haptic switch, so the
        # backend can also start/stop the underlying vectornav subscriber.
        # Optional — without it the flag only affects what the controller
        # loop does with already-flowing data.
        self._stuck_haptic_setter = stuck_haptic_setter
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        state = self._state
        receiver = self._receiver
        api_base_url = self._api_base_url
        strategy_switcher = self._strategy_switcher
        ui_dir = self._ui_dir
        ik_engine_url = self._ik_engine_url
        sensor_pool = self._sensor_pool
        stuck_haptic_setter = self._stuck_haptic_setter

        def _json_response(handler, body: dict, status: int = 200) -> None:
            payload = json.dumps(body).encode("utf-8")
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)

        def _read_json(handler) -> dict:
            length = int(handler.headers.get("Content-Length", 0))
            return json.loads(handler.rfile.read(length)) if length > 0 else {}

        # MIME table for the static asset server. Anything not listed gets
        # application/octet-stream — fine for the things Vite emits.
        _MIME = {
            ".html": "text/html; charset=utf-8",
            ".js":   "application/javascript",
            ".mjs":  "application/javascript",
            ".css":  "text/css",
            ".json": "application/json",
            ".svg":  "image/svg+xml",
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".woff": "font-woff",
            ".woff2": "font/woff2",
            ".ico":  "image/x-icon",
            ".map":  "application/json",
        }

        def _serve_static(handler, rel_path: str) -> bool:
            """Try to serve a file from ui_dir; return False on 404."""
            if ui_dir is None:
                return False
            # Normalise + reject traversal attempts.
            rel = rel_path.lstrip("/")
            target = (ui_dir / rel).resolve()
            try:
                target.relative_to(ui_dir.resolve())
            except ValueError:
                return False
            if not target.is_file():
                return False
            body = target.read_bytes()
            handler.send_response(200)
            handler.send_header(
                "Content-Type",
                _MIME.get(target.suffix.lower(), "application/octet-stream"),
            )
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return True

        def _proxy_ik(method: str, body: Optional[bytes] = None) -> tuple[int, bytes]:
            """Forward to rove_ik_engine and return (status, body)."""
            if not ik_engine_url:
                return 503, json.dumps(
                    {"error": "ik_engine_url not configured in steamdeck config"}
                ).encode()
            url = f"{ik_engine_url}/api/v1/ik/collision"
            req = urllib.request.Request(url, data=body, method=method)
            if body is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read() or b""
            except Exception as e:
                return 502, json.dumps({"error": f"ik engine unreachable: {e}"}).encode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args, **_kw):
                return  # we have our own logger

            # ---- GET ----------------------------------------------------
            def do_GET(self):
                p = self.path.split("?", 1)[0]
                if p == "/state":
                    _json_response(self, state.snapshot(receiver))
                    return
                if p == "/api/sensors/discover":
                    if sensor_pool is None:
                        _json_response(self, {"sensors": []})
                        return
                    try:
                        _json_response(self, {"sensors": sensor_pool.discover()})
                    except Exception as exc:
                        _json_response(self, {"error": str(exc)}, status=502)
                    return
                if p.startswith("/api/sensors/") and p.endswith("/info"):
                    sid = p[len("/api/sensors/"): -len("/info")]
                    if sensor_pool is None:
                        _json_response(self, {"error": "sensor pool disabled"}, status=503)
                        return
                    try:
                        _json_response(self, sensor_pool.get_info(sid))
                    except Exception as exc:
                        _json_response(self, {"error": str(exc)}, status=502)
                    return
                if p == "/api/sensors/state":
                    if sensor_pool is None:
                        _json_response(self, {"sensors": []})
                        return
                    _json_response(self, {"sensors": sensor_pool.snapshot()})
                    return
                if p == "/api/ik/collision":
                    code, raw = _proxy_ik("GET")
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if p == "/api/haptics/stuck":
                    _json_response(self, {"enabled": state.is_stuck_haptic_enabled()})
                    return

                # Static UI: / → index.html, /assets/* → file.
                if p == "/" or p == "/index.html":
                    if _serve_static(self, "index.html"):
                        return
                else:
                    if _serve_static(self, p):
                        return
                self.send_response(404)
                self.end_headers()

            # ---- POST ---------------------------------------------------
            def do_POST(self):
                p = self.path
                if p == "/estop":
                    state.set_estop(True)
                    api_status = post_estop_to_api(api_base_url)
                    log.warning("E-STOP engaged via UI (%s)", api_status)
                    _json_response(self, {"estopped": True, "api_status": api_status})
                    return
                if p == "/resume":
                    state.set_estop(False)
                    log.warning("E-STOP cleared via UI")
                    _json_response(self, {"estopped": False})
                    return
                if p == "/strategy":
                    try:
                        body = _read_json(self)
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
                    return
                if p == "/api/control/active":
                    try:
                        body = _read_json(self)
                        active = bool(body.get("active"))
                        state.set_control_active(active)
                        log.info("control_active = %s", active)
                        _json_response(self, {"active": active})
                    except Exception as exc:
                        _json_response(self, {"error": str(exc)}, status=400)
                    return
                if p == "/api/sensors/subscribe":
                    if sensor_pool is None:
                        _json_response(self, {"error": "sensor pool disabled"}, status=503)
                        return
                    try:
                        body = _read_json(self)
                        ids = list(body.get("ids", []))
                        # discover() must have been called to populate the
                        # cache; the React app does that on tab entry.
                        # Refresh in case operator hot-plugs sensors.
                        try:
                            sensor_pool.discover()
                        except Exception:
                            pass
                        subscribed = sensor_pool.set_subscriptions(ids)
                        _json_response(self, {"subscribed": subscribed})
                    except Exception as exc:
                        _json_response(self, {"error": str(exc)}, status=400)
                    return
                if p == "/api/ik/collision":
                    raw_body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    code, raw = _proxy_ik("POST", raw_body or b"{}")
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if p == "/api/haptics/stuck":
                    try:
                        body = _read_json(self)
                        enabled = bool(body.get("enabled"))
                        state.set_stuck_haptic_enabled(enabled)
                        if stuck_haptic_setter is not None:
                            stuck_haptic_setter(enabled)
                        log.info("stuck_haptic_enabled = %s", enabled)
                        _json_response(self, {"enabled": enabled})
                    except Exception as exc:
                        _json_response(self, {"error": str(exc)}, status=400)
                    return
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
        if self._sensor_pool is not None:
            self._sensor_pool.stop_all()


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
        "gripper_position",
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
            msg.gripper.position,
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
