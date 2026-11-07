# home-assistant-addons

Custom Home Assistant add-ons.

## Autoterm Flow 5

[autoterm_flow5](autoterm_flow5) replaces the control panel of an Autoterm Flow 5 coolant heater. One 5 V USB-UART adapter talks to the heater. The add-on publishes status, fuel use, fault codes, and the adjustable limits to Home Assistant over MQTT discovery.

Wiring and the entity list are in [autoterm_flow5/DOCS.md](autoterm_flow5/DOCS.md). The protocol notes follow public Flow 5 captures: 2400 baud 8N1, CRC-16/MODBUS with the high byte first, and panel commands for start, stop, and pump. The heater's stored coolant setpoint is not written; the add-on stops the burner at the limit set in Home Assistant.
