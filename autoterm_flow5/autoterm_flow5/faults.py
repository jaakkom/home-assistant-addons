"""Flow 5 fault names and one-shot notifications."""

from __future__ import annotations

from dataclasses import dataclass

# Comfort Control manual, Flow 5 / Flow 14D v2 column.
FAULT_NAMES: dict[int, str] = {
    1: "Ylikuumeneminen",
    3: "Huolto",
    12: "Ylijännite",
    14: "Jäähdytysnestepumppu tai -piiri",
    15: "Alijännite",
    17: "Polttoainepumppu",
    20: "Ei yhteyttä",
    22: "Polttoainepumppu",
    24: "Huolto",
    25: "Liian nopea lämmönnousu",
    26: "Huolto",
    29: "Tarkista polttoainejärjestelmä",
    30: "Ei yhteyttä",
    37: "Lämmitin lukittu",
    50: "Ei yhteyttä",
    78: "Tarkista polttoainejärjestelmä",
}

LOCKOUT_CODES = frozenset({1, 37})
SELF_STOP_CODES = frozenset({25})


def fault_name(code: int) -> str:
    if code == 0:
        return "Ei vikaa"
    return FAULT_NAMES.get(code, f"Vikakoodi {code}")


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
            message="Sarjayhteys lämmittimeen katkesi.",
        )
