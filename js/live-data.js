(() => {
  const state = {
    apiBase:
      window.SONDEDB_API_BASE ||
      (window.location.protocol === "file:" ? "http://localhost:8080" : ""),
    pollIntervalMs: 8000,
    timerId: null,
    hasLiveData: false,
  };

  const liveIndicator = document.getElementById("live-indicator");
  const liveIndicatorLabel = document.getElementById("live-indicator-label");
  const liveStatus = document.getElementById("live-status");

  function setLiveState(mode, title, detail) {
    if (!liveIndicator) return;

    liveIndicator.classList.remove("is-syncing", "is-offline");
    if (mode === "syncing") {
      liveIndicator.classList.add("is-syncing");
    }
    if (mode === "offline") {
      liveIndicator.classList.add("is-offline");
    }
    if (liveIndicatorLabel) {
      liveIndicatorLabel.textContent = title;
    }
    if (liveStatus) {
      liveStatus.textContent = detail;
    }
  }

  function normalizeString(value, fallback = "") {
    if (value === null || value === undefined) return fallback;
    const text = String(value).trim();
    return text || fallback;
  }

  function normalizeLevel(value) {
    const raw = normalizeString(value, "info").toLowerCase();
    const aliases = {
      critique: "critical",
      critical: "critical",
      warn: "warning",
      warning: "warning",
      info: "info",
      ok: "ok",
    };
    return aliases[raw] || "info";
  }

  function normalizeArray(value, mapper) {
    return Array.isArray(value) ? value.map(mapper) : [];
  }

  function normalizePayload(payload) {
    return {
      alertes: normalizeArray(payload.alertes, row => ({
        id: normalizeString(row.id),
        id_sonde: normalizeString(row.id_sonde || row.probe_id || row.probeId),
        type_alerte: normalizeString(row.type_alerte || row.type, "Anomalie WiFi"),
        description: normalizeString(row.description, "Aucune description"),
        niveau: normalizeLevel(row.niveau || row.level),
        horodatage: normalizeString(row.horodatage || row.detected_at || row.timestamp),
      })),
      interventions: normalizeArray(payload.interventions, row => ({
        id: normalizeString(row.id),
        id_sonde: normalizeString(row.id_sonde),
        technicien: normalizeString(row.technicien, "Auto"),
        description: normalizeString(row.description, "Intervention non renseignée"),
        date_intervention: normalizeString(row.date_intervention),
      })),
      mesures: normalizeArray(payload.mesures, row => ({
        id: normalizeString(row.id),
        id_sonde: normalizeString(row.id_sonde || row.probe_id || row.probeId),
        ssid: normalizeString(row.ssid, "<hidden>"),
        bssid: normalizeString(row.bssid, "00:00:00:00:00:00"),
        rssi: normalizeString(row.rssi, "-100"),
        canal: normalizeString(row.canal || row.channel, "0"),
        horodatage: normalizeString(row.horodatage || row.detected_at || row.timestamp),
      })),
      reseaux: normalizeArray(payload.reseaux, row => ({
        id: normalizeString(row.id),
        ssid: normalizeString(row.ssid, "<hidden>"),
        bssid: normalizeString(row.bssid, "00:00:00:00:00:00"),
        canal: normalizeString(row.canal || row.channel, "0"),
        date_detection: normalizeString(row.date_detection || row.horodatage),
      })),
      sondes: normalizeArray(payload.sondes, row => ({
        id: normalizeString(row.id),
        nom: normalizeString(row.nom || row.name, "ESP32"),
        localisation: normalizeString(row.localisation || row.location, "Non renseignée"),
        date_deploiement: normalizeString(row.date_deploiement || row.deployed_at),
      })),
      meta: payload.meta || {},
    };
  }

  function applyLivePayload(payload) {
    const normalized = normalizePayload(payload);
    DB.alertes = normalized.alertes;
    DB.interventions = normalized.interventions;
    DB.mesures = normalized.mesures;
    DB.reseaux = normalized.reseaux;
    DB.sondes = normalized.sondes;
    renderAll();
    state.hasLiveData = true;

    const updatedAt = normalizeString(
      normalized.meta.updated_at,
      new Date().toLocaleString("fr-FR")
    );
    setLiveState(
      "online",
      "LIVE — MySQL",
      `Synchronisé ${updatedAt}`
    );
  }

  async function refreshDashboard() {
    if (!state.hasLiveData) {
      setLiveState("syncing", "SYNC — API", "Connexion au flux temps réel…");
    }

    const url = `${state.apiBase}/api/dashboard?ts=${Date.now()}`;
    const response = await fetch(url, {
      cache: "no-store",
      headers: {
        Accept: "application/json",
      },
    });

    if (!response.ok) {
      let message = `Erreur API (${response.status})`;
      try {
        const payload = await response.json();
        if (payload && payload.error) {
          message = payload.error;
        }
      } catch (_error) {
        // Pas de body JSON exploitable, on garde le message HTTP simple.
      }
      throw new Error(message);
    }

    const payload = await response.json();
    applyLivePayload(payload);
  }

  function startLiveSync() {
    if (state.timerId) {
      clearInterval(state.timerId);
    }

    refreshDashboard().catch(error => {
      setLiveState(
        "offline",
        "OFFLINE — Démo",
        `API indisponible: ${error.message}`
      );
    });

    state.timerId = window.setInterval(() => {
      refreshDashboard().catch(error => {
        setLiveState(
          "offline",
          "OFFLINE — Démo",
          `API indisponible: ${error.message}`
        );
      });
    }, state.pollIntervalMs);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startLiveSync, { once: true });
  } else {
    startLiveSync();
  }
})();
