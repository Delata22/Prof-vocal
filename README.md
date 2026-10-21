# Prof vocal

Un prof oral qu'on peut interrompre, avec un tableau (le cahier) sur le 2e écran.

```
micro (page, VAD Silero) ──segment──▶ Whisper large-v3-turbo (GPU)
        ▲                                   │ texte
        │ audio phrase par phrase           ▼
   page (Opera) ◀── Piper (fr siwis) ◀── Claude Code headless (persistant, stream-json)
        │                                   │ Write cards/… + Bash cahier.py
        └── iframe ◀── cahier :8777 ◀───────┘
```

## Lancer

```bash
./prof.sh sift "SIFT (Scale-Invariant Feature Transform)"
```

Crée (ou reprend) le cahier `sift`, charge Whisper + Piper, lance Claude, ouvre http://localhost:8778/ dans Opera.
Cliquer « Démarrer le cours » (autorise micro et son). Parler ; couper le prof en parlant par-dessus.
Relancer le même slug reprend la même conversation Claude (`sessions/<slug>/prof.json`).

Variables : `PROF_MODEL` (défaut `claude-opus-5-5`), `PROF_EFFORT` (défaut `low`), `PROF_PORT` (8778), `PROF_NO_OPEN=1`.

## Tester sans micro

- Dans la page : bouton « Question test (wav) », ou taper une question + Entrée (interrompt aussi).
- Sans page : `PROF_NO_OPEN=1 ./prof.sh test "SIFT" &` puis `.venv/bin/python tests/test_client.py`
  (3 questions parlées par Piper, dont une interruption ; affiche les latences).
- `tests/make_wavs.py` régénère les wav de test.

## Fichiers

- `prof_server.py` : serveur aiohttp (page, WebSocket), Whisper, Piper, pilote de Claude.
- `prof.html` : page (tableau en iframe, barre d'état, transcription, VAD, lecture audio, barge-in).
- `prompt.md` : consignes du prof (ajoutées au system prompt de Claude Code).
- `latency.log` : latence mesurée par la page (fin de parole → 1er son).

## Détails

- Claude tourne en un seul processus `claude -p --input-format stream-json --output-format stream-json`
  (pas de démarrage à chaque tour) ; l'interruption envoie un `control_request` `interrupt`.
  `--setting-sources ""` : pas de hooks utilisateur. Outils : `Bash(python3 ~/.claude/cahier/cahier.py:*)`
  et `Write` limité à `sessions/<slug>/cards/` (le heredoc avec du LaTeX est refusé par le filtre de permissions).
- Le cahier n'est pas modifié.
