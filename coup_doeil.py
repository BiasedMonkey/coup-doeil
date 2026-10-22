#!/usr/bin/env python3
"""Coup d'œil — moniteur système toujours-au-dessus (CPU / GPU / RAM / batterie).

Vue compacte pour un coin de l'écran, vue détaillée (bouton ⤢) pour tout voir.
"""

import json
import math
import os
import struct
import sys
import time
from collections import defaultdict, deque
from glob import glob
from pathlib import Path

# Sous Wayland, une appli ne peut ni se positionner ni se forcer au-dessus :
# on passe par XWayland où WindowStaysOnTopHint fonctionne.
if os.environ.get("XDG_SESSION_TYPE") == "wayland" and os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QGridLayout, QHBoxLayout,
                               QLabel, QLayout, QPushButton, QSizePolicy, QSlider, QVBoxLayout,
                               QWidget)

HISTORY = 90          # secondes gardées en mémoire (vue détaillée)
COMPACT_COLS = 60     # secondes affichées en vue compacte
REFRESH_MS = 1000
EDGE = 7              # largeur (px) des bords qui servent à redimensionner
ANIM_MS = 33          # ~30 images/s : le graphe ne glisse que de ~4 px/s


# ───────────────────────────── lecture capteurs ─────────────────────────────
# Les fichiers lus chaque seconde restent ouverts : os.pread(fd, n, 0) relit une
# valeur fraîche (sysfs et /proc régénèrent le contenu à l'offset 0) sans les
# appels open/close, ~4× plus rapide. Plus besoin de psutil.

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


def read(path, conv=float, default=None):
    """Lecture ponctuelle (détection au démarrage)."""
    try:
        with open(path) as f:
            return conv(f.read().strip())
    except (OSError, ValueError):
        return default


class Node:
    """Fichier gardé ouvert et relu à la demande."""

    __slots__ = ("fd", "size")

    def __init__(self, path, size=128):
        self.size = size
        try:
            self.fd = os.open(path, os.O_RDONLY)
        except OSError:
            self.fd = None

    def __bool__(self):
        return self.fd is not None

    def raw(self):
        if self.fd is None:
            return None
        try:
            return os.pread(self.fd, self.size, 0)
        except OSError:          # ex. capteur d'un GPU en veille
            return None

    def text(self):
        b = self.raw()
        return None if b is None else b.decode().strip()

    def num(self, scale=1.0):
        b = self.raw()
        try:
            return float(b) / scale if b else None
        except ValueError:
            return None


def hwmon_by_name(name):
    for d in sorted(glob("/sys/class/hwmon/hwmon*")):
        if read(f"{d}/name", str) == name:
            yield d


def short_name(name):
    """« AMD Ryzen 7 7840HS w/ Radeon 780M Graphics » → « Ryzen 7 7840HS »."""
    name = name.split(" w/ ")[0]
    for junk in ("AMD ", " Graphics"):
        name = name.replace(junk, "")
    return name.strip()


def cpu_info():
    """(nom, nombre de cœurs physiques) depuis /proc/cpuinfo."""
    name, cores, phys = "CPU", set(), None
    try:
        for line in open("/proc/cpuinfo"):
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            if key == "model name":
                name = val
            elif key == "physical id":
                phys = val
            elif key == "core id":
                cores.add((phys, val))
    except OSError:
        pass
    return name, len(cores) or os.cpu_count()


def amdgpu_name(dev):
    """Nom commercial via la base de Mesa (id + révision PCI)."""
    did = read(dev / "device", str, "0x")[2:].upper()
    rev = read(dev / "revision", str, "0x")[2:].upper()
    try:
        for line in open("/usr/share/libdrm/amdgpu.ids"):
            parts = [p.strip() for p in line.split(",", 2)]
            if len(parts) == 3 and parts[0].upper() == did and parts[1].upper() == rev:
                return parts[2]
    except OSError:
        pass
    return f"AMD GPU {did}"


class CpuSensor:
    def __init__(self):
        self.name, self.cores = cpu_info()
        self.threads = os.cpu_count()
        self.max_freq = read("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", default=0) / 1e6
        self.stat = Node("/proc/stat", 65536)
        self.freq_nodes = [Node(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq")
                           for i in range(self.threads)]
        rapl = next((d for d in glob("/sys/class/powercap/intel-rapl:*")
                     if read(f"{d}/name", str) == "package-0"), None)
        self.energy = Node(f"{rapl}/energy_uj") if rapl else None
        k10 = next(hwmon_by_name("k10temp"), None)
        self.temp_node = Node(f"{k10}/temp1_input") if k10 else None
        self._last_energy = None
        self._last_times = self.times()

    def times(self):
        """[(occupé, total)] par thread, en ticks, depuis /proc/stat."""
        out = []
        for line in (self.stat.raw() or b"").split(b"\n")[1:]:
            if not line.startswith(b"cpu"):
                break
            v = [int(x) for x in line.split()[1:9]]
            total = sum(v)
            out.append((total - v[3] - v[4], total))      # moins idle et iowait
        return out

    def power(self):
        e = self.energy.num() if self.energy else None
        if e is None:
            return None
        t = time.monotonic()
        prev, self._last_energy = self._last_energy, (e, t)
        if not prev or e < prev[0]:      # premier tour ou compteur rebouclé
            return None
        return (e - prev[0]) / 1e6 / (t - prev[1])

    def sample(self):
        now = self.times()
        cores = [100 * (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0
                 for (b0, t0), (b1, t1) in zip(self._last_times, now)]
        self._last_times = now
        freqs = [(n.num(1e6) or 0) for n in self.freq_nodes]
        valid = [f for f in freqs if f] or [0]
        temp = self.temp_node.num(1000) if self.temp_node else None
        return {
            "load": sum(cores) / len(cores) if cores else 0,
            "cores": cores,
            "freqs": freqs,
            "freq": max(valid),
            "freq_avg": sum(valid) / len(valid),
            "temp": temp,
            "power": self.power(),
            "loadavg": os.getloadavg(),
        }


class ApuMetrics:
    """Bloc binaire gpu_metrics v2.x des APU AMD : puissance par partie de la puce.

    Disposition (amdgpu, struct gpu_metrics_v2_x) : en-tête de 4 octets, puis à
    l'octet 40 quatre uint16 en mW — socket (puce entière), cpu, soc, gfx —
    et à l'octet 48 les 8 cœurs. 0xFFFF = non fourni. Les valeurs bougent vite :
    lissage exponentiel pour un affichage lisible.
    """

    SMOOTH = {"socket": 0.5, "cores": 0.4, "soc": 0.4, "gfx": 0.25}   # gfx saute beaucoup

    def __init__(self, path):
        self.node = Node(path, 256)
        self.state = {}
        b = self.node.raw()
        self.ok = bool(b) and len(b) >= 64 and b[2] == 2

    def read(self):
        b = self.node.raw()
        if not b or len(b) < 64 or b[2] != 2:
            return None
        sock, _cpu, soc, gfx = struct.unpack_from("<4H", b, 40)
        cores = [c for c in struct.unpack_from("<8H", b, 48) if c != 0xFFFF]
        raw = {"socket": sock, "soc": soc, "gfx": gfx, "cores": sum(cores) if cores else 0xFFFF}
        out = {}
        for key, mw in raw.items():
            if mw == 0xFFFF:
                out[key] = None
                continue
            w = mw / 1000
            prev = self.state.get(key)
            self.state[key] = w if prev is None else prev + (w - prev) * self.SMOOTH[key]
            out[key] = self.state[key]
        return out


class GpuSensor:
    def __init__(self, card):
        self.dev = Path(card) / "device"
        self.pdev = self.dev.resolve().name          # ex. 0000:03:00.0
        hw = glob(f"{self.dev}/hwmon/hwmon*")
        self.hw = hw[0] if hw else None
        self.vram_total = read(self.dev / "mem_info_vram_total", int, 0)
        self.gtt_total = read(self.dev / "mem_info_gtt_total", int, 0)
        self.name = amdgpu_name(self.dev)
        self.integrated = "Graphics" in self.name or self.vram_total < 2**30
        self.metrics = None
        if self.integrated:
            m = ApuMetrics(self.dev / "gpu_metrics")
            self.metrics = m if m.ok else None
        node = lambda name: Node(self.dev / name)
        hwn = lambda name: Node(f"{self.hw}/{name}") if self.hw else Node("/nonexistent")
        self.runtime = node("power/runtime_status")
        self.busy, self.vram, self.gtt = node("gpu_busy_percent"), node("mem_info_vram_used"), node("mem_info_gtt_used")
        self.link_speed, self.link_width = node("current_link_speed"), node("current_link_width")
        self.sclk, self.mclk = hwn("freq1_input"), hwn("freq2_input")
        self.pavg, self.pinput, self.pcap = hwn("power1_average"), hwn("power1_input"), hwn("power1_cap")
        self.fan, self.volt = hwn("fan1_input"), hwn("in0_input")
        self.temps = []                               # [(étiquette, nœud)] trouvés une fois
        for f in sorted(glob(f"{self.hw}/temp*_input")) if self.hw else []:
            self.temps.append((read(f.replace("_input", "_label"), str) or Path(f).stem, Node(f)))

    @staticmethod
    def detect():
        cards = [GpuSensor(c) for c in sorted(glob("/sys/class/drm/card[0-9]"))
                 if read(f"{c}/device/vendor", str) == "0x1002"]   # AMD
        return sorted(cards, key=lambda g: (g.integrated, -g.vram_total))  # dGPU d'abord

    def sample(self):
        # Ne pas réveiller un dGPU en veille (runtime PM) juste pour le lire.
        if self.runtime.text() == "suspended":
            return {"asleep": True}
        temps = {label: v / 1000 for label, n in self.temps if (v := n.num()) is not None}
        speed, width = self.link_speed.text(), self.link_width.text()
        power = self.pavg.num(1e6)
        return {
            "asleep": False,
            "load": self.busy.num() or 0,
            "freq": self.sclk.num(1e9),                   # GHz
            "mclk": self.mclk.num(1e9),
            "power": power if power is not None else self.pinput.num(1e6),
            "cap": self.pcap.num(1e6),
            "temps": temps,
            "temp": temps.get("junction") or temps.get("edge"),
            "fan": self.fan.num(),
            "volt": self.volt.num(),
            "link": f"{speed.split(' PCIe')[0]} ×{width}" if speed and width else None,
            "vram": int(self.vram.num() or 0),
            "vram_total": self.vram_total,
            "gtt": int(self.gtt.num() or 0),
            "gtt_total": self.gtt_total,
        }


class RamSensor:
    def __init__(self):
        self.meminfo = Node("/proc/meminfo", 8192)
        self.temp_nodes = [Node(f"{d}/temp1_input") for d in hwmon_by_name("spd5118")]
        self.total = self.read_meminfo()["MemTotal"]
        self.temps, self.temps_at = [], 0.0

    TEMP_EVERY = 5.0   # s — la sonde des barrettes passe par l'I²C : ~1 ms par lecture

    def read_temps(self):
        if time.monotonic() - self.temps_at >= self.TEMP_EVERY:
            self.temps = [t for t in (n.num(1000) for n in self.temp_nodes) if t]
            self.temps_at = time.monotonic()
        return self.temps

    def read_meminfo(self):
        out = {}
        for line in (self.meminfo.raw() or b"").split(b"\n"):
            key, _, rest = line.partition(b":")
            if key in (b"MemTotal", b"MemFree", b"MemAvailable", b"SwapTotal", b"SwapFree"):
                out[key.decode()] = int(rest.split()[0]) * 1024
        return out

    def sample(self):
        m = self.read_meminfo()
        total, avail, free = m["MemTotal"], m["MemAvailable"], m["MemFree"]
        temps = self.read_temps()
        return {
            "load": (total - avail) / total * 100,
            "used": total - avail,
            "cache": max(0, avail - free),             # cache récupérable
            "free": free,
            "available": avail,
            "total": total,
            "swap": m["SwapTotal"] - m["SwapFree"],
            "swap_total": m["SwapTotal"],
            "temps": temps,
            "temp": max(temps) if temps else None,
        }


class SysSensor:
    STATUS = {"Discharging": "sur batterie", "Charging": "en charge",
              "Full": "pleine", "Not charging": "branché"}

    def __init__(self):
        self.bat = next(iter(sorted(glob("/sys/class/power_supply/BAT*"))), None)
        self.nodes = {}
        if self.bat:
            for name in ("status", "capacity", "current_now", "voltage_now", "power_now",
                         "charge_now", "charge_full", "energy_now", "energy_full"):
                n = Node(f"{self.bat}/{name}")
                if n:
                    self.nodes[name] = n
        btime = next((line for line in open("/proc/stat") if line.startswith("btime")), "btime 0")
        self.boot = int(btime.split()[1])

    def battery(self):
        if not self.bat:
            return None
        f = lambda name: self.nodes[name].num() if name in self.nodes else None
        status = self.nodes["status"].text() if "status" in self.nodes else ""
        cur, volt, pw = f("current_now"), f("voltage_now"), f("power_now")
        watts = abs(pw) / 1e6 if pw else (abs(cur * volt) / 1e12 if cur and volt else None)
        # temps restant : en µAh/µA (charge_*) ou en µWh/µW (energy_*)
        now, full, rate = f("charge_now"), f("charge_full"), abs(cur or 0)
        if now is None:
            now, full, rate = f("energy_now"), f("energy_full"), abs(pw or 0)
        left = None
        if rate and now is not None:
            if status == "Discharging":
                left = now / rate * 3600
            elif status == "Charging" and full:
                left = max(0, full - now) / rate * 3600
        return {"percent": f("capacity") or 0, "status": status,
                "label": self.STATUS.get(status, (status or "").lower()),
                "watts": watts, "left": left}

    def sample(self):
        return {"uptime": time.time() - self.boot, "battery": self.battery()}


class ProcSampler:
    """Top programmes CPU / RAM / mémoire GPU (vue détaillée seulement).

    Un seul fichier lu par processus (/proc/PID/stat : nom, temps CPU, RSS).
    Les descripteurs DRM (/dev/dri) ne sont recherchés que toutes les 10 s ;
    entre-temps on relit seulement leurs fdinfo.
    """

    RESCAN = 10.0

    def __init__(self):
        self.prev, self.t = {}, None
        self.names = {}
        self.drm_fds, self.drm_scan = [], 0.0

    def sample(self):
        now = time.monotonic()
        dt = now - self.t if self.t else None
        cpu, mem, cur, names = defaultdict(float), defaultdict(int), {}, {}
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/stat", "rb") as f:
                    data = f.read()
            except OSError:
                continue
            r = data.rfind(b")")
            name = data[data.find(b"(") + 1:r].decode(errors="replace")
            fields = data[r + 2:].split()
            ticks = int(fields[11]) + int(fields[12])       # utime + stime
            names[pid] = name
            cur[pid] = ticks
            if dt and pid in self.prev:
                cpu[name] += (ticks - self.prev[pid]) / CLK_TCK / dt * 100
            mem[name] += int(fields[21]) * PAGE             # RSS
        self.prev, self.t, self.names = cur, now, names
        top = lambda d, n: sorted(d.items(), key=lambda kv: -kv[1])[:n]
        return {"cpu": top(cpu, 5), "mem": top(mem, 6), "gpu": self.gpu_memory(), "count": len(cur)}

    def scan_drm(self):
        fds = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                for fd in os.listdir(f"/proc/{pid}/fd"):
                    try:
                        if os.readlink(f"/proc/{pid}/fd/{fd}").startswith("/dev/dri/"):
                            fds.append((pid, fd))
                    except OSError:
                        pass
            except OSError:
                continue
        self.drm_fds, self.drm_scan = fds, time.monotonic()

    def gpu_memory(self):
        """{pdev: [(programme, vram, gtt)]} via les fdinfo des nœuds DRM."""
        if time.monotonic() - self.drm_scan > self.RESCAN:
            self.scan_drm()
        seen, alive = set(), []
        usage = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for pid, fd in self.drm_fds:
            try:
                with open(f"/proc/{pid}/fdinfo/{fd}", "rb") as f:
                    text = f.read()
            except OSError:
                continue                                      # programme fermé
            alive.append((pid, fd))
            info = {}
            for line in text.split(b"\n"):
                k, _, v = line.partition(b":")
                info[k] = v.strip()
            key = (info.get(b"drm-pdev"), info.get(b"drm-client-id"))
            if key[1] is None or key in seen:                 # un client DRM = un seul compte
                continue
            seen.add(key)
            kib = lambda k: int(info.get(k, b"0").split()[0]) * 1024
            slot = usage[key[0].decode()][self.names.get(pid, pid)]
            slot[0] += kib(b"drm-memory-vram")
            slot[1] += kib(b"drm-memory-gtt")
        self.drm_fds = alive
        return {dev: sorted(((n, v, g) for n, (v, g) in progs.items() if v + g),
                            key=lambda x: -(x[1] + x[2]))
                for dev, progs in usage.items()}


# ───────────────────────────────── dessin ─────────────────────────────────

BG = QColor(18, 20, 26, 235)
PANEL = QColor(255, 255, 255, 10)
DIM = QColor(255, 255, 255, 22)
TEXT = "#e6e8ee"
MUTED = "#8a90a0"


def heat_color(v):
    """Couleur d'un point selon sa valeur v ∈ [0, 100]."""
    # Dégradé cyan (0 %) → vert → jaune → rouge (100 %)
    v = max(0.0, min(100.0, v)) / 100
    return QColor.fromHsvF(0.53 * (1 - v) ** 1.3, 0.75, 0.98)


def over(top, bottom):
    """Couleur obtenue en posant top (semi-transparent) sur bottom."""
    ta, ba = top.alphaF(), bottom.alphaF()
    a = ta + ba * (1 - ta)
    mix = lambda t, b: (t * ta + b * ba * (1 - ta)) / a
    return QColor.fromRgbF(mix(top.redF(), bottom.redF()), mix(top.greenF(), bottom.greenF()),
                           mix(top.blueF(), bottom.blueF()), a)


def faded(c, alpha):
    c = QColor(c)
    c.setAlphaF(alpha)
    return c


PART_COLORS = {
    "CPU": QColor("#5ac8fa"), "iGPU": QColor("#c084fc"), "SoC": QColor("#facc15"),
    "Puce": QColor("#f472b6"), "dGPU": QColor("#fb923c"), "Reste": QColor("#64748b"),
}
GRAPH_BG = over(PANEL, BG)   # fond réel sous les graphes (carte posée sur la fenêtre)
DRAG_TINT = QColor(90, 200, 250, 28)
DRAG_BG = over(DRAG_TINT, BG)  # même chose pendant qu'on déplace la carte


def set_background_opacity(pct):
    """Opacité du fond de la fenêtre ; le texte et les courbes restent opaques."""
    global BG, GRAPH_BG, DRAG_BG
    BG = QColor(18, 20, 26, round(255 * pct / 100))
    GRAPH_BG = over(PANEL, BG)
    DRAG_BG = over(DRAG_TINT, BG)


def temp_norm(t):
    """30 °C → 0, 100 °C → 100 : les graphes de température se colorent tôt."""
    return (t - 30) / 70 * 100


PITCH = 3.0   # écart entre deux points fins, en pixels


def paint_column(p, x, h, dy, rows, level, rmax, alpha=1.0, stagger=0.0):
    """Une colonne de points en trame, remplie jusqu'à level ∈ [0, 1].

    Le point du sommet est partiel et les points rétrécissent vers le bas :
    la taille des points porte le dégradé, comme une trame d'imprimerie.
    """
    top = level * rows
    for row in range(rows):
        y = h - (row + 0.5 + stagger) * dy
        if row < top:
            s = min(1.0, top - row)                          # sommet partiel
            depth = (row + 1) / top                          # 1 au sommet, ~0 en bas
            s *= 0.25 + 0.75 * depth                         # plus fin vers le bas
            p.setBrush(faded(heat_color((row + 1) / rows * 100), alpha * (0.45 + 0.55 * depth)))
            r = rmax * (0.3 + 0.7 * s)
        else:
            p.setBrush(DIM)
            r = rmax * 0.38
        p.drawEllipse(QPointF(x, y), r, r)


SMOOTH_PASSES = 2     # passes de lissage avant la courbe (0 = suit chaque pic)
FADE = 0.35           # durée du fondu entre deux courbes (fraction de seconde)
FADE_COLS = 5         # colonnes de droite que le lissage peut modifier


def soften(vals, passes=SMOOTH_PASSES):
    """Lissage léger [1, 2, 1] / 4, répété : arrondit les pics isolés."""
    v = [x or 0.0 for x in vals]
    n = len(v)
    for _ in range(passes):
        v = [(v[max(i - 1, 0)] + 2 * v[i] + v[min(i + 1, n - 1)]) / 4 for i in range(n)]
    return v


def bspline(v, t):
    """B-spline cubique uniforme à l'indice fractionnaire t.

    Contrairement à une interpolation, la courbe frôle les points sans passer
    exactement dessus : elle n'a jamais d'angle (courbure continue).
    """
    i = math.floor(t)
    u = t - i
    pt = lambda k: v[max(0, min(len(v) - 1, k))]
    p0, p1, p2, p3 = pt(i - 1), pt(i), pt(i + 1), pt(i + 2)
    u2, u3 = u * u, u * u * u
    return ((1 - u) ** 3 * p0 + (3 * u3 - 6 * u2 + 4) * p1
            + (-3 * u3 + 3 * u2 + 3 * u + 1) * p2 + u3 * p3) / 6


def grid_pitch(dpr):
    """Écart entre les points des courbes, arrondi à un nombre ENTIER de pixels
    d'écran (4 px réels à 125 %). Sinon chaque point tombe à une position
    différente entre deux pixels, est dessiné un peu différemment de son voisin,
    et l'ensemble forme un moiré qui ondule pendant le défilement."""
    return max(3, round(PITCH * dpr)) / dpr


def pearl_layout(h, pitch):
    """Rangées espacées exactement d'un pas de grille, calées sur le bas."""
    return max(4, int(h / pitch)), pitch


def pearl_points(w, h, vals, dx, x0, x_true, pitch, passes=SMOOTH_PASSES):
    """Calcule la courbe perlée (une fois par seconde) sans rien dessiner.

    Colonnes de points en x0 + c·pitch ; la courbe y est évaluée en x_true + c·pitch
    (l'écart est rattrapé au moment de l'affichage). Renvoie :
      • groups  : {(couleur, rayon): [points]} → un drawPoints par groupe ;
      • outline : haut de la poussière par colonne, pour découper le fond.
    """
    rows, dy = pearl_layout(h, pitch)
    smooth = soften(vals, passes)
    raw = defaultdict(list)
    outline = []
    ys = [h - (row + 0.5) * dy for row in range(rows)]
    c = 0
    x = x0
    while x < w + pitch:
        v = bspline(smooth, (x_true + c * pitch) / dx - 0.5)
        v = 0.0 if v < 0 else 1.0 if v > 1 else v
        top = v * rows
        full = int(top - 1) + 1 if top >= 1 else 0          # rangées de poussière
        for row in range(full):
            k = round((row + 1) / top * 8)                  # 0 en bas → 8 près de la courbe
            raw[(row, k)].append(QPointF(x, ys[row]))
        if top > 0:                                          # la perle, à la hauteur exacte
            y = max(dy / 2, min(h - dy / 2, h - top * dy + dy / 2))
            raw[("perle", round(v * 50))].append(QPointF(x, y))
            covered = min(rows, int(math.ceil(top)))
        else:
            covered = 0
        outline.append((x, h - covered * dy))                # fond visible au-dessus
        x += pitch
        c += 1
    groups = []
    for key, pts in raw.items():
        if key[0] == "perle":
            color, r = heat_color(key[1] * 2), 1.6
        else:
            row, k = key
            color, r = faded(heat_color((row + 1) / rows * 100), 0.18 + 0.5 * k / 8), 0.9 + 0.35 * k / 8
        groups.append((color, r, pts))
    return groups, outline


def draw_points(p, groups):
    pen = QPen()
    pen.setCapStyle(Qt.RoundCap)
    for color, r, pts in groups:
        pen.setColor(color)
        pen.setWidthF(2 * r)
        p.setPen(pen)
        p.drawPoints(pts)


# Positions intermédiaires par pixel d'écran. 1 = le défilement avance pixel
# d'écran par pixel d'écran : chaque point garde exactement la même forme à tout
# instant. Avec des positions entre deux pixels (2, 4…), les points d'une grille
# régulière deviennent tous nets puis tous flous en même temps : l'image respire.
PHASES = 1


class Frame:
    """La courbe d'une seconde : calculée une fois, tracée à la demande dans
    chacune des PHASES positions (décalées d'un quart de pixel d'écran).
    Tracer à la demande étale le travail sur plusieurs images d'animation au
    lieu de tout faire d'un coup quand une mesure arrive."""

    def __init__(self, groups, clip, tiles, tw, n, frac, size, dpr):
        self.groups, self.clip, self.tiles, self.tw = groups, clip, tiles, tw
        self.n, self.frac, self.size, self.dpr = n, frac, size, dpr
        self.images = [None] * PHASES

    def image(self, k):
        if self.images[k] is None:
            W, h, dpr = self.size[0], self.size[1], self.dpr
            pm = QPixmap(math.ceil(W * dpr), math.ceil(h * dpr))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.translate(-k / PHASES / dpr, 0)    # même tracé, décalé d'un quart de pixel
            draw_points(p, self.groups)
            p.setClipPath(self.clip)
            p.resetTransform()                   # le motif est copié au pixel entier ;
            p.drawTiledPixmap(QRectF(0, 0, W, h), self.tiles[k],   # sa version k est
                              QPointF((self.n / dpr) % self.tw, 0))  # déjà décalée
            p.end()
            self.images[k] = pm
        return self.images[k]


class DotGraph(QWidget):
    """Historique en courbe perlée qui défile en continu.

    Coût réparti ainsi :
      • 1×/s  : calcul de la courbe, puis tracé dans PHASES images décalées chacune
                d'un quart de pixel d'écran. Chaque image est un vrai tracé vectoriel
                (et non une image floutée par interpolation) : toutes ont la même
                netteté, donc aucun scintillement pendant le défilement ;
      • 30×/s : simple copie de la bonne image au pixel entier (paintEvent), et
                seulement si la position affichée a réellement changé (frame_key).
    """

    def __init__(self, source, cols, clock, height=46, norm=None):
        super().__init__()
        self.source, self.cols, self.clock, self.norm = source, cols, clock, norm
        self.cache = self.cache_key = self.prev = None     # Frame courant / précédent
        self.dust_key = self.dust = None
        self.shown = None
        # On peint notre propre fond : Qt n'a plus à redessiner la fenêtre
        # translucide derrière à chaque image de l'animation.
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.base_height = height
        self.setMinimumHeight(round(height * 0.6))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def sizeHint(self):
        return QSize(200, self.base_height)

    def dust_tiles(self, h, dpr):
        """Motif de fond (points gris), une version par phase, dessiné une fois par taille."""
        key = (h, dpr)
        if key != self.dust_key:
            # largeur du motif = m colonnes, choisie pour tomber sur un nombre entier
            # de pixels d'écran (sinon le motif se décalerait en se répétant)
            pitch = grid_pitch(dpr)                  # nombre entier de pixels d'écran
            m = 4
            tw = m * pitch
            rows, dy = pearl_layout(h, pitch)
            pen = QPen(DIM, 1.15)
            pen.setCapStyle(Qt.RoundCap)
            tiles = []
            for k in range(PHASES):
                tile = QPixmap(round(tw * dpr), math.ceil(h * dpr))
                tile.setDevicePixelRatio(dpr)
                tile.fill(Qt.transparent)
                q = QPainter(tile)
                q.setRenderHint(QPainter.Antialiasing)
                q.setPen(pen)
                off = -k / PHASES / dpr
                q.drawPoints([QPointF(pitch / 2 + j * pitch + off, h - (r + 0.5) * dy)
                              for j in range(m) for r in range(rows)])
                q.end()
                tiles.append(tile)
            self.dust_key, self.dust = key, (tiles, tw)
        return self.dust

    def values(self):
        vals = []
        for v in list(self.source)[-(self.cols + 1):]:     # +1 : la colonne qui entre
            if v is not None:
                v = max(0.0, min(100.0, self.norm(v) if self.norm else v)) / 100
            vals.append(v)
        return vals

    def render(self):
        w, h = self.width(), self.height()
        dx = w / self.cols
        dpr = self.devicePixelRatioF()
        # Grille ancrée sur le temps écoulé : la poussière ne saute pas à chaque seconde.
        # On la place sur un nombre ENTIER de pixels d'écran (n) ; le reste (frac)
        # est rattrapé à l'affichage.
        pitch = grid_pitch(dpr)
        phase = (self.clock.tick * dx) % pitch * dpr
        n = math.floor(phase)
        frac = phase - n
        x0 = pitch / 2 - n / dpr
        W = w + dx + pitch
        groups, outline = pearl_points(W - pitch, h, self.values(), dx, x0, x0 - frac / dpr, pitch,
                                       self.clock.smooth_passes)
        clip = QPainterPath()                    # le fond n'apparaît qu'au-dessus de la courbe
        clip.moveTo(0, 0)
        for x, y in outline:
            clip.lineTo(x - pitch / 2, y)
            clip.lineTo(x + pitch / 2, y)
        clip.lineTo(W, 0)
        clip.closeSubpath()
        tiles, tw = self.dust_tiles(h, dpr)
        return Frame(groups, clip, tiles, tw, n, frac, (W, h), dpr)

    def shift(self):
        if not self.clock.smooth_scroll:
            return 1.0          # sans défilement fluide : la courbe avance d'un cran par mesure
        return min(1.0, (time.monotonic() - self.clock.last_sample) * 1000 / self.clock.refresh_ms)

    def offset(self, extra_cols, frac):
        """Décalage à l'écran → (pixels entiers, phase)."""
        total = (extra_cols + self.shift()) * self.width() / self.cols * self.devicePixelRatioF() + frac
        whole = math.floor(total)
        k = round((total - whole) * PHASES)
        return whole + k // PHASES, k % PHASES

    def frame_key(self):
        """Ce qui serait affiché maintenant : si rien ne change, inutile de repeindre."""
        if self.cache_key != (self.clock.tick, self.width(), self.height()):
            return None
        s = self.shift()
        fading = self.prev is not None and s < FADE
        return self.offset(0, self.cache.frac) + (round(s / FADE * 16) if fading else -1,)

    def paintEvent(self, _):
        key = (self.clock.tick, self.width(), self.height())
        if key != self.cache_key:
            same_size = self.cache_key and self.cache_key[1:] == key[1:]
            self.prev = self.cache if same_size else None
            self.cache, self.cache_key = self.render(), key
        whole, k, fade = self.shown = self.frame_key()
        dpr = self.devicePixelRatioF()
        w, h, dxs = self.width(), self.height(), self.width() / self.cols
        bg = DRAG_BG if getattr(self.parentWidget(), "dragging", False) else GRAPH_BG
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(self.rect(), bg)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        p.drawPixmap(QPointF(-whole / dpr, 0), self.cache.image(k))   # copie au pixel entier
        if fade < 0:
            return
        # Le lissage modifie le bout droit de la courbe quand une mesure arrive.
        # Fondu enchaîné exact sur ces colonnes : ancienne × (1 − f) + nouvelle × f,
        # mélangées en mode « Plus » → la quantité de lumière reste constante
        # (superposer simplement les deux courbes faisait pulser le bout droit).
        f = fade / 16
        pw, pk = self.offset(1, self.prev.frac)                       # une colonne d'avance
        edge = math.floor((w - FADE_COLS * dxs) * dpr) / dpr          # bord au pixel entier
        strip = QPixmap(math.ceil((w - edge) * dpr) + 1, math.ceil(h * dpr))
        strip.setDevicePixelRatio(dpr)
        strip.fill(Qt.transparent)
        q = QPainter(strip)
        q.setOpacity(1 - f)
        q.drawPixmap(QPointF(-pw / dpr - edge, 0), self.prev.image(pk))
        q.setCompositionMode(QPainter.CompositionMode_Plus)
        q.setOpacity(f)
        q.drawPixmap(QPointF(-whole / dpr - edge, 0), self.cache.image(k))
        q.end()
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(QRectF(edge, 0, w - edge, h), bg)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        p.drawPixmap(QPointF(edge, 0), strip)

    def tick_anim(self):
        """Appelé ~30×/s : ne demande un rafraîchissement que si l'image doit bouger."""
        key = self.frame_key()
        if key is None or key != self.shown:
            self.update()


class HalftoneBar(QWidget):
    """Jauge de remplissage en trame fine.

    Utilisé : dégradé du cyan vers la couleur de la charge, bord adouci.
    Cache   : même teinte, points de plus en plus petits.
    Libre   : poussière de points à peine visible.
    reverse : dégradé inversé (batterie : rouge quand c'est presque vide).
    """

    def __init__(self, height=9):
        super().__init__()
        self.used = self.cache = 0.0
        self.scale, self.reverse = 1.0, False
        self.state = None
        self.setFixedHeight(height)

    def set(self, used, cache=0.0, scale=1.0, reverse=False):
        used = max(0.0, min(1.0, used))
        cache = max(0.0, min(1.0 - used, cache))
        # au-delà de ~1/200 près, le dessin serait identique : on ne repeint pas
        state = (round(used * 200), round(cache * 200), scale, reverse)
        if state != self.state:
            self.state = state
            self.used, self.cache, self.scale, self.reverse = used, cache, scale, reverse
            self.update()

    def color(self, pos):
        return heat_color((1 - pos) * 100 if self.reverse else pos * 100 * self.scale)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        rows = max(1, int(h / PITCH))
        if rows > 1:
            dy = h / (rows + 0.5)
            cols = int(w / (PITCH * 0.8))
            dx = w / cols
            rmax = 0.5 * min(dx * 1.25, dy)
        else:
            dy = h
            cols = int(w / PITCH)
            dx = w / cols
            rmax = 0.5 * min(dx * 0.85, h)
        used, cache = self.used, self.cache
        end_cache = used + cache
        cache_c = faded(self.color(used), 0.55)
        groups = defaultdict(list)            # (couleur, rayon) → points : 1 appel par groupe
        for c in range(cols):
            pos = (c + 0.5) / cols
            if pos <= used:
                s = 0.45 + 0.55 * min(1.0, (used - pos) * cols / 5)   # bord adouci
                color = self.color(pos).rgba()
            elif pos <= end_cache:
                s = 0.8 - 0.5 * (pos - used) / cache
                color = cache_c.rgba()
            else:
                s = 0.38
                color = DIM.rgba()
            x = (c + 0.5) * dx
            st = 0.5 if rows > 1 and c % 2 else 0.0
            pts = groups[(color, round(rmax * s, 2))]
            for row in range(rows):
                pts.append(QPointF(x, (row + 0.5 + st) * dy if rows > 1 else h / 2))
        pen = QPen()
        pen.setCapStyle(Qt.RoundCap)
        for (color, r), pts in groups.items():
            pen.setColor(QColor.fromRgba(color))
            pen.setWidthF(2 * r)
            p.setPen(pen)
            p.drawPoints(pts)


class PowerRows(QWidget):
    """Consommation par composant : une ligne par composant, toujours dans le même
    ordre, chacune avec sa barre de points (longueur = part du total) et sa valeur.
    Toutes les barres partent du même bord : on compare d'un coup d'œil."""

    def __init__(self, names, labels=None):
        super().__init__()
        self.labels = labels or {}            # clé → nom affiché (ex. « dGPU » → « RX 7700S »)
        self.names = list(names)
        self.rescale()
        # Toujours les mêmes lignes, dans le même ordre : la hauteur ne bouge
        # jamais et chaque composant reste à sa place.
        self.order = list(names)
        self.parts, self.total, self.state = [(n, None) for n in names], 0.0, None

    def rescale(self):
        """Dimensions selon la taille du texte (option)."""
        self.px = max(7, round(10 * FONT_SCALE))
        self.ROW = round(15 * FONT_SCALE)
        font = self.font()
        font.setPixelSize(self.px)
        fm = QFontMetrics(font)
        self.name_w = 14 + max(fm.horizontalAdvance(self.labels.get(n, n)) for n in self.names)
        self.VALUE_W = fm.horizontalAdvance("88.8 W") + fm.horizontalAdvance("100 %") + 16
        self.setFixedHeight(len(self.names) * self.ROW)
        self.update()

    def set(self, parts, total):
        values = dict(parts)
        parts = [(n, values.get(n)) for n in self.order]
        state = (tuple((n, None if w is None else round(w, 1)) for n, w in parts), round(total, 1))
        if state != self.state:
            self.state, self.parts, self.total = state, parts, total
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        font = p.font()
        font.setPixelSize(self.px)
        p.setFont(font)
        bar_x0, bar_x1 = self.name_w + 4, w - self.VALUE_W - 6
        cols = max(1, int((bar_x1 - bar_x0) / (PITCH * 1.35)))
        dx = (bar_x1 - bar_x0) / cols
        r = min(dx * 0.45, 2.1)
        pen = QPen()
        pen.setCapStyle(Qt.RoundCap)
        for i, (name, watts) in enumerate(self.parts):
            y = i * self.ROW + self.ROW / 2
            color = PART_COLORS[name]
            known = watts is not None
            frac = watts / self.total if known and self.total > 0 else 0
            # nom (avec sa pastille de couleur)
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawEllipse(QPointF(4, y), 3, 3)
            p.setPen(QColor(TEXT))
            p.drawText(QRectF(11, y - self.ROW / 2, self.name_w - 11, self.ROW), Qt.AlignVCenter | Qt.AlignLeft,
                       self.labels.get(name, name))
            # barre de points : pleine jusqu'à la part du total, le dernier point partiel
            lit = frac * cols
            full, dim = [], []
            for c in range(cols):
                pt = QPointF(bar_x0 + (c + 0.5) * dx, y)
                (full if c < int(lit) else dim).append(pt)
            pen.setColor(color)
            pen.setWidthF(2 * r)
            p.setPen(pen)
            p.drawPoints(full)
            part = lit - int(lit)
            if part > 0.05 and int(lit) < cols:
                pen.setWidthF(2 * r * (0.35 + 0.65 * part))
                p.setPen(pen)
                p.drawPoint(QPointF(bar_x0 + (int(lit) + 0.5) * dx, y))
                dim = dim[1:]
            pen.setColor(DIM)
            pen.setWidthF(2 * r * 0.4)
            p.setPen(pen)
            p.drawPoints(dim)
            # valeur et part du total
            p.setPen(QColor(TEXT if known else MUTED))
            vw = self.VALUE_W - 6 - p.fontMetrics().horizontalAdvance("100 %")
            p.drawText(QRectF(w - self.VALUE_W, y - self.ROW / 2, vw, self.ROW), Qt.AlignVCenter | Qt.AlignRight,
                       f"{watts:.1f} W" if known else "—")
            if known:
                p.setPen(QColor(MUTED))
                p.drawText(QRectF(w - self.VALUE_W, y - self.ROW / 2, self.VALUE_W, self.ROW),
                           Qt.AlignVCenter | Qt.AlignRight, f"{frac * 100:.0f} %")


class DotBar(QWidget):
    """Un point par cœur : il grossit et change de couleur avec la charge."""

    def __init__(self, count, height=8):
        super().__init__()
        self.levels = [0.0] * count
        self.setFixedHeight(height)

    def set_levels(self, levels):
        levels = [round(l * 40) / 40 for l in levels]      # 40 teintes suffisent
        if levels != self.levels:
            self.levels = levels
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        n = len(self.levels)
        dx, h = self.width() / n, self.height()
        r = min(dx, h) * 0.4
        for i, lvl in enumerate(self.levels):
            if lvl > 0.02:
                p.setBrush(heat_color(lvl * 100))
                rr = r
            else:
                p.setBrush(DIM)
                rr = r * 0.6
            p.drawEllipse(QPointF((i + 0.5) * dx, h / 2), rr, rr)


class Threads(QWidget):
    """Une mini-colonne en trame par thread (fréquence dessous en option)."""

    def __init__(self, n, height=78, show_freq=True):
        super().__init__()
        self.loads, self.freqs = [0] * n, [0] * n
        self.show_freq = show_freq
        self.setFixedHeight(height)

    def set(self, loads, freqs):
        self.loads, self.freqs = loads, freqs
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        n = len(self.loads)
        w = self.width()
        h = self.height() - (14 if self.show_freq else 0)
        rows = max(3, int(h / PITCH))
        dy = h / (rows + 0.5)
        slot = w / n
        sub = 3 if slot >= 14 else 2                   # 2-3 colonnes fines par thread
        dx = PITCH * 0.85
        rmax = 0.5 * min(dx * 1.25, dy)
        font = p.font()
        font.setPointSizeF(6.5 * FONT_SCALE)
        p.setFont(font)
        for i, (load, f) in enumerate(zip(self.loads, self.freqs)):
            cx = (i + 0.5) * slot
            p.setPen(Qt.NoPen)
            for k in range(sub):
                x = cx + (k - (sub - 1) / 2) * dx
                paint_column(p, x, h, dy, rows, load / 100, rmax)
            if self.show_freq:
                p.setPen(QColor(MUTED))
                p.drawText(QRectF(i * slot, h + 1, slot, 13), Qt.AlignCenter, f"{f:.1f}")


def fmt(v, unit, digits=0):
    return "—" if v is None else f"{v:.{digits}f}{unit}"


def gib(b):
    return b / 2**30


def size(b):
    return f"{b / 2**30:.1f} Go" if b >= 2**30 else f"{b / 2**20:.0f} Mo"


def duration(s):
    h, m = divmod(int(s) // 60, 60)
    if h >= 24:
        return f"{h // 24} j {h % 24} h"
    return f"{h} h {m:02d}" if h else f"{m} min"


FONT_SCALE = 1.0      # taille du texte (option), 1 = normale
_LABELS = []          # textes créés par label(), pour les remettre à l'échelle


def style_label(l):
    color, px, bold, spacing = l.base_style
    px = max(7, round(px * FONT_SCALE))
    l.setStyleSheet(f"color:{color}; font-size:{px}px; font-weight:{700 if bold else 400};"
                    f" letter-spacing:{spacing}px;")
    # Une ligne de texte garde toujours la même hauteur, même vide, et un texte
    # qui s'allonge ne peut pas élargir la fenêtre d'un coup : pas de sauts.
    l.setFixedHeight(round(px * 1.45) + 2)
    if l.reserve:
        # Largeur réservée pour la valeur la plus longue (ex. « 100 °C ») : jamais
        # coupée, et fixe tant que la taille du texte ne change pas.
        f = QFont(l.font())
        f.setPixelSize(px)
        f.setBold(bold)
        f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
        l.setMinimumWidth(QFontMetrics(f).horizontalAdvance(l.reserve) + 2)
    else:
        l.setMinimumWidth(1)


def label(text="", color=MUTED, px=11, bold=False, spacing=0, reserve=None, cls=None):
    l = (cls or QLabel)(text)
    l.base_style = (color, px, bold, spacing)
    l.reserve = reserve
    style_label(l)
    _LABELS.append(l)
    return l


class ElidedLabel(QLabel):
    """Texte qui se raccourcit avec « … » quand la place manque, au lieu d'être coupé."""

    def __init__(self, text=""):
        super().__init__()
        self.full = text
        super().setText(text)

    def setText(self, text):
        self.full = text
        self.elide()

    def text(self):
        return self.full

    def sizeHint(self):                     # la place souhaitée reste celle du texte entier
        h = super().sizeHint()
        return QSize(self.fontMetrics().horizontalAdvance(self.full) + 4, h.height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.elide()

    def changeEvent(self, e):               # nouvelle police (taille du texte)
        super().changeEvent(e)
        self.elide()

    def elide(self):
        shown = self.fontMetrics().elidedText(self.full, Qt.ElideRight, max(0, self.width() - 2))
        if shown != super().text():
            super().setText(shown)


def set_font_scale(scale):
    """Change la taille de tous les textes ; ceux qui ont été détruits sont oubliés."""
    global FONT_SCALE
    FONT_SCALE = scale
    alive = []
    for l in _LABELS:
        try:
            style_label(l)
            alive.append(l)
        except RuntimeError:          # widget Qt déjà détruit (ex. panneau d'options fermé)
            pass
    _LABELS[:] = alive


class Grip(QLabel):
    """Poignée ⠿ : on l'attrape pour déplacer le module dans la fenêtre."""

    def __init__(self, card):
        super().__init__("⠿")
        self.card = card
        self.setAttribute(Qt.WA_Hover)          # pour le style :hover
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(18, 20)
        self.setStyleSheet(
            "QLabel{color:rgba(230,232,238,0.55); font-size:16px; border-radius:5px;}"
            "QLabel:hover{color:#5ac8fa; background:rgba(90,200,250,0.16);}")
        self.setToolTip("Glisser pour déplacer ce module")
        self.setCursor(Qt.OpenHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.setCursor(Qt.ClosedHandCursor)
            self.window().drag_begin(self.card)
        e.accept()          # sinon la fenêtre entière partirait avec la souris

    def mouseMoveEvent(self, e):
        self.window().drag_move(self.card, e.globalPosition().toPoint())

    def mouseReleaseEvent(self, e):
        self.setCursor(Qt.OpenHandCursor)
        self.window().drag_end(self.card)


def chevron(tip):
    """Petit bouton ▾ / ▸ pour replier ou déplier."""
    b = QPushButton("▾")
    b.setFixedSize(16, 18)
    b.setCursor(Qt.PointingHandCursor)
    b.setToolTip(tip)
    b.setStyleSheet(
        "QPushButton{color:rgba(230,232,238,0.55); font-size:12px; border:none;"
        " border-radius:5px; background:transparent;}"
        "QPushButton:hover{color:#5ac8fa; background:rgba(90,200,250,0.16);}")
    return b


class Card(QWidget):
    """Bloc arrondi : poignée, titre, nom du matériel, grosse valeur à droite."""

    def __init__(self, module_id, title, subtitle="", big_px=17, collapsible=False):
        super().__init__()
        self.module_id = module_id
        self.dragging = False
        self.elastic = False         # devient vrai dès qu'on ajoute un graphe
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(8, 8, 10, 9)
        self.body.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(Grip(self))
        self.collapsed = False
        self.parts = []                  # tout sauf l'en-tête : ce qu'on masque en repliant
        self.folded_parts = set()         # sous-parties repliées (ex. la consommation)
        if collapsible:
            self.chevron = chevron("Replier / déplier ce module")
            self.chevron.clicked.connect(lambda: self.window().toggle_card(self))
            head.addWidget(self.chevron)
        head.addWidget(label(title, MUTED, 11, True, 1, reserve=title))
        self.subtitle = label(subtitle, TEXT, 11, cls=ElidedLabel)
        head.addWidget(self.subtitle)
        head.addStretch()
        self.temp = label("", MUTED, 11, True, reserve="100 °C")   # colorée selon sa valeur
        self.temp_state = None
        self.temp.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        head.addWidget(self.temp)
        self.watts = label("", "#facc15", 11, True, reserve="⚡ 88.8 W")   # consommation
        self.watts.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        head.addWidget(self.watts)
        self.big = label("—", TEXT, big_px, True, reserve="100 %")
        self.big.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        head.addWidget(self.big)
        self.body.addLayout(head)

    def set_temp(self, t):
        if t is None:
            self.temp.setText("")
            return
        self.temp.setText(f"{t:.0f} °C")
        color = heat_color(temp_norm(t)).name()
        if color != self.temp_state:                       # restyler seulement si la teinte change
            self.temp_state = color
            self.temp.base_style = (color,) + self.temp.base_style[1:]
            style_label(self.temp)

    def set_watts(self, w, suffix=""):
        self.watts.setText("" if w is None else f"⚡ {w:.1f} W{suffix}")

    def fold_with(self, name, widget):
        self.sections = getattr(self, "sections", {})
        self.sections[name] = widget

    def set_section_folded(self, name, folded):
        w = self.sections[name]
        (self.folded_parts.add if folded else self.folded_parts.discard)(w)
        w.setVisible(not folded and not self.collapsed)
        self.section_buttons[name].setText("▸" if folded else "▾")

    def set_collapsed(self, collapsed):
        """Replié : seule la ligne de titre reste (nom, température, watts, %)."""
        self.collapsed = collapsed
        for w in self.parts:
            w.setVisible(not collapsed and w not in self.folded_parts)
        if hasattr(self, "chevron"):
            self.chevron.setText("▸" if collapsed else "▾")

    def add(self, w):
        self.parts.append(w)
        if isinstance(w, DotGraph):
            # l'espace vertical en plus va aux graphes, au prorata de leur taille
            self.body.addWidget(w, w.base_height)
            self.elastic = True
        else:
            self.body.addWidget(w)
        return w

    def add_row(self, left, right=None, toggle=None):
        """Ligne « texte … valeur ». Avec toggle (nom), un bouton ▾ devant le texte
        replie ou déplie la sous-partie qu'on attachera ensuite (fold_with)."""
        row = QHBoxLayout()
        if toggle:
            row.setSpacing(4)
            b = chevron("Replier / déplier")
            b.clicked.connect(lambda: self.window().toggle_section(self, toggle))
            self.section_buttons = getattr(self, "section_buttons", {})
            self.section_buttons[toggle] = b
            row.addWidget(b)
            self.parts.append(b)
        a = label(left)
        row.addWidget(a)
        row.addStretch()
        b = label(right or "", TEXT, 10)
        row.addWidget(b)
        self.body.addLayout(row)
        self.parts += [a, b]
        return a, b

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        if self.dragging:                      # module en cours de déplacement
            p.setBrush(DRAG_TINT)
            p.setPen(QPen(QColor(90, 200, 250, 150), 1.2))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.6, 0.6, -0.6, -0.6), 10, 10)
        else:
            p.setBrush(PANEL)
            p.drawRoundedRect(self.rect(), 10, 10)


class StatGrid(QWidget):
    """Tableau étiquette → valeur, sur deux colonnes."""

    def __init__(self, items, cols=2):
        super().__init__()
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 2, 0, 2)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(2)
        self.values = {}
        for idx, (key, text) in enumerate(items):
            r, c = divmod(idx, cols)
            grid.addWidget(label(text, MUTED, 10), r, c * 2)
            v = label("—", TEXT, 11)
            v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(v, r, c * 2 + 1)
            self.values[key] = v
        for c in range(cols):
            grid.setColumnStretch(c * 2 + 1, 1)

    def set(self, key, text):
        self.values[key].setText(text)


class ProcList(QWidget):
    """Liste de programmes avec une petite barre de points chacun."""

    def __init__(self, n=5):
        super().__init__()
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(1)
        self.rows = []
        for i in range(n):
            name, bar, val = label("", TEXT, 10), HalftoneBar(5), label("", MUTED, 10)
            name.setFixedWidth(118)
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val.setMinimumWidth(52)
            grid.addWidget(name, i, 0)
            grid.addWidget(bar, i, 1)
            grid.addWidget(val, i, 2)
            self.rows.append((name, bar, val))
        grid.setColumnStretch(1, 1)

    def set_items(self, items):
        """items = [(nom, texte valeur, fraction 0..1)]"""
        for i, (name, bar, val) in enumerate(self.rows):
            if i < len(items):
                n, text, frac = items[i]
                name.setText(n if len(n) <= 18 else n[:17] + "…")
                val.setText(text)
                bar.set(frac)
            else:
                name.setText("")
                val.setText("")
                bar.set(0)


def legend(*items):
    """Petite légende HTML : (couleur, texte)."""
    return "&nbsp;&nbsp;".join(f"<span style='color:{c.name()}'>●</span> {t}" for c, t in items)


# ──────────────────────────────── languette ──────────────────────────────

DOCK_GAP = 2          # px : déposée à moins de ça d'un bord (gauche/droit), la fenêtre s'y range
DOCK_MARGIN = 6       # px d'espace entre la languette et le bord de l'écran


class DockTab(QWidget):
    """Languette affichée quand la fenêtre est rangée au bord de l'écran : une
    flèche pour la redéployer (clic) et un point par composant, coloré selon sa
    charge — on garde un coup d'œil même rangé. Glisser la déplace le long du bord."""

    def __init__(self, mon):
        super().__init__()
        self.mon, self.edge, self.levels, self.press = mon, "left", [], None
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Clic : déployer Coup d'œil — glisser : déplacer le long du bord")
        self.resize_for(4)

    def resize_for(self, n):
        self.setFixedSize(22, 30 + 14 * n)

    def set_levels(self, levels):
        """levels : charge de chaque composant en %, None s'il dort."""
        if len(levels) != len(self.levels):
            self.resize_for(len(levels))
        if levels != self.levels:
            self.levels = levels
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h, r = self.width(), self.height(), 9
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), r, r)
        p.fillPath(path, QColor(18, 20, 26, 245))
        p.setPen(QPen(QColor(255, 255, 255, 28), 1))      # léger contour : elle flotte
        p.drawPath(path)
        p.setPen(QColor(MUTED))
        f = p.font()
        f.setPixelSize(12)
        p.setFont(f)
        p.drawText(QRectF(0, 4, w, 18), Qt.AlignCenter, "▸" if self.edge == "left" else "◂")
        p.setPen(Qt.NoPen)
        for i, lvl in enumerate(self.levels):
            p.setBrush(DIM if lvl is None else heat_color(lvl))
            p.drawEllipse(QPointF(w / 2, 30 + 14 * i), 4, 4)

    def mousePressEvent(self, e):
        self.press = e.globalPosition().toPoint()

    def mouseMoveEvent(self, e):
        if self.press is not None and (e.globalPosition().toPoint() - self.press).manhattanLength() > 6:
            self.press = None
            self.mon.windowHandle().startSystemMove()     # le long du bord ; recollée ensuite

    def mouseReleaseEvent(self, e):
        if self.press is not None:
            self.press = None
            self.mon.undock()


# ──────────────────────────────── options ────────────────────────────────

AUTOSTART = (Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
             / "autostart" / "coup-doeil.desktop")


def set_autostart(on):
    """Lancer Coup d'œil à l'ouverture de la session (fichier ~/.config/autostart)."""
    if on:
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART.write_text("[Desktop Entry]\nType=Application\nName=Coup d'œil\n"
                             f"Exec=python3 \"{Path(__file__).resolve()}\"\n"
                             "Icon=coup-doeil\nX-GNOME-Autostart-enabled=true\n")
    else:
        AUTOSTART.unlink(missing_ok=True)


OPTIONS_QSS = f"""
QLabel {{ color: {TEXT}; font-size: 11px; }}
QCheckBox {{ color: {TEXT}; font-size: 11px; spacing: 7px; }}
QCheckBox::indicator {{ width: 13px; height: 13px; border-radius: 4px;
    border: 1px solid rgba(255,255,255,0.25); background: rgba(255,255,255,0.05); }}
QCheckBox::indicator:checked {{ background: #5ac8fa; border-color: #5ac8fa; }}
QComboBox {{ color: {TEXT}; font-size: 11px; background: rgba(255,255,255,0.07);
    border: 1px solid rgba(255,255,255,0.12); border-radius: 6px; padding: 3px 8px; min-width: 76px; }}
QComboBox::drop-down {{ border: none; width: 16px; }}
QComboBox QAbstractItemView {{ color: {TEXT}; background: #20232c; selection-background-color: #2f5d73;
    border: 1px solid rgba(255,255,255,0.12); outline: none; }}
QSlider::groove:horizontal {{ height: 4px; border-radius: 2px; background: rgba(255,255,255,0.12); }}
QSlider::sub-page:horizontal {{ border-radius: 2px; background: #5ac8fa; }}
QSlider::handle:horizontal {{ width: 12px; margin: -5px 0; border-radius: 6px; background: {TEXT}; }}
QPushButton {{ color: {TEXT}; font-size: 11px; background: rgba(255,255,255,0.07);
    border: 1px solid rgba(255,255,255,0.12); border-radius: 6px; padding: 5px 10px; }}
QPushButton:hover {{ background: rgba(90,200,250,0.18); }}
"""


class OptionsPanel(QWidget):
    """Petit panneau d'options qui s'ouvre sous le bouton ⚙ ; chaque réglage
    s'applique tout de suite et est retenu."""

    def __init__(self, mon):
        super().__init__(mon, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setStyleSheet(OPTIONS_QSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(8)
        lay.addWidget(label("OPTIONS", MUTED, 10, True, 2))

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)

        def choix(items, current, on_change):
            box = QComboBox()
            for text, value in items:
                box.addItem(text, value)
            box.setCurrentIndex(max(0, box.findData(current)))
            box.currentIndexChanged.connect(lambda i: on_change(box.itemData(i)))
            return box

        grid.addWidget(QLabel("Rafraîchissement"), 0, 0)
        grid.addWidget(choix([("0,5 s", 500), ("1 s", 1000), ("2 s", 2000), ("5 s", 5000)],
                             mon.refresh_ms, mon.set_refresh), 0, 1)
        grid.addWidget(QLabel("Taille du texte"), 3, 0)
        grid.addWidget(choix([("80 %", 80), ("90 %", 90), ("100 %", 100), ("115 %", 115),
                              ("130 %", 130), ("150 %", 150)],
                             round(FONT_SCALE * 100), mon.set_font_size), 3, 1)
        grid.addWidget(QLabel("Lissage des courbes"), 1, 0)
        grid.addWidget(choix([("Aucun", 0), ("Léger", 1), ("Moyen", 2), ("Fort", 4)],
                             mon.smooth_passes, mon.set_smoothing), 1, 1)
        grid.addWidget(QLabel("Opacité du fond"), 2, 0)
        row = QHBoxLayout()
        slider = QSlider(Qt.Horizontal)
        slider.setRange(30, 100)
        slider.setValue(round(BG.alphaF() * 100))
        value = QLabel(f"{slider.value()} %")
        value.setMinimumWidth(34)
        slider.valueChanged.connect(lambda v: (value.setText(f"{v} %"), mon.set_bg_opacity(v)))
        row.addWidget(slider)
        row.addWidget(value)
        grid.addLayout(row, 2, 1)
        lay.addLayout(grid)

        smooth = QCheckBox("Défilement fluide des courbes")
        smooth.setChecked(mon.smooth_scroll)
        smooth.toggled.connect(mon.set_smooth_scroll)
        lay.addWidget(smooth)
        auto = QCheckBox("Lancer à l'ouverture de session")
        auto.setChecked(AUTOSTART.exists())
        auto.toggled.connect(set_autostart)
        lay.addWidget(auto)

        lay.addSpacing(4)
        lay.addWidget(label("MODULES AFFICHÉS", MUTED, 10, True, 2))
        for module_id, name in mon.module_names().items():
            box = QCheckBox(name)
            box.setChecked(module_id not in mon.hidden)
            box.toggled.connect(lambda on, m=module_id: mon.set_module_visible(m, on))
            lay.addWidget(box)

        lay.addSpacing(4)
        reset = QPushButton("Réinitialiser la disposition")
        reset.setToolTip("Ordre des modules et tailles des deux vues par défaut")
        reset.clicked.connect(lambda: (mon.reset_layout(), self.close()))
        lay.addWidget(reset)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.setBrush(QColor(26, 29, 37, 248))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)


# ──────────────────────────────── fenêtre ────────────────────────────────

def settings():
    """Réglages (~/.config/coup-doeil/). Reprend ceux de l'ancien nom « moniteur »."""
    s = QSettings("coup-doeil", "coup-doeil")
    if not s.allKeys():
        old = QSettings("moniteur", "moniteur")
        for key in old.allKeys():
            s.setValue(key, old.value(key))
    return s


class Monitor(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = settings()
        self.setWindowTitle("Coup d'œil")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setMouseTracking(True)      # pour changer le curseur sur les bords
        self.settle_until = 0.0
        self.detailed = False            # vue affichée (isVisible() est faux avant show())
        self.size_timer = QTimer(self, singleShot=True, interval=500, timeout=self.save_size_now)

        self.cpu = CpuSensor()
        self.gpus = GpuSensor.detect()
        self.apu = next((g.metrics for g in self.gpus if g.metrics), None)
        self.ram = RamSensor()
        self.sys = SysSensor()
        self.procs = ProcSampler()
        self.hist = defaultdict(lambda: deque([None] * (HISTORY + 1), maxlen=HISTORY + 1))
        self.graphs = []
        self.tick = 0
        self.last_sample = time.monotonic()
        set_font_scale(self.settings.value("font_scale", 100, type=int) / 100)
        self.refresh_ms = self.settings.value("refresh_ms", REFRESH_MS, type=int)
        self.smooth_scroll = self.settings.value("smooth_scroll", True, type=bool)
        self.smooth_passes = self.settings.value("smooth_passes", SMOOTH_PASSES, type=int)
        set_background_opacity(self.settings.value("bg_opacity", 92, type=int))
        try:
            self.hidden = set(json.loads(self.settings.value("hidden", "[]") or "[]"))
        except ValueError:
            self.hidden = set()
        self.hist_labels = []
        self.layouts = {}
        try:
            self.folded = set(json.loads(self.settings.value("collapsed", "[]") or "[]"))
            self.folded_sections = set(json.loads(self.settings.value("folded_sections", "[]") or "[]"))
        except ValueError:
            self.folded, self.folded_sections = set(), set()

        root = QVBoxLayout(self)
        root.setSizeConstraint(QLayout.SetMinimumSize)  # taille libre, jamais sous le contenu
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(8)

        self.topbar = QWidget()
        top = QHBoxLayout(self.topbar)
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(label("● COUP D'ŒIL", MUTED, 10, True, 2))
        top.addStretch()
        self.expand = self._button("⤢", "Vue détaillée")
        self.expand.setCheckable(True)
        self.expand.toggled.connect(self.set_detailed)
        self.opts_btn = self._button("⚙", "Options")
        self.opts_btn.clicked.connect(self.show_options)
        top.addWidget(self.opts_btn)
        top.addWidget(self.expand)
        self.pin = self._button("📌", "Toujours au-dessus")
        self.pin.setCheckable(True)
        self.pin.toggled.connect(self.set_on_top)
        top.addWidget(self.pin)
        close = self._button("✕", "Fermer")
        close.clicked.connect(self.close)
        top.addWidget(close)
        root.addWidget(self.topbar)

        self.compact = self.build_compact()
        self.detail = self.build_detail()
        root.addWidget(self.compact)
        root.addWidget(self.detail)
        self.detail.hide()
        self.docked = None                         # "left" / "right" quand rangée au bord
        self.dock_tab = DockTab(self)
        root.addWidget(self.dock_tab, 0, Qt.AlignCenter)
        self.dock_tab.hide()
        self.dock_timer = QTimer(self, singleShot=True, interval=450, timeout=self.check_dock)

        self.timer = QTimer(self, interval=self.refresh_ms, timeout=self.refresh)
        self.timer.start()
        self.refresh()
        self.anim = QTimer(self, interval=ANIM_MS, timeout=self.animate)
        if self.smooth_scroll:
            self.anim.start()
        self.apply_hidden()
        for module_id in self.folded & set(self.compact_cards):
            self.set_card_collapsed(self.compact_cards[module_id], True)
        for key in self.folded_sections:
            view, module_id, name = key.split(":")
            card = (self.detail_cards if view == "d" else self.compact_cards).get(module_id)
            if card is not None and name in getattr(card, "sections", {}):
                card.set_section_folded(name, True)

        pos = self.settings.value("pos")
        if pos is not None:
            self.move(pos)
        else:
            geo = QApplication.primaryScreen().availableGeometry()
            self.move(geo.right() - 300 - 16, geo.top() + 16)
        self.pin.setChecked(self.settings.value("on_top", True, type=bool))
        detailed = self.settings.value("detailed", False, type=bool)
        if detailed:
            self.expand.setChecked(True)     # applique aussi la taille de la vue
        else:
            self.apply_view_size()
        self.wanted = None                   # géométrie voulue, réaffirmée après l'ouverture

    def showEvent(self, e):
        super().showEvent(e)
        if self.wanted is None:
            # Un gestionnaire de fenêtres (ou un script KWin) peut replacer et
            # redimensionner une fenêtre qui s'ouvre. On note ce que l'appli veut
            # et on le réapplique une fois l'ouverture terminée.
            self.wanted = (self.pos(), self.size())
            self.settle_until = time.monotonic() + 1.0
            QTimer.singleShot(250, self.reassert_geometry)

    def reassert_geometry(self):
        edge = self.settings.value("dock_edge")
        if edge in ("left", "right"):
            self.dock(edge, self.settings.value("dock_y", self.y(), type=int))
            return
        pos, _ = self.wanted
        self.apply_view_size()       # taille enregistrée ; le minimum est juste maintenant
        if self.pos() != pos:        # (modules repliés pris en compte après l'affichage)
            self.move(pos)

    def _button(self, text, tip):
        b = QPushButton(text)
        b.setToolTip(tip)
        b.setFixedSize(26, 22)
        b.setCursor(Qt.PointingHandCursor)
        b.setStyleSheet(
            "QPushButton{background:transparent;border:none;border-radius:6px;"
            f"color:{MUTED};font-size:13px;}}"
            "QPushButton:hover{background:rgba(255,255,255,0.08);}"
            "QPushButton:checked{background:rgba(90,200,250,0.25);color:white;}")
        return b

    def graph(self, key, cols, height=46, norm=None):
        g = DotGraph(self.hist[key], cols, self, height, norm)
        self.graphs.append(g)
        return g

    # --- construction : vue compacte ------------------------------------

    def build_compact(self):
        w = QWidget()
        w.setMinimumWidth(240)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        c = self.c_cpu = Card("cpu", "CPU", short_name(self.cpu.name), collapsible=True)
        c.add(self.graph("cpu", COMPACT_COLS))
        c.stats = c.add(label("", TEXT, 11))
        c.bar_label, _ = c.add_row(f"{self.cpu.threads} threads")
        c.bar = c.add(DotBar(self.cpu.threads))
        cards = {"cpu": c}

        self.c_gpus = []
        for i, gpu in enumerate(self.gpus):
            c = Card(f"gpu{i}", "iGPU" if gpu.integrated else "GPU", short_name(gpu.name), collapsible=True)
            c.add(self.graph(f"gpu{i}", COMPACT_COLS))
            c.stats = c.add(label("", TEXT, 11))
            c.mem_label, c.mem_pct = c.add_row("")
            c.mem = c.add(HalftoneBar(14))
            self.c_gpus.append(c)
            cards[c.module_id] = c

        c = self.c_ram = Card("ram", "RAM", f"{gib(self.ram.total):.0f} Go", collapsible=True)
        c.add(self.graph("ram", COMPACT_COLS))
        c.stats = c.add(label("", TEXT, 11))
        c.mem_label, c.mem_pct = c.add_row("")
        c.mem = c.add(HalftoneBar(14))
        c.swap_label, _ = c.add_row("")
        c.swap = c.add(HalftoneBar(5))
        cards["ram"] = c

        self.c_bat = None
        if self.sys.bat:
            c = self.c_bat = Card("bat", "BATTERIE", "", collapsible=True)
            c.mem = c.add(HalftoneBar(9))
            c.stats = c.add(label("", TEXT, 11))
            c.split_label, c.split_total = c.add_row("Consommation", toggle="conso")
            c.split = c.add(PowerRows(self.part_names(), self.part_labels()))
            c.fold_with("conso", c.split)
            cards["bat"] = c

        self.compact_cards = cards
        self.compact_cols = [(w, lay)]
        self.place(cards, self.compact_cols, "order_compact",
                   [["cpu"] + [f"gpu{i}" for i in range(len(self.gpus))] + ["ram", "bat"]])
        return w

    # --- construction : vue détaillée -----------------------------------

    def build_detail(self):
        w = QWidget()
        cols = QHBoxLayout(w)
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(8)
        col_w = 345

        cards = {}
        self.detail_cols = []
        for _ in range(3):
            v = QVBoxLayout()
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(8)
            box = QWidget()
            box.setMinimumWidth(col_w)
            box.setLayout(v)
            cols.addWidget(box, 1)
            self.detail_cols.append((box, v))

        c = self.d_cpu = Card("cpu", "CPU", self.cpu.name.split(" w/ ")[0], 20)
        c.add(self.hist_label("Charge"))
        c.add(self.graph("cpu", HISTORY, 64))
        c.add(label("Température", MUTED, 10))
        c.add(self.graph("cpu_temp", HISTORY, 32, temp_norm))
        c.stats = c.add(StatGrid([
            ("freq_avg", "Fréq. moyenne"), ("freq", "Fréq. max"),
            ("power", "Puissance cœurs"), ("temp", "Température"),
            ("package", "Puce entière"), ("soc", "SoC (E/S, mém.)"),
            ("cores", "Cœurs / threads"), ("boost", "Boost max"),
            ("loadavg", "Charge 1/5/15 min"), ("load", "Utilisation")]))
        c.add(label("Threads — charge et GHz", MUTED, 10))
        c.threads = c.add(Threads(self.cpu.threads))
        cards["cpu"] = c

        c = self.d_sys = Card("sys", "SYSTÈME", "", 14)
        c.stats = c.add(StatGrid([
            ("battery", "Batterie"), ("status", "État"),
            ("watts", "Puissance batterie"), ("left", "Temps restant"),
            ("uptime", "Allumé depuis"), ("procs", "Programmes")]))
        c.split_label, c.split_total = c.add_row("Consommation par composant", toggle="conso")
        c.split = c.add(PowerRows(self.part_names(), self.part_labels()))
        c.fold_with("conso", c.split)
        c.add(label("Programmes — CPU (100 % = 1 thread)", MUTED, 10))
        c.procs = c.add(ProcList(5))
        cards["sys"] = c

        self.d_gpus = []
        for i, gpu in enumerate(self.gpus):
            c = Card(f"gpu{i}", "iGPU" if gpu.integrated else "GPU", short_name(gpu.name), 20)
            c.add(self.hist_label("Charge"))
            c.add(self.graph(f"gpu{i}", HISTORY, 56 if not gpu.integrated else 40))
            if gpu.integrated:
                c.stats = c.add(StatGrid([
                    ("freq", "Fréquence"), ("power", "Puissance GPU"),
                    ("temp", "Température"), ("volt", "Tension"),
                    ("vram", "VRAM"), ("gtt", "GTT")]))
                c.mem_label, c.mem_pct = c.add_row("")
                c.mem = c.add(HalftoneBar(17))
                c.add(label("Mémoire GPU par programme", MUTED, 10))
                c.procs = c.add(ProcList(3))
            else:
                c.add(label("VRAM utilisée", MUTED, 10))
                c.add(self.graph(f"gpu{i}_vram", HISTORY, 32))
                c.stats = c.add(StatGrid([
                    ("freq", "Fréq. GPU"), ("mclk", "Fréq. mémoire"),
                    ("power", "Puissance"), ("fan", "Ventilateur"),
                    ("edge", "Temp. bord"), ("junction", "Temp. jonction"),
                    ("mem", "Temp. mémoire"), ("volt", "Tension"),
                    ("link", "Lien PCIe"), ("gtt", "GTT")]))
                c.mem_label, c.mem_pct = c.add_row("")
                c.mem = c.add(HalftoneBar(17))
                c.add(label("VRAM par programme", MUTED, 10))
                c.procs = c.add(ProcList(4))
            self.d_gpus.append(c)
            cards[c.module_id] = c

        c = self.d_ram = Card("ram", "RAM", f"{gib(self.ram.total):.0f} Go", 20)
        c.add(self.hist_label("Utilisation"))
        c.add(self.graph("ram", HISTORY, 64))
        c.add(label("Swap", MUTED, 10))
        c.add(self.graph("swap", HISTORY, 24))
        c.stats = c.add(StatGrid([
            ("used", "Utilisée"), ("cache", "Cache"),
            ("free", "Libre"), ("available", "Disponible"),
            ("swap", "Swap"), ("temp", "Temp. barrettes")]))
        c.legend = c.add(label("", MUTED, 10))
        c.legend.setTextFormat(Qt.RichText)
        c.mem = c.add(HalftoneBar(17))
        c.swap_label, _ = c.add_row("")
        c.swap = c.add(HalftoneBar(6))
        c.add(label("Programmes — RAM", MUTED, 10))
        c.procs = c.add(ProcList(6))
        cards["ram"] = c

        self.detail_cards = cards
        self.place(cards, self.detail_cols, "order_detail",
                   [["cpu", "sys"], [f"gpu{i}" for i in range(len(self.gpus))], ["ram"]])
        return w

    # --- modules déplaçables --------------------------------------------

    def place(self, cards, columns, key, default):
        """Range les cartes dans les colonnes selon l'ordre enregistré (sinon défaut)."""
        self.layouts[key] = (cards, columns, default)
        try:
            order = json.loads(self.settings.value(key, "") or "null")
        except ValueError:
            order = None
        if not (isinstance(order, list) and len(order) == len(columns)
                and all(isinstance(col, list) for col in order)):
            order = default
        order = [[m for m in col if m in cards] for col in order]
        placed = {m for col in order for m in col}
        for m in cards:                          # module nouveau : à sa place par défaut
            if m not in placed:
                home = next((i for i, col in enumerate(default) if m in col), len(order) - 1)
                order[home].append(m)
        for (_, lay), ids in zip(columns, order):
            for m in ids:
                self.add_card(lay, cards[m])
            lay.addStretch(0)        # garde les cartes en haut si aucune ne grandit

    @staticmethod
    def add_card(lay, card, index=-1):
        """Une carte avec graphe s'étire en hauteur ; les autres (et les repliées) non."""
        grow = card.elastic and not card.collapsed
        card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding if grow else QSizePolicy.Maximum)
        lay.insertWidget(index, card, 1 if grow else 0)

    @staticmethod
    def cards_in(lay):
        return [lay.itemAt(i).widget() for i in range(lay.count()) if lay.itemAt(i).widget()]

    def visible_cols(self):
        return self.detail_cols if self.detailed else self.compact_cols

    def drag_begin(self, card):
        card.dragging = True
        card.update()

    def drag_move(self, card, gpos):
        cols = self.visible_cols()
        # colonne la plus proche de la souris, puis rang selon la hauteur
        box, lay = min(cols, key=lambda c: abs(c[0].mapFromGlobal(gpos).x() - c[0].width() / 2))
        y = box.mapFromGlobal(gpos).y()
        others = [w for w in self.cards_in(lay) if w is not card]
        shown = [w for w in others if w.isVisible()]            # les masqués n'ont pas de place
        below = next((o for o in shown if o.geometry().center().y() > y), None)
        index = others.index(below) if below else (others.index(shown[-1]) + 1 if shown else len(others))
        current = next(l for _, l in cols if l.indexOf(card) >= 0)
        if current is lay and self.cards_in(lay).index(card) == index:
            return
        current.removeWidget(card)
        self.add_card(lay, card, index)
        self.layout().activate()

    def drag_end(self, card):
        card.dragging = False
        card.update()
        compact = self.visible_cols() is self.compact_cols
        order = [[w.module_id for w in self.cards_in(lay)] for _, lay in self.visible_cols()]
        self.settings.setValue("order_compact" if compact else "order_detail", json.dumps(order))

    # --- options --------------------------------------------------------

    def history_text(self):
        secs = HISTORY * self.refresh_ms / 1000
        return f"{secs:g} s" if secs < 120 else f"{secs / 60:g} min"

    def hist_label(self, text):
        l = label(f"{text} — {self.history_text()}", MUTED, 10)
        self.hist_labels.append((l, text))
        return l

    def show_options(self):
        panel = OptionsPanel(self)
        panel.adjustSize()
        b = self.opts_btn
        pos = b.mapToGlobal(QPoint(b.width(), b.height() + 4)) - QPoint(panel.width(), 0)
        geo = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        pos.setX(max(geo.left(), min(pos.x(), geo.right() - panel.width())))
        pos.setY(max(geo.top(), min(pos.y(), geo.bottom() - panel.height())))
        panel.move(pos)
        panel.show()

    def set_refresh(self, ms):
        self.refresh_ms = ms
        self.timer.setInterval(ms)
        self.settings.setValue("refresh_ms", ms)
        for l, text in self.hist_labels:
            l.setText(f"{text} — {self.history_text()}")

    def set_font_size(self, pct):
        set_font_scale(pct / 100)
        self.settings.setValue("font_scale", pct)
        for w in self.findChildren(PowerRows):
            w.rescale()
        for w in self.findChildren(Threads):
            w.update()
        self.layout().activate()

    def set_bg_opacity(self, pct):
        set_background_opacity(pct)
        self.settings.setValue("bg_opacity", pct)
        self.update()
        for g in self.graphs:
            g.update()

    def set_smooth_scroll(self, on):
        self.smooth_scroll = on
        self.settings.setValue("smooth_scroll", on)
        (self.anim.start if on else self.anim.stop)()
        for g in self.graphs:
            g.shown = None
            g.update()

    def set_smoothing(self, passes):
        self.smooth_passes = passes
        self.settings.setValue("smooth_passes", passes)
        for g in self.graphs:                      # courbes à recalculer
            g.cache_key = g.prev = None
            g.update()

    def module_names(self):
        names = {"cpu": f"CPU — {short_name(self.cpu.name)}"}
        for i, g in enumerate(self.gpus):
            names[f"gpu{i}"] = f"{'iGPU' if g.integrated else 'GPU'} — {short_name(g.name)}"
        names["ram"] = "RAM"
        if self.c_bat:
            names["bat"] = "Batterie"
        names["sys"] = "Système (grande vue)"
        return names

    def set_module_visible(self, module_id, on):
        (self.hidden.discard if on else self.hidden.add)(module_id)
        self.settings.setValue("hidden", json.dumps(sorted(self.hidden)))
        self.apply_hidden()

    def toggle_section(self, card, name):
        folded = card.sections[name] not in card.folded_parts
        key = f"{'d' if card in self.detail_cards.values() else 'c'}:{card.module_id}:{name}"
        (self.folded_sections.add if folded else self.folded_sections.discard)(key)
        self.settings.setValue("folded_sections", json.dumps(sorted(self.folded_sections)))
        before, window_before = card.height(), self.height()
        card.set_section_folded(name, folded)
        if self.detailed:
            return                         # grande vue : les colonnes s'arrangent seules
        for _ in range(3):
            QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        card.layout().activate()
        self.layout().activate()
        delta = card.sizeHint().height() - before
        geo = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        h = window_before + delta
        if delta > 0:                      # en dépliant, ne pas dépasser le bas de l'écran
            h = min(h, max(window_before, geo.bottom() - self.y() + 1))
        self.resize(self.width(), max(self.minimumSizeHint().height(), h))

    def toggle_card(self, card):
        self.set_card_collapsed(card, not card.collapsed, adjust=True)

    def set_card_collapsed(self, card, collapsed, adjust=False):
        """Replie ou déplie un module de la vue compacte. Avec adjust, la fenêtre
        rétrécit (ou regrandit) de la place libérée, sans toucher aux autres modules."""
        before, window_before = card.height(), self.height()
        card.set_collapsed(collapsed)
        lay = next((l for _, l in self.compact_cols if l.indexOf(card) >= 0), None)
        if lay is not None:
            grow = card.elastic and not collapsed
            card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding if grow else QSizePolicy.Maximum)
            lay.setStretchFactor(card, 1 if grow else 0)
        (self.folded.add if collapsed else self.folded.discard)(card.module_id)
        self.settings.setValue("collapsed", json.dumps(sorted(self.folded)))
        if not adjust or not self.isVisible():
            return
        # Les tailles minimales des modules sont recalculées de façon différée par Qt :
        # on applique ces recalculs maintenant, sinon la fenêtre croirait que le
        # module replié occupe encore sa place.
        for _ in range(3):
            QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        card.layout().activate()
        self.layout().activate()
        if collapsed:
            card.expanded_h = before
            delta = before - card.sizeHint().height()
        else:   # hauteur d'avant le repli si on la connaît, sinon sa taille naturelle
            delta = (getattr(card, "expanded_h", None) or card.sizeHint().height()) - before
        geo = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        if collapsed:
            h = window_before - delta
        else:                  # en dépliant, ne pas dépasser le bas de l'écran
            h = min(window_before + delta, max(window_before, geo.bottom() - self.y() + 1))
        self.resize(self.width(), max(self.minimumSizeHint().height(), h))

    def apply_hidden(self):
        for cards, _, _ in self.layouts.values():
            for module_id, card in cards.items():
                card.setVisible(module_id not in self.hidden)

    def reset_layout(self):
        """Ordre des modules et tailles des deux vues par défaut."""
        for card in list(self.compact_cards.values()):
            if card.collapsed:
                self.set_card_collapsed(card, False)
        for cards in (self.compact_cards, self.detail_cards):
            for card in cards.values():
                for name in getattr(card, "sections", {}):
                    card.set_section_folded(name, False)
        self.folded_sections.clear()
        self.settings.remove("folded_sections")
        for key in ("order_compact", "order_detail", "size_compact", "size_detail"):
            self.settings.remove(key)
        for key, (cards, columns, default) in list(self.layouts.items()):
            for _, lay in columns:
                while lay.count():
                    lay.takeAt(0)                  # les cartes restent, on les replace
            self.place(cards, columns, key, default)
        self.apply_hidden()
        self.apply_view_size()

    # --- bascule de vue / au-dessus -------------------------------------

    def set_on_top(self, on):
        # QWidget.setWindowFlag() détruit et recrée la fenêtre X11 : KWin perdait
        # alors l'état « au-dessus » une fois sur deux. QWindow.setFlag() modifie
        # la fenêtre existante (message _NET_WM_STATE direct au gestionnaire).
        handle = self.windowHandle()
        if handle is None:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, on)   # pas encore créée
        else:
            handle.setFlag(Qt.WindowStaysOnTopHint, on)
        self.settings.setValue("on_top", on)

    def set_detailed(self, on):
        old = self.geometry()
        self.save_size_now()                       # taille de la vue qu'on quitte
        self.settle_until = time.monotonic() + 1.0 # ignorer les tailles transitoires
        self.detailed = on
        (self.compact if on else self.detail).hide()
        (self.detail if on else self.compact).show()
        self.expand.setText("⤡" if on else "⤢")
        self.expand.setToolTip("Vue compacte" if on else "Vue détaillée")
        self.settings.setValue("detailed", on)
        self.refresh(sample=False)
        self.apply_view_size()
        # garder le bord le plus proche du coin d'origine, et rester dans l'écran
        geo = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        x = old.right() - self.width() + 1 if old.center().x() > geo.center().x() else old.x()
        x = max(geo.left(), min(x, geo.right() - self.width()))
        y = max(geo.top(), min(old.y(), geo.bottom() - self.height()))
        self.move(x, y)

    def view_key(self):
        return "size_detail" if self.detailed else "size_compact"

    def apply_view_size(self):
        """Reprend la taille retenue pour la vue affichée (sinon sa taille naturelle)."""
        for _ in range(3):                 # tailles minimales à jour (modules repliés…)
            QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        self.layout().activate()
        saved = self.settings.value(self.view_key())
        size = saved if isinstance(saved, QSize) else self.sizeHint()
        self.resize(size.expandedTo(self.minimumSizeHint()))

    def resizeEvent(self, e):
        # La taille est enregistrée 0,5 s après le dernier changement, et jamais
        # juste après une bascule de vue (tailles intermédiaires pendant la transition).
        self.size_timer.start()
        super().resizeEvent(e)

    def save_size_now(self):
        if time.monotonic() >= self.settle_until and self.isVisible() and not self.docked:
            self.settings.setValue(self.view_key(), self.size())

    def closeEvent(self, e):
        self.save_size_now()
        super().closeEvent(e)

    # --- mise à jour ----------------------------------------------------

    def animate(self):
        if self.isMinimized():
            return
        for g in self.graphs:
            if g.isVisible():
                g.tick_anim()


    def refresh(self, sample=True):
        if sample:
            self.tick += 1
            self.last_sample = time.monotonic()
            self.data = {
                "cpu": self.cpu.sample(),
                "gpus": [g.sample() for g in self.gpus],
                "ram": self.ram.sample(),
                "apu": self.apu.read() if self.apu else None,
            }
            d = self.data
            self.hist["cpu"].append(d["cpu"]["load"])
            self.hist["cpu_temp"].append(d["cpu"]["temp"])
            for i, g in enumerate(d["gpus"]):
                self.hist[f"gpu{i}"].append(None if g["asleep"] else g["load"])
                if not g["asleep"] and g["vram_total"]:
                    self.hist[f"gpu{i}_vram"].append(g["vram"] / g["vram_total"] * 100)
                else:
                    self.hist[f"gpu{i}_vram"].append(None)
            r = d["ram"]
            self.hist["ram"].append(r["load"])
            self.hist["swap"].append(r["swap"] / r["swap_total"] * 100 if r["swap_total"] else 0)

        if self.isMinimized():
            return
        if self.docked:
            self.update_dock_tab()
            return
        if self.detailed:
            self.update_detail()
        else:
            self.update_compact()
        for g in self.graphs:
            if g.isVisible():
                g.tick_anim()

    def update_compact(self):
        d = self.data
        c, cpu = self.c_cpu, d["cpu"]
        c.big.setText(fmt(cpu["load"], " %"))
        pw = self.powers()
        c.set_watts(pw["cpu"], "" if pw["cpu_is_cores"] else " puce")
        chip = f"   puce {pw['package']:.1f} W" if pw["cpu_is_cores"] and pw["package"] else ""
        c.set_temp(cpu["temp"])
        c.stats.setText(f"{cpu['freq_avg']:.2f} GHz moy · {cpu['freq']:.2f} max{chip}")
        c.bar.set_levels([v / 100 for v in cpu["cores"]])

        for gpu, c, g in zip(self.gpus, self.c_gpus, d["gpus"]):
            if g["asleep"]:
                c.big.setText("veille")
                c.set_watts(0.0)
                c.set_temp(None)
                c.stats.setText("en veille (économie d'énergie)")
                c.mem_label.setText("VRAM  —")
                c.mem_pct.setText("")
                c.mem.set(0)
                continue
            c.big.setText(fmt(g["load"], " %"))
            if gpu.integrated and pw["igpu"] is not None:
                c.set_watts(pw["igpu"])                      # partie graphique seule
            else:                                             # sans métriques : PPT = toute la puce
                c.set_watts(g["power"], " puce" if gpu.integrated else "")
            c.set_temp(g["temp"])
            c.stats.setText(f"Fréquence  {fmt(g['freq'], ' GHz', 2)}")
            self.fill_gpu_mem(gpu, c, g)

        c, r = self.c_ram, d["ram"]
        c.big.setText(fmt(r["load"], " %"))
        c.set_temp(r["temp"])
        c.stats.setText(f"{gib(r['used']):.1f} / {gib(r['total']):.1f} Go   cache {gib(r['cache']):.1f} Go")
        c.mem_label.setText("Mémoire  ● utilisée  ○ cache")
        c.mem_pct.setText(f"{gib(r['available']):.1f} Go dispo")
        c.mem.set(r["used"] / r["total"], r["cache"] / r["total"])
        c.swap_label.setText(f"Swap  {gib(r['swap']):.1f} / {gib(r['swap_total']):.1f} Go")
        c.swap.set(r["swap"] / r["swap_total"] if r["swap_total"] else 0)

        if self.c_bat:
            self.update_battery(self.c_bat, self.sys.battery())

    def update_battery(self, c, b):
        if not b:
            return
        pct = b["percent"]
        c.big.setText(f"{pct:.0f} %")
        icon = {"Charging": "⚡ ", "Full": "🔌 ", "Not charging": "🔌 "}.get(b["status"], "")
        c.subtitle.setText(icon + b["label"])
        # jauge « réservoir » : rouge quand c'est presque vide, cyan quand c'est plein
        c.mem.set(pct / 100, reverse=True)
        left = duration(b["left"]) if b["left"] else None
        if b["status"] == "Charging":
            text = f"Charge  +{fmt(b['watts'], ' W', 1)}" + (f"   pleine dans {left}" if left else "")
        elif b["status"] == "Discharging":
            text = f"Décharge  −{fmt(b['watts'], ' W', 1)}" + (f"   reste {left}" if left else "")
        else:
            text = "Sur secteur"
        c.stats.setText(text)
        self.fill_split(c)

    def powers(self):
        """Puissances par composant (W), à partir des meilleurs capteurs disponibles."""
        d = self.data
        apu = d.get("apu") or {}
        cores = apu.get("cores")
        package = apu.get("socket") or d["cpu"]["power"]          # puce entière
        dgpu = next((0.0 if g["asleep"] else g["power"]
                     for gpu, g in zip(self.gpus, d["gpus"]) if not gpu.integrated), None)
        return {"cpu": cores if cores is not None else package, "cpu_is_cores": cores is not None,
                "igpu": apu.get("gfx"), "soc": apu.get("soc"), "package": package, "dgpu": dgpu}

    def part_labels(self):
        """Noms affichés dans la consommation : le vrai matériel plutôt que « CPU / iGPU »."""
        igpu = next((g for g in self.gpus if g.integrated), None)
        dgpu = next((g for g in self.gpus if not g.integrated), None)
        return {
            "CPU": short_name(self.cpu.name),
            "iGPU": short_name(igpu.name) if igpu else "iGPU",                      # Radeon 780M
            "dGPU": short_name(dgpu.name).replace("Radeon ", "") if dgpu else "dGPU",  # RX 7700S
            "SoC": "SoC",
            "Puce": "Puce (autre)",
            "Reste": "Écran, RAM…",
        }

    def part_names(self):
        names = ["CPU"] + (["iGPU"] if self.apu else [])
        if any(not g.integrated for g in self.gpus):
            names.append("dGPU")
        if self.apu:
            names += ["SoC", "Puce"]
        if self.sys.bat:
            names.append("Reste")
        return names

    def split(self):
        """[(composant, watts)], total, texte du total — pour la barre de répartition."""
        pw = self.powers()
        b = self.sys.battery()
        parts = []
        if pw["cpu_is_cores"]:
            parts.append(("CPU", pw["cpu"]))
            if pw["igpu"] is not None:
                parts.append(("iGPU", pw["igpu"]))
            if pw["soc"] is not None:
                parts.append(("SoC", pw["soc"]))
            known = sum(w for _, w in parts)
            if pw["package"]:
                if known > pw["package"]:
                    # Les capteurs par partie ne moyennent pas sur la même durée que
                    # celui de la puce entière (le plus fiable) : on les ramène à
                    # proportion pour que leur somme ne dépasse pas la puce.
                    parts = [(n, w * pw["package"] / known) for n, w in parts]
                parts.append(("Puce", max(0.0, pw["package"] - known)))  # reste de la puce
        elif pw["package"] is not None:
            parts.append(("CPU", pw["package"]))
        if pw["dgpu"] is not None:
            parts.append(("dGPU", pw["dgpu"]))
        measured = sum(w for _, w in parts)
        if b and b["status"] == "Discharging" and b["watts"]:
            # sur batterie, le total est connu : le reste = écran, RAM, SSD, Wi-Fi, pertes…
            parts.append(("Reste", max(0.0, b["watts"] - measured)))
            return parts, max(b["watts"], measured), f"total {b['watts']:.1f} W"
        return parts, measured, f"mesuré {measured:.1f} W · total inconnu sur secteur"

    def fill_split(self, c):
        parts, total, text = self.split()
        c.split_total.setText(text)
        c.split.set(parts, total)

    def fill_gpu_mem(self, gpu, c, g):
        if gpu.integrated:
            # iGPU : petite VRAM réservée + GTT (prise sur la RAM)
            total = g["vram_total"] + g["gtt_total"]
            used = g["vram"] + g["gtt"]
            c.mem_label.setText(f"VRAM {size(g['vram'])} + GTT {size(g['gtt'])}")
            c.mem_pct.setText(size(used))
            c.mem.set(g["vram"] / total, g["gtt"] / total, scale=3)   # 1/3 de la RAM = déjà beaucoup
        else:
            pct = g["vram"] / g["vram_total"] * 100 if g["vram_total"] else 0
            c.mem_label.setText(f"VRAM  {gib(g['vram']):.1f} / {gib(g['vram_total']):.1f} Go")
            c.mem_pct.setText(f"{pct:.0f} %")
            c.mem.set(pct / 100)

    def update_detail(self):
        d = self.data
        c, cpu = self.d_cpu, d["cpu"]
        c.big.setText(fmt(cpu["load"], " %"))
        s = c.stats
        s.set("freq_avg", f"{cpu['freq_avg']:.2f} GHz")
        s.set("freq", f"{cpu['freq']:.2f} GHz")
        pw = self.powers()
        c.set_watts(pw["cpu"], "" if pw["cpu_is_cores"] else " puce")
        s.set("power", fmt(pw["cpu"], " W", 1))
        s.set("package", fmt(pw["package"], " W", 1))
        s.set("soc", fmt(pw["soc"], " W", 1))
        c.set_temp(cpu["temp"])
        s.set("temp", fmt(cpu["temp"], " °C", 1))
        s.set("cores", f"{self.cpu.cores} / {self.cpu.threads}")
        s.set("boost", f"{self.cpu.max_freq:.2f} GHz")
        s.set("loadavg", " / ".join(f"{x:.1f}" for x in cpu["loadavg"]))
        s.set("load", fmt(cpu["load"], " %", 1))
        c.threads.set(cpu["cores"], cpu["freqs"])

        # processus : un peu plus coûteux, une fois toutes les 2 s
        if self.tick % 2 == 1 or not hasattr(self, "proc_data"):
            self.proc_data = self.procs.sample()
        pd = self.proc_data

        sysd = self.sys.sample()
        c, s = self.d_sys, self.d_sys.stats
        b = sysd["battery"]
        if b:
            c.big.setText(f"{b['percent']:.0f} %")
            s.set("battery", f"{b['percent']:.0f} %")
            s.set("status", b["label"])
            sign = {"Discharging": "−", "Charging": "+"}.get(b["status"], "")
            s.set("watts", f"{sign}{fmt(b['watts'], ' W', 1)}" if sign else "secteur")
            s.set("left", duration(b["left"]) if b["left"] else "—")
        s.set("uptime", duration(sysd["uptime"]))
        s.set("procs", str(pd["count"]))
        self.fill_split(c)
        c.procs.set_items([(n, f"{v:.0f} %", v / 100) for n, v in pd["cpu"]])

        for gpu, c, g in zip(self.gpus, self.d_gpus, d["gpus"]):
            s = c.stats
            if g["asleep"]:
                c.big.setText("veille")
                c.set_watts(0.0)
                c.set_temp(None)
                for k in s.values:
                    s.set(k, "—")
                continue
            c.big.setText(fmt(g["load"], " %"))
            c.set_temp(g["temp"])
            s.set("freq", fmt(g["freq"], " GHz", 2))
            s.set("temp" if gpu.integrated else "edge", fmt(g["temps"].get("edge"), " °C"))
            s.set("volt", fmt(g["volt"], " mV"))
            if gpu.integrated:
                gfx = pw["igpu"] if pw["igpu"] is not None else g["power"]
                c.set_watts(gfx, "" if pw["igpu"] is not None else " puce")
                s.set("power", fmt(gfx, " W", 1))
                s.set("vram", f"{g['vram'] / 2**20:.0f} / {g['vram_total'] / 2**20:.0f} Mo")
                s.set("gtt", f"{size(g['gtt'])} / {gib(g['gtt_total']):.0f} Go")
            else:
                s.set("mclk", fmt(g["mclk"], " GHz", 2))
                c.set_watts(g["power"])
                s.set("power", f"{fmt(g['power'], ' W', 1)} / {fmt(g['cap'], ' W')}")
                s.set("fan", "arrêté" if g["fan"] == 0 else fmt(g["fan"], " tr/min"))
                s.set("junction", fmt(g["temps"].get("junction"), " °C"))
                s.set("mem", fmt(g["temps"].get("mem"), " °C"))
                s.set("link", g["link"] or "—")
                s.set("gtt", size(g["gtt"]))
            self.fill_gpu_mem(gpu, c, g)
            progs = pd["gpu"].get(gpu.pdev, [])
            if gpu.integrated:
                total = gpu.vram_total + gpu.gtt_total
                c.procs.set_items([(n, size(v + t), (v + t) / total * 3) for n, v, t in progs])
            else:
                c.procs.set_items([(n, size(v), v / gpu.vram_total) for n, v, _ in progs if v])

        c, r = self.d_ram, d["ram"]
        c.big.setText(fmt(r["load"], " %"))
        s = c.stats
        s.set("used", size(r["used"]))
        s.set("cache", size(r["cache"]))
        s.set("free", size(r["free"]))
        s.set("available", size(r["available"]))
        s.set("swap", f"{size(r['swap'])} / {size(r['swap_total'])}")
        c.set_temp(r["temp"])
        s.set("temp", " · ".join(f"{t:.0f} °C" for t in r["temps"]) or "—")
        used_c = heat_color(r["load"])
        c.legend.setText(legend((used_c, f"utilisée {gib(r['used']):.1f}"),
                                (faded(used_c, 0.5), f"cache {gib(r['cache']):.1f}"),
                                (QColor(MUTED), f"libre {gib(r['free']):.1f} Go")))
        c.mem.set(r["used"] / r["total"], r["cache"] / r["total"])
        swap_f = r["swap"] / r["swap_total"] if r["swap_total"] else 0
        c.swap_label.setText(f"Swap  {size(r['swap'])} / {size(r['swap_total'])}")
        c.swap.set(swap_f)
        c.procs.set_items([(n, size(v), v / r["total"] * 4) for n, v in pd["mem"]])

    # --- fenêtre sans bordure : fond + déplacement --------------------------

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 14, 14)
        p.fillPath(path, BG)

    def edges_at(self, pos):
        edges = Qt.Edge(0)
        if pos.x() <= EDGE:
            edges |= Qt.LeftEdge
        if pos.x() >= self.width() - EDGE:
            edges |= Qt.RightEdge
        if pos.y() <= EDGE:
            edges |= Qt.TopEdge
        if pos.y() >= self.height() - EDGE:
            edges |= Qt.BottomEdge
        return edges

    CURSORS = {
        Qt.LeftEdge: Qt.SizeHorCursor, Qt.RightEdge: Qt.SizeHorCursor,
        Qt.TopEdge: Qt.SizeVerCursor, Qt.BottomEdge: Qt.SizeVerCursor,
        Qt.LeftEdge | Qt.TopEdge: Qt.SizeFDiagCursor, Qt.RightEdge | Qt.BottomEdge: Qt.SizeFDiagCursor,
        Qt.RightEdge | Qt.TopEdge: Qt.SizeBDiagCursor, Qt.LeftEdge | Qt.BottomEdge: Qt.SizeBDiagCursor,
    }

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        edges = self.edges_at(e.position().toPoint())
        if edges != Qt.Edge(0):
            self.windowHandle().startSystemResize(edges)   # le bord : on redimensionne
        else:
            self.user_moving = True                          # seul un déplacement à la souris
            self.windowHandle().startSystemMove()          # peut ranger la fenêtre au bord

    def mouseMoveEvent(self, e):
        shape = self.CURSORS.get(self.edges_at(e.position().toPoint()))
        if shape is None:
            self.unsetCursor()
        else:
            self.setCursor(shape)

    def leaveEvent(self, e):
        self.unsetCursor()
        super().leaveEvent(e)

    def moveEvent(self, e):
        if not self.docked and time.monotonic() >= self.settle_until:
            self.settings.setValue("pos", self.pos())
        if self.isVisible():
            self.dock_timer.start()          # vérifié une fois le déplacement terminé
        super().moveEvent(e)

    # --- ranger au bord de l'écran ----------------------------------------

    def screen_geo(self):
        return (self.screen() or QApplication.primaryScreen()).availableGeometry()

    def check_dock(self):
        """Après un déplacement à la souris : déposée contre le bord gauche ou droit
        de l'écran → rangée. Si elle l'est déjà, on la recolle au bord (on peut
        glisser la languette le long de celui-ci)."""
        if self.docked:
            self.stick_to_edge()
            return
        if not getattr(self, "user_moving", False):
            return                       # démarrage, repositionnement automatique… : jamais
        self.user_moving = False
        geo = self.screen_geo()
        if self.x() <= geo.left() + DOCK_GAP:
            self.dock("left")
        elif self.x() + self.width() - 1 >= geo.right() - DOCK_GAP:
            self.dock("right")

    def dock(self, edge, y=None):
        self.docked = edge
        self.settings.setValue("dock_edge", edge)
        self.settle_until = time.monotonic() + 1.0
        for w in (self.topbar, self.compact, self.detail):
            w.hide()
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.dock_tab.edge = edge
        self.update_dock_tab()
        self.dock_tab.show()
        for _ in range(3):
            QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        self.layout().activate()
        self.resize(self.dock_tab.size())
        self.stick_to_edge(self.y() if y is None else y)

    def stick_to_edge(self, y=None):
        geo = self.screen_geo()
        x = geo.left() + DOCK_MARGIN if self.docked == "left" else geo.right() - self.width() + 1 - DOCK_MARGIN
        y = max(geo.top(), min(self.y() if y is None else y, geo.bottom() - self.height() + 1))
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)
        self.settings.setValue("dock_y", y)

    def undock(self):
        if not self.docked:
            return
        edge, self.docked = self.docked, None
        self.settings.remove("dock_edge")
        self.settle_until = time.monotonic() + 1.0
        self.dock_tab.hide()
        self.layout().setContentsMargins(10, 8, 10, 10)
        self.topbar.show()
        (self.detail if self.detailed else self.compact).show()
        self.refresh(sample=False)
        for _ in range(3):
            QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        self.apply_view_size()
        geo = self.screen_geo()
        x = geo.left() + 8 if edge == "left" else geo.right() - self.width() - 7
        y = max(geo.top(), min(self.y(), geo.bottom() - self.height() + 1))
        self.move(x, y)
        self.settings.setValue("pos", QPoint(x, y))

    def update_dock_tab(self):
        d = getattr(self, "data", None)
        if not d:
            return
        levels = [d["cpu"]["load"]]
        levels += [None if g["asleep"] else g["load"] for g in d["gpus"]]
        levels.append(d["ram"]["load"])
        self.dock_tab.set_levels(levels)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Coup d'œil")
    app.setDesktopFileName("coup-doeil")        # relie la fenêtre à son lanceur
    app.setFont(QFont("Inter", 10))
    icon = Path(__file__).with_name("coup-doeil.svg")
    app.setWindowIcon(QIcon(str(icon)) if icon.exists() else QIcon.fromTheme("coup-doeil"))
    w = Monitor()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
