/* VulnMap — index_login visual polish
   ------------------------------------------------------------
   Effects:
   - Background glow animator
   - Card 3D tilt (mouse / pointer)
   - Button ripple
   - Input focus halo
   - Reveal on load / intersection
   - Shake on error flashes, confetti on success
   - Respects prefers-reduced-motion
*/

(() => {
  const doc = document;
  const prefersReduced =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // -------- Utilities
  const clamp = (n, min, max) => Math.min(max, Math.max(min, n));
  const lerp = (a, b, t) => a + (b - a) * t;

  // Find the main auth card on this page only
  const card = doc.querySelector(".card.rtl");
  const btns = Array.from(doc.querySelectorAll(".btn"));
  const inputs = Array.from(doc.querySelectorAll(".input, input, select, textarea"));

  // -------- Background glow (CSS variables)
  // Creates a slow-shifting gradient glow across the page background.
  const bg = {
    t: 0,
    speed: 0.0009,
    rafId: null,
    tick() {
      this.t += this.speed;
      // Two orbs gently orbiting; values tuned for a subtle effect
      const x1 = 60 + Math.sin(this.t) * 30;   // %
      const y1 = -8 + Math.cos(this.t * 0.7) * 8;
      const x2 = -10 + Math.cos(this.t * 0.9) * 25;
      const y2 = 10 + Math.sin(this.t * 0.6) * 10;

      // Apply as background with CSS var updates (no layout thrash)
      doc.documentElement.style.setProperty(
        "--vm-bg-orb-1",
        `radial-gradient(1200px 600px at ${x1}% ${y1}%, rgba(42,111,255,.085), transparent 55%)`
      );
      doc.documentElement.style.setProperty(
        "--vm-bg-orb-2",
        `radial-gradient(1000px 500px at ${x2}% ${y2}%, rgba(102,217,168,.06), transparent 50%)`
      );

      // Compose on <body> background if not reduced
      if (!prefersReduced) {
        document.body.style.backgroundImage =
          `var(--vm-bg-orb-1), var(--vm-bg-orb-2), ${getComputedStyle(document.body).backgroundImage || "none"}`;
      }
      this.rafId = requestAnimationFrame(this.tick.bind(this));
    },
    start() {
      if (prefersReduced) return;
      // Avoid duplicating the orbs on re-init
      if (!this.rafId) this.rafId = requestAnimationFrame(this.tick.bind(this));
    },
    stop() {
      if (this.rafId) cancelAnimationFrame(this.rafId);
      this.rafId = null;
    },
  };

  // -------- Card tilt
  const tilt = {
    max: 6,           // degrees
    shadow: 24,       // px shadow movement
    active: false,
    onMove(e) {
      if (!card) return;
      const rect = card.getBoundingClientRect();
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      const dx = (e.clientX - cx) / (rect.width / 2);  // -1..1
      const dy = (e.clientY - cy) / (rect.height / 2); // -1..1
      const rx = clamp(-dy * this.max, -this.max, this.max);
      const ry = clamp(dx * this.max, -this.max, this.max);
      card.style.transform = `rotateX(${rx}deg) rotateY(${ry}deg) translateZ(0)`;
      card.style.transition = "transform .06s ease";
      card.style.willChange = "transform";
      card.style.boxShadow = `0 ${lerp(8, this.shadow, Math.abs(dy))}px 30px rgba(0,0,0,.28)`;
    },
    onLeave() {
      if (!card) return;
      card.style.transform = "rotateX(0deg) rotateY(0deg)";
      card.style.boxShadow = "";
      card.style.transition = "transform .25s ease";
    },
    bind() {
      if (!card || prefersReduced) return;
      card.addEventListener("pointermove", this.onMove.bind(this));
      card.addEventListener("pointerleave", this.onLeave.bind(this));
      this.active = true;
    }
  };

  // -------- Button ripple
  function attachRipples() {
    btns.forEach((b) => {
      if (b.dataset.rippleBound) return;
      b.dataset.rippleBound = "1";
      b.style.overflow = "hidden";
      b.style.position = b.style.position || "relative";
      b.addEventListener("click", function (e) {
        if (prefersReduced) return;
        const rect = b.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const d = Math.max(rect.width, rect.height);
        const r = doc.createElement("span");
        r.style.position = "absolute";
        r.style.left = `${x - d / 2}px`;
        r.style.top = `${y - d / 2}px`;
        r.style.width = r.style.height = `${d}px`;
        r.style.borderRadius = "50%";
        r.style.background = "rgba(255,255,255,.25)";
        r.style.transform = "scale(0)";
        r.style.pointerEvents = "none";
        r.style.transition = "transform .45s ease, opacity .6s ease";
        b.appendChild(r);
        requestAnimationFrame(() => {
          r.style.transform = "scale(1)";
          r.style.opacity = "0";
        });
        setTimeout(() => r.remove(), 600);
      });
    });
  }

  // -------- Inputs: focus halo
  function enhanceInputs() {
    inputs.forEach((el) => {
      if (el.dataset.haloBound) return;
      el.dataset.haloBound = "1";
      el.addEventListener("focus", () => {
        el.style.boxShadow = "0 0 0 2px rgba(42,111,255,.35)";
      });
      el.addEventListener("blur", () => {
        el.style.boxShadow = "";
      });
    });
  }

  // -------- Reveal in
  function reveal() {
    const items = [
      ...doc.querySelectorAll(".chip, .elev, form .input, form .btn, form .btn.secondary")
    ];
    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (en.isIntersecting) {
          en.target.style.transform = "translateY(0)";
          en.target.style.opacity = "1";
          io.unobserve(en.target);
        }
      });
    }, { threshold: 0.1 });

    items.forEach((el, i) => {
      el.style.opacity = "0";
      el.style.transform = "translateY(8px)";
      el.style.transition = `opacity .5s ease ${i * 40}ms, transform .5s ease ${i * 40}ms`;
      io.observe(el);
    });
  }

  // -------- Shake on error, confetti on success
  function feedback() {
    const hasErr = !!doc.querySelector(".flash.err");
    const hasOk  = !!doc.querySelector(".flash.ok");
    if (hasErr && card) {
      card.animate(
        [
          { transform: "translateX(0)" },
          { transform: "translateX(-6px)" },
          { transform: "translateX(6px)" },
          { transform: "translateX(0)" }
        ],
        { duration: 320, easing: "ease" }
      );
    }
    if (hasOk) confettiBurst();
  }

  // Minimal confetti (emoji) — zero dependencies
  function confettiBurst() {
    if (prefersReduced) return;
    const N = 14;
    for (let i = 0; i < N; i++) {
      const s = doc.createElement("div");
      s.textContent = ["✨", "🎉", "🥳", "💫"][i % 4];
      s.style.position = "fixed";
      s.style.zIndex = "9999";
      s.style.pointerEvents = "none";
      s.style.left = `${Math.random() * 100}%`;
      s.style.top = "-10px";
      s.style.fontSize = `${Math.round(16 + Math.random() * 10)}px`;
      s.style.transform = `translateY(0) rotate(${Math.random() * 360}deg)`;
      doc.body.appendChild(s);
      const endY = window.innerHeight + 40;
      const duration = 900 + Math.random() * 700;
      s.animate(
        [
          { transform: s.style.transform, opacity: 1 },
          { transform: `translateY(${endY}px) rotate(${Math.random() * 720}deg)`, opacity: 0 }
        ],
        { duration, easing: "cubic-bezier(.22,.61,.36,1)" }
      ).onfinish = () => s.remove();
    }
  }

  // -------- Init
  document.addEventListener("DOMContentLoaded", () => {
    bg.start();
    tilt.bind();
    attachRipples();
    enhanceInputs();
    reveal();
    feedback();

    // If base.html progress API exists, close it on load (in case a previous route left it open)
    if (window.VM && VM.progress) {
      VM.progress.hide();
    }
  });

  // Clean up (if this page is replaced by HTMX/pjax in future)
  window.addEventListener("beforeunload", () => bg.stop());
})();
