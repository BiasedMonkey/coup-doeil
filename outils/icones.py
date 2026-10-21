#!/usr/bin/env python3
"""Génère les propositions d'icône de Coup d'œil (SVG) et une planche d'aperçu."""

import colorsys
import math
import os
import sys
from pathlib import Path

OUT = Path(__file__).parent / "icones"


def heat(v):
    """Même dégradé que l'appli : cyan (0) → vert → jaune → rouge (1)."""
    v = max(0.0, min(1.0, v))
    r, g, b = colorsys.hsv_to_rgb(0.53 * (1 - v) ** 1.3, 0.75, 0.98)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def frame(body):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" height="128">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#232733"/><stop offset="1" stop-color="#12141a"/>
    </linearGradient>
  </defs>
  <rect x="6" y="6" width="116" height="116" rx="26" fill="url(#bg)"/>
  <rect x="6.5" y="6.5" width="115" height="115" rx="25.5" fill="none" stroke="#ffffff" stroke-opacity="0.08"/>
{body}
</svg>
"""


def dot(x, y, r, color, opacity=1.0):
    return f'  <circle cx="{x:.2f}" cy="{y:.2f}" r="{r:.2f}" fill="{color}" fill-opacity="{opacity:.2f}"/>'


DIM = "#ffffff"


def icon_perles():
    """A — la courbe perlée de l'appli."""
    out = []
    curve = lambda x: 0.42 + 0.22 * math.sin((x - 20) / 15) + 0.12 * math.sin((x - 20) / 6.5)
    step, bottom, top = 7.0, 100, 30
    x = 22.0
    while x <= 106:
        v = curve(x)
        y_curve = bottom - v * (bottom - top)
        y = bottom
        while y > top - 1:
            if y > y_curve + 4:
                k = (bottom - y) / max(1, bottom - y_curve)
                out.append(dot(x, y, 1.1 + 1.0 * k, heat((bottom - y) / (bottom - top)), 0.25 + 0.5 * k))
            elif y <= y_curve - 4:
                out.append(dot(x, y, 0.9, DIM, 0.10))
            y -= step
        out.append(dot(x, y_curve, 3.4, heat(v)))
        x += step
    return frame("\n".join(out))


def icon_led():
    """B — matrice LED : barres de points égaux."""
    out = []
    heights = [3, 5, 4, 7, 6, 4]
    cols, rows = len(heights), 7
    x0, y0, pitch = 27, 99, 12.2
    for c, hgt in enumerate(heights):
        for r in range(rows):
            x, y = x0 + c * pitch, y0 - r * pitch
            if r < hgt:
                out.append(dot(x, y, 4.4, heat((r + 1) / rows)))
            else:
                out.append(dot(x, y, 2.0, DIM, 0.12))
    return frame("\n".join(out))


def icon_jauge():
    """C — compteur : arc de points, aiguille en perle."""
    out = []
    cx, cy, rad, n = 64, 70, 38, 25
    fill = 0.72
    for i in range(n):
        a = math.radians(225 - 270 * i / (n - 1))
        x, y = cx + rad * math.cos(a), cy - rad * math.sin(a)
        f = i / (n - 1)
        if f <= fill:
            out.append(dot(x, y, 3.6, heat(f)))
        else:
            out.append(dot(x, y, 1.8, DIM, 0.15))
    a = math.radians(225 - 270 * fill)
    for k in range(1, 6):                                   # aiguille en pointillés
        t = k / 6
        out.append(dot(cx + rad * 0.72 * t * math.cos(a), cy - rad * 0.72 * t * math.sin(a),
                       1.4 + 0.5 * t, heat(fill), 0.5 + 0.5 * t))
    out.append(dot(cx, cy, 6.5, "#e6e8ee"))
    out.append(dot(cx, cy, 3.0, heat(fill)))
    return frame("\n".join(out))


def icon_oeil():
    """D — « coup d'œil » : un œil en points, iris en dégradé."""
    out = []
    cx, cy = 64, 64
    for i in range(28):                                     # contour en amande
        t = i / 28 * 2 * math.pi
        x = cx + 46 * math.cos(t)
        y = cy + 27 * math.sin(t) * (abs(math.cos(t)) ** 0.0) * (1 - 0.25 * abs(math.cos(t)))
        out.append(dot(x, y, 2.0, "#e6e8ee", 0.75))
    for i in range(20):                                     # iris en dégradé
        t = i / 20 * 2 * math.pi - math.pi / 2
        out.append(dot(cx + 17 * math.cos(t), cy + 17 * math.sin(t), 2.9, heat(i / 19)))
    for i in range(10):
        t = i / 10 * 2 * math.pi
        out.append(dot(cx + 9.5 * math.cos(t), cy + 9.5 * math.sin(t), 1.6, heat(0.15), 0.6))
    out.append(dot(cx, cy, 5.5, "#0b0c10"))
    out.append(dot(cx + 3.5, cy - 3.5, 2.0, "#ffffff", 0.9))
    return frame("\n".join(out))


def icon_puce():
    """E — puce : un processeur en points avec une courbe perlée dedans."""
    out = []
    x0, y0, size = 34, 34, 60
    out.append(f'  <rect x="{x0}" y="{y0}" width="{size}" height="{size}" rx="10" fill="#1b1e27" '
               'stroke="#e6e8ee" stroke-opacity="0.55" stroke-width="2"/>')
    for k in range(5):                                      # broches en points
        p = x0 + 10 + k * 10
        for (x, y) in [(p, y0 - 8), (p, y0 + size + 8), (x0 - 8, p), (x0 + size + 8, p)]:
            out.append(dot(x, y, 2.3, "#e6e8ee", 0.55))
    curve = lambda t: 0.35 + 0.3 * math.sin(t * 5.2 - 1) + 0.1 * math.sin(t * 13)
    for i in range(9):
        t = i / 8
        v = max(0.05, min(0.95, curve(t)))
        x = x0 + 10 + t * (size - 20)
        y = y0 + size - 10 - v * (size - 20)
        out.append(dot(x, y, 3.0, heat(v)))
    return frame("\n".join(out))


ICONS = {
    "A-perles": ("A — Courbe perlée", icon_perles),
    "B-led": ("B — Matrice LED", icon_led),
    "C-jauge": ("C — Compteur", icon_jauge),
    "D-oeil": ("D — Coup d'œil", icon_oeil),
    "E-puce": ("E — Puce", icon_puce),
}


def main():
    OUT.mkdir(exist_ok=True)
    for key, (_, fn) in ICONS.items():
        (OUT / f"{key}.svg").write_text(fn())
    if len(sys.argv) > 1:
        preview(Path(sys.argv[1]))


def preview(path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    cell, n = 190, len(ICONS)
    img = QImage(cell * n, 250, QImage.Format_ARGB32)
    img.fill(QColor("#2b2f3a"))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    f = QFont()
    f.setPixelSize(14)
    f.setBold(True)
    p.setFont(f)
    for i, (key, (title, _)) in enumerate(ICONS.items()):
        r = QSvgRenderer(str(OUT / f"{key}.svg"))
        x = i * cell
        r.render(p, QRectF(x + 31, 16, 128, 128))
        for j, s in enumerate((48, 32, 22)):                   # tailles barre des tâches / menu
            r.render(p, QRectF(x + 30 + j * 52, 160 + (48 - s) / 2, s, s))
        p.setPen(QColor("#e6e8ee"))
        p.drawText(QRectF(x, 218, cell, 24), Qt.AlignCenter, title)
    p.end()
    img.save(str(path))


if __name__ == "__main__":
    main()
