# Coup d'œil

Un petit moniteur système pour Linux, à garder dans un coin de l'écran : CPU, GPU,
iGPU, RAM et batterie en un coup d'œil, avec des graphiques en points qui défilent
en douceur.

<p align="center">
  <img src="captures/vue-compacte.png" alt="Vue compacte" height="560">
  &nbsp;&nbsp;
  <img src="captures/grande-vue.png" alt="Grande vue" height="560">
</p>

*Captures réalisées avec des données de démonstration.*

## Ce qu'il affiche

| Module | Vue compacte | Grande vue (bouton ⤢) |
|---|---|---|
| **CPU** | charge, fréquences, température, ⚡ watts des cœurs, un point par thread | température dans le temps, puce entière / SoC, charge moyenne, colonne par thread avec sa fréquence |
| **GPU / iGPU** | charge, fréquence, température, ⚡ watts, remplissage de la VRAM | fréquence mémoire, ventilateur, 3 températures, tension, lien PCIe, **VRAM par programme** |
| **RAM** | utilisée / cache / libre, swap, température des barrettes | historique RAM et swap, **programmes les plus gourmands** |
| **Batterie** | %, charge ou décharge en watts, temps restant, **consommation par composant** | idem dans la carte Système, avec les programmes les plus gourmands en CPU |

- **📌 Toujours au-dessus** des autres fenêtres.
- **Modules déplaçables** : attrape la poignée ⠿ à gauche d'un titre (dans la grande vue, d'une colonne à l'autre).
- **Modules repliables** (vue compacte) : le bouton ▾ réduit un module à sa ligne de titre (température, watts, %) et la fenêtre rétrécit d'autant ; ▸ le redéplie.
- **Consommation repliable** : le ▾ devant « Consommation » (module Batterie, carte Système) masque la liste des composants et ne garde que le total.
- **Rangée au bord de l'écran** : dépose la fenêtre contre le bord gauche ou droit, elle se range en une languette (un point coloré par composant pour garder un œil sur la charge). Le bouton **⊟** (en haut à gauche) réduit la fenêtre en languette **sur place**, à mi-hauteur ; la languette se pose ensuite où l'on veut. Au clic, la fenêtre se déploie **centrée verticalement sur la languette** (décalée si besoin pour rester entière à l'écran). Une courte animation fait « sortir » la fenêtre de la languette, et l'y ramène (désactivable dans les options).
- **Coin supérieur gauche** (KDE Plasma) : y amener la souris affiche ou masque Coup d'œil — `./install.sh --coin` installe le petit script KWin correspondant (aussi disponible en raccourci clavier « Afficher / masquer Coup d'œil »).
- **Fenêtre redimensionnable** par ses bords ; les courbes prennent l'espace en plus. Chaque vue retient sa taille, sa position et l'ordre de ses modules.
- **Au lancement**, la fenêtre s'ouvre toujours dépliée, en vue compacte, à sa dernière place.
- **Consommation par composant** : classement trié, avec le nom du matériel (ex. Ryzen 7 7840HS, Radeon 780M, RX 7700S, SoC, reste de la puce, et sur batterie « écran, RAM… »).

## Options (bouton ⚙)

<img src="captures/options.png" alt="Options" align="right" height="300">

- **Rafraîchissement** : 0,5 / 1 / 2 / 5 s.
- **Lissage des courbes** : aucun, léger, moyen, fort.
- **Opacité du fond** : de 30 à 100 % (texte et courbes restent nets).
- **Défilement fluide** : à couper pour économiser un peu de CPU.
- **Animations** : transition languette ↔ fenêtre.
- **Lancer à l'ouverture de session**.
- **Modules affichés** : masquer ceux dont tu n'as pas besoin.
- **Réinitialiser la disposition** : ordre des modules et tailles par défaut.

Chaque réglage s'applique tout de suite et est retenu.
<br clear="right">

## Matériel pris en charge

Pensé et testé sur un **Framework Laptop 16** (Ryzen 7 7840HS, Radeon 780M, Radeon RX 7700S) sous **CachyOS / KDE Plasma**.

- **CPU** : tout x86 Linux (charge, fréquences). Température via `k10temp` (AMD), puissance via RAPL.
- **GPU** : cartes **AMD** (pilote `amdgpu`). Les noms commerciaux viennent de la base de Mesa (`/usr/share/libdrm/amdgpu.ids`).
- **Puissance détaillée de la puce** (cœurs / iGPU / SoC) : APU AMD exposant `gpu_metrics` v2.x.
- **Température de la RAM** : barrettes DDR5 avec sonde `spd5118`.

Un capteur absent n'est pas une erreur : la valeur s'affiche « — ».

## Installation

Dépendances : **Python 3** et **PySide6** (sur Arch / CachyOS : `sudo pacman -S pyside6`).

```bash
git clone https://github.com/BiasedMonkey/coup-doeil.git
cd coup-doeil
./install.sh
```

Le script copie l'appli dans `~/.local/share/coup-doeil/`, ajoute l'icône et le lanceur
« Coup d'œil » au menu. Options (combinables) : `--favori` l'ajoute aux favoris du menu
KDE, `--coin` active l'affichage / masquage par le coin supérieur gauche de l'écran.

Pour essayer sans installer : `python3 coup_doeil.py`.

## Bon à savoir

- **Wayland** : une application Wayland n'a le droit ni de se placer ni de rester au-dessus des autres. Coup d'œil passe donc par XWayland (`QT_QPA_PLATFORM=xcb`), où ces deux fonctions marchent.
- **Léger** : environ 2 % d'un cœur en vue compacte. Les capteurs restent ouverts et sont relus sans réouverture, chaque courbe n'est calculée qu'une fois par mesure, et l'animation ne fait que la faire glisser.
- **Net en mouvement** : les points des courbes sont posés sur une grille calée sur les pixels réels de l'écran (aussi en mise à l'échelle 125 %), et le défilement avance pixel par pixel. Chaque point garde ainsi exactement la même forme : pas de moiré ni de scintillement.
- **RAM « fantôme » des GPU** : quand un dGPU AMD se met en veille, le pilote recopie le contenu de sa VRAM dans la RAM. Un modèle d'IA local resté chargé peut ainsi occuper plusieurs Go de RAM sans apparaître dans les gestionnaires de tâches. La ligne « GTT » de la carte GPU (grande vue) le révèle.

## Vérifier

```bash
python3 tests/verifier.py
```

Teste sans écran, avec une configuration temporaire : déplacement des modules,
tailles retenues par vue, stabilité de l'interface (aucun module ne doit changer
de taille quand la batterie, le GPU ou les valeurs changent) et options.

## Outils

- `outils/galerie.py` : compare en direct plusieurs styles de graphiques en points (nécessite `psutil`).
- `outils/icones.py` : régénère les propositions d'icône dans `outils/icones/`.
- `outils/captures.py` : régénère les captures du README avec des données de démonstration.
