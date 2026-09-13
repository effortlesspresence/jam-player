# BLE reachability: the post-boot recovery window

**Status:** implemented (services_v2, September 2026). Applies to Pi-based
JAM Players only.

## The failure this prevents

A fielded JAM Player (firmware `7440d2d`) stops advertising BLE the moment
it decides it is online *and* registered. That decision was made with a
connectivity check that falls back to a bare TCP connect to public DNS, and
a bare TCP connect succeeds on networks that go nowhere:

- captive portals (hotel / guest WiFi with a sign-in page),
- guest networks that firewall our API and Tailscale,
- uplinks that hand out DHCP but have no working internet.

On such a network the player believes it is online, stops advertising, and
becomes unreachable by every path we have: the setup app cannot see it
(no BLE), support cannot reach it (no Tailscale), and it never contacts the
backend (no internet). The customer's only remedy was a replacement unit.

## The guarantee

**BLE provisioning runs for 15 minutes after every boot, unconditionally.**
Whatever the player believes about its network, a power-cycle makes it
reachable from the setup app for 15 minutes. After the window closes the
pre-existing rule resumes: BLE stays up while the player is offline or
unregistered, and stops only when it is online *and* registered.

Recovery procedure for a stranded player, on any firmware that includes
this change:

1. Power-cycle the JAM Player.
2. Within 15 minutes, open the setup app, open the player, tap
   **Change WiFi Network**.
3. Pick a working network (saved or new). The player reconnects and the
   new network becomes its top autoconnect choice.

## How it is built, and why each piece is shaped the way it is

### The window counts from when BLE actually came up

Fifteen minutes from power-on is not the guarantee; fifteen minutes in
which a phone can reach the player is. Anything that delays BLE's start
(a slow filesystem check, a bluetoothd restart, BLE crash-looping into its
start limit and being brought back) would otherwise quietly eat the window,
so the state manager takes the later of power-on and the moment
`jam-ble-provisioning` most recently became active (systemd's
`ActiveEnterTimestampMonotonic`, which shares a clock with `/proc/uptime`
on a device that never suspends). If systemd cannot answer, the power-on
rule stands alone: unknown never shrinks the window. systemd is only asked
once the power-on window has already closed, so this costs nothing for the
first fifteen minutes. A side effect worth knowing: a BLE restart later in
the day reopens a window, which is the availability-first outcome.

### The window is measured from `/proc/uptime`
`common/system.py: seconds_since_boot()`. Not wall-clock arithmetic:
`fake-hwclock` restores a stale saved time early in boot and NTP later
steps the clock by hours, so "now minus boot timestamp" can jump or go
negative mid-boot (this is exactly what hid a 34-hour outage in one
incident's journal). `/proc/uptime` is monotonic, unaffected by either, and
counts from *boot* rather than process start, so the window is correct
even if `jam-ble-state-manager` crashes and restarts. If the file cannot
be read the helper returns `0.0`, i.e. "just booted", which keeps the
window **open**. Every failure mode fails toward reachability.

### The window check ignores the connectivity result on purpose
`jam_ble_state_manager.py: _should_ble_run()` returns `True` inside the
window *before* consulting `is_online` or registration. A false "online"
from a captive portal is the precise input that used to strand players; it
must not be able to close the window.

### The window closes itself through the existing periodic connectivity loop (15 s)
`_periodic_connectivity_check()` already re-evaluates `_should_ble_run` on
every tick while online, because registration can complete via the app
without any connectivity transition. The window closing is the same kind
of event, so no extra timer exists. The close is logged once per boot.

### `jam-boot-check` never stops BLE
It used to stop BLE at boot for "network up + registered" players as a
safety net. That is the exact moment the window matters most, so
`jam_boot_check.py` now only *ensures* BLE is running and leaves every
stop decision to the state manager.

### Cost, accepted deliberately
Every player spends 15 minutes of radio time after every boot, including
the nightly reboot. We chose that over "unreachable is possible".

## Autoconnect priority: the network you chose last wins

NetworkManager picks among saved WiFi profiles by
`connection.autoconnect-priority`; nothing in our code ever set it, so a
player with several known networks reconnected to whichever NM preferred.

`common/network.py: promote_wifi_connection_priority()` runs after every
successful user-driven connect (new network, saved network, or
re-selecting the current one) and sets that profile's priority to one
above the highest of the others, leaving the rest untouched so their
relative order is preserved. At the ceiling (900, inside NM's ±999 range)
it renumbers the others compactly in their existing order and puts the
chosen profile on top. It is best-effort: the device is already connected
when it runs, and a failed priority write is logged, never raised, and
never turns a successful connect into a reported failure.

## The SSH private key stays off the air once registered

The BLE Device Info characteristic carries the device's SSH private key
because the setup app forwards it to the backend in the **register** call;
that is how the backend obtains the key for app-registered players. It is
therefore a registration credential: needed before registration, redundant
after. With BLE now up for 15 minutes after every boot on every fielded
player, and the characteristic readable without pairing, the key is sent
only while the player is unregistered and is an **empty string** once it
is registered (`jam_ble_provisioning.py: _send_device_info_chunked`).

The field is always present. Both fielded apps declare `sshPrivateKey` as
a required, non-optional string; omitting it would make Device Info
undecodable and leave the player un-setup-able from the app, which is the
failure this whole document exists to prevent. Being announced does not
change this: announce ≠ registered, and an announced-but-unregistered
player still needs to be registered from the app.

## Mobile-app side (jam-sphere, Android + iOS)

A **Change WiFi Network** action on the player detail screen scans for
`JAM-PLAYER-<last 5 of deviceUuid, uppercased>`, connects, verifies the
UUID, and reuses the existing WiFi screen (saved networks, saved or new
password). If the player is not advertising within the scan timeout, the app says so
and tells the user to power-cycle the player and retry as soon as it starts
up. That instruction is honest for both firmwares: new firmware accepts BLE
for 15 minutes after boot; `7440d2d` accepts it only for the seconds between
boot and its connectivity check passing (or indefinitely while offline), so
the apps deliberately do not promise a 15-minute window. No BLE payload or
characteristic changed; the apps remain compatible with `7440d2d`.

## Tests

`src/jam_player/services_v2/tests/` — stdlib `unittest`, written to run **on
a JAM Player**, not on a developer machine: they import the real service
modules (dbus, GLib, sdnotify, requests, nacl). They never touch the D-Bus
system bus, NetworkManager or the network — every external call is patched —
so they are safe to run on a fielded, registered player.

```
sudo /opt/jam/venv/bin/python3 -m unittest discover -s tests -t . -v
```

from the `services_v2` directory of the checkout on the device, or
`tests/run_on_device.sh`, which resolves the venv and directory for you.

## Hardening from the adversarial review (September 2026)

Each item below closes a way the guarantee could still have failed. All are
in `jam_ble_state_manager.py` unless noted.

- **BLE is re-asserted on every periodic tick (15 s)**, not only on connectivity
  transitions. A BLE exit that systemd did not restart (a half-installed
  venv tripping `StartLimitBurst`, a manual `systemctl stop`, the exit-0
  path) used to stay down until the next transition or reboot. `start_service`
  now runs `systemctl reset-failed` first so a unit that hit its start
  limit can come back. The BLE units are deliberately NOT ordered after
  `jam-venv-repair.service`: ordering cannot block on a failed repair, but
  a hung or long one would hold BLE for up to 35 minutes while the boot-
  relative window drained. BLE waits on nothing at boot; the re-assert
  above brings it back once a repair finishes.
- **Only a reachable backend can stop BLE.** "Online" via the Cloudflare or
  Google TLS fallback means the internet answers but our API did not. That
  is the firewalled-guest-network case, and the player can reach neither the
  backend nor support, so it is treated as degraded and BLE stays up.
- **The periodic loop cannot die.** PyGObject silently removes a timeout
  source whose callback raises; one stray exception (a dying card making
  `Path.exists()` raise) would have ended offline detection for the boot.
  The callback body is wrapped and the source always returns `True`. The
  watchdog pinger never raises for the same reason, and the initial
  synchronous connectivity checks feed the watchdog by hand so a network
  with black-holed resolvers cannot get the service killed at boot.
- **`.internet_verified` is cleared at the start of every boot.** It is a
  persistent file that only the offline path deleted, so after a
  power-cycle onto a captive portal the player reported `isConnected: true`
  for the 40 to 90 seconds the first checks take, and the fielded apps route
  a registered, "connected" player straight to its detail screen, never to
  WiFi setup. Nothing about the previous boot's network is evidence about
  this one.
- **The provisioning-confirm write is a no-op on a registered player**
  (`jam_ble_provisioning.py`). The recovery window puts an unauthenticated
  GATT service on every registered, content-playing player for 15 minutes
  after each boot. The WiFi-credentials write is the recovery path and stays.
  The confirm write restarted the display and has no legitimate use once
  `.registered` exists: the apps write it exactly once, before that flag is
  created. The screen-id write is deliberately **not** gated: the apps' link
  step writes it *after* confirm, as the fast path while the WebSocket is
  still coming up, so gating it broke first-time linking (caught in review of
  the diff). Its blast radius is small regardless: content is fetched by the
  backend's screen assignment, not by this file, so a rogue write can at
  most trigger a refetch and a wrong local "linked" indication.
- **A dead bluetoothd no longer leaves a zombie advertiser.** The BLE unit is
  `Requires=`, `PartOf=` and `After=bluetooth.service`, and three consecutive
  D-Bus failures on the adapter power check make the process exit for a
  systemd restart. The rfkill unblock now runs before the adapter lookup, so
  a soft-blocked radio is unblocked instead of crash-looping on "no adapter".
- **Boot-check no longer blocks on BLE startup** (`jam_boot_check.py` uses
  `systemctl start --no-block`); BLE's 10 to 30 second `Type=notify` startup
  was delaying announce and the content manager on every boot.
- **Priority promotion runs off the connect thread** (`common/network.py`),
  as a single `nmcli` listing with escape-safe parsing, so the "connected"
  status reaches the app immediately regardless of how many profiles are
  saved.

### The window never closes on an active session

A session started at minute 14 used to be cut off mid-connect: the app saw
read errors and then its own 60-second timeout, and the player could be left
half-configured. The window is now held open while either is true:

- a phone is connected to our GATT server (asked of BlueZ directly, so it
  stays correct across a restart of either service), or
- a WiFi connection started from the app is still running
  (`jam-ble-provisioning` raises `/run/jam/ble_session_active` for the life
  of the attempt; tmpfs, so no card write and nothing survives a reboot).

There is deliberately no overall deadline on the hold: a session that is
genuinely under way is never cut off, whenever it started. Being unreachable
is the failure this whole design pays to avoid, so availability wins over
tidiness here. Neither signal can become a permanent hold on its own: the
BlueZ answer clears itself when the phone disconnects or the link times out,
and the WiFi-connect marker is ignored once it is older than five minutes,
because a connect attempt is bounded by nmcli's own timeouts and a marker
older than that means the attempt died without clearing it. Any failure to
answer the question resolves to "no session", so a D-Bus problem can never
hold the radio open.

Not a concern after review: nothing can delete a `JamPlayer` record. There is
no delete endpoint, no delete call anywhere in the backend or web app, and no
cascade that reaches the table, so the "backend forgot a registered player"
case this section previously worried about is not reachable through the
product.
