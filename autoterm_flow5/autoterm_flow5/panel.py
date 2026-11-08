"""Sidebar page served through the Supervisor ingress proxy."""

from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

LOG = logging.getLogger("autoterm_flow5")

_PREFIX = re.compile(r"^/[A-Za-z0-9_./\-]*$")
_SWITCHES = {"power", "circulation"}
_PRESSED = {"stop", "fuel_reset", "fuel_refuel"}
_CHOICES = {
    "mode": {"thermostat", "preheat", "pump"},
    "aggregation": {"min", "avg", "max"},
}
_NUMBERS = {
    "room_target",
    "room_drop",
    "room_rise",
    "min_on",
    "min_off",
    "sensor_stale",
    "coolant_limit",
    "coolant_drop",
    "shutdown_voltage",
    "shutdown_voltage_seconds",
    "fan_temperature",
    "pump_dose",
    "preheat_minutes",
    "tank_capacity",
    "fuel_level",
    "simulate_room",
    "simulate_fault",
}


class PanelHub:
    """Latest status for the page, and commands waiting for the control loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, str] = {}
        self._commands: list[tuple[str, str]] = []

    def publish(self, values: dict[str, str]) -> None:
        with self._lock:
            self._values = dict(values)

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._values)

    def submit(self, key: str, value: str) -> None:
        with self._lock:
            self._commands.append((f"autoterm_flow5/set/{key}", value))

    def drain(self) -> list[tuple[str, str]]:
        with self._lock:
            commands = self._commands
            self._commands = []
            return commands


def ingress_prefix(header: str) -> str:
    """Keep a Supervisor ingress path and drop anything else."""
    text = header.strip()
    if not text or not _PREFIX.fullmatch(text):
        return ""
    return text.rstrip("/")


def page_language(header: str) -> str:
    for part in header.split(","):
        tag = part.split(";", 1)[0].strip().lower()
        if tag.startswith("fi"):
            return "fi"
        if tag.startswith("en"):
            return "en"
    return "en"


def command_accepted(key: str, value: str) -> bool:
    if key in _SWITCHES:
        return value in {"ON", "OFF"}
    if key in _PRESSED:
        return True
    choices = _CHOICES.get(key)
    if choices is not None:
        return value in choices
    if key in _NUMBERS:
        try:
            float(value)
        except ValueError:
            return False
        return True
    return False


class PanelServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], hub: PanelHub) -> None:
        self.hub = hub
        super().__init__(address, PanelHandler)


class PanelHandler(BaseHTTPRequestHandler):
    server: PanelServer

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            language = page_language(self.headers.get("Accept-Language", ""))
            prefix = ingress_prefix(self.headers.get("X-Ingress-Path", ""))
            page = PAGE.replace("__LANG__", language).replace("__PREFIX__", prefix)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            body = json.dumps(self.server.hub.snapshot()).encode("utf-8")
            self._send(200, body, "application/json")
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/command":
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 8192:
            self._send(400, b"Bad request", "text/plain; charset=utf-8")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            commands = _command_list(payload)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, KeyError):
            self._send(400, b"Bad request", "text/plain; charset=utf-8")
            return
        if not commands or len(commands) > 40:
            self._send(400, b"Bad request", "text/plain; charset=utf-8")
            return
        for key, value in commands:
            if not command_accepted(key, value):
                self._send(400, b"Bad request", "text/plain; charset=utf-8")
                return
        for key, value in commands:
            self.server.hub.submit(key, value)
        self._send(200, b'{"ok":true}', "application/json")

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _command_list(payload: object) -> list[tuple[str, str]]:
    if not isinstance(payload, dict):
        raise ValueError("payload")
    raw = payload.get("commands", [payload])
    if not isinstance(raw, list):
        raise ValueError("commands")
    commands: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("command")
        commands.append((str(item["key"]), str(item.get("value", "")).strip()))
    return commands


def start_panel(hub: PanelHub, port: int) -> PanelServer:
    """Listen for the ingress proxy. The caller keeps the control loop running."""
    server = PanelServer(("0.0.0.0", port), hub)
    thread = threading.Thread(target=server.serve_forever, name="ingress", daemon=True)
    thread.start()
    LOG.info("Ingress panel listening on %s", port)
    return server


PAGE = """<!DOCTYPE html>
<html lang="__LANG__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Autoterm Flow 5</title>
<style>
  :root { color-scheme: light dark; --bg: #eef2f4; --card: #fff; --text: #1b2420; --muted: #5d6b66; --line: #d5ded9; --accent: #2f6f3e; --bad: #9d2c2c; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #101412; --card: #1a211d; --text: #e7f0ea; --muted: #9aaba3; --line: #2c3832; --accent: #7dba78; --bad: #e07a7a; }
  }
  body { margin: 0; font: 15px/1.45 system-ui, sans-serif; background: var(--bg); color: var(--text); }
  main { max-width: 920px; margin: 0 auto; padding: 20px 16px 48px; }
  h1 { font-size: 1.4rem; margin: 0 0 4px; }
  h2 { font-size: 1.05rem; margin: 26px 0 10px; }
  p { margin: 0; }
  .lead, .note { color: var(--muted); }
  .note { min-height: 1.3em; margin-top: 8px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }
  .tile, .level { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px; }
  .tile span, label span { display: block; color: var(--muted); font-size: 0.78rem; }
  .tile strong { font-size: 1.12rem; }
  .bad { color: var(--bad); }
  .level { margin-top: 10px; }
  .level span { display: block; }
  .level strong { font-size: 1.12rem; }
  .track { height: 10px; border-radius: 99px; background: var(--line); overflow: hidden; margin-top: 8px; }
  .fill { height: 100%; width: 0; background: var(--accent); }
  .row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
  .fields { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 12px; }
  label { font-size: 0.92rem; }
  input, select, button { font: inherit; border-radius: 8px; border: 1px solid var(--line); padding: 8px 10px; background: var(--card); color: inherit; }
  button { cursor: pointer; background: var(--accent); color: #fff; border: 0; }
  button.ghost { background: transparent; color: inherit; border: 1px solid var(--line); }
  input[type="checkbox"] { width: 1.1rem; height: 1.1rem; }
</style>
</head>
<body>
<main>
  <h1>Autoterm Flow 5</h1>
  <p class="lead" id="lead"></p>
  <div class="row" id="actions"></div>
  <h2 id="diag-title"></h2>
  <div class="grid" id="tiles"></div>
  <section class="level"><span id="level-label"></span><strong id="level-value"></strong><div class="track"><div class="fill" id="level-fill"></div></div></section>
  <h2 id="set-title"></h2>
  <form id="settings">
    <div class="fields" id="fields"></div>
    <div class="row">
      <button type="submit" id="save"></button>
      <button type="button" class="ghost" id="refuel"></button>
      <button type="button" class="ghost" id="reset-fuel"></button>
    </div>
    <p class="note" id="note"></p>
  </form>
</main>
<script>
const LANG = "__LANG__";
const ROOT = "__PREFIX__";
const TEXT = {
  en: {
    lead: "Heater status and the settings that also exist as Home Assistant entities.",
    diag: "Diagnostics", settings: "Settings", save: "Save", saved: "Saved", failed: "Could not save",
    heatOn: "Heating on", heatOff: "Heating off", stop: "Stop", refuel: "Tank filled", reset: "Reset fuel total",
    link: "Link", online: "Online", offline: "Offline", heater: "Heater", phase: "Phase", fault: "Fault",
    noFault: "No fault", coolant: "Coolant", voltage: "Voltage", room: "Room", pump: "Pump",
    consumption: "Consumption", average: "Hour average", level: "Fuel level", remaining: "Remaining",
    hours: "Time left", total: "Fuel total", simulation: "Simulation", on: "On", off: "Off",
    mode: "Mode", aggregation: "Aggregation", room_target: "Room target", room_drop: "Room drop",
    room_rise: "Room rise",     min_on: "Minimum run (min)", min_off: "Minimum idle (min)",
    sensor_stale: "Sensor stale (min)",
    preheat_minutes: "Preheat (min)", coolant_limit: "Coolant limit", coolant_drop: "Coolant drop",
    shutdown_voltage: "Shutdown voltage", shutdown_voltage_seconds: "Shutdown voltage time (s)",
    fan_temperature: "Fan temperature",
    tank_capacity: "Tank capacity (L)", fuel_level: "Fuel in tank (L)", pump_dose: "Pump dose (mL)",
    circulation: "Circulation while waiting"
  },
  fi: {
    lead: "Lämmittimen tila ja asetukset, jotka ovat myös Home Assistantin entiteettejä.",
    diag: "Diagnostiikka", settings: "Asetukset", save: "Tallenna", saved: "Tallennettu", failed: "Tallennus ei onnistunut",
    heatOn: "Lämmitys päälle", heatOff: "Lämmitys pois", stop: "Pysäytä", refuel: "Tankki täytetty", reset: "Nollaa kulutuslaskuri",
    link: "Yhteys", online: "Yhteydessä", offline: "Poissa", heater: "Lämmitin", phase: "Vaihe", fault: "Vika",
    noFault: "Ei vikaa", coolant: "Neste", voltage: "Jännite", room: "Huone", pump: "Pumppu",
    consumption: "Kulutus", average: "Tunnin keskiarvo", level: "Polttoaine", remaining: "Jäljellä",
    hours: "Aikaa jäljellä", total: "Kulutus yhteensä", simulation: "Simulointi", on: "Päällä", off: "Pois",
    mode: "Tila", aggregation: "Yhdistäminen", room_target: "Huonetavoite", room_drop: "Huoneen pudotus",
    room_rise: "Huoneen nousu",     min_on: "Vähimmäiskäynti (min)", min_off: "Vähimmäisseisonta (min)",
    sensor_stale: "Anturin vanheneminen (min)",
    preheat_minutes: "Esilämmitys (min)", coolant_limit: "Nesteen yläraja", coolant_drop: "Nesteen pudotus",
    shutdown_voltage: "Sammutusjännite", shutdown_voltage_seconds: "Sammutusjännitteen aika (s)",
    fan_temperature: "Puhaltimen lämpö",
    tank_capacity: "Tankin koko (l)", fuel_level: "Polttoainetta tankissa (l)", pump_dose: "Pumpun annos (ml)",
    circulation: "Kierto odotuksessa"
  }
};
const T = TEXT[LANG] || TEXT.en;
const TILES = [
  ["link", "link"], ["mode", "heater"], ["phase", "phase"], ["fault_text", "fault"],
  ["coolant", "coolant", " °C"], ["voltage", "voltage", " V"], ["room", "room", " °C"],
  ["pump_hz", "pump", " Hz"], ["fuel_lph", "consumption", " L/h"], ["fuel_avg_lph", "average", " L/h"],
  ["fuel_remaining_l", "remaining", " L"], ["fuel_hours", "hours", " h"], ["fuel_total_l", "total", " L"],
  ["simulation", "simulation"]
];
const FIELDS = [
  {key: "mode", state: "mode_select", type: "select", options: ["thermostat", "preheat", "pump"]},
  {key: "aggregation", type: "select", options: ["min", "avg", "max"]},
  {key: "room_target", min: 0, max: 40, step: 0.5},
  {key: "room_drop", min: 0, max: 10, step: 0.5},
  {key: "room_rise", min: 0, max: 10, step: 0.5},
  {key: "min_on", min: 0, max: 120, step: 1},
  {key: "min_off", min: 0, max: 120, step: 1},
  {key: "sensor_stale", min: 1, max: 120, step: 1},
  {key: "preheat_minutes", min: 1, max: 480, step: 1},
  {key: "coolant_limit", min: 20, max: 95, step: 1},
  {key: "coolant_drop", min: 1, max: 40, step: 1},
  {key: "shutdown_voltage", min: 9, max: 30, step: 0.1},
  {key: "shutdown_voltage_seconds", min: 1, max: 600, step: 1},
  {key: "fan_temperature", min: 30, max: 60, step: 1},
  {key: "tank_capacity", min: 0, max: 500, step: 0.5},
  {key: "fuel_level", min: 0, max: 500, step: 0.1},
  {key: "pump_dose", min: 0.01, max: 0.2, step: 0.001},
  {key: "circulation", type: "switch"}
];
let dirty = false;
let built = false;

function url(path) { return (ROOT ? ROOT + "/" : "") + path; }
function text(key) { return T[key] || key; }
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, function (ch) {
    return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch];
  });
}
function shown(key, value, suffix) {
  if (key === "link") return value === "ON" ? text("online") : text("offline");
  if (key === "simulation") return value === "ON" ? text("on") : text("off");
  if (key === "fault_text" && !value) return text("noFault");
  if (!value) return "—";
  return escapeHtml(value) + (suffix || "");
}

function render(data) {
  document.getElementById("tiles").innerHTML = TILES.map(function (item) {
    const value = shown(item[0], data[item[0]] || "", item[2]);
    const bad = item[0] === "fault_text" && data.fault_active === "ON";
    return '<article class="tile"><span>' + text(item[1]) + '</span><strong class="' + (bad ? "bad" : "") + '">' + value + '</strong></article>';
  }).join("");
  const percent = Number(data.fuel_level_percent);
  const known = data.fuel_level_percent !== undefined && data.fuel_level_percent !== "";
  document.getElementById("level-label").textContent = text("level");
  document.getElementById("level-value").textContent = known ? Math.round(percent) + " %" : "—";
  document.getElementById("level-fill").style.width = known ? Math.max(0, Math.min(100, percent)) + "%" : "0";
  const heating = data.power === "ON";
  document.getElementById("actions").innerHTML =
    '<button type="button" id="heat">' + text(heating ? "heatOff" : "heatOn") + '</button>' +
    '<button type="button" class="ghost" id="stop">' + text("stop") + '</button>';
  document.getElementById("heat").onclick = function () { send([{key: "power", value: heating ? "OFF" : "ON"}]); };
  document.getElementById("stop").onclick = function () { send([{key: "stop", value: "pressed"}]); };
  fill(data);
}

function build() {
  document.getElementById("fields").innerHTML = FIELDS.map(function (field) {
    if (field.type === "select") {
      const options = field.options.map(function (option) {
        return '<option value="' + option + '">' + option + '</option>';
      }).join("");
      return '<label><span>' + text(field.key) + '</span><select id="f-' + field.key + '">' + options + '</select></label>';
    }
    if (field.type === "switch") {
      return '<label><span>' + text(field.key) + '</span><input id="f-' + field.key + '" type="checkbox"></label>';
    }
    return '<label><span>' + text(field.key) + '</span><input id="f-' + field.key + '" type="number" min="' + field.min + '" max="' + field.max + '" step="' + field.step + '"></label>';
  }).join("");
  document.querySelectorAll("#fields input, #fields select").forEach(function (element) {
    element.addEventListener("input", function () { dirty = true; });
    element.addEventListener("change", function () { dirty = true; });
  });
  built = true;
}

function fill(data) {
  if (!built) build();
  if (dirty) return;
  FIELDS.forEach(function (field) {
    const element = document.getElementById("f-" + field.key);
    if (document.activeElement === element) return;
    const raw = data[field.state || field.key] || "";
    if (field.type === "switch") element.checked = raw === "ON";
    else if (raw !== "") element.value = raw;
  });
}

async function send(commands) {
  const note = document.getElementById("note");
  try {
    const response = await fetch(url("api/command"), {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({commands: commands})
    });
    if (!response.ok) throw new Error("status");
    dirty = false;
    note.textContent = text("saved");
    refresh();
  } catch (error) {
    note.textContent = text("failed");
  }
}

async function refresh() {
  try {
    const response = await fetch(url("api/state"), {cache: "no-store"});
    if (!response.ok) return;
    render(await response.json());
  } catch (error) {
    return;
  }
}

document.getElementById("lead").textContent = text("lead");
document.getElementById("diag-title").textContent = text("diag");
document.getElementById("set-title").textContent = text("settings");
document.getElementById("save").textContent = text("save");
document.getElementById("refuel").textContent = text("refuel");
document.getElementById("reset-fuel").textContent = text("reset");
document.getElementById("settings").addEventListener("submit", function (event) {
  event.preventDefault();
  const commands = FIELDS.map(function (field) {
    const element = document.getElementById("f-" + field.key);
    const value = field.type === "switch" ? (element.checked ? "ON" : "OFF") : element.value;
    return {key: field.key, value: value};
  }).filter(function (command) { return command.value !== ""; });
  send(commands);
});
document.getElementById("refuel").onclick = function () { send([{key: "fuel_refuel", value: "pressed"}]); };
document.getElementById("reset-fuel").onclick = function () { send([{key: "fuel_reset", value: "pressed"}]); };
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""
