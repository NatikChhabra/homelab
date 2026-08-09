"""Does the GPU's idle power state cost the first request after a pause?

The card sits at P8 / 210 MHz when idle against a 2100 MHz maximum, a 10x clock
difference, so the first request after a quiet period could plausibly pay a ramp
penalty that never shows up in a back-to-back benchmark. Voice is exactly the
workload that always arrives after idle, so if the penalty is real it is paid on
literally every real command while being invisible to every benchmark.

Method: wait for a genuinely idle GPU (verified by polling nvidia-smi, not
assumed, because other workers are benchmarking on this same card), record the
pstate immediately before firing, then fire one request and immediately fire a
second. The difference between them is the cold-start cost.
"""

import json
import statistics
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, r"C:\ServerData\Stacks\jarvis\perf")
import measure  # noqa: E402

MODEL = "qwen2.5:3b"
SYSTEM = measure.PROMPT + "\n" + measure.BOILER
UTT = "set the AC to 22"


def gpu():
    p = subprocess.run(
        ["docker", "exec", "jarvis-ollama", "nvidia-smi",
         "--query-gpu=pstate,clocks.sm,power.draw,utilization.gpu",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, errors="replace")
    parts = [x.strip() for x in (p.stdout.strip().splitlines() or [""])[0].split(",")]
    if len(parts) < 4:
        return {"pstate": "?", "sm_mhz": 0, "watt": 0.0, "util": 0}
    return {"pstate": parts[0], "sm_mhz": int(parts[1]),
            "watt": float(parts[2]), "util": int(parts[3])}


def wait_idle(seconds, timeout=180):
    """Wait until the GPU has been quiet for `seconds` consecutive seconds."""
    t_start = time.time()
    quiet = 0
    while time.time() - t_start < timeout:
        g = gpu()
        if g["util"] == 0 and g["sm_mhz"] < 800:
            quiet += 1
            if quiet >= seconds:
                return True, g
        else:
            quiet = 0
        time.sleep(1)
    return False, gpu()


def fire():
    return measure.chat(UTT, MODEL, 8192, None, True, SYSTEM)


def main():
    idle_targets = [10, 30, 60]
    measure.chat("hello", MODEL, 8192, None, True, SYSTEM)  # ensure loaded
    rows = []
    for want in idle_targets:
        ok, g = wait_idle(want)
        if not ok:
            print("  idle %2ds: GPU never went quiet (other workers active) -- skipped, "
                  "pstate=%s sm=%s util=%s" % (want, g["pstate"], g["sm_mhz"], g["util"]),
                  flush=True)
            continue
        pre = gpu()
        cold = fire()
        warm = fire()
        post = gpu()
        rows.append({"idle_s": want, "pre": pre, "post": post,
                     "cold": cold, "warm": warm})
        print("  idle %2ds: pre=%s/%sMHz/%.1fW | COLD prefill %.0f decode %.0f (%d tok) "
              "| WARM prefill %.0f decode %.0f (%d tok) | delta %.0f ms"
              % (want, pre["pstate"], pre["sm_mhz"], pre["watt"],
                 cold["prefill_ms"], cold["decode_ms"], cold["out_tokens"],
                 warm["prefill_ms"], warm["decode_ms"], warm["out_tokens"],
                 (cold["prefill_ms"] + cold["decode_ms"])
                 - (warm["prefill_ms"] + warm["decode_ms"])), flush=True)

    if rows:
        # Normalise by token count: cold and warm can generate different lengths,
        # and comparing raw totals would then measure the sampler, not the clock.
        print("\n===== SUMMARY =====")
        for r in rows:
            c, w = r["cold"], r["warm"]
            cps = c["decode_ms"] / max(c["out_tokens"], 1)
            wps = w["decode_ms"] / max(w["out_tokens"], 1)
            print("  idle %2ds: cold %.2f ms/tok vs warm %.2f ms/tok  (%+.1f%%), "
                  "cold prefill %.0f vs warm %.0f ms"
                  % (r["idle_s"], cps, wps, 100 * (cps - wps) / wps,
                     c["prefill_ms"], w["prefill_ms"]))
        cd = [r["cold"]["decode_ms"] / max(r["cold"]["out_tokens"], 1) for r in rows]
        wd = [r["warm"]["decode_ms"] / max(r["warm"]["out_tokens"], 1) for r in rows]
        print("  median cold %.2f ms/tok, median warm %.2f ms/tok"
              % (statistics.median(cd), statistics.median(wd)))
    with open(r"C:\ServerData\Stacks\jarvis\perf\results-gpu-coldwarm.json", "w") as fh:
        json.dump(rows, fh, indent=2)


if __name__ == "__main__":
    main()
