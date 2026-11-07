# Autoterm Flow 5

The add-on replaces the heater control panel. One 5 V USB-UART adapter is wired in place of the panel cable. The add-on is the only device on that link: it polls status, reports the room temperature, and decides when to start and stop.

This controls a combustion heater. On the first run, watch it: confirm status first, then stop, and only then start.

## Wiring

Flow 5 / BINAR-5S uses 2400 baud, 8N1, 5 V TTL. A 3.3 V adapter usually gets no reply.

| Adapter | Heater wire | Direction |
| --- | --- | --- |
| TX | white | panel to heater |
| RX | yellow | heater to panel |
| GND | blue or black | common ground |

Do not connect the red +12 V wire. It powers the panel and is not serial data.

Leave the physical panel disconnected. Two masters on the same connector collide, and the panel can lose communication.

The Mosquitto add-on is required so entities appear through MQTT discovery. Temperature entities are read from the Home Assistant API. Enter them as a comma-separated list, for example `sensor.living_room,sensor.bedroom`.

## Control

The climate entity holds the room target. Heating does not start when the add-on boots. Turn it on from the climate entity or the switch, or select preheat or pump-only mode.

Room temperature comes from Home Assistant sensors. The coldest reading is used by default. That same reading is sent to the heater as the panel cabin temperature. The setpoint itself is not sent on the wire.

Heat demand starts when the room is below the target by the drop (default 2 °C) and ends when it is above the target by the rise (default 1 °C). Minimum run and minimum idle are 10 minutes, so a one-degree bounce does not relight the burner immediately.

The coolant limit is its own number, default 88 °C, range 20–95 °C. The drop required before another start is 18 °C by default. The limit is enforced by a stop command from the add-on. The heater's stored setpoint is not written, because no confirmed frame exists for that. If the heater's own limit is lower, the heater stops first.

The burner is not started without a fresh sensor reading. If the reading goes stale during a run, the burner is stopped. Overheating (code 1) and lock (code 37) do not restart until heating is switched off and back on.

There is no power-level command. Low, middle, and high are the heater's own stages and are published as sensors.

Every setting is a Home Assistant number, switch, or select. Changes are stored in `/data/settings.json` and survive a restart. The add-on option page supplies only the initial values. Delete that file to start again from the option page.

## Fuel

Pump frequency comes from the extended 58-byte frame. The 12 V TH-11 pump (assy 4995) meters 0.044 ml per stroke. Instantaneous consumption is `L/h = frequency_Hz * dose_ml * 3.6`. The total integrates the time between samples. A gap longer than five seconds adds nothing. The total is stored in `/data/fuel.json` and can be reset with a button. The 24 V pump dose can differ, so the dose is adjustable.

Extended telemetry is requested with the `PUBR0` handshake. It is on by default. If the 58-byte frame does not arrive, the handshake is sent again about once a minute.

## Fault codes

The fault code, English text, and a fault binary sensor are published to Home Assistant. A notification is sent only when the code changes. It is removed when the code returns to zero. The MQTT topic `autoterm_flow5/event/fault` carries the code and text, so an automation can forward it to a phone. A lost serial link creates its own notification.

| Code | Text |
| --- | --- |
| 1 | Overheating |
| 3, 24, 26 | Service |
| 25 | Temperature rising too fast |
| 12 | Overvoltage |
| 14 | Coolant pump or circuit |
| 15 | Undervoltage |
| 17, 22 | Fuel pump |
| 20, 30, 50 | No connection |
| 29, 78 | Check the fuel system |
| 37 | Heater locked |

An unknown code is shown as its number.

## Simulation

Turn on **Simulation** in the add-on options to run without a USB-UART adapter. The process does not open a serial port. An internal heater answers status requests, the `PUBR0` handshake, and start, stop, and pump commands with the same frames a Flow 5 uses.

When no temperature entities are configured, the control temperature is **Simulated room temperature** (default 15 °C). Change it from that number entity to warm or cool the room. If temperature entities are set, those Home Assistant sensors are used instead, so the integration can be checked against real sensors while the heater stays simulated.

**Simulated fault code** makes the virtual heater report that code. Use it to check the fault sensor and the notification. Set it back to 0 to clear the fault. The code is not sent to a real heater.

Coolant climbs while the burner is on and falls after a stop, so the coolant limit and the following restart can be seen in Home Assistant. The **Simulation** binary sensor is on while this mode is active.

## Tests

No heater is required. The same command runs in GitHub Actions on pull requests and on pushes to `main`.

```bash
PYTHONPATH=autoterm_flow5 python3 -m unittest discover -s autoterm_flow5/tests
```
