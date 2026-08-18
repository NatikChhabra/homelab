# NUMEN.md — operating contract

## What this is
A local-first personal AI. Voice, smart home, PC, phone, messages, files. Everything runs on
the user's own hardware unless they explicitly choose otherwise.

## The differentiator
Every action is authorised on four axes, ALL of which must pass:
  WHO asked × WHICH DEVICE they asked from × WHAT capability × WHICH DEVICE it targets
Devices are cryptographically enrolled (Ed25519). Every request is signed.
Verdicts: allow / confirm / notify-owner / deny.
The claim: a perfect voice match from an unenrolled device is REFUSED.

## THE LATENCY LAW — the most important rule here
Smart home commands NEVER touch a language model. Lights, fan, AC, scenes, TV = pattern
match, under 50ms, always. If a light switch reaches the LLM, that is a BUG, not slowness.
Anything requiring reasoning may take longer, but must say so within 300ms.

## Hardware adaptation
Detect at setup and configure automatically. NVIDIA→CUDA, AMD→ROCm/Vulkan, Apple→Metal,
none→CPU. NEVER assume CUDA — the founder's own second machine is AMD.
4GB is the design target: LLM resident, STT/TTS load on demand, vision is API-key or 12GB+.
Degrade, never fail. No GPU means a smaller model and a clear message, not an error.

## Never
- assume a device count, entity ID, room name, GPU vendor, or model
- report success before the system confirms it
- print a secret, in whole or in part — verify by count: grep -c "^KEY=" file → 1
- docker cp anything that must survive a rebuild — bind-mount or bake
- git checkout <path> / restore / reset --hard / clean / stash drop / `>` into a source file
- claim certainty about a device that cannot report state

## Always
- evidence over description: "done" means pasted command output
- one task per commit
- state a rollback in one line
- ask the user before anything irreversible
- skip and continue when blocked; never stall waiting for me
