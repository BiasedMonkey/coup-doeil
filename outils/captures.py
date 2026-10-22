#!/usr/bin/env python3
"""Régénère les captures du README (captures/) avec des données de démonstration :
aucun programme ni valeur de la machine n'y apparaît, sauf les noms du matériel."""

import math
import os
import random
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="coup-doeil-captures-")
racine = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(racine))

import coup_doeil as m  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
w = m.Monitor()
w.show()
Go, Mo = 2**30, 2**20
demo = {"cpu": [("firefox", 38.0), ("code", 21.0), ("kwin_wayland", 9.0), ("python3", 6.0), ("pipewire", 2.0)],
        "mem": [("firefox", 2.1 * Go), ("code", 1.3 * Go), ("plasmashell", 0.4 * Go), ("python3", 0.2 * Go),
                ("kwin_wayland", 0.15 * Go), ("pipewire", 0.05 * Go)],
        "count": 412,
        "gpu": {g.pdev: ([("firefox", 300 * Mo, 200 * Mo), ("kwin_wayland", 150 * Mo, 80 * Mo)] if g.integrated
                         else [("game", 5.2 * Go, 0)]) for g in w.gpus}}
w.procs.sample = lambda: demo
vraie = w.sys.battery
w.sys.battery = lambda: {**(vraie() or {}), "percent": 76.0, "status": "Discharging",
                         "label": "sur batterie", "watts": 32.0, "left": 2.4 * 3600}
QTest.qWait(2300)
random.seed(5)
for cle, base in [("cpu", 32), ("gpu0", 58), ("gpu1", 14), ("ram", 55), ("cpu_temp", 62), ("gpu0_vram", 68), ("swap", 8)]:
    for i in range(m.HISTORY + 1):
        w.hist[cle].append(max(0, min(100, base + 22 * math.sin(i / 7 + len(cle)) + random.uniform(-10, 10))))
for vue, fichier in ((False, "vue-compacte.png"), (True, "grande-vue.png")):
    w.expand.setChecked(vue)
    QTest.qWait(1200)
    w.tick += 1
    w.proc_data = demo
    w.refresh(sample=False)
    QTest.qWait(150)
    w.grab().save(str(racine / "captures" / fichier))
panneau = m.OptionsPanel(w)
panneau.adjustSize()
panneau.show()
QTest.qWait(100)
panneau.grab().save(str(racine / "captures" / "options.png"))
print("captures régénérées dans", racine / "captures")
