# JAM Player Bench Test Plan

Living checklist of the on-device tests that verify the reliability changes made
from September 2026 onward (see the audit report for the findings each test
closes). Unit tests prove logic; these prove behaviour on a real player, on a
real network, with a real phone. Add a case here for every change that alters
what a customer or a network can observe.

**Status legend:** `[ ]` not run · `[x]` passed · `[!]` failed (link the issue) ·
`[~]` passed with a noted caveat.

Every command below is a single line and is meant to be pasted as-is.

---

## 0. Bench prerequisites

- **A Pi JAM Player on the branch under test.** Point it at the branch and pull:
  `echo testing | sudo tee /etc/jam/config/environment && sudo systemctl start jam-update && journalctl -u jam-update -f`
  Confirm the commit: `cat /etc/jam/version.txt`. (Fielded default is `main`;
  a file containing `prod` or `false` also means `main`.)
- **Run the on-device unit suites first** (no stubs — these need Pi libraries):
  `cd /opt/jam/services && sudo tests/run_on_device.sh`
  Everything must pass, including the suites that only skip on a laptop
  (`test_api_health_probe` device-only cases, `test_update_ble_session_guard`,
  `test_ble_recovery_window`, `test_captive_portal`, `test_offline_quiet`).
- **Watching logs.** The SD card keeps WARNING and above only
  (`MaxLevelStore=warning`); INFO ships to the backend's Logs & Errors panel when
  online. `journalctl -u <unit> -f` therefore shows the decisive lines for these
  tests, which are deliberately WARNING (e.g. "KEEPING the profile",
  "forgetting profile", "deferring BLE restarts").
- **Phones:** the current JAM Setup app on Android *and* iOS; ideally one phone
  kept on an older store build to prove the `failed` fallback path.
- **Network fixtures:** a good WiFi with internet; a WiFi whose router has its WAN
  unplugged (associates, no internet); a dual-band pair named like `Shop` and
  `Shop-5G`; a spare router (WAN unplugged) with a free LAN port for the
  dead-uplink ethernet case.
- **Backend visibility:** the JAM Sphere super-admin dashboard for the bench
  device, and API Gateway access logs / CloudWatch `Count` for `/jam-players/health`.

---

## 1. Reusable setup recipes

### R1 — Stale clock (the warehouse device)

A Pi 4 has no RTC. At boot `fake-hwclock` restores the time saved at the last
shutdown; if the device sat boxed for months that time is months old, and every
connectivity probe is a *verified* TLS handshake whose certificate validity is
judged against the device clock — so certificates issued after that stale date
read "not yet valid" and the probes fail even on a perfect network. To recreate
it faithfully:

1. Make the device look never-provisioned and offline. Unplug ethernet, then
   delete every WiFi profile:
   `sudo nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-11-wireless"{print $1}' | xargs -r -n1 sudo nmcli connection delete`
2. Set the clock into the past and persist it as the "last shutdown" time
   (chrony must be stopped or it corrects you immediately):
   `sudo systemctl stop chrony && sudo date -s "2024-06-01 10:00:00" && sudo fake-hwclock save && date`
3. Cold-boot like an unboxing: `sudo poweroff`, pull power, wait 10 s, plug in.
   (`fake-hwclock load` only moves the clock *forward*; at boot the kernel starts
   near the epoch / systemd's build date, so the saved 2024 time is applied.
   chrony starts but has no network, so nothing corrects it.)
4. Prove the failure mode exists before you provision anything:
   `date; chronyc tracking | grep -E "Leap|System time"; echo | openssl s_client -connect 8.8.8.8:443 2>&1 | grep -iE "verify (error|return)"`
   Expect the stale date, `Leap status : Not synchronised`, and
   `verify error:num=9:certificate is not yet valid`.
5. Provision WiFi from the app and observe (tests B1/B2).

**How far back to set it.** A certificate only reads "not yet valid" if it was
*issued after* the device's stale date. Google's `8.8.8.8` cert rotates about
every 90 days, so 6 months back fails it — but Cloudflare's and our own ACM
certificate are roughly a year long and may still be inside their validity
window, in which case the backend probe *succeeds* and you never reach the
`none` verdict. **Set ≥ 2 years back to guarantee every probe fails.** Then also
run a realistic 6–9 months back and record which probes fail with
`echo | openssl s_client -connect <api-host>:443 -servername <api-host> 2>&1 | grep -iE "verify (error|return)"`
— both are real fleet scenarios.

**Restore:** `sudo systemctl start chrony && sleep 15 && chronyc tracking | grep Leap`
(steps the clock; Debian's default `makestep 1 3` applies — our chrony config is
a `conf.d` drop-in, not a replacement).

### R2 — NTP unreachable

Faithful: block outbound UDP 123 at the test router's firewall. Quick
simulation on the device (same effect on `check_chrony_sync()`):
`sudo systemctl stop chrony` before provisioning, `sudo systemctl start chrony`
to restore.

### R3 — Dead-uplink ethernet

Spare router with its WAN cable unplugged. Plug the player's ethernet into a LAN
port: it gets DHCP and a default gateway, but there is no internet behind it.
NetworkManager prefers ethernet for the default route (metric 100 vs WiFi 600),
so every probe leaves via the dead cable.

### R4 — Wrong password / R5 — Dual-band names

R4: any good WiFi, deliberately mistyped password. R5: two SSIDs where one is a
prefix of the other (`Shop`, `Shop-5G`), with a saved good profile for the longer
one.

### R6 — "API not serving" without touching the backend

The BLE-stop rule needs *recent proof from a signed API call* (the heartbeat
stamps `/run/jam/api_last_ok`). Simulate an API that has stopped serving by
stopping the thing that produces the proof: `sudo systemctl stop jam-heartbeat`
(evidence goes stale after 15 min) or `sudo rm -f /run/jam/api_last_ok` (no
evidence at all). Restore with `sudo systemctl start jam-heartbeat`. Read the
stamp with `stat -c %y /run/jam/api_last_ok`.

### R7 — An update while a phone is mid-setup

With a phone connected over BLE (on the Device Details / location step, *after*
WiFi connected), trigger an update: `sudo systemctl start jam-update && journalctl -u jam-update -f`.
To exercise the bluetooth-restart branch, first make `main.conf` differ:
`echo "# bench" | sudo tee -a /etc/bluetooth/main.conf`.

---

## 2. Test cases

### A — WiFi profile lifecycle (audit fixed items 4, 5, 6)

**A1 · Wrong password leaves nothing behind** `[ ]`
Setup R4. Steps: connect from the app with the wrong password. Expected: app
shows *Invalid password* within ~10 s. Verify:
`nmcli -f NAME,TYPE connection show | grep jam-wifi` shows **no** profile for
that SSID, and `journalctl -u jam-ble-provisioning -b --no-pager | grep -c "discarding connection profile"` ≥ 1.
Then retry with the correct password → connects (A5 covers this).

**A2 · A typo never deletes the working profile** `[ ]`
Setup: device connected to SSID X on a saved good profile. Steps: from the app,
choose X again with a wrong password. Expected: *Invalid password*; **the device
stays online on X** (or returns to it within seconds). Verify:
`nmcli -f NAME,ACTIVE connection show | grep jam-wifi` still lists the original
profile, active.

**A3 · Exact SSID match, not substring** `[ ]`
Setup R5 (good saved profile for `Shop-5G`). Steps: connect to `Shop` with a
wrong password. Expected: `Shop-5G`'s profile is untouched and still connects.

**A4 · Slow or absent DHCP keeps a still-activating profile** `[~]`
Setup: a network whose DHCP server is disabled. Steps: connect from the app.
Expected: app shows *timeout* at ~30 s; the profile is **kept** while NM reports
it activating (`journalctl -u jam-ble-provisioning -b --no-pager | grep -c "still activating"` ≥ 1).
Caveat by design: if NM later gives up, that profile lingers as an orphan —
accepted in exchange for never destroying a valid slow activation.

**A5 · Correct retry after a wrong attempt** `[ ]`
Steps: A1 then the right password. Expected: connects; exactly one `jam-wifi-*`
profile for the SSID.

### B — Forget-on-`none` corroboration (audit #8, Decision C)

**B1 · Stale clock, NTP reachable → accurate verdict, no forget** `[ ]`
Setup R1 (2 years back), NTP open. Steps: provision a good WiFi from the app.
Expected: the device waits for chrony (≤ 15 s), the clock steps, classification
returns `backend`, app shows **Connected**; announce follows. Verify:
`date; chronyc tracking | grep Leap` (now correct / `Normal`);
`nmcli -f NAME connection show | grep -c jam-wifi` = 1;
`journalctl -u jam-ble-provisioning -b --no-pager | grep -ciE "forgetting profile|KEEPING the profile"` = 0.
Record time from Connect to the terminal status (B5).

**B2 · Stale clock, NTP blocked → profile kept, then self-recovers** `[ ]`
Setup R1 + R2. Steps: provision a good WiFi. Expected: after ~15 s wait the
verdict is `none`; app shows *no usable internet* then *failed*; the device logs
`KEEPING the profile: clock is not NTP-synced`; the profile **exists**. Then
restore NTP (R2). Expected: chrony steps the clock, the state manager's next
ticks see the backend, `.internet_verified` appears and heartbeat/announce run —
**without re-provisioning**. Verify:
`ls -la /etc/jam/device_data/.internet_verified; systemctl is-active jam-heartbeat`.
This is the whole point of #8: a fielded 7440d2d device would have needed
nothing here either; the pre-fix tree would have deleted the profile and looped.

**B3 · Dead-uplink ethernet + good WiFi → kept; unplug → online** `[ ]`
Setup R3 with the clock synced (normal bench state). Steps: provision a good
WiFi from the app. Expected: verdict `none` (probes went out the cable); app
shows *no usable internet*/*failed*; log says `KEEPING the profile: ethernet
holds the default route`; profile exists. Unplug the cable. Expected: online via
WiFi within ~15–30 s. Verify: `ip route show default` (WiFi device) and
`systemctl is-active jam-heartbeat`. Known limitation while the dead cable stays
plugged in: the device remains offline — that needs the NM `[connectivity]`
follow-up (open decision).

**B4 · Synced clock + genuinely dead WiFi → forgetting still happens** `[ ]`
Regression guard for the captive-portal behaviour. Setup: WAN-unplugged WiFi
router, device clock already synced (it was online this boot), no ethernet.
Steps: connect from the app. Expected: `none` → log `forgetting profile` → app
*no usable internet*/*failed*. Verify: `nmcli connection show | grep -c jam-wifi` = 0.

**B5 · Timing budget** `[ ]`
Both apps poll for a terminal status every 2 s for 60 s. Measure Connect → terminal
status in B2 (the slow path: connect + 15 s wait + ~18 s failing probes). Expected
< 60 s. Record the number. If it ever exceeds 60 s the app shows *timed out*
while the device still finishes correctly — acceptable but note it.

**B6 · Normal path is unchanged** `[ ]`
Synced clock, good WiFi. Expected: no measurable extra delay (the clock wait
returns immediately; no `chronyc burst` in `journalctl -u chrony -b`).

**B7 · Old app build** `[ ]`
Repeat B2 with an older store build of the app. Expected: it shows a generic
failure (the trailing `failed`), never a stuck spinner.

### C — BLE service health (audit fixed items 1, 2, 3; tick cadence)

**C1 · No crash loop** `[ ]`
After boot, wait 2 min. Verify:
`systemctl show jam-ble-provisioning -p NRestarts -p ActiveEnterTimestamp; journalctl -u jam-ble-provisioning -b --no-pager | grep -ciE "unreachable three times|NameError"`
Expected `NRestarts=0`, count 0, and the player is continuously visible in the
app's scan list. (The pre-fix tree restarted every ~20 s.)

**C2 · State manager runs clean; a crashed BLE comes back** `[ ]`
Verify `journalctl -u jam-ble-state-manager -b --no-pager | grep -c TypeError` = 0.
Then, online and inside the boot window: `sudo kill -9 $(systemctl show -p MainPID --value jam-ble-provisioning)`.
Expected: back within one tick (≤ 15 s) — `systemctl is-active jam-ble-provisioning`.

**C3 · The window closes at 15 min for a registered, online player** `[ ]`
Registered device, online, heartbeat healthy. Expected: BLE stops advertising
shortly after 15 min from boot and the journal shows `Post-boot BLE recovery
window closed`. Verify advertising: `busctl --system get-property org.bluez /org/bluez/hci0 org.bluez.LEAdvertisingManager1 ActiveInstances` (1 = advertising, 0 = stopped).

**C4 · Offline detected in ~30 s, online in ≤ 15 s** `[ ]`
Registered, online, past the window (BLE off). Power off the AP. Expected: within
~30–45 s the device goes offline (2 misses × 15 s), BLE advertises again, the
scan list shows it with `isConnected` clear. Power the AP back on. Expected:
online within ~15 s of association; BLE stops again (fresh API stamp needed — see D3).

**C5 · The newest network wins autoconnect** `[ ]`
Two saved networks. Connect to the second from the app. Verify:
`nmcli -f NAME,connection.autoconnect-priority connection show | grep jam-wifi`
— the newest has the highest priority.

### D — Connectivity probe and API health (Decision A)

**D1 · No more health GETs from an updated device** `[ ]`
Backend side: API Gateway access logs / CloudWatch `Count` for
`GET /jam-players/health` from this device's IP drops to **zero** after the
update (was ~12,300/day). Device side sanity:
`sudo /opt/jam/venv/bin/python3 -c "import sys;sys.path.insert(0,'/opt/jam/services');from common import network;print(network.check_internet_connectivity())"`
→ `(True, 'jam_backend')` while online.

**D2 · Heartbeat stamps API health** `[ ]`
`stat -c %y /run/jam/api_last_ok` advances every ~5 min while online.

**D3 · No API proof → BLE stays up past the window** `[ ]`
Setup R6 (`stop jam-heartbeat`), registered, online, past 15 min. Expected: once
the stamp is > 15 min old, BLE starts advertising again even though the edge is
reachable (C3's `ActiveInstances` → 1). Restore heartbeat → next stamp → BLE
stops again. This is the "keep advertising during our outage" behaviour on the
new signal.

**D4 · No evidence at all → BLE up** `[ ]`
`sudo rm -f /run/jam/api_last_ok` on a registered online player past the window.
Expected: BLE advertises within a tick; returns to stopped after the next heartbeat.

**D5 · The connect-flow classifier still uses the real HTTP check** `[ ]`
During any app connect (B6), the backend sees exactly one `GET /jam-players/health`
per attempt — event-driven, not periodic.

### E — Updater respects a live BLE session (audit #9)

**E1 · Unchanged `main.conf` → bluetooth is not restarted** `[ ]`
Run an update with no BLE config change. Verify
`journalctl -u jam-update -b --no-pager | grep -c "Restarted bluetooth"` = 0 and
`journalctl -u bluetooth -b --no-pager | grep -c "Started"` did not increase.

**E2 · Update during a live session defers BLE restarts; registration completes** `[ ]`
Setup R7 (phone connected, `main.conf` modified). Expected: journal shows
`A BLE setup session is in progress; deferring BLE restarts`; the phone's link
**survives**; finish registration in the app; after the phone disconnects the
journal shows `BLE setup session ended ...; proceeding` and bluetooth restarts once.

**E3 · A session longer than 5 min is not held hostage** `[ ]`
Keep the phone connected > 5 min during R7. Expected: warning `BLE setup session
still active after 300s; leaving BLE units (and bluetoothd) untouched`; every
other service restarts; the update completes; BLE picks up the new code at the
next boot.

**E4 · First-connect auto-update with the app in hand** `[ ]`
Fresh (unregistered) device with an update pending on the branch. Provision WiFi
and immediately proceed through registration. Expected: no disconnect, no
"Updating…" takeover of the BLE flow before registration completes.

### F — Reporting and display (MAC / network status)

**F1 · Setup screens show the MACs** `[ ]`
Any non-content screen shows `Wi-Fi MAC:` and (if present) `Ethernet MAC:` under
the device UUID. Compare with `nmcli -t -f GENERAL.TYPE,GENERAL.HWADDR device show`.

**F2 · Backend shows MACs and network** `[ ]`
After announce (fresh) or the first heartbeat after boot, both dashboards show
Wi-Fi MAC, Ethernet MAC, and Network (SSID / Ethernet) for the bench device.

**F3 · Offline shows "last connected"** `[ ]`
Power off the AP for > 10 min. Expected: dashboard flips to offline with the last
network shown; no false-offline while the device is healthy.

### H — Content pipeline integrity (audit #12, #13, #14)

**H1 · A kill mid-download never publishes a truncated file** `[ ]`
Setup: publish a layout with a large (>30 MB) video; watch `ls -la /opt/jam/content/live_media/`.
Steps: while `<hash>.mp4.part` is growing, `sudo systemctl restart jam-content-manager`.
Expected: the `.part` disappears (SIGTERM handler or startup sweep); no `<hash>.mp4`
exists until the download completes; after it completes a `<hash>.mp4.size` sidecar
matches `stat -c %s <hash>.mp4`; the scene plays. Verify:
`ls -la /opt/jam/content/live_media/ | grep -E "part|size"; journalctl -u jam-content-manager -b --no-pager | grep -cE "Removed stale partial|exiting cleanly"`.

**H2 · A legacy truncated file is healed on the first load** `[ ]`
Setup: with the manager stopped, truncate a referenced video in place:
`sudo systemctl stop jam-content-manager && f=$(ls /opt/jam/content/live_media/*.mp4 | head -1) && sudo truncate -s 50% "$f" && sudo rm -f "$f.size" && sudo systemctl start jam-content-manager`
Expected: journal shows `failed validation ... will re-download`; the file is
re-downloaded in full and gets a sidecar; mpv plays it without freezing.

**H3 · One failing asset → partial publish, no cleanup, self-retry** `[ ]`
Setup: block one asset's CDN URL at the router (or `/etc/hosts` the media host to 127.0.0.1
for ONE load), publish a change touching two scenes.
Expected: journal `could not be downloaded; published the rest, skipping media cleanup, retry owed`;
the reachable scene updates; the previous asset of the blocked scene is **still on disk**;
retries at 30 s, 60 s, 120 s … (`Retrying the content load that did not complete`).
Unblock → `Owed refresh completed` **without republishing**; cleanup runs. Verify with
`journalctl -u jam-content-manager -f`.

**H4 · Re-linking the same screen keeps content (offline)** `[ ]`
Setup: registered player with content, AP powered off. Steps: from the app, re-run the
link step for the same screen. Expected: content keeps playing; no `Cleared` line in
`journalctl -u jam-content-manager -b --no-pager | grep -c Cleared`; `screen_id.txt`'s mtime
did not change (`stat -c %y /etc/jam/device_data/screen_id.txt`). (Pre-fix: the WiFi setup
screen appeared.)

**H5 · A relink never blanks the board** `[ ]`
Steps (online): link the player to a different screen. Expected: the old content keeps
playing until the new set finishes downloading, then swaps in atomically — no "Waiting
for content" gap, no blank frame. Steps (offline): AP off, relink over BLE. Expected: the
old content keeps playing indefinitely; when the AP returns, the new screen's content
downloads and swaps in. Product rule: an offline player never loses its last content.

**H6 · SET_SCREEN_ID no longer restarts the manager** `[ ]`
Steps: relink via the web dashboard while a download is in flight. Expected:
`journalctl -u jam-ws-commands -b --no-pager | grep -c "nudging content manager"` ≥ 1 and
`systemctl show jam-content-manager -p NRestarts` unchanged; the in-flight download finishes.

### G — Gates

**G1 · On-device unit suites green** `[ ]` — `cd /opt/jam/services && sudo tests/run_on_device.sh`
**G2 · Undefined-name gate** `[ ]` — passes on the device and on a laptop:
`cd src/jam_player/services_v2 && python3 -m unittest tests.test_no_undefined_names`

---

## 3. Pending — add cases when the change lands

- **Content pipeline CRITICAL (lightdm storm):** delete the first scheduled
  scene's media file while others exist → expected no `systemctl restart lightdm`,
  no watchdog kill, next playable scene plays; restore file → resumes.
- **Timezone applied live:** change the location timezone → schedule flips
  without a reboot.
- **Updater validate-before-promote, hardware watchdog, health-monitor cooldown.**
- **Mobile:** Android link-loss message; verify-after-`connected` on old firmware;
  Location Services gate; enterprise-network detection; ethernet "continue
  without WiFi".
- **Polling:** network-status send-on-change; registration via WS push;
  update-poll / outlet cadence.
