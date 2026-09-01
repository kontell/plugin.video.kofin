# LAN server discovery

Signing in started with the user typing a server address from memory into the Account tab, or leaving it blank and answering `account.login`'s keyboard fallback. Every other Jellyfin client finds servers on the local network instead. This adds a **Find servers on local network** button under `serverAddress`, visible only while logged out, which broadcasts Jellyfin's UDP discovery probe, verifies each answer over HTTP, offers the results, and fills in the field.

Nothing in the add-on did UDP, sockets or broadcast before this — `core/discovery.py` is the first.

## The protocol

Client discovery is one plaintext datagram, `who is JellyfinServer?`, broadcast to `255.255.255.255:7359`.

Server-side it is `AutoDiscoveryHost.cs`: a background service that binds `IPAddress.Any:7359` when `NetworkConfiguration.AutoDiscovery` is true — the default — matches the string case-insensitively, and replies **unicast** to the sender with `ServerDiscoveryInfo(Address, Id, Name, EndpointAddress)` as JSON. It is unchanged in the v12 line, so jf12 is covered. `Address` is `GetSmartApiUrl(remote)`: the URL that server publishes *for that caller*, scheme and port included.

Confirmed against the household server before any code was written:

```
sendto(b'who is JellyfinServer?', ('255.255.255.255', 7359))
→ 0.003s from ('192.168.1.167', 7359):
  {"Address":"http://192.168.1.167:8096","Id":"2606bcf8…","Name":"minipie","EndpointAddress":null}
```

Prior art agrees on message, port and broadcast address throughout: jellyfin-kodi's `_server_discovery` (one probe, 1 s idle read), jellyfin-apiclient-python (identical), jellyfin-sdk-kotlin's `LocalServerDiscovery` (500 ms per read, up to 15 servers, `255.255.255.255` since its PR #484), jellyfin-webos (re-probes every 15 s, forever).

## The window is a retry budget, not a patience budget

This is the decision the rest of the design hangs off, and it is the one that is easy to get backwards.

Server-side handling is receive, serialise three strings, send. No database, no disk, no auth. That is why the measured reply was 3 ms, and it means a reply not seen within a few tens of milliseconds was **lost, not delayed**. Waiting longer recovers nothing. Another probe does.

The half that gets lost is ours. An access point transmits a broadcast frame at the lowest basic rate, unacknowledged, buffered against the DTIM interval for sleeping clients, and many consumer APs rate-limit or filter broadcast outright; the server's reply, by contrast, is unicast to our ephemeral port and comparatively safe. So the budget is spent re-broadcasting rather than listening harder: **probe at t=0, 1 s and 2 s, 1 s socket read timeout, close at 3 s** (`discovery.SCAN_SECONDS`, `PROBE_INTERVAL_SECONDS`, `READ_TIMEOUT_SECONDS`).

Three seconds is also under the point where a button press reads as hung, and the HTTP verification overlaps it, so in the ordinary case it is the whole cost of the feature.

## Servers that never answer

No timeout reaches any of these, which is why #30829 points at manual entry rather than suggesting another go:

- `AutoDiscovery` unchecked in the server's network settings.
- Docker **bridge** networking — a published UDP port does not receive `255.255.255.255` traffic. Jellyfin's own docs push host networking for this reason.
- A server on another subnet or VLAN; broadcast does not route.
- A host where something already holds UDP 7359. Only one process per machine can bind it, so a second Jellyfin on the same box is invisible to discovery — **read a live test against jf12 with that in mind**, since it usually shares a host with a production server.
- A multi-homed client: `255.255.255.255` leaves by the default route only. Upstream deliberately chose this over enumerating interfaces (it is what let the Kotlin SDK drop Android's `ACCESS_WIFI_STATE` permission), and it is not worth reversing here.

## Verifying a hit is not a liveness check

The server proved it is alive by answering. What is in question is the address it gave, because `GetSmartApiUrl` returns the *published* URL, which a reverse-proxied install answers to a client on its own wire — an upstream report has `https://pc.interlinx.bc.ca:8920` handed back over the LAN.

So each hit is probed at `/System/Info/Public` (`auth.probe_public_info`), and when the published address does not answer, the datagram's own source address is tried with the published scheme and port (`discovery.fallback_address`). That is the fork's `_convert_endpoint_address_to_manual_address` and the Kotlin SDK's client-filled `endpointAddress`, and it is the one endpoint in the exchange known to be reachable. Whichever address answered is the one written to the setting.

`DISCOVERY_PROBE_TIMEOUT = (3.05, 5.0)`, `retries=0`. The connect leg is `api.PROBE_TIMEOUT`'s and carries its reason — 3.05 s covers exactly one TCP SYN retransmit. The read leg tightens from that budget's ten seconds, deliberately: every way this probe fails (an unroutable IP, a name with no answer, a TLS mismatch) fails in the connect leg, and a generous read leg only stalls a dialog someone is watching. `retries=0` because the scan window is already the retry policy.

Probes are submitted as each datagram lands, on a small pool, so they overlap the rest of the window.

## Why the button closes the settings dialog

`<close>true</close>` is load-bearing here rather than house style. Kodi runs `CGUIDialogAddonSettings::SaveAndClose()` **before** the builtin, so the route writes `serverAddress` with the dialog already shut; written into a dialog that is still open, the value is reverted when the user backs out — the hazard `service/backdrop.py` records for a different setting.

Having closed it, the route reopens it (`Addon.OpenSettings`, the same builtin as `actions.open_settings`). The filled field is the confirmation — there is no success toast — and Log in is the next press.

The `type="string"` dialect is deliberate too: every `<close>true</close>` button in `settings.xml` is that dialect, and all three `type="action"` settings omit `<close>` on purpose.

## A server that answered nothing is offered, not hidden

It sorts last, its row is marked, and picking it still writes the address with a warning. The user may know the network better than the probe does — a box that is briefly down, a firewall rule about to be changed — and silently dropping a server the user can see running is worse than offering one that may not work.

## Android: measured, not inferred

The reply is unicast to our own ephemeral port, so no `MulticastLock` or `CHANGE_WIFI_MULTICAST_STATE` should be needed — the Kotlin SDK dropped Android's `ACCESS_WIFI_STATE` when it moved to `255.255.255.255` for the same reason. That was an inference until 2026-09-01, when `tests/live/harness/probe_discovery.py` ran the real `scan()` inside Kodi 22.0b1 on a Galaxy Tab S5e (Android 13) and got the server back with **the reply landing at 22 ms** — against 3 ms wired from the workstation. Both are two orders of magnitude inside the window, which is the measurement the "lost, not delayed" reasoning above needed. No permission, no lock, no traceback (S10.7 in `docs/testing-plan.md`).

What that run did *not* cover: the published-address fallback, because the Tab's server published the address it was answering from, so `fallback_address` returned the same string. That path is still only unit-tested.

`kodi-drive` has nothing on UDP discovery from an add-on, and the Android result above is exactly the shape it wants — a `kodi-drive:contribute` candidate.

## Out of scope

Continuous rescanning, IPv6, mDNS, remembering more than one server, and any change to the login flow itself. The button fills a field; `account.login` is untouched.
