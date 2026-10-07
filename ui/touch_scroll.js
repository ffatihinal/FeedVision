/*
 * touch_scroll.js — YEDEK çözüm: dokunmatik ekran Chromium'a "mouse" olarak
 * geldiğinde (pointerType: "mouse") sürüklemeyi sayfa kaydırmaya çevirir.
 * Gerçek touch olaylarına dokunmaz; sadece mousedown/mousemove/mouseup dinler.
 * Kaçış yolu: Shift/Alt/Ctrl/Meta basılıyken normal fare davranışı korunur.
 */
(function () {
  "use strict";

  var DRAG_THRESHOLD_PX = 6;
  var FRICTION = 0.94;
  var MIN_VELOCITY = 0.3; // px/kare; altına inince atalet durur
  var MAX_VELOCITY = 35; // px/kare; kontrolsüz fırlamayı önler
  var MAX_INERTIA_DISTANCE = 600; // px; toplam atalet kaydırması bunu geçmez
  var MIN_DT_MS = 8; // ara mousemove'suz tek büyük sıçramadan sahte hız çıkmasın
  // Kaydırma devre dışı hedefler: slider/metin kutusu/ROI canvas'ı bozulmasın
  var SKIP_SELECTOR = "input, textarea, select, canvas, [contenteditable], iframe, [data-no-drag-scroll]";

  var state = null; // { x, y, lastX, lastY, lastT, scroller, dragging, vx, vy }
  var inertiaId = 0;
  var suppressClick = false;

  function isScrollable(el) {
    var cs = getComputedStyle(el);
    var canY = /(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight;
    var canX = /(auto|scroll)/.test(cs.overflowX) && el.scrollWidth > el.clientWidth;
    return canY || canX;
  }

  function findScroller(target) {
    for (var el = target; el && el !== document.body && el !== document.documentElement; el = el.parentElement) {
      if (el.nodeType === 1 && isScrollable(el)) return el;
    }
    return document.scrollingElement || document.documentElement;
  }

  function stopInertia() {
    if (inertiaId) cancelAnimationFrame(inertiaId);
    inertiaId = 0;
  }

  function reset() {
    state = null;
  }

  function onMouseDown(e) {
    stopInertia(); // yeni dokunuş atalet kaydırmayı iptal eder
    if (e.button !== 0 || e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return;
    var t = e.target;
    if (!(t instanceof Element) || t.closest(SKIP_SELECTOR)) return;
    // Seçim ve görsel sürüklemeyi başlatmasın
    e.preventDefault();
    // preventDefault odaklanmayı da engeller; açık input'tan odağı elle bırak
    if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
    state = {
      x: e.clientX, y: e.clientY, lastX: e.clientX, lastY: e.clientY, lastT: performance.now(),
      scroller: findScroller(t), dragging: false, vx: 0, vy: 0,
    };
  }

  function onMouseMove(e) {
    if (!state) return;
    // mouseup kaçırıldıysa (ör. pencere dışında bırakma) fare gezinmesi sayfayı kaydırmaya devam etmesin
    if (e.buttons === 0) { reset(); return; }
    if (!state.dragging) {
      if (Math.abs(e.clientX - state.x) < DRAG_THRESHOLD_PX && Math.abs(e.clientY - state.y) < DRAG_THRESHOLD_PX) return;
      state.dragging = true;
    }
    var dx = state.lastX - e.clientX;
    var dy = state.lastY - e.clientY;
    var now = performance.now();
    var dt = Math.max(now - state.lastT, MIN_DT_MS);
    // Hızı ~16ms'lik kareye normalize et, yumuşatarak sakla
    state.vx = 0.6 * (dx * 16 / dt) + 0.4 * state.vx;
    state.vy = 0.6 * (dy * 16 / dt) + 0.4 * state.vy;
    state.lastX = e.clientX;
    state.lastY = e.clientY;
    state.lastT = now;
    state.scroller.scrollLeft += dx;
    state.scroller.scrollTop += dy;
    e.preventDefault();
  }

  function onMouseUp() {
    if (!state) return;
    var s = state;
    reset();
    if (!s.dragging) return; // eşik aşılmadı: normal tıklama çalışsın
    suppressClick = true;
    // click, mouseup'tan hemen sonra gelir; gelmezse (farklı eleman) bayrak takılı kalmasın
    setTimeout(function () { suppressClick = false; }, 0);
    // Bırakmadan önce uzun durulduysa hız anlamsızdır
    if (performance.now() - s.lastT > 100) return;
    startInertia(s.scroller, clampVelocity(s.vx), clampVelocity(s.vy));
  }

  function clampVelocity(v) {
    return Math.max(-MAX_VELOCITY, Math.min(MAX_VELOCITY, v));
  }

  function startInertia(scroller, vx, vy) {
    var travelled = 0;
    function step() {
      vx *= FRICTION;
      vy *= FRICTION;
      if (Math.abs(vx) < MIN_VELOCITY && Math.abs(vy) < MIN_VELOCITY) { inertiaId = 0; return; }
      var beforeX = scroller.scrollLeft, beforeY = scroller.scrollTop;
      scroller.scrollLeft += vx;
      scroller.scrollTop += vy;
      // Sınıra dayandıysa dur
      if (scroller.scrollLeft === beforeX && scroller.scrollTop === beforeY) { inertiaId = 0; return; }
      travelled += Math.abs(scroller.scrollLeft - beforeX) + Math.abs(scroller.scrollTop - beforeY);
      if (travelled >= MAX_INERTIA_DISTANCE) { inertiaId = 0; return; }
      inertiaId = requestAnimationFrame(step);
    }
    inertiaId = requestAnimationFrame(step);
  }

  function onClickCapture(e) {
    if (!suppressClick) return;
    suppressClick = false;
    e.preventDefault();
    e.stopPropagation();
  }

  function onDragStart(e) {
    // Canlı yayın <img>'i ve diğer görseller sürüklenmesin (engelli hedefler hariç)
    if (e.target instanceof Element && !e.target.closest(SKIP_SELECTOR)) e.preventDefault();
  }

  document.addEventListener("mousedown", onMouseDown, { capture: true, passive: false });
  window.addEventListener("mousemove", onMouseMove, { passive: false });
  window.addEventListener("mouseup", onMouseUp);
  window.addEventListener("blur", reset);
  document.documentElement.addEventListener("mouseleave", reset);
  document.addEventListener("click", onClickCapture, true);
  document.addEventListener("dragstart", onDragStart, true);
})();
