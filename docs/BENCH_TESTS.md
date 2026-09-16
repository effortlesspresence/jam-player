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
- **Image contents (settles the customer firewall whitelist).** Whether `mpv` and `tailscale` ship in
  the image, or are apt-installed on first boot, decides whether the Debian and Raspberry Pi mirrors
  plus the Tailscale apex are steady-state whitelist entries or a recovery-only footnote. If the
  install line never appears across any boot, the image has them and three entries come off the list
  customers get.
```
journalctl -u jam-boot-check --no-pager | grep -iE "MPV not installed|Tailscale not installed|install.sh"; echo "--- present now? ---"; command -v mpv tailscale
```

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
WiFi connected), trigger an update: `sudo systemctl start jam-update`. INFO lines
are not stored on the card, so watch service (re)start times instead:
`for u in jam-update jam-player-display jam-heartbeat jam-tailscale jam-ble-state-manager bluetooth; do echo "$u $(systemctl show -p ExecMainStartTimestamp -p ActiveEnterTimestamp -p Result --value $u | tr '\n' ' ')"; done`.
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

Rule since 2026-09-16: the updater restarts every non-Bluetooth service
immediately, then decides ONCE whether a BLE setup session is live -- if it is,
bluetoothd and both BLE units are left on their current code until the next
boot. There is no waiting. (The earlier 5-minute wait held every other restart,
outlived jam-player-display's 5-minute stale-flag budget so the two fought over
the screen, and -- jam-update being Type=oneshot -- would have pinned any unit
ordered After= it. That is what the 2026-09-15 bench flap was.)

**E1 · Unchanged `main.conf` → bluetooth is not restarted** `[ ]`
Run an update with no BLE config change. Verify
`journalctl -u bluetooth -b --no-pager | grep -c "Started"` did not increase and
`systemctl show -p ExecMainStartTimestamp --value bluetooth` is unchanged.

**E2 · Update during a live session leaves BLE alone and does not wait** `[ ]`
Setup R7 (phone connected, `main.conf` modified). Expected: the R7 loop shows
jam-player-display, jam-heartbeat and jam-tailscale (re)started within seconds
of each other and of jam-update's exit -- no 5-minute gap; the journal has the
warning `BLE setup session in progress; leaving BLE units (and bluetoothd) on
their current code until the next boot`; bluetooth and both BLE units keep their
old start timestamps; the phone's link **survives** and registration completes.

**E3 · Session over before the restart → BLE restarts once, updater does not re-fire** `[ ]`
Disconnect the phone before the install finishes (or update with no phone at
all), `main.conf` modified. Expected: bluetooth restarted exactly once, both BLE
units restarted after every other service, `ls /run/jam/first_connect_update_triggered`
exists, and `systemctl show -p ExecMainStartTimestamp --value jam-update` does
not change afterwards (the restarted state manager did not start a second run).

**E4 · First-connect auto-update with the app in hand** `[ ]`
Fresh (unregistered) device with an update pending on the branch. Provision WiFi
and immediately proceed through registration. Expected: no disconnect; ONE
"Updating…" screen for the length of the install; then exactly one transition to
the correct state screen (no content → updating → content flap); heartbeat and
tailscale alive within a minute of the update completing (`tailscale ip -4`).

**E5 · Mixed-version window: old state manager, new everything else** `[ ]`
The E2 outcome leaves jam-ble-state-manager on the OLD build until the next
boot. With the phone still connected and the location ACTIVE with no content
yet, the display must show the correct setup-ladder screen (screen link /
downloading), never "Set up your JAM Player" (AWAITING_NETWORK), even though
`ls /run/jam/state_manager_alive` does not exist. Then reboot: the stamp appears
and the screens are unchanged. (2026-09-16: the new display treated the missing
stamp as offline and showed the setup screen until reboot.)

**E6 · No stalls or timeouts in the state manager during the update** `[ ]`
During E2/E4, `journalctl -u jam-ble-state-manager -b --no-pager | grep -cE "Timeout restarting|start jam-update timed out"`
= 0 (both systemctl calls are now `--no-block`; queued jobs run when jam-update exits).

### F — Reporting and display (MAC / network status)

**F1 · Setup screens show the MACs, and nothing collides** `[ ]`
Any non-content screen shows `Wi-Fi MAC:` and (if present) `Ethernet MAC:` under
the device UUID. Compare with `nmcli -t -f GENERAL.TYPE,GENERAL.HWADDR device show`.
On the setup screen, "Get ready to JAM." sits clearly ABOVE "Device ID: XXXXX"
and the QR code is full size (2026-09-16 bench: the two MAC lines pushed
"Device ID" up over the tagline). To force a fresh render on the bench:
`sudo rm -f /var/cache/jam-player-display/*.png && sudo systemctl restart jam-player-display`.

**F1b · Every screen, every display size, by test** `[ ]`
From the services_v2 directory on the device:
`sudo /opt/jam/venv/bin/python3 -m unittest tests.test_display_screen_layout -v`.
Renders all eight non-content screens at 720p/1080p/1440p/4K with 0/1/2 MAC
lines and asserts no text or QR border overlaps anything and nothing runs off
the image; the two QR screens must keep a full-size code at every size.

**F2 · Backend shows MACs and network** `[ ]`
After announce (fresh) or the first heartbeat after boot, both dashboards show
Wi-Fi MAC, Ethernet MAC, and Network (SSID / Ethernet) for the bench device.

**F3 · Offline shows "last connected"** `[ ]`
Power off the AP for > 10 min. Expected: dashboard flips to offline with the last
network shown; no false-offline while the device is healthy.

**F4 · Logs reach the backend** `[ ]`
Within a minute of any service starting on new code, JamPlayerLog rows for this
device appear in the testing dashboard's Logs & Errors panel (INFO level). If
not: `sudo journalctl -b --no-pager | grep -F "[log_shipper]"` (drops are reported
only after 1 h of continuous failure) and confirm the handler is attached:
`cd /opt/jam/services && sudo /opt/jam/venv/bin/python -c "import logging; from common.logging_config import setup_service_logging; setup_service_logging('jam-heartbeat'); print([type(h).__name__ for h in logging.getLogger().handlers])"`
must list `BackendLogHandler`. (2026-09-11..16 code never attached it.)

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

### I — Updater safety and release control (audit #17, parts 1–6)

**I1 · A release whose updater cannot load is refused, not promoted** `[ ]`
Setup: on the bench branch, commit a deliberate `NameError` (e.g. `x = undefined_name` at the
bottom of `services_v2/common/paths.py`) and push. Steps:
`sudo systemctl start jam-update; journalctl -u jam-update -b --no-pager | grep -E "FAILED validation|aborted before promotion"`
Expected: both lines; `/etc/jam/version.txt` unchanged; the old updater still installed
(`cmp /opt/jam/services/jam_update.py /opt/jam/updater-lkg/jam_update.py` may differ — the point is
it was NOT replaced); a backend error "did not pass validation" in Logs & Errors. Revert the
commit → next run validates, logs `promoting and re-executing`, and `/opt/jam/updater-lkg/lkg_version.txt`
holds the previous commit.

**I2 · A broken installed updater is restored at boot** `[ ]`
Steps: `sudo sed -i '1i import definitely_missing_module' /opt/jam/services/jam_update.py && sudo reboot`
Expected on the next boot: `journalctl -u jam-venv-repair -b --no-pager | grep -E "BROKEN|RESTORED"` shows
both; `jam-update` then runs on the restored file and reports "restored from last-known-good" to the
backend (Logs & Errors); `/var/lib/jam/updater_recovered` is gone afterwards.

**I3 · Two silent deaths in a row trigger a restore even when the file imports** `[ ]`
Steps: `echo 2 | sudo tee /var/lib/jam/updater_attempts && sudo reboot`
Expected: guard logs `2 boots in a row without recording a controlled exit` and restores; after a
normal `jam-update` run the counter file is gone (`ls /var/lib/jam/`).

**I4 · Release target decisions** `[ ]`
Setup: in the super-admin Releases panel, cut a release for the bench branch and set its target.
For each row run `sudo systemctl start jam-update; journalctl -u jam-update -b --no-pager | grep "Release target decision"`:
- eligible 0% (bench device outside the bucket), no stable → `staying put`; with a stable set →
  `running the stable release instead` and `version.txt` = stable commit
- eligible 100% → `eligible for the current release target` → updates to the target
- hold on → `HELD` → no change
- no release targeted, follow-HEAD off (the default; also what a target created for `main` without a
  release looks like) → `follow-HEAD is off; staying put` → no change, even after a push to the branch
- no release targeted, follow-HEAD ON (bench branch only; the backend refuses it on main) →
  `following branch HEAD` → updates to the branch tip
- backend unreachable (block the API host at the router for one run), cached answer present →
  `using the cached release-target answer` and the cached decision applies (a cached hold still holds)
- backend unreachable AND `sudo rm /var/lib/jam/update_target.json` → `staying put this run (fail closed)`;
  `version.txt` unchanged. `following branch HEAD` must NEVER appear in either unreachable case

**I5 · First-connect update waits for announce** `[ ]`
Fresh (unregistered) device; provision WiFi from the app. Expected:
`journalctl -u jam-ble-state-manager -b --no-pager | grep -E "Device announced; starting the first-connect update|Announce not observed"`
shows the first form (the second only if announce took > 90 s).

**I6 · Stale git locks do not stop updates forever** `[ ]`
Steps: `sudo touch -d '20 minutes ago' /home/comitup/jam-player/.git/index.lock && sudo systemctl start jam-update && journalctl -u jam-update -b --no-pager | grep "Removed stale git lock"`
Expected: the lock is removed and the fetch proceeds.

**I7 · The dashboard knows the branch and the version** `[ ]`
After a boot, the super-admin detail modal shows Branch = `testing` (bench) and Version changed = the
time of the last real change; the Releases panel's "Not reporting" row does not include this device.

**I8 · Stale-firmware job** `[ ]`
Set the bench branch's target to a release the device is eligible for but block the device from
updating (block git at the router) for > 72 h — or
temporarily lower the job's 72 h/3-day rule in a test deploy. Expected: `staleFirmwareSince` set, a
"Stale firmware" badge in the modal, a fleet alert once the threshold is crossed, one email; the alert
resolves after the device catches up.

**I9 · A player never moves backwards** `[ ]`
The path that must be impossible: lowering the eligibility knob pulling devices that already took the
release back onto the old one.
1. Put the bench player on the target release (eligible 100%). Confirm `cat /etc/jam/version.txt`.
2. Lower `eligiblePercent` below the device's bucket (the plan box names the bucket), with a stable
   release set. Reboot or `sudo systemctl start jam-update`.
3. Expected: `already installed the target; staying put` in the decision line, `version.txt` unchanged.
   The device must NOT move to the stable commit.
4. Now target the branch at an OLDER release directly. Expected: `REFUSING to move backwards` and an
   error in Logs & Errors; `version.txt` unchanged.
5. Re-target the newer release. Expected: a normal update, proving the refusal is not sticky.
```
sudo systemctl start jam-update; journalctl -u jam-update -b --no-pager | grep -E "staying put|REFUSING to move backwards|Release target decision"
```


**I10 · A fielded player converges in ONE nightly reboot** `[ ]`
The whole-fleet path, and the one that bit us on the bench. A player on fielded firmware pulls the
branch tip, promotes `jam_update.py` + `common/`, and re-execs with `version.txt` still naming the OLD
commit. The new updater must FINISH that update, not re-decide it.
1. Flash a card on the fielded commit, set its branch, let it reach the internet.
2. Expected in one run: `Continuing an update already in flight: installing the checked-out <sha>`,
   then the install completes and `cat /etc/jam/version.txt` equals the branch tip.
3. The failure this guards against: `version.txt` still on the old commit while
   `git -C /home/comitup/jam-player rev-parse --short HEAD` shows the new one. That is a player that
   will NEVER converge, because every later boot repeats the same non-decision.
4. Confirm the target governs from the next run: restart the updater and expect a normal
   `Release target decision:` line, with no further change.
```
journalctl -u jam-update -b --no-pager | grep -E "Continuing an update already in flight|Release target decision|Already up to date"; cat /etc/jam/version.txt; echo; sudo git -C /home/comitup/jam-player rev-parse --short HEAD
```


### J — Display and lightdm (audit #11)

**Read this first.** On a healthy fielded player `lightdm` is **expected to be in `failed` state**
after boot, and the display is fine. That is deliberate: the `jam-no-restart.conf` drop-in sets
`Restart=no` so lightdm crashes exactly once (a Bookworm GObject teardown bug) instead of
crash-looping every ~3 s, which was the customer-visible "flashes between content and black"
symptom. mpv holds the DRM surface independently and keeps rendering. Do not "fix" lightdm being
failed, and do not restart it to recover a display: a restart spawns a new X session that grabs DRM
master from mpv, and on a static-image scene mpv never reclaims the surface, so the screen stays
black until a manual reboot. That is the mechanism behind the field incidents, and it is why the
hotplug monitor's restart was removed.

**J0 · The healthy signature** `[ ]`
On a normally booted player with content playing, all four must hold: picture on screen,
`lightdm` failed, `Restart=no`, and zero lightdm restarts from the display service.
```
systemctl is-failed lightdm; systemctl show lightdm -p Restart --value; systemctl is-active jam-player-display; journalctl -u jam-player-display -b --no-pager | grep -c "restarting lightdm"
```

**J1 · Reproduce the storm on CURRENT code, before changing anything** `[ ]` *(bench unit only — this
deliberately produces a black screen)*
Hide the first scheduled scene's media file, then restart the display service.
```
sudo python3 -c "import json,os;s=json.load(open('/opt/jam/content/live_scenes/scenes.json'));m='/opt/jam/content/live_media/'+s[0]['media_file'];os.rename(m,m+'.hidden');print(m)" && sudo systemctl restart jam-player-display
```
Expected on current code: `restarting lightdm to fix display` within ~30 s and then roughly every 5 s;
the unit watchdog-killed at 60 s; `failed` within ~5 minutes; black screen despite valid cached
content. Watch it with:
```
journalctl -u jam-player-display -f | grep -E "restarting lightdm|Watchdog|start request repeated|MPV process died"
```
Restore: rename the `.hidden` file back, `sudo systemctl reset-failed jam-player-display lightdm`, reboot.

**J2a · One missing file no longer blocks the rest** `[ ]`
With at least two scheduled scenes, repeat J1's setup (hide only the FIRST scene's media file).
Expected: playback starts on the next scene whose file exists, `restarting lightdm` never appears, and
the unit stays `active`. Before the fix this state produced the J1 storm.

**J2b · Nothing playable keeps the service alive and self-heals** `[ ]`
Hide EVERY media file, then restart the display service.
```
sudo find /opt/jam/content/live_media -maxdepth 1 -type f -exec sudo mv {} {}.hidden \; && sudo systemctl restart jam-player-display
```
Expected over the next 10 minutes: no `restarting lightdm`, no watchdog kill, `systemctl is-active
jam-player-display` still `active`, and the "cannot start MPV" ERROR logged at most once (it is
throttled to 10 minutes). Then restore the files and confirm playback resumes **without a reboot** —
that is the whole point of keeping the service alive.
```
sudo find /opt/jam/content/live_media -maxdepth 1 -name '*.hidden' -exec sh -c 'sudo mv "$1" "${1%.hidden}"' _ {} \; && sleep 45 && systemctl is-active jam-player-display
```

**J3 · HDMI unplugged across a reboot (the field incident)** `[ ]`
Power off, unplug HDMI, power on, leave it 5 minutes, then plug HDMI in.
Expected: no `restarting lightdm` at any point, the display unit stays `active`, the hotplug monitor
logs the reconnect, and the picture appears within a few seconds of plugging in (Wayfire handles
hotplug natively).
```
journalctl -u jam-player-display -u jam-display-hotplug-monitor -b --no-pager | grep -E "restarting lightdm|HDMI|reconnect"
```

**J4 · Real repeated mpv crashes still recover** `[ ]`
Kill mpv five times inside the 30 s window and confirm the self-heal is the display service
restarting mpv, not a lightdm restart.
```
for i in 1 2 3 4 5; do sudo pkill -9 mpv; sleep 3; done; sleep 20; systemctl is-active jam-player-display; journalctl -u jam-player-display -b --no-pager | grep -cE "restarting lightdm"
```
Expected: content playing again, unit `active`, count `0`.


**J5 · A display restarted after a LONG install still defers to the updater** `[ ]`
Start an update (`sudo systemctl start jam-update`); while it is installing, age
the coordination flag past the display's 5-minute budget:
`sudo touch -d '-6 min' /run/jam-update-in-progress`. Expected: at the restart
the flag is re-stamped (`stat -c %y /run/jam-update-in-progress` is seconds old
while jam-update is still running), the boot identity screen does NOT appear
mid-update, no state screen appears before the "Updating…" screen is taken
down, and the display transitions exactly once when jam-update exits.

### K — feh cleanup patterns (VERIFY FIRST, no code change yet)

**Why this group exists.** `kill_feh_processes()` in the display service, the `ExecStopPost` in
`jam-player-display.service`, and jam-update's kill of the display's screen all use the pattern
`feh.*jam_display`. That predates the display image cache. Cached screens are written to
`/var/cache/jam-player-display/` — spelled with hyphens — so on paper the pattern matches only the
fallback renders at `/tmp/jam_display_*.png` and misses every cached screen. Tested off-device the
mismatch is certain; what it actually does to the picture depends on X stacking, which only a player
can answer. **Run K1–K4 on the CURRENT build before anything is changed.** If K1 shows the pattern
matching nothing, the gap is real. If it matches, the analysis is wrong and nothing should change.

Setup used by K1–K3: force a static cached screen by moving the manifest aside (reversible).
```
sudo mv /opt/jam/content/live_scenes/scenes.json /opt/jam/content/live_scenes/scenes.json.bak && sudo systemctl restart jam-player-display && sleep 25
```
Restore afterwards:
```
sudo mv /opt/jam/content/live_scenes/scenes.json.bak /opt/jam/content/live_scenes/scenes.json && sudo systemctl restart jam-player-display
```

**K1 · Does the sweep pattern match a live cached screen?** `[ ]`
With a static screen up, list the real feh processes, then list what the sweep pattern would hit.
```
echo "--- all feh ---"; pgrep -a feh; echo "--- what 'feh.*jam_display' matches ---"; pgrep -af 'feh.*jam_display'; echo "(empty second list + non-empty first = the gap is real)"
```
Record the exact image path feh is showing. Expected if the analysis holds: the first list shows a
path under `/var/cache/jam-player-display/`; the second is empty.

**K2 · Do orphans survive a service restart and accumulate?** `[ ]`
```
pgrep -c feh; sudo systemctl restart jam-player-display; sleep 25; pgrep -c feh; pgrep -a feh
```
Expected if the analysis holds: the count goes **up** by one per restart and old paths stay in the
list. If cleanup works, it stays at one. Repeat the restart once more to be sure.

**K3 · Can a stale screen become visible?** `[ ]`
The decisive question, and the one that needs eyes on the TV. With at least one orphan from K2,
restore the manifest so the device returns to playing content, then stop mpv and watch the screen.
```
sudo mv /opt/jam/content/live_scenes/scenes.json.bak /opt/jam/content/live_scenes/scenes.json && sleep 30 && pgrep -a feh && sudo pkill -9 mpv
```
Record what appears in the seconds before mpv restarts: customer content, black, or the old static
screen from K1. An old static screen appearing is the customer-visible symptom.

**K4 · The update case** `[ ]`
With a static screen up (setup above), run an update and watch feh across it.
```
sudo systemctl start jam-update & sleep 20; echo "--- during ---"; pgrep -a feh; sleep 120; echo "--- after ---"; pgrep -a feh
```
Expected if the analysis holds: during the update BOTH the updater's `/tmp/jam_updating.png` and the
older cached screen are running, and after the updater removes its own, the cached one is still
listed. Note whether the TV shows the old screen after the update finishes and before content
returns.

**If K1–K4 confirm it**, the change is one pattern in three files —
`src/jam_player/services_v2/jam_player_display.py`, `systemd/jam-player-display.service`, and
`src/jam_player/services_v2/jam_update.py` — widened to `feh.*(jam_display|jam-player-display)`,
which covers both locations and still cannot match `jam_updating`. Re-run K1–K4 afterwards: K1's
second list should then be non-empty, and K2's count should stay at one.


### L — Boot identity screen and Plymouth splash

The player shows a device-identity screen (Device ID, setup network name, full UUID, Wi-Fi and
ethernet MACs) for 15 s at every boot, before its first real mode, and once it has a render good
enough to cache it promotes that image to the Plymouth boot splash. Shipped images keep the JAM logo
until a player generates its own screen, and the logo is preserved alongside so it can be restored.

**L1 · It appears once per boot, with the right data** `[ ]`
Reboot a provisioned player and watch the TV. Expected: the identity screen for about 15 s, then the
normal content or setup screen. Check the Device ID matches the last five characters of the UUID and
the MACs match what the dashboard reports for this player.
```
sudo reboot
```
```
journalctl -u jam-player-display -b --no-pager | grep -E "Boot identity screen up|skipping the boot identity|boot splash"
```

**L2 · A service restart does NOT re-show it** `[ ]`
The unit restarts on watchdog kills, crashes and every update; re-holding for 15 s each time would
delay the customer's content. Expected: the count stays at 1 across restarts, and the screen does not
reappear on the TV.
```
journalctl -u jam-player-display -b --no-pager | grep -c "Boot identity screen up"; sudo systemctl restart jam-player-display; sleep 25; journalctl -u jam-player-display -b --no-pager | grep -c "Boot identity screen up"
```

**L3 · The splash is replaced once, and the logo is kept** `[ ]`
Expected after the first boot with data: `splash.jam-logo.png` exists and holds the original logo,
`splash.png` matches the cached identity render, and a second reboot does NOT rewrite the file (the
SD card is the scarcest resource on the device — check the modification time is unchanged).
```
ls -l /usr/share/plymouth/themes/pix/splash.png /usr/share/plymouth/themes/pix/splash.jam-logo.png; md5sum /usr/share/plymouth/themes/pix/splash.png /var/cache/jam-player-display/boot_identity_*.png
```
Then reboot and confirm the new splash actually appears during boot. If it does not, this image keeps
its Plymouth theme in an initramfs; that is a harmless no-op, and the 15 s screen still works.
Restore the logo at any time with:
```
sudo cp /usr/share/plymouth/themes/pix/splash.jam-logo.png /usr/share/plymouth/themes/pix/splash.png
```

**L4 · A player with no UUID shows nothing and keeps the logo** `[ ]`
Simulate a freshly imaged device. Expected: `No device UUID yet; skipping` in the log, no identity
screen on the TV, and `splash.png` untouched.
```
sudo mv /etc/jam/device_data/device_uuid.txt /tmp/device_uuid.bak && sudo rm -f /run/jam/boot_identity_shown && sudo systemctl restart jam-player-display && sleep 20 && journalctl -u jam-player-display -b --no-pager | grep -E "No device UUID yet|Boot identity screen up"
```
Restore: `sudo mv /tmp/device_uuid.bak /etc/jam/device_data/device_uuid.txt && sudo reboot`

**L5 · An update at boot keeps its own screen** `[ ]`
The identity screen must never fight jam-update for the display. With an update running at boot,
expect `jam-update owns the screen; skipping` and the updating screen undisturbed.
```
sudo rm -f /run/jam/boot_identity_shown; sudo systemctl start jam-update & sleep 5; sudo systemctl restart jam-player-display; sleep 20; journalctl -u jam-player-display -b --no-pager | grep -E "owns the screen|Boot identity screen up"
```


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
- **Hardware watchdog, health-monitor cooldown.**
- **Mobile:** Android link-loss message; verify-after-`connected` on old firmware;
  Location Services gate; enterprise-network detection; ethernet "continue
  without WiFi".
- **Polling:** network-status send-on-change; registration via WS push;
  update-poll / outlet cadence.
