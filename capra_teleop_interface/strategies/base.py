"""Strategy pattern for interpreting controller input.

A strategy converts a neutral ``ControllerInput`` into the protobuf
``RoveControl`` message that gets shipped to the rover, and optionally
returns a ``HapticCommand`` for feedback to the operator. Swapping
strategies at runtime swaps the entire control scheme without touching
the controller or network layers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..controllers.input_model import ControllerInput, HapticCommand
from ..proto.core import RoveControl_pb2


class ControlStrategy(ABC):
    """Base class for all control strategies."""

    #: Human-readable name, shown in logs / UI.
    name: str = "unnamed"

    @abstractmethod
    def build_message(self, inp: ControllerInput) -> RoveControl_pb2.RoveControl:
        """Return a populated ``RoveControl`` message for this frame."""

    def compute_haptics(
        self, inp: ControllerInput, message: RoveControl_pb2.RoveControl
    ) -> Optional[HapticCommand]:
        """Optional: derive a rumble command from this frame.

        Default returns ``None`` (no rumble). Override to provide feedback
        like "track slip", "end-of-travel on a flipper", or "collision".
        """
        return None

    def on_activate(self) -> None:
        """Hook called when this strategy becomes active. Default: no-op."""

    def on_deactivate(self) -> None:
        """Hook called when this strategy is swapped out. Default: no-op."""
