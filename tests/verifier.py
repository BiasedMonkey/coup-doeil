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

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="coup-doeil-test-")
# écran virtuel de la taille d'un portable 16" (2048×1280 en pixels logiques)
ecran = Path(os.environ["XDG_CONFIG_HOME"]) / "ecran.json"
ecran.write_text('{"screens":[{"name":"ecran","x":0,"y":0,"width":2048,"height":1280,'
                 '"logicalDpi":96,"logicalBaseDpi":96,"dpr":1}]}')
os.environ["QT_QPA_PLATFORM"] = f"offscreen:configfile={ecran}"
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
verifie("relance : toujours en vue compacte, à sa taille", not w.detailed and w.size().toTuple() == (430, 1000),
        w.size().toTuple())
w.expand.setChecked(True)
QTest.qWait(1200)
verifie("puis grande vue : sa taille est retenue (1300×900)", w.size().toTuple() == (1300, 900), w.size().toTuple())

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

print("Options (appliquées tout de suite, retenues après relance)")
w.close()
w = m.Monitor()
w.show()
w.expand.setChecked(False)
QTest.qWait(1200)
w.set_refresh(2000)
w.set_bg_opacity(55)
w.set_smooth_scroll(False)
w.set_smoothing(4)
cache = next(iter(w.compact_cards))
w.set_module_visible(cache, False)
QTest.qWait(50)
verifie("rafraîchissement : minuterie à 2 s", w.timer.interval() == 2000)
verifie("défilement fluide coupé : plus d'animation", not w.anim.isActive())
verifie(f"module « {cache} » masqué", not w.compact_cards[cache].isVisible())
panneau = m.OptionsPanel(w)
panneau.show()
QTest.qWait(50)
verifie("le panneau d'options s'ouvre", panneau.isVisible())
panneau.close()
w.close()
w = m.Monitor()
w.show()
QTest.qWait(1200)
verifie("réglages retenus après relance",
        (w.refresh_ms, w.smooth_scroll, w.smooth_passes, round(m.BG.alphaF() * 100), cache in w.hidden)
        == (2000, False, 4, 55, True))
w.reset_layout()
verifie("réinitialiser remet l'ordre par défaut",
        [c.module_id for c in w.cards_in(w.compact_cols[0][1])][0] == "cpu")
m.set_autostart(True)
verifie("lancement à l'ouverture de session : fichier créé", m.AUTOSTART.exists())
m.set_autostart(False)

print("Replier / déplier (vue compacte)")
w.close()
w = m.Monitor()
w.show()
w.expand.setChecked(False)
QTest.qWait(1200)
w.move(20, 10)
carte = next(c for c in w.compact_cards.values() if c.isVisible() and c.elastic)
h0, c0 = w.height(), carte.height()
autres = {c.module_id: c.height() for c in w.compact_cards.values() if c is not carte}
w.toggle_card(carte)
QTest.qWait(100)
verifie("replié : ne garde que sa ligne de titre", carte.height() < c0 / 2, f"{c0} → {carte.height()} px")
verifie("la fenêtre rétrécit de la place libérée", abs((h0 - w.height()) - (c0 - carte.height())) <= 2,
        f"{h0} → {w.height()}")
verifie("les autres modules ne bougent pas",
        {c.module_id: c.height() for c in w.compact_cards.values() if c is not carte} == autres)
w.toggle_card(carte)
QTest.qWait(100)
verifie("déplié : module et fenêtre retrouvent leur taille",
        abs(carte.height() - c0) <= 2 and abs(w.height() - h0) <= 2, f"{carte.height()} px, fenêtre {w.height()}")
w.toggle_card(carte)
QTest.qWait(700)
w.close()
w = m.Monitor()
verifie("l'état replié est retenu après relance", w.compact_cards[carte.module_id].collapsed)

if w.c_bat:
    print("Consommation repliable (module Batterie)")
    w.show()
    w.expand.setChecked(False)
    QTest.qWait(1200)
    w.move(20, 10)
    b = w.c_bat
    if b.collapsed:
        w.toggle_card(b)
        QTest.qWait(100)
    h0, b0 = w.height(), b.height()
    w.toggle_section(b, "conso")
    QTest.qWait(100)
    verifie("repliée : la liste disparaît, jauge et total restent",
            not b.split.isVisible() and b.mem.isVisible() and b.split_total.isVisible())
    verifie("la fenêtre rétrécit de la place libérée", abs((h0 - w.height()) - (b0 - b.height())) <= 2)
    w.toggle_section(b, "conso")
    QTest.qWait(100)
    verifie("dépliée : tout revient", b.split.isVisible() and abs(w.height() - h0) <= 2)

print("Ranger au bord de l'écran")
w.close()
w = m.Monitor()
w.show()
w.expand.setChecked(False)
QTest.qWait(1200)
taille = w.size().toTuple()
w.move(0, 200)
QTest.qWait(700)
verifie("contre le bord, mais déplacée par l'appli : pas rangée", w.docked is None)
w.user_moving = True                       # comme un dépôt à la souris
w.move(0, 220)
QTest.qWait(700)
verifie("déposée contre le bord gauche : rangée en languette", w.docked == "left" and w.width() < 40,
        f"{w.size().toTuple()} en {w.pos().toTuple()}")
w.tick += 1
w.refresh()
verifie("la languette montre un point par composant", len(w.dock_tab.levels) == 2 + len(w.gpus))
w.move(5, 500)
QTest.qWait(700)
verifie("glissée le long du bord : recollée, à 6 px du bord", w.x() == m.DOCK_MARGIN and w.y() == 500)
QTest.mousePress(w.dock_tab, Qt.LeftButton, Qt.NoModifier, QPoint(10, 10))
QTest.mouseRelease(w.dock_tab, Qt.LeftButton, Qt.NoModifier, QPoint(10, 10))
QTest.qWait(300)
verifie("clic : redéployée à sa taille, à l'écart du bord", w.docked is None and w.size().toTuple() == taille and w.x() > 0,
        f"{w.size().toTuple()} en {w.pos().toTuple()}")
QTest.qWait(1200)
w.user_moving = True
w.move(2048 - w.width() + 1, 100)
QTest.qWait(700)
QTest.qWait(300)
w.close()
w = m.Monitor()
w.show()
QTest.qWait(800)
verifie("relance : jamais en languette, dépliée à sa dernière place", w.docked is None and not w.detailed
        and w.x() + w.width() - 1 <= 2048 and w.width() > 100, f"{w.size().toTuple()} en {w.pos().toTuple()}")

print("Bouton ⇤ « Ranger au bord »")
w.close()
w = m.Monitor()
w.show()
w.expand.setChecked(False)
QTest.qWait(1200)
w.move(300, 150)
QTest.qWait(600)
place, taille = w.pos().toTuple(), w.size().toTuple()
w.dock_btn.click()
QTest.qWait(300)
verifie("rangée au bord le plus proche (gauche)", w.docked == "left" and w.x() == m.DOCK_MARGIN)
QTest.mousePress(w.dock_tab, Qt.LeftButton, Qt.NoModifier, QPoint(10, 10))
QTest.mouseRelease(w.dock_tab, Qt.LeftButton, Qt.NoModifier, QPoint(10, 10))
QTest.qWait(300)
verifie("clic sur la languette : retour exact à sa place", w.docked is None and w.pos().toTuple() == place
        and w.size().toTuple() == taille, f"{w.pos().toTuple()} (attendu {place})")
QTest.qWait(1200)
w.move(2048 - w.width() - 300, 150)
QTest.qWait(600)
w.dock_btn.click()
QTest.qWait(300)
verifie("près du bord droit : rangée à droite", w.docked == "right")
w.undock()

print()
print("Tout est bon." if not echecs else f"{len(echecs)} échec(s) : {', '.join(echecs)}")
sys.exit(1 if echecs else 0)
