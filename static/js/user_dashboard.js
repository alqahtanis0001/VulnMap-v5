// static/js/user_dashboard.js
(function () {
  // ---------- Endpoint bootstrap ----------
  function getEndpoints() {
    const el = document.getElementById('app-endpoints');
    if (!el || !el.dataset) {
      console.error('app-endpoints element missing');
      return { scanJson: '/scan.json', resolveJson: '/resolve.json', archiveJson: '/archive.json' };
    }
    return {
      scanJson: el.dataset.scanJson || '/scan.json',
      resolveJson: el.dataset.resolveJson || '/resolve.json',
      archiveJson: el.dataset.archiveJson || '/archive.json',
      withdrawJson: el.dataset.withdrawJson || '/withdraw.json'
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

  function idempotencyKey(){
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return String(Date.now()) + Math.random().toString(16).slice(2);
  }

  // ---------- Row templating & rendering ----------
  function rowHtml(p, kind){
    const common =
      `<td class="td-port">${p.port_number}</td>`+
      `<td>—</td>`+
      `<td class="td-reward">${formatReward(p.reward||0)}</td>`;

    if (kind === 'discovered') {
      return `<tr data-row-id="${p.id}">
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
      return `<tr data-row-id="${p.id}">
        ${common}
        <td class="td-status">✅ تم الحل</td>
        <td><span class="small muted">—</span></td>
      </tr>`;
    }

    // archived
    return `<tr data-row-id="${p.id}">
      ${common}
      <td class="td-status">📦 مؤرشف</td>
      <td><span class="small muted">—</span></td>
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
  window.VM = Object.freeze({
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
    }
  });

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
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
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
