#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <WiFiManager.h>   // bibliothèque à installer via le gestionnaire
#include <Preferences.h>   // stockage flash interne ESP32

// ── Config runtime (chargée depuis le flash) ──────────────────────────────────
char g_apiUrl[128]    = "https://10.1.40.51/api/ingest";
char g_apiToken[80]   = "";
char g_probeName[50]  = "ESP32-LAB";
char g_probeLoc[100]  = "Non configuree";
char g_probeId[8]     = "1";

const char* FIRMWARE_VERSION  = "sondedb-esp32-v2";
const unsigned long SCAN_INTERVAL_MS = 10000;
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
bool g_probeActive = true;  // false = pause demandée par le dashboard

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

    // ── Détection SSID dupliqué — uniquement pour les APs de confiance ──────────
    for (int s = 0; s < seenCount; s++) {
      bool isTrusted = false;
      for (int t = 0; t < TRUSTED_AP_COUNT; t++) {
        if (sameText(ssid, trustedAps[t].ssid)) { isTrusted = true; break; }
      }
      if (isTrusted && seenSsids[s] == ssid && seenBssids[s] != bssid) {
        addAlert(alerts, alertCount,
          "SSID duplique",
          "Plusieurs BSSID diffusent le meme SSID: " + ssid,
          "warning");
      }
    }

    if (seenCount < MAX_NETWORKS_PER_PUSH) {
      seenSsids[seenCount]  = ssid;
      seenBssids[seenCount] = bssid;
      seenCount++;
    }
  }

  payload += "],";

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

// ── Client HTTPS statique (évite le memory leak avec new/delete répétés) ──────
static WiFiClientSecure g_secureClient;
static bool g_secureClientReady = false;

void ensureSecureClient() {
  if (!g_secureClientReady) {
    g_secureClient.setInsecure(); // réseau local : pas de vérif certificat
    g_secureClientReady = true;
  }
}

// ── Envoi HTTP ou HTTPS ───────────────────────────────────────────────────────
void pushScan() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi non connecte, scan ignore.");
    return;
  }

  String payload = buildPayload();
  Serial.println("Payload JSON construit (" + String(payload.length()) + " octets)");

  HTTPClient http;
  if (String(g_apiUrl).startsWith("https://")) {
    ensureSecureClient();
    http.begin(g_secureClient, g_apiUrl);
  } else {
    http.begin(g_apiUrl);
  }

  http.addHeader("Content-Type", "application/json");
  if (strlen(g_apiToken) > 0) http.addHeader("X-API-Token", g_apiToken);

  int statusCode = http.POST(payload);
  if (statusCode > 0) {
    Serial.print("POST -> HTTP ");
    Serial.println(statusCode);
    if (statusCode == 201) Serial.println(http.getString());
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

  // Migration automatique HTTP → HTTPS
  String apiUrl = String(g_apiUrl);
  if (apiUrl.startsWith("http://")) {
    apiUrl.replace("http://", "https://");
    apiUrl.toCharArray(g_apiUrl, sizeof(g_apiUrl));
    savePrefs();
    Serial.println("Migration URL : http -> https effectuee.");
  }

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

// ── Sync dashboard : récupère config + état on/off ────────────────────────────
// Parsing JSON minimaliste (pas de lib externe requise)
bool jsonBool(const String& json, const String& key, bool fallback) {
  int idx = json.indexOf("\"" + key + "\"");
  if (idx < 0) return fallback;
  int colon = json.indexOf(":", idx);
  if (colon < 0) return fallback;
  String rest = json.substring(colon + 1);
  rest.trim();
  if (rest.startsWith("true"))  return true;
  if (rest.startsWith("false")) return false;
  return fallback;
}

String jsonString(const String& json, const String& key, const String& fallback) {
  int idx = json.indexOf("\"" + key + "\"");
  if (idx < 0) return fallback;
  int colon = json.indexOf(":", idx);
  if (colon < 0) return fallback;
  int q1 = json.indexOf("\"", colon + 1);
  if (q1 < 0) return fallback;
  int q2 = json.indexOf("\"", q1 + 1);
  if (q2 < 0) return fallback;
  return json.substring(q1 + 1, q2);
}

void syncWithServer() {
  if (WiFi.status() != WL_CONNECTED) return;

  String url = String(g_apiUrl);
  url.replace("/api/ingest", "/api/probe/" + String(g_probeId) + "/sync");

  HTTPClient http;
  if (url.startsWith("https://")) {
    ensureSecureClient();
    http.begin(g_secureClient, url);
  } else {
    http.begin(url);
  }
  if (strlen(g_apiToken) > 0) http.addHeader("X-API-Token", g_apiToken);

  int code = http.GET();
  if (code != 200) { http.end(); return; }

  String body = http.getString();
  http.end();

  // Active / pause
  g_probeActive = jsonBool(body, "active", true);
  Serial.println(g_probeActive ? "Sonde: ACTIVE" : "Sonde: EN PAUSE (dashboard)");

  // Config en attente
  if (body.indexOf("\"config\":{") >= 0) {
    String newName = jsonString(body, "name", "");
    String newLoc  = jsonString(body, "location", "");
    bool changed = false;
    if (newName.length() > 0 && newName != String(g_probeName)) {
      newName.toCharArray(g_probeName, sizeof(g_probeName));
      changed = true;
    }
    if (newLoc.length() > 0 && newLoc != String(g_probeLoc)) {
      newLoc.toCharArray(g_probeLoc, sizeof(g_probeLoc));
      changed = true;
    }
    if (changed) {
      savePrefs();
      Serial.println("Config mise a jour : " + String(g_probeName) + " @ " + String(g_probeLoc));
    }
  }
}

// ── Portail de reconfiguration (appui long BOOT pendant le fonctionnement) ────
void checkReconfigButton() {
  if (digitalRead(RESET_PIN) != LOW) return;

  unsigned long pressStart = millis();
  Serial.println("Bouton BOOT detecte, maintenir 3s pour ouvrir le portail de config...");
  while (digitalRead(RESET_PIN) == LOW) {
    if (millis() - pressStart >= 3000) {
      Serial.println("Ouverture du portail de reconfiguration...");
      WiFiManager wm;
      wm.addParameter(paramApiUrl);
      wm.addParameter(paramApiToken);
      wm.addParameter(paramProbeName);
      wm.addParameter(paramProbeLoc);
      wm.addParameter(paramProbeId);
      wm.setSaveParamsCallback(saveParamsCallback);
      wm.startConfigPortal("SondeDB-Config", "sondedb1234");
      Serial.println("Portail ferme. Reprise du scan.");
      return;
    }
    delay(50);
  }
}

// ── Loop ──────────────────────────────────────────────────────────────────────
void loop() {
  checkReconfigButton();

  unsigned long now = millis();
  if (now - lastScanAt >= SCAN_INTERVAL_MS) {
    lastScanAt = now;
    syncWithServer();
    if (g_probeActive) {
      pushScan();
    }
  }
  delay(250);
}
