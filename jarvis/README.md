# JARVIS — Local Voice-Driven Home Assistant

Voice-activated AI assistant wired to home automation. Self-hosted, zero-cost, runs 24/7 on Docker Desktop + WSL2.

## Stack

| Service | Port | Purpose |
|---------|------|---------|
| ollama | 11434 | LLM inference engine (CPU) |
| home-assistant | 8123 | Automation and device control |
| wyoming-whisper | 10200 | Speech-to-text |
| wyoming-wake-word | 10400 | Wake word detection ("Hey Jarvis") |
| wyoming-tts | 10500 | Text-to-speech (Kokoro) |
| jarvis-ui | 3100 | Web interface + Rainmeter backend |

## Hardware

- **Compute**: Asus A15 laptop (RTX 3050, no CUDA support on this WSL2/Docker Desktop setup)
- **Storage**: C: (config volumes), D: (external if needed)
- **RAM**: 16 GB system, ~9.3 GB allocated to Ollama

**GPU works.** An earlier version of this file said passthrough had failed and the
stack ran CPU-only. That was true at the time and is no longer. The real fault was
that the RTX 3050 had *no NVIDIA driver at all* — Windows had bound its generic
`BasicDisplay` driver to the card, leaving it at "cannot start (Code 10)", dead to
every application on the machine rather than just to Docker. Installing 610.88
fixed it. No Windows reboot was needed, but Docker Desktop had to be restarted,
because WSL projects the host driver libraries into `/usr/lib/wsl/lib` only at VM
boot and a running VM keeps serving stale ones.

Measured after the fix: `library=CUDA compute=8.6 total="4.0 GiB"`, and Whisper STT
went from 2434 ms to ~270 ms — a 9x speedup.

## Start

```bash
cd /c/ServerData/Stacks/jarvis
docker compose up -d
```

Check status:

```bash
docker compose logs -f home-assistant
docker exec jarvis-ollama ollama ps
```

## Conventions

- PUID=1000, PGID=1000, TZ=Asia/Kolkata
- External named volumes (config on C:, model cache in container)
- `restart: always`
- Single `jarvis` network; services resolve by container name
- Secrets in `/c/ServerData/Stacks/secrets.env` (gitignored)

## Tuya IP watchdog

Local Tuya control binds to a device's IP address, so when DHCP moves a device the
entities go unavailable and nothing says why. The AZIOT light switch dropped off
WiFi twice on 2026-08-08 and recovered both times; this is the safety net for the
time it comes back on a different address.

The real fix is a DHCP reservation at the router (`192.168.29.1`) for both the
switch (`192.168.29.12`) and the IR blaster (`192.168.29.236`). The watchdog covers
what a reservation cannot: a router reboot that loses it, a factory reset, or a
replacement router.

```bash
python tuya-watchdog.py --check            # read-only; exit 0 clean, 1 moved, 2 offline
python tuya-watchdog.py --repair           # dry run by default -- prints, writes nothing
python tuya-watchdog.py --repair --commit  # actually rewrites the config entry
```

Install the scheduled job (every 15 minutes) from an **elevated** PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File install-watchdog-task.ps1
# -CheckOnly  alert only, never writes
# -Uninstall  remove the job
```

**What it deliberately does not do.** It never opens a Tuya connection. A Tuya
device permits exactly one local session at a time and Home Assistant holds it, so
a probe from outside HA returns `914` and the port reads closed — that is healthy,
not a fault, and a watchdog that read it as "device down" would flap constantly and
be worse than no watchdog. Liveness is decided instead from the ARP table, Home
Assistant's own entity availability, and HA's ESTABLISHED sockets to port 6668.

**How a moved device is found again.** By MAC. Note that the Device ID embeds the
MAC for the blaster (`853521608cce4edb3d49` -> `8c:ce:4e:db:3d:49`) but **not** for
the switch, whose ID ends in non-hex characters and whose derived MAC appears
nowhere on the network. So the switch's MAC is learned from ARP whenever it is
confirmed at its configured address and cached in `tuya-watchdog-state.json`. If
that file is deleted while the switch is already on a new address, it cannot be
rediscovered until one check runs with the switch back at its configured IP. The
file holds MACs and IPs only, never local keys, and is gitignored.

**Not yet verified:** the live repair path has never run against a real DHCP
change. To test it for real, change the switch's reservation at the router,
power-cycle the switch, run `--check` (expect `MOVED`, exit 1), then
`--repair --commit`, and confirm `light.node_switch_gang_1` becomes available and
physically toggles. Restore the reservation afterwards.
