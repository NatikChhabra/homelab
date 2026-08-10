import sys
sys.path.insert(0, '/app')
from hermes.executor import _describe

tests = [
    ("target_awake", {"state": "awake", "awake": True}),
    ("target_asleep", {"state": "asleep_or_hibernated", "awake": False}),
    ("target_off", {"state": "off", "awake": False}),
    ("target_full", {"ip": "192.168.29.167", "awake": True, "state": "awake",
        "confidence": "high", "wakeable": False, "rtt_ms": 1, "detected_via": "icmp",
        "arp_mac": "D8-43-AE-17-63-25"}),
    ("desktop_active", {
        "hostname": "ARJUN_RAJPUT",
        "logged_in_user": "arjun_rajput\\Naiti",
        "session_id": 1,
        "interactive": True,
        "uptime_seconds": 39761,
        "uptime_human": "0d 11h 2m",
        "active_window": {"title": "Arc", "process": "Arc", "pid": 23232},
        "volume_percent": 100,
        "muted": False,
        "battery": {"present": True, "percent": 100, "charging": True, "status": "High"},
    }),
]

for name, payload in tests:
    speech = _describe("pc_status", payload)
    print(f"{name:20} -> {speech!r}")
