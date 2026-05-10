"""Arcade drive + Ovis-as-Twist strategy.

Sticks and triggers drive the rover; the Ovis arm is commanded as a 6-DOF
Cartesian twist (normalized scalars in [-1, 1]) that the on-rover IK
engine resolves into joint velocities. We don't track per-actuator state
here — the IK loop owns that.

Layout:
    Left stick:   Y forward/back, X steer  → tracks
    Right stick:  X = Ovis +x, -Y = Ovis +y → linear xy
    Triggers:     LT − RT                  → Ovis +z (vertical)
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

STICK_DEADZONE = 0.08


def _dz(value: float, dz: float = STICK_DEADZONE) -> float:
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

        # --- Ovis twist (normalized [-1, 1]) ---
        msg.ovis.position.x = _clamp(_dz(inp.right_x))
        msg.ovis.position.y = _clamp(-_dz(inp.right_y))
        msg.ovis.position.z = _clamp(inp.right_trigger - inp.left_trigger)
        yaw = (1.0 if inp.is_pressed(Button.RB) else 0.0) - (1.0 if inp.is_pressed(Button.LB) else 0.0)
        pitch = (1.0 if inp.is_pressed(Button.A) else 0.0) - (1.0 if inp.is_pressed(Button.B) else 0.0)
        roll = (1.0 if inp.is_pressed(Button.Y) else 0.0) - (1.0 if inp.is_pressed(Button.X) else 0.0)
        msg.ovis.orientation.yaw = yaw
        msg.ovis.orientation.pitch = pitch
        msg.ovis.orientation.roll = roll

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
