# JARVIS host-side Wake-on-LAN helper

A tiny HTTP service that runs on the **Windows host** (the laptop at
`C:\ServerData`) and broadcasts Wake-on-LAN magic packets to Natik's main PC on
behalf of Home Assistant.

---

## Why this exists

Home Assistant runs in Docker Desktop on WSL2. Measured on 2026-08-08:

```
jarvis-hass eth0 : 172.30.0.7/16  brd 172.30.255.255
main PC          : 192.168.29.167 on 192.168.29.0/24
```

Those are different layer-2 segments, and the WSL2 bridge does not forward L2
broadcast. A magic packet broadcast from inside the container physically cannot
reach the PC's NIC, no matter how the `wake_on_lan` integration is configured.
So HA calls this helper over HTTP, and the helper does the broadcast from the
real LAN adapter.

---

## Where it listens, and why that is safe

**Bind address: `127.0.0.1:8099` only.** Verified with `Get-NetTCPConnection`:
the listener shows `LocalAddress 127.0.0.1`, never `0.0.0.0`.

That sounds like it would block Docker, but it does not. Measured with a probe
listener bound to `http://+:8099/` and curled from inside `jarvis-hass`:

| Address called from container | Result | Arrived on host as |
|---|---|---|
| `host.docker.internal` | HTTP 200 | `local=127.0.0.1:8099` |
| `192.168.65.254` | HTTP 200 | `local=127.0.0.1:8099` |
| `192.168.29.108` (host LAN IP) | HTTP 200 | `local=192.168.29.108:8099` |
| `172.28.240.1` (WSL vEthernet) | failed | n/a |
| `172.30.0.1` (bridge gateway) | failed | n/a |

Docker Desktop forwards container traffic onto the host's **loopback**. So a
loopback-only bind is reachable from Home Assistant while being invisible to the
LAN and to the internet. Confirmed by negative test — from inside the container:

```
http://192.168.29.108:8099/health  -> connection failed (not reachable over LAN)
http://100.80.120.33:8099/health   -> connection failed (not reachable over Tailscale)
```

No Windows Firewall rule is needed, and none should be added. Do not "fix" this
by binding to `+` or to the LAN IP; that would expose the wake endpoint to every
device on the network and to Tailscale peers.

`host.docker.internal` resolves to an IPv6 address (`fdc4:f303:9324::254`) but
IPv6 to it **fails**; curl falls back to IPv4 `192.168.65.254`, which works. Do
not force IPv6.

---

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health` | none | liveness. Returns version + uptime only, nothing about the PC. |
| `GET` | `/status` | bearer | is the PC awake? |
| `POST` | `/wake` | bearer | broadcast the magic packet |
| `GET` | `/wake` | bearer | alias for the above, see the 411 note |

`/health` is deliberately unauthenticated so uptime monitoring can probe it
without holding the secret. It leaks nothing.

### The 411 trap (important for the HA side)

`http.sys` rejects a POST that has neither a body nor a `Content-Length` header
with **`411 Length Required`**, *before* the request reaches this script.
A bare `rest_command` with no payload sends exactly that shape. Measured from
inside `jarvis-hass`:

```
curl -X POST -H "Authorization: Bearer ..." .../wake     -> http=411
curl -X POST -H 'Content-Length: 0' ... .../wake         -> 200, 4 packets sent
curl        -H "Authorization: Bearer ..." .../wake      -> 200, 4 packets sent  (GET alias)
```

So either send a payload from HA, or use the `GET` alias. Both are verified
working.

### `/wake` response

```json
{"sent":true,"packets_sent":4,"mac":"D8:43:AE:17:63:25",
 "source_ip":"192.168.29.108","broadcast":"192.168.29.255",
 "state_before":"awake",
 "note":"magic packet is unacknowledged; poll /status to confirm",
 "detail":[{"dest":"192.168.29.255:9","sent":102,"ok":true}, ...]}
```

Four packets go out: the directed subnet broadcast and the global broadcast, on
UDP ports 9 and 7. `sent: true` means **the packets left this host** — it does
*not* mean the PC woke. A magic packet is never acknowledged. Poll `/status`.

There is a 3-second rate limit on `/wake` (returns `429`) so a stuck HA
automation cannot machine-gun the LAN.

### `/status` and its honest limits

```json
{"state":"awake","confidence":"high","wakeable":false,"awake":true,
 "detected_via":"icmp","rtt_ms":1,"arp_mac":"D8-43-AE-17-63-25",
 "arp_state":"Reachable","arp_forced_refresh":true,
 "caveat":"confirmed responding via icmp"}
```

`state` is one of `awake`, `asleep_or_hibernated`, `off`, `unknown`.

**Read the `confidence` field.** Only `awake` is provable:

- **`awake`** — confidence `high`. The machine answered ICMP, or accepted a TCP
  connection on 445/3389. ICMP is tried first; the TCP probe exists because
  Windows Firewall often drops inbound ping while still accepting SMB/RDP, which
  would otherwise report a running PC as asleep. The TCP probe opens a socket
  and sends no data.
- **`asleep_or_hibernated`** — confidence `low`. The NIC answered ARP, so it has
  standby power and is armed. **This cannot be distinguished from a full
  shutdown with WOL armed.** A NIC armed for wake keeps ARP offload alive in S5
  exactly as it does in S3/S4. Since this PC is expected to wake from full
  shutdown, that ambiguity is the normal case here, not an edge case.
- **`off`** — confidence `low`. Nothing answered at all. Usually a real power-off
  with the NIC unarmed, or unplugged. A wake may still succeed, because some
  firmware stays armed while silently dropping ARP in S5.

The useful bit is therefore **`wakeable`**, not the sleep/off distinction.

The ARP cache is force-evicted (`Remove-NetNeighbor`) and re-resolved on every
`/status`, because Windows keeps `Stale` entries for minutes after a machine
dies and reading the cache would report a dead PC as merely asleep. If eviction
fails (not elevated), `arp_forced_refresh` is `false` and confidence drops to
`very_low`.

---

## Token setup

The bearer token is **never** stored in the script. It is read from, in order:

1. `$env:JARVIS_WOL_TOKEN`
2. the `JARVIS_WOL_TOKEN=` key in `C:\ServerData\Stacks\jarvis\secrets.env`

`secrets.env` is already gitignored and already holds the Tuya local keys, so
this adds no new class of exposure. The helper **fails closed**: it refuses to
start with no token, or with one shorter than 16 characters.

Generate one:

```powershell
.\install-task.ps1 -GenerateToken
```

That appends a 64-char CSPRNG hex token and prints only its first 8 characters.
To rotate: replace the value and restart the task.

---

## Install as a boot service

Runs as `SYSTEM` with an `At Startup` trigger, so it is up before anyone logs
in — a Startup-folder shortcut would not be.

**Requires an elevated PowerShell:**

```powershell
cd C:\ServerData\Stacks\jarvis\host-helper
.\install-task.ps1 -GenerateToken      # omit -GenerateToken if a token exists
```

It registers the task **"JARVIS - WOL Helper"**, starts it immediately, and
verifies the port is bound. Settings: 30-second start delay (so DHCP has
produced a default route), restart up to 10 times at 1-minute intervals, no
execution time limit.

Check it:

```powershell
Get-ScheduledTask -TaskName 'JARVIS - WOL Helper'
Get-Content C:\ServerData\Stacks\jarvis\host-helper\wol-helper.log -Tail 30
```

---

## Test

```powershell
$tok = ((Get-Content C:\ServerData\Stacks\jarvis\secrets.env |
         Where-Object { $_ -like 'JARVIS_WOL_TOKEN=*' }) -split '=',2)[1].Trim()

Invoke-RestMethod http://127.0.0.1:8099/health
Invoke-RestMethod http://127.0.0.1:8099/status -Headers @{Authorization="Bearer $tok"}
Invoke-RestMethod http://127.0.0.1:8099/wake -Method Post -Headers @{Authorization="Bearer $tok"}
```

`Invoke-RestMethod -Method Post` sets `Content-Length: 0` itself, so it does not
hit the 411 trap.

From inside Home Assistant:

```bash
docker exec jarvis-hass curl -s http://host.docker.internal:8099/health
```

> Note when testing with `curl.exe` from PowerShell 5.1: an empty-string
> argument like `--data ''` is silently dropped, so curl consumes the next
> argument as the POST body and your `-H` header vanishes, producing a
> confusing `401`. Use `-d '{}'` instead.

---

## Uninstall

```powershell
.\install-task.ps1 -Uninstall
```

Then stop any running instance:

```powershell
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.CommandLine -match '-File\s+"?C:\\ServerData\\Stacks\\jarvis\\host-helper\\wol-helper\.ps1' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Do **not** find the process by `Get-NetTCPConnection ... OwningProcess` — an
`HttpListener` port is owned by `http.sys` and reports PID 4 (System). Killing
that does nothing useful.

Finally, remove the `JARVIS_WOL_TOKEN` line from `secrets.env`.

---

## Safety boundary

This helper can do exactly one thing to the main PC: send a wake packet.
It contains no shutdown, sleep, restart or logoff path, by design. `/status`
only pings, opens a TCP socket without sending data, and reads ARP.
