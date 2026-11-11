#!/usr/bin/env python3
"""Prof vocal : micro (VAD navigateur) -> Whisper (GPU) -> Claude Code headless -> Piper -> navigateur.

Une page sur http://localhost:8778/ : le cahier en iframe (tableau) + barre d'état.
WebSocket /ws :
  client -> serveur : binaire = segment de parole Float32 16 kHz ;
                      JSON {t: start | barge | text | latency | misfire}
  serveur -> client : JSON {t: state | user | gen | say | tool | done | error | info}
"""
import argparse, asyncio, base64, json, os, re, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NV = HERE / ".venv/lib/python3.12/site-packages/nvidia"
# ctranslate2 charge cuBLAS / cuDNN au démarrage : il faut LD_LIBRARY_PATH avant le lancement du processus.
if os.environ.get("PROF_REEXEC") != "1":
    os.environ["PROF_REEXEC"] = "1"
    libs = [str(NV / "cublas/lib"), str(NV / "cudnn/lib")]
    os.environ["LD_LIBRARY_PATH"] = ":".join(libs + [os.environ.get("LD_LIBRARY_PATH", "")])
    os.execv(sys.executable, [sys.executable, *sys.argv])

import numpy as np
import soxr
from aiohttp import web, WSMsgType

CAHIER = Path(__file__).resolve().parent / "cahier" / "cahier.py"   # le tableau (données : ~/.claude/cahier)
DATA = Path(os.environ.get("CAHIER_HOME", Path.home() / ".claude/cahier"))   # données du tableau : séances, voix, lexique
VOICE = DATA / "voices/fr_FR-siwis-medium.onnx"
SESS = HERE / "sessions"
LOG = HERE / "latency.log"
CASQUE = Path.home() / ".local/bin/casque-mode"        # bascule A2DP / HFP du casque Bluetooth (géré ailleurs)
CASQUE_MARK = Path.home() / ".local/state/whiteboard/casque-appel-par-prof"   # c'est nous qui l'avons mis en appel
LEXIQUE = DATA / "prononciation.json"   # partagé avec le cahier (cahier.py prononce)


class Watched:
    """Fichier relu seulement quand il change (l'utilisateur ou le prof peut l'éditer en cours de séance)."""
    def __init__(self, path, parse):
        self.path, self.parse, self.mtime, self.value = Path(path), parse, None, parse(None)

    def get(self):
        try:
            m = self.path.stat().st_mtime
        except FileNotFoundError:
            m = None
        if m != self.mtime:
            self.mtime = m
            try:
                self.value = self.parse(self.path.read_text() if m else None)
            except Exception as e:
                log(f"{self.path.name} illisible : {e}")
        return self.value


def parse_lexique(txt):
    lex = json.loads(txt) if txt else {}
    pats = [(re.compile(rf"\b{re.escape(w)}\b", re.I), say)
            for w, say in sorted(lex.items(), key=lambda kv: -len(kv[0])) if not w.startswith("_")]
    return pats


def parse_vocab(txt):
    """Un terme par ligne (ou séparés par des virgules) ; # = commentaire."""
    out = []
    for line in (txt or "").splitlines():
        line = line.split("#")[0]
        out += [w.strip() for w in line.split(",") if w.strip()]
    return out


def log(*a):
    t = time.time()
    print(time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}", *a, flush=True)


# ---------------- oreilles : Whisper ----------------

class Ears:
    HALLU = re.compile(r"sous-titr|merci d'avoir regard|abonnez-vous|amara\.org", re.I)

    def __init__(self, model, compute, topic, slug, beam=5):
        from faster_whisper import WhisperModel
        t = time.time()
        self.beam = beam
        device = os.environ.get("PROF_WHISPER_DEVICE", "cuda")
        try:
            self.m = WhisperModel(model, device=device, compute_type=compute)
        except Exception as e:                # pas de carte NVIDIA (ou CUDA absent) : processeur, plus lent
            if device == "cpu":
                raise
            log(f"whisper : {device} indisponible ({str(e).splitlines()[0][:120]}) ; repli sur le processeur (int8)")
            self.m = WhisperModel(model, device="cpu", compute_type="int8")
        self.topic = topic
        # vocabulaire : commun (vocab.txt du projet) + propre au cours (sessions/<slug>/vocab.txt)
        self.vocabs = [Watched(HERE / "vocab.txt", parse_vocab), Watched(SESS / slug / "vocab.txt", parse_vocab)]
        self.transcribe(np.zeros(16000, np.float32))  # chauffe
        log(f"whisper {model} {compute} prêt ({time.time() - t:.1f} s) ; vocabulaire : {len(self.words())} termes")

    def words(self):
        seen, out = set(), []
        for v in self.vocabs:
            for w in v.get():
                if w.lower() not in seen:
                    seen.add(w.lower()); out.append(w)
        return out

    def prompt_for(self, maths=False):
        words = self.words()
        # initial_prompt : le contexte (Whisper imite le style et l'orthographe du prompt) ;
        # hotwords : les termes techniques à favoriser.
        # Le prompt est une phrase naturelle : une liste « Vocabulaire : a, b, c » fait continuer la liste
        # à Whisper sur un énoncé court (« Allo » -> « SIFT, DOG, ... laulich, laulich »). Les termes
        # techniques sont dans le prompt (hotwords faisait sortir de l'islandais sur un test).
        termes = ", de ".join(words[:12])
        prompt = f"Bonjour. Aujourd'hui, en cours, on parle de {self.topic}" + (f", de {termes}." if termes else ".")
        if maths:   # dictée d'une ligne de calcul : un prompt de calcul, sinon Whisper recopie la phrase de cours
            prompt = ("Soit n un entier. Soit x un réel. Pour tout i, f de x égale n plus 1. "
                      "D de 4 égale un seizième fois moins deux fois neuf, égale moins 1.12. Sigma 2 égale racine de 2 fois sigma 1.")
        return prompt

    def transcribe(self, audio, maths=False, use_prompt=True, beam=None):
        """use_prompt / beam : variantes pour le banc d'essai (bench_asr.py)."""
        prompt = self.prompt_for(maths) if use_prompt else None
        segs, _ = self.m.transcribe(audio, language="fr", beam_size=beam or self.beam, vad_filter=False,
                                    condition_on_previous_text=False, initial_prompt=prompt)
        segs = [s for s in segs if s.no_speech_prob < 0.8 and s.compression_ratio < 2.4 and s.avg_logprob > -1.0]
        txt = " ".join(s.text.strip() for s in segs).strip()
        # Whisper recopie le prompt sur un son trop court ; mais une vraie phrase peut ressembler à un bout
        # du prompt (« soit n un entier ») : on ne rejette qu'une copie d'au moins la moitié du prompt.
        t = txt.lower().strip(" .")
        copie = bool(prompt) and t in prompt.lower() and len(t) > 0.5 * len(prompt)
        return "" if self.HALLU.search(txt) or self.garbage(txt) or copie else txt

    @staticmethod
    def garbage(txt):
        """Hallucination typique : autre alphabet que le latin, ou un mot répété en boucle."""
        if re.search(r"[^\x00-\u024F\u0370-\u03FF\u2000-\u206F\u20AC]", txt):
            return True
        ws = re.findall(r"\w+", txt.lower())
        # boucle seulement : un mot répété au moins 8 fois et > 50 % du texte. Une dictée de calcul répète
        # légitimement « plus », « zéro »… (« 1 plus 2 plus 3 » était rejeté avec l'ancien seuil de 30 %).
        top = max((ws.count(w) for w in set(ws)), default=0)
        return top >= 8 and top / len(ws) > 0.5


# ---------------- voix : Piper ----------------

class Voice:
    """Voix Piper au choix (fichiers de ~/.claude/cahier/voices), réglage gardé dans voice.json."""
    DIR = VOICE.parent
    CONF = HERE / "voice.json"
    SAMPLE = "Bonjour ! Prenons un pixel et ses quatre voisins : le gradient nous dit dans quelle direction l'image change le plus vite."
    MAX_SPEAKERS = 12          # mls a 125 locuteurs : on n'en propose que quelques-uns

    def __init__(self):
        from piper import SynthesisConfig
        self.SynthesisConfig = SynthesisConfig
        self.loaded = {}
        self.lex = Watched(LEXIQUE, parse_lexique)
        conf = json.loads(self.CONF.read_text()) if self.CONF.exists() else {}
        self.set(conf.get("voice", "fr_FR-siwis-medium"), conf.get("speaker"), conf.get("speed", 1.0), save=False)
        self.pcm("Bonjour.")
        log(f"piper prêt : {self.name} {self.speaker or ''} vitesse {self.speed}")

    def catalog(self):
        out = []
        for j in sorted(self.DIR.glob("*.onnx.json")):
            name = j.name[:-len(".onnx.json")]
            spk = list(json.loads(j.read_text()).get("speaker_id_map", {}))[:self.MAX_SPEAKERS]
            short = name.replace("fr_FR-", "")
            out += [{"voice": name, "speaker": sp, "label": f"{short} · {sp}"} for sp in spk] if spk else \
                   [{"voice": name, "speaker": None, "label": short}]
        return out

    def _load(self, name):
        if name not in self.loaded:
            from piper import PiperVoice
            self.loaded[name] = PiperVoice.load(str(self.DIR / f"{name}.onnx"))
        return self.loaded[name]

    def set(self, name, speaker=None, speed=1.0, save=True):
        if not (self.DIR / f"{name}.onnx").exists():
            name, speaker = "fr_FR-siwis-medium", None
        v = self._load(name)
        sid = v.config.speaker_id_map.get(speaker) if speaker is not None and v.config.speaker_id_map else None
        self.name, self.v, self.speaker = name, v, speaker if sid is not None else None
        self.speed = max(0.6, min(1.6, float(speed or 1.0)))
        self.cfg = self.SynthesisConfig(speaker_id=sid, length_scale=1 / self.speed)
        self.sr = v.config.sample_rate
        if save:
            self.CONF.write_text(json.dumps({"voice": name, "speaker": self.speaker, "speed": self.speed}))

    def current(self):
        return {"voice": self.name, "speaker": self.speaker, "speed": self.speed}

    def prononce(self, text):
        for pat, say in self.lex.get():
            text = pat.sub(say, text)
        return text

    def pcm(self, text):
        """Renvoie (pcm int16, taux) : le taux dépend de la voix (16, 22,05 ou 44,1 kHz)."""
        v, cfg, sr = self.v, self.cfg, self.sr          # instantané : la voix peut changer pendant la synthèse
        text = self.prononce(text)
        parts = [c.audio_int16_bytes for c in v.synthesize(text, cfg)]
        return b"".join(parts) + b"\0\0" * int(sr * 0.18), sr   # petit silence de fin de phrase


SPEAKABLE = [
    (re.compile(r"```.*?```", re.S), " "),
    (re.compile(r"\$\$.*?\$\$", re.S), " "),
    (re.compile(r"\$[^$]*\$"), " "),
    (re.compile(r"`[^`]*`"), " "),
    (re.compile(r"https?://\S+"), " "),
    (re.compile(r"\[\[[^\]]*\]\]"), " "),
    (re.compile(r"⟦[^⟧]*⟧?"), " "),                   # repère inline (ou coupé en fin de tour)
    (re.compile(r"[*_#>|`~]+"), " "),
    (re.compile(r"^\s*[-•]\s+", re.M), ""),
    (re.compile(r"\s+"), " "),
]


def speakable(t):
    for r, s in SPEAKABLE:
        t = r.sub(s, t)
    return t.strip()


SENT_END = re.compile(r"[.!?…]+[»\"')]*\s+|\n+")


CUE = re.compile(r"⟦([^⟧]*)⟧")
CUE_CMDS = {"mark", "focus", "duo", "show", "pin", "unpin", "step", "play", "replay", "layout"}


def take_cues(raw):
    """Repères inline ⟦mark 3.19 --ligne 2⟧ : renvoie (texte sans repères, [commandes])."""
    cues = [c.strip() for c in CUE.findall(raw) if c.strip()]
    return CUE.sub(" ", raw), cues


def cut_sentences(buf, flush=False):
    """Renvoie (phrases complètes, reste)."""
    out = []
    while True:
        m = SENT_END.search(buf)
        if not m:
            break
        # ne coupe pas un repère ⟦…⟧ en deux (il peut arriver en plusieurs morceaux pendant le streaming)
        if buf[:m.end()].count("⟦") > buf[:m.end()].count("⟧"):
            close = buf.find("⟧", m.end())
            if close < 0:
                break
            m2 = SENT_END.search(buf, close + 1)
            if not m2:
                break
            m = m2
        # ne coupe pas une formule $...$ en deux
        if buf[:m.end()].count("$") % 2:
            nxt = buf.find("$", m.end())
            if nxt < 0:
                break
            m2 = SENT_END.search(buf, nxt + 1)
            if not m2:
                break
            m = m2
        out.append(buf[:m.end()])
        buf = buf[m.end():]
    if len(buf) > 220:                       # phrase interminable : coupe à une virgule
        k = buf.rfind(", ", 0, 200)
        if k > 40:
            out.append(buf[:k + 1]); buf = buf[k + 2:]
    if flush and buf.strip():
        out.append(buf); buf = ""
    return out, buf


# ---------------- cerveau : Claude Code headless persistant ----------------

class Brain:
    def __init__(self, slug, topic, model, effort, on_event):
        self.slug, self.topic, self.model, self.effort = slug, topic, model, effort
        self.on_event = on_event
        self.dir = SESS / slug
        (self.dir / "cards").mkdir(parents=True, exist_ok=True)
        vocab = self.dir / "vocab.txt"
        if not vocab.exists():
            vocab.write_text(f"# Vocabulaire de la dictée pour « {topic} » (un terme par ligne), en plus de vocab.txt du projet.\n")
        self.meta_f = self.dir / "prof.json"
        self.meta = json.loads(self.meta_f.read_text()) if self.meta_f.exists() else {}
        self.proc = None
        self.pending = []            # gens en attente de leur "result", dans l'ordre
        self.req = 0

    @property
    def fresh(self):
        return not self.meta.get("session_id")

    # --- consignes mises à jour en cours de séance ---
    @staticmethod
    def prompt_sections(text):
        """{titre de section ## : empreinte} d'un prompt.md (gabarit brut)."""
        import hashlib
        out = {}
        for part in ("\n" + text).split("\n## ")[1:]:
            title, _, body = part.partition("\n")
            out[title.strip()] = hashlib.sha1(body.strip().encode()).hexdigest()[:12]
        return out

    def old_prompt(self):
        """Le prompt.md en vigueur au début de cette séance Claude (pour une séance d'avant ce suivi) : version git
        de prompt.md à la date du premier message de la séance."""
        import subprocess
        sid = self.meta.get("session_id")
        for f in Path.home().glob(f".claude/projects/*/{sid}.jsonl"):
            try:
                for line in f.open():
                    ts = json.loads(line).get("timestamp")
                    if ts:
                        h = subprocess.run(["git", "-C", str(HERE), "log", "-1", f"--before={ts}", "--format=%H", "--", "prompt.md"],
                                           capture_output=True, text=True).stdout.strip()
                        if h:
                            return subprocess.run(["git", "-C", str(HERE), "show", f"{h}:prompt.md"],
                                                  capture_output=True, text=True).stdout
                        return None
            except (OSError, ValueError):
                pass
        return None

    def check_prompt_update(self, raw):
        """À la reprise d'une séance : si des sections de prompt.md sont nouvelles ou ont changé, une note pour le
        prochain message (sinon le prof garde les habitudes de la conversation et ignore la nouveauté)."""
        new = self.prompt_sections(raw)
        self.new_sections, self.prompt_notice = new, ""
        if not self.meta.get("session_id"):
            self.meta["prompt_sections"] = new
            return
        old = self.meta.get("prompt_sections")
        if old is None:
            prev = self.old_prompt()
            old = self.prompt_sections(prev) if prev else new
        changed = [t for t, h in new.items() if old.get(t) != h]
        if changed:
            self.prompt_notice = ("[Note du système : tes consignes ont été mises à jour depuis le début de cette séance. "
                                  "Sections nouvelles ou modifiées : " + " ; ".join(f"« {t} »" for t in changed) +
                                  ". Relis-les dans tes consignes et applique-les dès maintenant : elles priment sur "
                                  "ce que tu faisais jusqu'ici dans cette conversation.]")
            log("consignes changées depuis le début de la séance :", ", ".join(changed))
        else:
            self.meta["prompt_sections"] = new

    def prompt_notice_sent(self):
        self.prompt_notice = ""
        self.meta["prompt_sections"] = self.new_sections
        self.meta_f.write_text(json.dumps(self.meta))

    async def start(self):
        self.check_prompt_update((HERE / "prompt.md").read_text())
        system = (HERE / "prompt.md").read_text().replace("{slug}", self.slug).replace("{topic}", self.topic) \
            .replace("{pron}", f"python3 {HERE / 'pron.py'}") \
            .replace("{scene_py}", str(HERE / "scene.py")).replace("{cahier_py}", str(CAHIER)).replace("{scene_api}", str(HERE / "scene" / "API.md"))
        # Un Claude Code complet (décision de l'élève, 2026-11-06 : « toutes les perms classiques d'un Claude normal ») :
        # tous les outils standard, tout le dossier personnel, sa config utilisateur (CLAUDE.md, mémoire, hooks).
        # Permissions « classiques » : mode auto (le classifieur laisse passer le sûr) ; ce qui reste à décider
        # arrive au serveur (--permission-prompt-tool stdio, control_request can_use_tool) et l'élève répond
        # dans la page (Autoriser / Refuser / Toujours pour cette séance). Jamais bypassPermissions.
        # Le cahier, la prononciation et ses cartes restent pré-autorisés (le tableau ne doit jamais attendre).
        allowed = [f"Bash(python3 {CAHIER}:*)", "Bash(python3 ~/.claude/cahier/cahier.py:*)", "Edit(./cards/**)",
                   "Read(./vocab.txt)", "Edit(./vocab.txt)", f"Bash(python3 {HERE / 'pron.py'}:*)",
                   f"Bash(python3 {HERE / 'scene.py'}:*)", "Read", "Glob", "Grep"]
        args = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages",
                "--setting-sources", "user",            # ses instructions globales, sa mémoire, ses hooks
                "--model", self.model, "--effort", self.effort,
                "--add-dir", str(Path.home()),
                "--allowedTools", *allowed,
                "--permission-mode", os.environ.get("PROF_PERMISSION_MODE", "auto"),
                "--permission-prompt-tool", "stdio",
                "--append-system-prompt", system]
        if self.meta.get("session_id"):
            args += ["--resume", self.meta["session_id"]]
        env = dict(os.environ)
        env.pop("LD_LIBRARY_PATH", None)
        env.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
        self.proc = await asyncio.create_subprocess_exec(
            *args, cwd=self.dir, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
        self.pending = []
        asyncio.create_task(self._read())
        asyncio.create_task(self._stderr())
        log(f"claude lancé ({self.model}, effort {self.effort})",
            "reprise " + self.meta["session_id"] if self.meta.get("session_id") else "nouvelle session")

    async def _send(self, obj):
        if not self.proc or self.proc.returncode is not None:
            await self.start()
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def ask(self, gen, text, images=()):
        """images : [(media_type, base64)] jointes au message (blocs image du message utilisateur)."""
        self.pending.append(gen)
        content = [{"type": "text", "text": text}] + [
            {"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}} for mt, b64 in images
        ] if images else text
        await self._send({"type": "user", "message": {"role": "user", "content": content}})

    async def answer_permission(self, request_id, allow, input_=None, permissions=None, message=""):
        """Réponse à une demande d'autorisation (control_request can_use_tool)."""
        resp = ({"behavior": "allow", "updatedInput": input_ or {}} if allow
                else {"behavior": "deny", "message": message or "Refusé par l'élève."})
        if allow and permissions:
            resp["updatedPermissions"] = permissions
        await self._send({"type": "control_response",
                          "response": {"subtype": "success", "request_id": request_id, "response": resp}})

    async def interrupt(self):
        if self.pending:
            self.req += 1
            await self._send({"type": "control_request", "request_id": f"int{self.req}",
                              "request": {"subtype": "interrupt"}})

    @property
    def busy(self):
        return bool(self.pending)

    async def _stderr(self):
        async for line in self.proc.stderr:
            s = line.decode(errors="replace").rstrip()
            if s:
                log("claude stderr:", s[:300])

    async def _read(self):
        proc = self.proc
        tool_input = {}
        async for line in proc.stdout:
            try:
                j = json.loads(line)
            except Exception:
                continue
            gen = self.pending[0] if self.pending else None
            ty = j.get("type")
            if ty == "system" and j.get("subtype") == "init":
                sid = j.get("session_id")
                if sid and sid != self.meta.get("session_id"):
                    self.meta["session_id"] = sid
                    self.meta_f.write_text(json.dumps(self.meta))
            elif ty == "stream_event":
                ev = j["event"]
                et = ev.get("type")
                if et == "content_block_start":
                    cb = ev.get("content_block", {})
                    if cb.get("type") == "tool_use":
                        tool_input[ev.get("index")] = ""
                        await self.on_event("tool_start", gen, cb.get("name"))
                elif et == "content_block_delta":
                    d = ev.get("delta", {})
                    if d.get("type") == "text_delta":
                        await self.on_event("text", gen, d["text"])
                    elif d.get("type") == "input_json_delta":
                        tool_input[ev.get("index")] = tool_input.get(ev.get("index"), "") + d.get("partial_json", "")
                elif et == "content_block_stop":
                    if ev.get("index") in tool_input:
                        raw = tool_input.pop(ev.get("index"))
                        try:
                            inp = json.loads(raw)
                            cmd = inp.get("command") or ("prépare " + Path(inp.get("file_path", "?")).name)
                        except Exception:
                            cmd = raw
                        await self.on_event("tool", gen, cmd)
                    else:
                        await self.on_event("block_end", gen, None)
            elif ty == "user":   # résultat d'outil
                for c in j.get("message", {}).get("content", []):
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        out = c.get("content")
                        out = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                        await self.on_event("tool_result", gen, out[:300])
            elif ty == "control_request" and j.get("request", {}).get("subtype") == "can_use_tool":
                await self.on_event("permission", gen, j)
            elif ty == "result":
                if self.pending:
                    self.pending.pop(0)
                await self.on_event("result", gen, j)
        log("claude s'est arrêté, code", await proc.wait())
        for g in self.pending:
            await self.on_event("result", g, {"subtype": "process_exit"})
        self.pending = []



# ---------------- chef d'orchestre ----------------

class Prof:
    def __init__(self, a):
        self.a = a
        self.ws = None
        self.gen = 0                 # génération courante (une par question)
        self.buf = ""
        self.idx = 0
        self.ttsq = asyncio.Queue()
        self.interrupted = None      # {"last": str} si l'élève vient de couper
        self.t = {}                  # chronos de la génération courante
        self.history = []            # [(qui, texte)]
        self.ears = Ears(a.whisper, a.compute, a.topic, a.slug, a.beam)
        self.voice = Voice()
        self.brain = Brain(a.slug, a.topic, a.model, a.effort, self.on_brain)
        self.gen_text = ""
        self.cues = []
        self.outbox, self.gen_done = [], False   # phrases envoyées, pas encore jouées par la page
        self.perms = {}                           # demandes d'autorisation en attente : id -> (requête, message page)
        self.perm_always = set()                  # « toujours pour cette séance »
        self.silent = bool(self.brain.meta.get("silent"))
        self.pending_images = []        # [(chemin, media_type, base64)] à joindre au prochain message   # mode silencieux, mémorisé par board
        self.mode_notice = ""                  # repères en attente d'une phrase
        self.scene_errors = []                 # erreurs affichées dans une scène chez l'élève, pour le prochain message
        self.started = False

    async def send(self, obj):
        if self.ws is not None and not self.ws.closed:
            try:
                await self.ws.send_str(json.dumps(obj, ensure_ascii=False))
            except Exception:
                pass

    async def state(self, s, detail=""):
        await self.send({"t": "state", "s": s, "detail": detail})

    # --- nouvelle question ---
    async def question(self, text, t0=None, t_stt=None, prefix=""):
        await self.deny_pending_perms("L'élève est passé à autre chose (nouvelle question).")
        if self.brain.busy:
            await self.brain.interrupt()
        self.gen += 1
        self.buf, self.idx, self.gen_text, self.cues = "", 0, "", []
        self.outbox, self.gen_done = [], False
        self.t = {"t0": t0 or time.time(), "stt": t_stt or time.time()}
        await self.send({"t": "gen", "gen": self.gen})
        msg = prefix + text
        if self.brain.prompt_notice:    # consignes mises à jour depuis le début de la séance
            msg = self.brain.prompt_notice + "\n" + msg
            self.brain.prompt_notice_sent()
        if self.scene_errors:           # erreurs JS affichées dans une scène chez l'élève depuis le dernier message
            msg = ("[Erreur affichée dans une scène du tableau, chez l'élève (tu ne vois pas son écran) : "
                   + " ; ".join(self.scene_errors) + ". Corrige la scène.]\n" + msg)
            self.scene_errors = []
        if self.mode_notice:            # bascule voix / silencieux depuis le dernier message
            msg, self.mode_notice = self.mode_notice + "\n" + msg, ""
        if self.interrupted is not None:
            last = self.interrupted.get("last")
            if last:
                msg = (f"[Interruption : l'élève t'a coupé pendant que tu disais « {last} ». "
                       f"La suite de ta réponse n'a pas été entendue.]\n{msg}")
            else:
                msg = f"[Interruption : l'élève a repris la parole avant d'entendre ta réponse.]\n{msg}"
            self.interrupted = None
        images, self.pending_images = self.pending_images, []
        if images:          # photos collées / déposées : sauvées dans sessions/<slug>/images, jointes au message
            msg += "\n" + "\n".join(f"[photo jointe : {p}]" for p, _mt, _b in images)
        self.history.append(("élève", text))
        await self.state("thinking")
        await self.brain.ask(self.gen, msg, [(mt, b) for _p, mt, b in images])

    PATH_START = re.compile(r"(?:file://)?(?:~/|/)")

    async def attach_paths(self, text):
        """Chemins de fichiers dans un message de l'élève (file:///…, /home/…, ~/…, entre guillemets ou non,
        avec espaces) : chaque fichier qui existe est COPIÉ dans le board ; une image est jointe comme un collage,
        un autre fichier (PDF, texte…) est signalé au prof par sa copie, qu'il peut lire. Le chemin d'origine est
        remplacé dans le message transmis."""
        import shutil, urllib.parse
        out, k, found, pos = [], 0, 0, 0
        while True:
            m = self.PATH_START.search(text, pos)
            if not m:
                break
            pos = m.start() + 1
            if m.start() > 0 and not text[m.start() - 1].isspace() and text[m.start() - 1] not in "'\"«(:":
                continue                        # « a/b » dans une phrase : pas un chemin
            seg = text[m.start():].split("\n", 1)[0]
            # le plus long préfixe (jusqu'à un espace ou une ponctuation de fin) qui est un fichier existant
            cut = [i for i, ch in enumerate(seg) if ch in " \t\"'»)]>,;"] + [len(seg)]
            hit = None
            for c in sorted(set(cut), reverse=True):
                raw = seg[:c].rstrip(".:!?")
                p = urllib.parse.unquote(raw[7:]) if raw.startswith("file://") else raw
                try:
                    path = Path(p).expanduser().resolve()
                    if path.is_file() and path.is_relative_to(Path.home()):
                        hit = (c, raw, path)
                        break
                except (OSError, ValueError):
                    continue
            if not hit:
                continue
            c, raw, path = hit
            out.append(text[k:m.start()].rstrip("'\"« ") + " ")
            k = m.start() + len(raw)
            if k < len(text) and text[k] in "'\"»":
                k += 1
            pos = k
            found += 1
            try:
                if path.suffix.lower() in self.IMG_EXT:
                    name, data = await asyncio.get_running_loop().run_in_executor(None, self.read_dropped, str(path))
                    saved = await asyncio.get_running_loop().run_in_executor(None, self.save_image, data)
                    self.pending_images.append(saved)
                    self.pending_urls = getattr(self, "pending_urls", []) + ["/images/" + Path(saved[0]).name]
                    out.append(f"[photo jointe ci-dessous : {path.name}]")
                else:
                    if path.stat().st_size > 50 * 1024 * 1024:
                        raise ValueError(f"{path.name} : trop lourd (50 Mo max)")
                    d = SESS / self.a.slug / "files"
                    d.mkdir(parents=True, exist_ok=True)
                    dest = d / path.name
                    n = 2
                    while dest.exists() and dest.read_bytes() != path.read_bytes():
                        dest, n = d / f"{path.stem}-{n}{path.suffix}", n + 1
                    shutil.copy2(path, dest)
                    out.append(f"[fichier joint : {dest} (copie de {path.name}, tu peux le lire)]")
                    if path.suffix.lower() == ".pdf":       # et ses pages en images, comme des photos
                        pages, n = await asyncio.get_running_loop().run_in_executor(None, self.pdf_pages, dest)
                        for _name, data in pages:
                            saved = await asyncio.get_running_loop().run_in_executor(None, self.save_image, data)
                            self.pending_images.append(saved)
                            self.pending_urls = getattr(self, "pending_urls", []) + ["/images/" + Path(saved[0]).name]
                        out.append(f"[ses {len(pages)} première(s) page(s) sur {n} en images ci-dessous]")
                log(f"fichier du message joint : {path}")
            except Exception as e:
                log("fichier du message refusé :", e)
                out.append(f"[fichier {path.name} : non joint ({e})]")
                await self.send({"t": "error", "text": f"fichier non joint : {e}"})
        if not found:
            return text
        out.append(text[k:])
        return "".join(out).strip()

    def take_urls(self):
        urls, self.pending_urls = getattr(self, "pending_urls", []), []
        return urls

    IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
    PDF_MAX = 12          # pages d'un PDF envoyées en images au plus (le fichier entier reste lisible par le prof)

    def pdf_pages(self, path):
        """Un PDF → une image JPEG par page (1600 px de côté au plus, comme une photo) : [(nom, data URL)],
        et le nombre total de pages. pdftoppm (poppler)."""
        import subprocess, tempfile
        path = Path(path)
        try:
            info = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=20).stdout
            n = int(re.search(r"^Pages:\s+(\d+)", info, re.M).group(1))
        except Exception:
            raise ValueError(f"{path.name} : PDF illisible")
        last = min(n, self.PDF_MAX)
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run(["pdftoppm", "-jpeg", "-jpegopt", "quality=88", "-scale-to", "1600", "-f", "1",
                                "-l", str(last), str(path), f"{d}/p"], capture_output=True, text=True, timeout=120)
            files = sorted(Path(d).glob("p-*.jpg"), key=lambda f: int(f.stem.split("-")[-1]))
            if not files:
                raise ValueError(f"{path.name} : rendu du PDF impossible ({r.stderr.strip()[:120]})")
            out = [(f"{path.stem} p{i + 1}", "data:image/jpeg;base64," + base64.b64encode(f.read_bytes()).decode())
                   for i, f in enumerate(files)]
        log(f"PDF lu : {path} ({len(out)} page(s) sur {n} en images)")
        return out, n

    async def send_pdf_pages(self, path, label=None):
        """Pages d'un PDF renvoyées à la page comme des photos déposées (vignettes, jointes à la question suivante)."""
        pages, n = await asyncio.get_running_loop().run_in_executor(None, self.pdf_pages, path)
        for name, data in pages:
            await self.send({"t": "dropped", "name": name, "data": data})
        if n > len(pages):
            await self.send({"t": "error", "text": f"{label or Path(path).name} : {n} pages, seules les {len(pages)} premières sont jointes en images"})

    def read_dropped(self, uri):
        """Fichier glissé depuis le gestionnaire de fichiers (WebKitGTK ne donne que son chemin) : le serveur le lit
        lui-même — image seulement, 25 Mo max — et renvoie une data URL réduite,
        traitée ensuite exactement comme une photo collée. Le prof n'a jamais à lire un chemin."""
        import io, urllib.parse
        from PIL import Image, ImageOps
        p = urllib.parse.unquote(uri[7:] if uri.startswith("file://") else uri).strip()
        path = Path(p).expanduser().resolve()
        # pas de restriction de dossier (décision de l'élève : lecture partout) ; un dépôt passé par le portail
        # de fichiers arrive sous /run/user/<uid>/doc/…
        if path.suffix.lower() not in self.IMG_EXT:
            raise ValueError(f"{path.name} n'est pas une image")
        if not path.is_file():
            raise ValueError(f"{path.name} : fichier introuvable")
        if path.stat().st_size > 25 * 1024 * 1024:
            raise ValueError(f"{path.name} : trop lourde (25 Mo max)")
        if path.suffix.lower() in (".heic", ".heif"):
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError:
                raise ValueError(f"{path.name} : format HEIC non pris en charge (convertis-la en JPEG)")
        im = self._flatten(ImageOps.exif_transpose(Image.open(path)))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        log(f"image déposée lue : {path} ({im.size[0]}x{im.size[1]})")
        return path.name, "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    @staticmethod
    def _flatten(im):
        if im.mode in ("RGBA", "LA", "P"):        # fond blanc sous la transparence (captures d'écran)
            from PIL import Image
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
            im = bg
        im = im.convert("RGB")
        im.thumbnail((1600, 1600))
        return im

    def save_image(self, data_url):
        """Photo envoyée par la page (data URL) : réduite à 1600 px de côté au plus, JPEG, sauvée dans le board."""
        import io
        from PIL import Image, ImageOps
        b64 = data_url.split(",", 1)[-1]
        raw = base64.b64decode(b64)
        if len(raw) > 25 * 1024 * 1024:
            raise ValueError("image trop lourde (25 Mo max)")
        im = self._flatten(ImageOps.exif_transpose(Image.open(io.BytesIO(raw))))
        d = SESS / self.a.slug / "images"
        d.mkdir(parents=True, exist_ok=True)
        path = d / (time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}.jpg")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        path.write_bytes(buf.getvalue())
        log(f"photo sauvée : {path.name} ({im.size[0]}x{im.size[1]}, {len(buf.getvalue()) // 1024} ko)")
        return str(path), "image/jpeg", base64.b64encode(buf.getvalue()).decode()

    async def take_selection(self):
        """Ce que l'élève montre au tableau (sélection à la souris dans une carte) : (préfixe pour le prof,
        rappel court pour la page). La sélection est effacée une fois envoyée, pour ne pas coller aux suivantes."""
        try:
            sel = json.loads((CAHIER.parent / "sessions" / self.a.slug / "cahier.json").read_text())["state"].get("selection")
        except (OSError, ValueError, KeyError):
            return "", None
        if not sel or not sel.get("id"):
            return "", None
        where = f"carte {sel['id']}" + (f", ligne {sel['ligne']}" if sel.get("ligne") else "")
        what = f"formule {sel['tex']}" if sel.get("tex") else f"« {sel.get('texte', '')} »"
        p = await asyncio.create_subprocess_exec("python3", str(CAHIER), "selection", self.a.slug, "--clear",
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await p.wait()
        log(f"l'élève montre : {where}, {what}")
        short = f"{sel['id']}" + (f" ligne {sel['ligne']}" if sel.get("ligne") else "")
        return f"[L'élève montre au tableau : {where}, {what}]\n", short

    async def kickoff(self):
        if self.brain.fresh:
            p = await asyncio.create_subprocess_exec("python3", str(CAHIER), "state", self.a.slug,
                                                     stdout=asyncio.subprocess.PIPE)
            board = (await p.communicate())[0].decode().strip()
            board = f" Le tableau contient déjà des cartes d'une séance précédente :\n{board}\n" if "\t" in board else ""
            await self.question(f"[Séance : début du cours sur « {self.a.topic} ».{board} Salue-le en une phrase, "
                                "puis demande-lui en une question ce qu'il sait déjà ou par où il veut commencer.]")
            self.history.pop()

    @staticmethod
    def join_texte(parts):
        """« mark 12.2 --texte Q 12.2 » écrit sans guillemets : tout ce qui suit --texte, jusqu'à l'option
        suivante, forme un seul texte (sinon cahier.py refuse les mots en trop)."""
        if "--texte" not in parts:
            return parts
        i = parts.index("--texte") + 1
        j = next((k for k in range(i, len(parts)) if parts[k].startswith("--")), len(parts))
        return parts[:i] + ([" ".join(parts[i:j])] if j > i else []) + parts[j:]

    async def run_cues(self, cues, idx=None):
        """Exécute les repères d'une phrase au moment où elle est jouée (liste blanche de commandes du cahier)."""
        import shlex
        for c in cues:
            try:
                parts = shlex.split(c)
            except ValueError:
                parts = c.split()
            if parts and parts[0] == "pause":          # ⟦pause⟧ : coupure entre deux parties, gérée par la page
                continue
            if not parts or parts[0] not in CUE_CMDS:
                log(f"repère refusé : {c!r}")
                continue
            parts = self.join_texte(parts)
            t = time.time()
            p = await asyncio.create_subprocess_exec("python3", str(CAHIER), parts[0], self.a.slug, *parts[1:],
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out = (await p.communicate())[0].decode(errors="replace").strip().splitlines()
            log(f"repère exécuté (phrase {idx}) : {c}  [{time.time() - t:.2f} s] {out[-1] if out else ''}")

    def write_activity(self, active):
        """Regulator compte le temps d'une app de travail au premier plan ; quand on parle et écoute sans taper,
        ce fichier (frais de moins de 15 s, playing=true) lui dit que la séance est active.
        Seulement si l'app Whiteboard l'a demandé (variable PROF_ACTIVITY)."""
        path = os.environ.get("PROF_ACTIVITY")
        if not path:
            return
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(path).with_suffix(".tmp")
            tmp.write_text(json.dumps({"app": "whiteboard", "playing": active, "ts": time.time()}))
            os.replace(tmp, path)
        except OSError:
            pass

    async def headset(self, mode):
        """casque-mode appel|musique|status : une sortie d'une ligne, code 0 même en cas d'échec (lire le texte).
        Jamais en boucle ni de relance : les bascules rapides répétées coincent ce casque."""
        if not CASQUE.exists():
            return False, "casque-mode absent"
        p = await asyncio.create_subprocess_exec(str(CASQUE), mode, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
        try:
            out = (await asyncio.wait_for(p.communicate(), 20))[0].decode(errors="replace").strip()
        except asyncio.TimeoutError:
            p.kill()
            return False, "casque-mode ne répond pas"
        ok = out.startswith(("Mode appel", "Mode musique", "Déjà en mode")) or mode == "status"
        if mode == "appel" and out.startswith("Mode appel"):     # on l'a basculé : on le remettra en musique à la fin
            CASQUE_MARK.parent.mkdir(parents=True, exist_ok=True)
            CASQUE_MARK.write_text(str(time.time()))
        log(f"casque-mode {mode} : {out}")
        return ok, out

    # --- calibration du micro (banc d'essai de la reconnaissance vocale : bench_asr.py) ---
    async def calib(self, raw, sr, hdr):
        """Garde l'enregistrement brut (taux natif) + la phrase lue, et renvoie ce que Whisper a compris."""
        import wave
        run = re.sub(r"[^\w.-]+", "_", hdr.get("run", "run"))[:80]
        d = HERE / "calib" / run
        d.mkdir(parents=True, exist_ok=True)
        name = f"{int(hdr.get('idx', 0)):02d}-{'filtres' if hdr.get('filters') else 'brut'}"
        with wave.open(str(d / f"{name}.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
            w.writeframes((np.clip(raw, -1, 1) * 32767).astype(np.int16).tobytes())
        meta = {"file": f"{name}.wav", "sr": sr, "ref": hdr.get("ref", ""), "idx": hdr.get("idx"),
                "maths": bool(hdr.get("maths")), "filters": bool(hdr.get("filters")), "mic": hdr.get("mic", ""),
                "ts": time.time()}
        with open(d / "meta.jsonl", "a") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        audio = raw if sr == 16000 else soxr.resample(raw, sr, 16000, quality="HQ").astype(np.float32)
        text = await asyncio.get_running_loop().run_in_executor(None, self.ears.transcribe, audio, meta["maths"])
        log(f"calibration {run}/{name} : {text!r}  (lu : {meta['ref']!r})")
        await self.send({"t": "calib_ok", "idx": meta["idx"], "filters": meta["filters"], "text": text})

    # --- autorisations (le prof veut lancer une commande, écrire un fichier…) ---
    @staticmethod
    def perm_key(req):
        """Clé de « toujours pour cette séance » : l'outil, et pour Bash le premier mot de la commande."""
        tool, inp = req.get("tool_name", ""), req.get("input", {}) or {}
        if tool == "Bash":
            return "Bash:" + ((inp.get("command") or "").strip().split() or [""])[0]
        return tool

    @staticmethod
    def perm_text(req):
        tool, inp = req.get("tool_name", ""), req.get("input", {}) or {}
        if tool == "Bash":
            return "lancer une commande", inp.get("command", "")
        if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            verb = "écrire" if tool == "Write" else "modifier"
            return f"{verb} un fichier", inp.get("file_path") or inp.get("notebook_path", "")
        if tool in ("WebFetch", "WebSearch"):
            return "aller sur le web", inp.get("url") or inp.get("query", "")
        return f"utiliser {req.get('display_name') or tool}", json.dumps(inp, ensure_ascii=False)[:400]

    async def on_permission(self, gen, j):
        req, rid = j["request"], j["request_id"]
        if gen is not None and gen != self.gen:
            return await self.brain.answer_permission(rid, False, message="Réponse abandonnée (l'élève est passé à autre chose).")
        key = self.perm_key(req)
        if key in self.perm_always:
            log(f"autorisation (toujours pour la séance) : {key}")
            return await self.brain.answer_permission(rid, True, req.get("input"))
        what, detail = self.perm_text(req)
        msg = {"t": "perm", "id": rid, "what": what, "detail": detail, "tool": req.get("tool_name"),
               "why": req.get("description") or "", "key": key}
        self.perms[rid] = (req, msg)
        log(f"demande d'autorisation : {what} : {detail[:200]}")
        await self.send(msg)
        await self.queue_say(gen if gen is not None else self.gen, "J'ai besoin de ton accord à l'écran.")

    async def perm_answer(self, rid, choice):
        if rid not in self.perms:
            return
        req, _msg = self.perms.pop(rid)
        allow = choice in ("allow", "always")
        perms = None
        if choice == "always":
            self.perm_always.add(self.perm_key(req))
            # seulement des règles pour cet outil : jamais les suggestions « changer de mode » (acceptEdits ouvrait
            # aussi rm et compagnie pour toute la séance)
            perms = [p for p in req.get("permission_suggestions") or []
                     if p.get("destination") == "session" and p.get("type") == "addRules"] or None
        log(f"autorisation {'accordée' if allow else 'refusée'} ({choice}) : {self.perm_text(req)[1][:160]}")
        await self.brain.answer_permission(rid, allow, req.get("input"), perms)
        await self.send({"t": "perm_done", "id": rid})

    async def deny_pending_perms(self, why):
        for rid in list(self.perms):
            self.perms.pop(rid)
            await self.brain.answer_permission(rid, False, message=why)
            await self.send({"t": "perm_done", "id": rid})

    # --- événements du cerveau ---
    async def on_brain(self, kind, gen, data):
        if kind == "permission":
            return await self.on_permission(gen, data)
        if gen != self.gen:
            return                    # vieille génération (interrompue) : on ignore
        if kind == "text":
            if "first_text" not in self.t:
                self.t["first_text"] = time.time()
            self.buf += data
            self.gen_text += data
            sents, self.buf = cut_sentences(self.buf)
            for s in sents:
                await self.queue_say(gen, s)
        elif kind == "block_end":
            sents, self.buf = cut_sentences(self.buf, flush=True)
            for s in sents:
                await self.queue_say(gen, s)
        elif kind == "tool_start":
            sents, self.buf = cut_sentences(self.buf, flush=True)
            for s in sents:
                await self.queue_say(gen, s)
            await self.send({"t": "tool", "text": {"Read": "je lis…", "Glob": "je cherche…", "Grep": "je cherche…",
                                                   "Edit": "je modifie…", "Write": "j'écris…", "WebFetch": "je consulte le web…",
                                                   "WebSearch": "je cherche sur le web…"}.get(data, "j'agis…")})
        elif kind == "tool":
            m = re.search(r"(?:cahier|pron|scene)\.py\s+(\w+)\s*(.*)", data)
            short = f"{m.group(1)} {m.group(2).splitlines()[0][:80]}" if m else data.splitlines()[0][:100] if data else ""
            log("outil :", short)
            await self.send({"t": "tool", "text": ("tableau : " if m else "ordinateur : ") + short})
        elif kind == "tool_result":
            log("  ->", data.replace("\n", " ")[:160])
        elif kind == "result":
            sents, self.buf = cut_sentences(self.buf, flush=True)
            for s in sents:
                await self.queue_say(gen, s)
            await self.ttsq.put((gen, None, None, self.cues))
            self.cues = []
            if data.get("subtype") not in ("success", None):
                log("fin de tour :", data.get("subtype"))

    async def queue_say(self, gen, raw):
        # repères ⟦…⟧ : retirés du texte parlé et affiché, rattachés à cette phrase ; une phrase sans texte
        # (repère seul) les passe à la phrase suivante, ou à un « silence » en fin de tour
        raw, cues = take_cues(raw)
        self.cues += cues
        text = speakable(raw)
        if text and re.search(r"\w", text):
            await self.ttsq.put((gen, self.idx, text, self.cues))
            self.cues = []
            self.idx += 1

    async def tts_worker(self):
        loop = asyncio.get_running_loop()
        while True:
            gen, idx, text, cues = await self.ttsq.get()
            if gen != self.gen:
                continue
            if idx is None:
                if cues:                    # repères restés sans phrase après eux : un silence qui les porte
                    await self.say_out({"t": "say", "gen": gen, "idx": -1, "text": "", "sr": 16000, "cues": cues,
                                        "pcm": base64.b64encode(b"\0\0" * 800).decode()})
                self.gen_done = True
                await self.send({"t": "done", "gen": gen})
                continue
            if self.silent:                 # mode silencieux : ni synthèse ni son, la page affiche au rythme de lecture
                await self.say_out({"t": "say", "gen": gen, "idx": idx, "text": text, "cues": cues, "silent": True})
                continue
            t = time.time()
            pcm, sr = await loop.run_in_executor(None, self.voice.pcm, text)
            if gen != self.gen:
                continue
            if idx == 0:
                self.t["first_audio"] = time.time()
                tt = self.t
                log(f"latence serveur : stt {tt['stt'] - tt['t0']:.2f} s | 1er mot Claude "
                    f"{tt.get('first_text', 0) - tt['stt']:.2f} s | 1re phrase synthétisée "
                    f"{tt['first_audio'] - tt['t0']:.2f} s après réception (piper {time.time() - t:.2f} s)")
            await self.say_out({"t": "say", "gen": gen, "idx": idx, "text": text, "sr": sr, "cues": cues,
                                "pcm": base64.b64encode(pcm).decode()})

    async def say_out(self, msg):
        """Envoie une phrase à la page et la garde en file jusqu'à l'accusé de lecture ({t: played}) : si la page
        plante ou se recharge, elle la reçoit de nouveau à la reconnexion (la génération, elle, continue)."""
        if msg["gen"] == self.gen:
            self.outbox.append(msg)
        await self.send(msg)

    def played(self, gen, idx):
        """La page a fini de jouer (ou d'afficher) cette phrase : elle sort de la file et entre dans l'historique."""
        for k, m in enumerate(self.outbox):
            if m["gen"] == gen and m["idx"] == idx:
                for done in self.outbox[:k + 1]:            # celles d'avant aussi (jouées dans l'ordre)
                    if done["text"]:
                        self.history.append(("prof", done["text"]))
                del self.outbox[:k + 1]
                return

    def keep_audio(self, audio, text, raw=None, sr=16000):
        """Garde chaque segment + sa transcription (corpus pour un futur fine-tuning de Whisper sur sa voix)."""
        import wave
        d = SESS / self.a.slug / "audio"
        d.mkdir(parents=True, exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        with wave.open(str(d / f"{name}.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        if raw is not None and sr != 16000:      # le brut, au taux natif, pour comparer
            with wave.open(str(d / f"{name}.raw{sr}.wav"), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
                w.writeframes((np.clip(raw, -1, 1) * 32767).astype(np.int16).tobytes())
        with open(d / "transcriptions.jsonl", "a") as f:
            f.write(json.dumps({"audio": f"{name}.wav", "raw": f"{name}.raw{sr}.wav" if sr != 16000 else None,
                                "text": text, "whisper": text, "corrige": None},
                               ensure_ascii=False) + "\n")

    # --- websocket ---
    async def handle_ws(self, req):
        ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024, heartbeat=20)
        await ws.prepare(req)
        if req.query.get("v") != "2":
            # page chargée avant la mise à jour (elle se reconnecte en boucle) : refusée sans prendre la main
            await ws.close(code=4001, message=b"page perimee")
            return ws
        old, self.ws = self.ws, ws
        if old is not None and not old.closed:
            # une seule page active : la dernière ouverte gagne, l'ancienne est prévenue
            log("autre page déjà connectée : elle est remplacée")
            try:
                await old.send_str(json.dumps({"t": "replaced"}))
                await old.close(code=4001, message=b"ouverte ailleurs")
            except Exception:
                pass
        log("page connectée")
        await self.send({"t": "info", "slug": self.a.slug, "topic": self.a.topic, "model": self.a.model,
                         "history": self.history[-12:], "voices": self.voice.catalog(), "voice": self.voice.current(),
                         "silent": self.silent})
        next_sr = 16000          # taux du prochain segment binaire (annoncé par {t: audio, sr})
        next_hdr = {}            # en-tête complet du prochain segment ({target: "calib", …})
        for _req, pm in self.perms.values():      # demandes d'autorisation encore en attente
            await self.send(pm)
        if self.outbox or self.brain.busy:
            # reconnexion (plantage du rendu, rechargement) en pleine réponse : on reprend où la page s'était arrêtée
            log(f"reprise : {len(self.outbox)} phrase(s) pas encore jouée(s) renvoyée(s)"
                + (", le prof écrit encore" if self.brain.busy else ""))
            await self.send({"t": "gen", "gen": self.gen})
            for m in list(self.outbox):
                await self.send(m)
            if self.gen_done:
                await self.send({"t": "done", "gen": self.gen})
            await self.state("thinking" if self.brain.busy and not self.outbox else "listening")
        else:
            await self.state("listening")
        loop = asyncio.get_running_loop()
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                t0 = time.time()
                raw, sr, next_sr = np.frombuffer(msg.data, np.float32), next_sr, 16000
                hdr, next_hdr = next_hdr, {}
                if hdr.get("target") == "calib":      # calibration du micro : jamais envoyé au prof
                    asyncio.create_task(self.calib(raw, sr, hdr))
                    continue
                # la page envoie au taux natif du micro (souvent 48 kHz) : rééchantillonnage propre ici
                audio = raw if sr == 16000 else soxr.resample(raw, sr, 16000, quality="HQ").astype(np.float32)
                rms = float(np.sqrt(np.mean(raw ** 2))) if len(raw) else 0.0
                # trous de capture : plages d'échantillons exactement nuls (≥ 5 ms) au milieu du segment
                z = np.concatenate([[0], (raw == 0).astype(np.int8), [0]])
                st, en = np.flatnonzero(np.diff(z) == 1), np.flatnonzero(np.diff(z) == -1)
                holes = int(np.sum(((en - st) >= sr // 200) & (st > 0) & (en < len(raw))))
                log(f"segment reçu : {len(raw) / sr:.1f} s à {sr} Hz, max {float(np.abs(raw).max()) if len(raw) else 0:.2f}, "
                    f"rms {rms:.3f}" + (f", ⚠ {holes} trous de silence numérique (capture ?)" if holes else ""))
                await self.state("transcribing")
                text = await loop.run_in_executor(None, self.ears.transcribe, audio)
                log(f"entendu ({len(audio) / 16000:.1f} s d'audio, stt {time.time() - t0:.2f} s) : {text!r}")
                loop.run_in_executor(None, self.keep_audio, audio, text, raw, sr)
                if not text:
                    await self.state("listening")
                    if self.interrupted is not None and not self.brain.busy:
                        self.interrupted = None
                    continue
                t_stt = time.time()
                prefix, shows = await self.take_selection()
                sent = await self.attach_paths(text)
                await self.send({"t": "user", "text": text, "shows": shows, "images": self.take_urls()})
                await self.question(sent, t0, t_stt, prefix)
            elif msg.type == WSMsgType.TEXT:
                j = json.loads(msg.data)
                if j["t"] == "start" and not self.started:
                    self.started = True
                    await self.kickoff()
                elif j["t"] == "barge" and not self.brain.busy and not j.get("last"):
                    pass             # rien à couper (le prof ne parle pas et ne réfléchit pas)
                elif j["t"] == "barge":
                    # l'élève parle : on coupe la génération en cours (l'audio est déjà coupé côté page)
                    self.interrupted = {"last": j.get("last") or ""}
                    log("interruption ; dernière phrase entendue :", repr(j.get("last")))
                    await self.deny_pending_perms("L'élève t'a coupé la parole.")
                    self.gen += 1      # ignore tout ce qui arrive encore de l'ancienne génération
                    self.outbox = []
                    await self.brain.interrupt()
                elif j["t"] == "text":
                    prefix, shows = await self.take_selection()
                    sent = await self.attach_paths(j["text"])          # chemins de fichiers dans le message
                    await self.send({"t": "user", "text": j["text"], "shows": shows, "images": self.take_urls()})
                    await self.question(sent, prefix=prefix)
                elif j["t"] == "dropdiag":         # diagnostic du glisser-déposer (ce que WebKit transmet)
                    log("dépôt (diagnostic page) :", json.dumps(j.get("info"), ensure_ascii=False)[:600])
                elif j["t"] == "audio":
                    next_sr, next_hdr = int(j.get("sr", 16000)), j
                    if j.get("mic"):
                        log(f"micro : {j['mic']} ({next_sr} Hz)")
                elif j["t"] == "voice":
                    await loop.run_in_executor(None, self.voice.set, j["voice"], j.get("speaker"), j.get("speed", 1.0))
                    log("voix :", self.voice.current())
                    await self.send({"t": "voice", "voice": self.voice.current()})
                elif j["t"] == "sample":
                    pcm, sr = await loop.run_in_executor(None, self.voice.pcm, self.voice.SAMPLE)
                    await self.send({"t": "sample", "sr": sr, "pcm": base64.b64encode(pcm).decode()})
                elif j["t"] == "headset":
                    ok, out = await self.headset(j.get("mode", "status"))
                    await self.send({"t": "headset", "ok": ok, "out": out})
                elif j["t"] == "fake_turn" and os.environ.get("PROF_TEST") == "1":
                    # test : un tour du prof simulé, découpé en petits morceaux comme le streaming de Claude
                    self.gen += 1
                    self.buf, self.idx, self.gen_text, self.cues = "", 0, "", []
                    self.outbox, self.gen_done = [], False
                    self.t = {"t0": time.time(), "stt": time.time()}
                    await self.send({"t": "gen", "gen": self.gen})
                    txt, n = j["text"], int(j.get("chunk", 7))
                    for k in range(0, len(txt), n):
                        await self.on_brain("text", self.gen, txt[k:k + n])
                        await asyncio.sleep(0.02)
                    await self.on_brain("result", self.gen, {"subtype": "success"})
                elif j["t"] == "drop_paths":       # fichiers glissés depuis le disque (chemins / file://)
                    for uri in j.get("paths", [])[:6]:
                        try:
                            import urllib.parse
                            pp = Path(urllib.parse.unquote(uri[7:] if uri.startswith("file://") else uri).strip()).expanduser()
                            if pp.suffix.lower() == ".pdf":
                                if not pp.is_file():
                                    raise ValueError(f"{pp.name} : fichier introuvable")
                                await self.send_pdf_pages(pp)
                                continue
                            name, data = await loop.run_in_executor(None, self.read_dropped, uri)
                            await self.send({"t": "dropped", "name": name, "data": data})
                        except Exception as e:
                            log("dépôt refusé :", e)
                            await self.send({"t": "drop_error", "text": str(e)})
                elif j["t"] == "pdf":              # PDF choisi (📎) ou collé : une image par page, comme des photos
                    import tempfile
                    try:
                        raw = base64.b64decode(j.get("data", "").split(",", 1)[-1])
                        if len(raw) > 50 * 1024 * 1024:
                            raise ValueError("PDF trop lourd (50 Mo max)")
                        with tempfile.TemporaryDirectory() as d:
                            p = Path(d) / Path(j.get("name") or "document.pdf").name
                            p.write_bytes(raw)
                            await self.send_pdf_pages(p)
                    except Exception as e:
                        log("PDF refusé :", e)
                        await self.send({"t": "drop_error", "text": str(e)})
                elif j["t"] == "images":           # photos à joindre au prochain message de l'élève
                    urls = []
                    for im in j.get("images", [])[:16]:
                        try:
                            saved = await loop.run_in_executor(None, self.save_image, im.get("data", ""))
                        except Exception as e:
                            log("photo refusée :", e)
                            await self.send({"t": "error", "text": f"photo refusée : {e}"})
                            continue
                        self.pending_images.append(saved)
                        urls.append("/images/" + Path(saved[0]).name)
                    log(f"{len(urls)} photo(s) jointe(s) au prochain message")
                    self.pending_urls = urls
                elif j["t"] == "silent":
                    on = bool(j.get("on"))
                    if on != self.silent:
                        self.silent = on
                        self.brain.meta["silent"] = on
                        self.brain.meta_f.write_text(json.dumps(self.brain.meta))
                        # le prof l'apprend avec le prochain message de l'élève (pas de tour pour rien)
                        self.mode_notice = ("[Mode silencieux : l'élève lit tes réponses à l'écran et écrit au clavier ; "
                                            "il ne parle pas et n'entend rien. Encore plus court, tout au tableau.]" if on else
                                            "[Mode voix : l'élève t'entend de nouveau et peut parler.]")
                        log("mode silencieux" if on else "mode voix")
                    await self.send({"t": "silent", "on": self.silent})
                elif j["t"] == "perm_answer":
                    await self.perm_answer(j.get("id"), j.get("choice"))
                elif j["t"] == "played":
                    self.played(j.get("gen"), j.get("idx"))
                elif j["t"] == "cue":            # la page commence à JOUER une phrase : on exécute ses repères
                    log(f"phrase {j.get('idx')} jouée par la page, repères : {j.get('cues')}")
                    asyncio.create_task(self.run_cues(j.get("cues", []), j.get("idx")))
                elif j["t"] == "miclat":         # délai entre l'appui sur espace et le 1er son du micro
                    log(f"micro prêt en {j.get('ms')} ms ({j.get('mic', '')})")
                elif j["t"] == "scene_error":    # une scène du tableau a affiché une erreur chez l'élève
                    e = (f"« {j.get('scene')} » : " if j.get("scene") else "") + j.get("text", "")
                    log("erreur dans une scène :", e)
                    if e not in self.scene_errors and len(self.scene_errors) < 5:
                        self.scene_errors.append(e)
                elif j["t"] == "activity":       # battement de la page (toutes les 4 s) : signal pour Regulator
                    self.write_activity(bool(j.get("active")))
                elif j["t"] == "latency":
                    line = (f"{time.strftime('%F %T')} model={self.a.model} fin_parole->1er_son="
                            f"{j['ms'] / 1000:.2f}s (dont attente fin VAD {j.get('vad_ms', 0) / 1000:.2f}s)")
                    log(line)
                    with open(LOG, "a") as f:
                        f.write(line + "\n")
        if self.ws is ws:
            self.ws = None
            self.write_activity(False)
        log("page déconnectée")
        return ws


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("topic")
    ap.add_argument("--port", type=int, default=8778)
    ap.add_argument("--model", default=os.environ.get("PROF_MODEL", "claude-opus-5-5"))
    ap.add_argument("--effort", default=os.environ.get("PROF_EFFORT", "low"))
    # réglages de la reconnaissance vocale : voir bench_asr.py (mesure du WER sur les enregistrements de calibration)
    ap.add_argument("--whisper", default=os.environ.get("PROF_WHISPER", "large-v3-turbo"))
    ap.add_argument("--compute", default=os.environ.get("PROF_WHISPER_COMPUTE", "int8_float16"))
    ap.add_argument("--beam", type=int, default=int(os.environ.get("PROF_WHISPER_BEAM", "5")))
    a = ap.parse_args()

    prof = Prof(a)
    await prof.brain.start()
    asyncio.create_task(prof.tts_worker())

    app = web.Application()
    page = (HERE / "prof.html")
    app.router.add_get("/", lambda r: web.Response(text=page.read_text(), content_type="text/html",
                                                   headers={"Cache-Control": "no-store"}))
    app.router.add_get("/ws", prof.handle_ws)
    app.router.add_static("/tests", HERE / "tests")
    (SESS / a.slug / "images").mkdir(parents=True, exist_ok=True)
    app.router.add_static("/images", SESS / a.slug / "images")       # photos de l'élève (miniatures de la transcription)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", a.port).start()
    log(f"Prof prêt : http://localhost:{a.port}/  (tableau : http://localhost:8777/s/{a.slug})")
    await asyncio.Event().wait()


def casque_musique_si_besoin():
    """Fin de séance : remet le casque en musique, une seule fois, et seulement si le Prof l'avait mis en appel."""
    if CASQUE_MARK.exists() and CASQUE.exists():
        CASQUE_MARK.unlink(missing_ok=True)
        import subprocess
        try:
            out = subprocess.run([str(CASQUE), "musique"], capture_output=True, text=True, timeout=20).stdout.strip()
            log("casque-mode musique :", out)
        except Exception as e:
            log("casque-mode musique :", e)


if __name__ == "__main__":
    import signal, ctypes
    try:        # nom court du processus (Regulator : keep_alive de l'app Whiteboard)
        ctypes.CDLL(None).prctl(15, b"prof_server", 0, 0, 0)
    except (OSError, AttributeError):
        pass

    def _term(*_):                      # arrêt par Whiteboard : même sortie propre que Ctrl-C
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        casque_musique_si_besoin()
