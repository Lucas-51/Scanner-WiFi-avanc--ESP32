# SondeDB — Scanner WiFi avancé ESP32

Projet BTS CIEL — détection des réseaux WiFi et anomalies (Evil Twin)
à l'aide d'une sonde ESP32-WROOM-32, d'une API PHP et d'une base MySQL.

## Vue d'ensemble

```
┌──────────────┐    HTTPS POST JSON    ┌──────────────────┐    SQL    ┌─────────┐
│   ESP32      │ ────────────────────▶ │   api.php (PHP)  │ ────────▶ │  MySQL  │
│  (firmware)  │   /api/ingest         │                  │           │ sondedb │
└──────────────┘                       └──────────────────┘           └─────────┘
                                              ▲
                                              │ /api/dashboard (polling 8s)
                                       ┌──────────────────┐
                                       │  Dashboard HTML  │
                                       │   (navigateur)   │
                                       └──────────────────┘
```

Trois briques :
1. **ESP32** — scanne les réseaux WiFi 2.4 GHz et envoie les mesures en HTTPS
2. **API PHP** — reçoit les scans et lit/écrit dans MySQL
3. **Dashboard** — affiche les données en temps réel (polling toutes les 8 s)

## Arborescence

```
Scanner WiFi avancé ESP32/
│
├── README.md                  Ce fichier
│
├── docs/                      📄 Documentation projet (BTS)
│   ├── MCD_SondeDB.png                  Modèle Conceptuel de Données
│   ├── Sequence_EvilTwin_SondeDB.png    Diagramme de séquence
│   └── Cahier_Recettage_SondeDB.docx    Plan de tests
│
├── index.php                  ⚙️  Entrée dashboard (vérif session)
├── login.html                 🔐  Page de connexion (HTML pur)
├── api.php                    🌐  Routeur API REST
├── config.php                 🔒  Config DB + token (jamais exposé)
├── db.php                     🔌  Connexion PDO + fonctions utilitaires
├── sondedb_dashboard.html     📊  Template dashboard
├── .htaccess                  🔁  Règles de rewriting Apache
│
├── css/
│   ├── dashboard.css          Styles du dashboard
│   └── login.css              Styles de la page de connexion
│
├── js/
│   ├── dashboard.js           Logique du dashboard (KPIs, charts, tables)
│   ├── live-data.js           Polling toutes les 8 s
│   ├── speedtest.js           Mesure RSSI + débit download/upload
│   └── login.js               Logique du formulaire de connexion
│
├── firmware/
│   └── esp32_wifi_probe/
│       └── esp32_wifi_probe.ino    Firmware Arduino C++
│
└── sql/
    └── sondedb.sql            Schéma de la base + admin par défaut
```

## Déploiement

Sur le serveur Ubuntu 24.04 (`10.1.40.14` — Apache + MySQL + HTTPS) :

```bash
sudo a2enmod rewrite headers ssl
sudo apt install php php-mysql -y
sudo systemctl restart apache2
```

Depuis le Mac (déploiement complet) :

```bash
cd "/Users/lucasvarnier/Desktop/Scanner WiFi avancé ESP32"

# Copie de tous les fichiers nécessaires au serveur web
scp -r .htaccess api.php config.php db.php index.php login.html \
       sondedb_dashboard.html css js sql \
       lucas@10.1.40.14:/tmp/sondedb/

# Installation côté serveur
ssh -t lucas@10.1.40.14 'sudo cp -r /tmp/sondedb/. /var/www/html/ && \
                         sudo mysql < /var/www/html/sql/sondedb.sql && \
                         sudo chown -R www-data:www-data /var/www/html'
```

Édite `config.php` pour mettre tes propres credentials MySQL et token API.

- **Dashboard** : https://10.1.40.14/
- **Login par défaut** : `admin` / `admin1234` (à changer immédiatement)

## Sécurité

Le serveur est sécurisé selon les bonnes pratiques :

- **UFW** : firewall actif, seuls les ports 22 / 80 / 443 ouverts
- **HTTPS** : redirection forcée HTTP → HTTPS
- **Fail2ban** : bannissement IP après 5 tentatives de connexion échouées
- **MySQL** : écoute uniquement sur `127.0.0.1`, droits limités à SELECT/INSERT/UPDATE/DELETE
- **SSH** : root interdit, max 3 tentatives, timeout 30 s
- **Apache** : headers X-Frame-Options, HSTS, X-Content-Type-Options
- **Mises à jour** : `unattended-upgrades` activé

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
  "measurements": [
    { "ssid": "WiFi-Maison", "bssid": "AA:BB:CC:DD:EE:FF", "rssi": -62, "channel": 6 }
  ],
  "alerts": [
    { "type": "Evil Twin", "description": "SSID dupliqué détecté", "level": "critical" }
  ]
}
```

Header HTTP optionnel : `X-API-Token: <token>` (si défini dans `config.php`).

## Firmware ESP32

Sketch dans `firmware/esp32_wifi_probe/esp32_wifi_probe.ino`.

Configuration via le **portail WiFiManager** au premier démarrage :
maintenir le bouton **BOOT** (GPIO0) **3 secondes** pour rouvrir le portail.

Champs à remplir dans le portail (réseau `SondeDB-Config`, mdp `sondedb1234`) :
- SSID + mot de passe du WiFi local
- URL API : `https://10.1.40.14/api/ingest`
- Token API (optionnel)
- Nom + localisation + ID de la sonde

Le firmware migre automatiquement `http://` → `https://` au démarrage.
