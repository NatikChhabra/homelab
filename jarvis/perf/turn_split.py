"""Split one Home Assistant turn into: pass-1 model, pass-2 model, HA overhead.

roundtrips.py established that HA makes TWO Ollama calls for a tool-using turn
(and one for a tool-only turn where no follow-up text is needed). This attributes
the wall-clock of a turn to each pass using llama-server's own `total time` line,
so the residual is Home Assistant's own cost: intent plumbing, script execution
and HTTP.

Task IDs are monotonic, so a turn's requests are identified by "task id greater
than the maximum seen before the turn started". No clock agreement is needed,
which matters because this container runs TZ=Asia/Kolkata and `docker logs
--since` reads naive timestamps as container-local time.
"""

import json
import re
import statistics
import subprocess
import time
import urllib.request

HASS = "http://localhost:8123"
TOKEN = open(r"C:\ServerData\Stacks\jarvis\.bench-token.txt").read().strip()

NEWPROMPT = re.compile(r"task (\d+) \| new prompt.*?task\.n_tokens = (\d+)")
TOTAL = re.compile(r"task (\d+) \|\s+total time =\s+([\d.]+) ms")
PEVAL = re.compile(r"task (\d+) \| prompt eval time =\s+([\d.]+) ms /\s+(\d+) tokens")
EVAL = re.compile(r"task (\d+) \|\s+eval time =\s+([\d.]+) ms /\s+(\d+) ")

UTTS = [
    ("set the AC to 22", "script"),
    ("turn on the lights", "device"),
    ("good night", "script"),
    ("it's boiling in here", "comfort"),
    ("turn off the fan", "device"),
]


def ha(text, agent="conversation.jarvis"):
    req = urllib.request.Request(
        HASS + "/api/conversation/process",
        data=json.dumps({"text": text, "language": "en", "agent_id": agent}).encode(),
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
        method="POST")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=180) as r:
        res = json.load(r)
    return (time.perf_counter() - t0) * 1000, res


def scrape(n=4000):
    p = subprocess.run(["docker", "logs", "--tail", str(n), "jarvis-ollama"],
                       capture_output=True, text=True, errors="replace")
    txt = p.stdout + p.stderr
    prompts = {int(t): int(k) for t, k in NEWPROMPT.findall(txt)}
    totals = {int(t): float(v) for t, v in TOTAL.findall(txt)}
    peval = {int(t): (float(v), int(k)) for t, v, k in PEVAL.findall(txt)}
    ev = {int(t): (float(v), int(k)) for t, v, k in EVAL.findall(txt)}
    return prompts, totals, peval, ev


def main():
    ha("hello")
    time.sleep(1.0)
    rows = []
    print("%-22s %8s %6s %10s %10s %s"
          % ("utterance", "HA ms", "calls", "model ms", "HA ovh ms", "detail"))
    for utt, cat in UTTS:
        for _ in range(3):
            p0, _, _, _ = scrape()
            hi = max(p0) if p0 else 0
            ms, res = ha(utt)
            time.sleep(1.3)
            prompts, totals, peval, ev = scrape()
            tasks = sorted(t for t in prompts if t > hi)
            model_ms = sum(totals.get(t, 0.0) for t in tasks)
            detail = []
            for t in tasks:
                pv, pk = peval.get(t, (0, 0))
                evv, evk = ev.get(t, (0, 0))
                detail.append("p%d:%dtok/%.0fms+gen%dtok/%.0fms"
                              % (prompts[t], pk, pv, evk, evv))
            try:
                speech = res["response"]["speech"]["plain"]["speech"]
            except Exception:
                speech = ""
            rows.append({"utt": utt, "cat": cat, "ha_ms": ms, "calls": len(tasks),
                         "model_ms": model_ms, "overhead_ms": ms - model_ms,
                         "prompt_tokens": [prompts[t] for t in tasks],
                         "gen_tokens": [ev.get(t, (0, 0))[1] for t in tasks],
                         "speech": speech})
            print("%-22s %8.0f %6d %10.0f %10.0f %s"
                  % (utt, ms, len(tasks), model_ms, ms - model_ms, "; ".join(detail)))

    print("\n===== SUMMARY =====")
    by = {}
    for r in rows:
        by.setdefault(r["utt"], []).append(r)
    for utt, rs in by.items():
        print("  %-22s HA %6.0f ms = model %6.0f + overhead %6.0f | calls %s | gen tokens %s"
              % (utt,
                 statistics.median([r["ha_ms"] for r in rs]),
                 statistics.median([r["model_ms"] for r in rs]),
                 statistics.median([r["overhead_ms"] for r in rs]),
                 sorted({r["calls"] for r in rs}),
                 rs[0]["gen_tokens"]))
    tot = statistics.median([r["ha_ms"] for r in rows])
    mod = statistics.median([r["model_ms"] for r in rows])
    print("  overall: HA %.0f ms, model %.0f ms (%.0f%%), HA overhead %.0f ms (%.0f%%)"
          % (tot, mod, 100 * mod / tot, tot - mod, 100 * (tot - mod) / tot))
    with open(r"C:\ServerData\Stacks\jarvis\perf\results-turn-split.json", "w") as fh:
        json.dump(rows, fh, indent=2)


if __name__ == "__main__":
    main()
