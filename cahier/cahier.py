#!/usr/bin/env python3
"""Cahier d'apprentissage : un cahier par conversation, affiché dans le navigateur.

Données : ~/.claude/cahier/sessions/<slug>/cahier.json
Viewer  : http://localhost:8777/s/<slug>  (se met à jour tout seul)

Commandes :
  cahier.py new <slug> "<titre>"
  cahier.py add <slug> <id> "<titre>" --kind text|schema|derivation|animation
                [--step "<étape de la frise>"] [--file <chemin> | stdin]
  cahier.py focus <slug> <id>              # 1 carte en grand au centre, les autres en vignettes
  cahier.py duo <slug> <id1> <id2> [<id3> <id4>]   # 2 à 4 cartes côte à côte, les autres en vignettes
  cahier.py show <slug> <id> [<id>...]     # remplace les cartes ouvertes (1 id = focus, 2 à 4 au centre)
  cahier.py mark <slug> <id> [--ligne N] [--texte "0.56"] [--add]   # encadre / surligne ce qu'on cite
  cahier.py mark <slug> --clear
  cahier.py pin <slug> <id>... / unpin <slug> <id>...
  cahier.py replay <slug> <id>             # relance une animation
  cahier.py add ... --kind image --file photo.jpg   # photo / capture (copiée dans le cahier)
  cahier.py add ... --kind demo            # démo parlée : JSON {"anim": html?, "steps": [{"say", "show", "pause"}]}
  cahier.py play <slug> <id> [start|next|prev|repeat|pause]   # pilote une démo
  cahier.py selection <slug> [--clear]     # ce que l'élève a sélectionné au tableau
  cahier.py step <slug> <id> <k>          # amène une animation à son étape k (postMessage {step: k})
  cahier.py say "<texte>"                  # teste la voix
  cahier.py rm <slug> <id>
  cahier.py state <slug>                   # ce qui est affiché
  cahier.py list | open <slug> | serve
"""
import json, os, sys, time, argparse, subprocess, urllib.request, urllib.parse, webbrowser, hashlib, shutil
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent           # code (dépôt Prof-vocal) : cahier.py, viewer.html
ROOT = Path(os.environ.get("CAHIER_HOME", Path.home() / ".claude" / "cahier"))   # données : séances, voix, cache
SESS = ROOT / "sessions"
PORT = int(os.environ.get("CAHIER_PORT", 8777))
URL = f"http://localhost:{PORT}"
VOICE = ROOT / "voices" / "fr_FR-siwis-medium.onnx"
TTS = ROOT / "tts_cache"
PIPER = shutil.which("piper") or str(Path.home() / ".local/bin/piper")


def prononce(text):
    """Applique prononciation.json (ex. « gradient » que Piper lit comme un verbe)."""
    import re
    lex = json.loads((ROOT / "prononciation.json").read_text()) if (ROOT / "prononciation.json").exists() else {}
    for w, say in sorted(lex.items(), key=lambda kv: -len(kv[0])):
        if not w.startswith("_"):
            text = re.sub(rf"\b{re.escape(w)}\b", say, text, flags=re.IGNORECASE)
    return text


def tts(text):
    """Synthétise le texte avec Piper (mis en cache) et renvoie le chemin du wav."""
    text = prononce(text)
    TTS.mkdir(exist_ok=True)
    f = TTS / (hashlib.sha1(text.encode()).hexdigest()[:16] + ".wav")
    if not f.exists():
        tmp = f.with_suffix(".tmp.wav")
        subprocess.run([PIPER, "-m", str(VOICE), "-f", str(tmp), "--sentence-silence", "0.35"],
                       input=text.encode(), check=True, capture_output=True)
        os.replace(tmp, f)
    return f


def path(slug):
    return SESS / slug / "cahier.json"


def load(slug):
    p = path(slug)
    if not p.exists():
        sys.exit(f"cahier inconnu : {slug} (cahier.py new {slug} \"titre\")")
    return json.loads(p.read_text())


def save(slug, data):
    data["version"] = data.get("version", 0) + 1
    data["updated"] = time.time()
    p = path(slug)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


def marks_txt(st):
    """Encadrés en cours, lisibles par l'agent."""
    ms = st.get("marks", [])
    if not ms:
        return "encadrés : aucun"
    f = lambda m: f"{m['id']}" + (f" ligne {m['ligne']}" if m.get("ligne") else "") + (f" texte « {m['texte']} »" if m.get("texte") else "")
    return "encadrés : " + " ; ".join(f(m) for m in ms)


def sel_txt(st):
    """Ce que l'élève a sélectionné au tableau (pour guider le prof)."""
    s = st.get("selection")
    if not s:
        return "sélection de l'élève : aucune"
    return (f"sélection de l'élève : carte {s['id']}" + (f", ligne {s['ligne']}" if s.get("ligne") else "")
            + (f" : formule (LaTeX : {s['tex']})" if s.get("tex") else f" : « {s['texte']} »"))


def server_up():
    try:
        urllib.request.urlopen(URL + "/api/list", timeout=0.5)
        return True
    except Exception:
        return False


def ensure_server():
    if server_up():
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    log = open(ROOT / "server.log", "a")
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve"],
                     stdout=log, stderr=log, start_new_session=True)
    for _ in range(30):
        time.sleep(0.1)
        if server_up():
            return


# ---------- serveur ----------

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/" or p.startswith("/s/"):
            return self.send(200, (HERE / "viewer.html").read_text(), "text/html")
        if p == "/api/list":
            out = []
            for d in sorted(SESS.glob("*/cahier.json"), key=lambda f: -f.stat().st_mtime):
                j = json.loads(d.read_text())
                out.append({"slug": d.parent.name, "title": j.get("title"),
                            "cards": len(j.get("cards", [])), "updated": j.get("updated")})
            return self.send(200, json.dumps(out))
        if p == "/api/tts":
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            try:
                return self.send(200, tts(q.get("t", [""])[0]).read_bytes(), "audio/wav")
            except Exception as e:
                return self.send(500, str(e), "text/plain")
        if p.startswith("/files/"):          # images des cartes : /files/<slug>/<nom>
            parts = p.split("/")
            if len(parts) == 4 and ".." not in parts[2] + parts[3]:
                f = SESS / urllib.parse.unquote(parts[2]) / "files" / urllib.parse.unquote(parts[3])
                if f.is_file():
                    import mimetypes
                    return self.send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            return self.send(404, "not found", "text/plain")
        if p.startswith("/api/s/"):
            f = path(p[len("/api/s/"):])
            if not f.exists():
                return self.send(404, "{}")
            # ?v=N : attente longue, on répond dès que la version change (ou au bout de 25 s) ;
            # le tableau suit ainsi la voix sans délai, sans interroger en boucle
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            txt = f.read_text()
            if "v" in q:
                try:
                    v = int(q["v"][0])
                except ValueError:
                    v = None
                end = time.time() + 25
                mt = f.stat().st_mtime_ns
                cur = json.loads(txt).get("version")
                while v is not None and cur == v and time.time() < end:
                    time.sleep(0.03)
                    try:
                        m2 = f.stat().st_mtime_ns
                        if m2 != mt:
                            mt, txt = m2, f.read_text()
                            cur = json.loads(txt).get("version")
                    except (OSError, ValueError):
                        pass
            return self.send(200, txt)
        self.send(404, "not found", "text/plain")

    def do_POST(self):
        # le viewer renvoie l'état quand l'utilisateur clique (épingler, retirer)
        p = self.path
        if p.startswith("/api/s/") and p.endswith("/state"):
            slug = p[len("/api/s/"):-len("/state")]
            n = int(self.headers.get("Content-Length", 0))
            st = json.loads(self.rfile.read(n))
            data = load(slug)
            for k in ("pinned", "layout", "main", "selection"):
                if k in st:
                    data["state"][k] = st[k]
            save(slug, data)
            return self.send(200, json.dumps({"version": data["version"]}))
        self.send(404, "{}")


def reload_on_change():
    """Le serveur du tableau tourne à part (Whiteboard ne le relance pas) : quand cahier.py change (mise à jour
    du dépôt), il se relance tout seul avec le nouveau code. Le tableau se reconnecte de lui-même."""
    me = Path(__file__).resolve()
    t0 = me.stat().st_mtime_ns
    while True:
        time.sleep(2)
        try:
            if me.stat().st_mtime_ns != t0:
                time.sleep(0.5)                       # laisse finir l'écriture du fichier
                compile(me.read_text(), str(me), "exec")   # code cassé : on garde l'ancien serveur
                print(time.strftime("%H:%M:%S"), "cahier.py modifié : le serveur se relance", file=sys.stderr, flush=True)
                os.execv(sys.executable, [sys.executable, str(me), "serve"])
        except SyntaxError:
            t0 = me.stat().st_mtime_ns
        except OSError:
            pass


# ---------- CLI ----------

def main(argv=None, out=print):
    """argv : arguments (sinon la ligne de commande) ; out : où écrire les réponses (le serveur du prof
    appelle main directement, sans relancer Python à chaque repère)."""
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("new"); s.add_argument("slug"); s.add_argument("title")
    s = sub.add_parser("add"); s.add_argument("slug"); s.add_argument("id"); s.add_argument("title")
    s.add_argument("--kind", default="text", choices=["text", "schema", "derivation", "animation", "demo", "image"])
    s.add_argument("--step", default=None); s.add_argument("--file", default=None)
    s.add_argument("--no-show", action="store_true", help="ne pas épingler la carte")
    for c in ("show", "pin", "unpin", "rm", "duo"):
        s = sub.add_parser(c); s.add_argument("slug"); s.add_argument("ids", nargs="+")
    s = sub.add_parser("replay"); s.add_argument("slug"); s.add_argument("id")
    s = sub.add_parser("focus"); s.add_argument("slug"); s.add_argument("id")
    s = sub.add_parser("mark"); s.add_argument("slug"); s.add_argument("id", nargs="?")
    s.add_argument("--ligne", type=int); s.add_argument("--texte"); s.add_argument("--add", action="store_true")
    s.add_argument("--clear", action="store_true")
    s.add_argument("--list", action="store_true", help="affiche les encadrés en cours")
    s = sub.add_parser("layout"); s.add_argument("slug"); s.add_argument("mode", choices=["focus", "duo"])
    s = sub.add_parser("play"); s.add_argument("slug"); s.add_argument("id")
    s.add_argument("action", nargs="?", default="start", choices=["start", "next", "prev", "repeat", "pause"])
    s = sub.add_parser("say"); s.add_argument("text")
    s = sub.add_parser("selection"); s.add_argument("slug"); s.add_argument("--clear", action="store_true")
    s = sub.add_parser("step"); s.add_argument("slug"); s.add_argument("id"); s.add_argument("k", type=int)
    s = sub.add_parser("state"); s.add_argument("slug")
    s = sub.add_parser("open"); s.add_argument("slug")
    sub.add_parser("list"); sub.add_parser("serve")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        SESS.mkdir(parents=True, exist_ok=True)
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
        import threading
        threading.Thread(target=reload_on_change, daemon=True).start()
        srv.serve_forever()
        return

    if a.cmd == "say":
        f = tts(a.text)
        subprocess.run(["paplay", str(f)]) if shutil.which("paplay") else out(f)
        return

    if a.cmd == "list":
        for d in sorted(SESS.glob("*/cahier.json")):
            j = json.loads(d.read_text())
            out(f"{d.parent.name}\t{j['title']}\t{len(j['cards'])} cartes")
        return

    if a.cmd == "new":
        (SESS / a.slug).mkdir(parents=True, exist_ok=True)
        if path(a.slug).exists():
            out("existe déjà");
        else:
            save(a.slug, {"title": a.title, "created": time.time(), "cards": [],
                          "state": {"pinned": [], "replay": {}}})
        ensure_server()
        out(f"{URL}/s/{a.slug}")
        return

    if a.cmd == "open":
        ensure_server()
        webbrowser.open(f"{URL}/s/{a.slug}")
        out(f"{URL}/s/{a.slug}")
        return

    data = load(a.slug)
    st = data["state"]
    st.setdefault("layout", "focus")
    st.setdefault("main", st["pinned"][-1:])
    ids = {c["id"] for c in data["cards"]}

    def open_(i):
        if i not in st["pinned"]:
            st["pinned"].append(i)

    def focus(i):
        open_(i); st["layout"] = "focus"; st["main"] = [i]

    if a.cmd in ("focus", "duo", "show", "add"):   # on change de sujet : les encadrés d'avant disparaissent
        st["marks"] = []

    if a.cmd == "add":
        if a.kind == "image":       # photo / capture : copiée dans le dossier du cahier, servie par /files/
            if not a.file:
                sys.exit("--kind image demande --file <photo>")
            src = Path(a.file); dst = SESS / a.slug / "files"; dst.mkdir(exist_ok=True)
            name = f"{a.id}-{int(time.time())}{src.suffix.lower() or '.png'}"
            shutil.copyfile(src, dst / name)
            raw = name
        else:
            raw = Path(a.file).read_text() if a.file else sys.stdin.read()
        card = {"id": a.id, "title": a.title, "kind": a.kind, "t": time.time()}
        if a.kind == "derivation":
            card["lines"] = json.loads(raw)  # [{"l": "latex", "why": "texte"}]
        elif a.kind == "demo":
            card["demo"] = json.loads(raw)
            for st_ in card["demo"]["steps"]:   # pré-génère l'audio pour éviter l'attente
                if st_.get("say"):
                    tts(st_["say"])
        else:
            card["body"] = raw
        old = next((c for c in data["cards"] if c["id"] == a.id), None)
        prefix = a.id.split(".")[0]
        sibling = next((c["step"] for c in data["cards"] if c["id"].split(".")[0] == prefix), None)
        card["step"] = a.step or (old or {}).get("step") or sibling or prefix
        if old:
            data["cards"][data["cards"].index(old)] = card
        else:
            data["cards"].append(card)
        if not a.no_show:
            focus(a.id)
    elif a.cmd == "selection":
        if a.clear:
            st["selection"] = None
        else:
            out(sel_txt(st)); return
    elif a.cmd == "mark" and a.list:
        out(marks_txt(st))
        out(sel_txt(st)); return
    elif a.cmd == "mark":
        if a.clear:      # avec un id : efface seulement les encadrés de cette carte
            st["marks"] = [m for m in st.get("marks", []) if a.id and m["id"] != a.id]
        else:
            if a.id not in ids:
                sys.exit(f"carte inconnue : {a.id} ; existantes : {sorted(ids)}")
            if a.ligne is None and not a.texte:
                sys.exit("mark : donne --ligne N et/ou --texte \"...\"")
            m = {"id": a.id, "ligne": a.ligne, "texte": a.texte}
            st["marks"] = (st.get("marks", []) if a.add else []) + [m]
            if a.id not in st["main"]:         # on ne cite pas une carte cachée
                if st["layout"] == "duo" and len(st["main"]) < 4:
                    st["main"].append(a.id); open_(a.id)
                else:
                    focus(a.id)
    elif a.cmd == "focus":
        if a.id not in ids:
            sys.exit(f"carte inconnue : {a.id} ; existantes : {sorted(ids)}")
        focus(a.id)
    elif a.cmd == "layout":
        st["layout"] = a.mode
        if a.mode == "duo" and len(st["main"]) < 2:
            other = [i for i in reversed(st["pinned"]) if i not in st["main"]][:2 - len(st["main"])]
            st["main"] = st["main"] + other
        if a.mode == "focus":
            st["main"] = st["main"][-1:]
    elif a.cmd in ("show", "pin", "unpin", "rm", "duo"):
        bad = [i for i in a.ids if i not in ids]
        if bad:
            sys.exit(f"cartes inconnues : {bad} ; existantes : {sorted(ids)}")
        if a.cmd == "duo":
            if not 2 <= len(a.ids) <= 4:
                sys.exit("duo prend 2 à 4 cartes")
            for i in a.ids:
                open_(i)
            st["layout"] = "duo"; st["main"] = list(a.ids)
        elif a.cmd == "show":
            st["pinned"] = list(a.ids)
            st["layout"] = "focus" if len(a.ids) == 1 else "duo"
            st["main"] = list(a.ids[:4])
        elif a.cmd == "pin":
            st["pinned"] += [i for i in a.ids if i not in st["pinned"]]
        elif a.cmd == "unpin":
            st["pinned"] = [i for i in st["pinned"] if i not in a.ids]
            st["main"] = [i for i in st["main"] if i not in a.ids]
        else:
            data["cards"] = [c for c in data["cards"] if c["id"] not in a.ids]
            st["pinned"] = [i for i in st["pinned"] if i not in a.ids]
            st["main"] = [i for i in st["main"] if i not in a.ids]
        if a.cmd == "pin" and not st["main"]:
            st["main"] = [a.ids[0]]
        if not st["main"] and st["pinned"]:
            st["main"] = st["pinned"][-1:]
    elif a.cmd == "replay":
        st["replay"][a.id] = st["replay"].get(a.id, 0) + 1
        if a.id not in st["main"]:
            focus(a.id)
    elif a.cmd == "play":
        if a.id not in ids:
            sys.exit(f"carte inconnue : {a.id}")
        n = st.get("cmd", {}).get("n", 0) + 1
        st["cmd"] = {"id": a.id, "action": a.action, "n": n}
        if a.id not in st["main"]:
            focus(a.id)
    elif a.cmd == "step":
        if a.id not in ids:
            sys.exit(f"carte inconnue : {a.id}")
        n = st.get("cmd", {}).get("n", 0) + 1
        st["cmd"] = {"id": a.id, "action": "step", "k": a.k, "n": n}
        if a.id not in st["main"]:
            focus(a.id)
    elif a.cmd == "state":
        out(f"mise en page : {st['layout']} ; au centre : {st['main']} ; ouvertes : {st['pinned']}")
        out(marks_txt(st))
        for c in data["cards"]:
            out(f"  {c['id']}\t[{c['kind']}]\t{c['title']}\t(étape {c['step']})")
        return

    save(a.slug, data)
    ensure_server()
    out(f"ok ; {st['layout']} : {st['main']} ; ouvertes : {st['pinned']} ; {marks_txt(st)}")


if __name__ == "__main__":
    main()
