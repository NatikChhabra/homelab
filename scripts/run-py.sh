#!/bin/bash
# Runs a Stacks python script with secrets.env loaded (for scheduled tasks). 2026-11-01.
set -a; . /c/ServerData/Stacks/secrets.env; set +a
export PYTHONIOENCODING=utf-8
log=/c/ServerData/Stacks/logs/$(basename "$1" .py).log
"/c/Users/Naiti/AppData/Local/Programs/Python/Python311/python.exe" "$(cygpath -w "$1")" >> "$log" 2>&1
rc=$?   # capture before anything else overwrites $?
echo "$(date -u +%FT%TZ) exit $rc" >> "$log"
exit $rc
