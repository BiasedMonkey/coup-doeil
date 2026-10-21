#!/usr/bin/env python3
"""Vérifications de l'interface de Coup d'œil, sans écran (rendu hors écran).

    python3 tests/verifier.py

Chaque test utilise une configuration temporaire : tes réglages ne sont pas touchés.
"""

import os
import random
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="coup-doeil-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coup_doeil as m  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
echecs = []


def verifie(nom, condition, detail=""):
    print(f"  {'✓' if condition else '✗'} {nom}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        echecs.append(nom)


def ordre(cols):
    return [[c.module_id for c in m.Monitor.cards_in(lay)] for _, lay in cols]


def glisser(w, carte, cible, ou):
    """Glisse la poignée ⠿ de `carte` vers le haut/bas de `cible`, avec de vrais événements souris."""
    poignee = carte.findChild(m.Grip)
    QTest.mousePress(poignee, Qt.LeftButton, Qt.NoModifier, QPoint(4, 6))
    but = cible.mapToGlobal(QPoint(cible.width() // 2, 5 if ou == "haut" else cible.height() - 5))
    for k in range(1, 9):
        depart = poignee.mapToGlobal(QPoint(4, 6))
        QTest.mouseMove(poignee, poignee.mapFromGlobal(depart + (but - depart) * k / 8))
        app.processEvents()
    QTest.mouseRelease(poignee, Qt.LeftButton, Qt.NoModifier, poignee.mapFromGlobal(but))
    app.processEvents()


print("Modules déplaçables")
w = m.Monitor()
w.show()
w.expand.setChecked(False)
QTest.qWait(1200)
glisser(w, w.c_ram, w.c_cpu, "haut")
verifie("la RAM passe en tête (vue compacte)", ordre(w.compact_cols)[0][0] == "ram", ordre(w.compact_cols))
w.expand.setChecked(True)
QTest.qWait(1200)
if len(w.d_gpus) > 0:
    glisser(w, w.d_gpus[0], w.d_ram, "bas")
    verifie("un GPU change de colonne (grande vue)", "gpu0" in ordre(w.detail_cols)[2], ordre(w.detail_cols))
attendu = (ordre(w.compact_cols), ordre(w.detail_cols))
w.close()
w2 = m.Monitor()
verifie("l'ordre est retenu après relance", (ordre(w2.compact_cols), ordre(w2.detail_cols)) == attendu)
w2.close()

print("Tailles retenues par vue")
w = m.Monitor()
w.show()
QTest.qWait(1200)
if w.detailed:
    w.expand.setChecked(False)
    QTest.qWait(1200)
w.resize(430, 1000)
QTest.qWait(700)
w.expand.setChecked(True)
QTest.qWait(1200)
w.resize(1300, 900)
QTest.qWait(700)
w.expand.setChecked(False)
QTest.qWait(1200)
verifie("vue compacte : 430×1000", w.size().toTuple() == (430, 1000), w.size().toTuple())
w.expand.setChecked(True)
QTest.qWait(1200)
verifie("grande vue : 1300×900", w.size().toTuple() == (1300, 900), w.size().toTuple())
w.close()
w = m.Monitor()
w.show()
QTest.qWait(1200)
verifie("relance en grande vue : 1300×900", w.size().toTuple() == (1300, 900), w.size().toTuple())

print("Stabilité de l'interface (aucun module ne change de taille)")
random.seed(4)
vraie_batterie, vrais_procs = w.sys.battery, w.procs.sample
gpu_dedie = next((g for g in w.gpus if not g.integrated), None)
vrai_gpu = gpu_dedie.sample if gpu_dedie else None


def chaos(i):
    w.sys.battery = lambda: {**(vraie_batterie() or {"percent": 50}),
                             "status": "Discharging" if i % 2 == 0 else "Charging", "label": "x",
                             "watts": random.choice([5.0, 35.0, 80.0]), "left": random.choice([None, 300, 9000])}
    if gpu_dedie:
        gpu_dedie.sample = (lambda: {"asleep": True}) if i % 3 == 0 else vrai_gpu
    if w.apu:
        w.apu.read = lambda: {"socket": random.uniform(2, 40), "cores": random.uniform(0, 30),
                              "gfx": random.choice([0.0, random.uniform(0, 20)]), "soc": random.uniform(0, 5)}

    def procs():
        d = vrais_procs()
        d["cpu"], d["mem"] = d["cpu"][:random.randint(0, 5)], d["mem"][:random.randint(0, 6)]
        d["gpu"] = {k: v[:random.randint(0, 3)] for k, v in d["gpu"].items()}
        return d
    w.procs.sample = procs


for vue in ("compacte", "grande vue"):
    w.expand.setChecked(vue == "grande vue")
    QTest.qWait(1200)
    vus = []
    for i in range(20):
        chaos(i)
        w.tick += 1
        w.proc_data = w.procs.sample()
        w.refresh()
        QTest.qWait(30)
        vus.append((w.size().toTuple(),
                    {c.module_id: c.size().toTuple() for _, lay in w.visible_cols() for c in w.cards_in(lay)}))
    bouge = sorted({k for _, cartes in vus for k in cartes if len({v[1][k] for v in vus}) > 1})
    verifie(f"{vue} : rien ne bouge sur 20 s de conditions changeantes",
            not bouge and len({v[0] for v in vus}) == 1, f"modules qui bougent : {bouge}")

print()
print("Tout est bon." if not echecs else f"{len(echecs)} échec(s) : {', '.join(echecs)}")
sys.exit(1 if echecs else 0)
