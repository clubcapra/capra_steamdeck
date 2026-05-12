"""Template-method base class for controllers.

Defines the fixed polling-loop skeleton (open → loop { read → interpret →
send → haptics } → close) and delegates the device-specific pieces to
abstract hooks that subclasses must implement.

The public API is ``run()``; subclasses only override:
  * ``_open_device()``      – connect to the physical controller
  * ``_read_input()``       – produce a ``ControllerInput`` for this frame
  * ``_close_device()``     – release the handle
  * ``_create_haptic()``    – return the HapticFeedback for this device

The loop, strategy invocation, UDP send, and rate pacing are all handled
here so no subclass has to re-implement them.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Optional

from .input_model import Button, ControllerInput
from ..haptics.base import HapticFeedback, NullHaptic
from ..network.udp_receiver import UdpTorqueReceiver
from ..network.udp_sender import UdpSender
from ..strategies.base import ControlStrategy

log = logging.getLogger(__name__)


class ControllerBase(ABC):
    """Template method: drives the full read→send loop.

    Parameters
    ----------
    sender:
        UDP transport for outgoing protobuf frames.
    strategy:
        The initial control strategy. Can be swapped at runtime via
        ``set_strategy``.
    rate_hz:
        Target polling frequency. The loop paces itself to hit this.
    haptics_enabled:
        If ``False``, uses a ``NullHaptic`` regardless of what the subclass
        builds.
    """

    def __init__(
        self,
        sender: UdpSender,
        strategy: ControlStrategy,
        rate_hz: float = 50.0,
        haptics_enabled: bool = True,
        torque_receiver: Optional[UdpTorqueReceiver] = None,
        stick_deadzone: float = 0.05,
        trigger_deadzone: float = 0.02,
    ) -> None:
        self._sender = sender
        self._strategy = strategy
        self._rate_hz = rate_hz
        self._period = 1.0 / rate_hz
        self._haptics_enabled = haptics_enabled
        self._haptic: HapticFeedback = NullHaptic()
        self._torque_receiver = torque_receiver
        self._stop = False
        self._stick_deadzone = stick_deadzone
        self._trigger_deadzone = trigger_deadzone

    # ---- Template method ----------------------------------------------------

    def run(self) -> None:
        """Run the polling loop until ``stop()`` is called or input ends."""
        self._open_device()
        self._haptic = self._create_haptic() if self._haptics_enabled else NullHaptic()
        if self._torque_receiver is not None and self._haptics_enabled:
            self._torque_receiver.start()
        self._strategy.on_activate()

        log.info(
            "Controller loop started: device=%s, strategy=%s, rate=%.1fHz, target=%s:%d",
            self.__class__.__name__,
            self._strategy.name,
            self._rate_hz,
            self._sender.endpoint.host,
            self._sender.endpoint.port,
        )

        try:
            next_tick = time.monotonic()
            while not self._stop:
                inp = self._read_input()
                if inp is None:
                    # Device dropped; bail.
                    log.warning("Controller read returned None, stopping loop")
                    break

                # Hook: subclasses get to intercept (e.g., for strategy
                # cycling via the Guide button).
                self._handle_meta_buttons(inp)

                # Always build so strategy internal state (dt-integrated
                # flipper/arm positions) stays fresh; skipping builds would
                # cause a jump when the operator moves again.
                msg = self._strategy.build_message(inp)

                # Push-based control: the robot stops when packets stop
                # arriving, so suppress frames where nothing is commanded
                # rather than spam empty telemetry.
                if not inp.is_idle(self._stick_deadzone, self._trigger_deadzone):
                    self._sender.send(msg)

                # Haptics always tick — torque feedback is about what the
                # robot is doing, not what the operator is pressing.
                if self._torque_receiver is not None:
                    self._haptic.rumble(self._torque_receiver.as_haptic_command())
                else:
                    haptic_cmd = self._strategy.compute_haptics(inp, msg)
                    if haptic_cmd is not None:
                        self._haptic.rumble(haptic_cmd)

                # Rate pacing with drift correction.
                next_tick += self._period
                sleep_for = next_tick - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    # Fell behind; re-baseline rather than try to catch up.
                    next_tick = time.monotonic()
        finally:
            self._strategy.on_deactivate()
            if self._torque_receiver is not None:
                try:
                    self._torque_receiver.stop()
                except Exception:
                    pass
            try:
                self._haptic.stop()
            except Exception:
                pass
            self._close_device()
            log.info("Controller loop stopped")

    def stop(self) -> None:
        """Signal the polling loop to exit at the next iteration."""
        self._stop = True

    def set_strategy(self, strategy: ControlStrategy) -> None:
        """Hot-swap the active strategy."""
        log.info("Switching strategy: %s -> %s", self._strategy.name, strategy.name)
        self._strategy.on_deactivate()
        self._strategy = strategy
        self._strategy.on_activate()

    @property
    def strategy(self) -> ControlStrategy:
        return self._strategy

    # ---- Hooks subclasses override -----------------------------------------

    @abstractmethod
    def _open_device(self) -> None:
        """Initialize and open the physical controller."""

    @abstractmethod
    def _close_device(self) -> None:
        """Release the physical controller."""

    @abstractmethod
    def _read_input(self) -> Optional[ControllerInput]:
        """Return a snapshot for this frame, or ``None`` to exit the loop."""

    @abstractmethod
    def _create_haptic(self) -> HapticFeedback:
        """Build the haptic backend appropriate for this device."""

    # ---- Optional hook ------------------------------------------------------

    def _handle_meta_buttons(self, inp: ControllerInput) -> None:
        """Handle non-control buttons (strategy cycling, emergency stop, …).

        Default: pressing START triggers a clean stop. Subclasses may
        extend to cycle strategies on e.g. the Guide button.
        """
        if inp.is_pressed(Button.START) and inp.is_pressed(Button.BACK):
            # Two-button chord to avoid accidental stops.
            log.info("START+BACK chord: requesting stop")
            self.stop()
