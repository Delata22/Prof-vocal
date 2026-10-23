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
            ("reload", lambda *_: self.view.reload(), ["<Primary>r", "F5"]),
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
        # diagnostic du glisser-déposer au niveau GTK (journal whiteboard.log), sans l'intercepter
        view.connect("drag-drop", lambda w, ctx, x, y, t: (print(
            f"{time.strftime('%T')} GTK drag-drop : cibles {[str(a) for a in ctx.list_targets()]}", file=sys.stderr, flush=True), False)[1])
        if os.environ.get("WHITEBOARD_NO_GPU") == "1":     # piste si les écrans noirs viennent du rendu GPU
            s.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.NEVER)
        view.load_uri(self.hub)
        window.add(view)
        self.window, self.view = window, view
        window.show_all()

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
        line = (f"{time.strftime('%F %T')} rendu WebKit terminé : {name} ; page {view.get_uri()} ; "
                f"board {PROF.slug} ; dernières commandes : {last}")
        print(line, file=sys.stderr, flush=True)
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            with open(STATE / "crashes.log", "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
        if len(self.crashes) <= 3:
            GLib.timeout_add(500, lambda: (view.reload(), False)[1])
        else:
            view.load_html(f"""<body style="font:16px system-ui;padding:40px;background:#1b1b1a;color:#ecebe6">
              <h2>L'affichage a planté {len(self.crashes)} fois en une minute ({name}).</h2>
              <p>Le motif est noté dans {STATE / 'crashes.log'}.</p>
              <p><a style="color:#85B7EB" href="{self.hub}">Revenir à l'accueil</a> · ou F5 pour recharger.</p></body>""", self.hub)
        return True

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
    if not sys.stderr.isatty():         # lancé depuis le menu : pas de terminal pour voir les erreurs
        log_to_file()
    return WhiteboardApp().run([WM_CLASS, *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
