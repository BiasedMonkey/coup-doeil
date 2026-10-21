#!/usr/bin/env python3
"""Galerie de styles de graphiques en points pour Coup d'œil.

Chaque style reçoit la même série de valeurs et défile en continu (60 i/s).
Bouton « Démo / Réel » : signal de démonstration ou vraie charge CPU.
"""

import math
import os
import random
import sys
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coup_doeil  # noqa: E402  (bascule xcb + couleurs communes)
from coup_doeil import BG, DIM, MUTED, PANEL, TEXT, faded, heat_color, label  # noqa: E402

import psutil  # noqa: E402
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QPainter, QPainterPath  # noqa: E402
from PySide6.QtWidgets import (QApplication, QGridLayout, QPushButton,  # noqa: E402
                               QVBoxLayout, QWidget)

N = 60          # secondes visibles


# ───────────────────────────── outils communs ─────────────────────────────

def at(vals, t):
    """Valeur interpolée (cosinus, donc douce) à l'indice fractionnaire t."""
    i0 = max(0, min(len(vals) - 1, int(math.floor(t))))
    i1 = min(len(vals) - 1, i0 + 1)
    a, b = vals[i0], vals[i1]
    if a is None:
        return b
    if b is None:
        return a
    f = (1 - math.cos((t - i0) * math.pi)) / 2
    return a + (b - a) * f


def dot(p, x, y, r, color):
    p.setBrush(color)
    p.drawEllipse(QPointF(x, y), r, r)


# ───────────────────────────────── styles ─────────────────────────────────
# Signature : style(p, w, h, vals, shift)
#   vals  : N+1 valeurs dans [0, 1] (ou None), la plus récente à la fin
#   shift : 0 → 1 pendant la seconde qui s'écoule, pour le défilement fluide

def led_column(p, x, h, rows, dy, level, r, size_fn=None, alpha_fn=None):
    top = level * rows
    for row in range(rows):
        y = h - (row + 0.5) * dy
        if row < top:
            k = (row + 1) / max(top, 1e-6)                  # 1 = sommet
            s = min(1.0, top - row)                          # sommet partiel
            s = 0.35 + 0.65 * s
            if size_fn:
                s *= size_fn(k)
            c = heat_color((row + 1) / rows * 100)
            if alpha_fn:
                c = faded(c, alpha_fn(k))
            dot(p, x, y, r * s, c)
        else:
            dot(p, x, y, r * 0.42, DIM)


def style_led(p, w, h, vals, shift):
    """A — LED : gros points égaux, comme la toute première version."""
    rows = 10
    dx, dy = w / N, h / rows
    r = min(dx, dy) * 0.42
    for i, v in enumerate(vals):
        x = (i + 0.5 - shift) * dx
        led_column(p, x, h, rows, dy, v or 0, r)


def style_led_fine(p, w, h, vals, shift):
    """B — LED fine : 2× plus de points, tous de la même taille, grille droite."""
    rows = max(6, int(h / 3.2))
    sub = 2
    dx, dy = w / (N * sub), h / rows
    r = min(dx, dy) * 0.40
    for j in range((N + 1) * sub):
        t = j / sub
        x = (t + 0.5 / sub - shift) * dx * sub
        led_column(p, x, h, rows, dy, at(vals, t) or 0, r)


def style_trame(p, w, h, vals, shift):
    """C — Trame alignée : gros points en haut, de plus en plus fins vers le bas."""
    rows = max(6, int(h / 3.6))
    sub = 2
    dx, dy = w / (N * sub), h / rows
    r = min(dx, dy) * 0.5
    for j in range((N + 1) * sub):
        t = j / sub
        x = (t + 0.5 / sub - shift) * dx * sub
        led_column(p, x, h, rows, dy, at(vals, t) or 0, r,
                   size_fn=lambda k: 0.3 + 0.7 * k,
                   alpha_fn=lambda k: 0.5 + 0.5 * k)


def style_nuage(p, w, h, vals, shift):
    """D — Nuage : 1 point par seconde à la hauteur de la valeur, taille ∝ valeur."""
    for gx in range(0, int(w), 9):                            # fond pointillé
        for gy in range(4, int(h), 9):
            dot(p, gx + 4, gy, 0.6, DIM)
    dx = w / N
    for i, v in enumerate(vals):
        if v is None:
            continue
        x = (i + 0.5 - shift) * dx
        y = h - 3 - v * (h - 6)
        age = 0.25 + 0.75 * (i + 1) / len(vals)
        c = heat_color(v * 100)
        r = 1.0 + v * 3.2
        dot(p, x, y, r * 2.2, faded(c, 0.10 * age))           # halo
        dot(p, x, y, r, faded(c, age))


def style_perles(p, w, h, vals, shift):
    """E — Courbe perlée : perles sur une courbe lissée, poussière qui s'estompe dessous."""
    step = 3.0
    rows = int(h / step)
    cols = int(w / step) + 2
    off = shift * (w / N)
    for c in range(cols):
        x = c * step + step / 2 - off % step
        t = (x + off) / (w / N) - 0.5
        v = at(vals, t) or 0
        top = v * rows
        for row in range(rows):
            y = h - (row + 0.5) * step
            if row < top - 1:
                k = (row + 1) / top                        # 0 bas → 1 sommet
                dot(p, x, y, 0.55 + 0.5 * k, faded(heat_color((row + 1) / rows * 100), 0.15 + 0.5 * k))
            elif row < top:
                dot(p, x, h - top * step + step / 2, 1.5, heat_color(v * 100))   # la perle
            else:
                dot(p, x, y, 0.45, DIM)


STYLES = [
    ("A", "LED", "Gros points égaux (la première version)", style_led),
    ("B", "LED fine", "Deux fois plus de points, même taille, grille droite", style_led_fine),
    ("C", "Trame alignée", "Gros en haut, fins en bas, grille droite", style_trame),
    ("D", "Nuage", "Un point par seconde, plus gros quand c'est haut", style_nuage),
    ("E", "Courbe perlée", "Une ligne de perles et une poussière dessous", style_perles),
]


# ──────────────────────────────── fenêtre ────────────────────────────────

class Source:
    def __init__(self):
        self.demo = True
        self.vals = deque([None] * (N + 1), maxlen=N + 1)
        self.last = time.monotonic()
        self.phase = 0.0
        psutil.cpu_percent()
        for _ in range(N + 1):
            self.push()

    def push(self):
        if self.demo:
            self.phase += 1
            t = self.phase
            v = 0.45 + 0.28 * math.sin(t / 7) + 0.12 * math.sin(t / 2.3) + random.uniform(-0.08, 0.08)
            if random.random() < 0.05:
                v += 0.35
        else:
            v = psutil.cpu_percent() / 100
        self.vals.append(max(0.0, min(1.0, v)))
        self.last = time.monotonic()

    def shift(self):
        return min(1.0, time.monotonic() - self.last)


class Graph(QWidget):
    def __init__(self, fn, source, height):
        super().__init__()
        self.fn, self.source = fn, source
        self.setFixedHeight(height)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setClipRect(self.rect())
        self.fn(p, self.width(), self.height(), list(self.source.vals), self.source.shift())


class Card(QWidget):
    def __init__(self, letter, name, desc, fn, source):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(4)
        lay.addWidget(label(f"{letter} — {name}", TEXT, 12, True))
        lay.addWidget(label(desc, MUTED, 10))
        self.small = Graph(fn, source, 46)
        self.small.setFixedWidth(270)
        lay.addWidget(self.small)
        self.big = Graph(fn, source, 64)
        lay.addWidget(self.big)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(PANEL)
        p.drawRoundedRect(self.rect(), 10, 10)


class Gallery(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Coup d'œil — styles de points")
        self.source = Source()
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(10)
        self.toggle = QPushButton("Données : démo  (cliquer pour la vraie charge CPU)")
        self.toggle.setStyleSheet(f"color:{TEXT}; background:rgba(255,255,255,0.06);"
                                  "border:none; border-radius:6px; padding:6px;")
        self.toggle.clicked.connect(self.switch)
        root.addWidget(self.toggle)
        grid = QGridLayout()
        grid.setSpacing(10)
        self.cards = []
        for k, (letter, name, desc, fn) in enumerate(STYLES):
            c = Card(letter, name, desc, fn, self.source)
            c.setFixedWidth(370)
            grid.addWidget(c, k // 3, k % 3)
            self.cards.append(c)
        root.addLayout(grid)
        root.addWidget(label("En haut : taille vue compacte · En bas : taille vue détaillée", MUTED, 10))

        QTimer(self, interval=1000, timeout=self.source.push).start()
        QTimer(self, interval=16, timeout=self.repaint_graphs).start()   # ~60 i/s

    def switch(self):
        self.source.demo = not self.source.demo
        self.toggle.setText("Données : démo  (cliquer pour la vraie charge CPU)" if self.source.demo
                            else "Données : charge CPU réelle  (cliquer pour la démo)")

    def repaint_graphs(self):
        for c in self.cards:
            c.small.update()
            c.big.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), BG)


def main():
    app = QApplication(sys.argv)
    g = Gallery()
    g.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
