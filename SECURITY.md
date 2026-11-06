# Security policy

Please do **not** open public issues for security problems.

Report vulnerabilities privately via GitHub: **Security → Report a vulnerability**
(private vulnerability reporting). You will get a reply in the advisory thread.

Supported version: the latest commit on `main` / the `latest` container image.

Scope notes:

* The service is meant for a trusted LAN. The ESPHome Native API without
  `ESPHOME_ENCRYPTION_KEY` lets anyone in the LAN toggle the forward switches.
* Station credentials (WU `ID`/`PASSWORD`, Weathercloud `wid`/`key`) must never appear
  in logs, `data/` files or MQTT; any leak is a security bug.
