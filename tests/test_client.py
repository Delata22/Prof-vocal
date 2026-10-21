"""Test sans micro : joue le rôle de la page. Envoie des wav comme segments VAD,
mesure segment envoyé -> 1re phrase audio reçue, et teste l'interruption.
Usage : .venv/bin/python tests/test_client.py [port]"""
import asyncio, json, sys, time, wave, numpy as np, aiohttp
from pathlib import Path
HERE = Path(__file__).parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8778
VAD_WAIT = 0.55   # le VAD de la page attend ce silence avant d'envoyer le segment

def pcm(name):
    with wave.open(str(HERE / f"{name}.wav")) as w:
        return (np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768).tobytes()

async def turn(ws, name, cut_after=None):
    t0 = time.time(); await ws.send_bytes(pcm(name)); first = None; says = []
    print(f"\n>>> {name}")
    async for m in ws:
        j = json.loads(m.data)
        if j["t"] == "user": print(f"  entendu ({time.time()-t0:.2f}s) : {j['text']}")
        elif j["t"] == "tool": print(f"  [{j['text']}]")
        elif j["t"] == "say":
            if first is None:
                first = time.time() - t0
                print(f"  1re phrase audio après {first:.2f}s (+{VAD_WAIT}s VAD ≈ {first+VAD_WAIT:.2f}s fin de parole → son)")
            says.append(j["text"]); print(f"  prof [{j['idx']}] : {j['text']}")
            if cut_after and len(says) >= cut_after:
                print("  !!! l'élève coupe ici"); await ws.send_str(json.dumps({"t": "barge", "last": j["text"]}))
                return first, says
        elif j["t"] == "done": return first, says

async def main():
    async with aiohttp.ClientSession() as s, s.ws_connect(f"http://localhost:{PORT}/ws", max_msg_size=0) as ws:
        lat = []
        f, _ = await turn(ws, "question16k"); lat.append(f)
        f, _ = await turn(ws, "q_gradient", cut_after=2); lat.append(f)
        f, _ = await turn(ws, "q_coupe"); lat.append(f)
        print("\nlatences segment→1er son (sans VAD) :", [round(x, 2) for x in lat if x])

asyncio.run(main())
