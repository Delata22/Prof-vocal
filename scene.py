#!/usr/bin/python3
"""Scènes animées façon 3Blue1Brown pour le tableau.

  python3 scene.py check cards/3.4.js
      assemble cards/3.4.html (bibliothèque scene/scene.js incluse), la dessine hors écran étape par étape,
      signale les défauts (textes qui se chevauchent, traits qui traversent un texte, objets hors cadre,
      erreurs JS) et écrit l'aperçu cards/3.4.png (toutes les étapes).
  python3 scene.py add <slug> 3.4 "Titre" [--step "3. Étape"] --file cards/3.4.js [--force]
      pareil, puis ajoute la carte au cahier (kind animation) si la vérification passe (--force : même sinon).

Utilise le python3 du système (GTK + WebKit) : se relance avec /usr/bin/python3 si besoin.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE / "scene" / "scene.js"
CAHIER = Path.home() / ".claude" / "cahier" / "cahier.py"
KATEX = "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist"

try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    gi.require_foreign("cairo")
    from gi.repository import Gtk, WebKit2, GLib
    import cairo
except (ImportError, ValueError):
    if sys.executable != "/usr/bin/python3" and Path("/usr/bin/python3").exists():
        os.execv("/usr/bin/python3", ["/usr/bin/python3", *sys.argv])
    raise


def build(js: Path) -> Path:
    """La carte : document HTML autonome (KaTeX depuis le CDN, bibliothèque et scène incluses)."""
    code = js.read_text()
    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="{KATEX}/katex.min.css" crossorigin="anonymous">
<script src="{KATEX}/katex.min.js" crossorigin="anonymous"></script>
</head><body>
<script>
{LIB.read_text()}
</script>
<script>
{code}
</script>
</body></html>
"""
    out = js.with_suffix(".html")
    out.write_text(html)
    return out


def render(html_path: Path, png: Path, dark=True):
    """Dessine chaque étape (état final) hors écran ; renvoie (rapport par étape, erreurs, légendes)."""
    Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", dark)
    win = Gtk.OffscreenWindow()
    W = 1000
    win.set_default_size(W, 600)
    view = WebKit2.WebView()
    s = view.get_settings()
    s.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.NEVER)
    win.add(view)
    win.show_all()
    res = {"steps": [], "errors": [], "captions": [], "shots": []}

    def js(code, cb):
        def done(v, r):
            try:
                val = v.evaluate_javascript_finish(r)
                cb(json.loads(val.to_json(0) or "null"))
            except GLib.Error as e:
                cb({"__error": e.message})
        view.evaluate_javascript(code, -1, None, None, None, done)

    def later(ms, f):
        GLib.timeout_add(ms, lambda: (f(), False)[1])

    def fail(msg):
        res["errors"].append(msg)
        Gtk.main_quit()

    state = {"t": 0, "n": 0, "k": 0}

    def wait_ready():
        state["t"] += 1
        if state["t"] > 150:
            return fail("la scène ne s'est pas chargée en 30 s (KaTeX ou réseau ?)")

        def got(r):
            if r and r is not True and isinstance(r, dict) and r.get("__error"):
                return later(200, wait_ready)
            if r is True:
                js("document.body.style.background='var(--bg)';"
                   "JSON.stringify({n:__scene.steps(),size:__scene.size(),cap:__scene.captions(),err:__scene.errors()})",
                   lambda j: start(json.loads(j)))
            else:
                later(200, wait_ready)
        js("!!(window.__scene && __scene.ready())", got)

    def start(info):
        res["errors"] += info["err"]
        res["captions"] = info["cap"]
        w, h = info["size"]
        win.resize(W, int(W * h / w) + (44 if info["n"] else 6))
        state["n"] = max(1, info["n"])
        later(300, step)

    def step():
        k = state["k"]
        if k >= state["n"]:
            # puis toutes les étapes jouées animées (accéléré ×4) : erreurs qui n'arrivent qu'en mouvement
            def played(v, r):
                try:
                    errs = json.loads(v.call_async_javascript_function_finish(r).to_json(0) or "[]")
                    res["errors"] = list(dict.fromkeys(res["errors"] + [f"(pendant l'animation) {e}" for e in errs
                                                                       if e not in res["errors"]]))
                except GLib.Error as e:
                    res["errors"].append("lecture animée : " + e.message)
                Gtk.main_quit()
            view.call_async_javascript_function("return await __scene.playthrough(4);", -1, None, None, None, None, played)
            return

        def after_goto(_):
            later(350, lambda: js("JSON.stringify({r:__scene.report(),err:__scene.errors()})", got_report))

        def got_report(j):
            j = json.loads(j)
            res["steps"].append(j["r"])
            res["errors"] = list(dict.fromkeys(res["errors"] + j["err"]))
            view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None, snap)

        def snap(v, r):
            try:
                surf = v.get_snapshot_finish(r)
                res["shots"].append(surf)
            except GLib.Error as e:
                res["errors"].append("capture : " + e.message)
            state["k"] += 1
            step()
        js(f"__scene.goto({k}, true).then(() => 1)", after_goto)

    view.load_uri(html_path.resolve().as_uri())
    later(300, wait_ready)
    GLib.timeout_add_seconds(90, lambda: (fail("délai dépassé (90 s)"), False)[1])
    Gtk.main()
    montage(res["shots"], res["captions"], png)
    return res


def montage(shots, caps, png: Path):
    """Toutes les étapes sur une image (2 colonnes, moitié de taille), numérotées."""
    if not shots:
        return
    w, h = shots[0].get_width(), shots[0].get_height()
    k = 0.55 if len(shots) > 1 else 1.0
    cols = 1 if len(shots) == 1 else 2
    rows = (len(shots) + cols - 1) // cols
    cw, ch = int(w * k), int(h * k) + 26
    out = cairo.ImageSurface(cairo.FORMAT_RGB24, cw * cols, ch * rows)
    c = cairo.Context(out)
    c.set_source_rgb(0.5, 0.5, 0.5)
    c.paint()
    for i, s in enumerate(shots):
        x, y = (i % cols) * cw, (i // cols) * ch
        c.save()
        c.translate(x, y + 26)
        c.scale(k, k)
        c.set_source_surface(s, 0, 0)
        c.paint()
        c.restore()
        c.set_source_rgb(1, 1, 1)
        c.select_font_face("sans-serif")
        c.set_font_size(15)
        c.move_to(x + 8, y + 18)
        cap = caps[i] if i < len(caps) else ""
        c.show_text(f"étape {i}" + (f" : {cap[:55]}" if cap else "") if len(shots) > 1 or cap else "")
    out.write_to_png(str(png))


def check(js: Path) -> bool:
    html = build(js)
    png = js.with_suffix(".png")
    res = render(html, png)
    ok = not res["errors"] and not any(r["problems"] for r in res["steps"])
    n = len(res["steps"])
    print(f"{'ok' if ok else 'À CORRIGER'} : {html} ({n} étape{'s' if n > 1 else ''}) ; aperçu : {png.resolve()}")
    for e in res["errors"]:
        print("  erreur :", e.strip().splitlines()[0][:300])
    for i, r in enumerate(res["steps"]):
        for p in r["problems"]:
            print(f"  étape {i} : {p}")
    if res["steps"]:
        for h in res["steps"][-1]["hints"]:
            print(f"  conseil : {h}")
    if not ok:
        print("  Corrige le .js (placement : next_to / arrange / buff, positions, tailles), puis relance.")
    return ok


def main():
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "check":
        return 0 if check(Path(a[1])) else 1
    if len(a) >= 4 and a[0] == "add":
        slug, cid, title = a[1], a[2], a[3]
        rest = a[4:]
        force = "--force" in rest
        rest = [x for x in rest if x != "--force"]
        if "--file" not in rest:
            print("--file cards/<id>.js manquant")
            return 2
        i = rest.index("--file")
        js = Path(rest[i + 1])
        extra = rest[:i] + rest[i + 2:]
        ok = check(js)
        if not ok and not force:
            print("  Carte NON ajoutée (relance après correction, ou --force pour l'ajouter quand même).")
            return 1
        r = subprocess.run(["python3", str(CAHIER), "add", slug, cid, title, "--kind", "animation",
                            "--file", str(js.with_suffix(".html")), *extra], capture_output=True, text=True)
        print((r.stdout + r.stderr).strip())
        return r.returncode
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
