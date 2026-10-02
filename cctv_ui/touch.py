"""Poll the Waveshare FT6336U touch controller on I2C."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time

log = logging.getLogger(__name__)

ADDR = 0x38
BUS = 1


class TouchReader:
    def __init__(self) -> None:
        self._bus = None
        self._warned = False
        self._down = False
        self._retry_at = 0.0
        self.ready = False

    def open(self) -> None:
        if self._bus is not None:
            return
        smbus = _load_smbus()
        if smbus is None:
            self._warn("python3-smbus is not installed, panel taps are off")
            return
        _reset_ft6336()
        try:
            bus = smbus.SMBus(BUS)
        except OSError as exc:
            self._warn(f"i2c-{BUS} is not available ({exc})")
            return
        self._bus = bus
        self._warned = False
        if _chip_answers(bus):
            self.ready = True
            log.info("ft6336 ready on i2c-%s", BUS)
            return
        log.warning("ft6336 at 0x%02x did not answer. i2c scan: %s", ADDR, _scan(bus))

    def poll(self) -> tuple[int, int] | None:
        """Return a panel pixel when a finger first lands, otherwise None."""
        now = time.monotonic()
        if not self.ready and now >= self._retry_at:
            self._retry_at = now + 2.0
            if self._bus is None:
                self.open()
            elif _chip_answers(self._bus):
                self.ready = True
                log.info("ft6336 ready on i2c-%s", BUS)
        point = self._read_point()
        if point is None:
            self._down = False
            return None
        if self._down:
            return None
        self._down = True
        return point

    def close(self) -> None:
        bus = self._bus
        self._bus = None
        self.ready = False
        if bus is None:
            return
        try:
            bus.close()
        except OSError:
            pass

    def _read_point(self) -> tuple[int, int] | None:
        bus = self._bus
        if bus is None:
            return None
        try:
            count = bus.read_byte_data(ADDR, 0x02) & 0x0F
            if count < 1 or count > 2:
                return None
            data = bus.read_i2c_block_data(ADDR, 0x03, 4)
        except OSError as exc:
            self._warn(f"ft6336 read failed ({exc})")
            return None
        x = ((data[0] & 0x0F) << 8) | data[1]
        y = ((data[2] & 0x0F) << 8) | data[3]
        return x, y

    def _warn(self, message: str) -> None:
        if self._warned:
            return
        self._warned = True
        log.warning(message)


def _chip_answers(bus) -> bool:
    try:
        bus.read_byte_data(ADDR, 0x00)
    except OSError:
        return False
    return True


def _scan(bus) -> str:
    found: list[str] = []
    for addr in range(0x03, 0x78):
        try:
            bus.write_quick(addr)
        except OSError:
            continue
        found.append(f"0x{addr:02x}")
    return " ".join(found) if found else "none"


def _reset_ft6336() -> None:
    """TP_RST is GPIO 17, active low. Release it so the controller answers."""
    pinctrl = shutil.which("pinctrl")
    if not pinctrl:
        return
    subprocess.run([pinctrl, "set", "17", "op", "dl"], check=False, capture_output=True)
    time.sleep(0.01)
    subprocess.run([pinctrl, "set", "17", "op", "dh"], check=False, capture_output=True)
    time.sleep(0.05)


def _load_smbus():
    try:
        import smbus
    except ImportError:
        smbus = None
    if smbus is not None:
        return smbus
    try:
        import smbus2 as smbus
    except ImportError:
        return None
    return smbus
