// static/js/resolve_ports.js
(function () {
  if (!window.VM) {
    console.error('VM foundation not loaded before resolve_ports.js');
    return;
  }

  const EP = VM.endpoints;   // { resolveJson, archiveJson, unarchiveJson }
  const H  = VM.helpers;     // { idempotencyKey, ... }

  function csrfToken(){
    try {
      if (typeof VM.csrfToken === 'function') {
        return VM.csrfToken();
      }
    } catch (err) {}
    if (typeof window.getCsrfToken === 'function') {
      return window.getCsrfToken();
    }
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? (meta.getAttribute('content') || '') : '';
  }

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  if (!VM.state.rowTimers) {
    VM.state.rowTimers = new Map(); // key: rowId (or portId), value: setInterval handle
  }

  // Ensure any stray progress bars are hidden on load
  (function ensureMiniHidden() {
    const apply = () => document.querySelectorAll('.mini-progress').forEach(el => el.hidden = true);
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', apply, { once: true });
    } else {
      apply();
    }
  })();

  function stopTimerForRow(rowKey) {
    const t = VM.state.rowTimers.get(rowKey);
    if (t) {
      clearInterval(t);
      VM.state.rowTimers.delete(rowKey);
    }
  }

  // ---------------------------------------------------------------------------
  // API calls
  // ---------------------------------------------------------------------------
  function apiResolve(portId, attempt = 0) {
    return fetch(EP.resolveJson, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'fetch',
        'X-Idempotency-Key': H.idempotencyKey(),
        'X-CSRFToken': csrfToken()
      },
      credentials: 'same-origin',
      body: JSON.stringify({ port_id: portId })
    }).then(async r => {
      if (r.ok) {
        // server always returns JSON shape; may be {ok:false,error:'too_early',seconds_remaining:N}
        return r.json();
      }
      // transient conflicts / lock races → retry up to 2 times
      if ((r.status === 409 || r.status === 423) && attempt < 2) {
        await new Promise(res => setTimeout(res, 150 * (attempt + 1)));
        return apiResolve(portId, attempt + 1);
      }
      return { ok: false, error: 'http_' + r.status };
    });
  }

  function apiArchive(portId) {
    return fetch(EP.archiveJson, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'fetch',
        'X-CSRFToken': csrfToken()
      },
      credentials: 'same-origin',
      body: JSON.stringify({ port_id: portId })
    }).then(r => r.json());
  }

  function apiUnarchive(portId) {
    return fetch(EP.unarchiveJson, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'fetch',
        'X-CSRFToken': csrfToken()
      },
      credentials: 'same-origin',
      body: JSON.stringify({ port_id: portId })
    }).then(r => r.json());
  }

  function apiRemaining(portId) {
    // Use fixed endpoint; this primes per-click timer on the server
    return fetch(`/api/port/${encodeURIComponent(portId)}/remaining`, {
      credentials: 'same-origin'
    }).then(async r => {
      // Normalize to JSON response even on 4xx to simplify handling
      if (r.ok) return r.json();
      return Promise.reject({ status: r.status });
    });
  }

  // ---------------------------------------------------------------------------
  // UI helpers
  // ---------------------------------------------------------------------------
  function finalizeResolve(row, payload) {
    if (!row) return;
    const status = row.querySelector('.td-status');
    const actions = row.querySelector('.row-actions');
    const mini   = row.querySelector('.mini-progress');
    if (mini) mini.hidden = true;
    if (status) status.textContent = '✅ تم الحل';
    if (actions) actions.innerHTML = '<span class="small muted">—</span>';
    VM.updateCountsWallet(payload);
  }

  function finalizeArchive(row, payload) {
    if (!row) return;
    const status = row.querySelector('.td-status');
    const actions = row.querySelector('.row-actions');
    const mini   = row.querySelector('.mini-progress');
    if (mini) mini.hidden = true;
    if (status) status.textContent = '📦 مؤرشف';
    if (actions) actions.innerHTML = '<span class="small muted">—</span>';
  }

  function refreshFromPayload(payload) {
    if (!payload) return;
    if (payload.discovered && payload.resolved && payload.archived && VM.render && typeof VM.render.renderAll === 'function') {
      VM.render.renderAll(payload.discovered, payload.resolved, payload.archived);
    }
    VM.updateCountsWallet(payload);
  }

  function startMiniCountdown(row, rowId, seconds, onDone) {
    const mini = row.querySelector('.mini-progress');
    const fill = row.querySelector('.mini-fill');
    const txt  = row.querySelector('.mini-txt');

    if (mini) mini.hidden = false;
    if (fill) fill.style.width = '0%';
    if (txt)  txt.textContent = `⏳ ${seconds} ث`;

    stopTimerForRow(rowId);

    const total = Math.max(0, parseInt(seconds || 0, 10));
    const start = Date.now();

    const timer = setInterval(() => {
      const elapsed = Math.floor((Date.now() - start) / 1000);
      const left = Math.max(0, total - elapsed);
      const pct  = Math.min(100, Math.round(((total - left) / total) * 100));

      if (fill) fill.style.width = pct + '%';
      if (txt)  txt.textContent = `⏳ ${left} ث`;

      if (left <= 0) {
        clearInterval(timer);
        VM.state.rowTimers.delete(rowId);
        if (txt) txt.textContent = 'جارٍ الحل...';
        onDone && onDone();
      }
    }, 300);

    VM.state.rowTimers.set(rowId, timer);
  }

  // ---------------------------------------------------------------------------
  // Resolve flow
  // ---------------------------------------------------------------------------
  function handleResolve(btn) {
    const row   = btn.closest('tr');
    if (!row) return;

    const pid   = btn.dataset.portId;
    const rowId = row.getAttribute('data-row-id') || pid;

    // Replace any prior timer for this row
    stopTimerForRow(rowId);

    // Step A: Try authoritative remaining (primes server-side timer)
    apiRemaining(pid).then(data => {
      const remaining = Math.max(0, parseInt(data?.remaining ?? 0, 10));

      if (remaining <= 0) {
        // No wait needed → resolve immediately
        apiResolve(pid).then(payload => {
          if (payload && payload.ok) {
            finalizeResolve(row, payload);
          } else if (payload && payload.error === 'too_early' && payload.seconds_remaining > 0) {
            // Server says too early → show mini and then resolve
            startMiniCountdown(row, rowId, payload.seconds_remaining, () => {
              apiResolve(pid).then(p2 => {
                if (p2 && p2.ok) finalizeResolve(row, p2);
                else {
                  const mini = row.querySelector('.mini-progress');
                  if (mini) mini.hidden = true;
                }
              }).catch(() => {
                const mini = row.querySelector('.mini-progress');
                if (mini) mini.hidden = true;
              });
            });
          } else {
            const mini = row.querySelector('.mini-progress');
            if (mini) mini.hidden = true;
          }
        }).catch(() => {
          const mini = row.querySelector('.mini-progress');
          if (mini) mini.hidden = true;
        });
        return;
      }

      // Wait required → show mini and then resolve
      startMiniCountdown(row, rowId, remaining, () => {
        apiResolve(pid).then(payload => {
          if (payload && payload.ok) {
            finalizeResolve(row, payload);
          } else if (payload && payload.error === 'too_early' && payload.seconds_remaining > 0) {
            // Rare case: server still says too_early → run one more short countdown
            startMiniCountdown(row, rowId, payload.seconds_remaining, () => {
              apiResolve(pid).then(p2 => {
                if (p2 && p2.ok) finalizeResolve(row, p2);
                else {
                  const mini = row.querySelector('.mini-progress');
                  if (mini) mini.hidden = true;
                }
              }).catch(() => {
                const mini = row.querySelector('.mini-progress');
                if (mini) mini.hidden = true;
              });
            });
          } else {
            const mini = row.querySelector('.mini-progress');
            if (mini) mini.hidden = true;
          }
        }).catch(() => {
          const mini = row.querySelector('.mini-progress');
          if (mini) mini.hidden = true;
        });
      });
    }).catch((_err) => {
      // Step B: If remaining failed (404/403/network), try resolve once.
      apiResolve(pid).then(payload => {
        if (payload && payload.ok) {
          finalizeResolve(row, payload);
          return;
        }
        // If too_early, we still show a countdown using server-provided seconds_remaining.
        if (payload && payload.error === 'too_early' && payload.seconds_remaining > 0) {
          startMiniCountdown(row, rowId, payload.seconds_remaining, () => {
            apiResolve(pid).then(p2 => {
              if (p2 && p2.ok) finalizeResolve(row, p2);
              else {
                const mini = row.querySelector('.mini-progress');
                if (mini) mini.hidden = true;
              }
            }).catch(() => {
              const mini = row.querySelector('.mini-progress');
              if (mini) mini.hidden = true;
            });
          });
        } else {
          // Other errors → hide mini if shown
          const mini = row.querySelector('.mini-progress');
          if (mini) mini.hidden = true;
        }
      }).catch(() => {
        const mini = row.querySelector('.mini-progress');
        if (mini) mini.hidden = true;
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Archive flow
  // ---------------------------------------------------------------------------
  function handleArchive(btn) {
    const form = btn.closest('.archive-form');
    const row  = btn.closest('tr');
    if (!form || !row) return;

    const rowId = row.getAttribute('data-row-id');
    const pid   = form.querySelector('input[name="port_id"]')?.value;
    if (!pid) return;

    stopTimerForRow(rowId || pid);

    apiArchive(pid).then(data => {
      if (data && data.ok) {
        finalizeArchive(row, data);
        refreshFromPayload(data);
      } else {
        // optional toast
      }
    }).catch(() => {/* noop */});
  }

  function handleUnarchive(btn) {
    const form = btn.closest('.unarchive-form');
    const row  = btn.closest('tr');
    if (!form) return;
    const pid = form.querySelector('input[name="port_id"]')?.value;
    if (!pid) return;

    apiUnarchive(pid).then(data => {
      if (data && data.ok) {
        refreshFromPayload(data);
      } else {
        // optional toast
      }
    }).catch(() => {/* noop */});
  }

  // ---------------------------------------------------------------------------
  // Event Delegation
  // ---------------------------------------------------------------------------
  document.addEventListener('click', function (e) {
    const resolveBtn = e.target.closest('.resolve-btn');
    if (resolveBtn) {
      e.preventDefault();
      handleResolve(resolveBtn);
      return;
    }
    const archiveBtn = e.target.closest('.archive-form button');
    if (archiveBtn) {
      e.preventDefault();
      handleArchive(archiveBtn);
      return;
    }
    const unarchiveBtn = e.target.closest('.unarchive-form button');
    if (unarchiveBtn) {
      e.preventDefault();
      handleUnarchive(unarchiveBtn);
      return;
    }
  }, { passive: false });

})();
