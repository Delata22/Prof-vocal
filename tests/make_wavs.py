"""Fabrique des « fausses questions » parlées avec Piper (wav 16 kHz mono)."""
import sys, wave, numpy as np
from pathlib import Path
from piper import PiperVoice
HERE = Path(__file__).parent
v = PiperVoice.load(str(Path.home() / ".claude/cahier/voices/fr_FR-siwis-medium.onnx"))
Q = {
    "question16k": "Bonjour. Peux-tu m'expliquer à quoi sert le descripteur SIFT ?",
    "q_gradient": "D'accord, mais concrètement, comment on calcule l'orientation du gradient en un pixel ? Mets-moi un exemple au tableau.",
    "q_coupe": "Attends, attends. Pourquoi on utilise une différence de gaussiennes ?",
    # panneau « Ta réponse »
    "a_ligne": "sigma deux égale racine de deux fois sigma un",
    "a_pourquoi": "parce que les variances s'additionnent quand on enchaîne deux flous",
    "a_corrige": "corrige : racine de trois, pas racine de deux",
    "a_envoie": "c'est bon, j'envoie",
}
for name, text in Q.items():
    x = np.concatenate([c.audio_float_array for c in v.synthesize(text)])
    sr = v.config.sample_rate
    y = np.interp(np.arange(0, len(x), sr / 16000), np.arange(len(x)), x)
    with wave.open(str(HERE / f"{name}.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes())
    print(name, f"{len(y)/16000:.1f}s")
