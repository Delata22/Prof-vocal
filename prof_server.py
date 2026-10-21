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

CAHIER = Path.home() / ".claude/cahier/cahier.py"
VOICE = Path.home() / ".claude/cahier/voices/fr_FR-siwis-medium.onnx"
SESS = HERE / "sessions"
LOG = HERE / "latency.log"
LEXIQUE = Path.home() / ".claude/cahier/prononciation.json"   # partagé avec le cahier (cahier.py prononce)


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
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------- oreilles : Whisper ----------------

class Ears:
    HALLU = re.compile(r"sous-titr|merci d'avoir regard|abonnez-vous|amara\.org", re.I)

    def __init__(self, model, compute, topic, slug):
        from faster_whisper import WhisperModel
        t = time.time()
        self.m = WhisperModel(model, device="cuda", compute_type=compute)
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

    def transcribe(self, audio, maths=False):
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
        segs, _ = self.m.transcribe(audio, language="fr", beam_size=5, vad_filter=False,
                                    condition_on_previous_text=False, initial_prompt=prompt)
        segs = [s for s in segs if s.no_speech_prob < 0.8 and s.compression_ratio < 2.4 and s.avg_logprob > -1.0]
        txt = " ".join(s.text.strip() for s in segs).strip()
        copie = txt.lower().strip(" .") in prompt.lower()          # Whisper recopie le prompt sur un son trop court
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
    (re.compile(r"\[\[[^\]]*\]\]"), " "),        # marqueurs pour la page ([[réponse]])
    (re.compile(r"[*_#>|`~]+"), " "),
    (re.compile(r"^\s*[-•]\s+", re.M), ""),
    (re.compile(r"\s+"), " "),
]


def speakable(t):
    for r, s in SPEAKABLE:
        t = r.sub(s, t)
    return t.strip()


ANSWER_MARK = re.compile(r"\[\[\s*r[ée]ponse\s*\]\]", re.I)
SENT_END = re.compile(r"[.!?…]+[»\"')]*\s+|\n+")


def cut_sentences(buf, flush=False):
    """Renvoie (phrases complètes, reste)."""
    out = []
    while True:
        m = SENT_END.search(buf)
        if not m:
            break
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

    async def start(self):
        system = (HERE / "prompt.md").read_text().replace("{slug}", self.slug).replace("{topic}", self.topic) \
            .replace("{pron}", f"python3 {HERE / 'pron.py'}")
        # Bash limité au CLI du cahier ; Write limité à ./cards/ (le contenu des cartes, LaTeX compris :
        # un heredoc avec des « \ » ou des « * » est refusé par le filtre de permissions).
        allowed = [f"Bash(python3 {CAHIER}:*)", "Bash(python3 ~/.claude/cahier/cahier.py:*)", "Edit(./cards/**)",
                   # vocabulaire de la dictée et lexique de prononciation, que le prof complète en cours
                   # (~/.claude est protégé contre Edit : le lexique passe par pron.py)
                   "Read(./vocab.txt)", "Edit(./vocab.txt)", f"Bash(python3 {HERE / 'pron.py'}:*)"]
        args = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-partial-messages",
                "--setting-sources", "",                 # pas de hooks utilisateur (récap quotidien...)
                "--model", self.model, "--effort", self.effort,
                # pas d'auto-mémoire (elle autorise des écritures dans ~/.claude/projects/…/memory) ;
                # deny explicite sur ~/.claude, au cas où
                "--settings", json.dumps({"autoMemoryEnabled": False, "permissions": {"deny": [
                    "Edit(~/.claude/**)", f"Edit(/{Path.home()}/.claude/**)",
                    "Read(~/.claude/projects/**)", f"Read(/{Path.home()}/.claude/projects/**)"]}}),
                "--tools", "Bash,Write,Read,Edit", "--allowedTools", *allowed, "--permission-mode", "dontAsk",
                "--append-system-prompt", system]
        if self.meta.get("session_id"):
            args += ["--resume", self.meta["session_id"]]
        env = dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
        env.pop("LD_LIBRARY_PATH", None)
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

    async def ask(self, gen, text):
        self.pending.append(gen)
        await self._send({"type": "user", "message": {"role": "user", "content": text}})

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
            elif ty == "result":
                if self.pending:
                    self.pending.pop(0)
                await self.on_event("result", gen, j)
        log("claude s'est arrêté, code", await proc.wait())
        for g in self.pending:
            await self.on_event("result", g, {"subtype": "process_exit"})
        self.pending = []



# ---------------- conversion parole -> LaTeX (panneau « Ta réponse ») ----------------

CONV_SYS = r"""Tu convertis une ligne de calcul dictée en français (sortie d'une reconnaissance vocale, fautes possibles) en UNE ligne LaTeX.
Réponds uniquement par cette ligne LaTeX : pas de $, pas de bloc de code, pas de commentaire, pas de seconde ligne.
Lettres grecques en commandes (\sigma), indices avec _, « racine de » → \sqrt{}, « sur » ou « divisé par » → \frac{}{},
« fois » → \, ou \times, « environ égal » → \approx, « laplacien » → \nabla^2, « dérivée partielle » → \partial.
Nombres à virgule : point décimal (« un virgule cinq » → 1.5), en particulier dans un vecteur ou un n-uplet : (1.5, -0.5), jamais (1,5, -0,5).
La reconnaissance vocale écrit souvent le NOM d'une lettre comme un mot qui sonne pareil : retrouve la lettre.
« haine », « aine », « hen » → n ; « aime », « hem » → m ; « cas », « ka » → k ; « thé », « té » → t ; « pé », « paix » → p ;
« ix », « hix » → x ; « i grec » → y ; « zed » → z ; « elle », « aile » → l ; « èf », « effe » → f ; « gé », « j'ai » → g ;
« achète », « hache » → h ; « air », « erre » → r ; « esse » → s ; « vé » → v ; « bé » → b ; « cé », « c'est » → c ; « dé » → d.
« à mentir », « un entier » → un entier (\in \mathbb{N} ou \in \mathbb{Z} selon le contexte) ; « un réel », « une réelle » → \in \mathbb{R}.
Les mots du texte (soit, pour tout, il existe, donc, alors) restent du texte : \text{soit } n \in \mathbb{N}.
Écris fidèlement ce qui est dicté, même si c'est faux : ne corrige jamais le raisonnement.
Si le message contient « LIGNE ACTUELLE » et « CORRECTION », renvoie la ligne actuelle modifiée selon la consigne,
en ne changeant que ce qu'elle vise ; « X, pas Y » veut dire : remplace Y par X.
Cours : {topic}.
Exemples :
sigma deux égale racine de deux fois sigma un → \sigma_2 = \sqrt{2}\,\sigma_1
d sur dx de e puissance moins x carré → \frac{d}{dx} e^{-x^2}
soit haine à mentir → \text{soit } n \in \mathbb{N}
pour tout ix réel, èf de ix supérieur ou égal à zéro → \forall x \in \mathbb{R},\ f(x) \geq 0
LIGNE ACTUELLE : a = b^2 + c / CORRECTION : moins c, pas plus c → a = b^2 - c"""


class Converter:
    """Un processus Claude séparé (jamais le prof), persistant pour la latence, sans outils."""
    def __init__(self, model, topic):
        self.model, self.topic = model, topic
        self.proc, self.lock, self.n, self.drainer = None, asyncio.Lock(), 0, None

    async def start(self):
        env = dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
        env.pop("LD_LIBRARY_PATH", None)
        cwd = SESS / "_conv"
        cwd.mkdir(parents=True, exist_ok=True)
        self.proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--setting-sources", "", "--tools", "", "--model", self.model,
            "--effort", "low", "--no-session-persistence", "--system-prompt", CONV_SYS.replace("{topic}", self.topic),
            cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024)
        self.n = 0
        log(f"convertisseur LaTeX lancé ({self.model})")

    async def convert(self, text, current=None):
        async with self.lock:
            if self.drainer:                     # fin du tour précédent pas encore lue
                await self.drainer
                self.drainer = None
            if self.proc is None or self.proc.returncode is not None or self.n >= 40:   # contexte borné
                if self.proc and self.proc.returncode is None:
                    self.proc.kill()
                await self.start()
            self.n += 1
            msg = f"LIGNE ACTUELLE : {current}\nCORRECTION : {text}" if current is not None else text
            self.proc.stdin.write((json.dumps({"type": "user", "message": {"role": "user", "content": msg}}) + "\n").encode())
            await self.proc.stdin.drain()
            out, done = "", False
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                j = json.loads(line)
                if j.get("type") == "stream_event" and not done:
                    ev = j["event"]
                    if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                        out += ev["delta"]["text"]
                    elif ev.get("type") in ("content_block_stop", "message_stop") and out.strip():
                        done = True
                        # la fin du tour (result) arrive ~2 s après : on la lit en tâche de fond
                        self.drainer = asyncio.create_task(self._drain())
                        break
                elif j.get("type") == "result":
                    break
            return self.clean(out)

    async def _drain(self):
        while True:
            line = await self.proc.stdout.readline()
            if not line or json.loads(line).get("type") == "result":
                return

    @staticmethod
    def clean(out):
        out = out.strip().strip("`").strip()
        out = next((l for l in out.splitlines() if l.strip()), "")
        out = re.sub(r"^(latex)?\s*", "", out, flags=re.I) if out.lower().startswith("latex") else out
        return out.strip().strip("$").strip()


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
        self.ears = Ears(a.whisper, a.compute, a.topic, a.slug)
        self.voice = Voice()
        self.brain = Brain(a.slug, a.topic, a.model, a.effort, self.on_brain)
        self.conv = Converter(a.conv_model, a.topic)
        self.gen_text = ""
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
    async def question(self, text, t0=None, t_stt=None):
        if self.brain.busy:
            await self.brain.interrupt()
        self.gen += 1
        self.buf, self.idx, self.gen_text = "", 0, ""
        self.t = {"t0": t0 or time.time(), "stt": t_stt or time.time()}
        await self.send({"t": "gen", "gen": self.gen})
        msg = text
        if self.interrupted is not None:
            last = self.interrupted.get("last")
            if last:
                msg = (f"[Interruption : l'élève t'a coupé pendant que tu disais « {last} ». "
                       f"La suite de ta réponse n'a pas été entendue.]\n{text}")
            else:
                msg = f"[Interruption : l'élève a repris la parole avant d'entendre ta réponse.]\n{text}"
            self.interrupted = None
        self.history.append(("élève", text))
        await self.state("thinking")
        await self.brain.ask(self.gen, msg)

    async def kickoff(self):
        if self.brain.fresh:
            p = await asyncio.create_subprocess_exec("python3", str(CAHIER), "state", self.a.slug,
                                                     stdout=asyncio.subprocess.PIPE)
            board = (await p.communicate())[0].decode().strip()
            board = f" Le tableau contient déjà des cartes d'une séance précédente :\n{board}\n" if "\t" in board else ""
            await self.question(f"[Séance : début du cours sur « {self.a.topic} ».{board} Salue-le en une phrase, "
                                "puis demande-lui en une question ce qu'il sait déjà ou par où il veut commencer.]")
            self.history.pop()

    # --- panneau « Ta réponse » ---
    async def answer_line(self, text, last=None):
        """Une ligne dictée : nouvelle ligne de calcul, « parce que… », « corrige : … » ou « j'envoie »."""
        t = text.strip()
        low = t.lower()
        if not t:
            return await self.send({"t": "ans", "op": "none"})
        await self.state("listening")
        # « j'envoie » est souvent transcrit « j'en vois »
        if re.search(r"\bj'envoie\b|\bj'en ?vois\b|c'est ma réponse|envoie[rz]? (ma|la) réponse|\benvoyer?\b", low):
            return await self.send({"t": "ans", "op": "submit", "text": t})
        m = re.match(r"(parce que|parce qu'|car\b|pourquoi\b)\s*[:,]?\s*(.*)", t, re.I | re.S)
        if m:
            why = (m.group(1) + " " + m.group(2)).strip() if m.group(1).lower().startswith("parce") else m.group(2)
            return await self.send({"t": "ans", "op": "why", "text": why})
        m = re.match(r"(corrige|correction|corriger)\b\s*[:,.]?\s*(.*)", t, re.I | re.S)
        t0 = time.time()
        if m and last:
            latex = await self.conv.convert(m.group(2), current=last)
            op = "fix"
        else:
            latex = await self.conv.convert(m.group(2) if m else t)
            op = "add"
        log(f"ligne de calcul ({time.time() - t0:.2f} s) : {t!r} -> {latex!r}")
        if not re.search(r"[\\=^_+\-*/()<>|0-9]", latex) and len(latex.split()) > 3:   # une phrase, pas un calcul
            return await self.send({"t": "ans", "op": "none", "text": f"pas compris comme un calcul : « {t} »"})
        await self.send({"t": "ans", "op": op, "latex": latex, "text": t})

    async def submit_answer(self, lines):
        lines = [{"l": l.get("l", "").strip(), "why": l.get("why", "").strip()} for l in lines if l.get("l", "").strip()]
        if not lines:
            return
        k = self.brain.meta.get("answers", 0) + 1
        self.brain.meta["answers"] = k
        self.brain.meta_f.write_text(json.dumps(self.brain.meta))
        cid = f"R.{k}"
        f = self.brain.dir / "cards" / f"{cid}.json"
        f.write_text(json.dumps(lines, ensure_ascii=False))
        p = await asyncio.create_subprocess_exec("python3", str(CAHIER), "add", self.a.slug, cid, f"Ta réponse {k}",
                                                 "--kind", "derivation", "--step", "R. Tes réponses", "--file", str(f),
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        log(f"réponse écrite {cid} au tableau :", (await p.communicate())[0].decode().strip())
        body = "\n".join(f"Ligne {i + 1} : {l['l']}" + (f"   — {l['why']}" if l["why"] else "") for i, l in enumerate(lines))
        msg = (f"[Réponse écrite de l'élève, en LaTeX ; elle est au tableau, carte {cid}]\n{body}\n"
               f"[Commente-la ligne par ligne : ce qui est juste, et pour ce qui ne l'est pas, une question qui l'aide à "
               f"trouver lui-même. Tu peux mettre ta correction à côté : cahier.py duo {self.a.slug} {cid} <id>.]")
        await self.send({"t": "user", "text": "(réponse écrite : " + " ; ".join(l["l"] for l in lines) + ")"})
        await self.question(msg)
        self.history[-1] = ("élève", "réponse écrite " + cid)

    # --- événements du cerveau ---
    async def on_brain(self, kind, gen, data):
        if gen != self.gen:
            return                    # vieille génération (interrompue) : on ignore
        if kind == "text":
            if "first_text" not in self.t:
                self.t["first_text"] = time.time()
            self.buf += data
            before, self.gen_text = self.gen_text, self.gen_text + data
            if ANSWER_MARK.search(self.gen_text) and not ANSWER_MARK.search(before):
                log("le prof ouvre le panneau « Ta réponse »")
                await self.send({"t": "open_answer"})
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
            await self.send({"t": "tool", "text": "je note…" if data in ("Read", "Edit") else "j'écris au tableau…"})
        elif kind == "tool":
            m = re.search(r"(?:cahier|pron)\.py\s+(\w+)\s*(.*)", data)
            short = f"{m.group(1)} {m.group(2).splitlines()[0][:80]}" if m else data[:100]
            log("outil :", short)
            await self.send({"t": "tool", "text": "tableau : " + short})
        elif kind == "tool_result":
            log("  ->", data.replace("\n", " ")[:160])
        elif kind == "result":
            sents, self.buf = cut_sentences(self.buf, flush=True)
            for s in sents:
                await self.queue_say(gen, s)
            await self.ttsq.put((gen, None, None))
            if data.get("subtype") not in ("success", None):
                log("fin de tour :", data.get("subtype"))

    async def queue_say(self, gen, raw):
        text = speakable(raw)
        if text and re.search(r"\w", text):
            await self.ttsq.put((gen, self.idx, text))
            self.idx += 1

    async def tts_worker(self):
        loop = asyncio.get_running_loop()
        while True:
            gen, idx, text = await self.ttsq.get()
            if gen != self.gen:
                continue
            if idx is None:
                await self.send({"t": "done", "gen": gen})
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
            self.history.append(("prof", text))
            await self.send({"t": "say", "gen": gen, "idx": idx, "text": text, "sr": sr,
                             "pcm": base64.b64encode(pcm).decode()})

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
                         "history": self.history[-12:], "voices": self.voice.catalog(), "voice": self.voice.current()})
        next_sr = 16000          # taux du prochain segment binaire (annoncé par {t: audio, sr})
        next_hdr = {}            # en-tête complet ({target: "answer", last: ...} pour le panneau « Ta réponse »)
        await self.state("listening")
        loop = asyncio.get_running_loop()
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                t0 = time.time()
                raw, sr, next_sr = np.frombuffer(msg.data, np.float32), next_sr, 16000
                hdr, next_hdr = next_hdr, {}
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
                text = await loop.run_in_executor(None, self.ears.transcribe, audio, hdr.get("target") == "answer")
                log(f"entendu ({len(audio) / 16000:.1f} s d'audio, stt {time.time() - t0:.2f} s) : {text!r}")
                loop.run_in_executor(None, self.keep_audio, audio, text, raw, sr)
                if hdr.get("target") == "answer":
                    asyncio.create_task(self.answer_line(text, hdr.get("last")))
                    continue
                if not text:
                    await self.state("listening")
                    if self.interrupted is not None and not self.brain.busy:
                        self.interrupted = None
                    continue
                t_stt = time.time()
                await self.send({"t": "user", "text": text})
                await self.question(text, t0, t_stt)
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
                    self.gen += 1      # ignore tout ce qui arrive encore de l'ancienne génération
                    await self.brain.interrupt()
                elif j["t"] == "answer":
                    await self.submit_answer(j.get("lines", []))
                elif j["t"] == "answer_line":     # ligne tapée en français dans le panneau
                    asyncio.create_task(self.answer_line(j["text"], j.get("last")))
                elif j["t"] == "text":
                    await self.send({"t": "user", "text": j["text"]})
                    await self.question(j["text"])
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
                elif j["t"] == "latency":
                    line = (f"{time.strftime('%F %T')} model={self.a.model} fin_parole->1er_son="
                            f"{j['ms'] / 1000:.2f}s (dont attente fin VAD {j.get('vad_ms', 0) / 1000:.2f}s)")
                    log(line)
                    with open(LOG, "a") as f:
                        f.write(line + "\n")
        if self.ws is ws:
            self.ws = None
        log("page déconnectée")
        return ws


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("topic")
    ap.add_argument("--port", type=int, default=8778)
    ap.add_argument("--model", default=os.environ.get("PROF_MODEL", "claude-opus-5-5"))
    ap.add_argument("--effort", default=os.environ.get("PROF_EFFORT", "low"))
    ap.add_argument("--whisper", default="large-v3-turbo")
    ap.add_argument("--compute", default="int8_float16")
    ap.add_argument("--conv-model", default=os.environ.get("PROF_CONV_MODEL", "claude-haiku-5-5"))
    a = ap.parse_args()

    prof = Prof(a)
    await prof.brain.start()
    await prof.conv.start()
    asyncio.create_task(prof.tts_worker())

    app = web.Application()
    page = (HERE / "prof.html")
    app.router.add_get("/", lambda r: web.Response(text=page.read_text(), content_type="text/html",
                                                   headers={"Cache-Control": "no-store"}))
    app.router.add_get("/ws", prof.handle_ws)
    app.router.add_static("/tests", HERE / "tests")
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", a.port).start()
    log(f"Prof prêt : http://localhost:{a.port}/  (tableau : http://localhost:8777/s/{a.slug})")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
