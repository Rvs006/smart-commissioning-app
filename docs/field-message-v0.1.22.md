# Field message for v0.1.22

Send this only after the v0.1.22 release page resolves and its portable ZIP is
attached.

```text
Hi both,

The site build is now v0.1.22:
https://github.com/Rvs006/smart-commissioning-app/releases/tag/v0.1.22

Use the Windows portable ZIP on that page. It keeps the settings and run history
already stored on the laptop.

This includes every v0.1.21 field-readiness fix plus one worker-lifecycle
hardening change: after a database outage, a live scan now gets a confirmation
window to resume its heartbeat before the app can mark it failed. A genuinely
stopped worker still becomes an explicit failed run and never a false success.

Please start with one supervised BACnet scan, then run the ten-minute MQTT/UDMI
capture. Stop remains available and partial evidence is retained.

Product team
```
