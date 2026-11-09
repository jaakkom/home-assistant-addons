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
_SWITCHES = {"power", "circulation", "debug"}
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
  details { margin-top: 18px; }
  .hex { white-space: pre-wrap; word-break: break-all; font: 12px/1.4 ui-monospace, monospace; margin: 8px 0 0; }
</style>
</head>
<body>
<main>
  <h1>Autoterm Flow 5</h1>
  <p class="lead" id="lead"></p>
  <div class="row" id="actions">
    <button type="button" id="heat"></button>
    <button type="button" class="ghost" id="stop"></button>
  </div>
  <div id="groups"></div>
  <section class="level"><span id="level-label"></span><strong id="level-value"></strong><div class="track"><div class="fill" id="level-fill"></div></div></section>
  <details>
    <summary id="raw-title"></summary>
    <p class="hex" id="status-hex"></p>
    <p class="hex" id="ext-hex"></p>
    <div class="grid" id="bytes"></div>
  </details>
  <h2 id="set-title"></h2>
  <form id="settings" autocomplete="off">
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
const PREFIX = "__PREFIX__";
const TEXT = {
  en: {
    lead: "Live measurements and the settings that also exist as Home Assistant entities.",
    status: "Status", temperatures: "Temperatures", electrical: "Pump and fans", fuelTitle: "Fuel",
    timeTitle: "Runtime", codes: "Codes", raw: "Raw frames", settings: "Settings",
    save: "Save", saved: "Saved", failed: "Could not save", stale: "Could not refresh the page",
    heatOn: "Heating on", heatOff: "Heating off", stop: "Stop", refuel: "Tank filled", reset: "Reset fuel total",
    link: "Link", online: "Online", offline: "Offline", operating: "Operating mode", phase: "Phase",
    burner: "Burner", demand: "Heat demand", fault: "Fault", noFault: "No fault", lockout: "Lockout",
    telemetry: "Telemetry stale", glow: "Glow plug", fan: "Interior fan", simulation: "Simulation",
    on: "On", off: "Off", coolant: "Coolant", coolant_a: "Coolant A", coolant_b: "Coolant B",
    flame: "Flame", liquid: "Liquid sensor", overheat: "Overheat sensor", board: "Board", room: "Room",
    rising: "Rising sensor", voltage: "Voltage", pump: "Pump", fan_target: "Fan target",
    fan_actual: "Fan measured", fan_current: "Fan current", engine: "Engine state", relay: "Relay",
    consumption: "Consumption", per_minute: "Per minute", average: "Hour average", level: "Fuel level",
    remaining: "Remaining", hours: "Time left", total: "Fuel total", total_ml: "Fuel total (mL)",
    elapsed_minutes: "Elapsed minutes", elapsed_seconds: "Elapsed seconds", runtime: "Runtime",
    cooldown: "Cooldown counter", state_code: "State code", substate: "Substate", fault_code: "Fault code",
    ext_fault: "Extended fault", ext_mode: "Extended mode",
    mode: "Mode", aggregation: "Aggregation", room_target: "Room target", room_drop: "Room drop",
    room_rise: "Room rise", min_on: "Minimum run (min)", min_off: "Minimum idle (min)",
    sensor_stale: "Sensor stale (min)", preheat_minutes: "Preheat (min)", coolant_limit: "Coolant limit",
    coolant_drop: "Coolant drop", shutdown_voltage: "Shutdown voltage",
    shutdown_voltage_seconds: "Shutdown voltage time (s)", fan_temperature: "Fan temperature",
    tank_capacity: "Tank capacity (L)", fuel_level: "Fuel in tank (L)", pump_dose: "Pump dose (mL)",
    circulation: "Circulation while waiting", debug: "Debug log",
    simulate_room: "Simulated room", simulate_fault: "Simulated fault"
  },
  fi: {
    lead: "Mittaukset ja asetukset, jotka ovat myös Home Assistantin entiteettejä.",
    status: "Tila", temperatures: "Lämpötilat", electrical: "Pumppu ja puhaltimet", fuelTitle: "Polttoaine",
    timeTitle: "Käyntiaika", codes: "Koodit", raw: "Raakakehykset", settings: "Asetukset",
    save: "Tallenna", saved: "Tallennettu", failed: "Tallennus ei onnistunut", stale: "Sivua ei saatu päivitettyä",
    heatOn: "Lämmitys päälle", heatOff: "Lämmitys pois", stop: "Pysäytä", refuel: "Tankki täytetty", reset: "Nollaa kulutuslaskuri",
    link: "Yhteys", online: "Yhteydessä", offline: "Poissa", operating: "Toimintatila", phase: "Vaihe",
    burner: "Poltin", demand: "Lämmön tarve", fault: "Vika", noFault: "Ei vikaa", lockout: "Lukitus",
    telemetry: "Telemetria vanhentunut", glow: "Hehkutulppa", fan: "Sisäpuhallin", simulation: "Simulointi",
    on: "Päällä", off: "Pois", coolant: "Neste", coolant_a: "Neste A", coolant_b: "Neste B",
    flame: "Liekki", liquid: "Nesteanturi", overheat: "Ylikuumenemisanturi", board: "Piirilevy", room: "Huone",
    rising: "Nouseva anturi", voltage: "Jännite", pump: "Pumppu", fan_target: "Puhaltimen tavoite",
    fan_actual: "Puhallin mitattu", fan_current: "Puhallinvirta", engine: "Moottorin tila", relay: "Rele",
    consumption: "Kulutus", per_minute: "Minuutissa", average: "Tunnin keskiarvo", level: "Polttoaine",
    remaining: "Jäljellä", hours: "Aikaa jäljellä", total: "Kulutus yhteensä", total_ml: "Kulutus yhteensä (ml)",
    elapsed_minutes: "Käynti (min)", elapsed_seconds: "Käynti (s)", runtime: "Käyntiaika",
    cooldown: "Jäähdytyslaskuri", state_code: "Tilakoodi", substate: "Alitila", fault_code: "Vikakoodi",
    ext_fault: "Laajennettu vikakoodi", ext_mode: "Laajennettu tila",
    mode: "Ohjaus", aggregation: "Yhdistäminen", room_target: "Huonetavoite", room_drop: "Huoneen pudotus",
    room_rise: "Huoneen nousu", min_on: "Vähimmäiskäynti (min)", min_off: "Vähimmäisseisonta (min)",
    sensor_stale: "Anturin vanheneminen (min)", preheat_minutes: "Esilämmitys (min)", coolant_limit: "Nesteen yläraja",
    coolant_drop: "Nesteen pudotus", shutdown_voltage: "Sammutusjännite",
    shutdown_voltage_seconds: "Sammutusjännitteen aika (s)", fan_temperature: "Puhaltimen lämpö",
    tank_capacity: "Tankin koko (l)", fuel_level: "Polttoainetta tankissa (l)", pump_dose: "Pumpun annos (ml)",
    circulation: "Kierto odotuksessa", debug: "Debug-loki",
    simulate_room: "Simuloitu huone", simulate_fault: "Simuloitu vika"
  }
};
const T = TEXT[LANG] || TEXT.en;
const ON_OFF = {burner: 1, demand: 1, lockout: 1, telemetry_stale: 1, glow_plug: 1, fan_request: 1, simulation: 1};
const GROUPS = [
  ["status", [
    ["link", "link"], ["mode", "operating"], ["phase", "phase"], ["burner", "burner"],
    ["demand", "demand"], ["fault_text", "fault"], ["lockout", "lockout"],
    ["telemetry_stale", "telemetry"], ["glow_plug", "glow"], ["fan_request", "fan"],
    ["simulation", "simulation"]
  ]],
  ["temperatures", [
    ["coolant", "coolant", " °C"], ["coolant_a", "coolant_a", " °C"], ["coolant_b", "coolant_b", " °C"],
    ["flame", "flame", " °C"], ["liquid", "liquid", " °C"], ["overheat", "overheat", " °C"],
    ["board", "board", " °C"], ["room", "room", " °C"], ["rising_sensor", "rising", " °C"]
  ]],
  ["electrical", [
    ["voltage", "voltage", " V"], ["pump_hz", "pump", " Hz"], ["fan_target", "fan_target"],
    ["fan_actual", "fan_actual"], ["fan_current", "fan_current", " mA"],
    ["engine_state", "engine"], ["relay_state", "relay"]
  ]],
  ["fuelTitle", [
    ["fuel_lph", "consumption", " L/h"], ["fuel_ml_min", "per_minute", " mL/min"],
    ["fuel_avg_lph", "average", " L/h"], ["fuel_remaining_l", "remaining", " L"],
    ["fuel_hours", "hours", " h"], ["fuel_total_l", "total", " L"], ["fuel_total_ml", "total_ml", " mL"]
  ]],
  ["timeTitle", [
    ["elapsed_minutes", "elapsed_minutes", " min"], ["elapsed_seconds", "elapsed_seconds", " s"],
    ["runtime", "runtime", " s"], ["cooldown_counter", "cooldown"]
  ]],
  ["codes", [
    ["state_code", "state_code"], ["substate_code", "substate"], ["fault_code", "fault_code"],
    ["ext_fault_code", "ext_fault"], ["ext_mode", "ext_mode"]
  ]]
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
  {key: "simulate_room", min: 0, max: 40, step: 0.5},
  {key: "simulate_fault", min: 0, max: 255, step: 1},
  {key: "circulation", type: "switch"},
  {key: "debug", type: "switch"}
];
const edited = {};
let built = false;
let pending = false;
let sawState = false;
let view = {};

function panelRoot() {
  let path = location.pathname || "/";
  if (path.endsWith("/index.html")) path = path.slice(0, -"/index.html".length);
  if (path === "/" || path === "") return PREFIX;
  if (!path.endsWith("/")) path += "/";
  return path.slice(0, -1);
}
function url(path) {
  const root = panelRoot();
  return (root ? root + "/" : "") + path;
}
function text(key) { return T[key] || key; }
function shown(key, value, suffix) {
  if (key === "link") return value === "ON" ? text("online") : text("offline");
  if (ON_OFF[key]) return value === "ON" ? text("on") : text("off");
  if (key === "fault_text" && !value) return text("noFault");
  if (value === undefined || value === null || value === "") return "—";
  return String(value) + (suffix || "");
}
function isBad(key, data) {
  if (key === "fault_text") return data.fault_active === "ON";
  if (key === "link") return data.link !== "ON";
  if (key === "lockout" || key === "telemetry_stale") return data[key] === "ON";
  return false;
}

function buildMeasurements() {
  document.getElementById("groups").innerHTML = GROUPS.map(function (group) {
    const tiles = group[1].map(function (item) {
      return '<article class="tile"><span>' + text(item[1]) + '</span><strong id="val-' + item[0] + '"></strong></article>';
    }).join("");
    return '<h2>' + text(group[0]) + '</h2><div class="grid">' + tiles + '</div>';
  }).join("");
}

function paint(data) {
  view = data;
  GROUPS.forEach(function (group) {
    group[1].forEach(function (item) {
      const node = document.getElementById("val-" + item[0]);
      if (!node) return;
      node.textContent = shown(item[0], data[item[0]], item[2]);
      node.className = isBad(item[0], data) ? "bad" : "";
    });
  });
  const known = data.fuel_level_percent !== undefined && data.fuel_level_percent !== "";
  const percent = Number(data.fuel_level_percent);
  document.getElementById("level-label").textContent = text("level");
  document.getElementById("level-value").textContent = known ? Math.round(percent) + " %" : "—";
  document.getElementById("level-fill").style.width = known ? Math.max(0, Math.min(100, percent)) + "%" : "0";
  document.getElementById("heat").textContent = text(data.power === "ON" ? "heatOff" : "heatOn");
  document.getElementById("status-hex").textContent = data.status_hex || "—";
  document.getElementById("ext-hex").textContent = data.ext_hex || "—";
  paintBytes(data);
  fill(data);
}

function paintBytes(data) {
  const box = document.getElementById("bytes");
  const keys = Object.keys(data).filter(function (key) {
    return key.indexOf("status_byte_") === 0 || key.indexOf("ext_byte_") === 0;
  }).sort(function (a, b) {
    return a.localeCompare(b, undefined, {numeric: true});
  });
  const have = {};
  Array.prototype.forEach.call(box.children, function (node) {
    have[node.getAttribute("data-key")] = node;
  });
  keys.forEach(function (key) {
    let node = have[key];
    if (!node) {
      node = document.createElement("article");
      node.className = "tile";
      node.setAttribute("data-key", key);
      const label = document.createElement("span");
      label.textContent = key.replace("status_byte_", "S").replace("ext_byte_", "E");
      const value = document.createElement("strong");
      node.appendChild(label);
      node.appendChild(value);
      box.appendChild(node);
    }
    node.querySelector("strong").textContent = data[key];
    delete have[key];
  });
  Object.keys(have).forEach(function (key) { have[key].remove(); });
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
  FIELDS.forEach(function (field) {
    const element = document.getElementById("f-" + field.key);
    element.addEventListener("input", function () { edited[field.key] = true; });
  });
  built = true;
}

function fill(data) {
  if (!built) build();
  FIELDS.forEach(function (field) {
    if (edited[field.key]) return;
    const element = document.getElementById("f-" + field.key);
    if (!element || document.activeElement === element) return;
    const raw = data[field.state || field.key];
    if (raw === undefined || raw === "") return;
    if (field.type === "switch") {
      element.checked = raw === "ON";
    } else if (field.type === "select") {
      if (element.value !== raw) element.value = raw;
    } else if (element.value === "" || Number(element.value) !== Number(raw)) {
      element.value = raw;
    }
  });
}

async function send(commands) {
  if (pending || !commands.length) return;
  pending = true;
  const note = document.getElementById("note");
  try {
    const response = await fetch(url("api/command"), {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({commands: commands})
    });
    if (!response.ok) throw new Error("status");
    commands.forEach(function (command) { delete edited[command.key]; });
    note.textContent = text("saved");
    setTimeout(function () { pending = false; refresh(); }, 1200);
  } catch (error) {
    pending = false;
    note.textContent = text("failed");
  }
}

async function refresh() {
  const note = document.getElementById("note");
  try {
    const response = await fetch(url("api/state"), {cache: "no-store"});
    if (!response.ok) throw new Error("status");
    const data = await response.json();
    if (pending && view.power) data.power = view.power;
    paint(data);
    sawState = true;
    if (note.textContent === text("stale")) note.textContent = "";
  } catch (error) {
    note.textContent = text("stale");
  }
}

document.getElementById("lead").textContent = text("lead");
document.getElementById("raw-title").textContent = text("raw");
document.getElementById("set-title").textContent = text("settings");
document.getElementById("save").textContent = text("save");
document.getElementById("refuel").textContent = text("refuel");
document.getElementById("reset-fuel").textContent = text("reset");
document.getElementById("stop").textContent = text("stop");
document.getElementById("heat").textContent = text("heatOn");
buildMeasurements();
document.getElementById("heat").onclick = function () {
  const next = view.power === "ON" ? "OFF" : "ON";
  view = Object.assign({}, view, {power: next});
  paint(view);
  send([{key: "power", value: next}]);
};
document.getElementById("stop").onclick = function () { send([{key: "stop", value: "pressed"}]); };
document.getElementById("settings").addEventListener("submit", function (event) {
  event.preventDefault();
  if (!sawState) return;
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
