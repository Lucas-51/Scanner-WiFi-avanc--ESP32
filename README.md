# SondeDB — Scanner WiFi avancé ESP32

Projet BTS CIEL — détection des réseaux WiFi et anomalies (Evil Twin)
à l'aide d'une sonde ESP32-WROOM-32, d'une API PHP et d'une base MySQL.

## Vue d'ensemble

```
┌──────────────┐    HTTP POST JSON     ┌──────────────────┐    SQL    ┌─────────┐
│   ESP32      │ ────────────────────▶ │   api.php (PHP)  │ ────────▶ │  MySQL  │
│  (firmware)  │   /api/ingest         │                  │           │ sondedb │
└──────────────┘                       └──────────────────┘           └─────────┘
                                              ▲
                                              │ /api/dashboard
                                       ┌──────────────────┐
                                       │  Dashboard HTML  │
                                       │   (navigateur)   │
                                       └──────────────────┘
```

Trois briques :
1. **ESP32** — scanne les réseaux WiFi 2.4 GHz et envoie les mesures
2. **API PHP** — reçoit les scans et lit/écrit dans MySQL
3. **Dashboard** — affiche les données en temps réel

## Arborescence

```
├── config.php              Configuration centrale (DB, token API)
├── db.php                  Connexion PDO + fonctions utilitaires
├── api.php                 Routeur unique pour /api/*
├── index.php               Page d'accueil (vérifie la session)
├── login.html              Page de connexion
├── sondedb_dashboard.html  Dashboard HTML
├── .htaccess               Règles de rewriting Apache
│
├── css/style.css           Styles du dashboard
├── js/dashboard.js         Logique du dashboard (KPIs, charts, tables)
├── js/live-data.js         Rafraîchissement automatique toutes les 8 s
├── js/speedtest.js         Mesure RSSI + débit download/upload
│
├── sql/sondedb.sql         Schéma de la base + compte admin par défaut
└── firmware/esp32_wifi_probe/esp32_wifi_probe.ino   Sketch Arduino C++
```

## Déploiement

Sur le serveur Ubuntu (`10.1.40.51` — Apache + MySQL + phpMyAdmin) :

```bash
sudo a2enmod rewrite
sudo apt install php php-mysql -y
sudo systemctl restart apache2
```

Depuis le Mac :

```bash
scp -r .htaccess api.php config.php db.php index.php login.html \
       sondedb_dashboard.html css js sql firmware \
       lucas@10.1.40.51:/tmp/sondedb/

ssh -t lucas@10.1.40.51 'sudo cp -r /tmp/sondedb/. /var/www/html/ &&
                        sudo mysql < /var/www/html/sql/sondedb.sql &&
                        sudo chown -R www-data:www-data /var/www/html'
```

Édite `config.php` pour mettre tes propres credentials MySQL et un nouveau token API.

Dashboard : **http://10.1.40.51/**
Login par défaut : `admin` / `admin1234` (à changer immédiatement)

## Endpoints API

| Méthode | Route                          | Description                        |
|---------|--------------------------------|------------------------------------|
| GET     | `/api/health`                  | Ping serveur + base de données     |
| POST    | `/api/login`                   | Connexion utilisateur              |
| GET     | `/api/logout`                  | Déconnexion                        |
| GET     | `/api/dashboard`               | Données pour l'interface           |
| POST    | `/api/ingest`                  | Réception des scans ESP32          |
| POST    | `/api/purge`                   | Purge des données (5/15/30/60/all) |
| GET     | `/api/probe/{id}/sync`         | Config + état pour l'ESP32         |
| POST    | `/api/probe/{id}/settings`     | Modification d'une sonde           |
| GET     | `/api/speedtest/download`      | Test débit descendant (2 Mo)       |
| POST    | `/api/speedtest/upload`        | Test débit montant                 |

## Format JSON envoyé par l'ESP32

```json
{
  "probe":        { "id": 1, "name": "ESP32-LAB", "location": "Lab CIEL" },
  "measurements": [ { "ssid": "...", "bssid": "AA:BB:..", "rssi": -62, "channel": 6 } ],
  "alerts":       [ { "type": "Evil Twin", "description": "...", "level": "critical" } ]
}
```

Header HTTP requis : `X-API-Token: <token>` (défini dans `config.php`).

## Firmware ESP32

Sketch dans `firmware/esp32_wifi_probe/esp32_wifi_probe.ino`.
La configuration se fait via le **portail WiFiManager** au premier démarrage :
maintenir GPIO0 (BOOT) 3 secondes pour rouvrir le portail à tout moment.

Champs à remplir dans le portail :
- SSID + mot de passe du WiFi
- URL API : `http://10.1.40.51/api/ingest`
- Token API
- Nom + localisation + ID de la sonde
