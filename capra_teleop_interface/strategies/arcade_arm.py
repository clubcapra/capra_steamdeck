"""Arm control strategy: 6-DOF Ovis arm as Cartesian twist.

Layout:
    Right stick X / Y:   arm position X / Y  (up = -Y, raw passthrough)
    Left stick X:        arm orientation yaw  (twist left/right)
    Left stick Y:        arm orientation pitch  (up = +pitch)
    DPAD left / right:   arm orientation roll
    RB (right bumper):   toggle gripper open / closed

Tracks are zeroed — arm mode does not drive the rover.
"""
from __future__ import annotations

import time

from ..controllers.input_model import Button, ControllerInput, HapticCommand
from ..proto.core import RoveControl_pb2
from .base import ControlStrategy

STICK_DEADZONE = 0.08
# Expo exponent: >1 = flat near centre for fine control.
OVIS_EXPO = 2.5
# Full-stick output cap — avoids saturating the IK velocity envelope.
OVIS_AXIS_LIMIT = 0.6


def _scaled_dz(value: float, dz: float = STICK_DEADZONE) -> float:
    """Deadzone with output rescaled to [0, 1] past the threshold."""
    a = abs(value)
    if a < dz:
        return 0.0
    sign = 1.0 if value >= 0 else -1.0
    return sign * (a - dz) / (1.0 - dz)


def _expo(value: float, exponent: float = OVIS_EXPO) -> float:
    if value == 0.0:
        return 0.0
    sign = 1.0 if value >= 0 else -1.0
    return sign * (abs(value) ** exponent)


def _ovis_axis(raw: float) -> float:
    return _clamp(_expo(_scaled_dz(raw)) * OVIS_AXIS_LIMIT)


def _clamp(v: float) -> float:
    return max(-1.0, min(1.0, v))


class ArmControlStrategy(ControlStrategy):
    name = "arm_control"
    manages_gripper = True

    def __init__(self) -> None:
        self._last_update: float | None = None
        self._gripper_closed = False
        self._rb_was_pressed = False

    def on_activate(self) -> None:
        self._last_update = None

    def build_message(self, inp: ControllerInput) -> RoveControl_pb2.RoveControl:
        now = time.monotonic()
        self._last_update = now

        msg = RoveControl_pb2.RoveControl()
        msg.timestamp_us = int(now * 1_000_000)

        # Tracks zeroed — arm mode only.
        msg.tracks.left_vel = 0.0
        msg.tracks.right_vel = 0.0

        # Arm position: right stick XY; Z not mapped in this schema.
        msg.ovis.position.x = _ovis_axis(inp.right_x)
        msg.ovis.position.y = _ovis_axis(inp.right_y)
        msg.ovis.position.z = 0.0

        # Arm orientation: left stick X=yaw, Y=pitch (inverted); DPAD=roll.
        msg.ovis.orientation.yaw = _ovis_axis(inp.left_x)
        msg.ovis.orientation.pitch = _ovis_axis(-inp.left_y)
        roll = (
            (1.0 if inp.is_pressed(Button.DPAD_RIGHT) else 0.0)
            - (1.0 if inp.is_pressed(Button.DPAD_LEFT) else 0.0)
        )
        msg.ovis.orientation.roll = roll * OVIS_AXIS_LIMIT

        # Gripper: edge-triggered toggle on RB.
        rb = inp.is_pressed(Button.RB)
        if rb and not self._rb_was_pressed:
            self._gripper_closed = not self._gripper_closed
        self._rb_was_pressed = rb
        msg.gripper.position = 255 if self._gripper_closed else 0

        return msg

    def compute_haptics(
        self, inp: ControllerInput, message: RoveControl_pb2.RoveControl
    ) -> HapticCommand | None:
        arm_active = (
            abs(message.ovis.position.x) > 0.05
            or abs(message.ovis.position.y) > 0.05
            or abs(message.ovis.position.z) > 0.05
            or abs(message.ovis.orientation.yaw) > 0.05
            or abs(message.ovis.orientation.pitch) > 0.05
            or abs(message.ovis.orientation.roll) > 0.05
        )
        if not arm_active:
            return None
        return HapticCommand(
            low_frequency=0.0,
            high_frequency=0.15,
            duration_ms=80,
        )


# Alias for callers still using the old class name.
ArcadeArmStrategy = ArmControlStrategy
