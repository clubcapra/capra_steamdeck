"""Listen for RoveControl UDP packets and validate them.

Binds to a UDP port, parses each datagram as a ``RoveControl`` protobuf,
and prints a one-line summary per frame plus any range-violation warnings.
On exit (Ctrl-C), prints the aggregate counts.

Usage:
    python validate_protos.py --port 5005
    python validate_protos.py --port 5005 --send-test    # also sends a canary

Point the interface at your local machine while testing:
    python -m control_interface --host 127.0.0.1 --port 5005 ...
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time

# Support running this file directly from anywhere. We now live in
# scripts/, so the package root is one level up and its parent (where
# ``capra_teleop_interface`` is importable from) is two levels up.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_HERE)
_PKG_PARENT = os.path.dirname(_PKG_ROOT)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)
# Expose ``proto.core`` as a top-level import path too, matching what the
# generated files reference internally.
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from capra_teleop_interface.proto.core import JointState_pb2, RoveControl_pb2


TRACK_MIN, TRACK_MAX = -1.0, 1.0
FLIPPER_POS_LIMIT_DEG = 360.0
ARM_POS_LIMIT_DEG = 720.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate incoming RoveControl UDP frames")
    p.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    p.add_argument("--port", type=int, default=5005, help="UDP port (default: 5005)")
    p.add_argument(
        "--send-test",
        action="store_true",
        help="Before listening, send one canary RoveControl to ourselves",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Only print warnings and the final summary, not every frame",
    )
    return p.parse_args()


def validate(msg: RoveControl_pb2.RoveControl) -> list[str]:
    """Return a list of human-readable range/shape warnings, empty if clean."""
    problems: list[str] = []

    if not (TRACK_MIN <= msg.tracks.left_vel <= TRACK_MAX):
        problems.append(f"tracks.left_vel out of range: {msg.tracks.left_vel}")
    if not (TRACK_MIN <= msg.tracks.right_vel <= TRACK_MAX):
        problems.append(f"tracks.right_vel out of range: {msg.tracks.right_vel}")

    flippers = [
        ("fl", msg.flippers.fl),
        ("fr", msg.flippers.fr),
        ("rl", msg.flippers.rl),
        ("rr", msg.flippers.rr),
    ]
    for name, joint in flippers:
        if abs(joint.pos_deg) > FLIPPER_POS_LIMIT_DEG:
            problems.append(f"flipper {name} pos_deg suspicious: {joint.pos_deg}")

    for i in range(1, 7):
        act: JointState_pb2.JointState = getattr(msg.ovis, f"act_{i}")
        if abs(act.pos_deg) > ARM_POS_LIMIT_DEG:
            problems.append(f"ovis.act_{i} pos_deg suspicious: {act.pos_deg}")

    if msg.timestamp_us == 0:
        problems.append("timestamp_us is zero (sender didn't set it)")

    return problems


def format_line(msg: RoveControl_pb2.RoveControl) -> str:
    t = msg.tracks
    arm_parts = [f"{getattr(msg.ovis, f'act_{i}').pos_deg:+5.1f}" for i in range(1, 7)]
    return (
        f"t={msg.timestamp_us:>16}  "
        f"tracks L={t.left_vel:+.2f} R={t.right_vel:+.2f}  "
        f"flip fl/fr/rl/rr="
        f"{msg.flippers.fl.pos_deg:+6.1f}/{msg.flippers.fr.pos_deg:+6.1f}/"
        f"{msg.flippers.rl.pos_deg:+6.1f}/{msg.flippers.rr.pos_deg:+6.1f}  "
        f"arm={','.join(arm_parts)}"
    )


def send_canary(host: str, port: int) -> None:
    canary = RoveControl_pb2.RoveControl()
    canary.timestamp_us = int(time.time() * 1_000_000)
    canary.tracks.left_vel = 0.25
    canary.tracks.right_vel = -0.25
    canary.flippers.fl.pos_deg = 10.0
    canary.ovis.act_1.pos_deg = 5.0
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(canary.SerializeToString(), (host, port))
    finally:
        s.close()
    print(f"[canary] sent test RoveControl to {host}:{port}")


def main() -> int:
    args = parse_args()

    # Force line-buffered stdout so frames show up live even when stdout
    # is wrapped by a launcher/pipe (block-buffering would otherwise hide
    # output until the buffer fills).
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    print(f"Listening on {args.host}:{args.port}  (Ctrl-C to stop)", flush=True)

    if args.send_test:
        # Send to loopback regardless of bind host so the canary reaches us.
        send_canary("127.0.0.1", args.port)

    total = 0
    parse_failures = 0
    warn_frames = 0
    start = time.monotonic()

    try:
        while True:
            data, addr = sock.recvfrom(8192)
            total += 1

            msg = RoveControl_pb2.RoveControl()
            try:
                msg.ParseFromString(data)
            except Exception as exc:
                parse_failures += 1
                print(
                    f"[#{total:05d} {addr[0]}:{addr[1]}] PARSE FAIL ({len(data)}B): {exc}",
                    flush=True,
                )
                continue

            problems = validate(msg)
            if problems:
                warn_frames += 1
                print(f"[#{total:05d} {addr[0]}:{addr[1]}] {format_line(msg)}", flush=True)
                for p in problems:
                    print(f"    ! {p}", flush=True)
            elif not args.quiet:
                print(f"[#{total:05d} {addr[0]}:{addr[1]}] {format_line(msg)}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        elapsed = time.monotonic() - start
        rate = total / elapsed if elapsed > 0 else 0.0
        print()
        print("=== summary ===")
        print(f"frames received : {total}")
        print(f"parse failures  : {parse_failures}")
        print(f"warn frames     : {warn_frames}")
        print(f"elapsed         : {elapsed:.1f}s  (avg {rate:.1f} Hz)")
        sock.close()

    return 0 if parse_failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
