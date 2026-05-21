/**
 * SondeDB — Widget "réseau ciblé" + speedtest
 * ────────────────────────────────────────────────────────────────────
 * 1. Bandeau du haut : affiche le réseau WiFi sélectionné (SSID, RSSI,
 *    canal, BSSID, qualité) avec un sparkline des dernières mesures.
 * 2. Speedtest : analyse RSSI sur 12 secondes puis mesure le débit
 *    download (2 Mo) et upload (1 Mo).
 */
(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const LS_KEY = 'sondedb.targetSsid';

  // ══ Target WiFi : sélection + rendu du bandeau du haut ═════════════════════
  const target = { ssid: localStorage.getItem(LS_KEY) || '', sparkChart: null };

  /** Récupère les mesures pour un SSID donné, triées par horodatage croissant. */
  function measurementsFor(ssid) {
    return window.DB.mesures
      .filter(m => m.ssid === ssid)
      .sort((a, b) => String(a.horodatage).localeCompare(String(b.horodatage)));
  }

  /** Liste des SSID détectés (unique), triés par meilleur RSSI. */
  function availableSsids() {
    const byBest = new Map();
    window.DB.mesures.forEach(m => {
      const r = window.parseRssi(m.rssi);
      const cur = byBest.get(m.ssid);
      if (!cur || r > cur) byBest.set(m.ssid, r);
    });
    return [...byBest.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([ssid]) => ssid);
  }

  /** Scoring qualité basé sur RSSI moyen et nb d'alertes critiques. */
  function qualityFromStats(avgRssi, criticalAlerts, stddev = 0) {
    if (avgRssi <= -100) return { label: '—', color: 'var(--dim)' };
    if (avgRssi >= -55 && criticalAlerts === 0 && stddev < 5)
      return { label: 'EXCELLENT', color: 'var(--ok)' };
    if (avgRssi >= -65 && criticalAlerts <= 1 && stddev < 8)
      return { label: 'BON', color: 'var(--ok)' };
    if (avgRssi >= -75)
      return { label: 'MOYEN', color: 'var(--warn)' };
    return { label: 'FAIBLE', color: 'var(--danger)' };
  }

  /** Auto-sélection : premier SSID avec le meilleur signal si aucun choix. */
  function ensureTargetSelected() {
    if (target.ssid && window.DB.mesures.some(m => m.ssid === target.ssid)) return;
    const pool = availableSsids();
    target.ssid = pool[0] || '';
    if (target.ssid) localStorage.setItem(LS_KEY, target.ssid);
  }

  /** Populate du <select> SSID. */
  function populateSelect() {
    const sel = $('tw-select');
    if (!sel) return;
    const opts = availableSsids();
    const currentVal = target.ssid;
    sel.innerHTML = opts.length
      ? opts.map(s => `<option value="${s}"${s === currentVal ? ' selected' : ''}>${s}</option>`).join('')
      : '<option value="">Aucun réseau détecté</option>';
  }

  /** Mini-sparkline des RSSI récents du target WiFi. */
  function renderSparkline(points) {
    const ctx = $('tw-sparkline-canvas');
    if (!ctx || !window.Chart) return;
    if (target.sparkChart) { target.sparkChart.destroy(); target.sparkChart = null; }
    if (!points.length) return;
    target.sparkChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: points.map((_, i) => i),
        datasets: [{
          data: points,
          borderColor: '#2563eb',
          backgroundColor: 'rgba(37,99,235,0.08)',
          fill: true, tension: 0.4,
          borderWidth: 1.3, pointRadius: 0,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: { x: { display: false }, y: { display: false, min: -100, max: -30 } },
        animation: { duration: 400 },
      },
    });
  }

  /** Rendu complet du bandeau "Target WiFi". */
  function renderTargetWifi() {
    const wrap = $('target-wifi');
    if (!wrap) return;

    ensureTargetSelected();
    populateSelect();

    const mesures = target.ssid ? measurementsFor(target.ssid) : [];
    const latest = mesures[mesures.length - 1];
    const rssiNow = latest ? window.parseRssi(latest.rssi) : -100;
    const canal = latest?.canal ?? '—';
    const bssid = latest?.bssid ?? '—';
    const samples = mesures.slice(-20).map(m => window.parseRssi(m.rssi));

    const avgRssi = samples.length
      ? samples.reduce((a, b) => a + b, 0) / samples.length
      : -100;
    const mean = avgRssi;
    const stddev = samples.length
      ? Math.sqrt(samples.reduce((s, v) => s + (v - mean) ** 2, 0) / samples.length)
      : 0;

    const crit = window.DB.alertes.filter(a =>
      a.niveau === 'critical' && (
        (a.description || '').includes(target.ssid) ||
        (a.description || '').includes(bssid)
      )
    ).length;

    const q = qualityFromStats(avgRssi, crit, stddev);
    const rssiColor = window.rssiColor(rssiNow);

    const ssidEl = $('tw-ssid');
    if (ssidEl) ssidEl.textContent = target.ssid || 'Aucun réseau sélectionné';

    const rssiValEl = $('tw-rssi-val');
    if (rssiValEl) {
      rssiValEl.textContent = latest ? `${rssiNow}` : '—';
      rssiValEl.style.color = rssiColor;
    }

    const canalEl = $('tw-canal'); if (canalEl) canalEl.textContent = `ch${canal}`;
    const bssidEl = $('tw-bssid'); if (bssidEl) bssidEl.textContent = bssid;
    const samplesEl = $('tw-samples');
    if (samplesEl) samplesEl.textContent = `${samples.length} éch.`;

    const badge = $('tw-quality-badge');
    if (badge) {
      badge.textContent = q.label;
      badge.style.color = q.color;
      badge.style.borderColor = q.color;
    }

    renderSparkline(samples);
  }

  function onSelectChange(e) {
    target.ssid = e.target.value;
    localStorage.setItem(LS_KEY, target.ssid);
    renderTargetWifi();
  }

  // ══ Speedtest progressif (type speedtest.net) ══════════════════════════════
  const TEST_DURATION_S = 12;    // Durée de la mesure
  const POLL_INTERVAL_MS = 1000; // Rafraîchissement API
  const speedtest = {
    running: false, chart: null,
    samples: [], /* { t, rssi } */
  };

  /** Récupère la dernière mesure API pour le SSID ciblé. */
  async function fetchLatestForTarget() {
    if (!target.ssid) return null;
    try {
      const res = await fetch(`/api/dashboard?ts=${Date.now()}`, {
        cache: 'no-store',
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      });
      if (!res.ok) return null;
      const data = await res.json();
      if (Array.isArray(data.mesures)) {
        window.DB.mesures = data.mesures;
        window.DB.alertes = data.alertes || window.DB.alertes;
        window.DB.sondes  = data.sondes  || window.DB.sondes;
        window.DB.reseaux = data.reseaux || window.DB.reseaux;
        window._liveDataReceived = true;
      }
      const matches = (data.mesures || [])
        .filter(m => m.ssid === target.ssid)
        .sort((a, b) => String(b.horodatage).localeCompare(String(a.horodatage)));
      return matches[0] || null;
    } catch {
      return null;
    }
  }

  /** Stats agrégées sur les échantillons accumulés. */
  function computeStats(samples) {
    if (!samples.length) return { min:0, max:0, avg:-100, stddev:0 };
    const values = samples.map(s => s.rssi);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const avg = values.reduce((a, b) => a + b, 0) / values.length;
    const variance = values.reduce((s, v) => s + (v - avg) ** 2, 0) / values.length;
    return { min, max, avg, stddev: Math.sqrt(variance) };
  }

  /** Initialise / met à jour le graphe live du speedtest. */
  function renderSpeedtestChart() {
    const ctx = $('speedtest-chart');
    if (!ctx || !window.Chart) return;

    const labels = speedtest.samples.map(s => `${s.t.toFixed(1)}s`);
    const data   = speedtest.samples.map(s => s.rssi);

    if (!speedtest.chart) {
      speedtest.chart = new Chart(ctx, {
        type: 'line',
        data: {
          labels, datasets: [{
            label: 'RSSI (dBm)',
            data,
            borderColor: '#2563eb',
            backgroundColor: 'rgba(37,99,235,0.08)',
            fill: true, tension: 0.35,
            borderWidth: 1.5, pointRadius: 2,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          animation: { duration: 250 },
          plugins: { legend: { display: false } },
          scales: {
            y: { min:-100, max:-30,
              grid: { color: 'rgba(0,0,0,0.07)' },
              ticks: { color: '#9ca3af', font: { family:'JetBrains Mono', size:9 } } },
            x: { grid: { display: false },
              ticks: { color: '#9ca3af', font: { family:'JetBrains Mono', size:8 } } },
          },
        },
      });
    } else {
      speedtest.chart.data.labels = labels;
      speedtest.chart.data.datasets[0].data = data;
      speedtest.chart.update('none');
    }
  }

  /** Met à jour l'affichage live du speedtest. */
  function updateSpeedtestUI(progressPct) {
    const fill = $('speedtest-progress-fill');
    if (fill) fill.style.width = progressPct + '%';

    const stats = computeStats(speedtest.samples);
    const last = speedtest.samples[speedtest.samples.length - 1];

    const gaugeVal = $('speedtest-gauge-val');
    if (gaugeVal) {
      gaugeVal.textContent = last ? last.rssi : '—';
      gaugeVal.style.color = last ? window.rssiColor(last.rssi) : 'var(--dim)';
    }

    const gaugeSsid = $('speedtest-gauge-ssid');
    if (gaugeSsid) gaugeSsid.textContent = target.ssid ? `Test sur ${target.ssid}` : '—';

    const set = (id, value, fmt = v => v) =>
      { const el = $(id); if (el) el.textContent = speedtest.samples.length ? fmt(value) : '—'; };

    set('speedtest-min',  stats.min);
    set('speedtest-max',  stats.max);
    set('speedtest-avg',  stats.avg,    v => v.toFixed(1));
    set('speedtest-jit',  stats.stddev, v => v.toFixed(1));
    set('speedtest-count', speedtest.samples.length);

    renderSpeedtestChart();
  }

  /** Lance un test complet. */
  async function startSpeedtest() {
    if (speedtest.running) return;
    if (!target.ssid) {
      const label = $('speedtest-status');
      if (label) {
        label.textContent = 'Sélectionnez un WiFi cible';
        label.style.color = 'var(--warn)';
      }
      return;
    }

    speedtest.running = true;
    speedtest.samples = [];
    if (speedtest.chart) { speedtest.chart.destroy(); speedtest.chart = null; }

    const panel  = $('speedtest-panel');
    const btn    = $('btn-speedtest');
    const status = $('speedtest-status');
    const score  = $('speedtest-score');
    const fill   = $('speedtest-progress-fill');
    const spin   = $('speedtest-spinner');

    if (panel)  panel.style.display = 'block';
    if (btn)    btn.disabled = true;
    if (status) { status.textContent = `Analyse en cours — ${target.ssid}`; status.style.color = 'var(--accent)'; }
    if (score)  score.style.display = 'none';
    if (fill)   { fill.classList.remove('is-done'); fill.style.width = '0%'; }
    if (spin)   spin.style.display = 'block';

    updateSpeedtestUI(0);

    const startedAt = Date.now();
    const totalMs = TEST_DURATION_S * 1000;

    while (speedtest.running) {
      const elapsed = Date.now() - startedAt;
      if (elapsed >= totalMs) break;

      const m = await fetchLatestForTarget();
      if (m) {
        const rssi = window.parseRssi(m.rssi);
        const t = (Date.now() - startedAt) / 1000;
        speedtest.samples.push({ t, rssi });
        if (window.renderAll) window.renderAll();
      }

      const progress = Math.min(100, (Date.now() - startedAt) / totalMs * 100);
      updateSpeedtestUI(progress);

      await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
    }

    // Fin de test
    updateSpeedtestUI(100);
    if (fill) fill.classList.add('is-done');
    if (spin) spin.style.display = 'none';

    const stats = computeStats(speedtest.samples);
    const crit = window.DB.alertes.filter(a =>
      a.niveau === 'critical' && (a.description || '').includes(target.ssid)
    ).length;
    const q = qualityFromStats(stats.avg, crit, stats.stddev);

    if (status) {
      status.textContent = speedtest.samples.length
        ? `Test terminé — ${speedtest.samples.length} échantillon(s) sur ${TEST_DURATION_S}s`
        : 'Aucune donnée — l\'ESP32 est-il actif ?';
      status.style.color = 'var(--dim)';
    }

    if (score) {
      score.textContent = q.label;
      score.style.color = q.color;
      score.style.borderColor = q.color;
      score.style.display = 'inline-block';
    }

    if (btn) btn.disabled = false;
    speedtest.running = false;

    // Lance la mesure download/upload après le test RSSI
    await measureBandwidth();
  }

  function stopSpeedtest() { speedtest.running = false; }

  // ══ Mesure bande passante download / upload ════════════════════════════════
  async function measureBandwidth() {
    const bwSection = $('st-bw-section');
    const dlEl      = $('st-dl');
    const ulEl      = $('st-ul');
    const dlBar     = $('st-dl-bar');
    const ulBar     = $('st-ul-bar');
    const dlCard    = $('st-bw-dl');
    const ulCard    = $('st-bw-ul');

    if (bwSection) bwSection.style.display = 'grid';
    if (dlEl) dlEl.textContent = '…';
    if (ulEl) ulEl.textContent = '…';
    if (dlBar) dlBar.style.width = '0%';
    if (ulBar) ulBar.style.width = '0%';
    if (dlCard) dlCard.classList.add('is-testing');
    if (ulCard) ulCard.classList.add('is-testing');

    // ── Download ──────────────────────────────────────────────────────────────
    let dlMbps = null;
    try {
      const SIZE = 2 * 1024 * 1024; // 2 MB
      const t0 = performance.now();
      const res = await fetch(`/api/speedtest/download?ts=${Date.now()}`, {
        cache: 'no-store', credentials: 'same-origin',
      });
      await res.arrayBuffer();
      const secs = (performance.now() - t0) / 1000;
      dlMbps = (SIZE * 8 / 1_000_000) / secs;
      if (dlEl) dlEl.textContent = dlMbps.toFixed(1);
      if (dlBar) dlBar.style.width = Math.min(100, dlMbps / 1000 * 100) + '%';
    } catch {
      if (dlEl) dlEl.textContent = 'err';
    }
    if (dlCard) dlCard.classList.remove('is-testing');

    // ── Upload ────────────────────────────────────────────────────────────────
    try {
      const SIZE = 1 * 1024 * 1024; // 1 MB
      const data = new Uint8Array(SIZE);
      const t0 = performance.now();
      await fetch('/api/speedtest/upload', {
        method: 'POST', body: data, credentials: 'same-origin',
      });
      const secs = (performance.now() - t0) / 1000;
      const ulMbps = (SIZE * 8 / 1_000_000) / secs;
      if (ulEl) ulEl.textContent = ulMbps.toFixed(1);
      if (ulBar) ulBar.style.width = Math.min(100, ulMbps / 1000 * 100) + '%';
    } catch {
      if (ulEl) ulEl.textContent = 'err';
    }
    if (ulCard) ulCard.classList.remove('is-testing');
  }

  // ══ Init ════════════════════════════════════════════════════════════════════
  document.addEventListener('DOMContentLoaded', () => {
    const sel = $('tw-select');
    if (sel) sel.addEventListener('change', onSelectChange);
    const btn = $('btn-speedtest');
    if (btn) btn.addEventListener('click', startSpeedtest);
  });

  // Se re-render à chaque mise à jour du DB
  document.addEventListener('sondedb:render', renderTargetWifi);

  // Expose un petit helper de debug
  window.SondedbSpeedtest = { start: startSpeedtest, stop: stopSpeedtest };
})();
