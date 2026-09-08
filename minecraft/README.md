# Minecraft — Tensura Evolutions (NeoForge 1.21.1)

**Status: built and validated, NOT started.** Starting it needs one decision
from Natik first — see [Before first start](#before-first-start).

---

## Read this first: two things do not work the way you asked

### 1. Bedrock crossplay is not possible with this modpack

You asked for Java + Bedrock and said "idk if mods will be able to do that."
They cannot. This is not a tuning problem, and no configuration fixes it.

Geyser — the only Java/Bedrock bridge — states it plainly in its own FAQ:

> there is no way for Geyser to translate the features that most mods add
> (blocks, items, etc.), therefore servers that require mods to be installed
> clientside are unsupportable through Geyser.

Tensura Evolutions is built entirely out of custom blocks, items, mobs,
dimensions and skills. A Bedrock client has no way to receive or render any of
it. Geyser *does* officially support NeoForge as a platform — that part of the
idea was right — but the platform working and the *content* translating are
different things, and only the first is true.

So this stack is **Java Edition only**, and port 19132 is deliberately absent.
Publishing it would create a port that either refuses joins or drops players
into a world of invisible blocks, which is worse than not offering it at all.

**If Bedrock players matter more than this modpack**, the answer is a second,
separate Paper server with Geyser + Floodgate — vanilla-compatible, everyone
joins, no client installs. It cannot run at the same time as this one on this
hardware (see below). Say so and it gets built instead.

### 2. The pack wants 8-12 GB. This machine can honestly give 6 GB.

You said "give as much as it needs, idc." The amount it needs is not available.
Better to say that now than to set 8G quietly and let it surface as stutter.

| | |
|---|---|
| Host RAM | **15.4 GB** (2x8 GB, **both slots full**, board maxes at 32 GB) |
| Available at time of build | **776 MB** |
| WSL2 VM cap (`.wslconfig`) | 10 GB |
| Containers already inside it | ~7.0 GB across 33 containers |
| VM swap in use | **2923 / 3072 MB — thrashing before Minecraft exists** |
| Tensura Evolutions asks for | 8 GB minimum, 12 GB recommended |

The arithmetic does not close. Even with game mode stopping everything
non-essential (~3.9 GB reclaimed, measured):

```
  remaining containers (JARVIS + AdGuard + Homarr + Vaultwarden)   ~3.3 GB
  Minecraft at 6 GB heap + ~1.3 GB JVM overhead                    ~7.3 GB
  WSL VM kernel and cache                                          ~0.5 GB
                                                                  --------
  WSL VM total                                                    ~11.1 GB
  Windows itself                                                   ~3.0 GB
                                                                  --------
  TOTAL                                                           ~14.1 GB  of 15.4 GB
```

That leaves ~1.3 GB of margin and **requires Edge closed while playing** (it was
holding 2.6 GB during the build). At an 8 GB heap the total exceeds physical RAM
and the machine pages to disk, which on a modded server means multi-second
freezes — worse than the smaller heap.

**The real fix is RAM.** Both SODIMM slots hold 8 GB DDR4-3200 and the board
supports 32 GB. Replacing them with 2x16 GB is roughly Rs 5,000-7,000 and makes
every compromise here disappear: game mode becomes optional, the heap goes to
10 GB, and JARVIS stops competing. It is the highest-value change available and
it is a purchase, not a configuration.

---

## Before first start

Two things, in this order.

**1. The WSL VM cap must go from 10 GB to ~11.5 GB**, or Minecraft cannot fit.
`.wslconfig` is read only when the VM starts, so this needs a `wsl --shutdown`
— which **stops every container**, including AdGuard (DNS for the whole
tailnet) and the JARVIS voice loop. That is a RED action under
`Stacks/CLAUDE.md` section 11 and is **not** being done unattended. Pick a
window.

Note `.wslconfig` already carries a pending unapplied change:
`autoMemoryReclaim=gradual`, added 2026-07-28, has never taken effect for
exactly this reason. The same restart applies both.

**2. Then, in this order** — the order matters, see the ordering trap below:

```powershell
cd C:\ServerData\Stacks\minecraft
.\mc-gamemode.ps1 -On          # free ~3.9 GB first
docker compose up -d           # first boot pulls ~600 MB, takes 5-15 min
docker logs -f minecraft       # wait for: Done (...)! For help, type "help"
```

First boot downloads the modpack from CurseForge and generates the world. It is
slow, and that is normal. Later starts are fast.

---

## Pin the pack version after the first successful boot

`CF_FILENAME_MATCHER` is deliberately left commented out. A matcher that does
not match makes the container fail at boot, so guessing one is worse than
leaving it unset. First boot resolves "latest"; find what it actually chose:

```bash
docker logs minecraft | grep -iE "resolved|downloading|serverpack"
```

Put that exact value into `CF_FILENAME_MATCHER` and `docker compose up -d`.
Until then, a pack update can land on the next container recreate and change the
mod set under an existing world.

---

## Everyday use

```powershell
.\mc-gamemode.ps1 -Status      # what is running + live memory picture
.\mc-gamemode.ps1 -On          # before playing  (stops ~3.9 GB of stacks)
.\mc-gamemode.ps1 -Off         # AFTER playing   (brings them all back)
```

**Forgetting `-Off` is the failure this setup is most likely to hit.** Sonarr,
Radarr and Immich stay down, nothing downloads, and nothing says so. Audit
section 26 detects it and reports it as one cause rather than letting it appear
as ten unrelated port failures.

The script refuses to touch `jarvis`, `adguard`, `homarr`, `vaultwarden` or
`minecraft` even if its own list is edited carelessly later.

### Backups

```bash
bash mc-backup.sh              # world only -> D:\Backups\minecraft, 7-day retention
```

`minecraft_data` is **excluded** from the nightly `backup-volumes.sh`: it is
~15 GB, mostly a re-downloadable modpack, and at 14-day retention would put
~210 GB onto a 180 GB drive. The world — the only irreplaceable part — is backed
up by this script instead. It flushes through RCON first and re-enables saving
even if the tar fails.

**Scheduled and verified.** `ServerData - Minecraft World Backup` runs daily at
03:30 via the existing `run-job.ps1` wrapper (`-Job mc-backup`), so it inherits
that wrapper's locking, transcript logging, log rotation and failure reporting
rather than reinventing them. 03:30 was chosen to avoid the 04:00 / 04:30 /
05:00 / 05:30 slots the other jobs already hold.

Its status file reports `heartbeatConfigured: false`, and that is **accurate,
not a fault**: no Uptime Kuma push token exists for this job yet. The gap is
covered by audit section 26, which fails when no world archive is newer than
8 days — so a silently dead backup surfaces weekly rather than never. Create
`KUMA_PUSH_MC_BACKUP` and set it in `run-job.ps1` if you want same-day notice.

### Do not add an Uptime Kuma monitor for port 25565

Same trap as the Homarr tile, but worse: Kuma polls every 60 s by default, and
every poll is a TCP connect that wakes an autopaused server. A Kuma monitor
would keep the JVM permanently awake. If you want Kuma coverage, monitor the
*container* rather than the port.

---

## Design decisions worth knowing

| Choice | Why |
|---|---|
| **Named volume, not a bind mount from C:** | Docker Desktop on Windows serves bind mounts through a filesystem translation layer that is slow for many small files — precisely a Minecraft world. A named volume sits on the VM native ext4 and skips it. |
| **`mem_limit: 7680m`** | Load-bearing. Without a ceiling, an unbounded JVM exhausts the VM and the Linux OOM killer picks its victim by score — which is `jarvis-ollama` or a voice container long before it is Minecraft. With it, Docker kills Minecraft and `restart: always` brings it back. The voice loop is never the casualty. |
| **`ENABLE_AUTOPAUSE`** | SIGSTOPs the JVM when nobody is connected. Frees **CPU, not RAM** — the heap stays resident. It is here because an idle modded server still ticks, and those ticks compete with Whisper STT and Kokoro TTS for the same 8 cores. |
| **`MAX_TICK_TIME: -1`** | Required by autopause: a resumed process looks like one enormous tick and the watchdog would kill the server. Independently needed for a heavy pack, where chunk generation legitimately blocks past the 60 s default. |
| **`SIMULATION_DISTANCE: 6`** (default 10) | The largest CPU and RAM saving available without touching gameplay. It drives entity ticking and scales roughly quadratically. 6 is still beyond normal render range. |
| **`VIEW_DISTANCE: 8`** (default 10) | Same reasoning, smaller effect. |
| **`USE_AIKAR_FLAGS`** | Well-established G1GC tuning for Minecraft. Reduces GC pause spikes, which matter more here than raw throughput. |
| **RCON on, port not published** | RCON is a full server console. Homarr reaches it by container name over `minecraft_default`. It must never be bound to a host port. |
| **World on C: (SSD), backups on D: (HDD)** | D: is a 5400 rpm drive shared with the media library and live torrents; a world there would contend with qBittorrent for head time and stutter on save. C: has 66 GB free, which is ample. |

---

## Homarr tile

**Ordering trap:** `minecraft_default` does not exist until the Minecraft stack
has been started once. Running `docker compose up -d` in `Stacks/homarr` before
that fails with *"network minecraft_default not found"* and takes the dashboard
down — the one place you would look to find out what broke. The compose file
carries the same warning.

After Minecraft is up:

```powershell
cd C:\ServerData\Stacks\homarr
docker compose up -d
```

### What to add at `:7575`

Three tiles. Together they cover status, control and console, and they add
**zero** new containers — which matters, because RAM is the constraint.

**1. Minecraft server status widget** — online/offline, player count, MOTD.

- **Server address:** `minecraft:25565` — the container name, *not* the
  Tailscale address. Homarr runs status checks server-side from inside its own
  container, which is why it joins every stack network. Same reasoning as every
  existing tile. Pointed at the Tailscale IP it shows red while the server is
  perfectly healthy.
- **Refresh interval: set this to 10 minutes. Not the default.** This is not a
  preference. Every status ping is a TCP connect, and a TCP connect *wakes an
  autopaused server* — itzg's autopause cannot tell a ping from a login. At the
  default poll rate the server never pauses at all and the feature is dead
  config. At 10 minutes it wakes, waits 120 s, and re-pauses: paused ~8 minutes
  in every 10, and the tile still works.

**2. A link tile to Portainer** (`:9443`) for start / stop / restart / logs.

Homarr **cannot** do this itself — it has no Docker socket mounted, and it is
not getting one. Portainer already holds
`/run/host-services/docker.proxy.sock` and already does the job. Mounting the
socket into a second web-facing container would duplicate an existing
capability while doubling the blast radius of anything that reaches it: the
Docker socket is root-equivalent on the host.

**3. A bookmark tile for the console one-liner** (Homarr cannot run commands):

```bash
docker exec minecraft rcon-cli <command>
# e.g.  rcon-cli list          - who is online
#       rcon-cli op Natik      - grant yourself operator
#       rcon-cli save-all      - force a save before a backup
```

RCON port 25575 is deliberately unpublished; this reaches it from inside the
container, so nothing is exposed. Note that `docker exec` also wakes a paused
server, for the same reason as the status ping.

---

## Ports

| Port | Purpose | Published? |
|---|---|---|
| 25565 | Java Edition | yes |
| 25575 | RCON | **no** — internal to `minecraft_default` only |
| 19132 | Bedrock | **no** — cannot work with this modpack, see above |

No collision with the allocation listed in `Stacks/CLAUDE.md`.

**Tailscale only.** Do not forward 25565 to the public internet. Friends join
over the tailnet, like everything else here.

---

## Graphics and extra mods (phase 2, not installed)

### First, the thing that trips everyone up

**Tensura Evolutions is already a modpack** — hundreds of curated mods. Most of
what feels "missing" is already inside it. Bolting extra gameplay mods onto a
curated quest/progression pack is the standard way to break the quest book,
the balance, or both. So: performance and visuals only, and leave the pack's
own progression alone.

**Shaders and rendering mods are 100% client-side.** Nothing about graphics goes
on this server. The server has no GPU involvement whatsoever — it does not
render a single frame. All of the below goes into *your launcher profile on your
main PC*, and only the server-side section touches this box.

### Client side — graphics (your PC, your launcher)

For NeoForge 1.21.1 the stack is **Sodium (NeoForge build) + Iris**.

Not Embeddium, and not Oculus. Those are the legacy path for 1.20.1 and older,
where native Iris did not exist yet. On 1.21.1 Iris supports NeoForge directly
and wants the official NeoForge build of Sodium. Installing the Embeddium/Oculus
pair here is the most common way to end up with a broken launcher on this
version.

Shader pack, at the "average, not crushing" weight you asked for:

| Pack | Why |
|---|---|
| **Complementary Reimagined** *(recommendation)* | The best-optimised of the good-looking packs, and it ships scalable presets — start on Medium. Looks substantially better than vanilla without the frame cost of the heavy packs. |
| **BSL Shaders** | Equally popular, slightly prettier water and skies, slightly heavier. Fine alternative if you prefer the look. |

Avoid the ray-traced / path-traced packs (SEUS PTGI, Rethinking Voxels) — they
are far past "average" and would be unplayable alongside a 400-mod pack.

### Server side — performance mods

Chosen specifically because they attack **memory**, which is this server's
actual bottleneck, rather than frame rate, which is not the server's problem:

| Mod | Effect |
|---|---|
| **FerriteCore** | 40–50% memory reduction. By far the highest-value addition on a RAM-starved host. |
| **ModernFix** | ~20% less RAM, roughly 2× faster startup. |
| **Clumps** | Merges XP orbs, cutting entity ticks from grinders. |

**Verify these are not already in the pack before adding them.** Well-built packs
usually ship FerriteCore and ModernFix already, and installing a second copy
causes a hard crash at launch, not a warning. Check after first boot with:

```bash
docker exec minecraft sh -c 'ls /data/mods | grep -iE "ferrite|modernfix|clumps"'
```

### How to add them, once verified

Additions go through `CF_FORCE_INCLUDE_MODS` in the compose file, never by
hand-dropping jars into the volume — `AUTO_CURSEFORGE` reconciles `/data/mods`
against the pack manifest on every start, so a hand-placed jar is deleted the
next time the pack updates, silently and without an error.

Every client must run a **byte-identical** mod set to the server. Each addition
means everyone reinstalls. This is the main reason server-side-only additions
(Paper-style plugins) are attractive — and NeoForge does not offer them, which
is the trade you accepted by picking a real modpack over crossplay.

---

## Rollback

Nothing has been started, so rollback is deletion:

```powershell
docker compose down                      # only if it was ever started
docker volume rm minecraft_data          # destroys the world - be sure
Remove-Item -Recurse C:\ServerData\Stacks\minecraft
```

Then restore the three edited files from their `.bak.20260908T195927Z` copies:
`backup-volumes.sh`, `homarr/docker-compose.yml`, `server-audit.sh`.
