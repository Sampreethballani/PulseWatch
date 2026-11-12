/* PulseWatch — Chart.js visualisations, availability timeline, DNS + TLS panels */
(function () {
  'use strict';

  var PW = window.PulseWatch || {};
  var charts = {};

  function ready() {
    return typeof window.Chart !== 'undefined';
  }

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function themeColors() {
    return {
      primary: css('--primary') || '#4f46e5',
      success: css('--success') || '#0f9d68',
      warning: css('--warning') || '#d97706',
      danger: css('--danger') || '#e5484d',
      grid: css('--chart-grid') || 'rgba(16,24,40,.08)',
      text: css('--chart-text') || '#667085',
      surface: css('--surface') || '#ffffff'
    };
  }

  async function getJSON(url) {
    var response = await fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store' });
    if (!response.ok) {
      var payload = null;
      try { payload = await response.json(); } catch (error) { /* ignore */ }
      throw new Error((payload && payload.error) || 'Request failed (' + response.status + ')');
    }
    return response.json();
  }

  function showEmpty(id, visible) {
    var node = document.getElementById(id);
    if (node) node.hidden = !visible;
  }

  function destroy(name) {
    if (charts[name]) {
      charts[name].destroy();
      delete charts[name];
    }
  }

  function localLabel(iso, hours) {
    var date = new Date(iso);
    if (isNaN(date.getTime())) return iso;
    var options = hours <= 24
      ? { hour: '2-digit', minute: '2-digit' }
      : { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' };
    return new Intl.DateTimeFormat(undefined, options).format(date);
  }

  function tooltipTitle(items) {
    if (!items.length) return '';
    var raw = items[0].label;
    var date = new Date(raw);
    return isNaN(date.getTime()) ? raw : date.toLocaleString();
  }

  function gradient(context, color, height) {
    var fill = context.createLinearGradient(0, 0, 0, height || 300);
    fill.addColorStop(0, color + '55');
    fill.addColorStop(1, color + '00');
    return fill;
  }

  function axisOptions(colors, hours, unitLabel) {
    return {
      x: {
        grid: { display: false },
        border: { color: colors.grid },
        ticks: {
          color: colors.text,
          font: { size: 10 },
          maxRotation: 0,
          autoSkip: true,
          maxTicksLimit: 7,
          callback: function (value) {
            var label = this.getLabelForValue(value);
            return localLabel(label, hours);
          }
        }
      },
      y: {
        beginAtZero: true,
        grid: { color: colors.grid, drawTicks: false },
        border: { display: false },
        ticks: {
          color: colors.text,
          font: { size: 10 },
          padding: 6,
          callback: function (value) { return value + (unitLabel || ''); }
        }
      }
    };
  }

  function basePlugins(colors) {
    return {
      legend: { display: false },
      tooltip: {
        backgroundColor: colors.surface,
        titleColor: colors.text,
        bodyColor: colors.text,
        borderColor: colors.grid,
        borderWidth: 1,
        padding: 10,
        displayColors: false,
        callbacks: { title: tooltipTitle }
      }
    };
  }

  /* ------------------------------------------------------------------ */
  /* Response time line                                                  */
  /* ------------------------------------------------------------------ */
  function renderHistory(canvasId, emptyId, data) {
    var canvas = document.getElementById(canvasId);
    if (!canvas) return;
    destroy(canvasId);
    var hasData = data.values.some(function (value) { return value !== null; });
    showEmpty(emptyId, !hasData);
    if (!hasData) return;

    var colors = themeColors();
    var context = canvas.getContext('2d');

    charts[canvasId] = new window.Chart(canvas, {
      type: 'line',
      data: {
        labels: data.labels,
        datasets: [{
          label: 'Response time (ms)',
          data: data.values,
          borderColor: colors.primary,
          backgroundColor: gradient(context, colors.primary, canvas.clientHeight || 300),
          borderWidth: 2,
          pointRadius: data.values.length > 50 ? 0 : 2.5,
          pointHoverRadius: 4,
          pointBackgroundColor: colors.primary,
          tension: 0.32,
          spanGaps: false,
          fill: true
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 420 },
        interaction: { mode: 'index', intersect: false },
        plugins: Object.assign(basePlugins(colors), {
          tooltip: {
            backgroundColor: colors.surface,
            titleColor: colors.text,
            bodyColor: colors.text,
            borderColor: colors.grid,
            borderWidth: 1,
            padding: 10,
            displayColors: false,
            callbacks: {
              title: tooltipTitle,
              label: function (item) {
                if (item.raw === null) return 'No successful check';
                var status = (data.statuses && data.statuses[item.dataIndex]) || 'UP';
                return item.raw + ' ms · ' + status;
              }
            }
          }
        }),
        scales: axisOptions(colors, data.hours, ' ms')
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /* Comparison + uptime bars                                            */
  /* ------------------------------------------------------------------ */
  function renderComparison(sites) {
    var canvas = document.getElementById('comparisonChart');
    if (!canvas) return;
    destroy('comparisonChart');

    var usable = sites.filter(function (site) { return site.avg_response_ms !== null; });
    showEmpty('comparisonChartEmpty', usable.length === 0);
    if (!usable.length) return;

    var colors = themeColors();
    charts.comparisonChart = new window.Chart(canvas, {
      type: 'bar',
      data: {
        labels: usable.map(function (site) { return site.name; }),
        datasets: [{
          label: 'Average response time',
          data: usable.map(function (site) { return site.avg_response_ms; }),
          backgroundColor: colors.primary + 'cc',
          hoverBackgroundColor: colors.primary,
          borderRadius: 6,
          maxBarThickness: 34
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: Object.assign(basePlugins(colors), {
          tooltip: {
            backgroundColor: colors.surface,
            titleColor: colors.text,
            bodyColor: colors.text,
            borderColor: colors.grid,
            borderWidth: 1,
            padding: 10,
            displayColors: false,
            callbacks: {
              label: function (item) { return item.raw + ' ms average'; }
            }
          }
        }),
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: colors.text, font: { size: 10 }, maxRotation: 0, autoSkip: true }
          },
          y: {
            beginAtZero: true,
            grid: { color: colors.grid, drawTicks: false },
            border: { display: false },
            ticks: { color: colors.text, font: { size: 10 }, callback: function (value) { return value + ' ms'; } }
          }
        }
      }
    });
  }

  function renderUptime(sites) {
    var canvas = document.getElementById('uptimeChart');
    if (!canvas) return;
    destroy('uptimeChart');

    var usable = sites.filter(function (site) { return site.uptime_pct !== null; });
    showEmpty('uptimeChartEmpty', usable.length === 0);
    if (!usable.length) return;

    var colors = themeColors();
    charts.uptimeChart = new window.Chart(canvas, {
      type: 'bar',
      data: {
        labels: usable.map(function (site) { return site.name; }),
        datasets: [{
          label: 'Uptime',
          data: usable.map(function (site) { return site.uptime_pct; }),
          backgroundColor: usable.map(function (site) {
            return site.uptime_pct >= 99 ? colors.success : site.uptime_pct >= 95 ? colors.warning : colors.danger;
          }),
          borderRadius: 6,
          maxBarThickness: 22
        }]
      },
      options: {
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        plugins: Object.assign(basePlugins(colors), {
          tooltip: {
            backgroundColor: colors.surface,
            titleColor: colors.text,
            bodyColor: colors.text,
            borderColor: colors.grid,
            borderWidth: 1,
            padding: 10,
            displayColors: false,
            callbacks: {
              label: function (item) { return item.raw.toFixed(2) + '% uptime'; }
            }
          }
        }),
        scales: {
          x: {
            min: 0,
            max: 100,
            grid: { color: colors.grid, drawTicks: false },
            border: { display: false },
            ticks: { color: colors.text, font: { size: 10 }, callback: function (value) { return value + '%'; } }
          },
          y: {
            grid: { display: false },
            ticks: { color: colors.text, font: { size: 10 } }
          }
        }
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /* Availability timeline                                               */
  /* ------------------------------------------------------------------ */
  function renderTimeline(data) {
    var host = document.getElementById('availabilityTimeline');
    if (!host) return;
    host.textContent = '';

    data.buckets.forEach(function (bucket) {
      var bar = document.createElement('div');
      bar.className = 'timeline-bar tl-' + bucket.status;
      var rangeStart = PW.formatTime ? PW.formatTime(bucket.start) : bucket.start;
      var rangeEnd = PW.formatTime ? PW.formatTime(bucket.end) : bucket.end;
      bar.title = rangeStart + ' – ' + rangeEnd + ' · '
        + (bucket.total ? bucket.up + '/' + bucket.total + ' up' : 'no checks');
      host.append(bar);
    });

    var summary = document.getElementById('timelineSummary');
    if (summary) {
      summary.textContent = data.uptime_pct === null
        ? 'No checks in this period'
        : data.uptime_pct.toFixed(2) + '% uptime · ' + data.total_checks + ' checks · '
          + data.bucket_minutes + ' min buckets';
    }
    var start = document.getElementById('timelineStart');
    var end = document.getElementById('timelineEnd');
    if (start) start.textContent = PW.formatTime ? PW.formatTime(data.window_start) : '';
    if (end) end.textContent = PW.formatTime ? PW.formatTime(data.window_end) : 'now';
  }

  /* ------------------------------------------------------------------ */
  /* DNS + TLS                                                           */
  /* ------------------------------------------------------------------ */
  function setChip(id, text, tone) {
    var node = document.getElementById(id);
    if (!node) return;
    node.className = 'chip chip-xs chip-' + (tone || 'muted');
    node.textContent = text;
  }

  function kv(label, value) {
    var row = document.createElement('div');
    row.className = 'kv';
    var span = document.createElement('span');
    span.textContent = label;
    var strong = document.createElement('strong');
    strong.textContent = value;
    row.append(span, strong);
    return row;
  }

  async function loadDns(siteId) {
    setChip('dnsState', 'resolving', 'muted');
    try {
      var data = await getJSON('/api/websites/' + siteId + '/dns');
      var ipv4 = document.getElementById('dnsIpv4');
      var ipv6 = document.getElementById('dnsIpv6');
      var checked = document.getElementById('dnsChecked');
      if (ipv4) ipv4.textContent = data.ipv4.length ? data.ipv4.join(', ') : '—';
      if (ipv6) ipv6.textContent = data.ipv6.length ? data.ipv6.join(', ') : '—';
      if (checked) {
        checked.textContent = PW.formatTime ? PW.formatTime(data.checked_at, false) : 'just now';
        checked.title = data.checked_at;
      }
      setChip('dnsState', 'live', 'success');
    } catch (error) {
      setChip('dnsState', 'failed', 'danger');
      var target = document.getElementById('dnsIpv4');
      if (target) target.textContent = 'Lookup failed';
    }
  }

  function sslBanner(text, tone, iconName) {
    var banner = document.createElement('div');
    banner.className = 'ssl-banner ' + tone;
    var iconEl = document.createElement('i');
    iconEl.className = 'fa-solid ' + iconName;
    var span = document.createElement('span');
    span.textContent = text;
    banner.append(iconEl, span);
    return banner;
  }

  function renderSsl(data) {
    var body = document.getElementById('sslBody');
    if (!body) return;
    body.textContent = '';

    if (!data.applicable) {
      setChip('sslState', 'N/A', 'muted');
      body.append(sslBanner('SSL monitoring is not applicable for HTTP-only websites.', 'neutral', 'fa-circle-info'));
      body.append(kv('Scheme', 'http'));
      return;
    }

    var tone = 'neutral';
    var label = 'unknown';
    if (data.status === 'valid') { tone = 'success'; label = 'valid'; }
    else if (data.status === 'warning') { tone = 'warning'; label = 'expiring'; }
    else if (data.status === 'expired') { tone = 'danger'; label = 'expired'; }
    else if (data.status === 'invalid') { tone = 'danger'; label = 'invalid'; }
    else if (data.status === 'error') { tone = 'danger'; label = 'error'; }

    setChip('sslState', label, tone === 'neutral' ? 'muted' : tone);

    if (data.status === 'valid') {
      body.append(sslBanner('Certificate is valid and trusted.', 'good', 'fa-lock'));
    } else if (data.status === 'warning') {
      body.append(sslBanner('Certificate expires within 30 days — renew soon.', 'warn', 'fa-clock'));
    } else if (data.status === 'expired') {
      body.append(sslBanner('Certificate has expired.', 'bad', 'fa-lock-open'));
    } else if (data.status === 'invalid') {
      body.append(sslBanner(data.error || 'Certificate failed verification.', 'bad', 'fa-triangle-exclamation'));
    } else {
      body.append(sslBanner(data.error || 'TLS inspection unavailable.', 'neutral', 'fa-circle-info'));
    }

    if (data.issuer) body.append(kv('Issuer', data.issuer));
    if (data.expires_at) {
      body.append(kv('Expires', PW.formatTime ? PW.formatTime(data.expires_at) : data.expires_at));
    }
    if (data.days_remaining !== null && data.days_remaining !== undefined) {
      body.append(kv('Days remaining', data.days_remaining + (data.days_remaining === 1 ? ' day' : ' days')));
    }
    if (data.tls_version) body.append(kv('TLS version', data.tls_version));
    if (data.resolved_ip) body.append(kv('Connected to', data.resolved_ip));
  }

  async function loadSsl(siteId, refresh) {
    setChip('sslState', 'loading', 'muted');
    var body = document.getElementById('sslBody');
    if (body) {
      body.textContent = '';
      for (var index = 0; index < 3; index += 1) {
        var skeleton = document.createElement('div');
        skeleton.className = 'skeleton-row';
        body.append(skeleton);
      }
    }
    try {
      var data = await getJSON('/api/websites/' + siteId + '/ssl' + (refresh ? '?refresh=1' : ''));
      renderSsl(data);
    } catch (error) {
      setChip('sslState', 'error', 'danger');
      if (body) {
        body.textContent = '';
        body.append(sslBanner('TLS inspection is unavailable right now.', 'neutral', 'fa-circle-info'));
      }
    }
  }

  function initNetwork(siteId) {
    var refresh = document.getElementById('refreshNetwork');
    function load(force) {
      loadDns(siteId);
      loadSsl(siteId, Boolean(force));
    }
    if (refresh) {
      refresh.addEventListener('click', function () {
        refresh.disabled = true;
        load(true);
        window.setTimeout(function () { refresh.disabled = false; }, 1200);
      });
    }
    load(false);
  }

  /* ------------------------------------------------------------------ */
  /* Page initialisers                                                   */
  /* ------------------------------------------------------------------ */
  async function initAnalytics() {
    if (!ready()) return;
    var hours = Number(document.body.dataset.hours || 24);
    var select = document.getElementById('historySite');
    var data = await getJSON('/api/analytics/comparison?hours=' + hours);

    renderComparison(data.sites);
    renderUptime(data.sites);

    if (!select) return;
    select.textContent = '';
    if (!data.sites.length) {
      showEmpty('historyChartEmpty', true);
      return;
    }

    data.sites.forEach(function (site) {
      var option = document.createElement('option');
      option.value = site.id;
      option.textContent = site.name;
      select.append(option);
    });

    async function loadHistory() {
      var siteId = select.value;
      if (!siteId) return;
      try {
        var history = await getJSON('/api/websites/' + siteId + '/history?hours=' + hours);
        renderHistory('historyChart', 'historyChartEmpty', history);
      } catch (error) {
        showEmpty('historyChartEmpty', true);
        if (PW.toast) PW.toast('Could not load response time history.', 'danger');
      }
    }

    select.addEventListener('change', loadHistory);
    await loadHistory();
  }

  async function initDetails() {
    var siteId = document.body.dataset.websiteId;
    var hours = Number(document.body.dataset.hours || 24);
    if (!siteId) return;

    try {
      var timeline = await getJSON('/api/websites/' + siteId + '/timeline?hours=' + hours);
      renderTimeline(timeline);
    } catch (error) {
      var summary = document.getElementById('timelineSummary');
      if (summary) summary.textContent = 'Timeline unavailable';
    }

    if (ready()) {
      try {
        var history = await getJSON('/api/websites/' + siteId + '/history?hours=' + hours);
        renderHistory('responseChart', 'responseChartEmpty', history);
      } catch (error) {
        showEmpty('responseChartEmpty', true);
      }
    }

    initNetwork(siteId);

    if (ready()) {
      window.setInterval(function () {
        if (document.hidden) return;
        getJSON('/api/websites/' + siteId + '/timeline?hours=' + hours)
          .then(renderTimeline)
          .catch(function () { /* keep the previous timeline */ });
      }, 30000);
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    var onAnalytics = Boolean(document.getElementById('historyChart'));
    var onDetails = Boolean(document.getElementById('responseChart'));

    if (onAnalytics) {
      initAnalytics().catch(function () {
        showEmpty('comparisonChartEmpty', true);
        showEmpty('uptimeChartEmpty', true);
        showEmpty('historyChartEmpty', true);
      });
    }
    if (onDetails) {
      initDetails().catch(function () { /* panels degrade gracefully */ });
    }

    document.addEventListener('pw:theme', function () {
      if (onAnalytics) initAnalytics().catch(function () { /* ignore */ });
      if (onDetails && ready()) {
        var siteId = document.body.dataset.websiteId;
        var hours = Number(document.body.dataset.hours || 24);
        getJSON('/api/websites/' + siteId + '/history?hours=' + hours)
          .then(function (history) { renderHistory('responseChart', 'responseChartEmpty', history); })
          .catch(function () { /* ignore */ });
      }
    });
  });
})();
