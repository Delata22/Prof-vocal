#!/usr/bin/env python3
"""Whiteboard — le Prof vocal en application de bureau (GTK + WebKit) : fenêtre native, plusieurs boards.

  python3 whiteboard.py            # lance l'app (Python système : GTK 3 + WebKit2 4.1)
  python3 install_desktop.py       # entrée « Whiteboard » dans le menu des applications

L'accueil liste les boards (les cahiers de ~/.claude/cahier) ; « Reprendre » ou « Nouveau board » démarre le
serveur du Prof de ce board (prof_server.py, dans .venv : Whisper, Piper, Claude) et y fait naviguer la
fenêtre. Un seul Prof à la fois (le GPU). Le cahier (http://localhost:8777) reste le tableau.
prof.sh reste disponible pour le débogage, sur un autre port (8778).

Pour Regulator : WM_CLASS « Whiteboard », processus « whiteboard », et le Prof écrit
~/.local/state/regulator/activity/whiteboard.json pendant qu'il parle ou que tu enregistres.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2  # noqa: E402

ROOT = Path(__file__).resolve().parent
APP_ID = "com.delata.Whiteboard"
WM_CLASS = "Whiteboard"
TITLE = "Whiteboard"
ICON = ROOT / "assets" / "whiteboard.svg"
VENV_PY = ROOT / ".venv/bin/python"
CAHIER = Path.home() / ".claude/cahier/cahier.py"
CAHIERS = Path.home() / ".claude/cahier/sessions"
STATE = Path(GLib.get_user_state_dir()) / "whiteboard"
CONFIG = Path(GLib.get_user_config_dir()) / "whiteboard"
DATA_DIR = Path(GLib.get_user_data_dir()) / "whiteboard"
ACTIVITY = Path.home() / ".local/state/regulator/activity/whiteboard.json"
HUB_PORTS = range(8789, 8795)
PROF_PORT = 8790


# ---------------------------------------------------------------- boards et Prof
def slugify(topic: str) -> str:
    s = unicodedata.normalize("NFKD", topic).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "board"
    base, k = s, 2
    while (CAHIERS / s).exists():
        s, k = f"{base}-{k}", k + 1
    return s


def boards() -> list[dict]:
    out = []
    for f in CAHIERS.glob("*/cahier.json"):
        try:
            j = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        slug = f.parent.name
        prof = ROOT / "sessions" / slug / "prof.json"
        last = max(f.stat().st_mtime, prof.stat().st_mtime if prof.exists() else 0)
        out.append({"slug": slug, "title": j.get("title") or slug, "cards": len(j.get("cards", [])),
                    "updated": last, "prof": prof.exists()})
    return sorted(out, key=lambda b: -b["updated"])


class Prof:
    """Le serveur du Prof du board ouvert (un seul à la fois)."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.slug: str | None = None
        self.log: Path | None = None
        self.lock = threading.Lock()

    def url(self, hub: str) -> str:
        return f"http://127.0.0.1:{PROF_PORT}/?hub={urllib.request.quote(hub, safe='')}"

    def ready(self) -> bool:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PROF_PORT}/", timeout=0.5)
            return True
        except Exception:
            return False

    def start(self, slug: str, topic: str) -> None:
        with self.lock:
            if self.slug == slug and self.proc and self.proc.poll() is None:
                return
            self.stop()
            subprocess.run([sys.executable, str(CAHIER), "new", slug, topic], capture_output=True)  # + serveur 8777
            STATE.mkdir(parents=True, exist_ok=True)
            self.log = STATE / f"prof-{slug}.log"
            env = dict(os.environ, PROF_ACTIVITY=str(ACTIVITY), PROF_NO_OPEN="1")
            self.proc = subprocess.Popen(
                [str(VENV_PY), str(ROOT / "prof_server.py"), slug, topic, "--port", str(PROF_PORT)],
                cwd=ROOT, env=env, stdout=open(self.log, "w"), stderr=subprocess.STDOUT, start_new_session=True)
            self.slug = slug

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)    # le serveur et ses `claude`
                self.proc.wait(timeout=15)       # le Prof remet le casque en musique (~4 s)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.proc, self.slug = None, None
        write_activity(False)

    def status(self) -> dict:
        alive = bool(self.proc and self.proc.poll() is None)
        tail = ""
        if self.log and self.log.exists():
            lines = [l for l in self.log.read_text(errors="replace").splitlines() if "Warning" not in l]
            tail = "\n".join(lines[-6:])
        return {"slug": self.slug, "alive": alive, "ready": alive and self.ready(), "log": tail,
                "code": None if alive or not self.proc else self.proc.returncode}


def write_activity(playing: bool) -> None:
    try:
        ACTIVITY.parent.mkdir(parents=True, exist_ok=True)
        ACTIVITY.write_text(json.dumps({"app": "whiteboard", "playing": playing, "ts": time.time()}))
    except OSError:
        pass


PROF = Prof()


class Hub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    @property
    def hub(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/"

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            return self.send(200, (ROOT / "home.html").read_text(), "text/html")
        if p == "/icon.svg" and ICON.exists():
            return self.send(200, ICON.read_bytes(), "image/svg+xml")
        if p == "/api/boards":
            return self.send(200, json.dumps({"boards": boards(), "open": PROF.slug}))
        if p == "/api/prof":
            st = PROF.status()
            st["url"] = PROF.url(self.hub)
            return self.send(200, json.dumps(st))
        self.send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        j = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/open":
            b = next((b for b in boards() if b["slug"] == j.get("slug")), None)
            if not b:
                return self.send(404, json.dumps({"error": "board inconnu"}))
            threading.Thread(target=PROF.start, args=(b["slug"], b["title"]), daemon=True).start()
            return self.send(200, json.dumps({"slug": b["slug"]}))
        if self.path == "/api/new":
            topic = (j.get("topic") or "").strip()
            if not topic:
                return self.send(400, json.dumps({"error": "sujet vide"}))
            slug = slugify(j.get("slug") or topic)
            threading.Thread(target=PROF.start, args=(slug, topic), daemon=True).start()
            return self.send(200, json.dumps({"slug": slug}))
        if self.path == "/api/stop":
            PROF.stop()
            return self.send(200, "{}")
        self.send(404, "{}")


# ---------------------------------------------------------------- fenêtre
class WhiteboardApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.window = self.view = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        # sous X11, WM_CLASS vient du nom du programme : « Whiteboard » (Regulator, StartupWMClass du .desktop)
        GLib.set_prgname(WM_CLASS)
        Gdk.set_program_class(WM_CLASS)
        GLib.set_application_name(TITLE)
        if ICON.exists():
            try:
                Gtk.Window.set_default_icon_from_file(str(ICON))
            except GLib.Error:
                pass
        for port in (*HUB_PORTS, 0):
            try:
                self.server = ThreadingHTTPServer(("127.0.0.1", port), Hub)
                break
            except OSError:
                continue
        self.hub = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda *_: (self.quit(), False)[1])
        for name, cb, accels in [
            ("reload", lambda *_: self.reload_resume(self.view), ["<Primary>r", "F5"]),
            ("home", lambda *_: self.view.load_uri(self.hub + "?stop=1"), ["<Primary>h"]),   # arrête le prof du tableau
            ("quit", lambda *_: self.quit(), ["<Primary>q"]),
            ("fullscreen", self.toggle_fullscreen, ["F11"]),
            ("inspector", lambda *_: self.view.get_inspector().show(), ["<Primary><Shift>i"]),
        ]:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
            self.set_accels_for_action(f"app.{name}", accels)

    def do_activate(self):
        if self.window:
            return self.window.present()
        window = Gtk.ApplicationWindow(application=self, title=TITLE)
        w, h, maximized = self.load_window_state()
        window.set_default_size(w, h)
        if maximized:
            window.maximize()
        window.connect("delete-event", self.on_close)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        manager = WebKit2.WebsiteDataManager(base_data_directory=str(DATA_DIR),
                                             base_cache_directory=str(DATA_DIR / "cache"))
        context = WebKit2.WebContext.new_with_website_data_manager(manager)
        view = WebKit2.WebView(web_context=context)
        s = view.get_settings()
        s.set_enable_developer_extras(True)
        s.set_enable_media_stream(True)              # micro (getUserMedia)
        s.set_enable_webaudio(True)
        s.set_enable_mediasource(True)
        s.set_media_playback_requires_user_gesture(False)
        s.set_javascript_can_access_clipboard(True)
        view.connect("permission-request", self.on_permission)
        view.connect("decide-policy", self.on_policy)
        view.connect("notify::title", lambda v, _p: window.set_title(v.get_title() or TITLE))
        view.connect("web-process-terminated", self.on_web_crash)
        self.probe = []                              # sonde temporaire des plantages silencieux (voir probe_sample)
        GLib.timeout_add_seconds(2, self.probe_sample)
        window.connect("notify::is-active", lambda w, _p: self.probe_event("fenêtre " + ("active" if w.is_active() else "inactive")))
        window.connect("window-state-event", lambda w, e: self.probe_event(
            "fenêtre " + ("réduite" if e.new_window_state & Gdk.WindowState.ICONIFIED else "affichée"))
            if e.changed_mask & Gdk.WindowState.ICONIFIED else None)
        # Glisser-déposer d'un fichier : WebKitGTK passe à la page un dépôt VIDE (types sans données, constaté le
        # 2026-10-23). On l'intercepte donc au niveau GTK : on demande la liste d'URI nous-mêmes et on la donne à la page,
        # qui l'envoie au serveur ({t: "drop_paths"}) comme un chemin collé.
        view.connect("drag-drop", self.on_drag_drop)
        view.connect("drag-data-received", self.on_drag_data)
        # Rendu logiciel par défaut : les plantages du rendu (« crashed », ~0,3 s après un add/show au tableau)
        # pointent vers le compositing GPU de WebKitGTK (PC hybride Intel + NVIDIA). WHITEBOARD_GPU=1 pour le réactiver.
        if os.environ.get("WHITEBOARD_GPU") != "1":
            s.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.NEVER)
        view.load_uri(self.hub)
        window.add(view)
        self.window, self.view = window, view
        window.show_all()

    def on_drag_drop(self, view, ctx, x, y, t):
        targets = [str(a) for a in ctx.list_targets()]
        print(f"{time.strftime('%T')} GTK drag-drop : cibles {targets}", file=sys.stderr, flush=True)
        if "text/uri-list" not in targets:
            return False                              # un texte glissé : WebKit s'en occupe
        self._our_drop = True
        view.drag_get_data(ctx, Gdk.Atom.intern("text/uri-list", False), t)
        view.stop_emission_by_name("drag-drop")
        return True

    def on_drag_data(self, view, ctx, x, y, data, info, t):
        if not getattr(self, "_our_drop", False):
            return
        self._our_drop = False
        view.stop_emission_by_name("drag-data-received")
        uris = list(data.get_uris() or [])
        if not uris:                                  # certaines sources ne donnent que du texte
            uris = [u.strip() for u in (data.get_text() or "").splitlines() if u.strip() and not u.startswith("#")]
        paths = []
        for u in uris:
            f = Gio.File.new_for_uri(u) if "://" in u else Gio.File.new_for_path(u)
            if f.get_path():
                paths.append(f.get_path())
        print(f"{time.strftime('%T')} GTK dépôt : {paths or uris}", file=sys.stderr, flush=True)
        Gtk.drag_finish(ctx, bool(paths), False, t)
        if paths:
            js = ("typeof send === 'function' ? (send({t: 'drop_paths', paths: %s}),"
                  " (document.getElementById('sub') || {}).textContent = '📎 je lis l\u2019image…') : null") % json.dumps(paths)
            view.run_javascript(js, None, None, None)

    # --- Sonde TEMPORAIRE (2026-11-10) : le rendu meurt sans message ni rapport apport juste après l'arrivée d'une
    # carte. Toutes les 2 s, l'état du processus de rendu ; au plantage, la dernière minute et son code de sortie
    # vont dans crashes.log. À RETIRER une fois la cause trouvée (avec l'appel dans __init__ et dans on_web_crash).
    def probe_pids(self):
        out = []
        for t in Path(f"/proc/{os.getpid()}/task").iterdir():
            try:
                kids = (t / "children").read_text().split()
            except OSError:
                continue
            for k in kids:
                try:
                    if Path(f"/proc/{k}/comm").read_text().startswith("WebKitWebProc"):
                        out.append(int(k))
                except OSError:
                    pass
        return out

    @staticmethod
    def probe_stat(pid):
        """(état, cpu en ticks, code de sortie) d'après /proc/<pid>/stat ; code de sortie lisible pour un zombie."""
        st = Path(f"/proc/{pid}/stat").read_text()
        f = st[st.rindex(")") + 2:].split()             # champs 3 et suivants
        return f[0], int(f[11]) + int(f[12]), int(f[49]) if len(f) > 49 else None

    def probe_sample(self):
        try:
            now = time.time()
            mem = Path("/proc/meminfo").read_text()
            avail = int(re.search(r"MemAvailable:\s+(\d+)", mem).group(1)) / 1048576
            for pid in self.probe_pids():
                try:
                    status = Path(f"/proc/{pid}/status").read_text()
                    g = lambda k: int(re.search(rf"{k}:\s+(\d+)", status).group(1))
                    state, cpu, _ = self.probe_stat(pid)
                    nfd = len(os.listdir(f"/proc/{pid}/fd"))
                except (OSError, AttributeError, ValueError):
                    continue
                prev = next((x for x in reversed(self.probe) if x["pid"] == pid), None)
                pct = 100 * (cpu - prev["cpu"]) / os.sysconf("SC_CLK_TCK") / (now - prev["t"]) if prev else 0
                self.probe.append({"t": now, "pid": pid, "cpu": cpu, "line":
                                   f"{time.strftime('%T', time.localtime(now))} pid {pid} {state} "
                                   f"rss {g('VmRSS') // 1024} Mo (max {g('VmHWM') // 1024}) threads {g('Threads')} "
                                   f"fd {nfd} cpu {pct:.0f} % ; mémoire dispo {avail:.1f} Go"})
            self.probe = self.probe[-40:]
        except Exception as e:                          # la sonde ne doit jamais gêner l'app
            print("sonde :", e, file=sys.stderr)
        return True

    def probe_event(self, what):
        """Événement de fenêtre (premier plan, réduite) intercalé dans la sonde : le plantage les suit-il ?"""
        self.probe.append({"t": time.time(), "pid": None, "cpu": 0,
                           "line": f"{time.strftime('%T')}.{int(time.time() * 1000) % 1000:03d} {what}"})
        self.probe = self.probe[-40:]
        return False

    def probe_dump(self):
        """Au plantage : code de sortie du processus mort (s'il n'est pas encore réclamé) et la dernière minute."""
        lines = []
        for pid in dict.fromkeys(x["pid"] for x in self.probe[-8:] if x["pid"]):
            try:
                state, _, code = self.probe_stat(pid)
                how = ("tué par le signal %d" % (code & 0x7f) if code and code & 0x7f else
                       "sorti avec le code %d" % (code >> 8) if code is not None else "?")
                lines.append(f"pid {pid} : état {state}, {how} (statut brut {code})")
            except (OSError, ValueError):
                lines.append(f"pid {pid} : déjà disparu de /proc")
        lines += [x["line"] for x in self.probe[-30:]]
        try:
            with open(STATE / "crashes.log", "a") as f:
                f.write("    sonde (2 s) :\n" + "".join(f"      {l}\n" for l in lines))
        except OSError:
            pass

    def on_web_crash(self, view, reason):
        """Le processus de rendu est mort (écran noir) : on note le motif et le contexte, puis on recharge
        (au plus 3 fois par minute, sinon une page d'erreur au lieu d'une boucle)."""
        now = time.time()
        self.crashes = [t for t in getattr(self, "crashes", []) if now - t < 60] + [now]
        try:
            name = WebKit2.WebProcessTerminationReason(reason).value_nick
        except Exception:
            name = str(reason)
        last = ""
        if PROF.log and PROF.log.exists():          # dernières commandes du tableau, pour retrouver la carte en cause
            lines = [l for l in PROF.log.read_text(errors="replace").splitlines() if "outil" in l or "repère" in l]
            last = " | ".join(l.strip()[:160] for l in lines[-3:])
        gpu = "GPU" if os.environ.get("WHITEBOARD_GPU") == "1" else "rendu logiciel"
        line = (f"{time.strftime('%F %T')} rendu WebKit terminé : {name} ({gpu}) ; page {view.get_uri()} ; "
                f"board {PROF.slug} ; dernières commandes : {last}")
        GLib.timeout_add_seconds(15, self.log_crash_report)   # apport écrit son rapport quelques secondes après
        print(line, file=sys.stderr, flush=True)
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            with open(STATE / "crashes.log", "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
        self.probe_dump()                           # sonde temporaire : tout de suite, avant que le mort soit réclamé
        uri = view.get_uri()                        # après le plantage, la vue repasse vite à about:blank
        if len(self.crashes) <= 3:
            GLib.timeout_add(500, lambda: (self.reload_resume(view, uri), False)[1])
        else:
            view.load_html(f"""<body style="font:16px system-ui;padding:40px;background:#1b1b1a;color:#ecebe6">
              <h2>L'affichage a planté {len(self.crashes)} fois en une minute ({name}).</h2>
              <p>Le motif est noté dans {STATE / 'crashes.log'}.</p>
              <p><a style="color:#85B7EB" href="{self.hub}">Revenir à l'accueil</a> · ou F5 pour recharger.</p></body>""", self.hub)
        return True

    def reload_resume(self, view, uri=None):
        """Recharge la page du Prof en reprenant la séance là où elle en était (?resume=1 : pas d'écran
        « Démarrer le cours » ; le serveur renvoie les phrases pas encore jouées)."""
        uri = uri or view.get_uri() or self.hub
        if uri.startswith("about:"):
            uri = self.hub
        if uri.startswith(f"http://127.0.0.1:{PROF_PORT}/") and "resume=1" not in uri:
            uri += ("&" if "?" in uri else "?") + "resume=1"
        view.load_uri(uri)

    def log_crash_report(self):
        """Signal du plantage, s'il y a un rapport apport récent (/var/crash) du processus de rendu.
        apport écrit le rapport quelques secondes après : on le cherche à 15 s (appelé par un timeout)."""
        try:
            reps = sorted(Path("/var/crash").glob("*WebKitWebProcess*.crash"), key=lambda f: f.stat().st_mtime)
            if reps and time.time() - reps[-1].stat().st_mtime < 120:
                head = reps[-1].read_text(errors="replace")[:20000]
                sig = re.search(r"^Signal: *(\S+)", head, re.M)
                title = re.search(r"^(?:Title|CrashCounter|ExecutablePath): *(.*)$", head, re.M)
                with open(STATE / "crashes.log", "a") as f:
                    f.write(f"    rapport apport {reps[-1].name} : signal {sig.group(1) if sig else '?'}"
                            f"{' ; ' + title.group(1) if title else ''}\n")
        except OSError:
            pass
        # journal WebKit (WEBKIT_DEBUG=Process, dans le journal utilisateur) des 40 dernières secondes :
        # l'interface et le processus de rendu y disent pourquoi il s'est arrêté
        try:
            out = subprocess.run(["journalctl", "--user", "--since", "-40s", "--no-pager", "-o", "short-precise"],
                                 capture_output=True, text=True, timeout=10).stdout.splitlines()
            keep = [l for l in out if f"[{os.getpid()}]" in l or "WebKitWebProcess" in l
                    or "WebKitNetworkProcess" in l or "WebKitGPUProcess" in l]
            with open(STATE / "crashes.log", "a") as f:
                f.write("    journal WebKit :\n" + "".join(f"      {l[:400]}\n" for l in keep[-60:]))
        except (OSError, subprocess.SubprocessError):
            pass
        return False

    def on_permission(self, view, request):
        # micro et caméra : seulement pour nos pages locales ; le reste (géolocalisation, notifications…) refusé
        uri = view.get_uri() or ""
        if isinstance(request, WebKit2.UserMediaPermissionRequest) and uri.startswith(("http://127.0.0.1:", "http://localhost:")):
            request.allow()
        else:
            request.deny()
        return True

    LOCAL = ("http://127.0.0.1:", "http://localhost:", "about:", "blob:", "data:")

    def on_policy(self, view, decision, decision_type):
        """Seules les pages locales (accueil, Prof, cahier) s'ouvrent ; les liens externes ne mènent nulle part."""
        if decision_type == WebKit2.PolicyDecisionType.RESPONSE:
            return False
        uri = decision.get_navigation_action().get_request().get_uri()
        if uri.startswith(self.LOCAL):
            if decision_type == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
                decision.ignore()
                view.load_uri(uri)
                return True
            return False
        print(f"[lien externe bloqué] {uri}", file=sys.stderr)
        decision.ignore()
        return True

    def toggle_fullscreen(self, *_):
        if self.window.get_window().get_state() & Gdk.WindowState.FULLSCREEN:
            self.window.unfullscreen()
        else:
            self.window.fullscreen()

    def on_close(self, window, _event):
        try:
            CONFIG.mkdir(parents=True, exist_ok=True)
            size = window.get_size()
            (CONFIG / "window.json").write_text(json.dumps(
                {"width": size.width, "height": size.height, "maximized": window.is_maximized()}))
        except OSError:
            pass
        return False

    def load_window_state(self):
        try:
            d = json.loads((CONFIG / "window.json").read_text())
            return int(d.get("width", 1500)), int(d.get("height", 950)), bool(d.get("maximized", True))
        except (OSError, ValueError):
            return 1500, 950, True

    def do_shutdown(self):
        PROF.stop()
        Gtk.Application.do_shutdown(self)


def set_process_name(name: str) -> None:
    try:
        import ctypes
        ctypes.CDLL(None).prctl(15, name.encode()[:15], 0, 0, 0)  # PR_SET_NAME
    except (OSError, AttributeError):
        pass


def log_to_file() -> None:
    """stdout / stderr de l'app ET des processus WebKit (ils héritent du descripteur 2) dans un fichier,
    pour avoir la trace d'un écran noir."""
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        f = open(STATE / "whiteboard.log", "a", buffering=1)
        f.write(f"\n===== {time.strftime('%F %T')} lancement (pid {os.getpid()}) =====\n")
        os.dup2(f.fileno(), 1)
        os.dup2(f.fileno(), 2)
    except OSError:
        pass


def main() -> int:
    set_process_name("whiteboard")
    # diagnostic des plantages du rendu : les processus WebKit héritent de cette limite ; apport peut alors
    # écrire un rapport (/var/crash, avec le signal). WHITEBOARD_DEBUG=1 : journal WebKit (processus, compositing).
    try:
        import resource
        _soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE, (hard, hard))
    except (ImportError, ValueError, OSError):
        pass
    if os.environ.get("WHITEBOARD_DEBUG") == "1":
        os.environ.setdefault("WEBKIT_DEBUG", "Process,Loading,Compositing,Layers")
    # Diagnostic des rechargements (2026-11-08) : le processus de rendu s'arrête sans rapport apport (donc
    # probablement tué, pas un signal de plantage). Le journal « Process » de WebKit (dans whiteboard.log)
    # dit pourquoi l'interface l'arrête. Léger ; à retirer une fois la cause trouvée.
    os.environ.setdefault("WEBKIT_DEBUG", "Process")
    if not sys.stderr.isatty():         # lancé depuis le menu : pas de terminal pour voir les erreurs
        log_to_file()
    return WhiteboardApp().run([WM_CLASS, *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
