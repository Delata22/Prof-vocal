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

## Whiteboard (l'application)

```bash
/usr/bin/python3 install_desktop.py     # entrée « Whiteboard » dans le menu des applications, commande `whiteboard`
```

Fenêtre native GTK + WebKit (WM_CLASS « Whiteboard », processus « whiteboard »), sans navigateur.
L'accueil liste les boards (les cahiers de `~/.claude/cahier`) : « Reprendre », ou « Nouveau board » (sujet → slug).
L'app démarre le Prof du board choisi (`prof_server.py` sur le port 8790, un seul à la fois) ; « ⌂ Boards » revient
à l'accueil. Pendant que le prof parle ou que tu enregistres, le Prof écrit
`~/.local/state/regulator/activity/whiteboard.json` pour Regulator (app de travail « whiteboard »).
Casque Bluetooth : ⚙ Réglages → « mode appel pendant la séance » appelle `casque-mode appel` avant d'ouvrir le
micro et `casque-mode musique` une fois à la fin (seulement si c'est le Prof qui l'avait basculé).

## Mesurer la reconnaissance vocale

⚙ Réglages → « 🎙 Calibrer le micro » : lire 12 phrases (cours + calcul), avec puis sans les filtres du navigateur.
Les enregistrements bruts vont dans `calib/<date>_<micro>/`. Ensuite :

```bash
.venv/bin/python bench_asr.py              # WER par réglage -> calib/resultats.md
.venv/bin/python bench_asr.py --voxtral    # + Voxtral Mini 3B (~9 Go de VRAM)
```

Le réglage retenu se choisit par variables : `PROF_WHISPER` (large-v3-turbo), `PROF_WHISPER_COMPUTE`
(int8_float16), `PROF_WHISPER_BEAM` (5).

## Installer

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv/bin/python -r requirements.txt
```

## Lancer

```bash
./prof.sh sift "SIFT (Scale-Invariant Feature Transform)"
```

(pour le débogage ; au quotidien, l'app Whiteboard)

Crée (ou reprend) le cahier `sift`, charge Whisper + Piper, lance Claude, ouvre http://localhost:8778/ dans Opera.
Cliquer « Démarrer le cours » (autorise micro et son). Parler ; couper le prof en parlant par-dessus.
Relancer le même slug reprend la même conversation Claude (`sessions/<slug>/prof.json`).

Variables : `PROF_MODEL` (défaut `claude-opus-5-5`), `PROF_EFFORT` (défaut `low`), `PROF_PORT` (8778), `PROF_NO_OPEN=1`.

## Scènes animées (façon 3Blue1Brown)

Le prof dessine avec `scene/scene.js`, un moteur de scènes dont l'API copie manim (réseau, plan déformable, matrice, courbes, tableaux de valeurs ; `TransformFromCopy`, `Flow`, `ApplyMatrix`…). Référence : [scene/API.md](scene/API.md).
`python3 scene.py add <slug> <id> "Titre" --file cards/<id>.js` assemble la carte (HTML autonome, KaTeX depuis le CDN), la dessine hors écran (WebKit) étape par étape, refuse les chevauchements, traits sur un texte, débordements et erreurs JS, et écrit l'aperçu `cards/<id>.png` que le prof regarde. `check` : sans ajouter.

## Tester sans micro

- Micro : **espace** = 1er appui j'enregistre (et coupe le prof), 2e appui j'envoie ; Échap annule. Bouton « Mode » pour le mode auto (VAD, fin de phrase détectée seule, envoi forcé à 20 s ou avec « Envoyer »). Traces dans la console (`[prof …]`) et « VAD au repos » dans la barre.
- Page muette pour les tests : http://localhost:8778/?mute
- Voix du prof : **P** pause / reprise (exactement où il en était, sans l'interrompre), **←** reprend la phrase en cours (2 appuis rapides : la précédente), clic sur une phrase de la transcription : réécoute à partir d'elle.
- Explication en parties : avant chaque phrase qui change l'affichage (encadré, étape, autre carte), la voix attend. Deux réglages sous l'état : **Encadrés** (même carte, auto par défaut) et **Cartes** (passage à une autre carte, manuel par défaut). Auto : la suite après 5 s, **→** fige puis reprend ; manuel : **→** ou « Suite » pour continuer.
- ⚙ Réglages : choix du micro (et filtres écho/bruit/gain du navigateur), voix Piper (siwis, tom, upmc jessica/pierre, gilles, mls), vitesse, « Écouter un exemple ». Réglage de voix gardé dans `voice.json`.
- Capture du mode espace au taux natif du micro (AudioWorklet), rééchantillonnée en 16 kHz côté serveur (soxr) ; le brut est gardé dans `sessions/<slug>/audio/*.raw<taux>.wav`.
- Dans la page : bouton « Question test (wav) », ou taper une question + Entrée (interrompt aussi).
- Sans page : `PROF_NO_OPEN=1 ./prof.sh test "SIFT" &` puis `.venv/bin/python tests/test_client.py`
  (3 questions parlées par Piper, dont une interruption ; affiche les latences).
- `tests/make_wavs.py` régénère les wav de test.

## Fichiers

- `prof_server.py` : serveur aiohttp (page, WebSocket), Whisper, Piper, pilote de Claude.
- `prof.html` : page (tableau en iframe, barre d'état, transcription, VAD, lecture audio, barge-in).
- `prompt.md` : consignes du prof (ajoutées au system prompt de Claude Code).
- `latency.log` : latence mesurée par la page (fin de parole → 1er son).
- `vocab.txt` (+ `sessions/<slug>/vocab.txt`) : vocabulaire donné à Whisper (initial_prompt + hotwords), relu à chaud.
- `pron.py` : lexique de prononciation de Piper (`~/.claude/cahier/prononciation.json`, partagé avec le cahier), relu à chaud.
- `sessions/<slug>/audio/` : chaque segment wav + `transcriptions.jsonl` (corpus pour un futur fine-tuning de Whisper).

## Détails

- Claude tourne en un seul processus `claude -p --input-format stream-json --output-format stream-json`
  (pas de démarrage à chaque tour) ; l'interruption envoie un `control_request` `interrupt`.
  `--setting-sources ""` : pas de hooks utilisateur. Outils : `Bash(python3 ~/.claude/cahier/cahier.py:*)`
  et `Write` limité à `sessions/<slug>/cards/` (le heredoc avec du LaTeX est refusé par le filtre de permissions).
- Le cahier n'est pas modifié.
