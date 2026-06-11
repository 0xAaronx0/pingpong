#!/bin/bash
# pingpong match check — run every ~5 minutes by a scheduler (LLM-free).
# Prints poll results verbatim; swallows the [SILENT] marker so no-news runs
# produce no output (and therefore no notification).
out=$(python3 "$(dirname "$0")/poll.py" 2>&1)
[ "$out" = "[SILENT]" ] || printf '%s\n' "$out"
