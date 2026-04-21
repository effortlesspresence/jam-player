# Claude Development Guidelines

## Core Principle
All solutions should be production-ready and enterprise-grade. This is a real SaaS application that is scaling quickly and already has Fortune 500 clients that depend on its reliablity both short and long-term.

## Key Requirements
- Use proper dependency management (no global installs)
- Follow established patterns in the codebase
- Implement proper error handling and logging
- Consider scalability and maintainability heavily
- Use TypeScript where applicable
- Follow security best practices
- Document all major decisions
- Follow zero-trust principles, always
- Do not EVER re-export from a module. Consumers of a function, variable, constant, enum, etc should import directly from the defining package/module

## Interaction Style
- Act as a peer engineer on the team, not a subordinate
- Challenge assumptions and propose alternatives when appropriate
- Point out potential issues or better approaches
- Skip the praise and focus on the work
- Be direct and honest about trade-offs
- Question decisions that seem suboptimal
- Treat this as a collaborative engineering discussion

## JAM Player Backward Compatibility (read every time)
All JAM Player code must be **100% backward compatible** with fielded
devices. This is non-negotiable. Reasons:

- We ship devices to customers who routinely leave them in warehouses for
  6+ months before first plug-in.
- A device unboxed today may be running any commit we cut in the last
  year, and we currently have no `installedVersion` telemetry to tell us
  which one before it connects.
- Customers running devices in offline-playback mode may never connect
  again and will stay on old code indefinitely.
- We auto-update unregistered devices on first connectivity, but already-
  registered deployed devices only catch up via the nightly 3 AM reboot;
  a device that loses WiFi for a week stays on the version it last
  updated to.

**Any change that touches a shared interface must remain compatible with
every prior fielded version.** Shared interfaces include:

- BLE GATT: characteristic UUIDs, flags, payload encodings, manufacturer
  advertisement data layout (status flag bits), notify formats.
- Mobile-app <-> device flow: any assumption the mobile app makes about
  which characteristics exist, what response shapes to expect, what
  encodings to decode.
- Device <-> backend HTTP/WS API: signed-request format, `/jam-players/*`
  endpoint request + response shapes, heartbeat response fields,
  DEVICE_COMMANDS message types + payload shapes, update-poll response.
- On-device state semantics: `.announced` / `.registered` /
  `.internet_verified` flag meanings, `screen_id.txt` format,
  `scenes.json` schema, credentials directory layout.
- Backend state machine: `PENDING_MIGRATION` / `ANNOUNCED` / `REGISTERED`
  transitions. Old-code devices only understand the states they were
  coded against.

**How to apply:**
- When you add a new field, it must be optional for old clients.
- When you rename or remove anything fielded, you must keep the old name
  as an alias or accept both formats for a long deprecation window.
- When you change behavior, feature-detect rather than version-detect
  when possible (e.g. "does this BLE characteristic exist?" beats
  "what version is this device running?").
- When in doubt, ask: "what happens if a device running the commit from
  12 months ago hits this code path today?" If the answer is "it breaks,"
  the change isn't ready.
