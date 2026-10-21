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

from PySide6.QtCore import QPointF, QRectF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QGridLayout, QHBoxLayout, QLabel,
                               QLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

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


def pearl_layout(h):
    rows = max(4, int(h / PITCH))
    return rows, h / rows


def pearl_points(w, h, vals, dx, x0, x_true, passes=SMOOTH_PASSES):
    """Calcule la courbe perlée (une fois par seconde) sans rien dessiner.

    Colonnes de points en x0 + c·PITCH ; la courbe y est évaluée en x_true + c·PITCH
    (l'écart est rattrapé au moment de l'affichage). Renvoie :
      • groups  : {(couleur, rayon): [points]} → un drawPoints par groupe ;
      • outline : haut de la poussière par colonne, pour découper le fond.
    """
    rows, dy = pearl_layout(h)
    smooth = soften(vals, passes)
    raw = defaultdict(list)
    outline = []
    ys = [h - (row + 0.5) * dy for row in range(rows)]
    c = 0
    x = x0
    while x < w + PITCH:
        v = bspline(smooth, (x_true + c * PITCH) / dx - 0.5)
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
        x += PITCH
        c += 1
    groups = []
    for key, pts in raw.items():
        if key[0] == "perle":
            color, r = heat_color(key[1] * 2), 1.5
        else:
            row, k = key
            color, r = faded(heat_color((row + 1) / rows * 100), 0.15 + 0.5 * k / 8), 0.55 + 0.5 * k / 8
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


PHASES = 4      # positions intermédiaires préparées par pixel d'écran


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
            m = next((m for m in range(1, 17) if abs(m * PITCH * dpr - round(m * PITCH * dpr)) < 1e-6), 4)
            tw = m * PITCH
            rows, dy = pearl_layout(h)
            pen = QPen(DIM, 0.9)
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
                q.drawPoints([QPointF(PITCH / 2 + j * PITCH + off, h - (r + 0.5) * dy)
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
        phase = (self.clock.tick * dx) % PITCH * dpr
        n = math.floor(phase)
        frac = phase - n
        x0 = PITCH / 2 - n / dpr
        W = w + dx + PITCH
        groups, outline = pearl_points(W - PITCH, h, self.values(), dx, x0, x0 - frac / dpr)
        clip = QPainterPath()                    # le fond n'apparaît qu'au-dessus de la courbe
        clip.moveTo(0, 0)
        for x, y in outline:
            clip.lineTo(x - PITCH / 2, y)
            clip.lineTo(x + PITCH / 2, y)
        clip.lineTo(W, 0)
        clip.closeSubpath()
        tiles, tw = self.dust_tiles(h, dpr)
        return Frame(groups, clip, tiles, tw, n, frac, (W, h), dpr)

    def shift(self):
        return min(1.0, (time.monotonic() - self.clock.last_sample) * 1000 / REFRESH_MS)

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
    """Consommation par composant, en classement : une ligne par composant,
    du plus gourmand au moins gourmand, chacune avec sa barre de points
    (longueur = part du total) et sa valeur. Se lit d'un coup d'œil : le plus
    gros consommateur est toujours en haut, toutes les barres partent du même bord."""

    ROW = 15          # hauteur d'une ligne (px)
    NAME_W = 44       # colonne du nom
    VALUE_W = 74      # colonne « 12.3 W  38 % »

    def __init__(self, names):
        super().__init__()
        # Toujours les mêmes lignes, dans un ordre qui ne change que pour un écart
        # net : la hauteur ne bouge jamais, et deux valeurs proches ne s'échangent
        # pas sans arrêt (sinon toute l'interface « saute »).
        self.order = list(names)
        self.parts, self.total, self.state = [(n, None) for n in names], 0.0, None
        self.setFixedHeight(len(names) * self.ROW)

    def reorder(self, values):
        """Tri à bulles avec hystérésis : on échange deux voisins seulement si
        celui du bas dépasse celui du haut de 0,7 W et de 20 %."""
        order, changed = self.order, True
        while changed:
            changed = False
            for i in range(len(order) - 1):
                a, b = values.get(order[i]) or 0.0, values.get(order[i + 1]) or 0.0
                if b > a + max(0.7, 0.2 * a):
                    order[i], order[i + 1] = order[i + 1], order[i]
                    changed = True

    def set(self, parts, total):
        values = dict(parts)
        self.reorder(values)
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
        font.setPixelSize(10)
        p.setFont(font)
        bar_x0, bar_x1 = self.NAME_W, w - self.VALUE_W - 6
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
            p.drawText(QRectF(11, y - 7, self.NAME_W - 11, 14), Qt.AlignVCenter | Qt.AlignLeft, name)
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
            p.drawText(QRectF(w - self.VALUE_W, y - 7, 40, 14), Qt.AlignVCenter | Qt.AlignRight,
                       f"{watts:.1f} W" if known else "—")
            if known:
                p.setPen(QColor(MUTED))
                p.drawText(QRectF(w - 32, y - 7, 32, 14), Qt.AlignVCenter | Qt.AlignRight, f"{frac * 100:.0f} %")


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
        font.setPointSizeF(6.5)
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


def label(text="", color=MUTED, px=11, bold=False, spacing=0):
    l = QLabel(text)
    l.setStyleSheet(f"color:{color}; font-size:{px}px; font-weight:{700 if bold else 400};"
                    f" letter-spacing:{spacing}px;")
    # Une ligne de texte garde toujours la même hauteur, même vide, et un texte
    # qui s'allonge ne peut pas élargir la fenêtre d'un coup : pas de sauts.
    l.setFixedHeight(round(px * 1.45) + 2)
    l.setMinimumWidth(1)
    return l


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


class Card(QWidget):
    """Bloc arrondi : poignée, titre, nom du matériel, grosse valeur à droite."""

    def __init__(self, module_id, title, subtitle="", big_px=17):
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
        head.addWidget(label(title, MUTED, 11, True, 1))
        self.subtitle = label(subtitle, TEXT, 11)
        head.addWidget(self.subtitle)
        head.addStretch()
        self.watts = label("", "#facc15", 11, True)     # ⚡ consommation du composant
        head.addWidget(self.watts)
        self.big = label("—", TEXT, big_px, True)
        self.big.setMinimumWidth(70)
        self.big.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        head.addWidget(self.big)
        self.body.addLayout(head)

    def set_watts(self, w, suffix=""):
        self.watts.setText("" if w is None else f"⚡ {w:.1f} W{suffix}")

    def add(self, w):
        if isinstance(w, DotGraph):
            # l'espace vertical en plus va aux graphes, au prorata de leur taille
            self.body.addWidget(w, w.base_height)
            self.elastic = True
        else:
            self.body.addWidget(w)
        return w

    def add_row(self, left, right=None):
        row = QHBoxLayout()
        a = label(left)
        row.addWidget(a)
        row.addStretch()
        b = label(right or "", TEXT, 10)
        row.addWidget(b)
        self.body.addLayout(row)
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

        root = QVBoxLayout(self)
        root.setSizeConstraint(QLayout.SetMinimumSize)  # taille libre, jamais sous le contenu
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.addWidget(label("● COUP D'ŒIL", MUTED, 10, True, 2))
        top.addStretch()
        self.expand = self._button("⤢", "Vue détaillée")
        self.expand.setCheckable(True)
        self.expand.toggled.connect(self.set_detailed)
        top.addWidget(self.expand)
        self.pin = self._button("📌", "Toujours au-dessus")
        self.pin.setCheckable(True)
        self.pin.toggled.connect(self.set_on_top)
        top.addWidget(self.pin)
        close = self._button("✕", "Fermer")
        close.clicked.connect(self.close)
        top.addWidget(close)
        root.addLayout(top)

        self.compact = self.build_compact()
        self.detail = self.build_detail()
        root.addWidget(self.compact)
        root.addWidget(self.detail)
        self.detail.hide()

        self.timer = QTimer(self, interval=REFRESH_MS, timeout=self.refresh)
        self.timer.start()
        self.refresh()
        self.anim = QTimer(self, interval=ANIM_MS, timeout=self.animate)
        self.anim.start()

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
        pos, size = self.wanted
        if self.size() != size:
            self.resize(size)
        if self.pos() != pos:
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

        c = self.c_cpu = Card("cpu", "CPU", short_name(self.cpu.name))
        c.add(self.graph("cpu", COMPACT_COLS))
        c.stats = c.add(label("", TEXT, 11))
        c.bar_label, _ = c.add_row(f"{self.cpu.threads} threads")
        c.bar = c.add(DotBar(self.cpu.threads))
        cards = {"cpu": c}

        self.c_gpus = []
        for i, gpu in enumerate(self.gpus):
            c = Card(f"gpu{i}", "iGPU" if gpu.integrated else "GPU", short_name(gpu.name))
            c.add(self.graph(f"gpu{i}", COMPACT_COLS))
            c.stats = c.add(label("", TEXT, 11))
            c.mem_label, c.mem_pct = c.add_row("")
            c.mem = c.add(HalftoneBar(14))
            self.c_gpus.append(c)
            cards[c.module_id] = c

        c = self.c_ram = Card("ram", "RAM", f"{gib(self.ram.total):.0f} Go")
        c.add(self.graph("ram", COMPACT_COLS))
        c.stats = c.add(label("", TEXT, 11))
        c.mem_label, c.mem_pct = c.add_row("")
        c.mem = c.add(HalftoneBar(14))
        c.swap_label, _ = c.add_row("")
        c.swap = c.add(HalftoneBar(5))
        cards["ram"] = c

        self.c_bat = None
        if self.sys.bat:
            c = self.c_bat = Card("bat", "BATTERIE", "")
            c.mem = c.add(HalftoneBar(9))
            c.stats = c.add(label("", TEXT, 11))
            c.split_label, c.split_total = c.add_row("Consommation")
            c.split = c.add(PowerRows(self.part_names()))
            cards["bat"] = c

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
        c.add(label(f"Charge — {HISTORY} s", MUTED, 10))
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
        c.split_label, c.split_total = c.add_row("Consommation par composant")
        c.split = c.add(PowerRows(self.part_names()))
        c.add(label("Programmes — CPU (100 % = 1 thread)", MUTED, 10))
        c.procs = c.add(ProcList(5))
        cards["sys"] = c

        self.d_gpus = []
        for i, gpu in enumerate(self.gpus):
            c = Card(f"gpu{i}", "iGPU" if gpu.integrated else "GPU", short_name(gpu.name), 20)
            c.add(label(f"Charge — {HISTORY} s", MUTED, 10))
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
        c.add(label(f"Utilisation — {HISTORY} s", MUTED, 10))
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

        self.place(cards, self.detail_cols, "order_detail",
                   [["cpu", "sys"], [f"gpu{i}" for i in range(len(self.gpus))], ["ram"]])
        return w

    # --- modules déplaçables --------------------------------------------

    def place(self, cards, columns, key, default):
        """Range les cartes dans les colonnes selon l'ordre enregistré (sinon défaut)."""
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
        """Une carte avec graphe s'étire en hauteur ; les autres gardent leur taille."""
        card.setSizePolicy(QSizePolicy.Preferred,
                           QSizePolicy.Expanding if card.elastic else QSizePolicy.Maximum)
        lay.insertWidget(index, card, 1 if card.elastic else 0)

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
        index = sum(1 for o in others if o.geometry().center().y() < y)
        current = next(l for _, l in cols if l.indexOf(card) >= 0)
        if current is lay and lay.indexOf(card) == index:
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
        if time.monotonic() >= self.settle_until and self.isVisible():
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
        c.stats.setText(f"{cpu['freq_avg']:.2f} GHz moy · {cpu['freq']:.2f} max"
                        f"   {fmt(cpu['temp'], ' °C')}{chip}")
        c.bar.set_levels([v / 100 for v in cpu["cores"]])

        for gpu, c, g in zip(self.gpus, self.c_gpus, d["gpus"]):
            if g["asleep"]:
                c.big.setText("veille")
                c.set_watts(0.0)
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
            c.stats.setText(f"{fmt(g['freq'], ' GHz', 2)}   {fmt(g['temp'], ' °C')}")
            self.fill_gpu_mem(gpu, c, g)

        c, r = self.c_ram, d["ram"]
        c.big.setText(fmt(r["load"], " %"))
        c.stats.setText(f"{gib(r['used']):.1f} / {gib(r['total']):.1f} Go"
                        f"   cache {gib(r['cache']):.1f} Go   {fmt(r['temp'], ' °C')}")
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

    def part_names(self):
        names = ["CPU"] + (["iGPU", "SoC", "Puce"] if self.apu else [])
        if any(not g.integrated for g in self.gpus):
            names.append("dGPU")
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
                for k in s.values:
                    s.set(k, "—")
                continue
            c.big.setText(fmt(g["load"], " %"))
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
            self.windowHandle().startSystemMove()          # ailleurs : on déplace

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
        if time.monotonic() >= self.settle_until:
            self.settings.setValue("pos", self.pos())
        super().moveEvent(e)


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
