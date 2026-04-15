#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiManager.h>   // bibliothèque à installer via le gestionnaire
#include <Preferences.h>   // stockage flash interne ESP32

// ── Config runtime (chargée depuis le flash) ──────────────────────────────────
char g_apiUrl[128]    = "http://10.1.40.51:8080/api/ingest";
char g_apiToken[80]   = "";
char g_probeName[50]  = "ESP32-LAB";
char g_probeLoc[100]  = "Non configuree";
char g_probeId[8]     = "1";

const char* FIRMWARE_VERSION  = "sondedb-esp32-v2";
const unsigned long SCAN_INTERVAL_MS = 15000;
const int MAX_NETWORKS_PER_PUSH = 20;
const int MAX_ALERTS_PER_PUSH   = 12;

// Seuils de détection
const int  RSSI_WEAK_THRESHOLD    = -80;   // dBm : en dessous = signal faible
const int  CHANNEL_CROWD_THRESHOLD = 3;    // nb de réseaux sur le même canal = congestion

// ── GPIO ──────────────────────────────────────────────────────────────────────
// Maintenir le bouton BOOT (GPIO0) appuyé 3 s au démarrage = réinitialise la config WiFi
#define RESET_PIN 0

// ── Trusted APs (Evil Twin) ───────────────────────────────────────────────────
struct TrustedAccessPoint { const char* ssid; const char* bssid; };
TrustedAccessPoint trustedAps[] = {
  // { "MonSSID", "AA:BB:CC:DD:EE:FF" },
};
const int TRUSTED_AP_COUNT = sizeof(trustedAps) / sizeof(trustedAps[0]);

// ── Variables globales ────────────────────────────────────────────────────────
Preferences prefs;
unsigned long lastScanAt = 0;

WiFiManagerParameter* paramApiUrl;
WiFiManagerParameter* paramApiToken;
WiFiManagerParameter* paramProbeName;
WiFiManagerParameter* paramProbeLoc;
WiFiManagerParameter* paramProbeId;

// ── Persistance flash ─────────────────────────────────────────────────────────
void loadPrefs() {
  prefs.begin("sondedb", true);
  prefs.getString("api_url",    g_apiUrl,    sizeof(g_apiUrl));
  prefs.getString("api_token",  g_apiToken,  sizeof(g_apiToken));
  prefs.getString("probe_name", g_probeName, sizeof(g_probeName));
  prefs.getString("probe_loc",  g_probeLoc,  sizeof(g_probeLoc));
  prefs.getString("probe_id",   g_probeId,   sizeof(g_probeId));
  prefs.end();
}

void savePrefs() {
  prefs.begin("sondedb", false);
  prefs.putString("api_url",    g_apiUrl);
  prefs.putString("api_token",  g_apiToken);
  prefs.putString("probe_name", g_probeName);
  prefs.putString("probe_loc",  g_probeLoc);
  prefs.putString("probe_id",   g_probeId);
  prefs.end();
  Serial.println("Config sauvegardee en flash.");
}

// ── Callback WiFiManager : sauvegarde les params du portail ──────────────────
void saveParamsCallback() {
  strncpy(g_apiUrl,    paramApiUrl->getValue(),    sizeof(g_apiUrl)    - 1);
  strncpy(g_apiToken,  paramApiToken->getValue(),  sizeof(g_apiToken)  - 1);
  strncpy(g_probeName, paramProbeName->getValue(), sizeof(g_probeName) - 1);
  strncpy(g_probeLoc,  paramProbeLoc->getValue(),  sizeof(g_probeLoc)  - 1);
  strncpy(g_probeId,   paramProbeId->getValue(),   sizeof(g_probeId)   - 1);
  savePrefs();
}

// ── JSON helpers ──────────────────────────────────────────────────────────────
String escapeJson(const String& input) {
  String output;
  output.reserve(input.length() + 8);
  for (size_t i = 0; i < input.length(); i++) {
    const char c = input.charAt(i);
    switch (c) {
      case '\\': output += "\\\\"; break;
      case '"':  output += "\\\""; break;
      case '\n': output += "\\n";  break;
      case '\r': output += "\\r";  break;
      case '\t': output += "\\t";  break;
      default:   output += c;      break;
    }
  }
  return output;
}

bool sameText(const String& left, const char* right) {
  return left.equalsIgnoreCase(String(right));
}

// ── Alertes ───────────────────────────────────────────────────────────────────
struct AlertRecord { String type; String description; String level; };
// Prototype explicite pour éviter le bug de génération automatique de l'Arduino IDE
void addAlert(AlertRecord alerts[], int& alertCount, const String& type, const String& description, const String& level);

void addAlert(AlertRecord alerts[], int& alertCount,
              const String& type, const String& description, const String& level) {
  for (int i = 0; i < alertCount; i++) {
    if (alerts[i].type == type && alerts[i].description == description) return;
  }
  if (alertCount >= MAX_ALERTS_PER_PUSH) return;
  alerts[alertCount].type        = type;
  alerts[alertCount].description = description;
  alerts[alertCount].level       = level;
  alertCount++;
}

// ── Construction payload JSON ─────────────────────────────────────────────────
String buildPayload() {
  AlertRecord alerts[MAX_ALERTS_PER_PUSH];
  int alertCount = 0;

  String seenSsids[MAX_NETWORKS_PER_PUSH];
  String seenBssids[MAX_NETWORKS_PER_PUSH];
  int    seenCount = 0;

  // Comptage par canal pour détecter la congestion (canaux 1–14)
  int channelCount[15] = {0};

  int scanCount    = WiFi.scanNetworks(false, true);
  int networkCount = scanCount > MAX_NETWORKS_PER_PUSH ? MAX_NETWORKS_PER_PUSH : scanCount;

  String payload;
  payload.reserve(8192);
  payload += "{";
  payload += "\"probe\":{";
  payload += "\"id\":"         + String(g_probeId)                          + ",";
  payload += "\"name\":\""     + escapeJson(String(g_probeName))            + "\",";
  payload += "\"location\":\"" + escapeJson(String(g_probeLoc))             + "\",";
  payload += "\"firmware_version\":\"" + escapeJson(String(FIRMWARE_VERSION)) + "\",";
  payload += "\"status\":\"active\"";
  payload += "},";
  payload += "\"measurements\":[";

  for (int i = 0; i < networkCount; i++) {
    String ssid    = WiFi.SSID(i);
    String bssid   = WiFi.BSSIDstr(i);
    int    rssi    = WiFi.RSSI(i);
    int    channel = WiFi.channel(i);

    if (ssid.length() == 0) ssid = "<hidden>";

    if (i > 0) payload += ",";
    payload += "{";
    payload += "\"ssid\":\""  + escapeJson(ssid)  + "\",";
    payload += "\"bssid\":\"" + escapeJson(bssid) + "\",";
    payload += "\"rssi\":"    + String(rssi)       + ",";
    payload += "\"channel\":" + String(channel);
    payload += "}";

    // Comptage des réseaux par canal (2.4 GHz : canaux 1–14)
    if (channel >= 1 && channel <= 14) {
      channelCount[channel]++;
    }

    // ── Détection Evil Twin ──────────────────────────────────────────────────
    for (int t = 0; t < TRUSTED_AP_COUNT; t++) {
      if (sameText(ssid, trustedAps[t].ssid) && !sameText(bssid, trustedAps[t].bssid)) {
        addAlert(alerts, alertCount,
          "Evil Twin",
          "SSID autorise detecte avec un BSSID non approuve: " + ssid + " (" + bssid + ")",
          "critical");
      }
    }

    // ── Détection SSID dupliqué (Rogue AP potentiel) ──────────────────────────
    for (int s = 0; s < seenCount; s++) {
      if (seenSsids[s] == ssid && seenBssids[s] != bssid) {
        addAlert(alerts, alertCount,
          "SSID duplique",
          "Plusieurs BSSID diffusent le meme SSID: " + ssid,
          "warning");
      }
    }

    // ── Détection signal faible ────────────────────────────────────────────────
    if (rssi < RSSI_WEAK_THRESHOLD) {
      addAlert(alerts, alertCount,
        "Signal faible",
        "RSSI de " + String(rssi) + " dBm sur " + ssid + " (" + bssid + ")",
        "info");
    }

    if (seenCount < MAX_NETWORKS_PER_PUSH) {
      seenSsids[seenCount]  = ssid;
      seenBssids[seenCount] = bssid;
      seenCount++;
    }
  }

  payload += "],";

  // ── Détection congestion de canal (après la boucle) ──────────────────────────
  for (int ch = 1; ch <= 14; ch++) {
    if (channelCount[ch] >= CHANNEL_CROWD_THRESHOLD) {
      addAlert(alerts, alertCount,
        "Congestion canal",
        String(channelCount[ch]) + " reseaux detectes sur le canal " + String(ch) + " (2.4 GHz)",
        "warning");
    }
  }

  payload += "\"alerts\":[";
  for (int i = 0; i < alertCount; i++) {
    if (i > 0) payload += ",";
    payload += "{";
    payload += "\"type\":\""        + escapeJson(alerts[i].type)        + "\",";
    payload += "\"description\":\"" + escapeJson(alerts[i].description) + "\",";
    payload += "\"level\":\""       + escapeJson(alerts[i].level)       + "\"";
    payload += "}";
  }
  payload += "]}";

  WiFi.scanDelete();
  return payload;
}

// ── Envoi HTTP ────────────────────────────────────────────────────────────────
void pushScan() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi non connecte, scan ignore.");
    return;
  }

  String payload = buildPayload();
  Serial.println("Payload JSON construit (" + String(payload.length()) + " octets)");

  HTTPClient http;
  http.begin(g_apiUrl);
  http.addHeader("Content-Type", "application/json");
  if (strlen(g_apiToken) > 0) {
    http.addHeader("X-API-Token", g_apiToken);
  }

  int statusCode = http.POST(payload);
  if (statusCode > 0) {
    Serial.print("POST -> HTTP ");
    Serial.println(statusCode);
    if (statusCode == 201) {
      Serial.println(http.getString());
    }
  } else {
    Serial.print("Erreur HTTP: ");
    Serial.println(http.errorToString(statusCode));
  }
  http.end();
}

// ── Setup ─────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== SondeDB ESP32 v2 ===");

  // Bouton RESET : maintenir BOOT (GPIO0) 3 s au démarrage pour effacer la config
  pinMode(RESET_PIN, INPUT_PULLUP);
  if (digitalRead(RESET_PIN) == LOW) {
    delay(3000);
    if (digitalRead(RESET_PIN) == LOW) {
      WiFiManager wm;
      wm.resetSettings();
      prefs.begin("sondedb", false);
      prefs.clear();
      prefs.end();
      Serial.println("Config effacee. Redemarrage...");
      delay(1000);
      ESP.restart();
    }
  }

  // Chargement config depuis le flash
  loadPrefs();
  Serial.println("Config : " + String(g_probeName) + " @ " + String(g_probeLoc));
  Serial.println("API    : " + String(g_apiUrl));

  // Paramètres affichés dans le portail web WiFiManager
  paramApiUrl    = new WiFiManagerParameter("api_url",    "URL API",           g_apiUrl,    127);
  paramApiToken  = new WiFiManagerParameter("api_token",  "Token API",         g_apiToken,  79);
  paramProbeName = new WiFiManagerParameter("probe_name", "Nom de la sonde",   g_probeName, 49);
  paramProbeLoc  = new WiFiManagerParameter("probe_loc",  "Localisation",      g_probeLoc,  99);
  paramProbeId   = new WiFiManagerParameter("probe_id",   "ID sonde (entier)", g_probeId,   7);

  WiFiManager wm;
  wm.addParameter(paramApiUrl);
  wm.addParameter(paramApiToken);
  wm.addParameter(paramProbeName);
  wm.addParameter(paramProbeLoc);
  wm.addParameter(paramProbeId);
  wm.setSaveParamsCallback(saveParamsCallback);
  wm.setConfigPortalTimeout(180); // 3 min pour configurer, sinon redémarre

  // Si aucun WiFi connu : ouvre le réseau "SondeDB-Config" (mdp: sondedb1234)
  Serial.println("Si pas de WiFi connu : connecte-toi a 'SondeDB-Config' (mdp: sondedb1234)");
  if (!wm.autoConnect("SondeDB-Config", "sondedb1234")) {
    Serial.println("Echec WiFi — redemarrage dans 3s...");
    delay(3000);
    ESP.restart();
  }

  Serial.print("WiFi connecte ! IP locale : ");
  Serial.println(WiFi.localIP());
  WiFi.setSleep(false);

  // Premier scan immédiat au démarrage
  lastScanAt = millis() - SCAN_INTERVAL_MS;
}

// ── Loop ──────────────────────────────────────────────────────────────────────
void loop() {
  unsigned long now = millis();
  if (now - lastScanAt >= SCAN_INTERVAL_MS) {
    lastScanAt = now;
    pushScan();
  }
  delay(250);
}
