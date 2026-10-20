(function () {
  'use strict';

  const ICONS = {
    dashboard:
      '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    facturen: '<path d="M6 3h9l4 4v14H6z"/><path d="M9 11h7M9 15h7M9 7h3"/>',
    bonnetjes:
      '<path d="M5 3h14v18l-3-2-2 2-2-2-2 2-2-2-3 2z"/><path d="M9 8h6M9 12h6"/>',
    btw: '<path d="M4 19V5m0 14h16"/><path d="M8 15l4-5 3 2 4-6"/>',
    bank: '<path d="M3 10l9-6 9 6"/><path d="M5 10h14v9H5z"/><path d="M9 19v-6m6 6v-6"/>',
    klanten:
      '<path d="M16 19v-1a4 4 0 00-8 0v1"/><circle cx="12" cy="8" r="4"/><path d="M20 19v-1a3 3 0 00-2-2.8"/>',
    instellingen:
      '<circle cx="12" cy="12" r="3"/><path d="M4 12h2m12 0h2M12 4v2m0 12v2M6.3 6.3l1.4 1.4m8.6 8.6l1.4 1.4M6.3 17.7l1.4-1.4m8.6-8.6l1.4-1.4"/>',
    uren: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    jaaroverzicht:
      '<path d="M4 19V5m0 14h16"/><path d="M8 15l3-4 2 2 4-6"/>',
  };

  const NAV = [
    { id: 'dashboard', href: 'dashboard.html', label: 'Dashboard', tab: true },
    { id: 'facturen', href: 'facturen.html', label: 'Facturen', tab: true },
    { id: 'bonnetjes', href: 'bonnetjes.html', label: 'Bonnetjes', tab: true },
    { id: 'btw', href: 'btw.html', label: 'BTW', fullLabel: 'BTW & aangifte', tab: true },
    { id: 'bank', href: 'bank.html', label: 'Bank' },
    { id: 'uren', href: 'uren.html', label: 'Uren', fullLabel: 'Uren & kilometers' },
    { id: 'jaaroverzicht', href: 'jaaroverzicht.html', label: 'Jaar' },
    { id: 'klanten', href: 'klanten.html', label: 'Klanten' },
    { id: 'instellingen', href: 'instellingen.html', label: 'Instellingen' },
  ];

  const PAGE_ALIASES = { 'factuur.html': 'facturen' };
  const ORIGINAL_TITLE = document.title;
  const file = window.location.pathname.split('/').pop() || 'dashboard.html';
  const currentPage = PAGE_ALIASES[file] || (document.body.dataset.page || file.replace('.html', ''));
  const appShell = document.querySelector('.app-shell');

  function icon(name) {
    return (
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      (ICONS[name] || '') +
      '</svg>'
    );
  }

  function navLink(item, className) {
    const active = item.id === currentPage;
    return (
      '<a href="' + item.href + '" class="' + className + (active ? ' active' : '') + '"' +
      (active ? ' aria-current="page"' : '') + '>' +
      (className === 'side-link' ? icon(item.id) : icon(item.id)) +
      '<span>' + (item.fullLabel || item.label) + '</span></a>'
    );
  }

  function renderSidebar() {
    const nav = document.querySelector('.sidebar .side-nav');
    if (nav) {
      nav.innerHTML = NAV.map(item => navLink(item, 'side-link')).join('');
      nav.setAttribute('aria-label', 'Hoofdnavigatie');
    }

    const foot = document.querySelector('.sidebar .sidebar-foot');
    if (foot) {
      foot.innerHTML =
        '<div class="side-user">' +
        '<span class="side-avatar" data-user-initials aria-hidden="true"></span>' +
        '<span class="side-user-copy">' +
        '<span class="side-user-name" data-user-company>Kasflow</span>' +
        '<span class="side-user-role" data-user-email>&nbsp;</span>' +
        '</span>' +
        '</div>' +
        '<button type="button" class="side-logout" data-logout>' +
        '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><path d="M16 17l5-5-5-5M21 12H9"/></svg>' +
        'Uitloggen</button>' +
        '<button type="button" class="side-install" data-install-app hidden>Kasflow op dit apparaat zetten</button>' +
        '<div class="side-legal">' +
        '<a href="privacy.html">Privacy</a>' +
        '<a href="voorwaarden.html">Voorwaarden</a>' +
        '<a href="verwerkersovereenkomst.html">Verwerking</a>' +
        '</div>';
      requestAnimationFrame(paintInstallButtons);
    }
  }

  function renderMobileShell() {
    if (!appShell) return;

    const bar = document.createElement('header');
    bar.className = 'mobile-bar';
    bar.innerHTML =
      '<a class="mobile-brand" href="dashboard.html" aria-label="Kasflow dashboard">' +
      '<span class="logo-mark" aria-hidden="true"></span><span>Kasflow</span></a>' +
      '<a class="mobile-avatar" href="instellingen.html" aria-label="Je account">' +
      '<span data-user-initials aria-hidden="true">K</span></a>';

    const tabs = document.createElement('nav');
    tabs.className = 'mobile-tabs';
    tabs.setAttribute('aria-label', 'Hoofdnavigatie');
    tabs.innerHTML =
      NAV.filter(item => item.tab)
        .map(item => {
          const active = item.id === currentPage;
          return (
            '<a href="' + item.href + '" class="mobile-tab' + (active ? ' active' : '') + '"' +
            (active ? ' aria-current="page"' : '') + '>' +
            icon(item.id) + '<span>' + item.label + '</span></a>'
          );
        })
        .join('') +
      '<button type="button" class="mobile-tab" id="more-toggle" aria-expanded="false" aria-controls="more-sheet">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/></svg>' +
      '<span>Meer</span></button>';

    const backdrop = document.createElement('div');
    backdrop.className = 'sheet-backdrop';
    backdrop.id = 'sheet-backdrop';
    backdrop.hidden = true;

    const sheet = document.createElement('div');
    sheet.className = 'more-sheet';
    sheet.id = 'more-sheet';
    sheet.setAttribute('role', 'dialog');
    sheet.setAttribute('aria-modal', 'true');
    sheet.setAttribute('aria-label', 'Meer navigatie');
    sheet.hidden = true;

    const moreActive = ['bank', 'uren', 'jaaroverzicht', 'klanten', 'instellingen'].includes(currentPage);
    const moreToggle = () => document.getElementById('more-toggle');

    const openSheet = () => {
      sheet.hidden = false;
      backdrop.hidden = false;
      requestAnimationFrame(() => {
        sheet.classList.add('open');
        backdrop.classList.add('open');
      });
      const toggle = moreToggle();
      if (toggle) toggle.setAttribute('aria-expanded', 'true');
      const firstLink = sheet.querySelector('a, button');
      if (firstLink) firstLink.focus();
      document.body.classList.add('sheet-open');
    };

    const closeSheet = () => {
      sheet.classList.remove('open');
      backdrop.classList.remove('open');
      const toggle = moreToggle();
      if (toggle) toggle.setAttribute('aria-expanded', 'false');
      document.body.classList.remove('sheet-open');
      window.setTimeout(() => {
        if (!sheet.classList.contains('open')) {
          sheet.hidden = true;
          backdrop.hidden = true;
        }
      }, 180);
    };

    sheet.innerHTML =
      '<div class="sheet-grabber" aria-hidden="true"></div>' +
      '<div class="sheet-head"><strong>Meer</strong>' +
      '<button type="button" class="icon-button" id="sheet-close" aria-label="Sluit menu">' +
      '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>' +
      '</button></div>' +
      '<div class="sheet-links">' +
      NAV.filter(item => !item.tab)
        .map(item => {
          const active = item.id === currentPage;
          return (
            '<a href="' + item.href + '" class="sheet-link' + (active ? ' active' : '') + '">' +
            icon(item.id) + '<span>' + item.label + '</span></a>'
          );
        })
        .join('') +
      '</div>' +
      '<div class="sheet-account">' +
      '<div class="sheet-user"><span class="side-avatar" data-user-initials aria-hidden="true"></span>' +
      '<span><span class="sheet-user-name" data-user-company>Kasflow</span>' +
      '<span class="sheet-user-mail" data-user-email>&nbsp;</span></span></div>' +
      '<button type="button" class="sheet-logout" data-logout>Uitloggen</button>' +
      '<button type="button" class="sheet-install" data-install-app hidden>Kasflow op dit apparaat zetten</button>' +
      '<div class="side-legal">' +
      '<a href="privacy.html">Privacy</a>' +
      '<a href="voorwaarden.html">Voorwaarden</a>' +
      '<a href="verwerkersovereenkomst.html">Verwerking</a>' +
      '</div>' +
      '</div>';
    requestAnimationFrame(paintInstallButtons);

    if (moreActive) {
      tabs.querySelector('#more-toggle')?.classList.add('active');
    }

    document.body.prepend(bar);
    document.body.append(tabs, backdrop, sheet);

    tabs.querySelector('#more-toggle')?.addEventListener('click', () => {
      if (sheet.classList.contains('open')) closeSheet();
      else openSheet();
    });
    backdrop.addEventListener('click', closeSheet);
    sheet.querySelector('#sheet-close')?.addEventListener('click', () => {
      closeSheet();
      moreToggle()?.focus();
    });
    sheet.querySelectorAll('a').forEach(link => link.addEventListener('click', closeSheet));
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && sheet.classList.contains('open')) {
        closeSheet();
        moreToggle()?.focus();
      }
    });
  }

  function redirectToLogin() {
    if (!appShell) return;
    const returnTo = encodeURIComponent(file);
    window.location.replace('login.html?next=' + returnTo);
  }

  async function api(path, options) {
    const settings = Object.assign({ method: 'GET' }, options || {});
    const headers = new Headers(settings.headers || {});
    let body = settings.body;
    if (body && !(body instanceof FormData) && typeof body === 'object') {
      headers.set('Content-Type', 'application/json');
      body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, Object.assign({}, settings, { headers, body }));
    } catch (error) {
      throw new Error('Geen verbinding met de server. Probeer het opnieuw.');
    }
    if (response.status === 401) {
      redirectToLogin();
      throw new Error('Je sessie is verlopen. Log opnieuw in.');
    }
    if (!response.ok) {
      let payload = null;
      try {
        payload = await response.json();
      } catch (error) {
        payload = null;
      }
      throw new Error(errorMessage(payload, response));
    }
    if (response.status === 204) return null;
    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('application/json')) return response.json();
    return response.text();
  }

  function errorMessage(payload, response) {
    if (payload && typeof payload.detail === 'string') return payload.detail;
    if (payload && Array.isArray(payload.detail) && payload.detail.length) {
      const first = payload.detail[0];
      const field = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : '';
      const fieldLabels = {
        email: 'E-mailadres',
        password: 'Wachtwoord',
        new_password: 'Nieuw wachtwoord',
        company_name: 'Bedrijfsnaam',
        kvk_number: 'KVK-nummer',
        name: 'Naam',
        amount: 'Bedrag',
        vendor: 'Winkel',
        receipt_date: 'Datum',
        number: 'Factuurnummer',
      };
      const label = fieldLabels[field] || '';
      return (label ? label + ': ' : '') + (first.msg || 'Controleer de invoer.');
    }
    if (response && response.status === 403) return 'Deze aanvraag is geweigerd.';
    if (response && response.status === 404) return 'Niet gevonden.';
    return 'Er ging iets mis. Probeer het opnieuw.';
  }

  async function loadUser() {
    if (!appShell) return null;
    try {
      const response = await fetch('/api/auth/me', { headers: { Accept: 'application/json' } });
      if (response.status === 401) {
        redirectToLogin();
        return null;
      }
      if (!response.ok) return null;
      return await response.json();
    } catch (error) {
      return null;
    }
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, character => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    })[character]);
  }

  function initials(value) {
    const text = String(value || '').trim();
    if (!text) return 'K';
    const words = text.split(/\s+/).filter(Boolean);
    return words
      .slice(0, 2)
      .map(word => word.charAt(0).toUpperCase())
      .join('');
  }

  function applyUser(user) {
    if (!user) return;
    document.querySelectorAll('[data-user-company]').forEach(el => {
      el.textContent = user.company_name;
    });
    document.querySelectorAll('[data-user-email]').forEach(el => {
      el.textContent = user.email;
    });
    document.querySelectorAll('[data-user-initials]').forEach(el => {
      el.textContent = initials(user.company_name);
    });
    document.title = ORIGINAL_TITLE.replace(/^Kasflow/, 'Kasflow — ' + user.company_name);
  }

  const moneyFormatter = new Intl.NumberFormat('nl-NL', {
    style: 'currency',
    currency: 'EUR',
  });
  const dateFormatter = new Intl.DateTimeFormat('nl-NL', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  });
  const shortDateFormatter = new Intl.DateTimeFormat('nl-NL', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  });

  function parseDate(value) {
    if (!value) return null;
    if (value instanceof Date) return value;
    return new Date(String(value).slice(0, 10) + 'T00:00:00');
  }

  function todayIso() {
    const now = new Date();
    const offset = now.getTimezoneOffset();
    return new Date(now.getTime() - offset * 60000).toISOString().slice(0, 10);
  }

  function statusOf(invoice) {
    if (invoice.status === 'paid') return 'paid';
    if (invoice.status === 'credited') return 'credited';
    const due = invoice.due_date || invoice.dueDate;
    return due && due < todayIso() ? 'overdue' : 'pending';
  }

  function isOpen(invoice) {
    const status = statusOf(invoice);
    return status === 'pending' || status === 'overdue';
  }

  const STATUS_LABELS = { paid: 'Betaald', pending: 'Openstaand', overdue: 'Te laat', credited: 'Gecrediteerd' };

  function statusBadge(status) {
    const safe = STATUS_LABELS[status] ? status : 'pending';
    return (
      '<span class="badge ' + safe + '"><span class="dot" aria-hidden="true"></span>' +
      STATUS_LABELS[safe] + '</span>'
    );
  }

  function note(element, message, tone) {
    if (!element) return;
    element.textContent = message || '';
    if (message) element.dataset.tone = tone || 'info';
    else delete element.dataset.tone;
  }

  function setBusy(button, busy, busyLabel) {
    if (!button) return;
    if (busy) {
      button.dataset.originalLabel = button.textContent;
      button.textContent = busyLabel || 'Bezig…';
      button.disabled = true;
      button.classList.add('is-busy');
    } else {
      if (button.dataset.originalLabel) button.textContent = button.dataset.originalLabel;
      button.disabled = false;
      button.classList.remove('is-busy');
    }
  }

  let deferredInstall = null;

  function paintInstallButtons() {
    document.querySelectorAll('[data-install-app]').forEach(button => {
      button.hidden = !deferredInstall;
    });
  }

  async function installApp() {
    if (!deferredInstall) {
      window.alert('Je browser biedt geen automatische installatie aan. Voeg Kasflow toe via het menu van je browser: "Toevoegen aan startscherm" of "App installeren".');
      return false;
    }
    deferredInstall.prompt();
    const choice = await deferredInstall.userChoice;
    deferredInstall = null;
    paintInstallButtons();
    return choice && choice.outcome === 'accepted';
  }

  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    deferredInstall = event;
    requestAnimationFrame(paintInstallButtons);
  });

  window.addEventListener('appinstalled', () => {
    deferredInstall = null;
    paintInstallButtons();
  });

  document.addEventListener('click', event => {
    if (event.target.closest('[data-install-app]')) {
      event.preventDefault();
      installApp();
    }
  });

  async function logout() {
    try {
      await fetch('/api/auth/logout', {
        method: 'POST',
        headers: { Accept: 'application/json' },
      });
    } catch (error) {
      // The local session is cleared server-side when possible; still leave.
    }
    window.location.href = 'login.html';
  }

  document.addEventListener('click', event => {
    const trigger = event.target.closest('[data-logout]');
    if (trigger) {
      event.preventDefault();
      logout();
    }
  });

  renderSidebar();
  renderMobileShell();

  const ready = loadUser().then(user => {
    applyUser(user);
    window.dispatchEvent(new CustomEvent('kasflow:ready', { detail: { user } }));
    return user;
  });

  window.Kasflow = {
    api,
    money: cents => moneyFormatter.format((Number(cents) || 0) / 100),
    formatDate: value => {
      const parsed = parseDate(value);
      return parsed ? dateFormatter.format(parsed) : '—';
    },
    formatShortDate: value => {
      const parsed = parseDate(value);
      return parsed ? shortDateFormatter.format(parsed) : '—';
    },
    todayIso,
    statusOf,
    isOpen,
    statusLabel: status => STATUS_LABELS[status] || STATUS_LABELS.pending,
    statusBadge,
    note,
    setBusy,
    escapeHtml,
    applyUser,
    initials,
    logout,
    installApp,
    canInstall: () => Boolean(deferredInstall),
    ready,
    get user() {
      return window.Kasflow._user || null;
    },
  };

  ready.then(user => {
    window.Kasflow._user = user;
    if (user) document.body.classList.add('is-authenticated');
  });

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js').catch(() => {
        // Offline ondersteuning is een extra; de app werkt ook zonder.
      });
    });
  }
})();
