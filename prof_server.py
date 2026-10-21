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

    def transcribe(self, audio):
        words = self.words()
        # initial_prompt : le contexte (Whisper imite le style et l'orthographe du prompt) ;
        # hotwords : les termes techniques à favoriser.
        prompt = f"Cours oral sur {self.topic}." + (f" Vocabulaire : {', '.join(words)}." if words else "")
        segs, _ = self.m.transcribe(audio, language="fr", beam_size=5, vad_filter=False,
                                    condition_on_previous_text=False, initial_prompt=prompt,
                                    hotwords=", ".join(words) or None)
        txt = " ".join(s.text.strip() for s in segs if s.no_speech_prob < 0.8).strip()
        return "" if self.HALLU.search(txt) else txt


# ---------------- voix : Piper ----------------

class Voice:
    def __init__(self):
        from piper import PiperVoice, SynthesisConfig
        self.v = PiperVoice.load(str(VOICE))
        self.cfg = SynthesisConfig(length_scale=1.0)
        self.sr = self.v.config.sample_rate
        self.lex = Watched(LEXIQUE, parse_lexique)
        self.pcm("Bonjour.")
        log("piper prêt")

    def prononce(self, text):
        for pat, say in self.lex.get():
            text = pat.sub(say, text)
        return text

    def pcm(self, text):
        text = self.prononce(text)
        parts = [c.audio_int16_bytes for c in self.v.synthesize(text, self.cfg)]
        return b"".join(parts) + b"\0\0" * int(self.sr * 0.18)   # petit silence de fin de phrase


SPEAKABLE = [
    (re.compile(r"```.*?```", re.S), " "),
    (re.compile(r"\$\$.*?\$\$", re.S), " "),
    (re.compile(r"\$[^$]*\$"), " "),
    (re.compile(r"`[^`]*`"), " "),
    (re.compile(r"https?://\S+"), " "),
    (re.compile(r"[*_#>|`~]+"), " "),
    (re.compile(r"^\s*[-•]\s+", re.M), ""),
    (re.compile(r"\s+"), " "),
]


def speakable(t):
    for r, s in SPEAKABLE:
        t = r.sub(s, t)
    return t.strip()


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
        self.buf, self.idx = "", 0
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

    # --- événements du cerveau ---
    async def on_brain(self, kind, gen, data):
        if gen != self.gen:
            return                    # vieille génération (interrompue) : on ignore
        if kind == "text":
            if "first_text" not in self.t:
                self.t["first_text"] = time.time()
            self.buf += data
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
            pcm = await loop.run_in_executor(None, self.voice.pcm, text)
            if gen != self.gen:
                continue
            if idx == 0:
                self.t["first_audio"] = time.time()
                tt = self.t
                log(f"latence serveur : stt {tt['stt'] - tt['t0']:.2f} s | 1er mot Claude "
                    f"{tt.get('first_text', 0) - tt['stt']:.2f} s | 1re phrase synthétisée "
                    f"{tt['first_audio'] - tt['t0']:.2f} s après réception (piper {time.time() - t:.2f} s)")
            self.history.append(("prof", text))
            await self.send({"t": "say", "gen": gen, "idx": idx, "text": text, "sr": self.voice.sr,
                             "pcm": base64.b64encode(pcm).decode()})

    def keep_audio(self, audio, text):
        """Garde chaque segment + sa transcription (corpus pour un futur fine-tuning de Whisper sur sa voix)."""
        import wave
        d = SESS / self.a.slug / "audio"
        d.mkdir(parents=True, exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        with wave.open(str(d / f"{name}.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        with open(d / "transcriptions.jsonl", "a") as f:
            f.write(json.dumps({"audio": f"{name}.wav", "text": text, "whisper": text, "corrige": None},
                               ensure_ascii=False) + "\n")

    # --- websocket ---
    async def handle_ws(self, req):
        ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024, heartbeat=20)
        await ws.prepare(req)
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
                         "history": self.history[-12:]})
        await self.state("listening")
        loop = asyncio.get_running_loop()
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                t0 = time.time()
                audio = np.frombuffer(msg.data, np.float32)
                log(f"segment reçu : {len(audio) / 16000:.1f} s, niveau max {float(np.abs(audio).max()) if len(audio) else 0:.2f}")
                await self.state("transcribing")
                text = await loop.run_in_executor(None, self.ears.transcribe, audio)
                log(f"entendu ({len(audio) / 16000:.1f} s d'audio, stt {time.time() - t0:.2f} s) : {text!r}")
                loop.run_in_executor(None, self.keep_audio, audio, text)
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
                elif j["t"] == "barge":
                    # l'élève parle : on coupe la génération en cours (l'audio est déjà coupé côté page)
                    self.interrupted = {"last": j.get("last") or ""}
                    log("interruption ; dernière phrase entendue :", repr(j.get("last")))
                    self.gen += 1      # ignore tout ce qui arrive encore de l'ancienne génération
                    await self.brain.interrupt()
                elif j["t"] == "text":
                    await self.send({"t": "user", "text": j["text"]})
                    await self.question(j["text"])
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
