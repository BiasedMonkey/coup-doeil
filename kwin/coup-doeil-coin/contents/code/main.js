// Coup d'œil — les coins supérieurs gauche et droit de l'écran affichent ou masquent la fenêtre.
// (aussi disponible en raccourci clavier, à assigner dans Configuration du système)

function fenetres() {
    return workspace.windowList().filter(function (w) {
        // fenêtre principale seulement : la languette permanente (fenêtre
        // « utilitaire », donc pas normale) reste toujours affichée
        return w.normalWindow &&
            (w.desktopFileName === "coup-doeil" || w.resourceName === "coup_doeil.py");
    });
}

function basculer() {
    const liste = fenetres();
    if (!liste.length) {
        return;                       // Coup d'œil n'est pas lancé
    }
    // au moins une fenêtre visible → on masque tout ; sinon on réaffiche
    const masquer = liste.some(function (w) { return !w.minimized; });
    liste.forEach(function (w) {
        w.minimized = masquer;
        if (!masquer) {
            workspace.activeWindow = w;
        }
    });
}

// coins supérieurs gauche et droit : même effet
[KWin.ElectricTopLeft, KWin.ElectricTopRight].forEach(function (coin) {
    registerScreenEdge(coin, function () {
        basculer();
        return true;
    });
});
registerShortcut("CoupDoeilBascule", "Afficher / masquer Coup d'œil", "", basculer);
