# Logging: what stays on the card, what goes to the backend

**Status:** implemented September 2026 (device: `services_v2/common/`;
backend + dashboards: jam-sphere). Applies to Pi-based JAM Players.

## The rule

JAM Players run from SD cards. Every log line written to the card is wear,
and fielded cards have failed from it. So:

| Level | On the card (journald) | Backend (dashboards) |
|---|---|---|
| DEBUG, INFO | never written | shipped when online, **dropped when offline** |
| WARNING | kept | shipped |
| ERROR, CRITICAL | kept | shipped |

"Kept on the card" means exactly what an operator standing in front of the
player needs to tell that a critical part of the system is not working.
Everything else is only useful with the whole fleet in view, which is what
the dashboards are for. A player that cannot reach the backend loses its
INFO/DEBUG lines for that period. That is accepted on purpose: no disk
queue, no retry buffer on the card.

Two independent mechanisms enforce the rule, so neither alone is load-bearing:

1. **journald** (`etc/systemd/journald.conf.d/jam.conf`): `MaxLevelStore=warning`
   drops everything below warning from *every* unit before it reaches
   storage, and the journal cap is 64 MB. Installed by jam-update; applies at
   the next journald restart (the nightly reboot).
2. **The services' own logging** (`common/logging_config.py`): the local
   stream handler is set to WARNING, so INFO/DEBUG are never even handed to
   journald; the backend handler receives everything at the configured level.
   The local handler also writes systemd's `<N>` priority prefix on every
   line (`common/journal.py`), because mechanism 1 filters on journald's
   PRIORITY field and an unprefixed stderr line is priority 6 (info) no
   matter what it says. Without the prefix, mechanism 1 would drop the
   WARNING lines too.

3. **Repeated request failures are de-duplicated** (`common/api.py`): an
   offline or backend-down player logs the first failure of each endpoint at
   WARNING (kept), repeats at DEBUG (shipped only), and one INFO on recovery.
   Before this, every failed heartbeat and poll was an ERROR line on the card.

`/etc/jam/config/log_level` keeps its meaning: it sets the level that is
*shipped*. `DEBUG` there sends DEBUG lines to the backend; it never puts
them on the card.

## Existing curated errors are unchanged

`report_error()` → `POST /jam-players/errors` → `JamPlayerError` stays as
the curated, long-lived record of "this service failed in this way", used
for alerting. Logs are the surrounding context with short retention. The
dashboards show both, side by side, per player.

## Wire contract

Authentication for the device endpoint is the existing Ed25519 device
signing (`X-Device-ID`, `X-Timestamp`, `X-Body-Hash`, `X-Signature`), the
same as `/device-status`. A player that has no signing identity yet (before
announce) cannot ship and drops its lines.

### `POST /jam-players/logs` (device)

```json
{
  "logs": [
    {
      "service": "JAM_BLE_STATE_MANAGER",
      "level": "INFO",
      "message": "Post-boot BLE recovery window closed (15 min)",
      "loggerName": "jam-ble-state-manager",
      "deviceTs": "2026-09-10T15:04:05.123Z"
    }
  ]
}
```

- `service`: a `jam_player_system_service` enum value (`OTHER` if unmapped).
- `level`: `DEBUG | INFO | WARNING | ERROR | CRITICAL`.
- `message`: server truncates to 2048 characters. `loggerName` ≤ 128, optional.
- `deviceTs`: the device's wall clock, informational only (it can be wrong
  early in boot); the server stamps `receivedAt`.
- 1–200 entries per batch; body ≤ 256 KB. Malformed batch → 400, device drops it.

Response `200`:

```json
{ "accepted": 200, "dropped": 0 }
```

`dropped` > 0 with `"reason": "rate_limited"` means the per-device cap
(600 lines per rolling minute) was hit; the server inserts ONE marker line
(`WARNING`, service `OTHER`, "Rate cap: dropped N log lines") and the device
does not retry. `"reason": "invalid_entries"` means some entries were
rejected individually (empty or non-string `message`, unknown `level`); an
unknown `service` is stored as `OTHER` rather than rejected. The cap is
soft: two batches arriving in the same instant can overshoot by one batch.
A body over 256 KB is a 413; an unknown device is a 404. The device treats
any non-2xx as "drop this batch".

### `GET /jam-players/by-id/{jamPlayerId}/logs` (admin, JWT)

Query: `level` (minimum level, optional), `service` (optional), `since`,
`until` (ISO-8601, optional), `page` (1-based, default 1), `limit`
(default 100, max 500). Authorization is the same brand scoping as the
dashboard list; super-admin sees any player.

```json
{
  "logs": [ { "id": "...", "service": "...", "level": "...", "message": "...",
              "loggerName": "...", "deviceTs": "...", "receivedAt": "..." } ],
  "pagination": { "page": 1, "limit": 100, "total": 0, "totalPages": 0 }
}
```

Newest first, paged with the same `page` + `limit` convention as every
other listing endpoint: `total` counts every line matching the filters,
and an older page exists while `page` < `totalPages`. `until` defaults to
the server's clock when the caller omits it; pin it on the first request
and send the same value back with every later page, so lines arriving
while an operator pages cannot push rows from one page into the next.
`limit` above 500 is a 400, not a clamp. Authorization: `JAM_SUPER_ADMIN`
and `JAM_SUPPORT` see any player; `FEATURED_BRAND_ADMIN` only players in
their own brand, the same rule as the dashboard list.

### `GET /jam-players/by-id/{jamPlayerId}/errors` (admin, JWT)

Query: `page` (1-based, default 1), `limit` (default 100, max 500). Same
authorization, same pagination block as the logs listing.

```json
{
  "errors": [ { "id": "...", "affectedService": "...", "severity": "...",
                "errorMessage": "...", "createdAt": "..." } ],
  "pagination": { "page": 1, "limit": 100, "total": 0, "totalPages": 0 }
}
```

## Storage and retention (backend)

`JamPlayerLog` in Postgres: `id`, `jamPlayerId`, `service`
(`jam_player_system_service`), `level` (`jam_player_log_level`), `message`
(varchar 2048), `loggerName` (varchar 128, nullable), `deviceTs`,
`receivedAt`. Indexes on `(jamPlayerId, receivedAt)`,
`(jamPlayerId, service, level, receivedAt)`, and `(level, receivedAt)` — the
last one exists for the retention sweep, which would otherwise scan the
whole table per batch. Migration `20260910180000_jam_player_log`.

A daily scheduled job deletes: DEBUG/INFO older than 7 days, WARNING older
than 30 days, ERROR/CRITICAL older than 90 days. `JamPlayerError` retention
is unchanged.

## Device implementation

`common/log_shipper.py` (the backend handler), `common/logging_config.py`
(wiring) and `common/journal.py` (journald priority); their module
docstrings carry the detail. Summary:

- `setup_service_logging()` installs two root handlers once per process
  and only re-levels them on repeated calls (modules import each other): a
  `StreamHandler` on stderr pinned at WARNING, and a `BackendLogHandler` at
  the configured level. The first call's service tag wins, so a process
  that imports another service module keeps its own enum (jam-update
  imports jam-player-display lazily and must stay `JAM_UPDATE`).
- **journald priority.** Every jam-* unit has `StandardError=journal` and
  no `SyslogLevel=`, so systemd tags an unprefixed stderr line as priority
  6 (info) whatever it says, and `MaxLevelStore=warning` filters on that
  field. The local handler therefore writes systemd's `<N>` line prefix
  (`<4>` WARNING, `<3>` ERROR, `<2>` CRITICAL; on every line of a
  traceback, since journald parses it per line) whenever `$JOURNAL_STREAM`
  is set, which is systemd's signal that it owns the stream. journald
  strips the prefix; `journalctl` output is unchanged. Run by hand in a
  terminal, the lines print without it.
- The backend handler keeps an in-memory ring buffer (1000 entries, oldest
  dropped) and one daemon thread that flushes every 30 seconds or as soon
  as 200 entries are queued. A flush drains the buffer in batches of at
  most 200 entries and ~200 KB serialized, one signed POST each
  (`common.api.api_request`, 20-second timeout; a cold-started Lambda can exceed 10 s). Any exception, `None`
  response or non-2xx drops that batch and ends that flush; whatever is
  left waits for the next wake, so an unreachable backend costs one
  timeout per 30 seconds. Nothing touches disk, ever.
- Nothing is sent until the device is announced and has its device UUID
  and signing key; until then entries are discarded without a request.
- The send path is quiet. `common.api` logs an ERROR on every timeout or
  connection failure; while the shipper is sending, records logged on that
  thread are dropped by both handlers (a thread-local guard, exposed to the
  stream handler as `ShipperInternalFilter`). Otherwise an offline player
  would write an ERROR to the card every 30 seconds and re-ship it.
- The handler never raises, never blocks the logging thread, and never
  logs through the logging system. Its health is out of band:
  `BackendLogHandler.stats()` counters, and at most one line per hour
  written straight to stderr at warning priority:
  `[log_shipper] N log batch(es) dropped since <time>; last: <reason>`.
- On exit, `logging.shutdown()` calls the handler's `flush()`, which makes
  one synchronous send of the oldest batch with a 3-second timeout. A
  service that dies to SIGTERM without a Python signal handler skips this
  and loses up to 30 seconds of lines.
- Message is truncated to 2048 characters and `loggerName` to 128 on the
  device (matching the server), so a batch of tracebacks cannot exceed the
  body cap. `deviceTs` is `record.created` as ISO-8601 UTC with
  milliseconds and a `Z` suffix.
- Service mapping from the service's logger name to the enum, one value per
  Pi service (values added 2026-09-10 for the services that were landing
  as `OTHER`): `jam-announce→JAM_ANNOUNCE`, `jam-ble-provisioning→JAM_BLE_PROVISIONING`,
  `jam-ble-state-manager→JAM_BLE_STATE_MANAGER`, `jam-boot-check→JAM_BOOT_CHECK`,
  `jam-first-boot→JAM_FIRST_BOOT`, `jam-health-monitor→JAM_HEALTH_MONITOR`,
  `jam-heartbeat→JAM_HEARTBEAT`, `jam-player-display→JAM_PLAYER_DISPLAY`,
  `jam-registration-poller→JAM_REGISTRATION_POLLER`, `jam-update→JAM_UPDATE`,
  `jam-ws-commands→JAM_WEBSOCKET_COMMANDS`, `jam-display-cache-prewarm→JAM_DISPLAY_CACHE_PREWARM`,
  `jam-display-hotplug-monitor→JAM_DISPLAY_HOTPLUG_MONITOR`,
  `jam-display-wait-for-hdmi→JAM_DISPLAY_WAIT_FOR_HDMI`,
  `jam-installed-version-reporter→JAM_INSTALLED_VERSION_REPORTER`,
  `jam-outlet-status-poller→JAM_OUTLET_STATUS_POLLER`, `jam-venv-repair→JAM_VENV_REPAIR`,
  `jam-tailscale→JAM_TAILSCALE`, `jam-chrony-peering→JAM_CHRONY_PEERING`; anything
  else → `OTHER`. `NETWORK_MANAGER`, `BLUETOOTH`, `CHRONY`, `TAILSCALE` are
  reserved for the system daemons themselves.

### Verifying on a device

The units run the copy jam-update installs to `/opt/jam/services/`, so
that is where the probe below runs; the unit tests run from the repo
checkout (`/home/comitup/jam-player/src/jam_player/services_v2/tests/run_on_device.sh`).
`jam-heartbeat` is just the example unit.

- **The card only has WARNING+.** `journalctl -u jam-heartbeat -b` shows
  no INFO lines at all once journald has restarted with the drop-in; that
  is by design, not a broken service. The lines that are there must be
  stored at their real priority: `journalctl -u jam-heartbeat -b -o verbose | grep -c 'PRIORITY=[0-4]'`
- Prove the prefix survives the store rule with a KNOWN warning: `sudo systemd-run --unit=jam-probe -p StandardError=journal /opt/jam/venv/bin/python3 -c 'import sys; sys.path.insert(0, "/opt/jam/services"); from common.logging_config import setup_service_logging; setup_service_logging("jam-probe").warning("probe")'; sleep 1; journalctl -u jam-probe -o verbose | grep -c 'PRIORITY=4'` → `1`. (Checking that no `PRIORITY=6` lines exist is vacuous: journald never stores them once the drop-in is active.)
  must be 0 (a non-zero count means unprefixed lines are reaching journald
  and will be discarded).
- **Batches are leaving.** The shipper is silent on success on purpose.
  To watch it on one unit, set `JAM_LOG_SHIPPER_TRACE=1` in that unit's
  environment: `sudo mkdir -p /etc/systemd/system/jam-heartbeat.service.d && printf '[Service]\nEnvironment=JAM_LOG_SHIPPER_TRACE=1\n' | sudo tee /etc/systemd/system/jam-heartbeat.service.d/trace.conf >/dev/null && sudo systemctl daemon-reload && sudo systemctl restart jam-heartbeat && journalctl -u jam-heartbeat -f`
  then prints `[log_shipper] sent N records (HTTP 200)` on every flush, and
  `[log_shipper] dropped N records (<reason>)` on every failure. Remove it
  when done: `sudo rm -r /etc/systemd/system/jam-heartbeat.service.d && sudo systemctl daemon-reload && sudo systemctl restart jam-heartbeat`.
- **One-shot probe** (no service restart; ships one INFO line as service
  `OTHER` and prints the result): `cd /opt/jam/services && sudo /opt/jam/venv/bin/python3 -c "import logging,sys; sys.path.insert(0,'.'); from common.log_shipper import BackendLogHandler; h=BackendLogHandler('OTHER'); h.emit(logging.LogRecord('probe',logging.INFO,'-',0,'log shipper probe',None,None)); print(h.flush_now(), h.stats())"`
  prints `True` with `records_sent: 1` when the pipe works, `False` with
  `last_error` (`HTTP 401`: not announced or key not on file; `request
  failed`: no route to the backend) when it does not.
- **Failures over time.** `journalctl -u jam-heartbeat -b | grep log_shipper`
  shows the at-most-hourly drop line; a player that is online and healthy
  has none.
- **The other end.** The player's Logs & Errors panel in the dashboard,
  filtered to the service; a freshly restarted service shows its startup
  banner there within about 30 seconds.

## Reading logs now

- Fleet / remote: the player's **Logs & Errors** panel in the admin and
  super-admin JAM Players dashboards (filter by service and level).
- On the device: `journalctl -u 'jam-*' -p warning -b` shows what is kept
  locally. INFO lines are not there by design.

## Tests

On-device only (`services_v2/tests/run_on_device.sh`), like the rest of the
suite: `tests/test_log_shipper.py` and `tests/test_logging_config.py`
exercise the real modules with `api_request` and the identity readers
patched, so they never touch the network or the credential files.

## Hardening from the adversarial review (September 2026)

- **Uncaught exceptions reach the card and the backend.** The interpreter's
  default traceback goes to stderr unprefixed, so journald would tag it info
  and drop it; a service crash-looping on an uncaught exception would have
  left no cause anywhere. `setup_service_logging` installs `sys.excepthook`
  and `threading.excepthook` that log a CRITICAL record instead.
- **`logging.shutdown()` does no network I/O.** It runs in every exit path,
  including the display's SIGTERM handling after the screen has already
  blanked; a send there added its timeout plus unbounded DNS resolution.
  The last few seconds of INFO lines on exit are lost by design.
- **The shipper's lock is re-entrant.** Python runs signal handlers on the
  main thread and every service logs inside its SIGTERM handler; a plain
  lock could deadlock a process caught mid-`emit()` until systemd's SIGKILL.
- **The journal check verifies stderr's device and inode** against
  `$JOURNAL_STREAM`, so children whose stderr is a pipe (captured update
  commands, terminal commands from the dashboard) do not emit the prefix.
- **Logging setup survives a damaged venv.** The shipper imports the signed
  request stack lazily; if it cannot be imported, services still get a plain
  stream handler instead of failing to start (this protects the HDMI
  wait-for-display gate and venv repair, which run before any repair).
- **Attribution is by the process, not by import order.** A module imported
  for shared code (the display module, imported by cache prewarm) used to
  tag the whole process with its own service; the `__main__` module's call
  now wins.
- **HDMI connect and disconnect events are WARNING** so they stay on the
  card; `jam-content-manager` is mapped to its own enum value; the device
  request timeout is 20 seconds to cover cold-started Lambdas.
- Repeated request failures are de-duplicated (see above).

Considered and rejected: `MaxLevelStore=notice`. It would not keep
systemd's "Started" or "Scheduled restart job" lines (those are INFO), only
the exit-code detail of a failure, which our services now record as a
CRITICAL traceback anyway. It would add kernel and daemon chatter on every
boot: more writes for no more signal. The threshold stays at warning.

## Offline write rates

The rule above removes INFO volume, but what actually wears the card is how
many 30-second kernel writeback windows contain at least one stored entry.
An adversarial review found several loops that kept writing WARNING or ERROR
every minute or every ten seconds while a player was offline, which defeated
the rule entirely. Each is now: keep the FIRST occurrence, ship the repeats,
and never fail in a way that makes systemd restart a unit merely because it
is offline.

| Source | Was (entries/hour offline) | Now |
|---|---|---|
| installed-version reporter restart loop | 300-480 | 0 (exits 0 when offline; re-run on reconnect) |
| registration poller, per-minute timer | 180-240 | 0 (exits 0 when offline) |
| chrony peering multicast send | 360 | 1 per outage + 1 on recovery |
| WebSocket reconnect errors | 60 | 1 per outage + 1 on recovery |
| Tailscale, never provisioned | ~300 | 0 (exits 0 when offline) |
| content manager polls | 12-36 | 1 per outage + 1 on recovery |
| announce + health monitor, unannounced | ~45 per boot | 0 |
| display, missing media file | 7200 while it lasts | 1 per file |

`common.network.is_internet_verified()` is the one reader of the
`.internet_verified` flag the BLE state manager maintains; the display, the
BLE Device Info and the oneshot gate (`device_is_offline()`, in the same
module) all go through it.

**How the gated oneshots get re-run.** Tailscale, announce and the
installed-version reporter exit 0 when the gate says offline, and with
`RemainAfterExit=yes` they then sit "active (exited)". The state manager
re-runs them (`systemctl restart`) on every offline-to-online transition it
detects, however connectivity returned: the same WiFi recovering on its own,
an upstream outage ending, or a new network from the app. It also re-runs
them when a boot, or a restart of the state manager itself, finds the device
online, because at boot these units race the manager and may have hit the
gate before the flag existed. The app is never required for recovery.

**It is a cache, and it lags.** Going online, the flag is written on the
first passing check and *before* the manager restarts the services that
depend on it, so a fresh "online" is never missed. Going offline, it can
linger for roughly 20 seconds (the link drops) to two minutes (the link
stays up, the internet behind it is dead, and the resolvers are dark) while
the manager collects three consecutive failed checks. Every reader tolerates
acting on a stale "online" for that long: for the gated services it means
one bounded burst of warnings per offline transition rather than a
per-minute stream.

**It can also be frozen.** One process maintains it. So the manager stamps
its liveness on tmpfs every tick, and a stamp older than 60 seconds makes
the answer "unknown" instead of a confident stale value. Unknown fails
toward offline for the display and BLE (the user is steered to WiFi setup)
and toward online for the gates (a dead manager can never silently disable
a service). Both directions are pinned by tests. The per-minute timer also
stopped writing a systemd timestamp file on every elapse
(`Persistent=false`; `OnBootSec` still covers catch-up).

Two related changes: `ForwardToSyslog=no`, so that an image that happens to
carry rsyslog cannot quietly reinstate INFO writes to `/var/log/syslog`; and
one deliberate `BOOT MARKER` warning per boot, because a healthy boot now
stores nothing at all and boots with no entries disappear from
`journalctl --list-boots`, which the dark-period forensics depend on.
