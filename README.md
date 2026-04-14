# SondeDB ESP32 + MySQL

Ce projet relie maintenant trois blocs:

- un sketch ESP32 qui scanne les réseaux WiFi visibles et remonte les anomalies simples de type Evil Twin ;
- une API Python légère qui reçoit les POST de la sonde et lit/écrit dans MySQL ;
- le dashboard existant, désormais synchronisé en direct avec l'API au lieu de rester sur des données statiques.

## 1. Configuration MySQL

Le serveur d'application lit sa config dans `.env`. Tu peux partir de `.env.example` :

```bash
cp .env.example .env
```

Puis renseigner au minimum :

```env
DB_HOST=10.1.40.51
DB_PORT=3306
DB_NAME=sondedb
DB_USER=ton_user_mysql
DB_PASSWORD=ton_mot_de_passe
API_TOKEN=un_token_partage_avec_les_esp32
```

Le script essaie de créer la base si elle n'existe pas, puis applique automatiquement le schéma de [`sql/schema.mysql.sql`](sql/schema.mysql.sql), désormais aligné sur ton dump `sondedb_english.sql` :

- `probes`
- `wifi_networks`
- `measurements`
- `alerts`
- `interventions`

## 2. Démarrage du serveur

Installe la dépendance MySQL Python :

```bash
python3 -m pip install -r requirements.txt
```

Si macOS bloque l'installation système, utilise un environnement virtuel local :

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Puis lance l'application :

```bash
python3 server.py
```

ou, si tu utilises le `venv` :

```bash
.venv/bin/python server.py
```

Ensuite ouvre le dashboard via :

```text
http://localhost:8080
```

Si tu déploies aussi `server.py` sur la machine `10.1.40.51`, l'URL à utiliser pour l'ESP32 sera alors `http://10.1.40.51:8080/api/ingest`.

## 3. Flash de l'ESP32

Le sketch est dans [`firmware/esp32_wifi_probe/esp32_wifi_probe.ino`](firmware/esp32_wifi_probe/esp32_wifi_probe.ino).

Avant compilation, remplace :

- `WIFI_SSID` et `WIFI_PASSWORD`
- `API_URL`
- `API_TOKEN`
- `PROBE_ID` avec l'identifiant numérique déjà présent dans la table `probes`
- `trustedAps[]` avec tes SSID/BSSID légitimes

Le sketch :

- scanne périodiquement les réseaux visibles ;
- envoie les mesures RSSI/canal/BSSID à l'API ;
- génère une alerte `Evil Twin` si un SSID déclaré comme légitime apparaît avec un autre BSSID ;
- génère une alerte `SSID duplique` si plusieurs BSSID diffusent le même SSID dans un scan.

## 4. Format JSON envoyé par l'ESP32

```json
{
  "probe": {
    "id": 1,
    "name": "ESP32-LAB",
    "location": "Laboratoire BTS CIEL",
    "firmware_version": "sondedb-esp32-v1",
    "status": "active"
  },
  "measurements": [
    {
      "ssid": "MonWifiEntreprise",
      "bssid": "AA:BB:CC:DD:EE:FF",
      "rssi": -62,
      "channel": 6
    }
  ],
  "alerts": [
    {
      "type": "Evil Twin",
      "description": "SSID autorise detecte avec un BSSID non approuve",
      "level": "critical"
    }
  ]
}
```

## 5. Endpoints disponibles

- `GET /api/health` : test rapide de l'API et de la connexion MySQL
- `GET /api/dashboard` : données normalisées consommées par le dashboard
- `POST /api/ingest` : ingestion des scans ESP32

## 6. Mapping vers le dashboard

Le dashboard conserve ses libellés français, mais l'API fait maintenant la traduction automatique depuis ton schéma SQL anglais :

- `probes.name` → `nom`
- `probes.location` → `localisation`
- `measurements.channel` → `canal`
- `measurements.timestamp` → `horodatage`
- `alerts.alert_type` → `type_alerte`
- `alerts.severity` → niveau d'affichage du dashboard
