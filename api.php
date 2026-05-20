<?php
// SondeDB — Routeur API unique (toutes les routes /api/*)

require __DIR__ . '/db.php';

header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type, Authorization, X-API-Token');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') { http_response_code(204); exit; }

$path = parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH);
$method = $_SERVER['REQUEST_METHOD'];

try {
    // GET /api/health
    if ($method === 'GET' && $path === '/api/health') {
        db()->query('SELECT 1');
        send_json(200, ['status' => 'ok', 'server_time' => now_sql()]);
    }

    // POST /api/login
    if ($method === 'POST' && $path === '/api/login') {
        $u = norm_text($_POST['username'] ?? '');
        $p = norm_text($_POST['password'] ?? '');
        if ($u === '' || $p === '') send_error(400, 'Identifiant et mot de passe requis.');

        $st = db()->prepare('SELECT password_hash FROM users WHERE username = ? LIMIT 1');
        $st->execute([$u]);
        $row = $st->fetch();
        if (!$row || !verify_pbkdf2($row['password_hash'], $p)) {
            send_error(401, 'Identifiant ou mot de passe incorrect.');
        }
        start_session_safe();
        $_SESSION['user'] = $u;
        send_json(200, ['status' => 'ok', 'username' => $u]);
    }

    // GET /api/logout
    if ($method === 'GET' && $path === '/api/logout') {
        start_session_safe();
        $_SESSION = [];
        session_destroy();
        header('Location: /login.html');
        exit;
    }

    // GET /api/dashboard
    if ($method === 'GET' && $path === '/api/dashboard') {
        require_login();
        $c = cfg();
        $pdo = db();
        $data = [];

        $data['sondes'] = $pdo->query("
            SELECT p.id, p.name AS nom, p.location AS localisation,
                   DATE_FORMAT(p.deploy_date, '%Y-%m-%d %H:%i:%s') AS date_deploiement,
                   DATE_FORMAT(MAX(m.timestamp), '%Y-%m-%d %H:%i:%s') AS last_seen,
                   p.is_active
            FROM probes p
            LEFT JOIN measurements m ON m.probe_id = p.id
            GROUP BY p.id ORDER BY p.name ASC
        ")->fetchAll();

        $st = $pdo->prepare("
            SELECT id, probe_id AS id_sonde, alert_type AS type_alerte, description,
                   CASE WHEN severity IN ('critical','high') THEN 'critical'
                        WHEN severity = 'medium' THEN 'warning'
                        ELSE 'info' END AS niveau,
                   DATE_FORMAT(timestamp, '%Y-%m-%d %H:%i:%s') AS horodatage
            FROM alerts ORDER BY timestamp DESC LIMIT ?
        ");
        $st->bindValue(1, (int)$c['limit_alertes'], PDO::PARAM_INT);
        $st->execute(); $data['alertes'] = $st->fetchAll();

        $st = $pdo->prepare("
            SELECT id, probe_id AS id_sonde, technician AS technicien, description,
                   DATE_FORMAT(intervention_date, '%Y-%m-%d %H:%i:%s') AS date_intervention
            FROM interventions ORDER BY intervention_date DESC LIMIT ?
        ");
        $st->bindValue(1, (int)$c['limit_interventions'], PDO::PARAM_INT);
        $st->execute(); $data['interventions'] = $st->fetchAll();

        $st = $pdo->prepare("
            SELECT * FROM (
              SELECT id, probe_id AS id_sonde, ssid, bssid, rssi, channel AS canal,
                     DATE_FORMAT(timestamp, '%Y-%m-%d %H:%i:%s') AS horodatage
              FROM measurements ORDER BY timestamp DESC LIMIT ?
            ) r ORDER BY horodatage ASC, id ASC
        ");
        $st->bindValue(1, (int)$c['limit_mesures'], PDO::PARAM_INT);
        $st->execute(); $data['mesures'] = $st->fetchAll();

        $st = $pdo->prepare("
            SELECT id, ssid, bssid, channel AS canal,
                   DATE_FORMAT(first_seen, '%Y-%m-%d %H:%i:%s') AS date_detection
            FROM wifi_networks ORDER BY first_seen DESC LIMIT ?
        ");
        $st->bindValue(1, (int)$c['limit_reseaux'], PDO::PARAM_INT);
        $st->execute(); $data['reseaux'] = $st->fetchAll();

        $data['meta'] = ['source' => 'mysql', 'updated_at' => now_sql(),
                         'db_host' => $c['db_host'], 'db_name' => $c['db_name']];
        send_json(200, $data);
    }

    // POST /api/ingest — ESP32
    if ($method === 'POST' && $path === '/api/ingest') {
        $payload = read_json_body();
        if (!check_api_token($payload)) send_error(401, 'Token API invalide.');

        $probe = is_array($payload['probe'] ?? null) ? $payload['probe'] : [];
        $probeId = norm_int(pick($probe,'id','probe_id','id_sonde') ?? pick($payload,'probe_id','id_sonde','id'));
        $name = norm_text(pick($probe,'name','nom'), 'ESP32-' . ($probeId > 0 ? $probeId : 'NEW'));
        $loc  = norm_text(pick($probe,'location','localisation'), 'Location to be defined');
        $depl = norm_timestamp(pick($probe,'deploy_date','date_deploiement','deployed_at') ?? pick($payload,'deploy_date'));

        $pdo = db();
        $pdo->beginTransaction();
        try {
            if ($probeId > 0) {
                $pdo->prepare("INSERT INTO probes (id,name,location,deploy_date) VALUES (?,?,?,?)
                               ON DUPLICATE KEY UPDATE name=VALUES(name), location=VALUES(location), deploy_date=VALUES(deploy_date)")
                    ->execute([$probeId, $name, $loc, $depl]);
            } else {
                $st = $pdo->prepare("SELECT id FROM probes WHERE name=? AND location=? LIMIT 1");
                $st->execute([$name, $loc]);
                $row = $st->fetch();
                if ($row) {
                    $probeId = (int)$row['id'];
                    $pdo->prepare("UPDATE probes SET deploy_date=? WHERE id=?")->execute([$depl, $probeId]);
                } else {
                    $pdo->prepare("INSERT INTO probes (name,location,deploy_date) VALUES (?,?,?)")
                        ->execute([$name, $loc, $depl]);
                    $probeId = (int)$pdo->lastInsertId();
                }
            }

            // Mesures
            $measList = is_array($payload['measurements'] ?? null) ? $payload['measurements']
                      : (is_array($payload['mesures'] ?? null) ? $payload['mesures'] : []);
            $measCount = 0;
            $insMeas = $pdo->prepare("INSERT INTO measurements (probe_id,ssid,bssid,rssi,channel,timestamp) VALUES (?,?,?,?,?,?)");
            $insNet  = $pdo->prepare("INSERT INTO wifi_networks (ssid,bssid,channel,first_seen) VALUES (?,?,?,?)
                                      ON DUPLICATE KEY UPDATE ssid=VALUES(ssid), channel=VALUES(channel)");
            foreach ($measList as $m) {
                if (!is_array($m)) continue;
                $ssid = norm_text(pick($m,'ssid','name'), '<hidden>');
                $bssid = norm_bssid(pick($m,'bssid','mac'));
                if ($bssid === '' && $ssid === '<hidden>') continue;
                if ($bssid === '') $bssid = '00:00:00:00:00:00';
                $rssi = norm_int(pick($m,'rssi','signal'), -100);
                $ch   = norm_int(pick($m,'canal','channel'), 0);
                $ts   = norm_timestamp(pick($m,'horodatage','detected_at','timestamp'));
                $insMeas->execute([$probeId, $ssid, $bssid, $rssi, $ch, $ts]);
                $insNet->execute([$ssid, $bssid, $ch, $ts]);
                $measCount++;
            }

            // Alertes
            $alertList = is_array($payload['alerts'] ?? null) ? $payload['alerts']
                       : (is_array($payload['alertes'] ?? null) ? $payload['alertes'] : []);
            $alertCount = 0;
            $insAlert = $pdo->prepare("INSERT INTO alerts (probe_id,alert_type,description,severity,timestamp) VALUES (?,?,?,?,?)");
            foreach ($alertList as $a) {
                if (!is_array($a)) continue;
                $type = norm_text(pick($a,'type_alerte','type'));
                $desc = norm_text($a['description'] ?? '');
                if ($type === '' && $desc === '') continue;
                $insAlert->execute([
                    $probeId,
                    $type !== '' ? $type : 'WiFi anomaly',
                    $desc !== '' ? $desc : 'ESP32 probe alert.',
                    severity_alias(pick($a,'severity','niveau','level')),
                    norm_timestamp(pick($a,'timestamp','horodatage','detected_at')),
                ]);
                $alertCount++;
            }
            $pdo->commit();
        } catch (Throwable $e) { $pdo->rollBack(); throw $e; }

        send_json(201, [
            'status' => 'ok', 'probe_id' => $probeId,
            'measurements_saved' => $measCount, 'alerts_saved' => $alertCount,
            'server_time' => now_sql(),
        ]);
    }

    // POST /api/purge
    if ($method === 'POST' && $path === '/api/purge') {
        require_login();
        $body = read_json_body();
        $pdo = db();

        if (($body['scope'] ?? '') === 'all') {
            // Tout supprimer : mesures, alertes, et réseaux détectés
            $dm = $pdo->exec("DELETE FROM measurements");
            $da = $pdo->exec("DELETE FROM alerts");
            $dn = $pdo->exec("DELETE FROM wifi_networks");
            send_json(200, ['status'=>'ok','scope'=>'all',
                            'deleted_measurements'=>(int)$dm,
                            'deleted_alerts'=>(int)$da,
                            'deleted_networks'=>(int)$dn]);
        }

        $minutes = norm_int($body['minutes'] ?? 0);
        if (!in_array($minutes, [5,15,30,60], true)) {
            send_error(400, 'Intervalle invalide. Valeurs : 5, 15, 30, 60 ou scope=all.');
        }
        $st = $pdo->prepare("DELETE FROM measurements WHERE timestamp >= NOW() - INTERVAL ? MINUTE");
        $st->bindValue(1, $minutes, PDO::PARAM_INT); $st->execute();
        $dm = $st->rowCount();
        $st = $pdo->prepare("DELETE FROM alerts WHERE timestamp >= NOW() - INTERVAL ? MINUTE");
        $st->bindValue(1, $minutes, PDO::PARAM_INT); $st->execute();
        $da = $st->rowCount();
        send_json(200, ['status'=>'ok','interval_minutes'=>$minutes,
                        'deleted_measurements'=>$dm,'deleted_alerts'=>$da]);
    }

    // GET /api/probe/{id}/sync — ESP32
    if ($method === 'GET' && preg_match('#^/api/probe/(\d+)/sync$#', $path, $m)) {
        if (!check_api_token()) send_error(401, 'Token API invalide.');
        $id = (int)$m[1];
        $pdo = db();
        $st = $pdo->prepare("SELECT is_active, config_pending FROM probes WHERE id = ?");
        $st->execute([$id]);
        $row = $st->fetch();
        if (!$row) send_json(200, ['active'=>true,'config'=>null]);
        $config = $row['config_pending'] ? json_decode($row['config_pending'], true) : null;
        if ($config) {
            $pdo->prepare("UPDATE probes SET config_pending=NULL WHERE id=?")->execute([$id]);
        }
        send_json(200, ['active' => (bool)$row['is_active'], 'config' => $config]);
    }

    // POST /api/probe/{id}/settings — dashboard
    if ($method === 'POST' && preg_match('#^/api/probe/(\d+)/settings$#', $path, $m)) {
        require_login();
        $id = (int)$m[1];
        $body = read_json_body();
        $config = [];
        if (isset($body['name']))     $config['name']     = norm_text($body['name']);
        if (isset($body['location'])) $config['location'] = norm_text($body['location']);
        $active = $body['active'] ?? null;

        $pdo = db();
        if ($config && $active !== null) {
            $pdo->prepare("UPDATE probes SET config_pending=?, is_active=? WHERE id=?")
                ->execute([json_encode($config), $active ? 1 : 0, $id]);
        } elseif ($config) {
            $pdo->prepare("UPDATE probes SET config_pending=? WHERE id=?")
                ->execute([json_encode($config), $id]);
        } elseif ($active !== null) {
            $pdo->prepare("UPDATE probes SET is_active=? WHERE id=?")
                ->execute([$active ? 1 : 0, $id]);
        }
        send_json(200, ['status' => 'ok']);
    }

    // GET /api/speedtest/download — chunk de 2 Mo pour mesurer la bande passante descendante
    if ($method === 'GET' && $path === '/api/speedtest/download') {
        $size = 2 * 1024 * 1024; // 2 MB
        header('Content-Type: application/octet-stream');
        header('Content-Length: ' . $size);
        header('Cache-Control: no-cache, no-store, must-revalidate');
        header('Access-Control-Allow-Origin: *');
        echo str_repeat('X', $size);
        exit;
    }

    // POST /api/speedtest/upload — reçoit les données et renvoie la taille mesurée
    if ($method === 'POST' && $path === '/api/speedtest/upload') {
        $data = file_get_contents('php://input');
        send_json(200, ['received_bytes' => strlen($data), 'status' => 'ok']);
    }

    send_error(404, 'Point d\'entrée API introuvable.');

} catch (Throwable $e) {
    error_log('[SondeDB] ' . $e->getMessage());
    send_error(500, $e->getMessage());
}
