<?php
/**
 * SondeDB — Page d'accueil
 * ────────────────────────────────────────────────────────────────────
 * Vérifie qu'une session existe, sinon redirige vers la page de login.
 * Si OK : affiche le dashboard.
 */
require __DIR__ . '/db.php';

if (!current_user()) {
    header('Location: /login.html');
    exit;
}

readfile(__DIR__ . '/sondedb_dashboard.html');
