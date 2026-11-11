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

The official Mosquitto broker is required so entities appear through MQTT discovery. Leave the MQTT fields empty. The add-on then reads the host, port, username, and password from the Supervisor. Mosquitto 6.5 does not register that service, and the log says `Service not enabled`. Update Mosquitto broker to 7.1 or newer, then restart this add-on. Only decoded values go to Home Assistant. Undecoded status and parameter bytes, whole frames in hex, and per-second counters (elapsed time, runtime, cooldown counter) stay on the add-on page; entities for them left over from older versions are removed on start. Temperature entities are read from the Home Assistant API. Enter them as a comma-separated list, for example `sensor.living_room,sensor.bedroom`.

## Control

Thermostat mode is the comfort-panel mode. Comfort Control sets a temperature and a run time, and the run time can be unlimited. Flow 5 regulates from the room sensor and that temperature, with its own hysteresis, for as long as the run time allows. Preheat is separate: that mode follows the coolant temperature and does not use the room sensor.

The climate card's target is the temperature. Run time is its own control, in minutes. Zero is unlimited, which is the default, and the longest finite time is 12 hours. The two are sent together when heating is switched on, including when the burner is already running, and again when either value changes. They are not sent again on a timer. With the add-on thermostat off, a burner that returns to idle stays idle: a finite run is meant to end, and an unlimited run is the heater's own session. A fault is not restarted. Turning heating off sends stop once, and again about every five seconds only while the burner is still running. The heating switch and the mode are saved, because Home Assistant does not retain the card's commands. After a restart or an update, heating that was on resumes in the same mode, and heating that was off stays off. Restarting the add-on does not send stop. A burn found running after a restart counts as having had its minimum run. The card shows heating while the burner runs, idle while heating is on and the burner waits, and off when heating is off. After the MQTT connection drops and returns, the add-on subscribes to the card's commands again and reports itself online.

Room temperature comes from Home Assistant sensors. Enter them on the add-on configuration page, for example `sensor.analog_input_8`. That list is read on every start. The coldest reading is used by default. That reading is sent to the heater as the comfort panel's cabin temperature, when it changes and about every thirty seconds.

**Add-on thermostat** (on by default): the add-on starts and stops the heater from the room reading, so it does not have to rely on the heater's own thermostat. The heater is stopped when the room reaches the target plus **Stop above target** (0.5 °C), after it has run at least ten minutes. It starts again when the room falls to the target minus **Room hysteresis** (1 °C), after it has been off at least five minutes. Switching heating on in a room already at target does not light the heater. A heater that ends an unlimited session by itself is started again the same way. A stale room reading changes nothing. Settings saved before this version that still hold the old defaults (2 °C, 1 °C, ten minutes off) move to the new ones once. Turn the switch off to leave stopping and starting to the heater. The coolant temperature never stops thermostat mode.

## Adaptive coolant control

Optional. Off by default. When enabled, the add-on learns a small model of the house and radiator network (UA, radiator coefficient C, and outdoor-bin corrections K) and chooses the liquid standby temperature so the heater stays in longer burns with fewer ignitions. It only changes the coolant target. Room hysteresis stays as you set it.

Set an outdoor temperature entity on the configuration page. The algorithm writes a new coolant target while the heater is idle and holds the thermostat start until the heater reads the value back. A rejected write is retried three times. After that the heater starts with its own setting, an error is logged, and that cycle is not learned from. All learning uses the target read back from the heater.

Liquid standby counts only when the heater reports a waiting or blowing state after burning, the liquid is within 2 °C of the target, and the pump is stopped. The power stage comes from the heater's state name (Low, Middle, High), or from the nearest pump frequency (1.8, 3.0 or 4.4 Hz). Startup is not counted as burn time.

The algorithm can stop and restart only to raise the target: after liquid standby while the room is still cold, or when power is clearly too low for thirty minutes. Neither happens before the burn phase has lasted the minimum burn time (20 min by default). All such stops share one 90-minute lockout, with at most two stops in six hours. When the limit is reached, a warning is logged and corrections pause. A raise never lowers the target.

Safeguard: when the room is more than the room hysteresis plus 1 °C below target (2 °C with the defaults), the burn is no longer left to wait for the next rest. The six-hour limit does not apply, stops may come 45 minutes apart, and low power at Low or Mid is judged from the room alone, not from the learned UA, after fifteen minutes without the room warming. Such a correction goes straight to the RISE target (75 °C) and turns RISE mode on, so it does not step up one degree at a time. The minimum burn time still applies, and a target already at the adaptive maximum is not stopped for.

When the room is more than 1 °C below target (RISE mode), a high target of 75 °C is used with no active corrections. In RISE mode UA and C are still learned but the K corrections are not. RISE ends when the room is within 0.3 °C of target. Enabling the feature during a run only observes that cycle. After the add-on starts, it waits 30 seconds for telemetry before falling back to the safe coolant target.

UA (heat loss in kW per °C of indoor–outdoor difference) is measured between two ignitions at least three hours apart where the room is back within 0.3 °C of where it began. Every ignition opens such a window, and the oldest one that fits gives the sample, so a first ignition at an unusual room temperature (for example right after a restart) does not hold learning back. Heat stored in the house while it warms up therefore does not count as loss. Windows longer than twelve hours, or with a mean indoor–outdoor difference below 8 °C, are skipped. A telemetry or sensor dropout of up to five minutes keeps the window. The open window is saved every five minutes and survives an add-on restart or update of up to ten minutes. The log says when a window starts, how many are open and how far the oldest has come, and when they are dropped. The first sample comes at the earliest at an ignition three hours or more after an earlier one at the same room temperature. UA is learned even while the feature is off. A UA learned by version 0.5.1 or older is discarded once, because that version counted warm-up heat as loss.

Turning the feature off restores your previous manual coolant target on the next idle. Learned values are kept in `/data/adaptive_model.json` across restarts. Use **Reset adaptive learning** to clear them.

The sidebar and Home Assistant show adaptive status (`off` / `learning` / `active`), mode (`continuous` / `intermittent` / `maximum` / `rise`), the last computed coolant target, the target confirmed by the heater, ignitions per day, and the learned UA and C.

The control unit keeps its own settings block. On startup the add-on reads that block and shows it on the add-on page and as Home Assistant entities: standby temperature (20–95 °C), auxiliary-heater mode and its standby temperature (80–95 °C), cabin-heater relay and the temperature that turns it on (30–60 °C), pump signal, pump-with-engine, pump-in-standby, and the alarm channel. Change them there. The change is written back to the same block, and bytes that were not changed stay as they were read. These values are not on the add-on configuration page, so that page cannot show a second copy. A write the heater does not accept while it is burning is tried again once the burner is idle. The room target, the run time, the cabin-temperature report, and the preheat duration are still ordinary run commands. They are not part of that stored block. Low supply voltage can still stop preheat and pump mode. Thermostat mode leaves voltage and coolant stops to the heater.

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
