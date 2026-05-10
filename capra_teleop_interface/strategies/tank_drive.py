"""Tank drive strategy.

Sticks drive the tracks. Flipper control is Steam Deck only: hold a back
grip pad to select which flipper, press DPAD up/down to move it. Multiple
grips held together move multiple flippers at once. If no grip is held,
DPAD up/down moves all four flippers together.

Grip → flipper:
    L4 (top-left)     → front-left
    R4 (top-right)    → front-right
    L5 (bottom-left)  → rear-left
    R5 (bottom-right) → rear-right

On an Xbox pad the grip buttons are always False, so DPAD alone falls
through to the "move all" branch.
"""
from __future__ import annotations

import time

from ..controllers.input_model import Button, ControllerInput, HapticCommand
from ..proto.core import RoveControl_pb2
from .base import ControlStrategy


# Deadzone for analog sticks to avoid drift.
STICK_DEADZONE = 0.08


def _deadzone(value: float, dz: float = STICK_DEADZONE) -> float:
    return 0.0 if abs(value) < dz else value


class TankDriveStrategy(ControlStrategy):
    name = "tank_drive"

    def __init__(self) -> None:
        self._last_update: float | None = None

    def on_activate(self) -> None:
        self._last_update = None

    def build_message(self, inp: ControllerInput) -> RoveControl_pb2.RoveControl:
        now = time.monotonic()
        self._last_update = now

        msg = RoveControl_pb2.RoveControl()
        msg.timestamp_us = int(now * 1_000_000)

        # Tracks: stick Y is inverted on most controllers (up = -1)
        msg.tracks.left_vel = -_deadzone(inp.left_y)
        msg.tracks.right_vel = -_deadzone(inp.right_y)

        # Flippers: hold a back grip to select which flipper, use DPAD to
        # move it. With no grip held, DPAD moves all four together. Each
        # frame we emit a {-1, 0, +1} step direction; the rover-side
        # actuator loop integrates that into position.
        dpad_dir = (
            1 if inp.is_pressed(Button.DPAD_UP)
            else -1 if inp.is_pressed(Button.DPAD_DOWN)
            else 0
        )
        l4 = inp.is_pressed(Button.L4)
        r4 = inp.is_pressed(Button.R4)
        l5 = inp.is_pressed(Button.L5)
        r5 = inp.is_pressed(Button.R5)
        none_selected = not (l4 or r4 or l5 or r5)
        msg.flippers.fl = dpad_dir if (l4 or none_selected) else 0
        msg.flippers.fr = dpad_dir if (r4 or none_selected) else 0
        msg.flippers.rl = dpad_dir if (l5 or none_selected) else 0
        msg.flippers.rr = dpad_dir if (r5 or none_selected) else 0

        # Ovis arm is idle in this strategy (reserved for ArmStrategy).
        return msg

    def compute_haptics(
        self, inp: ControllerInput, message: RoveControl_pb2.RoveControl
    ) -> HapticCommand | None:
        # Rumble proportionally to track speed — gives the operator a sense
        # of how hard the rover is being commanded.
        speed = max(abs(message.tracks.left_vel), abs(message.tracks.right_vel))
        if speed < 0.1:
            return None
        return HapticCommand(
            low_frequency=speed * 0.4,
            high_frequency=speed * 0.2,
            duration_ms=80,
        )
