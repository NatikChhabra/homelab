# Minecraft — Tensura Evolutions (NeoForge 1.21.1)

**Status: working. Deliberately stopped when not in use.**

Tensura Evolutions **1.0.69.9** · MC **1.21.1** · NeoForge **21.1.249** ·
**253** server mods installed from 313 declared (23 excluded as client-only).
Verified up with `rcon-cli list` and `mc-monitor`, not just a log line.

---

## Read this first

### Minecraft is opt-in on this host. Start it, play, stop it.

```powershell
cd C:\ServerData\Stacks\minecraft
.\mc-gamemode.ps1 -On           # park ~3.9 GB of optional stacks
docker compose up -d            # start the server (~1-2 min after first boot)

# ... play ...

docker compose stop             # STOP IT when you finish
.\mc-gamemode.ps1 -Off          # bring your services back
```

Add `-IncludeJarvis` to `-On`/`-Off` to also park the JARVIS stack (~2.75 GB,
mostly `jarvis-ollama`). That **breaks the voice loop** until `-Off`, so it is
opt-in and never part of a plain `-On`.

### Why it must be stopped — this took the house offline once

**Incident, 2026-09-09.** Minecraft holds ~5.4 GB on a 15.4 GB host and
originally carried `restart: always`, so it resurrected itself on every Docker
engine restart — including with nobody playing. The WSL VM reached
**SwapFree 880 kB of 3.1 GB**. Two ordinary `docker` commands timed out at 120 s.

AdGuard **never stopped and never crashed** — `status=running`, `restarts=0`,
game mode protects it unconditionally. But it was starved badly enough that its
upstream DoH queries timed out:

```
dnsproxy: requesting https://family.adguard-dns.com/dns-query:
  Client.Timeout exceeded while awaiting headers
```

So **DNS for the entire house went down while every container looked healthy.**

Two changes came out of that, and neither should be undone:

1. **`restart: unless-stopped`**, a deliberate exception to this repo's
   `restart: always` convention. A manual stop now survives engine restarts and
   reboots. Minecraft cannot resurrect itself. It still restarts if it crashes
   while genuinely running.
2. **Audit checks for the leading indicator.** Section 17 already catches DNS
   *after* it fails; section 26 now warns on VM available memory (<1500 MB) and
   swap headroom (<40% free), and fails below 700 MB / 15%. That is the
   condition that causes the outage, caught while there is still time.

### It cannot run alongside everything else — measured, not estimated

| State | VM MemAvailable | Swap free | Result |
|---|---|---|---|
| All 34 containers + Minecraft | **857 MB** | **880 kB / 3.1 GB** | thrashing, 3 restarts, DNS down |
| Optional stacks parked + Minecraft | 2.1 GB | recovering | Minecraft + JARVIS both healthy |
| Minecraft stopped | 5.6 GB | 1.0 GB | comfortable |

Host is **15.4 GB, 2×8 GB, both SODIMM slots full**, board maxes at 32 GB. The
WSL VM is capped at 10 GB. Heap is **5G** with `mem_limit: 6656m`; peak during
mod loading was 96% of that limit with no OOM. The pack asks for 8–12 GB — this
box cannot give that, and no amount of tuning changes it.

**The real fix is RAM.** 2×16 GB DDR4-3200 is roughly ₹5,000–7,000 and removes
every compromise here: game mode becomes optional, the heap goes to 8–10 GB, and
nothing has to be parked to play.

> `.wslconfig` is already set to `memory=11GB` but has **never applied** —
> `MemTotal` is still 10 GB. It needs a true `wsl --shutdown`, which takes the
> whole server down and has not been authorised.

---

## Connecting

**Java Edition only**, `<server-tailscale-ip>:25565`, with the Tensura
Evolutions pack installed in your launcher. Tailscale only — do **not** forward
25565 to the public internet.

### Bedrock cannot join this pack

Geyser is the only Java↔Bedrock bridge, and its own FAQ is explicit:

> there is no way for Geyser to translate the features that most mods add
> (blocks, items, etc.), therefore servers that require mods to be installed
> clientside are unsupportable through Geyser.

[Rainbow](https://github.com/GeyserMC/rainbow), Geyser's official custom-content
converter, does not rescue it either: it is **Fabric-only** (this is NeoForge)
and converts *appearance*, not behaviour. Tensura is its mechanics — skills,
evolution, quests — and a Bedrock client has no code to run any of it.

Port 19132 is therefore deliberately absent. **If Bedrock players matter more
than this pack**, the answer is a separate Paper + Geyser + Floodgate server:
real crossplay, no client installs, different world. It will not fit alongside
this one without more RAM.

---

## Client-side mods (your PC, not the server)

### Graphics — Iris and Sodium are already in the pack

**Do not install them by hand.** They appear in the server's client-only
exclusion list, which means they ship inside the client zip and install with the
pack. A second copy is a hard crash at launch, not a warning.

You already have the shader engine. What is missing is a **shader pack** — drop
one in `shaderpacks/` and select it under Video Settings → Shaders:

| Pack | Why |
|---|---|
| **Complementary Reimagined** *(recommended)* | Best-optimised of the good-looking packs, scalable presets — start on Medium. |
| **BSL Shaders** | Prettier water and skies, slightly heavier. |

Avoid SEUS PTGI / Rethinking Voxels — path-traced and unplayable next to 313 mods.

### Controller / gamepad

The pack ships *Controlling*, which is a **keybind-search screen** and has
nothing to do with controllers — an easy name to misread.

For an actual gamepad, add
**[Controllable](https://www.curseforge.com/minecraft/mc-mods/controllable)** by
MrCrayfish, **0.25.4 for NeoForge 1.21.1**: analog 360° movement, remappable
buttons, adjustable sensitivity, radial menu. Xbox, PlayStation and generic pads.

**Client-side only** — drop the jar in your instance's `mods/`. It does not go
on the server and nobody else needs it. Client-only input mods are the one
category that does *not* have to match the server's mod set.

### Server-side performance mods (optional)

Chosen because they attack **memory**, this server's actual bottleneck:
**FerriteCore** (40–50% memory reduction), **ModernFix** (~20% less RAM),
**Clumps** (fewer entity ticks). Check they are not already present first — a
duplicate jar is a hard crash:

```bash
docker exec minecraft sh -c 'ls /data/mods | grep -iE "ferrite|modernfix|clumps"'
```

Add them via `CF_FORCE_INCLUDE_MODS`, never by hand-dropping jars —
`AUTO_CURSEFORGE` reconciles `/data/mods` against the manifest on every start
and would silently delete them.

---

## Admin

```bash
docker exec minecraft rcon-cli list             # who is online
docker exec minecraft rcon-cli op <YourName>    # grant yourself operator
docker exec minecraft rcon-cli save-all         # force a save
```

`OPS` is empty in the compose file — set it to your Minecraft username and
restart, or use `rcon-cli op` above. RCON port 25575 is deliberately **not**
published; these reach it from inside the container.

### Backups

```bash
bash mc-backup.sh    # world only -> D:\Backups\minecraft, 7-day retention
```

Scheduled daily 03:30 as `ServerData - Minecraft World Backup` via
`run-job.ps1 -Job mc-backup`, inheriting its locking, transcripts, rotation and
failure reporting. Proven end-to-end against the live server: RCON flush,
saving paused, tar, saving re-enabled, archive verified readable.

`minecraft_data` is **excluded** from the nightly `backup-volumes.sh` — ~15 GB of
mostly re-downloadable modpack × 14 days would not fit on a 180 GB drive. Only
the world, the irreplaceable part, is retained here.

The status file reports `heartbeatConfigured: false`, which is **accurate**: no
Uptime Kuma token exists for this job. Audit section 26 covers the gap by
failing when no archive is newer than 8 days.

---

## Homarr tile

A **Gaming** section on the dashboard holds a native `minecraftServerStatus`
widget (`minecraft:25565`), a Portainer shortcut for container control, and a
command notebook.

**Set the widget refresh to 10 minutes.** Not a preference: every status ping is
a TCP connect, and a TCP connect *wakes an auto-paused server* — itzg's
autopause cannot tell a ping from a login. At the default rate the server never
pauses. At 10 minutes it wakes, waits 120 s, and re-pauses.

For the same reason, **never add an Uptime Kuma monitor on 25565** — it polls
every 60 s and would pin the JVM permanently awake. Monitor the container instead.

Use the **container name**, not the Tailscale address: Homarr runs status checks
server-side from inside its own container, which is why it joins every stack
network.

> Tiles live in Homarr's SQLite DB, not in git. Homarr runs as **root** — never
> chown its `db.sqlite` to 1000. Back up `homarr_data` before touching it; a
> copy is in `homarr/db-backups/`.

---

## Design decisions worth knowing

| Choice | Why |
|---|---|
| **`dns: 1.1.1.1 / 8.8.8.8`** | Required, not decorative. Without it the installer failed 9× with `UnknownHostException: api.curseforge.com` / SERVFAIL, while `OOMKilled=false` and exit code 0 — it reads as a memory fault and is not one. Same defect that silently kills TMDb lookups elsewhere in this repo. |
| **`CF_SLUG` *and* `CF_MODPACK_ZIP`** | Both required. The zip alone errors with *"A modpack page URL or slug identifier is required even with a provided modpack zip"*. The pack authors set CurseForge's third-party-distribution flag to **not allowed**, so the API will never serve it — a human downloads it once to `modpack/modpack.zip`, which is also the version pin. |
| **`restart: unless-stopped`** | See the incident above. At 5.4 GB on this host, `always` is how DNS died. |
| **`mem_limit: 6656m`** | Load-bearing. Without a ceiling the JVM exhausts the VM and the OOM killer scores `jarvis-ollama` before Minecraft. With it, Docker kills Minecraft and the blast radius stays in this stack. |
| **Named volume, not a bind mount** | Docker Desktop serves Windows bind mounts through a slow translation layer — exactly wrong for a world's thousands of small region files. |
| **`ENABLE_AUTOPAUSE`** | Frees **CPU, not RAM**. Measured: host CPU averaged 21.9% across 16 logical processors, so CPU was never the scarce resource. Kept for the marginal win; do not trade anything valuable for it. |
| **`SIMULATION_DISTANCE: 6`** (default 10) | Largest CPU/RAM saving available without touching gameplay; drives entity ticking and scales roughly quadratically. |
| **World on C: (SSD), backups on D: (HDD)** | D: is a 5400 rpm drive shared with the media library and live torrents; a world there would contend with qBittorrent and stutter on save. |

---

## Ports

| Port | Purpose | Published? |
|---|---|---|
| 25565 | Java Edition | yes |
| 25575 | RCON | **no** — internal to `minecraft_default` only |
| 19132 | Bedrock | **no** — cannot work with this pack |

**Ordering trap:** `minecraft_default` only exists once this stack has started
once. Running `docker compose up -d` in `Stacks/homarr` before that fails with
*"network minecraft_default not found"* and takes the dashboard down.

---

## Rollback

```powershell
docker compose down
docker volume rm minecraft_data          # destroys the world - be sure
```

World archives are in `D:\Backups\minecraft\`. Restore with the command in the
header of `mc-backup.sh`. Edited files have `.bak.<UTC-timestamp>` copies
alongside them.
