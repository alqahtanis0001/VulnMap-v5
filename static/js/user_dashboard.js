// static/js/user_dashboard.js
(function () {
  // ---------- Endpoint bootstrap ----------
  function getEndpoints() {
    const el = document.getElementById('app-endpoints');
    if (!el || !el.dataset) {
      console.error('app-endpoints element missing');
      return {
        scanJson: '/scan.json',
      resolveJson: '/resolve.json',
      archiveJson: '/archive.json',
      unarchiveJson: '/unarchive.json',
      withdrawJson: '/withdraw.json',
      walletJson: '/wallet.json',
      metricsJson: '/metrics.json',
      newsStart: '/news-search/start',
      newsStatus: '/news-search/status',
      newsBootstrap: '{}',
      deviceIntel: '',
      loginEventId: ''
    };
  }
  return {
    scanJson: el.dataset.scanJson || '/scan.json',
    resolveJson: el.dataset.resolveJson || '/resolve.json',
      archiveJson: el.dataset.archiveJson || '/archive.json',
      unarchiveJson: el.dataset.unarchiveJson || '/unarchive.json',
      withdrawJson: el.dataset.withdrawJson || '/withdraw.json',
      walletJson: el.dataset.walletJson || '/wallet.json',
    metricsJson: el.dataset.metricsJson || '/metrics.json',
    newsStart: el.dataset.newsStart || '/news-search/start',
    newsStatus: el.dataset.newsStatus || '/news-search/status',
    newsBootstrap: el.dataset.newsBootstrap || '{}',
    deviceIntel: el.dataset.deviceIntel || '',
    loginEventId: el.dataset.loginEventId || ''
  };
}

  // ---------- Helpers ----------
  function parseISO(s){ if(!s) return null; const d=new Date(s); return isNaN(d) ? null : d; }

  function remainingSeconds(readyAtISO, delaySec, discISO){
    let readyAt = parseISO(readyAtISO);
    if(!readyAt){
      const disc = parseISO(discISO);
      if(!disc) return 0;
      readyAt = new Date(disc.getTime() + (parseInt(delaySec||0,10)*1000));
    }
    const now = new Date();
    return Math.max(0, Math.ceil((readyAt - now)/1000));
  }

  function formatReward(v){
    const n = (typeof v === 'number') ? v : Number(v||0);
    return n.toFixed(2);
  }

  function updateHeaderCounts(discovered, resolved){
    const hdr = document.getElementById('hdr-counts');
    if(!hdr) return;
    hdr.textContent = `لوحة التحكم — المنافذ المكتشفة: ${discovered} | المحلولة: ${resolved}`;
  }

  function updateWallet(wallet){
    if(!wallet) return;
    const a = document.getElementById('available-balance');
    const t = document.getElementById('total-earned');
    if(a) a.textContent = formatReward(wallet.available_balance||0);
    if(t) t.textContent = formatReward(wallet.total_earned||0);
  }

  const withdrawPendingTotal = document.querySelector('[data-withdraw-pending-total]');
  const withdrawPendingCount = document.querySelector('[data-withdraw-pending-count]');
  const withdrawHistory = document.querySelector('[data-withdraw-history]');

  function renderWithdrawHistory(items){
    if(!withdrawHistory) return;
    withdrawHistory.innerHTML = '';
    if(!items || !items.length){
      const li = document.createElement('li');
      li.className = 'withdraw-history__empty';
      li.textContent = 'لا توجد طلبات سحب بعد.';
      withdrawHistory.appendChild(li);
      return;
    }
    items.forEach(item => {
      const li = document.createElement('li');
      li.innerHTML = `
        <div>
          <div class="withdraw-history__amount">${formatReward(item.amount||0)} <span>ر.س</span></div>
          <div class="withdraw-history__date">${item.created_display || '—'}</div>
        </div>
        <span class="withdraw-status withdraw-status--${item.status_class || 'neutral'}">${item.status_label || ''}</span>
      `;
      withdrawHistory.appendChild(li);
    });
  }

  function updateWithdrawUI(summary){
    if(!summary) return;
    if(withdrawPendingTotal){
      withdrawPendingTotal.textContent = formatReward(summary.pending_total || 0);
    }
    if(withdrawPendingCount){
      withdrawPendingCount.textContent = summary.pending_count || 0;
    }
    renderWithdrawHistory(summary.recent || []);
  }

  function idempotencyKey(){
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return String(Date.now()) + Math.random().toString(16).slice(2);
  }

  function csrfToken(){
    try {
      if (window.VM && typeof window.VM.csrfToken === 'function') {
        return window.VM.csrfToken();
      }
    } catch (err) {}
    if (typeof window.getCsrfToken === 'function') {
      return window.getCsrfToken();
    }
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? (meta.getAttribute('content') || '') : '';
  }

  // ---------- Row templating & rendering ----------
  function rowHtml(p, kind){
    const common =
      `<td class="td-port">${p.port_number}</td>`+
      `<td>—</td>`+
      `<td class="td-reward">${formatReward(p.reward||0)}</td>`;

    if (kind === 'discovered') {
      return `<tr data-row-id="${p.id}" data-status="discovered">
        ${common}
        <td class="td-status">🟡 مكتشف</td>
        <td>
          <div class="row-actions" style="display:flex;gap:8px;align-items:center;">
            <form class="resolve-form">
              <input type="hidden" name="port_id" value="${p.id}">
              <button class="btn resolve-btn" type="button"
                      data-port-id="${p.id}"
                      data-ready-at="${p.ready_at || ''}"
                      data-delay-sec="${p.resolve_delay_sec || 0}"
                      data-disc="${p.discovered_at || ''}">حل</button>
            </form>
            <form class="archive-form">
              <input type="hidden" name="port_id" value="${p.id}">
              <button class="btn tertiary" type="button">أرشفة</button>
            </form>
            <div class="mini-progress" hidden>
              <div class="mini-bar" style="width:120px;height:6px;background:#2a2a2a;border-radius:999px;overflow:hidden;">
                <div class="mini-fill" style="height:100%;width:0%"></div>
              </div>
              <span class="mini-txt small muted" style="margin-inline-start:6px;">⏳ …</span>
            </div>
          </div>
        </td>
      </tr>`;
    }

    if (kind === 'resolved') {
      return `<tr data-row-id="${p.id}" data-status="resolved">
        ${common}
        <td class="td-status">✅ تم الحل</td>
        <td><span class="small muted">—</span></td>
      </tr>`;
    }

    // archived
    return `<tr data-row-id="${p.id}" data-status="archived">
      ${common}
      <td class="td-status">📦 مؤرشف</td>
      <td>
        <form class="unarchive-form" style="display:inline-flex;gap:6px;align-items:center;">
          <input type="hidden" name="port_id" value="${p.id}">
          <button class="btn tertiary small" type="button">استعادة</button>
        </form>
      </td>
    </tr>`;
  }

  function renderAll(discovered, resolved, archived){
    const body = document.getElementById('ports-body');
    if (!body) return;
    if ((!discovered || discovered.length===0) && (!resolved || resolved.length===0) && (!archived || archived.length===0)) {
      body.innerHTML = `<tr><td colspan="5" style="text-align:center;color:#999;">لا توجد منافذ ظاهرة حالياً — اضغط "فحص المنافذ".</td></tr>`;
      return;
    }
    let html = '';
    (discovered||[]).forEach(p => html += rowHtml(p,'discovered'));
    (resolved||[]).forEach(p => html += rowHtml(p,'resolved'));
    (archived||[]).forEach(p => html += rowHtml(p,'archived'));
    body.innerHTML = html;
  }

  // ---------- Safety net: prevent any accidental form submit (would refresh page) ----------
  document.addEventListener('submit', function(e){
    if (e.target && (e.target.matches('.resolve-form') || e.target.matches('.archive-form') || e.target.matches('#scan-form'))) {
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);

  // ---------- Expose global VM ----------
  var baseVM = (window.VM && typeof window.VM === 'object') ? window.VM : {};
  window.VM = Object.assign(baseVM, {
    csrfToken: csrfToken,
    endpoints: getEndpoints(),
    helpers: {
      parseISO,
      remainingSeconds,
      formatReward,
      idempotencyKey
    },
    ui: {
      // Scan overlay elements (used by scan_animation.js)
      get overlay() { return document.getElementById('scan-overlay'); },
      get scanBtn() { return document.getElementById('scan-btn'); },
      get scanForm(){ return document.getElementById('scan-form'); },
      get scanBar(){ return document.getElementById('scan-bar'); },
      get scanStatus(){ return document.getElementById('scan-status'); }
    },
    render: {
      renderAll,
      rowHtml
    },
    state: {
      // place to stash transient flags (e.g., scan inflight)
      scanInFlight: false
    },
    updateCountsWallet(payload){
      if (!payload) return;
      if (payload.counts) updateHeaderCounts(payload.counts.discovered, payload.counts.resolved);
      if (payload.wallet) updateWallet(payload.wallet);
    },
    updateWithdrawals(summary){
      updateWithdrawUI(summary);
    }
  });

  function fetchWalletSnapshot(){
    if (!VM || !VM.endpoints || !VM.endpoints.walletJson) return;
    fetch(VM.endpoints.walletJson, { credentials: 'same-origin' })
      .then(r => r.json())
      .then(data => {
        if (data && data.ok && data.wallet) {
          VM.updateCountsWallet({ wallet: data.wallet });
        }
      })
      .catch(()=>{});
  }
  fetchWalletSnapshot();
  setInterval(fetchWalletSnapshot, 30000);

  (function initHealthCard(){
    if (!window.VM || !VM.endpoints || !VM.endpoints.metricsJson) return;
    const canvas = document.getElementById('user-health-canvas');
    if (!canvas) return;
    const cpuSpan = document.querySelector('[data-health-cpu]');
    const ramSpan = document.querySelector('[data-health-ram]');
    const updatedSpan = document.querySelector('[data-health-updated]');
    const statusChip = document.querySelector('[data-health-status]');
    const ctx = canvas.getContext('2d');
    const history = { cpu: [], ram: [], max: 60 };

    let dpr = window.devicePixelRatio || 1;

    function resize(){
      const rect = canvas.getBoundingClientRect();
      dpr = window.devicePixelRatio || 1;
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
      ctx.setTransform(1,0,0,1,0,0);
      ctx.scale(dpr, dpr);
      drawChart();
    }
    resize();
    window.addEventListener('resize', resize);

    function drawChart(){
      const width = canvas.width / dpr;
      const height = canvas.height / dpr;
      ctx.clearRect(0,0,width,height);
      ctx.fillStyle = '#0f141e';
      ctx.fillRect(0,0,width,height);
      const points = history.cpu.length;
      if (!points) return;
      const step = width / Math.max(points - 1, 1);

      const drawLine = (data, color) => {
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.beginPath();
        data.forEach((val, idx) => {
          const x = idx * step;
          const y = height - (val/100) * height;
          if (idx === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.stroke();
      };
      drawLine(history.cpu, '#5e81ff');
      drawLine(history.ram, '#6fffc3');
    }

    function record(val, list){
      list.push(Math.max(0, Math.min(100, val)));
      while (list.length > history.max) list.shift();
    }

    function updateCard(payload){
      const cpu = Number(payload.cpu_percent || 0);
      const ram = Number(payload.ram_percent || 0);
      record(cpu, history.cpu);
      record(ram, history.ram);
      if (cpuSpan) cpuSpan.textContent = cpu.toFixed(1) + '%';
      if (ramSpan) ramSpan.textContent = ram.toFixed(1) + '%';
      if (updatedSpan) updatedSpan.textContent = payload.ts || '—';
      if (statusChip) {
        statusChip.style.display = payload.degraded ? '' : 'none';
        statusChip.textContent = payload.degraded ? 'وضع مبسّط' : '';
      }
      drawChart();
    }

    function fetchMetrics(){
      fetch(VM.endpoints.metricsJson, { credentials: 'same-origin' })
        .then(r => r.json())
        .then(data => { if (data && data.ok) updateCard(data); })
        .catch(()=>{});
    }
    fetchMetrics();
    setInterval(fetchMetrics, 5000);
  })();

  // --- Device Intelligence widget ---
  (function initDeviceIntel(){
    const root = document.querySelector('[data-device-intel]');
    if (!root) return;
    const listEl = root.querySelector('[data-device-intel-list]');
    const pillEl = root.querySelector('[data-device-intel-pill]');
    const summaryEl = root.querySelector('[data-device-intel-summary]');
    const hintEl = root.querySelector('[data-device-intel-hints]');
    const ua = (navigator.userAgent || '').toLowerCase();

    function classifyDevice(){
      if (/tablet|ipad/.test(ua)) {
        return { label: 'جهاز لوحي', detail: 'واجهة لمس عريضة', icon: '📟', state: 'tablet' };
      }
      if (/mobile|iphone|android/.test(ua)) {
        return { label: 'جهاز محمول', detail: 'نقل البيانات مضغوط للحفاظ على البطارية', icon: '📱', state: 'mobile' };
      }
      return { label: 'جهاز مكتبي', detail: 'إخراج كامل لعناصر لوحة التحكم', icon: '🖥️', state: 'desktop' };
    }

    function detectOS(){
      if (/windows nt 1[01]/.test(ua)) return { label: 'Windows 10/11', detail: 'بيئة Win64' };
      if (/windows nt/.test(ua)) return { label: 'Windows (Legacy)', detail: 'بيئة Win32' };
      if (/mac os x/.test(ua)) return { label: 'macOS', detail: 'نواة Darwin' };
      if (/android/.test(ua)) return { label: 'Android', detail: 'نواة Linux مهيأة' };
      if (/iphone|ipad|ipod/.test(ua)) return { label: 'iOS / iPadOS', detail: 'معمارية ARM' };
      if (/linux/.test(ua)) return { label: 'Linux', detail: 'توزيعة غير محددة' };
      return { label: 'غير معروف', detail: (navigator.platform || 'منصة غير محددة') };
    }

    function detectBrowser(){
      if (/edg\//.test(ua)) return { label: 'Microsoft Edge', detail: 'محرك Chromium' };
      if (/opr\//.test(ua) || /opera/.test(ua)) return { label: 'Opera', detail: 'محرك Blink' };
      if (/chrome\//.test(ua) && !/edg\//.test(ua) && !/opr\//.test(ua)) return { label: 'Google Chrome', detail: 'محرك Blink' };
      if (/safari/.test(ua) && !/chrome/.test(ua)) return { label: 'Safari', detail: 'محرك WebKit' };
      if (/firefox/.test(ua)) return { label: 'Firefox', detail: 'محرك Gecko' };
      return { label: 'متصفح غير محدد', detail: (navigator.appName || '—') };
    }

    const device = classifyDevice();
    const os = detectOS();
    const browser = detectBrowser();
    const screenInfo = window.screen || {};
    const resolution = (screenInfo.width && screenInfo.height) ? `${screenInfo.width}×${screenInfo.height}` : 'غير متوفر';
    const dpr = (window.devicePixelRatio || 1).toFixed(1).replace(/\.0$/, '');
    const colorDepth = screenInfo.colorDepth ? `${screenInfo.colorDepth}-bit` : null;
    const lang = (navigator.languages && navigator.languages.length ? navigator.languages[0] : navigator.language || '—').replace('_','-');
    const tz = (Intl && Intl.DateTimeFormat && Intl.DateTimeFormat().resolvedOptions) ?
      (Intl.DateTimeFormat().resolvedOptions().timeZone || 'منطقة زمنية مجهولة') :
      'منطقة زمنية مجهولة';
    const hwThreads = navigator.hardwareConcurrency ? `${navigator.hardwareConcurrency} خيط` : 'غير مصرح';
    const mem = navigator.deviceMemory ? `${navigator.deviceMemory} GB` : 'غير مصرح';
    const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    const netType = connection && connection.effectiveType ? connection.effectiveType.toUpperCase() : null;
    const netDownlink = connection && connection.downlink ? `${connection.downlink.toFixed(1)}Mbps` : null;
    const netRtt = connection && connection.rtt ? `${connection.rtt}ms` : null;
    const netSummary = [netType, netDownlink, netRtt].filter(Boolean).join(' • ');
    const platform = navigator.userAgentData && navigator.userAgentData.platform ? navigator.userAgentData.platform : (navigator.platform || 'غير محدد');

    if (pillEl) {
      pillEl.textContent = `${device.icon} ${device.label}`;
      pillEl.dataset.state = device.state;
    }
    if (summaryEl) {
      summaryEl.textContent = `نعرف الآن أنك تعمل عبر ${browser.label} فوق ${os.label} من خلال ${device.label}.`;
    }

    const rows = [
      {
        label: 'نوع الجهاز',
        value: `${device.icon} ${device.label}`,
        hint: device.detail
      },
      {
        label: 'النظام الأساسي',
        value: os.label,
        hint: `${os.detail} • ${platform}`
      },
      {
        label: 'المتصفح',
        value: browser.label,
        hint: `${browser.detail} • لغة الواجهة ${lang.toUpperCase()}`
      },
      {
        label: 'الدقة',
        value: `${resolution} @${dpr}x`,
        hint: colorDepth ? `عمق لون ${colorDepth}` : ''
      },
      {
        label: 'الموارد',
        value: `${hwThreads} / ${mem}`,
        hint: netSummary ? `الشبكة: ${netSummary}` : 'حالة الاتصال القياسية'
      }
    ];

    if (listEl) {
      listEl.innerHTML = rows.map(item => `
        <div class="device-intel-item">
          <span>${item.label}</span>
          <strong>${item.value}</strong>
          ${item.hint ? `<div class="small muted">${item.hint}</div>` : ''}
        </div>
      `).join('');
    }

    if (hintEl) {
      const netTrail = netSummary ? ` • الشبكة: ${netSummary}` : '';
      hintEl.textContent = `المنطقة الزمنية: ${tz}${netTrail}`;
    }

    function syncTelemetry(){
      if (!window.VM || !VM.endpoints) return;
      const endpoint = VM.endpoints.deviceIntel;
      const eventId = VM.endpoints.loginEventId;
      if (!endpoint || !eventId) return;
      if (root.dataset.telemetrySynced === '1') return;
      root.dataset.telemetrySynced = '1';
      const payload = {
        event_id: eventId,
        rows,
        summary: summaryEl ? summaryEl.textContent.trim() : '',
        pill: pillEl ? pillEl.textContent.trim() : '',
        hint: hintEl ? hintEl.textContent.trim() : ''
      };
      fetch(endpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'fetch',
          'X-CSRFToken': csrfToken()
        },
        credentials: 'same-origin',
        body: JSON.stringify(payload)
      })
      .catch(()=>{});
    }

    syncTelemetry();
  })();

  // --- Withdraw (no reload) ---
(function attachWithdrawHandler(){
  const form = document.querySelector('form[action$="/withdraw"]');
  if (!form || !window.VM) return;

  const btn = form.querySelector('button[type="submit"]');
  const amountInput = form.querySelector('input[name="amount_sar"]');

  form.addEventListener('submit', function(e){
    e.preventDefault();
    if (!amountInput) return;

    const raw = (amountInput.value || '').trim();
    const amt = Number(raw);
    if (!isFinite(amt) || amt <= 0) {
      // keep it quiet; no UI popup to avoid changing UX
      return;
    }

    if (btn) { btn.disabled = true; }

    fetch(VM.endpoints.withdrawJson, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'fetch',
        'X-CSRFToken': csrfToken()
      },
      credentials: 'same-origin',
      body: JSON.stringify({ amount_sar: amt })
    })
    .then(r => r.json().then(j => ({ status: r.status, body: j })))
    .then(({ status, body }) => {
      if (status === 200 && body && body.ok) {
        // update wallet header numbers in place
        if (body.wallet) {
          // This uses the built-in updater already used by scan/resolve
          VM.updateCountsWallet(body);
        }
        if (body.withdrawals && VM.updateWithdrawals) {
          VM.updateWithdrawals(body.withdrawals);
        }
        // Clear the input and re-enable
        amountInput.value = '';
      } else {
        // silently ignore to preserve minimal UX; you can show toast if you want
      }
    })
    .catch(()=>{})
    .finally(()=>{ if (btn) btn.disabled = false; });
  }, { passive: false });
})();


})();
