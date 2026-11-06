"""PiTFT 2.2 inch backlight on GPIO 18. High is lit, low is dark."""

from __future__ import annotations

PIN_BACKLIGHT = 18
BLANK_AFTER_S = 2 * 60


def idle_blank(last_input: float, now: float, blanked: bool, after_s: float = BLANK_AFTER_S) -> bool:
    """True when the light should go out."""
    if blanked:
        return False
    return now - last_input >= after_s


class Backlight:
    def __init__(self) -> None:
        self._pin = None
        self.on = True
        self._failed = False

    def set(self, on: bool) -> bool:
        if self._failed:
            return False
        try:
            if self._pin is None:
                from gpiozero import DigitalOutputDevice

                self._pin = DigitalOutputDevice(PIN_BACKLIGHT, initial_value=on)
            else:
                self._pin.value = on
        except Exception:
            self._failed = True
            return False
        self.on = on
        return True

    def close(self) -> None:
        pin = self._pin
        self._pin = None
        if pin is None:
            return
        try:
            pin.on()
        except Exception:
            pass
        try:
            pin.close()
        except Exception:
            pass
