<?php
/**
 * SondeDB — Base de données + fonctions utilitaires
 * ────────────────────────────────────────────────────────────────────
 * - cfg()                : retourne le tableau de config (singleton)
 * - db()                 : retourne la connexion PDO (singleton)
 * - send_json/send_error : réponse HTTP JSON
 * - read_json_body       : parse le body JSON d'une requête POST
 * - norm_*               : nettoyage des données reçues de l'ESP32
 * - verify_pbkdf2        : vérification du mot de passe
 * - check_api_token      : auth ESP32 par token
 */

function cfg(): array {
    static $cfg = null;
    if ($cfg === null) $cfg = require __DIR__ . '/config.php';
    return $cfg;
}

function db(): PDO {
    static $pdo = null;
    if ($pdo !== null) return $pdo;
    $c = cfg();
    // Sur Ubuntu, MySQL n'écoute que sur socket Unix — on l'utilise directement
    $dsn = "mysql:unix_socket=/var/run/mysqld/mysqld.sock;dbname={$c['db_name']};charset=utf8mb4";
    $pdo = new PDO($dsn, $c['db_user'], $c['db_pass'], [
        PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        PDO::ATTR_EMULATE_PREPARES   => false,
    ]);
    try { $pdo->exec("SET time_zone = '+02:00'"); } catch (\Throwable $e) { /* ignore */ }
    return $pdo;
}

function send_json(int $status, $payload): void {
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode($payload, JSON_UNESCAPED_UNICODE);
    exit;
}

function send_error(int $status, string $message): void {
    send_json($status, ['status' => 'error', 'error' => $message]);
}

function read_json_body(): array {
    $raw = file_get_contents('php://input') ?: '{}';
    $data = json_decode($raw, true);
    if (!is_array($data)) send_error(400, 'Payload JSON invalide.');
    return $data;
}

function now_sql(): string { return date('Y-m-d H:i:s'); }

function norm_text($v, string $default = ''): string {
    if ($v === null) return $default;
    $t = trim((string)$v);
    return $t === '' ? $default : $t;
}

function norm_int($v, int $default = 0): int {
    if (is_numeric($v)) return (int)$v;
    return $default;
}

function norm_bssid($v): string {
    $t = strtoupper(norm_text($v));
    $hex = preg_replace('/[^0-9A-F]/', '', $t);
    if (strlen($hex) === 12) {
        return implode(':', str_split($hex, 2));
    }
    return $t;
}

function norm_timestamp($v): string {
    $t = norm_text($v);
    if ($t === '') return now_sql();
    $ts = strtotime($t);
    return $ts ? date('Y-m-d H:i:s', $ts) : now_sql();
}

function pick($arr, ...$keys) {
    foreach ($keys as $k) {
        if (isset($arr[$k]) && $arr[$k] !== '') return $arr[$k];
    }
    return null;
}

function severity_alias($v): string {
    $map = ['critical'=>'critical','high'=>'high','warning'=>'medium','warn'=>'medium',
            'medium'=>'medium','info'=>'low','low'=>'low','ok'=>'low'];
    return $map[strtolower(norm_text($v, 'medium'))] ?? 'medium';
}

function start_session_safe(): void {
    if (session_status() === PHP_SESSION_NONE) {
        session_name('sondedb_session');
        session_start();
    }
}

function current_user(): ?string {
    start_session_safe();
    return $_SESSION['user'] ?? null;
}

function require_login(): string {
    $u = current_user();
    if (!$u) send_error(401, 'Session expirée. Reconnectez-vous.');
    return $u;
}

// Vérifie un hash PBKDF2 au format Werkzeug : pbkdf2:sha256:iter$salthex$hashhex
function verify_pbkdf2(string $stored, string $password): bool {
    $parts = explode('$', $stored);
    if (count($parts) !== 3) return false;
    [$prefix, $saltHex, $expectedHex] = $parts;
    $bits = explode(':', $prefix);
    if (count($bits) !== 3 || $bits[0] !== 'pbkdf2') return false;
    $algo = $bits[1];
    $iter = (int)$bits[2];
    $salt = hex2bin($saltHex);
    $expected = hex2bin($expectedHex);
    if ($salt === false || $expected === false) return false;
    $dk = hash_pbkdf2($algo, $password, $salt, $iter, strlen($expected), true);
    return hash_equals($expected, $dk);
}

function check_api_token(?array $payload = null): bool {
    $expected = cfg()['api_token'];
    if ($expected === '') return true;
    $h = $_SERVER['HTTP_X_API_TOKEN'] ?? '';
    $auth = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
    $bearer = str_starts_with($auth, 'Bearer ') ? trim(substr($auth, 7)) : '';
    $body = $payload['token'] ?? '';
    foreach ([$h, $bearer, $body] as $t) {
        if ($t !== '' && hash_equals($expected, (string)$t)) return true;
    }
    return false;
}
