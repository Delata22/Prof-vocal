#!/usr/bin/env python3
"""Banc d'essai de la reconnaissance vocale sur les enregistrements de calibration (calib/<run>/).

La page Prof (⚙ Réglages → « Calibrer le micro ») enregistre 12 phrases de référence, lues avec puis sans les
filtres du navigateur, au taux natif du micro. Ce script les transcrit avec plusieurs réglages et calcule le
WER (taux d'erreur sur les mots, après normalisation : minuscules, ponctuation, nombres écrits en lettres).

  .venv/bin/python bench_asr.py                 # tous les runs de calib/
  .venv/bin/python bench_asr.py --runs 2026-10-22-10-30_xxx --quick
  .venv/bin/python bench_asr.py --voxtral       # ajoute Voxtral Mini (transformers, ~9 Go de VRAM en bf16)

Résultats : calib/resultats.md (tableau) et calib/resultats.jsonl (chaque transcription).
"""
import argparse, json, os, re, sys, time
from pathlib import Path

os.environ.setdefault("PROF_REEXEC", "1")          # prof_server ne doit pas se relancer lui-même
HERE = Path(__file__).resolve().parent
NV = HERE / ".venv/lib/python3.12/site-packages/nvidia"
if os.environ.get("BENCH_REEXEC") != "1":          # cuBLAS / cuDNN pour ctranslate2 (comme prof_server)
    os.environ["BENCH_REEXEC"] = "1"
    os.environ["LD_LIBRARY_PATH"] = ":".join([str(NV / "cublas/lib"), str(NV / "cudnn/lib"), os.environ.get("LD_LIBRARY_PATH", "")])
    os.execv(sys.executable, [sys.executable, *sys.argv])

import numpy as np, soxr, wave
from num2words import num2words

sys.path.insert(0, str(HERE))
import prof_server as ps

CALIB = HERE / "calib"


# ---------- normalisation et WER ----------

def _num(m):
    neg, ent, dec = m.group(1), m.group(2), m.group(3)
    w = num2words(int(ent), lang="fr")
    if dec:
        w += " virgule " + num2words(int(dec), lang="fr")
    return (" moins " if neg else " ") + w + " "


def norm(t):
    t = t.lower().replace("’", "'")
    t = re.sub(r"(-|−)?\b(\d+)(?:[.,](\d+))?\b", _num, t)
    t = re.sub(r"[-'’]", " ", t)                 # vingt-huit -> vingt huit, l'image -> l image
    t = re.sub(r"[^\w\s]", " ", t)
    return t.split()


def edits(r, h):
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)]


# ---------- données ----------

def load(runs):
    items = []
    for run in sorted(p for p in CALIB.iterdir() if p.is_dir()):
        if (runs and run.name not in runs) or (not runs and run.name.startswith("synthetique")):
            continue                               # le run synthétique (Piper) ne sert qu'à tester le banc
        meta = {}
        for line in open(run / "meta.jsonl"):
            m = json.loads(line)
            meta[m["file"]] = m                    # la dernière prise d'une phrase gagne (« Refaire »)
        for m in meta.values():
            with wave.open(str(run / m["file"])) as w:
                x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
                sr = w.getframerate()
            items.append({**m, "run": run.name, "audio": soxr.resample(x, sr, 16000, quality="HQ").astype(np.float32)})
    return items


# ---------- systèmes ----------

def whisper_systems(quick):
    from faster_whisper import WhisperModel
    models = [("turbo", "large-v3-turbo", "int8_float16")] + ([] if quick else [("large-v3", "large-v3", "float16")])
    for name, model, compute in models:
        ears = ps.Ears.__new__(ps.Ears)            # même code de transcription que le serveur, sans le chauffer
        ears.m = WhisperModel(model, device="cuda", compute_type=compute)
        ears.beam, ears.topic = 5, "SIFT"
        ears.vocabs = [ps.Watched(HERE / "vocab.txt", ps.parse_vocab)]
        variants = [(5, True), (1, True), (5, False)] if not quick else [(5, True), (5, False)]
        for beam, prompt in variants:
            label = f"{name} {compute} · beam {beam} · {'prompt' if prompt else 'sans prompt'}"
            yield label, (lambda e, b, p: lambda it: e.transcribe(it["audio"], it["maths"], use_prompt=p, beam=b))(ears, beam, prompt)
        del ears


def voxtral_system():
    import torch
    from transformers import VoxtralForConditionalGeneration, AutoProcessor
    rid = "mistralai/Voxtral-Mini-3B-2507"
    proc = AutoProcessor.from_pretrained(rid)
    model = VoxtralForConditionalGeneration.from_pretrained(rid, torch_dtype=torch.bfloat16, device_map="cuda")

    def run(it):
        inputs = proc.apply_transcription_request(language="fr", audio=it["audio"], format="wav", sampling_rate=16000, model_id=rid)
        inputs = inputs.to("cuda", dtype=torch.bfloat16)
        out = model.generate(**inputs, max_new_tokens=200)
        return proc.batch_decode(out[:, inputs.input_ids.shape[1]:], skip_special_tokens=True)[0].strip()
    yield "Voxtral Mini 3B bf16", run


# ---------- banc ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", nargs="*")
    ap.add_argument("--quick", action="store_true", help="turbo seulement, 2 variantes")
    ap.add_argument("--voxtral", action="store_true")
    a = ap.parse_args()
    items = load(a.runs)
    if not items:
        sys.exit("aucun enregistrement dans calib/ : fais d'abord « Calibrer le micro » dans la page")
    print(f"{len(items)} enregistrements, runs : {sorted({i['run'] for i in items})}")
    systems = whisper_systems(a.quick)
    rows, log = [], open(CALIB / "resultats.jsonl", "w")
    groups = lambda it: ["tout", "filtres" if it["filters"] else "sans filtres", "calcul" if it["maths"] else "cours",
                         "micro : " + (it["mic"] or "?")[:40]]

    def bench(label, fn):
        acc, t0 = {}, time.time()
        for it in items:
            hyp = fn(it)
            r, h = norm(it["ref"]), norm(hyp)
            e = edits(r, h)
            for g in groups(it):
                s = acc.setdefault(g, [0, 0]); s[0] += e; s[1] += len(r)
            log.write(json.dumps({"systeme": label, "run": it["run"], "file": it["file"], "ref": it["ref"], "hyp": hyp,
                                  "erreurs": e, "mots": len(r)}, ensure_ascii=False) + "\n")
        dt = (time.time() - t0) / len(items)
        rows.append((label, {g: 100 * e / max(n, 1) for g, (e, n) in acc.items()}, dt))
        print(f"{label:55s} WER {rows[-1][1]['tout']:5.1f} %   ({dt:.2f} s/phrase)", flush=True)

    for label, fn in systems:
        bench(label, fn)
    if a.voxtral:
        for label, fn in voxtral_system():
            bench(label, fn)

    cols = ["tout", "filtres", "sans filtres", "cours", "calcul"] + sorted({g for _, d, _ in rows for g in d if g.startswith("micro")})
    md = ["| système | " + " | ".join(cols) + " | s/phrase |", "|" + "---|" * (len(cols) + 2)]
    for label, d, dt in sorted(rows, key=lambda r: r[1]["tout"]):
        md.append(f"| {label} | " + " | ".join(f"{d[c]:.1f}" if c in d else "–" for c in cols) + f" | {dt:.2f} |")
    txt = f"WER en % (plus bas = mieux), {len(items)} enregistrements.\n\n" + "\n".join(md) + "\n"
    (CALIB / "resultats.md").write_text(txt)
    print("\n" + txt)


if __name__ == "__main__":
    main()
