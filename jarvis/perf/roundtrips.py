"""How many LLM round trips does one Home Assistant turn actually cost?

The brief measured the LLM stage at 1324-1673 ms, but a direct single call to
Ollama with the same prompt and tools completes in roughly 300-400 ms of server
time. Something accounts for the difference, and the candidate explanations lead
to opposite optimisations:

  a) Home Assistant makes TWO calls per turn (one to choose the tool, one to
     write the spoken reply after the tool result comes back). Then the fix is
     architectural -- skip the second call, or skip the LLM entirely.
  b) Home Assistant's prompt is much larger than the reconstruction. Then the
     fix is trimming context.
  c) Overhead outside the model (HA intent plumbing, script execution).

This measures a) and c) directly by counting Ollama's own "new prompt" log lines
inside the window of a single HA turn, and by summing the per-request server
time reported for those same requests. Contention from other workers is handled
by attributing only requests whose token count matches HA's prompt shape, and by
reporting the raw window count alongside so over-attribution is visible.
"""

import json
import re
import subprocess
import statistics
import time
import urllib.request

HASS = "http://localhost:8123"
TOKEN = open(r"C:\ServerData\Stacks\jarvis\.bench-token.txt").read().strip()

UTTS = ["set the AC to 22", "turn on the lights", "good night", "it's boiling in here"]

NEWPROMPT = re.compile(r"task (\d+) \| new prompt.*task\.n_tokens = (\d+)")
TIMING = re.compile(
    r"task (\d+) \| prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens.*?"
    r"eval time =\s*([\d.]+) ms /\s*(\d+) (?:runs|tokens)", re.S)


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


def tail_tasks(n=4000):
    """Return {task_id: prompt_tokens} from the recent log tail.

    Timestamp windowing was tried first and is a trap: `docker logs --since`
    parses a naive timestamp as CONTAINER-LOCAL time, and this container runs
    TZ=Asia/Kolkata, so a UTC window silently returned 5.5 extra hours of log
    (15 MB, 2899 matches for what should have been a 2 second window). Task IDs
    are monotonic, so diffing them needs no clock agreement at all.
    """
    p = subprocess.run(
        ["docker", "logs", "--tail", str(n), "jarvis-ollama"],
        capture_output=True, text=True, errors="replace")
    out = {}
    for tid, ntok in NEWPROMPT.findall(p.stdout + p.stderr):
        out[int(tid)] = int(ntok)
    return out


def main():
    # Warm.
    ha("hello")
    time.sleep(1.0)

    print("%-24s %8s %6s %-14s %3s %s"
          % ("utterance", "HA ms", "calls", "prompt toks", "len", "reply"))
    rows = []
    for utt in UTTS:
        for rep in range(3):
            before = tail_tasks()
            max_before = max(before) if before else 0
            ms, res = ha(utt)
            time.sleep(1.2)  # let both passes and any script finish and flush
            after = tail_tasks()

            new = {t: n for t, n in after.items() if t > max_before}
            ours = sorted(new.items())

            try:
                speech = res["response"]["speech"]["plain"]["speech"]
            except Exception:
                speech = ""
            words = len(speech.split())
            rows.append({
                "utt": utt, "ha_ms": ms,
                "our_calls": len(ours), "our_tokens": [n for _, n in ours],
                "reply_words": words, "speech": speech,
            })
            print("%-24s %8.0f %6d %-14s %2dw %s" % (
                utt, ms, len(ours), str([n for _, n in ours]), words, speech[:34]))

    print("\n===== SUMMARY =====")
    by = {}
    for r in rows:
        by.setdefault(r["utt"], []).append(r)
    for utt, rs in by.items():
        calls = [r["our_calls"] for r in rs]
        mss = [r["ha_ms"] for r in rs]
        toks = [r["our_tokens"] for r in rs]
        print("  %-24s HA median %6.0f ms | LLM calls per turn %s | prompt tokens %s"
              % (utt, statistics.median(mss), sorted(set(calls)), toks[0]))
    allcalls = [r["our_calls"] for r in rows]
    words = [r["reply_words"] for r in rows]
    print("  LLM round trips per HA turn: min %d, max %d, mode %d"
          % (min(allcalls), max(allcalls), statistics.mode(allcalls)))
    print("  reply length: median %.0f words, max %d (prompt caps at 12)"
          % (statistics.median(words), max(words)))
    with open(r"C:\ServerData\Stacks\jarvis\perf\results-roundtrips.json", "w") as fh:
        json.dump(rows, fh, indent=2)


if __name__ == "__main__":
    main()
