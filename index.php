<?php
require __DIR__ . '/db.php';
if (!current_user()) {
    header('Location: /login.html');
    exit;
}
readfile(__DIR__ . '/sondedb_dashboard.html');
