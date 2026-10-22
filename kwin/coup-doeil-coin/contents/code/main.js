// Coup d'œil — le coin supérieur gauche de l'écran affiche ou masque la fenêtre.
// (aussi disponible en raccourci clavier, à assigner dans Configuration du système)

function fenetres() {
    return workspace.windowList().filter(function (w) {
        return w.desktopFileName === "coup-doeil" || w.resourceName === "coup_doeil.py";
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

registerScreenEdge(KWin.ElectricTopLeft, function () {
    basculer();
    return true;
});
registerShortcut("CoupDoeilBascule", "Afficher / masquer Coup d'œil", "", basculer);
