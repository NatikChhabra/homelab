#!/usr/bin/env python3
"""Detect the machine and choose a runtime profile. Never assume CUDA."""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

PROFILE_PATH = Path("numen/config/hardware.json")


@dataclass
class Hardware:
    os: str
    cpu_cores: int
    ram_gb: float
    gpu_vendor: str      # nvidia | amd | apple | intel | none
    gpu_name: str
    vram_mb: int
    backend: str         # cuda | rocm | vulkan | metal | cpu
    tier: str            # minimal | standard | full
    llm_model: str
    stt_model: str
    vision: str          # local | api_key | unavailable
    notes: list[str]


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


def detect_gpu() -> tuple[str, str, int]:
    """Returns (vendor, name, vram_mb). Checks NVIDIA, then AMD, then Apple, then none."""
    if shutil.which("nvidia-smi"):
        out = _run(["nvidia-smi", "--query-gpu=name,memory.total",
                    "--format=csv,noheader,nounits"])
        if out.strip():
            name, mem = out.strip().splitlines()[0].split(",")
            return "nvidia", name.strip(), int(mem.strip())

    if shutil.which("rocm-smi"):
        out = _run(["rocm-smi", "--showproductname", "--showmeminfo", "vram"])
        if out.strip():
            return "amd", "AMD GPU (see rocm-smi)", _parse_rocm_vram(out)

    if platform.system() == "Windows":
        out = _run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_VideoController | "
                    "Select-Object Name,AdapterRAM | ConvertTo-Json"])
        if out.strip():
            try:
                data = json.loads(out)
                data = data if isinstance(data, list) else [data]
                for gpu in data:
                    name = (gpu.get("Name") or "").strip()
                    vram = int((gpu.get("AdapterRAM") or 0) / (1024 * 1024))
                    low = name.lower()
                    if "nvidia" in low or "geforce" in low or "rtx" in low:
                        return "nvidia", name, vram
                    if "amd" in low or "radeon" in low:
                        return "amd", name, vram
                    if "intel" in low:
                        return "intel", name, vram
            except Exception:
                pass

    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return "apple", "Apple Silicon (unified memory)", 0

    return "none", "No discrete GPU detected", 0


def _parse_rocm_vram(out: str) -> int:
    for line in out.splitlines():
        if "vram" in line.lower() and "total" in line.lower():
            digits = "".join(c for c in line if c.isdigit())
            if digits:
                return int(digits) // (1024 * 1024)
    return 0


def choose_backend(vendor: str) -> str:
    return {
        "nvidia": "cuda",
        "amd": "rocm" if platform.system() == "Linux" else "vulkan",
        "apple": "metal",
    }.get(vendor, "cpu")


def choose_tier(vram_mb: int, ram_gb: float, vendor: str) -> str:
    """Apple unified memory: treat system RAM as usable VRAM."""
    effective = ram_gb * 1024 * 0.6 if vendor == "apple" else vram_mb
    if effective >= 12_000:
        return "full"
    if effective >= 3_500:
        return "standard"
    return "minimal"


def profile() -> Hardware:
    vendor, name, vram = detect_gpu()
    backend = choose_backend(vendor)
    try:
        import psutil
        ram = round(psutil.virtual_memory().total / (1024 ** 3), 1)
        cores = psutil.cpu_count(logical=False) or 1
    except Exception:
        ram, cores = 8.0, 4

    tier = choose_tier(vram, ram, vendor)
    notes: list[str] = []

    if tier == "full":
        llm, stt, vision = "qwen3:4b-instruct", "small-int8", "local"
    elif tier == "standard":
        llm, stt, vision = "qwen2.5:3b", "small-int8", "api_key"
        notes.append("Vision needs 12GB+ VRAM or your own API key.")
    else:
        llm, stt, vision = "qwen2.5:1.5b", "base-int8", "api_key"
        notes.append("No usable GPU found — running on CPU. Replies will be slower.")
        notes.append("Smart home commands stay instant; they never use the model.")

    if backend == "vulkan":
        notes.append("AMD on Windows: Vulkan backend. CUDA-only tools (faster-whisper, "
                     "TensorRT) are unavailable — using whisper.cpp instead.")
    if backend == "rocm":
        notes.append("AMD on Linux: ROCm backend.")

    return Hardware(platform.system(), cores, ram, vendor, name, vram,
                    backend, tier, llm, stt, vision, notes)


def save(hw: Hardware) -> None:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(asdict(hw), indent=2))


if __name__ == "__main__":
    hw = profile()
    save(hw)
    print(json.dumps(asdict(hw), indent=2))
    print(f"\nProfile: {hw.tier} tier, {hw.backend} backend")
    for n in hw.notes:
        print(f"  - {n}")
