# Field message for v0.1.21

Send this only after the v0.1.21 release page resolves and its portable ZIP is
attached.

```text
Hi both,

The site-readiness build is v0.1.21:
https://github.com/Rvs006/smart-commissioning-app/releases/tag/v0.1.21

Use the Windows portable ZIP on that page. It keeps the settings and run history
already stored on the laptop.

This carries the v0.1.20 BACnet deadlock and network-timeout fixes, plus the
final release gate: dead worker runs recover instead of staying on screen
forever, MQTT capture stops at the requested deadline, and the timestamp result
now separates confirmed clock faults from an ordinary one-hour publishing gap.

Please start with one supervised BACnet scan, then run the ten-minute MQTT/UDMI
capture. Stop remains available throughout and partial evidence is retained.

Product team
```
