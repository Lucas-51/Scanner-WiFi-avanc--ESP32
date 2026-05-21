<?php
/**
 * SondeDB — Configuration centrale
 * ────────────────────────────────────────────────────────────────────
 * Toutes les valeurs sensibles ou variables de l'application sont ici.
 * Pour déployer sur un autre serveur, seul ce fichier doit être édité.
 */

return [
    'db_host'  => '127.0.0.1',
    'db_port'  => 3306,
    'db_name'  => 'sondedb',
    'db_user'  => 'lucas',
    'db_pass'  => 'lucas',

    // Token partagé avec les ESP32 (mettre '' pour désactiver l'auth)
    'api_token' => 'e23a2ffc3daafc579d5e828cf57cd5506a843d8bf1ca4e10c94f86c8148ba7f9',

    'limit_mesures'       => 250,
    'limit_alertes'       => 100,
    'limit_reseaux'       => 100,
    'limit_interventions' => 50,
];
