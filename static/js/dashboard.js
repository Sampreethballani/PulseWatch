/* PulseWatch — application shell + dashboard behaviour */
(function () {
  'use strict';

  var PW = (window.PulseWatch = window.PulseWatch || {});
  var THEME_KEY = 'pw-theme';
  var POLL_MS = 15000;

  /* ------------------------------------------------------------------ */
  /* Theme                                                               */
  /* ------------------------------------------------------------------ */
  function currentTheme() {
    return document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'dark' : 'light';
  }

  function syncThemeIcon() {
    var icons = document.querySelectorAll('[data-theme-icon]');
    var next = currentTheme() === 'dark' ? 'fa-sun' : 'fa-moon';
    icons.forEach(function (icon) {
      icon.className = 'fa-solid ' + next;
    });
    var button = document.querySelector('[data-theme-toggle]');
    if (button) button.setAttribute('aria-label', 'Switch to ' + (currentTheme() === 'dark' ? 'light' : 'dark') + ' mode');
  }

  function applyTheme(theme, persist) {
    document.documentElement.setAttribute('data-bs-theme', theme);
    if (persist) {
      try { localStorage.setItem(THEME_KEY, theme); } catch (error) { /* ignore */ }
    }
    syncThemeIcon();
    document.dispatchEvent(new CustomEvent('pw:theme', { detail: { theme: theme } }));
  }

  /* ------------------------------------------------------------------ */
  /* Dates                                                               */
  /* ------------------------------------------------------------------ */
  var relFormatter = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });

  function toDate(iso) {
    if (!iso) return null;
    var date = new Date(iso);
    return isNaN(date.getTime()) ? null : date;
  }

  PW.formatTime = function (iso, withDate) {
    var date = toDate(iso);
    if (!date) return '—';
    var options = withDate === false
      ? { hour: '2-digit', minute: '2-digit', second: '2-digit' }
      : { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' };
    return new Intl.DateTimeFormat(undefined, options).format(date);
  };

  PW.relative = function (iso) {
    var date = toDate(iso);
    if (!date) return '—';
    var diff = (date.getTime() - Date.now()) / 1000;
    var abs = Math.abs(diff);
    if (abs < 45) return 'just now';
    if (abs < 3600) return relFormatter.format(Math.round(diff / 60), 'minute');
    if (abs < 86400) return relFormatter.format(Math.round(diff / 3600), 'hour');
    return relFormatter.format(Math.round(diff / 86400), 'day');
  };

  function hydrateTimes(root) {
    (root || document).querySelectorAll('time[data-utc]').forEach(function (node) {
      var iso = node.getAttribute('data-utc');
      var label = PW.formatTime(iso);
      if (label === '—') return;
      node.textContent = label;
      node.title = iso;
    });
  }

  /* ------------------------------------------------------------------ */
  /* Toasts                                                              */
  /* ------------------------------------------------------------------ */
  PW.toast = function (message, variant) {
    var host = document.getElementById('toastHost');
    if (!host || !window.bootstrap) return;
    var toast = document.createElement('div');
    toast.className = 'toast align-items-center text-bg-' + (variant || 'success') + ' border-0';
    toast.setAttribute('role', 'status');
    toast.setAttribute('aria-live', 'polite');

    var body = document.createElement('div');
    body.className = 'd-flex';
    var text = document.createElement('div');
    text.className = 'toast-body';
    text.textContent = message;
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close btn-close-white me-2 m-auto';
    close.setAttribute('data-bs-dismiss', 'toast');
    close.setAttribute('aria-label', 'Close');
    body.append(text, close);
    toast.append(body);
    host.append(toast);

    var instance = new window.bootstrap.Toast(toast, { delay: 4200 });
    toast.addEventListener('hidden.bs.toast', function () { toast.remove(); });
    instance.show();
  };

  /* ------------------------------------------------------------------ */
  /* Small DOM helpers                                                   */
  /* ------------------------------------------------------------------ */
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function icon(name) {
    var node = document.createElement('i');
    node.className = 'fa-solid ' + name;
    node.setAttribute('aria-hidden', 'true');
    return node;
  }

  function canManage() {
    return document.body.dataset.canManage === '1';
  }

  function statusPill(status) {
    var value = (status || 'UNKNOWN').toUpperCase();
    var pill = el('span', 'status-pill status-' + value.toLowerCase());
    pill.append(el('span', 'status-dot'), document.createTextNode(value));
    return pill;
  }

  function codeBadge(code) {
    if (!code) return el('span', 'text-muted', '—');
    var tone = code < 300 ? 'ok' : code < 500 ? 'warn' : 'bad';
    return el('span', 'code-badge code-' + tone, code);
  }

  function latencyCell(site) {
    if (site.response_time_ms === null || site.response_time_ms === undefined) {
      return el('span', 'text-muted', '—');
    }
    var span = el('span', 'latency latency-' + (site.performance_class || 'unknown'),
      Math.round(site.response_time_ms));
    span.append(el('small', null, 'ms'));
    return span;
  }

  function uptimeCell(value) {
    if (value === null || value === undefined) return el('span', 'text-muted', 'N/A');
    var cls = value >= 99 ? 'is-good' : value >= 95 ? 'is-warn' : 'is-bad';
    return el('span', 'uptime-value ' + cls, value.toFixed(2) + '%');
  }

  function timeCell(iso) {
    var cell = el('td', 'cell-time');
    if (!iso) {
      cell.append(el('span', 'text-muted', 'Never'));
      return cell;
    }
    var time = el('time', null, PW.formatTime(iso));
    time.setAttribute('datetime', iso);
    time.setAttribute('data-utc', iso);
    time.title = iso;
    cell.append(time);
    return cell;
  }

  function deleteForm(site) {
    var form = document.createElement('form');
    form.method = 'post';
    form.action = '/websites/' + site.id + '/delete';
    form.setAttribute('data-confirm', 'Delete ' + site.name + '? All monitoring history and incidents for this website will be removed.');
    var button = el('button', 'btn-icon danger');
    button.type = 'submit';
    button.title = 'Delete';
    button.append(icon('fa-trash-can'));
    form.append(button);
    return form;
  }

  function actionCell(site) {
    var cell = document.createElement('td');
    var wrap = el('div', 'table-actions');

    var view = el('a', 'btn-icon');
    view.href = '/websites/' + site.id;
    view.title = 'View details';
    view.append(icon('fa-arrow-up-right-from-square'));

    wrap.append(view);
    if (canManage()) {
      var edit = el('button', 'btn-icon');
      edit.type = 'button';
      edit.title = 'Edit';
      edit.dataset.editSite = '';
      edit.dataset.siteId = site.id;
      edit.dataset.siteName = site.name;
      edit.dataset.siteUrl = site.url;
      edit.append(icon('fa-pen'));

      var check = el('button', 'btn-icon');
      check.type = 'button';
      check.title = 'Check now';
      check.dataset.post = '/websites/' + site.id + '/check';
      check.append(icon('fa-bolt'));

      wrap.append(edit, check, deleteForm(site));
    }
    cell.append(wrap);
    return cell;
  }

  function buildSiteRow(site) {
    var row = document.createElement('tr');
    row.dataset.siteRow = '';
    row.dataset.siteId = site.id;
    row.dataset.status = site.status;
    row.dataset.search = (site.name + ' ' + site.url).toLowerCase();

    var siteCell = document.createElement('td');
    var cellWrap = el('div', 'site-cell');
    cellWrap.append(el('span', 'site-avatar', (site.name || '?').charAt(0).toUpperCase()));
    var meta = el('div', 'site-meta');
    var name = el('a', 'site-name', site.name);
    name.href = '/websites/' + site.id;
    meta.append(name, el('span', 'site-url', site.hostname));
    cellWrap.append(meta);
    siteCell.append(cellWrap);

    var statusCell = document.createElement('td');
    statusCell.append(statusPill(site.status));

    var codeCell = document.createElement('td');
    codeCell.append(codeBadge(site.http_status_code));

    var latency = document.createElement('td');
    latency.append(latencyCell(site));

    var uptime = el('td', 'd-none d-xl-table-cell');
    uptime.append(uptimeCell(site.uptime_24h));

    row.append(siteCell, statusCell, codeCell, latency, uptime, timeCell(site.checked_at), actionCell(site));
    return row;
  }

  /* ------------------------------------------------------------------ */
  /* Table filtering (dashboard + websites page)                         */
  /* ------------------------------------------------------------------ */
  function initTableFilter(options) {
    var search = document.getElementById(options.searchId);
    var status = document.getElementById(options.statusId);
    var body = document.getElementById(options.bodyId);
    if (!body) return;

    var filterRow = document.createElement('tr');
    filterRow.className = 'empty-row';
    filterRow.id = options.bodyId + 'FilterEmpty';
    filterRow.hidden = true;
    var td = document.createElement('td');
    td.colSpan = options.colSpan || 8;
    td.append(el('div', 'empty-inline', 'No websites match your search or status filter.'));
    filterRow.append(td);
    body.append(filterRow);

    function apply() {
      var term = (search && search.value || '').trim().toLowerCase();
      var wanted = status ? status.value : 'all';
      var rows = body.querySelectorAll('tr[data-site-row]');
      var visible = 0;
      rows.forEach(function (row) {
        var matchesTerm = !term || (row.dataset.search || '').indexOf(term) !== -1;
        var matchesStatus = wanted === 'all' || row.dataset.status === wanted;
        var show = matchesTerm && matchesStatus;
        row.hidden = !show;
        if (show) visible += 1;
      });
      filterRow.hidden = !(rows.length > 0 && visible === 0);
      if (options.counterId) {
        var counter = document.getElementById(options.counterId);
        if (counter) counter.textContent = visible + ' website' + (visible === 1 ? '' : 's') + ' shown';
      }
    }

    if (search) search.addEventListener('input', apply);
    if (status) status.addEventListener('change', apply);
    apply();
    return apply;
  }

  /* ------------------------------------------------------------------ */
  /* Dashboard polling                                                   */
  /* ------------------------------------------------------------------ */
  var dashboardState = { filters: null, timer: null };

  function renderSummary(summary) {
    var map = {
      total: summary.total,
      up: summary.up,
      down: summary.down,
      degraded: summary.degraded
    };
    Object.keys(map).forEach(function (key) {
      var node = document.querySelector('[data-summary="' + key + '"]');
      if (node) node.textContent = map[key];
    });

    var avg = document.querySelector('[data-summary="avg_response"]');
    if (avg) {
      avg.textContent = '';
      if (summary.avg_response_ms === null || summary.avg_response_ms === undefined) {
        avg.append(el('span', 'text-muted', '—'));
      } else {
        avg.append(document.createTextNode(Math.round(summary.avg_response_ms)));
        avg.append(el('small', null, 'ms'));
      }
    }

    var uptime = document.querySelector('[data-summary="uptime_24h"]');
    if (uptime) {
      uptime.textContent = '';
      if (summary.uptime_24h === null || summary.uptime_24h === undefined) {
        uptime.append(el('span', 'text-muted', 'N/A'));
      } else {
        uptime.append(document.createTextNode(summary.uptime_24h.toFixed(2)));
        uptime.append(el('small', null, '%'));
      }
    }
  }

  function renderIncidentFeed(items) {
    var host = document.getElementById('recentIncidentList');
    if (!host) return;
    host.textContent = '';
    if (!items.length) {
      var empty = el('div', 'empty-inline');
      empty.append(icon('fa-shield-heart'));
      empty.append(el('p', null, 'No incidents recorded. Healthy so far.'));
      host.append(empty);
      return;
    }
    items.forEach(function (incident) {
      var isOpen = incident.incident_status === 'OPEN';
      var item = el('div', 'incident-item');
      var badge = el('span', 'incident-icon ' + (isOpen ? 'is-open' : 'is-closed'));
      badge.append(icon(isOpen ? 'fa-triangle-exclamation' : 'fa-circle-check'));

      var body = el('div', 'incident-body');
      var title = el('div', 'incident-title');
      var link = el('a', null, incident.website_name);
      link.href = '/websites/' + incident.website_id;
      title.append(link, el('span', 'chip chip-xs ' + (isOpen ? 'chip-danger' : 'chip-muted'), incident.incident_status));
      body.append(title);
      body.append(el('p', 'incident-desc', incident.description || 'Availability incident'));

      var time = el('span', 'incident-time');
      time.textContent = PW.formatTime(incident.started_at)
        + (incident.duration_seconds !== null && incident.duration_seconds !== undefined
          ? ' · ' + formatDuration(incident.duration_seconds) : '');
      body.append(time);

      item.append(badge, body);
      host.append(item);
    });
  }

  function formatDuration(seconds) {
    seconds = Number(seconds) || 0;
    if (seconds < 60) return seconds + 's';
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + 'm ' + (seconds % 60) + 's';
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + 'h ' + (minutes % 60) + 'm';
    return Math.floor(hours / 24) + 'd ' + (hours % 24) + 'h';
  }

  function renderEngine(monitor) {
    var last = document.querySelector('[data-engine="last"]');
    if (last) {
      last.textContent = monitor.last_cycle_finished ? PW.relative(monitor.last_cycle_finished) : 'not started';
      if (monitor.last_cycle_finished) last.title = monitor.last_cycle_finished;
    }
    var next = document.querySelector('[data-engine="next"]');
    if (next) {
      next.textContent = monitor.next_cycle_at ? PW.relative(monitor.next_cycle_at) : 'waiting';
      if (monitor.next_cycle_at) next.title = monitor.next_cycle_at;
    }
    var stateLabels = document.querySelectorAll('[data-engine-state]');
    stateLabels.forEach(function (node) {
      node.textContent = monitor.running ? 'Cycle in progress' : 'Auto-checks scheduled';
    });
    var bar = document.querySelector('[data-engine-bar]');
    if (bar) {
      var percent = 0;
      if (monitor.next_cycle_at) {
        var nextAt = new Date(monitor.next_cycle_at).getTime();
        var remaining = nextAt - Date.now();
        percent = Math.max(0, Math.min(100, 100 - (remaining / Math.max(1, monitor.interval * 1000)) * 100));
      }
      bar.style.width = (monitor.running ? 100 : percent) + '%';
    }
  }

  async function refreshDashboard(manual) {
    var body = document.getElementById('dashTableBody');
    if (!body) return;
    try {
      var response = await fetch('/api/dashboard', {
        headers: { Accept: 'application/json' },
        cache: 'no-store'
      });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      var data = await response.json();

      renderSummary(data.summary);
      renderEngine(data.monitor);
      renderIncidentFeed(data.recent_incidents);

      var rows = data.sites.map(buildSiteRow);
      body.textContent = '';
      if (!rows.length) {
        var emptyRow = document.createElement('tr');
        emptyRow.className = 'empty-row';
        var cell = document.createElement('td');
        cell.colSpan = 7;
        cell.innerHTML = '';
        var state = el('div', 'empty-state');
        var iconBox = el('span', 'empty-icon');
        iconBox.append(icon('fa-satellite-dish'));
        var heading = el('h3', null, 'No websites monitored yet');
        var paragraph = el('p', null, 'Add your first URL to start collecting availability, latency, DNS and TLS data.');
        state.append(iconBox, heading, paragraph);
        if (canManage()) {
          var actions = el('div', 'empty-actions');
          var addButton = el('button', 'btn btn-primary btn-sm');
          addButton.type = 'button';
          addButton.setAttribute('data-add-site', '');
          addButton.append(icon('fa-plus'), document.createTextNode(' Add website'));
          actions.append(addButton);
          state.append(actions);
        }
        cell.append(state);
        emptyRow.append(cell);
        body.append(emptyRow);
      } else {
        rows.forEach(function (row) { body.append(row); });
      }

      if (dashboardState.filters) dashboardState.filters();
      hydrateTimes();
      if (manual) PW.toast('Dashboard refreshed.', 'success');
    } catch (error) {
      if (manual) PW.toast('Could not refresh dashboard data.', 'danger');
    }
  }

  function startPolling() {
    if (dashboardState.timer) return;
    dashboardState.timer = window.setInterval(function () {
      if (document.hidden) return;
      refreshDashboard(false);
    }, POLL_MS);
  }

  /* ------------------------------------------------------------------ */
  /* Modals                                                              */
  /* ------------------------------------------------------------------ */
  function websiteModal() {
    var node = document.getElementById('websiteModal');
    if (!node || !window.bootstrap) return null;
    return window.bootstrap.Modal.getOrCreateInstance(node);
  }

  function openAddModal() {
    var modal = websiteModal();
    var form = document.getElementById('websiteForm');
    if (!modal || !form) return;
    form.action = '/websites/add';
    form.reset();
    document.getElementById('websiteModalTitle').textContent = 'Add a website';
    document.getElementById('websiteModalSubtitle').textContent = 'Only public HTTP and HTTPS addresses can be monitored.';
    var submit = document.getElementById('websiteSubmit');
    submit.querySelector('span').textContent = 'Add website';
    submit.querySelector('i').className = 'fa-solid fa-plus';
    var nameInput = document.getElementById('websiteName');
    if (nameInput) nameInput.placeholder = 'e.g. Production API';
    modal.show();
    window.setTimeout(function () { document.getElementById('websiteUrl').focus(); }, 220);
  }

  function openEditModal(dataset) {
    var modal = websiteModal();
    var form = document.getElementById('websiteForm');
    if (!modal || !form) return;
    form.action = '/websites/' + dataset.siteId + '/edit';
    document.getElementById('websiteName').value = dataset.siteName || '';
    document.getElementById('websiteUrl').value = dataset.siteUrl || '';
    document.getElementById('websiteModalTitle').textContent = 'Edit website';
    document.getElementById('websiteModalSubtitle').textContent = 'Changing the URL keeps the existing history for this monitor.';
    var submit = document.getElementById('websiteSubmit');
    submit.querySelector('span').textContent = 'Save changes';
    submit.querySelector('i').className = 'fa-solid fa-floppy-disk';
    modal.show();
  }

  /* ------------------------------------------------------------------ */
  /* Confirm dialog + generic POST actions                               */
  /* ------------------------------------------------------------------ */
  var pendingForm = null;

  function confirmModal() {
    var node = document.getElementById('confirmModal');
    if (!node || !window.bootstrap) return null;
    return window.bootstrap.Modal.getOrCreateInstance(node);
  }

  function interceptConfirm(form) {
    var modal = confirmModal();
    if (!modal) {
      if (window.confirm(form.getAttribute('data-confirm') || 'Are you sure?')) form.submit();
      return;
    }
    pendingForm = form;
    var text = document.getElementById('confirmText');
    if (text) text.textContent = form.getAttribute('data-confirm') || 'This action cannot be undone.';
    modal.show();
  }

  async function runPost(button) {
    var url = button.getAttribute('data-post');
    if (!url) return;
    var original = button.innerHTML;
    button.classList.add('is-busy');
    button.disabled = true;
    try {
      var response = await fetch(url, {
        method: 'POST',
        headers: { 'X-Requested-With': 'fetch' },
        redirect: 'follow'
      });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      PW.toast('Check completed. Refreshing data…', 'success');
      window.setTimeout(function () { window.location.reload(); }, 450);
    } catch (error) {
      PW.toast('The check could not be started.', 'danger');
      button.classList.remove('is-busy');
      button.disabled = false;
      button.innerHTML = original;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Sidebar                                                             */
  /* ------------------------------------------------------------------ */
  function initSidebar() {
    var toggle = document.getElementById('sidebarToggle');
    var sidebar = document.getElementById('sidebar');
    var backdrop = document.getElementById('sidebarBackdrop');
    if (!toggle || !sidebar) return;

    function close() {
      sidebar.classList.remove('is-open');
      if (backdrop) backdrop.hidden = true;
      toggle.setAttribute('aria-expanded', 'false');
    }

    toggle.addEventListener('click', function () {
      var open = sidebar.classList.toggle('is-open');
      if (backdrop) backdrop.hidden = !open;
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    if (backdrop) backdrop.addEventListener('click', close);
    sidebar.querySelectorAll('a').forEach(function (link) {
      link.addEventListener('click', close);
    });
  }

  /* ------------------------------------------------------------------ */
  /* Settings live preview                                               */
  /* ------------------------------------------------------------------ */
  function initSettingsPreview() {
    var fast = document.getElementById('fast_threshold');
    var moderate = document.getElementById('moderate_threshold');
    if (!fast || !moderate) return;

    function update() {
      document.querySelectorAll('[data-preview="fast"]').forEach(function (node) {
        node.textContent = fast.value || '—';
      });
      document.querySelectorAll('[data-preview="moderate"]').forEach(function (node) {
        node.textContent = moderate.value || '—';
      });
    }
    fast.addEventListener('input', update);
    moderate.addEventListener('input', update);
  }

  /* ------------------------------------------------------------------ */
  /* Global listeners                                                    */
  /* ------------------------------------------------------------------ */
  document.addEventListener('click', function (event) {
    var themeButton = event.target.closest('[data-theme-toggle]');
    if (themeButton) {
      applyTheme(currentTheme() === 'dark' ? 'light' : 'dark', true);
      return;
    }

    var addButton = event.target.closest('[data-add-site]');
    if (addButton) {
      event.preventDefault();
      openAddModal();
      return;
    }

    var editButton = event.target.closest('[data-edit-site]');
    if (editButton) {
      event.preventDefault();
      openEditModal(editButton.dataset);
      return;
    }

    var postButton = event.target.closest('[data-post]');
    if (postButton) {
      event.preventDefault();
      runPost(postButton);
      return;
    }

    var refresh = event.target.closest('#dashRefresh');
    if (refresh) {
      refresh.disabled = true;
      refreshDashboard(true).finally(function () { refresh.disabled = false; });
    }
  });

  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (form.matches && form.matches('form[data-confirm]')) {
      event.preventDefault();
      interceptConfirm(form);
    }
  });

  document.addEventListener('DOMContentLoaded', function () {
    syncThemeIcon();
    hydrateTimes();
    initSidebar();
    initSettingsPreview();

    var accept = document.getElementById('confirmAccept');
    if (accept) {
      accept.addEventListener('click', function () {
        var modal = confirmModal();
        if (modal) modal.hide();
        if (pendingForm) {
          var form = pendingForm;
          pendingForm = null;
          form.submit();
        }
      });
    }

    document.querySelectorAll('[data-autodismiss]').forEach(function (alert) {
      window.setTimeout(function () {
        if (window.bootstrap && alert.isConnected) {
          window.bootstrap.Alert.getOrCreateInstance(alert).close();
        }
      }, 6500);
    });

    if (document.getElementById('dashTableBody')) {
      dashboardState.filters = initTableFilter({
        searchId: 'dashSearch',
        statusId: 'dashStatusFilter',
        bodyId: 'dashTableBody',
        counterId: 'dashTableCount',
        colSpan: 7
      });
      startPolling();
      hydrateTimes();
    }

    if (document.getElementById('siteTableBody')) {
      initTableFilter({
        searchId: 'siteSearch',
        statusId: 'siteStatusFilter',
        bodyId: 'siteTableBody',
        colSpan: 8
      });
    }
  });

  PW.hydrateTimes = hydrateTimes;
  PW.statusPill = statusPill;
  PW.formatDuration = formatDuration;
})();
