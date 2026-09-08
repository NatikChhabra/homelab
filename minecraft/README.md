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

Not yet scheduled. Add it to Task Scheduler alongside the other jobs once you
are happy with it.

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

Then add a Minecraft widget in Homarr with:

- **Server address:** `minecraft:25565` — the container name, *not* the
  Tailscale IP. Homarr runs status checks server-side from inside its own
  container, which is why it joins every stack network. Same reasoning as every
  existing tile.
- **RCON:** host `minecraft`, port `25575`, password from `Stacks/minecraft/.env`

Do not point the status check at the server's Tailscale address — it will show
red even when the server is perfectly healthy.

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

## Adding more mods (next phase)

Not started — you asked to confirm this is right first.

When we do: the pack's own mods come from CurseForge via `AUTO_CURSEFORGE` and
must not be hand-edited. Additions go through `CF_FORCE_INCLUDE_MODS` or a
separately mounted directory, so a pack update cannot silently delete them.
Every client must run a byte-identical mod set — which is the main reason
server-side-only additions (Paper-style plugins) are attractive, and NeoForge
does not offer them.

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
