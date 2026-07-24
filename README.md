# sdr-pi-relay

A small buffering/self-healing relay that sits in front of `rtl_tcp` on a
Raspberry Pi running an RTL-SDR dongle, fixing several problems that show up
when serving `rtl_tcp` clients (e.g. SDR++) over WiFi.

## Why this exists

Stock `rtl_tcp`, run directly, has two real bugs that show up under normal
use over a WiFi hotspot:

1. It delivers IQ data in bursts (a regular ~500ms-period pattern of
   300-650ms gaps followed by catch-up dumps) instead of smoothly. Total
   throughput is fine (~94-100% of expected) — it's not data loss, it's
   pulsed delivery — but that reads as audible/visual stutter to any client
   expecting a steady stream.
2. It reliably wedges for minutes inside `rtlsdr_cancel_async()` after any
   client disconnects (reproduced 3/3 times in testing — the process
   doesn't crash, it just hangs, so nothing brings it back except killing
   it). Root cause is inside `librtlsdr`/`libusb`'s async-transfer
   cancellation, not something worth patching directly.

`rtl_tcp_relay.py` fixes both by sitting between `rtl_tcp` (moved to
`localhost` only) and the network: it paces the bursty data back out
smoothly, and force-restarts `rtl_tcp` proactively after every disconnect
so the wedge never gets a chance to matter. See git log for each fix's
history and rationale — they landed as separate, independently reviewable
commits on purpose.

A client (SDR++, on desktop or Android) needs **zero changes or awareness**
of any of this — it just connects to the relay's address:port exactly as
it would to a real `rtl_tcp`.

## Architecture

```
 SDR++ client  <--TCP:1234-->  rtl_tcp_relay.py  <--TCP:1235-->  rtl_tcp  <--USB-->  RTL-SDR dongle
   (any host)                  (0.0.0.0:1234)      (127.0.0.1     (patched,
                                                      only)         localhost only)
```

## Install (on the Pi)

1. Build `rtl_tcp` from `osmocom/rtl-sdr` tag `v2.0.2` with
   `patches/rtl_tcp-tcp_nodelay.patch` applied, install to
   `/usr/local/bin/rtl_tcp` (links against the distro's existing
   `librtlsdr.so.0` — no need to reinstall the library itself).
2. Copy `rtl_tcp_relay.py` to `/home/<user>/rtl_tcp_relay.py`.
3. Install `systemd/rtl_tcp.service` and `systemd/rtl_tcp_relay.service` to
   `/etc/systemd/system/`, adjusting `User=` and the relay's path if needed.
4. `systemctl daemon-reload && systemctl enable --now rtl_tcp rtl_tcp_relay`.

## Known limitations

- **Single client only** — this is inherent to stock `rtl_tcp`'s design (one
  dongle, one accept loop) and the relay mirrors it. Multiple simultaneous
  listeners would need the relay extended to fan out one upstream connection
  to N downstream clients (not implemented — not needed yet).
- **~4s reconnect tax** — the proactive wedge-recycle kills and restarts
  `rtl_tcp` after every single disconnect, even when it wasn't actually
  wedged that time, trading a small guaranteed delay for avoiding the
  multi-minute failure mode. Fine for occasional monitoring use; would be
  worth revisiting for anything more real-time/interactive.
- All simultaneous listeners share one tuner — frequency/gain/sample-rate
  changes from one client affect everyone connected.
