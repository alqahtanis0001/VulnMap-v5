// static/js/scan_animation.js
(function(){
  if (!window.VM) {
    console.error('VM foundation not loaded before scan_animation.js');
    return;
  }

  const EP = VM.endpoints;
  const UI = VM.ui;
  // Manual scan-window timing control lives here.
  // The scan/search window stays open for this exact duration before any result can appear.
  const SCAN_WINDOW_DURATION_MS = 12000;
  // Optional random extra time. Keep 0 when you want the duration to be exact.
  const SCAN_WINDOW_RANDOM_EXTRA_MS = 0;
  // If the backend is still not done after the visual window finishes, wait this long before showing an error.
  const SCAN_BACKEND_GRACE_MS = 10000;
  const RESULT_POPUP_DELAY_MS = 260;
  const SCAN_FETCH_TIMEOUT_MS = 25000;
  const SCAN_HARD_STOP_BUFFER_MS = 5000;

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
    clearScanTimer('scanRequestTimer');
    clearScanTimer('scanVisualTimer');
    clearScanTimer('scanResultWaitTimer');
    clearScanTimer('scanResultTimer');
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

  function scanWindowDurationMs() {
    const baseMs = Math.max(0, Number(SCAN_WINDOW_DURATION_MS) || 0);
    const extraMs = Math.max(0, Number(SCAN_WINDOW_RANDOM_EXTRA_MS) || 0);
    return baseMs + (extraMs ? Math.floor(Math.random() * (extraMs + 1)) : 0);
  }

  function resultModal() {
    return document.getElementById('scan-result-overlay');
  }

  function closeScanResult() {
    const modal = resultModal();
    if (modal) modal.setAttribute('aria-hidden', 'true');
  }

  function showScanResult(payload) {
    const modal = resultModal();
    if (!modal) return;

    const mark = document.getElementById('scan-result-mark');
    const title = document.getElementById('scan-result-title');
    const body = document.getElementById('scan-result-body');
    const closeBtn = document.getElementById('scan-result-close');
    const changed = Number(payload && payload.changed || 0);
    if (!mark || !title || !body) return;

    mark.classList.remove('is-empty', 'is-error');
    if (payload && payload.ok) {
      title.textContent = 'اكتمل الفحص بنجاح';
      if (changed > 0) {
        mark.textContent = '✓';
        body.textContent = `تم اكتشاف ${changed} منفذ(اً) جديداً.`;
      } else {
        mark.textContent = '0';
        mark.classList.add('is-empty');
        body.textContent = 'لم يتم العثور على منافذ جديدة.';
      }
    } else {
      mark.textContent = '!';
      mark.classList.add('is-error');
      title.textContent = 'تعذّر إكمال الفحص';
      body.textContent = 'حدث خطأ أثناء فحص المنافذ. حاول مرة أخرى.';
    }

    modal.setAttribute('aria-hidden', 'false');
    if (closeBtn) {
      try { closeBtn.focus({ preventScroll: true }); }
      catch (err) { closeBtn.focus(); }
    }
  }

  function attachResultHandlers() {
    const modal = resultModal();
    if (!modal || modal._vmBound) return;

    const closeBtn = document.getElementById('scan-result-close');
    if (closeBtn) closeBtn.addEventListener('click', closeScanResult);
    modal.addEventListener('click', e => {
      if (e.target === modal) closeScanResult();
    });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && modal.getAttribute('aria-hidden') === 'false') {
        closeScanResult();
      }
    });
    modal._vmBound = true;
  }

  function resetScanUi(runId, btn, btnLabel) {
    if (!isCurrentScan(runId)) return;

    clearScanTimer('scanCloseTimer');
    clearScanTimer('scanFailSafeTimer');
    clearScanTimer('scanRequestTimer');
    clearScanTimer('scanVisualTimer');
    clearScanTimer('scanResultWaitTimer');
    clearScanTimer('scanResultTimer');
    hideScanOverlay();
    if (btn) {
      btn.disabled = false;
      btn.classList.remove('disabled');
      btn.textContent = btnLabel || '🔍 فحص المنافذ';
    }
    VM.state.scanInFlight = false;
  }

  function showScanResultAfterOverlay(runId, payload) {
    if (!isCurrentScan(runId)) return;
    clearScanTimer('scanResultTimer');
    VM.state.scanResultTimer = setTimeout(() => {
      if (!isCurrentScan(runId) || VM.state.scanInFlight) return;
      showScanResult(payload);
    }, RESULT_POPUP_DELAY_MS);
  }

  // ---- Attach UI handlers ----
  function attachHandlers() {
    attachResultHandlers();

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
    closeScanResult();

    const overlay = UI.overlay, bar = UI.scanBar, status = UI.scanStatus;
    const btn = UI.scanBtn;
    const btnLabel = btn ? btn.textContent : '';
    let backendDone = false;
    let visualDone = false;
    let finalized = false;
    let payload = null;

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

    const totalMs = scanWindowDurationMs();
    const start = performance.now();

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
    function settleScanPayload(nextPayload) {
      if (!isActiveScan(runId) || backendDone) return;
      payload = nextPayload || { ok:false };
      backendDone = true;
      clearScanTimer('scanRequestTimer');
      clearScanTimer('scanResultWaitTimer');
      finishIfReady();
    }

    VM.state.scanRequestTimer = setTimeout(() => {
      settleScanPayload({ ok:false, error:'timeout' });
    }, SCAN_FETCH_TIMEOUT_MS);

    function scheduleResultWait() {
      if (!isActiveScan(runId) || backendDone || VM.state.scanResultWaitTimer) return;
      if (status) status.textContent = 'تجهيز نتيجة الفحص...';
      VM.state.scanResultWaitTimer = setTimeout(() => {
        settleScanPayload({ ok:false, error:'timeout' });
      }, RESULT_WAIT_AFTER_VISUAL_MS);
    }

    function finishIfReady() {
      if (!isActiveScan(runId)) return;
      if (finalized || !visualDone || !backendDone) return;
      finalized = true;

      try {
        const changed = (payload && payload.changed) ? payload.changed : 0;

        if (status) {
          if (payload && payload.ok) {
            status.textContent = changed > 0
              ? `تم اكتشاف ${changed} منفذ(اً).`
              : 'لم يتم العثور على منافذ جديدة.';
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
        // Close overlay smoothly, then show an explicit result window.
        clearScanTimer('scanCloseTimer');
        VM.state.scanCloseTimer = setTimeout(() => {
          if (!isCurrentScan(runId)) return;
          resetScanUi(runId, btn, btnLabel);
          showScanResultAfterOverlay(runId, payload);
        }, 650);
      }
    }

    function tick(now) {
      if (!isActiveScan(runId) || finalized) return;

      const elapsed = now - start;
      const rawPct = Math.round((elapsed / totalMs) * 100);
      const pct = backendDone ? Math.min(99, rawPct) : Math.min(94, rawPct);

      // Smooth easing for progress bar
      const easedPct = Math.pow(pct / 100, 0.8) * 100;
      if (bar) bar.style.width = easedPct.toFixed(1) + '%';

      // Update stage message evenly
      if (stepIdx < steps.length && elapsed >= (stepIdx + 1) * stepDur) {
        if (status) status.textContent = steps[stepIdx++];
      }

      if (elapsed >= totalMs) {
        visualDone = true;
        scheduleResultWait();
        finishIfReady();
      }

      if (!finalized) {
        requestAnimationFrame(tick);
      }
    }

    requestAnimationFrame(tick);

    // Fire backend request right away. If it never settles, the result wait
    // timer turns the stalled scan into a visible error dialog.
    fetch(EP.scanJson, {
      method: 'POST',
      headers: {
        'X-Requested-With': 'fetch',
        'X-CSRFToken': csrfToken()
      },
      credentials: 'same-origin'
    }).then(r => r.json())
      .then(d => { settleScanPayload(d); })
      .catch(() => { settleScanPayload({ ok:false }); });

    // Hard fail-safe: never leave scan lock stuck on client.
    VM.state.scanFailSafeTimer = setTimeout(() => {
      if (!isActiveScan(runId)) return;
      payload = payload || { ok:false };
      resetScanUi(runId, btn, btnLabel);
      showScanResultAfterOverlay(runId, payload);
    }, 30000);
  }

  // ---- Initialize ----
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attachHandlers, { once: true });
  } else {
    attachHandlers();
  }

})();
