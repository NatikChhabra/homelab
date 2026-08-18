import asyncio
import os
import sys
import time
import numpy as np
import aiohttp

sys.path.insert(0, r"C:\ServerData\Stacks\jarvis")
from router import IntentRouter

HASS = os.getenv("HASS_URL", "http://localhost:8123").rstrip("/")
TOKEN = os.getenv("JARVIS_HASS_TOKEN") or os.getenv("HA_API_TOKEN", "")
if not TOKEN:
    _secrets = r"C:\ServerData\Stacks\secrets.env"
    if os.path.isfile(_secrets):
        with open(_secrets) as f:
            for line in f:
                if line.startswith("JARVIS_HASS_TOKEN="):
                    TOKEN = line.strip().split("=", 1)[1].strip('"').strip("'")
                    break

COMMANDS = [
    "turn on the lights",
    "turn off the lights",
    "lights on",
    "lights off",
    "turn on light one",
    "turn off light two",
    "set the ac to 22",
    "ac on",
    "ac off",
    "set the fan to speed 3",
    "boost the fan",
    "good night",
    "movie time",
    "I'm home",
    "set the ac to 24",
    "fan speed 3",
]

async def measure():
    async with aiohttp.ClientSession() as session:
        router = IntentRouter(HASS, TOKEN, hermes_enabled=False)
        latencies = []
        for i in range(50):
            utt = COMMANDS[i % len(COMMANDS)]
            t0 = time.perf_counter()
            res = await router.process(session, utt, {})
            dt_ms = (time.perf_counter() - t0) * 1000.0
            latencies.append(dt_ms)
            assert res.path in ("local", "greeting"), f"Command {utt!r} fell through to {res.path}"

        latencies = np.array(latencies)
        p50 = np.percentile(latencies, 50)
        p95 = np.percentile(latencies, 95)
        p99 = np.percentile(latencies, 99)

        print(f"50 Smart Home Commands Measured:")
        print(f"  p50: {p50:.2f} ms")
        print(f"  p95: {p95:.2f} ms")
        print(f"  p99: {p99:.2f} ms")
        print(f"  min: {np.min(latencies):.2f} ms | max: {np.max(latencies):.2f} ms")

asyncio.run(measure())
