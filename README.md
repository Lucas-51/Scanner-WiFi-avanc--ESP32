# SondeDB — ESP32 + MySQL + PHP

Stack ultra simple : Apache + PHP + MySQL. Trois briques :

- un sketch ESP32 qui scanne les réseaux WiFi et remonte les alertes Evil Twin ;
- une mini-API PHP (`api.php`) qui reçoit les POST de la sonde et lit/écrit MySQL ;
- un dashboard HTML/JS servi par Apache.

## Déploiement sur Ubuntu (10.1.40.51)

Sur le serveur (Apache + MySQL + phpMyAdmin déjà installés) :

```bash
# 1. Activer mod_rewrite (une seule fois)
sudo a2enmod rewrite
sudo systemctl restart apache2

# 2. S'assurer que AllowOverride All est autorisé dans /etc/apache2/apache2.conf
#    pour le DocumentRoot (généralement /var/www/html).

# 3. Installer le module PHP MySQL si pas déjà fait
sudo apt install php php-mysql -y
```

Depuis ton Mac :

```bash
cd "Scanner WiFi avancé ESP32"
scp -r .htaccess api.php config.php db.php index.php login.html \
       sondedb_dashboard.html css js media sql firmware \
       lucas@10.1.40.51:/var/www/html/
```

Puis sur le serveur :

```bash
# Créer la base + tables
mysql -u root -p < /var/www/html/sql/sondedb.sql

# Permissions
sudo chown -R www-data:www-data /var/www/html
```

Édite `/var/www/html/config.php` pour mettre tes vraies creds MySQL et un nouveau token API.

Le dashboard est alors disponible sur `http://10.1.40.51/`.
Login par défaut : `admin` / `admin1234` (à changer immédiatement).

## Endpoints API

- `GET  /api/health`              — ping API + MySQL
- `POST /api/login`               — auth (form-urlencoded `username`/`password`)
- `GET  /api/logout`              — destruction de session
- `GET  /api/dashboard`           — payload normalisé (auth requise)
- `POST /api/ingest`              — ingestion JSON ESP32 (token)
- `POST /api/purge`               — supprime mesures + alertes (5/15/30/60 min)
- `GET  /api/probe/{id}/sync`     — config/état pour l'ESP32 (token)
- `POST /api/probe/{id}/settings` — modification config (auth requise)

## Format JSON envoyé par l'ESP32

```json
{
  "probe":  { "id": 1, "name": "ESP32-LAB", "location": "Lab CIEL" },
  "measurements": [ { "ssid": "...", "bssid": "AA:BB:..", "rssi": -62, "channel": 6 } ],
  "alerts": [ { "type": "Evil Twin", "description": "...", "level": "critical" } ]
}
```

L'ESP32 doit envoyer le header `X-API-Token: <token>` (ou `Authorization: Bearer …`).

## Firmware

Sketch dans `firmware/esp32_wifi_probe/esp32_wifi_probe.ino`.
Avant compilation, remplace `WIFI_SSID`, `WIFI_PASSWORD`, `API_URL`
(`http://10.1.40.51/api/ingest`), `API_TOKEN`, `PROBE_ID`, et `trustedAps[]`.

## Structure

```
config.php         creds MySQL + token API
db.php             PDO + helpers (auth PBKDF2, JSON, normalisation)
api.php            routeur unique pour /api/*
index.php          dashboard (redirige vers login si pas de session)
login.html         page de connexion
sondedb_dashboard.html   dashboard HTML
.htaccess          rewrite mod_rewrite : /api/* -> api.php
css/, js/, media/  assets statiques
sql/sondedb.sql    schéma complet + admin par défaut
firmware/          sketch ESP32
```
