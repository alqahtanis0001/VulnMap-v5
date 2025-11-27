(function(){
  const root = document.getElementById('news-search-module');
  if (!root) return;

  const btn = root.querySelector('[data-role="news-button"]');
  const progressWrap = root.querySelector('[data-role="news-progress-wrap"]');
  const progressFill = root.querySelector('[data-role="news-progress-fill"]');
  const timerEl = root.querySelector('[data-role="news-timer"]');
  const resultBox = root.querySelector('[data-role="news-result"]');
  const resultTitle = root.querySelector('[data-role="news-result-title"]');
  const resultBody = root.querySelector('[data-role="news-result-body"]');
  const hitMeta = root.querySelector('[data-role="news-hit-meta"]');
  const preview = root.querySelector('[data-role="news-preview"]');
  const statusPill = root.querySelector('[data-role="news-status-pill"]');
  const clearBtn = root.querySelector('[data-role="news-clear"]');
  const clearBtnLabel = clearBtn ? (clearBtn.textContent.trim() || 'حذف الإشعار') : 'حذف الإشعار';

  const endpoints = (window.VM && window.VM.endpoints) || {};
  const csrfToken = (window.VM && window.VM.csrfToken) ? window.VM.csrfToken : function(){ return ''; };

  let bootstrap = {};
  try {
    bootstrap = JSON.parse(endpoints.newsBootstrap || '{}');
  } catch (err) {
    bootstrap = {};
  }
  let job = bootstrap.job || null;
  updatePreview(null, false);

  let tickTimer = null;
  let pollTimer = null;

  renderJob(job);
  scheduleStatusFetch(2000);

  if (btn) {
    btn.addEventListener('click', function(){
      if (job && job.status === 'in_progress') {
        return;
      }
      startSearch();
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener('click', handleClearClick);
  }

  function setStatus(state, label){
    if (!statusPill) return;
    statusPill.dataset.state = state;
    statusPill.textContent = label;
  }

  function updatePreview(hitView, show){
    if (!preview) return;
    if (show && hitView && hitView.display_full) {
      preview.innerHTML = 'آخر توقيت منشور: <strong>' + hitView.display_full + '</strong> — ' + (hitView.duration_label || 'غير محدد');
    } else {
      preview.textContent = 'سيظهر تقرير الذكاء الاصطناعي فور اكتمال البحث وتحليل قاعدة بيانات VulnMap.';
    }
  }

  function resetUI(){
    setStatus('idle', 'هادئ');
    if (progressWrap) progressWrap.hidden = true;
    if (timerEl) timerEl.textContent = '';
    if (progressFill) progressFill.style.width = '0%';
    if (resultBox) resultBox.hidden = true;
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'تشغيل البحث الذكي';
    }
  }

  function renderJob(newJob){
    job = newJob || null;
    clearInterval(tickTimer);
    tickTimer = null;
    if (!job) {
      resetUI();
      return;
    }

    if (job.status === 'in_progress') {
      if (btn) {
        btn.disabled = true;
        btn.textContent = 'الذكاء الاصطناعي يعمل...';
      }
      if (progressWrap) progressWrap.hidden = false;
      if (resultBox) resultBox.hidden = true;
      setStatus('running', 'الذكاء الاصطناعي يبحث');
      updateProgressUI();
      tickTimer = setInterval(updateProgressUI, 1000);
      scheduleStatusFetch(30000);
    } else if (job.status === 'completed') {
      setStatus('done', job.result && job.result.has_hit ? 'خبر عاجل من الذكاء الاصطناعي' : 'انتهى التحليل');
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'تشغيل مهمة جديدة';
      }
      if (progressWrap) progressWrap.hidden = false;
      if (progressFill) progressFill.style.width = '100%';
      if (timerEl) timerEl.textContent = 'اكتمل البحث.';
      showResult(job.result || null);
      if (!(job.result && job.result.has_hit)) {
        updatePreview(null, false);
      }
    } else {
      resetUI();
    }
  }

  function startSearch(){
    if (resultBox) resultBox.hidden = true;
    setStatus('running', 'الذكاء الاصطناعي يبحث');
    if (progressWrap) progressWrap.hidden = false;
    if (progressFill) progressFill.style.width = '3%';
    if (timerEl) timerEl.textContent = 'يتم تشغيل العقدة البعيدة للذكاء الاصطناعي والتحكم في زحف الأخبار...';

    fetch(endpoints.newsStart || '/news-search/start', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': typeof csrfToken === 'function' ? csrfToken() : '',
        'X-Requested-With': 'fetch'
      },
      credentials: 'same-origin',
      body: '{}'
    })
    .then(r => r.json().then(body => ({ status: r.status, body })))
    .then(({ status, body }) => {
      if (status === 200 && body && body.ok) {
        renderJob(body.job || null);
      } else {
        resetUI();
      }
    })
    .catch(() => resetUI());
  }

  function updateProgressUI(){
    if (!job || job.status !== 'in_progress') return;
    const started = Date.parse(job.started_at || '');
    const eta = job.eta ? Date.parse(job.eta) : (started + (job.duration_sec || 0) * 1000);
    if (Number.isFinite(started) && Number.isFinite(eta) && eta > started) {
      const now = Date.now();
      const progress = Math.min(0.99, Math.max(0, (now - started) / (eta - started)));
      if (progressFill) progressFill.style.width = (progress * 100).toFixed(2) + '%';
      const remainingMs = Math.max(0, eta - now);
      const remainingSec = Math.ceil(remainingMs / 1000);
      if (timerEl) timerEl.textContent = 'الذكاء الاصطناعي يجمع الأدلة... الوقت المتبقي: ' + formatRemaining(remainingSec);
      if (remainingSec <= 1) {
        scheduleStatusFetch(1500);
      }
    }
  }

  function formatRemaining(totalSeconds){
    if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) {
      return 'جارٍ إنهاء التحليل العقلي...';
    }
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    if (minutes > 0) {
      return minutes + ' د ' + seconds.toString().padStart(2, '0') + ' ث من معالجة VulnMap';
    }
    return seconds + ' ثوانٍ';
  }

  function showResult(result){
    if (!resultBox) return;
    resultBox.hidden = false;
    const hasHit = result && result.has_hit;
    if (resultTitle) {
      resultTitle.textContent = hasHit ? 'ذكاء VulnMap رصد ضربة جديدة' : 'أتم الذكاء الاصطناعي المسح ولم يجد ضربات';
    }
    if (resultBody) {
      resultBody.textContent = result ? (result.message || 'لم يُرجع الذكاء الاصطناعي أي تفاصيل إضافية.') : '';
    }
    if (hitMeta) {
      if (hasHit && result.hit_display) {
        const h = result.hit_display;
        let text = 'التاريخ: ' + (h.display_full || '—');
        text += ' — المدة: ' + (h.duration_label || 'غير محددة');
        if (h.details) {
          text += ' — ' + h.details;
        }
        hitMeta.textContent = text;
      } else {
        hitMeta.textContent = '';
      }
    }
    if (hasHit && result.hit_display) {
      updatePreview(result.hit_display, true);
    } else {
      updatePreview(null, false);
    }
    if (clearBtn) {
      clearBtn.disabled = false;
      clearBtn.hidden = false;
      clearBtn.textContent = clearBtnLabel;
    }
  }

  function scheduleStatusFetch(delayMs){
    clearTimeout(pollTimer);
    if (!delayMs) delayMs = 30000;
    pollTimer = setTimeout(fetchStatus, delayMs);
  }

  function fetchStatus(){
    fetch(endpoints.newsStatus || '/news-search/status', {
      method: 'GET',
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' }
    })
    .then(r => r.json().then(body => ({ status: r.status, body })))
    .then(({ status, body }) => {
      if (status === 200 && body && body.ok) {
        renderJob(body.job || null);
      }
    })
    .catch(()=>{})
    .finally(() => {
      if (job && job.status === 'in_progress') {
        scheduleStatusFetch(30000);
      }
    });
  }

  function handleClearClick(){
    if (!endpoints.newsClear) {
      renderJob(null);
      updatePreview(null, false);
      return;
    }
    if (clearBtn) {
      clearBtn.disabled = true;
      clearBtn.textContent = 'جارٍ الحذف...';
    }
    fetch(endpoints.newsClear, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': typeof csrfToken === 'function' ? csrfToken() : '',
        'X-Requested-With': 'fetch'
      },
      credentials: 'same-origin',
      body: '{}'
    })
    .then(r => r.json().then(body => ({ status: r.status, body })))
    .then(({ status, body }) => {
      if (status === 200 && body && body.ok) {
        renderJob(null);
        updatePreview(null, false);
        if (clearBtn) {
          clearBtn.textContent = 'تم الحذف';
        }
      } else {
        throw new Error('clear_failed');
      }
    })
    .catch(() => {
      if (clearBtn) {
        clearBtn.textContent = 'تعذّر الحذف';
        setTimeout(() => {
          clearBtn.textContent = clearBtnLabel;
          clearBtn.disabled = false;
        }, 1600);
      }
    })
    .finally(() => {
      if (clearBtn && clearBtn.textContent !== 'تعذّر الحذف') {
        setTimeout(() => {
          clearBtn.textContent = clearBtnLabel;
          clearBtn.disabled = false;
        }, 1200);
      }
    });
  }
})();
