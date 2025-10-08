// static/js/scan_animation.js
(function(){
  if (!window.VM) {
    console.error('VM foundation not loaded before scan_animation.js');
    return;
  }

  const EP = VM.endpoints;
  const UI = VM.ui;

  // Attach once
  function attachHandlers(){
    const btn  = UI.scanBtn;
    const form = UI.scanForm;
    if (btn && !btn._vmBound) {
      btn.addEventListener('click', doScan);
      btn._vmBound = true;
    }
    if (form && !form._vmBound) {
      // safety: Enter key in input inside form
      form.addEventListener('submit', function(e){ e.preventDefault(); doScan(); });
      form._vmBound = true;
    }
  }

  // Core scan flow
  function doScan(){
    if (VM.state.scanInFlight) return;
    VM.state.scanInFlight = true;

    const overlay = UI.overlay, bar = UI.scanBar, status = UI.scanStatus;
    const btn = UI.scanBtn;

    // show overlay if exists
    if (overlay) {
      overlay.style.display = 'flex';
    }
    if (btn) {
      btn.disabled = true;
      btn.classList.add('disabled');
    }
    if (bar) bar.style.width = '0%';
    if (status) status.textContent = 'تهيئة محرك الاكتشاف...';

    // 4–9s animation
    const totalMs = 4000 + Math.floor(Math.random() * 5000);
    const start   = performance.now();

    // fire request immediately
    let payload = null;
    const req = fetch(EP.scanJson, {
      method: 'POST',
      headers: { 'X-Requested-With': 'fetch' },
      credentials: 'same-origin'
    }).then(r => r.json())
      .then(d => { payload = d; })
      .catch(() => { payload = { ok:false }; });

    // progress text stages
    const steps = [
      'تهيئة محرك الاكتشاف...',
      'مسح الشبكة...',
      'تحليل المنافذ...',
      'التقاط الثغرات...',
      'مزامنة النتائج...'
    ];
    let stepIdx = 0;

    function tick(now){
      const elapsed = now - start;
      const pct = Math.min(100, Math.round((elapsed/totalMs)*100));
      if (bar) bar.style.width = pct + '%';

      if (elapsed > (stepIdx+1)*800 && stepIdx < steps.length) {
        if (status) status.textContent = steps[stepIdx++];
      }

      if (elapsed < totalMs) {
        requestAnimationFrame(tick);
      } else {
        // wait for network completion if still pending
        Promise.resolve(req).finally(() => {
          const changed = (payload && payload.changed) ? payload.changed : 0;

          if (status) {
            if (payload && payload.ok) {
              status.textContent = changed > 0
                ? `تم اكتشاف ${changed} منفذ(اً).`
                : 'لا توجد ثغرات مكتشفة.';
            } else {
              status.textContent = 'تعذّر إكمال الفحص. حاول مجدداً.';
            }
          }

          if (payload && payload.ok) {
            // update counts/wallet + render rows in-place
            VM.updateCountsWallet(payload);
            VM.render.renderAll(payload.discovered, payload.resolved, payload.archived);
          }

          // graceful close
          setTimeout(() => {
            if (overlay) overlay.style.display = 'none';
            if (btn) { btn.disabled = false; btn.classList.remove('disabled'); }
            VM.state.scanInFlight = false;
          }, 600);
        });
      }
    }

    requestAnimationFrame(tick);
  }

  // init on DOM ready (in case this file loads before elements exist)
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attachHandlers, { once: true });
  } else {
    attachHandlers();
  }
})();
