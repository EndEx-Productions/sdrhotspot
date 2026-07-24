# sdrhotspot

A Raspberry Pi + RTL-SDR dongle that creates its own WiFi hotspot and serves
a live IQ stream to any `rtl_tcp`-compatible SDR client (e.g. SDR++, desktop
or Android) that connects to it — no internet/home-network dependency
needed in the field. Built for receive-only spectrum monitoring.

## Hardware used

- Raspberry Pi 3 Model B (Rev 1.2), Raspberry Pi OS (Trixie)
- Nooelec NESDR SMArt v5 (RTL2832U + R820T tuner)
- A **good** 5V/2.5A+ power supply and cable — a marginal one will cause
  intermittent undervoltage/brownouts that produce symptoms easy to mistake
  for software bugs. Verify with `vcgencmd get_throttled` (should stay
  `0x0` under load) and `dmesg | grep -i voltage`.

## What's in this repo

- `hotspot/` — the Pi's own WiFi access point (hostapd + dnsmasq + a static
  IP for `wlan0`) and the kernel-module blacklist needed for the dongle.
- `rtl_tcp_relay.py` + `patches/` — a relay that sits in front of `rtl_tcp`
  fixing several real bugs in it (see "Why the relay exists" below).
- `systemd/` — unit files for `rtl_tcp` and the relay.

## Full setup, in order

### 1. RTL-SDR dongle bring-up

The Realtek chip in these dongles is also used by DVB-T TV tuner cards, and
Linux will load the wrong (TV tuner) driver for it by default, which
prevents SDR software from claiming the device. Blacklist it — copy
`hotspot/modprobe.d/*.conf` to `/etc/modprobe.d/` (also blacklists an
unrelated WiFi dongle driver, `8192cu`, that caused a similar conflict on
this build; skip that one if you don't have that hardware) and reboot.

Install the SDR tools: `apt install rtl-sdr`. No manual udev rule is
needed for USB permissions — `librtlsdr0` ships its own
(`/usr/lib/udev/rules.d/60-librtlsdr0.rules`), which is what actually grants
device access.

Verify: `rtl_test` should detect the dongle and show tuner info without
permission errors.

### 2. WiFi hotspot bring-up

`apt install hostapd dnsmasq`.

- Copy `hotspot/wlan0-static-ip.service` to `/etc/systemd/system/` — gives
  `wlan0` a fixed `192.168.4.1/24` before `hostapd`/`dnsmasq` start, since
  neither will come up cleanly on an interface with no address yet.
- Copy `hotspot/hostapd.conf` to `/etc/hostapd/hostapd.conf`. **Replace
  `wpa_passphrase=CHANGE_ME` with a real passphrase before deploying** —
  the placeholder is intentional, this file is committed to a public repo.
  Point `/etc/default/hostapd`'s `DAEMON_CONF` at this path. `channel=11`
  was chosen after a site survey (`iw dev wlan0 scan`) showed the default
  channel sitting adjacent to a strong neighboring AP — re-survey and pick
  a clean channel for your own environment rather than copying this value
  blindly.
- Copy `hotspot/dnsmasq.conf` to `/etc/dnsmasq.conf` (this replaces the
  whole file — it's a hotspot-dedicated config, not merged with a stock
  one). Hands out `192.168.4.2`-`192.168.4.20` to clients.
- `systemctl daemon-reload && systemctl enable --now wlan0-static-ip hostapd dnsmasq`.
- WiFi regulatory domain was set to `US` (`raspi-config nonint
  do_wifi_country US`) — set this to your actual country; it affects which
  channels/power levels are legal.

Verify: another device should see the `SDR-Pi` SSID, be able to join, and
get an address in the `192.168.4.x` range.

### 3. `rtl_tcp` + relay (this is the part with the interesting bug fixes)

See "Why the relay exists" below for the rationale — this section is just
the steps.

1. Build `rtl_tcp` from `osmocom/rtl-sdr` tag `v2.0.2` with
   `patches/rtl_tcp-tcp_nodelay.patch` applied, install to
   `/usr/local/bin/rtl_tcp` (links against the distro's existing
   `librtlsdr.so.0` — no need to reinstall the library itself; `apt install
   librtlsdr-dev libusb-1.0-0-dev cmake build-essential` for the build
   deps).
2. Copy `rtl_tcp_relay.py` to `/home/<user>/rtl_tcp_relay.py`.
3. Install `systemd/rtl_tcp.service` and `systemd/rtl_tcp_relay.service` to
   `/etc/systemd/system/`, adjusting `User=` and the relay's path if
   needed.
4. `systemctl daemon-reload && systemctl enable --now rtl_tcp rtl_tcp_relay`.

Verify: from another device on the hotspot, `rtl_tcp` should be reachable
at `192.168.4.1:1234`.

### 4. Client

Install SDR++ (desktop or Android — no modification needed, it's used
stock). Join the `SDR-Pi` WiFi network. Add an rtl_tcp source pointed at
`192.168.4.1:1234`.

## Why the relay exists

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
   (any host,                  (0.0.0.0:1234)      (127.0.0.1     (patched,
    on SDR-Pi WiFi)                                  only)         localhost only)
```

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
