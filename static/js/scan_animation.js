// static/js/scan_animation.js
(function(){
  if (!window.VM) {
    console.error('VM foundation not loaded before scan_animation.js');
    return;
  }

  const EP = VM.endpoints;
  const UI = VM.ui;

  function clearScanTimer(name) {
    if (!VM.state) return;
    const timer = VM.state[name];
    if (timer !== undefined && timer !== null) {
      clearTimeout(timer);
      VM.state[name] = null;
    }
  }

  function beginScanRun() {
    VM.state = VM.state || {};
    clearScanTimer('scanCloseTimer');
    clearScanTimer('scanFailSafeTimer');
    VM.state.scanRunId = (VM.state.scanRunId || 0) + 1;
    VM.state.scanInFlight = true;
    return VM.state.scanRunId;
  }

  function isCurrentScan(runId) {
    return !!(VM.state && VM.state.scanRunId === runId);
  }

  function isActiveScan(runId) {
    return isCurrentScan(runId) && !!VM.state.scanInFlight;
  }

  function csrfToken(){
    if (typeof window.getCsrfToken === 'function') {
      return window.getCsrfToken();
    }
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? (meta.getAttribute('content') || '') : '';
  }

  function showScanOverlay() {
    const overlay = UI.overlay;
    if (overlay) {
      overlay.style.setProperty('display', 'flex', 'important');
      overlay.style.setProperty('visibility', 'visible', 'important');
      overlay.style.setProperty('opacity', '1', 'important');
      overlay.style.setProperty('position', 'fixed', 'important');
      overlay.style.setProperty('inset', '0', 'important');
      overlay.style.setProperty('z-index', '2147483000', 'important');
      overlay.setAttribute('aria-hidden', 'false');
    } else if (VM.progress && typeof VM.progress.show === 'function') {
      VM.progress.show('جارٍ الفحص...', 'يتم تحليل المنافذ، يرجى الانتظار.');
    }
  }

  function hideScanOverlay() {
    const overlay = UI.overlay;
    if (overlay) {
      overlay.style.setProperty('display', 'none', 'important');
      overlay.setAttribute('aria-hidden', 'true');
    }
    if (VM.progress && typeof VM.progress.hide === 'function') {
      VM.progress.hide();
    }
  }

  function resetScanUi(runId, btn, btnLabel) {
    if (!isCurrentScan(runId)) return;

    clearScanTimer('scanCloseTimer');
    clearScanTimer('scanFailSafeTimer');
    hideScanOverlay();
    if (btn) {
      btn.disabled = false;
      btn.classList.remove('disabled');
      btn.textContent = btnLabel || '🔍 فحص المنافذ';
    }
    VM.state.scanInFlight = false;
  }

  // ---- Attach UI handlers ----
  function attachHandlers() {
    const btn  = UI.scanBtn;
    const form = UI.scanForm;
    if (btn && !btn._vmBound) {
      btn.addEventListener('click', doScan);
      btn._vmBound = true;
    }
    if (form && !form._vmBound) {
      form.addEventListener('submit', e => { e.preventDefault(); doScan(); });
      form._vmBound = true;
    }
  }

  // ---- Core Scan Flow ----
  function doScan() {
    VM.state = VM.state || {};
    if (VM.state.scanInFlight) return;
    const runId = beginScanRun();

    const overlay = UI.overlay, bar = UI.scanBar, status = UI.scanStatus;
    const btn = UI.scanBtn;
    const btnLabel = btn ? btn.textContent : '';

    // Overlay setup
    showScanOverlay();
    if (btn) {
      btn.disabled = true;
      btn.classList.add('disabled');
      btn.textContent = '⏳ جارٍ الفحص...';
    }
    if (bar) {
      bar.style.transition = 'width .15s linear';
      bar.style.width = '0%';
    }
    if (status) status.textContent = 'تهيئة محرك الاكتشاف...';

    // Duration: 6–10 seconds
    const totalMs = 12000 + Math.floor(Math.random() * 6000);
    const start = performance.now();

    // Fire backend request right away
    let payload = null;
    const req = fetch(EP.scanJson, {
      method: 'POST',
      headers: {
        'X-Requested-With': 'fetch',
        'X-CSRFToken': csrfToken()
      },
      credentials: 'same-origin'
    }).then(r => r.json())
      .then(d => { payload = d; })
      .catch(() => { payload = { ok:false }; });

    // Step messages (Arabic)
    const steps = [
      'تهيئة محرك الاكتشاف...',
      'مسح الشبكة...',
      'تحليل المنافذ المحسن...',
      'التقاط الثغرات...',
      'مزامنة النتائج...'
    ];
    let stepIdx = 0;
    const stepDur = totalMs / steps.length;

    // ---- Animation Tick ----
    function tick(now) {
      if (!isActiveScan(runId)) return;

      const elapsed = now - start;
      const pct = Math.min(100, Math.round((elapsed / totalMs) * 100));

      // Smooth easing for progress bar
      const easedPct = Math.pow(pct / 100, 0.8) * 100;
      if (bar) bar.style.width = easedPct.toFixed(1) + '%';

      // Update stage message evenly
      if (stepIdx < steps.length && elapsed >= (stepIdx + 1) * stepDur) {
        if (status) status.textContent = steps[stepIdx++];
      }

      if (elapsed < totalMs) {
        requestAnimationFrame(tick);
        return;
      }

      Promise.resolve(req).finally(() => {
        if (!isActiveScan(runId)) return;

        try {
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

          // Update wallet and tables
          if (payload && payload.ok) {
            VM.updateCountsWallet(payload);
            VM.render.renderAll(payload.discovered, payload.resolved, payload.archived);
          }

          // Finishing effects
          if (bar) {
            bar.style.transition = 'width 0.4s ease-out';
            bar.style.width = '100%';
          }
        } catch (err) {
          console.error('scan finalize failed', err);
        } finally {
          // Close overlay smoothly (always reset UI state).
          clearScanTimer('scanCloseTimer');
          VM.state.scanCloseTimer = setTimeout(() => {
            resetScanUi(runId, btn, btnLabel);
          }, 900);
        }
      });
    }

    requestAnimationFrame(tick);

    // Hard fail-safe: never leave scan lock stuck on client.
    VM.state.scanFailSafeTimer = setTimeout(() => {
      if (!isActiveScan(runId)) return;
      resetScanUi(runId, btn, btnLabel);
    }, 30000);
  }

  // ---- Initialize ----
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attachHandlers, { once: true });
  } else {
    attachHandlers();
  }

})();
