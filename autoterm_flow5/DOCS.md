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

Leave the physical panel disconnected. Two masters on the same connector collide, and the panel can lose communication. The add-on opens the serial port exclusively at 2400 8N1, with DTR and RTS held on and no hardware flow control. Autoterm Test disables those two lines. On this USB-UART that stops the heater from answering, so they stay on. After each request it keeps reading until a status frame arrives or the line goes quiet, so a slow reply is not overwritten by the next request. A gap under fifteen seconds is not logged as a lost heater. A read that comes back empty does not by itself close the port. The port stays open while any bytes have arrived within the last fifteen seconds. It is closed and opened again only when the adapter keeps failing and nothing recent has been heard.

The official Mosquitto broker is required so entities appear through MQTT discovery. Leave the MQTT fields empty. The add-on then reads the host, port, username, and password from the Supervisor. Mosquitto 6.5 does not register that service, and the log says `Service not enabled`. Update Mosquitto broker to 7.1 or newer, then restart this add-on. Temperature entities are read from the Home Assistant API. Enter them as a comma-separated list, for example `sensor.living_room,sensor.bedroom`.

## Control

Thermostat mode is the comfort-panel mode. Flow 5 regulates its power from the room sensor and the requested temperature, with its own hysteresis. Preheat is separate: that mode follows the coolant temperature and does not use the room sensor.

The climate card's target is the requested temperature. It is sent when heating is switched on, including when the burner is already running, and again whenever the card value changes. While heating stays on, the same request is sent about once a minute, and again if the burner goes out with no fault. A fault is not restarted. Turning heating off sends stop once, and again about every five seconds only while the burner is still running. Heating does not start when the add-on boots. Restarting the add-on does not send stop. If the heating switch is already on, the card's requested temperature is sent once.

Room temperature comes from Home Assistant sensors. Enter them on the add-on configuration page, for example `sensor.analog_input_8`. That list is read on every start. The coldest reading is used by default. That reading is sent to the heater as the comfort panel's cabin temperature, when it changes and about every thirty seconds. The add-on does not send stop because the room or the coolant reached a limit. If the burner goes out with no fault while heating is still on, the requested temperature is sent again.

The control unit keeps its own settings block. On startup the add-on reads that block and shows it on the add-on page and as Home Assistant entities: standby temperature (20–95 °C), auxiliary-heater mode and its standby temperature (80–95 °C), cabin-heater relay and the temperature that turns it on (30–60 °C), pump signal, pump-with-engine, pump-in-standby, and the alarm channel. Change them there. The change is written back to the same block, and bytes that were not changed stay as they were read. These values are not on the add-on configuration page, so that page cannot show a second copy. A write the heater does not accept while it is burning is tried again once the burner is idle. The room target, the cabin-temperature report, and the preheat duration are still ordinary run commands. They are not part of that stored block. Low supply voltage can still stop preheat and pump mode. Thermostat mode leaves voltage and coolant stops to the heater.

There is no power-level command. Low, middle, and high are the heater's own stages and are published as sensors.

Every setting is a Home Assistant number, switch, or select. Changes are stored in `/data/settings.json` and survive a restart. The add-on option page supplies the initial values. Delete that file to start again from the option page. The simulation switch is the exception: it is read from the configuration page on every start, because that page is the only place it can be changed.

The option page is grouped. Simulation and the device link come first, then temperature control, run times, coolant and voltage, fuel, and MQTT. Home Assistant 2025.10 or newer shows the groups. An older flat options file is still accepted.

## Sidebar

The add-on serves an ingress page on port 8099. On the add-on page, turn on **Show in sidebar**. Home Assistant then opens the diagnostics and settings page the same way it opens the Terminal & SSH add-on. The page shows the live measurements: temperatures, voltage, pump, flame, fans, fuel, and the raw frame bytes. The same values are published to Home Assistant. Coolant temperature comes from the parameter stream's liquid sensor while that stream is running, because the short status frame is not polled then. An empty measurement is not published, so it does not clear a sensor to unknown. It refreshes those values in place. Saving a setting writes the same value as the matching Home Assistant entity. Serial port, baud, MQTT, and the simulation switch stay on the add-on configuration page, because changing them needs a restart. The sidebar entry is available to administrators.

## Fuel

Pump frequency comes from the extended 58-byte frame. The 12 V TH-11 pump (assy 4995) meters 0.044 ml per stroke. Instantaneous consumption is `L/h = frequency_Hz * dose_ml * 3.6`. The total integrates the time between samples. A gap longer than five seconds adds nothing. The total is stored in `/data/fuel.json` and can be reset with the **Reset fuel total** button. The 24 V pump dose can differ, so the dose is adjustable.

Set **Tank capacity** to the tank size in litres. The level stays unknown until you press **Tank filled** or set **Fuel in tank**. A fill sets the remaining amount to the tank size and leaves the lifetime total unchanged. Consumption after that reduces the remaining amount. **Fuel level** is the remaining share of the tank, in percent, and is the entity to use on a gauge card. **Fuel time remaining** is the remaining litres divided by **Fuel hour average**. That average is the fuel used during the observed seconds of the last hour, including idle time, so a quiet hour makes the remaining time longer. Seconds with no sample, including a gap over five seconds, are left out. Until an hour has been observed, the average uses the time observed so far. It stays unknown until the pump has run once in that window. A smaller tank capacity caps the stored level.

Extended telemetry starts when the add-on sends the diagnostic-mode command, device `0x03` type `0x07` with payload `01`. That is the command the official tester sends. The heater then transmits the parameter block on its own, as device `0x02` type `0x01`. The tester parses that block as 63 bytes with the same CRC-16/MODBUS checksum, and its timer sends only a small keepalive. The empty device `0x02` query does not start the stream, and the status request only returns the 18-byte status. Diagnostic mode is on by default. The first listen after the port opens waits about a frame period, so a restart during a burn sees the stream before it writes. Until a checksum-valid parameter frame arrives, the enable command is repeated about every five seconds, and status is still requested so the link does not look lost. Status is not written in the same poll as that enable command, and a start or stop is not written in the same poll as either of them. After that first frame, status is not requested again: a status poll was cutting the stream. The official eight-second keepalive is sent only on a poll that heard nothing. Diagnostic mode is asked again only after that keepalive has been sent and the frame is still missing, about thirty seconds. Parameter frames suppress those requests only while they are still arriving, about two seconds after the last one. A single bad header does not silence the link for fifteen seconds. A plain status reply does not count as that stream. A parameter frame is 63 bytes. When the next frame starts one byte early, the missing byte is restored if the checksum then matches and the voltage, pump, and flame are in a real range. If more bytes are missing, the short frame is dropped and the next header is kept. A serial read glitch does not reopen the port while bytes have arrived within the last fifteen seconds. Start and stop go out on their own. Cabin temperature waits until a parameter frame has passed the checksum, or until the line has been quiet; it is not written on top of a frame that already failed. A burst that fails the checksum is not answered in that same poll. A broken but recognizable frame is discarded whole, so the next frame stays aligned. The add-on finishes an incoming frame before it sends anything, so a request does not cut the parameter frame in half. A failed checksum is logged with the device, type, and length from the rejected header, the raw frame, the checksum that was expected, whether the add-on wrote anything first, and the unread tail.

**Debug log** prints whether that frame arrives. Turn it on from the add-on configuration or from the Debug log switch. The configuration value is applied on every start. The switch takes effect on the next poll and lasts until the next restart. While it is on, each heater status change is logged, for example `Debug: heater status: Ignition 1`. The log says `Debug: 58-byte telemetry ok` with mode, voltage, pump frequency, and flame temperature, and repeats that about once a minute. If the frame is missing, the log says so at startup, again when the heater status changes, and again each time diagnostic mode is requested. The same line says how many bytes arrived, so a status-only reply is visible. The short status line `Heater answered` does not mean the 58-byte frame arrived. The diagnostic binary sensor **Telemetry stale** shows the same fact in Home Assistant.

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

Turn on **Simulation** in the add-on options to run without a USB-UART adapter. The process does not open a serial port. An internal heater answers status requests, the diagnostic-mode command, the stored-settings block, and start, stop, and pump commands with the same frames a Flow 5 uses.

When no temperature entities are configured, the control temperature is **Simulated room temperature** (default 15 °C). Change it from that number entity to warm or cool the room. If temperature entities are set, those Home Assistant sensors are used instead, so the integration can be checked against real sensors while the heater stays simulated.

**Simulated fault code** makes the virtual heater report that code. Use it to check the fault sensor and the notification. Set it back to 0 to clear the fault. The code is not sent to a real heater.

Coolant climbs while the burner is on and falls after a stop. The **Simulation** binary sensor is on while this mode is active.

## Tests

No heater is required. The same command runs in GitHub Actions on pull requests and on pushes to `main`.

```bash
PYTHONPATH=autoterm_flow5 python3 -m unittest discover -s autoterm_flow5/tests
```
