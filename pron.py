#!/usr/bin/env python3
"""Lexique de prononciation de Piper (~/.claude/cahier/prononciation.json, partagé avec le cahier).
  pron.py add "<mot écrit>" "<orthographe phonétique>"
  pron.py rm "<mot écrit>"
  pron.py list"""
import json, os, sys
from pathlib import Path
F = Path(os.environ.get("CAHIER_HOME", Path.home() / ".claude/cahier")) / "prononciation.json"
lex = json.loads(F.read_text()) if F.exists() else {}
cmd, args = (sys.argv[1] if len(sys.argv) > 1 else "list"), sys.argv[2:]
if cmd == "add" and len(args) == 2:
    lex[args[0]] = args[1]
elif cmd == "rm" and len(args) == 1:
    lex.pop(args[0], None)
elif cmd != "list":
    sys.exit(__doc__)
if cmd != "list":
    tmp = F.with_suffix(".tmp")
    tmp.write_text(json.dumps(lex, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, F)
for k, v in lex.items():
    if not k.startswith("_"):
        print(f"{k} -> {v}")
