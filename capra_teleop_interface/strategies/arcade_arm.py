"""Arm control strategy: 6-DOF Ovis arm as Cartesian twist.

Layout:
    Right stick X:        arm position Y    (sideways)
    Right stick Y:        arm position X    (push forward = up on stick = +X)
    Right trigger (RT):   arm position +Z   (push trigger = up)
    Left  trigger (LT):   arm position −Z   (push trigger = down)
    Left stick X:         arm orientation yaw    (twist left/right)
    Left stick Y:         arm orientation pitch  (up = +pitch)
    DPAD left / right:    arm orientation roll
    RB (right bumper):    toggle gripper open / closed

All continuous inputs (sticks + triggers) are run through the same
deadzone + expo curve as the tracks strategies — see ``shape_stick``
in arcade_drive — so small deflections give fine control and the feel
is identical across modes. Roll stays on the DPAD; it's the one axis
without a free continuous input on the stock Xbox/Deck layout.

Tracks are zeroed — arm mode does not drive the rover.
"""
from __future__ import annotations

import time

from ..controllers.input_model import Button, ControllerInput, HapticCommand
from ..proto.core import RoveControl_pb2
from .arcade_drive import shape_stick
from .base import ControlStrategy

# Full-stick output cap — keeps the IK solver inside its linearised
# velocity envelope. Stick raw [-1, 1] becomes [-OVIS_AXIS_LIMIT,
# OVIS_AXIS_LIMIT] after expo shaping and this multiplier.
OVIS_AXIS_LIMIT = 0.6


def _clamp(v: float) -> float:
    return max(-1.0, min(1.0, v))


def _ovis_axis(raw: float) -> float:
    """Deadzone + symmetric expo (same curve as the tracks) + IK saturation cap.

    ``shape_stick`` produces a value in [-1, 1] with quadratic feel near zero;
    multiplying by ``OVIS_AXIS_LIMIT`` keeps full-stick output inside what the
    solver can integrate without saturating.
    """
    return _clamp(shape_stick(raw) * OVIS_AXIS_LIMIT)


class ArmControlStrategy(ControlStrategy):
    name = "arm_control"
    manages_gripper = True

    def __init__(self) -> None:
        self._last_update: float | None = None
        self._gripper_closed = False
        self._rb_was_pressed = False

    def on_activate(self, gripper_position: int = 0) -> None:
        self._last_update = None
        # Seed from controller latch so re-activating doesn't snap the
        # gripper open after the operator left it closed in a previous
        # arm session.
        self._gripper_closed = gripper_position >= 128
        self._rb_was_pressed = False

    def build_message(self, inp: ControllerInput) -> RoveControl_pb2.RoveControl:
        now = time.monotonic()
        self._last_update = now

        msg = RoveControl_pb2.RoveControl()
        msg.timestamp_us = int(now * 1_000_000)

        # Tracks zeroed — arm mode only.
        msg.tracks.left_vel = 0.0
        msg.tracks.right_vel = 0.0

        # --- Arm position ----------------------------------------------------
        # Right stick swapped vs. world XY so "push forward" maps to +X
        # (away from the rover) and "left/right" maps to ±Y.
        msg.ovis.position.x = _ovis_axis(-inp.right_y)
        msg.ovis.position.y = _ovis_axis(inp.right_x)
        # Z on the triggers: RT − LT, both already in [0, 1], so the
        # difference lands in [-1, 1] just like a stick axis and feeds
        # through the same expo curve.
        msg.ovis.position.z = _ovis_axis(inp.right_trigger - inp.left_trigger)

        # --- Arm orientation -------------------------------------------------
        msg.ovis.orientation.yaw = _ovis_axis(inp.left_x)
        msg.ovis.orientation.pitch = _ovis_axis(-inp.left_y)
        # Roll is discrete (DPAD) — no continuous input free on this layout.
        roll = (
            (1.0 if inp.is_pressed(Button.DPAD_RIGHT) else 0.0)
            - (1.0 if inp.is_pressed(Button.DPAD_LEFT) else 0.0)
        )
        msg.ovis.orientation.roll = roll * OVIS_AXIS_LIMIT

        # --- Gripper ---------------------------------------------------------
        # Edge-triggered toggle on RB.
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
