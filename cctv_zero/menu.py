"""Home, camera, debug, and power screens for the four buttons."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Menu:
    page: str = "home"
    selected: int = 0
    phase: str = "idle"
    cameras: tuple[str, ...] = ()
    notice: str = ""


@dataclass(frozen=True)
class Press:
    menu: Menu
    effect: str = ""


def home_label(phase: str) -> str:
    if phase == "running":
        return "CCTV (running)"
    if phase == "starting":
        return "CCTV (starting)"
    return "CCTV"


def rows(menu: Menu) -> tuple[str, ...]:
    if menu.page == "home":
        return (home_label(menu.phase), "Debug", "Power off")
    if menu.page == "power":
        return ("Yes", "No")
    if menu.page == "debug":
        return ()
    names = menu.cameras or ("No camera",)
    if menu.phase in {"starting", "running"}:
        return names + ("Stop",)
    return names


def _home(menu: Menu, selected: int) -> Menu:
    return Menu(
        page="home",
        selected=selected,
        phase=menu.phase,
        cameras=menu.cameras,
        notice=menu.notice,
    )


def press(menu: Menu, button: str) -> Press:
    """Apply one short button press. Holds are logged by the UI and do not select."""
    action = button.strip().lower()
    if action in {"up", "down"} and menu.page != "debug":
        count = len(rows(menu)) or 1
        delta = -1 if action == "up" else 1
        return Press(
            Menu(
                page=menu.page,
                selected=(menu.selected + delta) % count,
                phase=menu.phase,
                cameras=menu.cameras,
                notice=menu.notice,
            )
        )
    if action == "back":
        if menu.page == "debug":
            return Press(_home(menu, 1))
        if menu.page == "power":
            return Press(_home(menu, 2))
        if menu.page == "cameras":
            return Press(_home(menu, 0))
        return Press(menu)
    if action != "select":
        return Press(menu)
    if menu.page == "home":
        if menu.selected == 1:
            return Press(
                Menu(
                    page="debug",
                    phase=menu.phase,
                    cameras=menu.cameras,
                    notice=menu.notice,
                )
            )
        if menu.selected == 2:
            return Press(
                Menu(
                    page="power",
                    selected=1,
                    phase=menu.phase,
                    cameras=menu.cameras,
                    notice=menu.notice,
                )
            )
        return Press(
            Menu(
                page="cameras",
                phase=menu.phase,
                cameras=menu.cameras,
                notice=menu.notice,
            )
        )
    if menu.page == "power":
        if menu.selected == 0:
            return Press(menu, effect="poweroff")
        return Press(_home(menu, 2))
    if menu.page == "debug":
        return Press(menu)
    labels = rows(menu)
    label = labels[menu.selected % len(labels)]
    if label == "No camera":
        return Press(menu)
    if label == "Stop":
        return Press(
            Menu(page="home", cameras=menu.cameras),
            effect="stop",
        )
    return Press(
        Menu(page="home", phase="starting", cameras=menu.cameras),
        effect=f"start:{label}",
    )
