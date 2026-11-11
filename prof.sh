#!/usr/bin/env bash
# Lance le Prof vocal : ./prof.sh <slug> "<sujet>"   (variables : PROF_MODEL, PROF_EFFORT, PROF_PORT, PROF_NO_OPEN)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
SLUG="${1:?usage: ./prof.sh <slug> \"<sujet>\"}"
TOPIC="${2:-$SLUG}"
PORT="${PROF_PORT:-8778}"
URL="http://localhost:$PORT/"

python3 "$(dirname "$(readlink -f "$0")")/cahier/cahier.py" new "$SLUG" "$TOPIC"     # crée le cahier (ou le réutilise) et lance son serveur
if curl -s -o /dev/null "$URL"; then echo "un Prof tourne déjà sur $URL"; exit 1; fi

if [ -z "${PROF_NO_OPEN:-}" ]; then
  ( for _ in $(seq 60); do curl -s -o /dev/null "$URL" && break; sleep 0.5; done
    if command -v opera >/dev/null; then opera --new-window "$URL" >/dev/null 2>&1; else xdg-open "$URL"; fi ) &
fi
exec .venv/bin/python prof_server.py "$SLUG" "$TOPIC" --port "$PORT"
