/**
 * SondeDB — Logique de la page de connexion
 * ────────────────────────────────────────────────────────────────────
 * - Horloge en bas de page mise à jour chaque seconde
 * - Soumission du formulaire vers /api/login en POST URL-encoded
 * - Redirection vers / si succès, sinon affichage du message d'erreur
 */
(() => {
  'use strict';

  const $ = id => document.getElementById(id);

  // ── Horloge ───────────────────────────────────────────────────────────────
  function updateClock() {
    const el = $('login-time');
    if (!el) return;
    el.textContent = new Date().toLocaleTimeString('fr-FR', {
      hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
  }
  setInterval(updateClock, 1000);
  updateClock();

  // ── Soumission du formulaire ──────────────────────────────────────────────
  async function doLogin(e) {
    e.preventDefault();
    const btn = $('btn-login');
    const err = $('error-box');
    btn.disabled = true;
    btn.textContent = 'Connexion…';
    err.classList.remove('show');

    const body = new URLSearchParams({
      username: $('username').value.trim(),
      password: $('password').value,
    });

    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        body,
        credentials: 'same-origin',
      });
      const data = await res.json();
      if (res.ok && data.status === 'ok') {
        window.location.href = '/';
      } else {
        err.textContent = 'Identifiant ou mot de passe incorrect';
        err.classList.add('show');
        btn.disabled = false;
        btn.textContent = 'Se connecter';
      }
    } catch {
      err.textContent = 'Erreur réseau — serveur inaccessible';
      err.classList.add('show');
      btn.disabled = false;
      btn.textContent = 'Se connecter';
    }
  }

  // Exposé pour le onsubmit du formulaire
  window.doLogin = doLogin;
})();
