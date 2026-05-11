"""CLI entry point: pick a controller, pick a strategy, start streaming.

Usage:
    python3 -m capra_teleop_interface --host 192.168.1.50 --port 5005 \
        --device steamdeck --strategy arcade

Run from the *parent* of ``capra_teleop_interface/`` (so Python can find
the package on sys.path). The ``__package__`` fixup below also lets
``python3 capra_teleop_interface/__main__.py …`` work from there.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

# Support both ``python3 -m capra_teleop_interface`` (where __package__
# is set and relative imports work) and ``python3
# capra_teleop_interface/__main__.py`` (where Python treats this as a
# top-level script with no parent package). In the latter case we put
# the package's parent on sys.path and reassign __package__ so the
# relative imports below resolve either way.
if __package__ in (None, ""):
    _here = os.path.dirname(os.path.abspath(__file__))
    _parent = os.path.dirname(_here)
    if _parent not in sys.path:
        sys.path.insert(0, _parent)
    __package__ = os.path.basename(_here)

from .controllers import (
    ControllerBase,
    SteamDeckController,
    XboxController,
)
from .network import (
    BindEndpoint,
    UdpEndpoint,
    UdpSender,
    UdpTelemetryReceiver,
    UdpTorqueReceiver,
)
from .strategies import (
    ArcadeArmStrategy,
    ControlStrategy,
    TankDriveStrategy,
)
from .ui_server import CsvLogger, TeleopHttpServer, TeleopState, zero_rove_control


DEVICES = {
    "xbox": XboxController,
    "steamdeck": SteamDeckController,
}

STRATEGIES = {
    "tank": TankDriveStrategy,
    "arcade": ArcadeArmStrategy,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rove control interface")
    p.add_argument("--host", required=True, help="Rover IP or hostname")
    p.add_argument("--port", type=int, required=True, help="Rover UDP port")
    p.add_argument(
        "--device",
        choices=DEVICES.keys(),
        default="xbox",
        help="Controller backend (default: xbox)",
    )
    p.add_argument(
        "--strategy",
        choices=STRATEGIES.keys(),
        default="tank",
        help="Control strategy (default: tank)",
    )
    p.add_argument(
        "--rate", type=float, default=50.0, help="Polling rate in Hz (default: 50)"
    )
    p.add_argument(
        "--device-index",
        type=int,
        default=0,
        help="Joystick index if multiple are connected",
    )
    p.add_argument(
        "--no-haptics", action="store_true", help="Disable rumble feedback"
    )
    p.add_argument(
        "--torque-listen-host",
        default=None,
        help=(
            "Bind address for inbound torque feedback "
            "(default: disabled; e.g. 0.0.0.0)"
        ),
    )
    p.add_argument(
        "--torque-listen-port",
        type=int,
        default=5006,
        help="UDP port for inbound torque feedback (default: 5006)",
    )
    p.add_argument(
        "--torque-max",
        type=float,
        default=50.0,
        help="Torque (Nm) that maps to full-scale rumble (default: 50.0)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    p.add_argument(
        "--print-frames",
        action="store_true",
        help="Print a one-line summary of each RoveControl frame as it's sent",
    )
    p.add_argument(
        "--debug-input",
        action="store_true",
        help="Print the raw ControllerInput ~1x/sec (sticks, triggers, buttons)",
    )
    p.add_argument(
        "--probe",
        action="store_true",
        help=(
            "Log every raw button press/release and axis change with its "
            "pygame index. Used to discover device-specific mappings."
        ),
    )
    p.add_argument(
        "--telemetry-listen-host",
        default="0.0.0.0",
        help="Bind address for inbound RoveTelemetry (default: 0.0.0.0).",
    )
    p.add_argument(
        "--telemetry-listen-port",
        type=int,
        default=7001,
        help="UDP port the rover pushes RoveTelemetry to (default: 7001).",
    )
    p.add_argument(
        "--no-ui",
        action="store_true",
        help="Disable the local web UI / telemetry pipeline.",
    )
    p.add_argument(
        "--ui-host",
        default="127.0.0.1",
        help="Bind address for the operator UI (default: 127.0.0.1).",
    )
    p.add_argument(
        "--ui-port",
        type=int,
        default=8765,
        help="HTTP port for the operator UI (default: 8765).",
    )
    p.add_argument(
        "--log-dir",
        type=Path,
        default=Path("logs"),
        help="Directory for sent/received CSV logs (default: ./logs).",
    )
    p.add_argument(
        "--no-log",
        action="store_true",
        help="Don't write CSV logs.",
    )
    p.add_argument(
        "--api-base-url",
        default="http://192.168.2.2:8080",
        help=(
            "rove_sensor_api base URL used by the UI E-stop button "
            "(default: http://192.168.2.2:8080). Set to '' to disable the "
            "direct api call (zeroed outbound commands still happen)."
        ),
    )
    return p.parse_args(argv)


def _format_frame(msg) -> str:
    t = msg.tracks
    o = msg.ovis
    return (
        f"t={msg.timestamp_us:>16}  "
        f"tracks L={t.left_vel:+.2f} R={t.right_vel:+.2f}  "
        f"flip fl/fr/rl/rr="
        f"{msg.flippers.fl:+d}/{msg.flippers.fr:+d}/"
        f"{msg.flippers.rl:+d}/{msg.flippers.rr:+d}  "
        f"twist xyz=({o.position.x:+.2f},{o.position.y:+.2f},{o.position.z:+.2f}) "
        f"ypr=({o.orientation.yaw:+.2f},{o.orientation.pitch:+.2f},{o.orientation.roll:+.2f})  "
        f"grip={'O' if msg.gripper.open_state else 'C'}"
    )


def _attach_probe(controller: ControllerBase) -> None:
    """Log every raw pygame button/hat/axis transition with its index.

    Bypasses the phantom filter and index maps entirely so we can discover
    what indices the kernel driver actually exposes (e.g. where Steam Deck
    back grips land on hid-steam without Steam Input remapping).
    """
    original = controller._read_input
    prev_buttons: dict[int, bool] = {}
    prev_hat: list[tuple[int, int]] = [(0, 0)]
    prev_axes: dict[int, float] = {}
    printed_header = [False]

    def read_and_probe():
        inp = original()
        joy = getattr(controller, "_joystick", None)
        if joy is None:
            return inp

        if not printed_header[0]:
            printed_header[0] = True
            try:
                print(
                    f"[probe] device={joy.get_name()!r} "
                    f"buttons={joy.get_numbuttons()} "
                    f"axes={joy.get_numaxes()} "
                    f"hats={joy.get_numhats()}",
                    flush=True,
                )
            except Exception:
                pass

        try:
            n_buttons = joy.get_numbuttons()
        except Exception:
            n_buttons = 0
        for i in range(n_buttons):
            try:
                state = bool(joy.get_button(i))
            except Exception:
                continue
            if state != prev_buttons.get(i, False):
                print(f"[probe] button {i:2d}: {'DOWN' if state else 'UP'}", flush=True)
                prev_buttons[i] = state

        try:
            n_hats = joy.get_numhats()
        except Exception:
            n_hats = 0
        if n_hats > 0:
            try:
                hat = joy.get_hat(0)
            except Exception:
                hat = (0, 0)
            if hat != prev_hat[0]:
                print(f"[probe] hat 0: {hat}", flush=True)
                prev_hat[0] = hat

        try:
            n_axes = joy.get_numaxes()
        except Exception:
            n_axes = 0
        for i in range(n_axes):
            try:
                v = float(joy.get_axis(i))
            except Exception:
                continue
            prev = prev_axes.get(i)
            # Print first value seen + any change > 0.3 from the last
            # reported value. Avoids drowning the log in drift.
            if prev is None or abs(v - prev) > 0.3:
                print(f"[probe] axis {i}: {v:+.2f}", flush=True)
                prev_axes[i] = v

        return inp

    controller._read_input = read_and_probe


def _tee_input(controller: ControllerBase) -> None:
    """Wrap _read_input so the raw snapshot prints roughly once per second."""
    original = controller._read_input
    last_print = 0.0

    def read_and_print():
        nonlocal last_print
        inp = original()
        now = time.monotonic()
        if inp is not None and (now - last_print) >= 1.0:
            last_print = now
            btns = sorted(b.name for b in inp.buttons) or ["-"]
            print(
                f"[input] L=({inp.left_x:+.2f},{inp.left_y:+.2f}) "
                f"R=({inp.right_x:+.2f},{inp.right_y:+.2f}) "
                f"LT={inp.left_trigger:.2f} RT={inp.right_trigger:.2f} "
                f"buttons={','.join(btns)} idle={inp.is_idle()}",
                flush=True,
            )
        return inp

    controller._read_input = read_and_print


def _tee_sender(sender: UdpSender) -> None:
    """Wrap sender.send so every successful send also prints a frame summary."""
    original_send = sender.send
    frame_count = 0

    def send_and_print(msg):
        nonlocal frame_count
        ok = original_send(msg)
        if ok:
            frame_count += 1
            print(f"[#{frame_count:05d} -> {sender.endpoint.host}:{sender.endpoint.port}] "
                  f"{_format_frame(msg)}", flush=True)
        return ok

    sender.send = send_and_print


def _install_sender_hooks(sender: UdpSender, pre_send_filters, observers) -> None:
    """Wrap ``sender.send`` with pre-send filters and post-send observers.

    Filters mutate the outgoing message before the wire write (used by the
    E-stop to zero outbound commands while keeping the heartbeat going).
    Observers run after a successful send so a slow logger never throttles
    the control loop's send rate.
    """
    original_send = sender.send
    _log = logging.getLogger(__name__)

    def send_filtered_and_observed(msg):
        for fn in pre_send_filters:
            try:
                fn(msg)
            except Exception as e:
                _log.debug("pre-send filter raised: %s", e)
        ok = original_send(msg)
        if ok:
            for fn in observers:
                try:
                    fn(msg)
                except Exception as e:
                    _log.debug("send observer raised: %s", e)
        return ok

    sender.send = send_filtered_and_observed


def build_controller(args: argparse.Namespace) -> ControllerBase:
    endpoint = UdpEndpoint(host=args.host, port=args.port)
    sender = UdpSender(endpoint)
    if args.print_frames:
        _tee_sender(sender)
    strategy: ControlStrategy = STRATEGIES[args.strategy]()
    device_cls = DEVICES[args.device]

    torque_receiver: UdpTorqueReceiver | None = None
    if args.torque_listen_host and not args.no_haptics:
        torque_receiver = UdpTorqueReceiver(
            endpoint=BindEndpoint(
                host=args.torque_listen_host,
                port=args.torque_listen_port,
            ),
            torque_max=args.torque_max,
        )

    controller = device_cls(
        sender=sender,
        strategy=strategy,
        rate_hz=args.rate,
        haptics_enabled=not args.no_haptics,
        device_index=args.device_index,
        torque_receiver=torque_receiver,
    )
    if args.debug_input:
        _tee_input(controller)
    if args.probe:
        _attach_probe(controller)
    return controller


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    controller = build_controller(args)

    # --- Observability: telemetry receiver, UI server, CSV logger ------
    state = TeleopState()
    csv_logger: CsvLogger | None = None
    if not args.no_log:
        csv_logger = CsvLogger(args.log_dir)

    receiver: UdpTelemetryReceiver | None = None
    if not args.no_ui:
        receiver = UdpTelemetryReceiver(
            BindEndpoint(args.telemetry_listen_host, args.telemetry_listen_port)
        )
        receiver.subscribe(lambda t: None)  # placeholder; state is read on demand
        if csv_logger is not None:
            receiver.subscribe(csv_logger.log_recv)
        receiver.start()

    # Tee the strategy → wire path so every sent frame updates state + logs.
    # The pre-send filter is what makes the E-stop button cut motion: it
    # mutates the outgoing message to all zeros whenever state.is_estopped()
    # is set, while still letting the heartbeat reach the rover.
    pre_send = [lambda msg, _s=state: zero_rove_control(msg) if _s.is_estopped() else None]
    observers = [state.on_sent]
    if csv_logger is not None:
        observers.append(csv_logger.log_sent)
    _install_sender_hooks(controller._sender, pre_send, observers)  # type: ignore[attr-defined]

    ui_server: TeleopHttpServer | None = None
    if not args.no_ui:
        ui_server = TeleopHttpServer(
            state,
            receiver,
            host=args.ui_host,
            port=args.ui_port,
            api_base_url=args.api_base_url,
        )
        ui_server.start()

    # Ctrl-C should exit the loop cleanly, not crash out mid-send.
    def handle_sigint(signum, frame):  # noqa: ARG001
        logging.info("SIGINT received, stopping...")
        controller.stop()

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    try:
        controller.run()
    except Exception:
        logging.exception("Controller loop crashed")
        return 1
    finally:
        if ui_server is not None:
            try:
                ui_server.stop()
            except Exception:
                pass
        if receiver is not None:
            try:
                receiver.stop()
            except Exception:
                pass
        if csv_logger is not None:
            try:
                csv_logger.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
