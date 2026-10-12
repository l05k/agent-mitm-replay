#!/bin/bash
# Verifier for the smoke task: /app/result.txt must hold the scripted artifact.
set -u

file=/app/result.txt
mkdir -p /logs/verifier

if [ -f "$file" ] && [ "$(cat "$file")" = "harbor-replay-smoke" ] \
 && [ "$(wc -c < "$file" | tr -d ' ')" = "20" ]; then
    echo "pass: $file holds the 20 byte smoke line"
    echo 1 > /logs/verifier/reward.txt
else
    echo "fail: expected $file to hold exactly 'harbor-replay-smoke' (20 bytes)"
    ls -l "$file" || true
    echo 0 > /logs/verifier/reward.txt
fi
