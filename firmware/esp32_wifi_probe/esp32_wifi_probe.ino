#include <HTTPClient.h>
#include <WiFi.h>

const char* WIFI_SSID = "A_REMPLACER";
const char* WIFI_PASSWORD = "A_REMPLACER";

const char* API_URL = "http://10.1.40.51:8080/api/ingest";
const char* API_TOKEN = "change_me";

const int PROBE_ID = 1;
const char* PROBE_NAME = "ESP32-LAB";
const char* PROBE_LOCATION = "Laboratoire BTS CIEL";
const char* FIRMWARE_VERSION = "sondedb-esp32-v1";

const unsigned long SCAN_INTERVAL_MS = 15000;
const int MAX_NETWORKS_PER_PUSH = 20;
const int MAX_ALERTS_PER_PUSH = 12;

struct TrustedAccessPoint {
  const char* ssid;
  const char* bssid;
};

TrustedAccessPoint trustedAps[] = {
  { "MonWifiEntreprise", "AA:BB:CC:DD:EE:FF" },
  { "MonWifiInvite", "11:22:33:44:55:66" },
};

struct AlertRecord {
  String type;
  String description;
  String level;
};

unsigned long lastScanAt = 0;

String escapeJson(const String& input) {
  String output;
  output.reserve(input.length() + 8);

  for (size_t i = 0; i < input.length(); i++) {
    const char c = input.charAt(i);
    switch (c) {
      case '\\':
        output += "\\\\";
        break;
      case '"':
        output += "\\\"";
        break;
      case '\n':
        output += "\\n";
        break;
      case '\r':
        output += "\\r";
        break;
      case '\t':
        output += "\\t";
        break;
      default:
        output += c;
        break;
    }
  }

  return output;
}

bool sameText(const String& left, const char* right) {
  return left.equalsIgnoreCase(String(right));
}

void addAlert(
  AlertRecord alerts[],
  int& alertCount,
  const String& type,
  const String& description,
  const String& level
) {
  for (int i = 0; i < alertCount; i++) {
    if (alerts[i].type == type && alerts[i].description == description) {
      return;
    }
  }

  if (alertCount >= MAX_ALERTS_PER_PUSH) {
    return;
  }

  alerts[alertCount].type = type;
  alerts[alertCount].description = description;
  alerts[alertCount].level = level;
  alertCount++;
}

void ensureWifiConnected() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  Serial.print("Connexion au WiFi");
  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 40) {
    delay(500);
    Serial.print(".");
    attempts++;
  }

  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("WiFi connecte, IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("Impossible de se connecter au WiFi.");
  }
}

String buildPayload() {
  AlertRecord alerts[MAX_ALERTS_PER_PUSH];
  int alertCount = 0;

  String seenSsids[MAX_NETWORKS_PER_PUSH];
  String seenBssids[MAX_NETWORKS_PER_PUSH];
  int seenCount = 0;

  int scanCount = WiFi.scanNetworks(false, true);
  int networkCount = scanCount > MAX_NETWORKS_PER_PUSH ? MAX_NETWORKS_PER_PUSH : scanCount;

  String payload;
  payload.reserve(8192);
  payload += "{";
  payload += "\"probe\":{";
  payload += "\"id\":" + String(PROBE_ID) + ",";
  payload += "\"name\":\"" + escapeJson(PROBE_NAME) + "\",";
  payload += "\"location\":\"" + escapeJson(PROBE_LOCATION) + "\",";
  payload += "\"firmware_version\":\"" + escapeJson(FIRMWARE_VERSION) + "\",";
  payload += "\"status\":\"active\"";
  payload += "},";
  payload += "\"measurements\":[";

  for (int i = 0; i < networkCount; i++) {
    String ssid = WiFi.SSID(i);
    String bssid = WiFi.BSSIDstr(i);
    int rssi = WiFi.RSSI(i);
    int channel = WiFi.channel(i);

    if (ssid.length() == 0) {
      ssid = "<hidden>";
    }

    if (i > 0) {
      payload += ",";
    }

    payload += "{";
    payload += "\"ssid\":\"" + escapeJson(ssid) + "\",";
    payload += "\"bssid\":\"" + escapeJson(bssid) + "\",";
    payload += "\"rssi\":" + String(rssi) + ",";
    payload += "\"channel\":" + String(channel);
    payload += "}";

    for (unsigned int trustedIndex = 0; trustedIndex < sizeof(trustedAps) / sizeof(trustedAps[0]); trustedIndex++) {
      if (sameText(ssid, trustedAps[trustedIndex].ssid) && !sameText(bssid, trustedAps[trustedIndex].bssid)) {
        addAlert(
          alerts,
          alertCount,
          "Evil Twin",
          "SSID autorise detecte avec un BSSID non approuve: " + ssid + " (" + bssid + ")",
          "critical"
        );
      }
    }

    for (int seenIndex = 0; seenIndex < seenCount; seenIndex++) {
      if (seenSsids[seenIndex] == ssid && seenBssids[seenIndex] != bssid) {
        addAlert(
          alerts,
          alertCount,
          "SSID duplique",
          "Plusieurs BSSID diffusent le meme SSID: " + ssid,
          "warning"
        );
      }
    }

    if (seenCount < MAX_NETWORKS_PER_PUSH) {
      seenSsids[seenCount] = ssid;
      seenBssids[seenCount] = bssid;
      seenCount++;
    }
  }

  payload += "],";
  payload += "\"alerts\":[";
  for (int i = 0; i < alertCount; i++) {
    if (i > 0) {
      payload += ",";
    }
    payload += "{";
    payload += "\"type\":\"" + escapeJson(alerts[i].type) + "\",";
    payload += "\"description\":\"" + escapeJson(alerts[i].description) + "\",";
    payload += "\"level\":\"" + escapeJson(alerts[i].level) + "\"";
    payload += "}";
  }
  payload += "]";
  payload += "}";

  WiFi.scanDelete();
  return payload;
}

void pushScan() {
  ensureWifiConnected();
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }

  String payload = buildPayload();
  HTTPClient http;
  http.begin(API_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Token", API_TOKEN);

  int statusCode = http.POST(payload);
  if (statusCode > 0) {
    Serial.print("POST /api/ingest -> ");
    Serial.println(statusCode);
    String response = http.getString();
    Serial.println(response);
  } else {
    Serial.print("Erreur HTTP: ");
    Serial.println(statusCode);
  }

  http.end();
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);

  ensureWifiConnected();
  lastScanAt = millis() - SCAN_INTERVAL_MS;
}

void loop() {
  unsigned long now = millis();
  if (now - lastScanAt >= SCAN_INTERVAL_MS) {
    lastScanAt = now;
    pushScan();
  }
  delay(250);
}
