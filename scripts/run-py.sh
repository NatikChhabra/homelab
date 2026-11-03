#!/bin/bash
# Runs a Stacks python script with secrets.env loaded (for scheduled tasks). 2026-11-01.
set -a; . /c/ServerData/Stacks/secrets.env; set +a
export PYTHONIOENCODING=utf-8
log=/c/ServerData/Stacks/logs/$(basename "$1" .py).log
# 2026-11-03: tee, not >>, so run-job.ps1 also sees the output and can put the script's
# SUMMARY line in the Kuma heartbeat (it only ever said "exit 0" before).
"/c/Users/Naiti/AppData/Local/Programs/Python/Python311/python.exe" "$(cygpath -w "$1")" 2>&1 | tee -a "$log"
rc=${PIPESTATUS[0]}   # the python exit code, not tee's
echo "$(date -u +%FT%TZ) exit $rc" >> "$log"
exit $rc
