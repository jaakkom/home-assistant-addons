"""Flow 5 fault names and one-shot notifications."""

from __future__ import annotations

from dataclasses import dataclass

# Comfort Control manual, Flow 5 / Flow 14D v2 column.
FAULT_NAMES: dict[int, str] = {
    1: "Overheating",
    3: "Service",
    12: "Overvoltage",
    14: "Coolant pump or circuit",
    15: "Undervoltage",
    17: "Fuel pump",
    20: "No connection",
    22: "Fuel pump",
    24: "Service",
    25: "Temperature rising too fast",
    26: "Service",
    29: "Check the fuel system",
    30: "No connection",
    37: "Heater locked",
    50: "No connection",
    78: "Check the fuel system",
}

LOCKOUT_CODES = frozenset({1, 37})
SELF_STOP_CODES = frozenset({25})


def fault_name(code: int) -> str:
    if code == 0:
        return "No fault"
    return FAULT_NAMES.get(code, f"Fault code {code}")


@dataclass
class Notice:
    action: str
    notification_id: str
    title: str
    message: str
    code: int | None = None


class ChangeNotifier:
    """Emit a notice only when the watched value changes."""

    def __init__(self, notification_id: str, title: str) -> None:
        self.notification_id = notification_id
        self.title = title
        self.last: int | None = None

    def update(self, code: int, message: str | None = None) -> Notice | None:
        if self.last is None:
            self.last = 0 if code == 0 else None
            if code == 0:
                return None
        if code == self.last:
            return None
        self.last = code
        if code == 0:
            return Notice(
                action="dismiss",
                notification_id=self.notification_id,
                title=self.title,
                message="",
                code=0,
            )
        text = message if message is not None else fault_name(code)
        return Notice(
            action="create",
            notification_id=self.notification_id,
            title=self.title,
            message=text,
            code=code,
        )


class LinkNotifier:
    """One notice when the serial link drops, dismissed when it returns."""

    def __init__(self) -> None:
        self.online: bool | None = None

    def update(self, online: bool) -> Notice | None:
        if self.online is None:
            self.online = online
            if online:
                return None
        if online == self.online:
            return None
        self.online = online
        if online:
            return Notice(
                action="dismiss",
                notification_id="autoterm_flow5_link",
                title="Autoterm Flow 5",
                message="",
            )
        return Notice(
            action="create",
            notification_id="autoterm_flow5_link",
            title="Autoterm Flow 5",
            message="Serial link to the heater was lost.",
        )
