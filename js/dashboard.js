/* SondeDB — Dashboard (rendu + navigation + charts)
 * Expose : window.DB, window.renderAll, window.showSection,
 *          window.handleFile, window.doLogout, window.filterAlertes
 */
(() => {
  'use strict';

  // ── État central ────────────────────────────────────────────────────────────
  const DB = { alertes:[], interventions:[], mesures:[], reseaux:[], sondes:[] };
  const OFFLINE_THRESHOLD_MS = 3 * 60 * 1000; // 3 × 15 s + marge

  // ── Utils ───────────────────────────────────────────────────────────────────
  const $ = id => document.getElementById(id);
  const setText = (id, text) => { const el = $(id); if (el) el.textContent = text; };
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const clamp = (v, min, max) => Math.min(Math.max(v, min), max);
  const parseRssi = v => { const n = parseInt(v); return isNaN(n) ? -100 : n; };
  const dateFromSql = s => {
    if (!s) return null;
    const t = new Date(String(s).replace(' ', 'T') + 'Z').getTime();
    return isNaN(t) ? null : t;
  };

  /** Couleur RSSI : vert > orange > rouge selon seuils dBm */
  function rssiColor(rssi) {
    if (rssi > -60) return 'var(--ok)';
    if (rssi > -75) return 'var(--warn)';
    return 'var(--danger)';
  }

  /** État d'une sonde selon dernier scan + alertes critiques */
  function probeStatus(s) {
    const ts = dateFromSql(s.last_seen);
    if (!ts || Date.now() - ts > OFFLINE_THRESHOLD_MS) return 'offline';
    return DB.alertes.some(a => a.id_sonde === s.id && a.niveau === 'critical')
      ? 'alert' : 'online';
  }

  // ── CSV Import ──────────────────────────────────────────────────────────────
  const TABLE_MAP = {
    'id,id_sonde,type_alerte,description,niveau,horodatage': 'alertes',
    'id,id_sonde,technicien,description,date_intervention' : 'interventions',
    'id,id_sonde,ssid,bssid,rssi,canal,horodatage'         : 'mesures',
    'id,ssid,bssid,canal,date_detection'                   : 'reseaux',
    'id,nom,localisation,date_deploiement'                 : 'sondes',
  };

  function parseCSV(text) {
    const tables = { alertes:[], interventions:[], mesures:[], reseaux:[], sondes:[] };
    let table = null, headers = [];
    for (const raw of text.trim().split('\n')) {
      const line = raw.replace(/"/g, '').trim();
      if (!line) continue;
      const cols = line.split(',');
      const key = cols.join(',');
      if (TABLE_MAP[key]) { table = TABLE_MAP[key]; headers = cols; continue; }
      if (table && cols.length === headers.length) {
        const row = {};
        headers.forEach((h, i) => row[h] = cols[i]);
        tables[table].push(row);
      }
    }
    for (const k of Object.keys(tables)) if (tables[k].length) DB[k] = tables[k];
  }

  function handleFile(input) {
    const file = input.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = e => {
      parseCSV(e.target.result);
      renderAll();
      const banner = $('import-banner');
      if (banner) banner.style.display = 'flex';
      setText('import-info-text',
        `${file.name} importé — ${DB.mesures.length} mesures · ${DB.alertes.length} alertes · ${DB.sondes.length} sondes`);
    };
    reader.readAsText(file);
  }

  // ── Navigation ──────────────────────────────────────────────────────────────
  const SECTION_TITLES = {
    overview:'Vue générale', spectrum:'Spectre WiFi', alertes:'Alertes',
    interventions:'Interventions', sondes:'Sondes',
    import:'Import données', detail:'Détail sonde',
  };

  function showSection(id, el) {
    window.scrollTo({ top: 0, behavior: 'instant' });
    document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
    const target = $('sec-' + id);
    if (target) target.classList.add('active');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    if (el) el.classList.add('active');
    setText('page-title', SECTION_TITLES[id] || id);
  }

  // ── Charts registry + defaults ──────────────────────────────────────────────
  const charts = {};
  function destroyChart(id) { if (charts[id]) { charts[id].destroy(); delete charts[id]; } }

  const CH = {
    grid:  'rgba(255,255,255,0.04)',
    tick:  'rgba(240,240,240,0.32)',
    legend:'rgba(240,240,240,0.32)',
    font:  { family:'JetBrains Mono', size:9 },
  };

  const axisStyle = ({ hideGrid = false } = {}) => ({
    grid: hideGrid ? { display:false } : { color: CH.grid },
    ticks: { color: CH.tick, font: CH.font },
  });

  // ── Render orchestrator ─────────────────────────────────────────────────────
  function renderAll() {
    updateKPIs();
    renderCanalGrid();
    renderAlertesMini();
    renderRSSIList();
    renderSondeMap('sonde-map-nodes');
    renderSondeMap('sonde-map-detail');
    renderSpectrumChart();
    renderCanalPie();
    renderRSSIHist();
    renderMesuresTable();
    renderAlertesTable();
    renderAlertesCharts();
    renderTimeline();
    renderInterventionChart();
    renderSondesTable();
    updateBadgeAlertes();
    updateTime();
    // Événement pour les autres modules (speedtest, target-wifi)
    document.dispatchEvent(new CustomEvent('sondedb:render'));
  }

  // ── KPIs ────────────────────────────────────────────────────────────────────
  function updateKPIs() {
    const fmt = n => (n === 0 && !window._liveDataReceived) ? '—' : n;
    const onlineSondes = DB.sondes.filter(s => probeStatus(s) !== 'offline').length;
    setText('kpi-sondes',  fmt(onlineSondes));
    setText('kpi-reseaux', fmt(DB.reseaux.length));
    setText('kpi-mesures', fmt(DB.mesures.length));
    setText('kpi-alertes', fmt(DB.alertes.length));
  }

  function updateBadgeAlertes() {
    const critical = DB.alertes.filter(a => a.niveau === 'critical').length;
    const badge = $('badge-alertes');
    if (badge) badge.style.display = critical > 0 ? 'block' : 'none';
    setText('alertes-count-badge', `${DB.alertes.length} alertes`);
  }

  function updateTime() {
    const now = new Date();
    setText('topbar-time',
      now.toLocaleTimeString('fr-FR', { hour:'2-digit', minute:'2-digit', second:'2-digit' }));
    setText('hdr-date-el',
      now.toLocaleDateString('fr-FR').replace(/\//g, '.').toUpperCase());
    setText('last-update',
      now.toLocaleTimeString('fr-FR', { hour:'2-digit', minute:'2-digit', second:'2-digit' }));
  }

  // ── Canal Grid (occupation 2.4 GHz) ─────────────────────────────────────────
  function renderCanalGrid() {
    const grid = $('canal-grid');
    if (!grid) return;

    const alertCanals = new Set(
      DB.alertes
        .map(a => DB.mesures.find(m => m.id_sonde === a.id_sonde)?.canal)
        .filter(Boolean)
    );

    const counts = {};
    DB.mesures.forEach(m => { counts[m.canal] = (counts[m.canal] || 0) + 1; });
    const maxCount = Math.max(...Object.values(counts), 1);

    grid.innerHTML = Array.from({length:13}, (_,i) => i+1).map(c => {
      const count = counts[c] || 0;
      const h = count ? Math.max(20, Math.round(count / maxCount * 110)) : 6;
      const isAlert = alertCanals.has(String(c));
      const cls = isAlert ? 'active' : count ? 'has-signal' : '';
      return `<div class="canal-bar-wrap">
        <div class="canal-bar ${cls}" style="height:${h}px"
          onmouseover="showTooltip(event,'Canal ${c} — ${count} réseau(x)${isAlert?' · Alerte':''}')"
          onmouseout="hideTooltip()"></div>
        <div class="canal-label">${c}</div>
      </div>`;
    }).join('');
  }

  // ── Mini Alertes ────────────────────────────────────────────────────────────
  function renderAlertesMini() {
    const list = $('alertes-mini-list');
    if (!list) return;
    if (!DB.alertes.length) {
      list.innerHTML = '<div class="empty-msg">Aucune alerte</div>';
      return;
    }
    list.innerHTML = DB.alertes.slice(0, 8).map(a => `
      <div class="alert-mini-row">
        <span class="alert-lvl ${a.niveau}">${esc(a.niveau)}</span>
        <div class="alert-main">
          <div class="alert-type">${esc(a.type_alerte)}</div>
          <div class="alert-desc">${esc(a.description)}</div>
        </div>
        <div class="alert-ts">${esc((a.horodatage||'').split(' ')[0])}</div>
      </div>`).join('');
  }

  // ── RSSI List (par SSID unique) ─────────────────────────────────────────────
  function renderRSSIList() {
    const el = $('rssi-list');
    if (!el) return;
    if (!DB.mesures.length) {
      el.innerHTML = '<div class="empty-msg">Aucune mesure</div>';
      return;
    }
    const unique = {};
    DB.mesures.forEach(m => { if (!unique[m.ssid]) unique[m.ssid] = m; });
    el.innerHTML = Object.values(unique).slice(0, 8).map(m => {
      const rssi  = parseRssi(m.rssi);
      const pct   = clamp(((rssi + 100) / 70) * 100, 0, 100);
      const color = rssiColor(rssi);
      return `<div class="rssi-row">
        <div class="rssi-ssid">${esc(m.ssid)}</div>
        <div class="rssi-bar-track">
          <div class="rssi-bar-fill" style="width:${pct.toFixed(0)}%;background:${color}"></div>
        </div>
        <div class="rssi-val" style="color:${color}">${rssi} dBm</div>
        <div class="rssi-canal">ch${esc(m.canal)}</div>
      </div>`;
    }).join('');
  }

  // ── Sonde Map ───────────────────────────────────────────────────────────────
  const SONDE_POSITIONS = [
    { top:'42%', left:'38%' }, { top:'65%', left:'62%' }, { top:'30%', left:'70%' },
    { top:'55%', left:'25%' }, { top:'75%', left:'45%' }, { top:'20%', left:'50%' },
  ];

  function renderSondeMap(containerId) {
    const c = $(containerId);
    if (!c) return;
    c.innerHTML = DB.sondes.map((s, i) => {
      const pos = SONDE_POSITIONS[i % SONDE_POSITIONS.length];
      const status = probeStatus(s);
      const color = status === 'alert' ? 'var(--danger)'
                  : status === 'offline' ? 'var(--text3)'
                  : 'var(--text)';
      return `<div class="sonde-node${status==='offline'?' is-offline':''}" style="top:${pos.top};left:${pos.left}">
        <div class="sonde-ring" style="border-color:${color}">
          <div class="sonde-dot" style="background:${color}"></div>
        </div>
        <div class="sonde-label" style="color:${color}">${esc(s.nom)}</div>
      </div>`;
    }).join('');
  }

  // ── Spectrum Chart ──────────────────────────────────────────────────────────
  function renderSpectrumChart() {
    destroyChart('spectrum');
    const ctx = $('spectrumChart');
    if (!ctx) return;

    const byCanal = {};
    DB.mesures.forEach(m => {
      const c = parseInt(m.canal);
      if (!byCanal[c]) byCanal[c] = [];
      byCanal[c].push(parseRssi(m.rssi));
    });
    const labels = Array.from({length:13}, (_,i) => i+1);
    const data = labels.map(c => byCanal[c] ? Math.max(...byCanal[c]) : -100);

    const alertCanals = new Set(
      DB.alertes
        .map(a => DB.mesures.find(m => m.id_sonde === a.id_sonde)?.canal)
        .filter(Boolean)
        .map(Number)
    );

    const colors = data.map((v, i) =>
      alertCanals.has(i+1) ? 'rgba(255,45,45,0.85)'
      : v > -100 ? 'rgba(200,255,0,0.55)'
      : 'rgba(255,255,255,0.05)'
    );

    charts.spectrum = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: labels.map(l => 'Ch ' + l),
        datasets: [{
          label:'RSSI max (dBm)', data, backgroundColor:colors,
          borderColor: colors.map(c => c.replace('0.55','0.9').replace('0.85','1')),
          borderWidth: 1, borderRadius: 0,
        }],
      },
      options: {
        responsive:true, maintainAspectRatio:false,
        plugins: { legend:{ display:false } },
        scales: {
          y: { min:-110, max:-30, ...axisStyle() },
          x: axisStyle({ hideGrid:true }),
        },
      },
    });
  }

  // ── Canal Pie ───────────────────────────────────────────────────────────────
  const PIE_PALETTE = [
    'rgba(200,255,0,0.75)','rgba(255,255,255,0.6)','rgba(255,45,45,0.7)',
    'rgba(255,136,0,0.7)','rgba(0,230,118,0.6)','rgba(150,200,255,0.6)',
    'rgba(200,255,0,0.4)','rgba(255,255,255,0.3)',
  ];

  function renderCanalPie() {
    destroyChart('canalPie');
    const ctx = $('canalPieChart');
    if (!ctx) return;
    const counts = {};
    DB.mesures.forEach(m => { counts['ch'+m.canal] = (counts['ch'+m.canal]||0)+1; });
    const labels = Object.keys(counts);
    charts.canalPie = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels,
        datasets: [{
          data: Object.values(counts),
          backgroundColor: PIE_PALETTE.slice(0, labels.length),
          borderWidth: 0,
        }],
      },
      options: {
        responsive:true, maintainAspectRatio:false,
        plugins: {
          legend: {
            display:true, position:'right',
            labels: { color: CH.legend, font: CH.font, boxWidth: 8, padding: 8 },
          },
        },
      },
    });
  }

  // ── RSSI Histogram ──────────────────────────────────────────────────────────
  const HIST_COLORS = [
    'rgba(200,255,0,0.9)','rgba(255,255,255,0.7)',
    'rgba(255,45,45,0.8)','rgba(255,136,0,0.8)',
  ];

  function renderRSSIHist() {
    destroyChart('rssiHist');
    const ctx = $('rssiHistChart');
    if (!ctx) return;
    const ssids = [...new Set(DB.mesures.map(m => m.ssid))].slice(0, 4);
    const datasets = ssids.map((s, i) => ({
      label: s,
      data: DB.mesures.filter(m => m.ssid === s).map(m => parseRssi(m.rssi)),
      borderColor: HIST_COLORS[i],
      backgroundColor: HIST_COLORS[i].replace(/0\.[789]/, '0.08'),
      tension: 0.3, fill: false, pointRadius: 3, borderWidth: 1.5,
    }));
    const maxPts = Math.max(...datasets.map(d => d.data.length), 0);
    charts.rssiHist = new Chart(ctx, {
      type: 'line',
      data: {
        labels: Array.from({length:maxPts}, (_,i) => 'M' + (i+1)),
        datasets,
      },
      options: {
        responsive:true, maintainAspectRatio:false,
        plugins: {
          legend: {
            display:true, position:'bottom',
            labels: { color: CH.legend, font: CH.font, boxWidth: 8 },
          },
        },
        scales: {
          y: { min:-100, max:-40, ...axisStyle() },
          x: axisStyle({ hideGrid:true }),
        },
      },
    });
  }

  // ── Mesures Table ───────────────────────────────────────────────────────────
  function renderMesuresTable() {
    const tbody = $('mesures-tbody');
    if (!tbody) return;
    tbody.innerHTML = DB.mesures.map(m => {
      const rssi = parseRssi(m.rssi);
      const color = rssiColor(rssi);
      const sonde = DB.sondes.find(s => s.id === m.id_sonde);
      return `<tr>
        <td style="color:var(--accent)">${esc(m.ssid)}</td>
        <td style="color:var(--text2);font-size:9px">${esc(m.bssid)}</td>
        <td style="color:${color}">${rssi} dBm</td>
        <td>${esc(m.canal)}</td>
        <td>${esc(sonde?.nom || 'S' + m.id_sonde)}</td>
        <td style="color:var(--text2);font-size:9px">${esc(m.horodatage)}</td>
      </tr>`;
    }).join('');
  }

  // ── Alertes Table + filter ──────────────────────────────────────────────────
  let alerteFilter = 'all';
  function filterAlertes(f, btn) {
    alerteFilter = f;
    document.querySelectorAll('#filter-niveau .filter-btn')
      .forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
    renderAlertesTable();
  }
  function renderAlertesTable() {
    const tbody = $('alertes-tbody');
    if (!tbody) return;
    const rows = alerteFilter === 'all'
      ? DB.alertes
      : DB.alertes.filter(a => a.niveau === alerteFilter);
    tbody.innerHTML = rows.map(a => {
      const sonde = DB.sondes.find(s => s.id === a.id_sonde);
      return `<tr>
        <td style="font-weight:600">${esc(a.type_alerte)}</td>
        <td style="color:var(--text2)">${esc(a.description)}</td>
        <td><span class="badge ${a.niveau}">${esc(a.niveau)}</span></td>
        <td style="color:var(--dim)">${esc(sonde?.nom || 'Sonde ' + a.id_sonde)}</td>
        <td style="color:var(--text2);font-size:9px">${esc(a.horodatage)}</td>
      </tr>`;
    }).join('');
  }

  // ── Alertes Charts ──────────────────────────────────────────────────────────
  const NIVEAU_COLORS = {
    critical:'rgba(255,45,45,0.8)', warning:'rgba(255,136,0,0.8)',
    info:'rgba(255,255,255,0.3)',   ok:'rgba(0,230,118,0.7)',
  };

  function renderAlertesCharts() {
    destroyChart('alerteNiveau');
    destroyChart('alerteType');
    const ctxN = $('alerteNiveauChart');
    const ctxT = $('alerteTypeChart');

    const niveaux = {};
    DB.alertes.forEach(a => { niveaux[a.niveau] = (niveaux[a.niveau]||0)+1; });

    if (ctxN) {
      charts.alerteNiveau = new Chart(ctxN, {
        type:'doughnut',
        data:{
          labels: Object.keys(niveaux),
          datasets: [{
            data: Object.values(niveaux),
            backgroundColor: Object.keys(niveaux).map(k => NIVEAU_COLORS[k] || 'rgba(255,255,255,0.2)'),
            borderWidth: 0,
          }],
        },
        options:{
          responsive:true, maintainAspectRatio:false,
          plugins:{ legend:{ display:true, position:'right',
            labels:{ color:CH.legend, font:CH.font, boxWidth:8 } } },
        },
      });
    }

    const types = {};
    DB.alertes.forEach(a => { types[a.type_alerte] = (types[a.type_alerte]||0)+1; });

    if (ctxT) {
      charts.alerteType = new Chart(ctxT, {
        type:'bar',
        data:{
          labels: Object.keys(types),
          datasets:[{ data: Object.values(types),
            backgroundColor:'rgba(255,45,45,0.65)', borderRadius:0, borderWidth:0 }],
        },
        options:{
          responsive:true, maintainAspectRatio:false, indexAxis:'y',
          plugins:{ legend:{ display:false } },
          scales:{ x: axisStyle(), y: axisStyle({ hideGrid:true }) },
        },
      });
    }
  }

  // ── Timeline ────────────────────────────────────────────────────────────────
  function renderTimeline() {
    const el = $('timeline-list');
    if (!el) return;
    if (!DB.interventions.length) {
      el.innerHTML = '<div class="empty-msg">Aucune intervention</div>';
      return;
    }
    el.innerHTML = DB.interventions.map((iv, i) => {
      const sonde = DB.sondes.find(s => s.id === iv.id_sonde);
      const last = i === DB.interventions.length - 1;
      return `<div class="timeline-item">
        <div class="timeline-dot-wrap">
          <div class="timeline-dot"></div>
          ${!last ? '<div class="timeline-line"></div>' : ''}
        </div>
        <div class="timeline-content">
          <div class="timeline-header">
            <span class="timeline-tech">${esc(iv.technicien)}</span>
            <span class="timeline-date">${esc((iv.date_intervention||'').split(' ')[0])}</span>
          </div>
          <div class="timeline-desc">${esc(iv.description)}</div>
          <div class="timeline-sonde">${esc(sonde?.nom || 'Sonde ' + iv.id_sonde)} — ${esc(sonde?.localisation || '')}</div>
        </div>
      </div>`;
    }).join('');
  }

  // ── Intervention Chart ──────────────────────────────────────────────────────
  function renderInterventionChart() {
    destroyChart('intervention');
    const ctx = $('interventionChart');
    if (!ctx) return;
    const bySonde = {};
    DB.interventions.forEach(iv => {
      const sonde = DB.sondes.find(s => s.id === iv.id_sonde);
      const key = sonde?.nom || ('S' + iv.id_sonde);
      bySonde[key] = (bySonde[key] || 0) + 1;
    });
    charts.intervention = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: Object.keys(bySonde),
        datasets: [{
          data: Object.values(bySonde),
          backgroundColor: 'rgba(200,255,0,0.5)',
          borderRadius: 0, borderWidth: 0,
        }],
      },
      options: {
        responsive:true, maintainAspectRatio:false,
        plugins: { legend: { display:false } },
        scales: {
          y: { ...axisStyle(), ticks:{ ...axisStyle().ticks, stepSize:1 } },
          x: axisStyle({ hideGrid:true }),
        },
      },
    });
  }

  // ── Sondes Table ────────────────────────────────────────────────────────────
  function renderSondesTable() {
    const tbody = $('sondes-tbody');
    if (!tbody) return;
    tbody.innerHTML = DB.sondes.map(s => {
      const status = probeStatus(s);
      const badge = status === 'alert'   ? '<span class="badge critical">Alerte</span>'
                  : status === 'offline' ? '<span class="badge offline">Hors ligne</span>'
                                         : '<span class="badge ok">Actif</span>';
      const ssidColor = status === 'offline' ? 'var(--text3)' : 'var(--accent)';
      const locColor  = status === 'offline' ? 'var(--text3)' : '';
      const isPaused  = s.is_active === '0' || s.is_active === false || s.is_active === 0;
      const powerBtn  = isPaused
        ? `<button onclick="setProbePower(${esc(s.id)},true)" title="Activer" style="background:var(--ok);border:none;color:#000;padding:4px 10px;cursor:pointer;font-size:9px;font-family:inherit;letter-spacing:.06em;margin-right:6px;">▶ ON</button>`
        : `<button onclick="setProbePower(${esc(s.id)},false)" title="Mettre en pause" style="background:var(--bg3);border:1px solid var(--border);color:var(--text2);padding:4px 10px;cursor:pointer;font-size:9px;font-family:inherit;letter-spacing:.06em;margin-right:6px;">⏸ OFF</button>`;
      const editBtn = `<button onclick="openProbeModal(${esc(s.id)},'${esc(s.nom)}','${esc(s.localisation)}')" title="Configurer" style="background:none;border:1px solid var(--border);color:var(--text2);padding:4px 10px;cursor:pointer;font-size:9px;font-family:inherit;letter-spacing:.06em;">✎ CONFIG</button>`;
      return `<tr>
        <td style="color:var(--dim)">${esc(s.id)}</td>
        <td style="color:${ssidColor}">${esc(s.nom)}</td>
        <td style="color:${locColor}">${esc(s.localisation)}</td>
        <td style="color:var(--text2);font-size:9px">${esc(s.date_deploiement)}</td>
        <td>${badge}</td>
        <td style="white-space:nowrap">${powerBtn}${editBtn}</td>
      </tr>`;
    }).join('');
  }

  // ── Tooltip flottant ────────────────────────────────────────────────────────
  function showTooltip(e, text) {
    const t = $('tooltip');
    if (!t) return;
    t.textContent = text;
    t.style.display = 'block';
    t.style.left = (e.clientX + 12) + 'px';
    t.style.top  = (e.clientY - 28) + 'px';
  }
  function hideTooltip() {
    const t = $('tooltip');
    if (t) t.style.display = 'none';
  }
  document.addEventListener('mousemove', e => {
    const t = $('tooltip');
    if (t && t.style.display === 'block') {
      t.style.left = (e.clientX + 12) + 'px';
      t.style.top  = (e.clientY - 28) + 'px';
    }
  });

  // ── Déconnexion ─────────────────────────────────────────────────────────────
  function doLogout() {
    fetch('/api/logout', { method:'GET', credentials:'same-origin' })
      .finally(() => { window.location.href = '/login'; });
  }

  // ── Video Scrubbing (Détail Sonde) ──────────────────────────────────────────
  function initVideoScrubbing() {
    const video     = $('sonde-video');
    const container = $('video-scroll-container');
    const fill      = $('video-progress-fill');
    const label     = $('video-scroll-label');
    if (!video || !container) return;
    video.pause();

    function scrub() {
      const section = $('sec-detail');
      if (!section || !section.classList.contains('active')) return;
      const rect = container.getBoundingClientRect();
      const scrollable = container.offsetHeight - window.innerHeight;
      if (scrollable <= 0) return;
      const progress = clamp(Math.max(0, -rect.top) / scrollable, 0, 1);
      if (video.readyState >= 1 && video.duration) {
        video.currentTime = progress * video.duration;
      }
      if (fill)  fill.style.width = (progress * 100).toFixed(2) + '%';
      if (label) label.style.opacity = progress > 0.02 ? '0' : '1';
    }
    window.addEventListener('scroll', scrub, { passive: true });
    video.addEventListener('loadedmetadata', scrub);
  }

  // ── Init ────────────────────────────────────────────────────────────────────
  setInterval(updateTime, 1000);

  // Expose API publique
  window.DB = DB;
  window.renderAll = renderAll;
  window.showSection = showSection;
  window.handleFile = handleFile;
  window.doLogout = doLogout;
  window.filterAlertes = filterAlertes;
  window.showTooltip = showTooltip;
  window.hideTooltip = hideTooltip;
  window.probeStatus = probeStatus;
  window.parseRssi = parseRssi;
  window.rssiColor = rssiColor;

  // ── Gestion sonde depuis le dashboard ───────────────────────────────────────
  window.openProbeModal = function(id, nom, loc) {
    $('modal-probe-id').value   = id;
    $('modal-probe-name').value = nom;
    $('modal-probe-loc').value  = loc;
    $('modal-feedback').textContent = '';
    const overlay = $('probe-modal-overlay');
    overlay.style.display = 'flex';
  };

  window.closeProbeModal = function() {
    $('probe-modal-overlay').style.display = 'none';
  };

  window.saveProbeSettings = async function() {
    const id  = $('modal-probe-id').value;
    const nom = $('modal-probe-name').value.trim();
    const loc = $('modal-probe-loc').value.trim();
    if (!nom || !loc) { $('modal-feedback').textContent = 'Nom et localisation requis.'; return; }
    $('modal-feedback').textContent = 'Envoi…';
    try {
      const r = await fetch(`/api/probe/${id}/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: nom, location: loc }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || r.status);
      $('modal-feedback').textContent = 'Config envoyée — appliquée au prochain cycle ESP32.';
      setTimeout(window.closeProbeModal, 1800);
    } catch(e) {
      $('modal-feedback').textContent = 'Erreur : ' + e.message;
    }
  };

  window.setProbePower = async function(id, active) {
    try {
      const r = await fetch(`/api/probe/${id}/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ active }),
      });
      if (!r.ok) { const d = await r.json(); throw new Error(d.error || r.status); }
    } catch(e) {
      alert('Erreur : ' + e.message);
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { renderAll(); initVideoScrubbing(); });
  } else {
    renderAll();
    initVideoScrubbing();
  }
})();
