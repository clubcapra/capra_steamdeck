"""Arcade drive + Ovis-as-Twist strategy.

Sticks and triggers drive the rover; the Ovis arm is commanded as a 6-DOF
Cartesian twist (normalized scalars in [-1, 1]) that the on-rover IK
engine resolves into joint velocities. We don't track per-actuator state
here — the IK loop owns that.

Layout:
    Left stick:   Y forward/back, X steer  → tracks
    Right stick:  X = Ovis +x, -Y = Ovis +y → arm linear xy
    Left trigger:  Ovis +z (arm up)
    Right trigger: Ovis −z (arm down)
    Bumpers:      LB / RB                  → Ovis yaw (-/+)
    Face A/B:     pitch +/-
    Face X/Y:     roll  +/-
    DPAD:         flipper directional steps (front, rear pairs)
    Back grip 5:  toggle gripper open/closed (Steam Deck; A on Xbox)
"""
from __future__ import annotations

import time

from ..controllers.input_model import Button, ControllerInput, HapticCommand
from ..proto.core import RoveControl_pb2
from .base import ControlStrategy

# Inputs below this magnitude are treated as zero (drift / noise).
STICK_DEADZONE = 0.08
# Exponent for the expo curve on Ovis axes. >1 = more travel before the
# command grows fast; the curve is x = sign(x) * |x|^EXPO. 2.0 = squared,
# very gentle near zero; 1.0 = linear (off).
OVIS_EXPO = 2.5
# Maximum normalised output for the Ovis axes (≤ 1). Drops "full stick"
# from saturating the engine's velocity envelope so fine control stays
# possible. Tracks/flippers/yaw etc. are unaffected.
OVIS_AXIS_LIMIT = 0.6


def _scaled_dz(value: float, dz: float = STICK_DEADZONE) -> float:
    """Deadzone with re-scaling so the output starts at exactly 0 when the
    stick clears the deadzone, then grows smoothly to ±1. Without the
    re-scaling, |output| jumps from 0 → dz at the threshold and turns the
    stick into a step input — which is what the operator was feeling as
    "all or nothing in millimetres".
    """
    a = abs(value)
    if a < dz:
        return 0.0
    sign = 1.0 if value >= 0 else -1.0
    return sign * (a - dz) / (1.0 - dz)


def _expo(value: float, exponent: float = OVIS_EXPO) -> float:
    """Sign-preserving power curve. ``exponent`` of 1 is linear; >1 yields
    a flatter response near zero (precise fine control) and the same end
    point at full deflection."""
    if value == 0.0:
        return 0.0
    sign = 1.0 if value >= 0 else -1.0
    return sign * (abs(value) ** exponent)


def _ovis_axis(raw: float) -> float:
    """Shape a stick / trigger reading into an Ovis twist component."""
    return _clamp(_expo(_scaled_dz(raw)) * OVIS_AXIS_LIMIT)


def _dz(value: float, dz: float = STICK_DEADZONE) -> float:
    """Hard deadzone used by the locomotion side (tracks, flippers)."""
    return 0.0 if abs(value) < dz else value


def _clamp(v: float) -> float:
    return max(-1.0, min(1.0, v))


class ArcadeArmStrategy(ControlStrategy):
    name = "arcade_arm"

    def __init__(self) -> None:
        self._last_update: float | None = None
        self._gripper_open = False
        self._gripper_btn_was_pressed = False

    def on_activate(self) -> None:
        self._last_update = None

    def build_message(self, inp: ControllerInput) -> RoveControl_pb2.RoveControl:
        now = time.monotonic()
        self._last_update = now

        msg = RoveControl_pb2.RoveControl()
        msg.timestamp_us = int(now * 1_000_000)

        # --- Arcade tracks ---
        throttle = -_dz(inp.left_y)
        steer = _dz(inp.left_x)
        msg.tracks.left_vel = _clamp(throttle + steer)
        msg.tracks.right_vel = _clamp(throttle - steer)

        # --- Flippers: D-pad (front pair / rear pair) ---
        front_dir = (
            (1 if inp.is_pressed(Button.DPAD_UP) else 0)
            - (1 if inp.is_pressed(Button.DPAD_DOWN) else 0)
        )
        rear_dir = (
            (1 if inp.is_pressed(Button.DPAD_RIGHT) else 0)
            - (1 if inp.is_pressed(Button.DPAD_LEFT) else 0)
        )
        msg.flippers.fl = front_dir
        msg.flippers.fr = front_dir
        msg.flippers.rl = rear_dir
        msg.flippers.rr = rear_dir

        # --- Ovis twist (normalised, scaled deadzone + expo curve) ---
        # Sticks/triggers feed the linear axes through `_ovis_axis`, which
        # gives a smooth zero-out near rest and a flatter response near
        # centre so operators can dial in millimetre-scale motion. Buttons
        # stay binary but are knocked down to OVIS_AXIS_LIMIT so a button
        # press doesn't immediately saturate the velocity envelope.
        msg.ovis.position.x = _ovis_axis(inp.right_x)
        msg.ovis.position.y = _ovis_axis(-inp.right_y)
        # LT = up (+z), RT = down (−z).
        msg.ovis.position.z = _ovis_axis(inp.left_trigger - inp.right_trigger)
        yaw = (1.0 if inp.is_pressed(Button.RB) else 0.0) - (1.0 if inp.is_pressed(Button.LB) else 0.0)
        pitch = (1.0 if inp.is_pressed(Button.A) else 0.0) - (1.0 if inp.is_pressed(Button.B) else 0.0)
        roll = (1.0 if inp.is_pressed(Button.Y) else 0.0) - (1.0 if inp.is_pressed(Button.X) else 0.0)
        msg.ovis.orientation.yaw = yaw * OVIS_AXIS_LIMIT
        msg.ovis.orientation.pitch = pitch * OVIS_AXIS_LIMIT
        msg.ovis.orientation.roll = roll * OVIS_AXIS_LIMIT

        # --- Gripper: edge-trigger on R5 (back grip) so each click toggles.
        gripper_btn = inp.is_pressed(Button.R5) or inp.is_pressed(Button.START)
        if gripper_btn and not self._gripper_btn_was_pressed:
            self._gripper_open = not self._gripper_open
        self._gripper_btn_was_pressed = gripper_btn
        msg.gripper.open_state = self._gripper_open

        return msg

    def compute_haptics(
        self, inp: ControllerInput, message: RoveControl_pb2.RoveControl
    ) -> HapticCommand | None:
        track_speed = max(abs(message.tracks.left_vel), abs(message.tracks.right_vel))
        arm_active = (
            abs(message.ovis.position.x) > 0.05
            or abs(message.ovis.position.y) > 0.05
            or abs(message.ovis.position.z) > 0.05
            or abs(message.ovis.orientation.yaw) > 0.05
            or abs(message.ovis.orientation.pitch) > 0.05
            or abs(message.ovis.orientation.roll) > 0.05
        )
        if track_speed < 0.1 and not arm_active:
            return None
        return HapticCommand(
            low_frequency=track_speed * 0.35,
            high_frequency=0.15 if arm_active else 0.0,
            duration_ms=80,
        )
