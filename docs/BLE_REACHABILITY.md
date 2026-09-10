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

### The window closes itself through the existing 7-second loop
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
password). If the player is not advertising within the scan timeout, the
app says so honestly and gives the power-cycle instruction above. That
copy is correct for old and new firmware alike, so the apps do no version
detection. No BLE payload or characteristic changed; the apps remain
compatible with `7440d2d`.

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
