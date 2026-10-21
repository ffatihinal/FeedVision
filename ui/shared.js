// FeedVision — Operatör ve Admin sayfaları arasında PAYLAŞILAN JS.
//
// Ne var burada: konsol log yardımcısı (logToConsole), canlı durum
// WebSocket bağlantısı (startStatusWebSocket) ve Kontrol Kriterleri alarm
// banner'ı + sesli alarm (renderRuleAlarm/playAlarmSound). Bu üçü hem
// Operatör hem Admin ekranında birebir aynı davranmalı — ayrı kopyalar
// zamanla birbirinden sapardı (15-09-2026, G grubu: Operatör/Admin
// ayrımı sırasında ortak mantık buraya çıkarıldı).
//
// 6 motorlu sistem (21-10-2026): Besleme Başlat paneli, bağımsız Rotasyon/Yardımcı çift
// panelleri, hareket durum lambası ve kanal bazlı hız doğrulaması da burada (dosya sonu).
//
// Motor ham komutları, ROI/kalibrasyon, kural tanımlama, grafikler gibi
// SADECE Admin'e özgü kod burada YOK — o admin.html'in kendi script'inde.

// ==========================================================================
//  KONSOL — buton/görev/sonuç kaydı. Operatör sayfasında #console elementi
//  YOK (kasıtlı, sade arayüz) — bu durumda sessizce sadece tarayıcı
//  konsoluna (devtools) yazar, sayfada hata fırlatmaz.
// ==========================================================================

const CONSOLE_MAX_LINES = 500;  // performans için cap — tam log geçmişi önemli değil

// Küçük yardımcı: metni bir <span>'e textContent ile koyar (innerHTML YOK) —
// buttonName/task/result değerleri kullanıcı/cihaz kaynaklı olabileceğinden
// (ör. seri porttan gelen hata metni) XSS'e karşı hep DOM node olarak kurulur.
function makeConsoleSpan(className, text) {
  const span = document.createElement("span");
  span.className = className;
  span.textContent = text;
  return span;
}

function logToConsole(buttonName, task, result, isError) {
  const box = document.getElementById("console");
  const time = new Date().toLocaleTimeString("tr-TR");
  if (!box) {
    // Operatör sayfası gibi konsol paneli olmayan sayfalarda: yine de bir
    // iz bıraksın diye tarayıcı devtools konsoluna yazılır.
    const logFn = isError ? console.error : console.log;
    logFn(`[${time}] ${buttonName} — Görev: ${task} — Sonuç: ${result}`);
    return;
  }
  const line = document.createElement("div");
  line.className = "line";
  line.appendChild(makeConsoleSpan("time", `[${time}]`));
  line.appendChild(document.createTextNode(" "));
  line.appendChild(makeConsoleSpan("button-name", buttonName));
  line.appendChild(document.createTextNode(" — "));
  line.appendChild(makeConsoleSpan("task", `Görev: ${task}`));
  line.appendChild(document.createTextNode(" — "));
  line.appendChild(makeConsoleSpan(isError ? "error" : "result", `Sonuç: ${result}`));
  box.appendChild(line);
  while (box.children.length > CONSOLE_MAX_LINES) {
    box.removeChild(box.firstChild);  // en eski satırları at
  }
  box.scrollTop = box.scrollHeight;  // en son satır her zaman görünsün
}

// ==========================================================================
//  CANLI DURUM WEBSOCKET — /ws/status'a bağlanır, her mesajı çağırana
//  (onMessage) iletir. Bağlantı koparsa 3sn sonra otomatik tekrar dener.
//  Sayfaya özgü render (encoder rakamları, ham JSON, grafikler vb.) çağıran
//  tarafın kendi onMessage callback'inde yapılır — bu fonksiyon SADECE
//  bağlantıyı kurar/canlı tutar.
// ==========================================================================

function startStatusWebSocket(onMessage) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/status`);
  ws.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    onMessage(data);
    renderLiveMotorViews(data);  // bağımsız panel satırları + hareket lambası + yardımcı motor etiketleri (ortak, elemanlar yoksa sessizce atlanır)
    renderRuleAlarm(data.rule_violations || [], data.rule_skipped || []);
  };
  ws.onclose = () => {
    setTimeout(() => startStatusWebSocket(onMessage), 3000);
  };
  return ws;
}

// ==========================================================================
//  KONTROL KRİTERLERİ ALARM BANNER + SESLİ ALARM (madde 1+2+7 görüntüsü, madde 9
//  sesi) — her iki sayfada da #rule-alarm-banner elementi bulunmalı.
//  #rules-skipped-info SADECE Admin'de var (Operatör'e teknik "okunamayan
//  kural" detayı gösterilmiyor, bkz. UI/UX planı) — yoksa sessizce atlanır.
// ==========================================================================

let alarmMuted = false;
let alarmAudioCtx = null;
let lastAlarmSignature = "";
let lastAlarmPlayedAt = 0;
const ALARM_REPEAT_MS = 8000;  // alarm devam ederken hatirlatma araligi

function getAlarmAudioCtx() {
  if (!alarmAudioCtx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    alarmAudioCtx = new Ctx();
  }
  return alarmAudioCtx;
}

function beep(frequency, durationMs, times) {
  const ctx = getAlarmAudioCtx();
  if (!ctx) return;
  let t = ctx.currentTime;
  for (let i = 0; i < times; i++) {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "square";
    osc.frequency.value = frequency;
    gain.gain.value = 0.2;
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(t);
    osc.stop(t + durationMs / 1000);
    t += (durationMs + 120) / 1000;
  }
}

// MP3 tabanlı alarm (15-09-2026, USB hoparlör altyapısı — donanım bugün
// yok, tarayıcı çalma testi yapılabilir). Her Kontrol Kriteri kendi
// alarm_sound'unu (ui/alarm_sounds/ içindeki dosya adı) seçebilir; hiçbiri
// seçilmemişse aşağıdaki Web Audio ton sistemine (kritik/uyarı bip) düşülür.
function playAlarmSoundFile(filename) {
  const audio = new Audio(`/alarm-sounds/${encodeURIComponent(filename)}`);
  audio.play().catch((e) => {
    // Otomatik oynatma engeli (tarayıcı henüz kullanıcı etkileşimi görmedi)
    // ya da dosya bulunamadı/bozuk — sessizce ton sistemine düş, hata
    // fırlatıp sayfayı bozma.
    console.warn(`Alarm MP3 çalınamadı (${filename}), ton sistemine düşülüyor:`, e.message);
    beep(880, 200, 3);
  });
}

function playAlarmSound(violations) {
  if (alarmMuted || violations.length === 0) {
    lastAlarmSignature = "";
    return;
  }
  const signature = violations.map((v) => `${v.rule_id}:${v.stop_motor}:${v.alarm_sound || ""}`).sort().join(",");
  const now = Date.now();
  if (signature === lastAlarmSignature && now - lastAlarmPlayedAt < ALARM_REPEAT_MS) return;
  lastAlarmSignature = signature;
  lastAlarmPlayedAt = now;
  // İlk (varsa) MP3 seçilmiş ihlali çal — birden fazla ihlal aynı anda
  // olsa bile üst üste binen sesler yerine tek bir alarm sesi tercih edilir.
  const withSound = violations.find((v) => v.alarm_sound);
  if (withSound) {
    playAlarmSoundFile(withSound.alarm_sound);
    return;
  }
  const critical = violations.some((v) => v.stop_motor);
  if (critical) beep(880, 200, 3);
  else beep(440, 300, 1);
}

// ==========================================================================
//  MOTOR DURDURAN İHLAL → KONSOL SATIRI (23-09-2026) — sahadan gelen kritik
//  bulgu: _rule_engine_loop() (main.py) bir stop_motor=true kriteri ihlal
//  bulup bridge.send_command({"cmd":"stop"}) çağırdığında, bu şu ana kadar
//  TAMAMEN SESSİZDİ (operatör/admin konsolunda hiçbir iz yok) — "Basinc"
//  kriteri kalibre edilmemiş bir ROI'den okuyup muhtemelen motoru sürekli
//  otomatik durduruyordu, hiç fark edilemedi.
//
//  _rule_engine_loop ihlal sürdüğü sürece HER 2 saniyede bir aynı "stop"
//  komutunu tekrar gönderiyor (bilinçli — interlock ısrarcı olsun diye) ve
//  /ws/status da saniyede 10 kez aynı rule_violations listesini akıtıyor;
//  bu yüzden burada sadece DURUM DEĞİŞİMİNDE (ihlal yeni başladı/bitti)
//  konsola satır düşülür — her mesajda tekrar basmak gürültü yaratır.
//  Kapsam kasıtlı olarak stop_motor=true kriterlerle sınırlı (motoru
//  gerçekten durduran ihlaller) — sadece banner'da görünen alarm-only
//  kriterler zaten canlı banner'da görünür, buraya girmez.
let activeStopViolations = new Map();  // rule_id -> en son bilinen ihlal nesnesi

function logRuleViolationTransitions(violations) {
  const current = new Map(violations.filter((v) => v.stop_motor).map((v) => [v.rule_id, v]));
  for (const [ruleId, v] of current) {
    if (!activeStopViolations.has(ruleId)) {
      const detail = v.error
        ? `⚠ Değer okunamadı (${v.error}) — motor durduruldu`
        : `⚠ Aralık dışı (${v.value}, izin verilen [${v.min ?? "-"}, ${v.max ?? "-"}]) — motor durduruldu`;
      logToConsole(v.rule_name, `Kontrol Kriteri (${v.source_label})`, detail, true);
    }
  }
  for (const [ruleId, v] of activeStopViolations) {
    if (!current.has(ruleId)) {
      logToConsole(v.rule_name, `Kontrol Kriteri (${v.source_label})`, "✓ Normale döndü — motor durdurma kalktı", false);
    }
  }
  activeStopViolations = current;
}

function renderRuleAlarm(violations, skipped) {
  const banner = document.getElementById("rule-alarm-banner");
  logRuleViolationTransitions(violations);
  playAlarmSound(violations);
  if (!banner) return;  // banner olmayan bir sayfada (yok bugun) sessizce atla
  if (violations.length === 0) {
    banner.style.display = "none";
  } else {
    // Kısa/anlaşılır: teknik detay (perspektif/kontur vb.) yok, sadece
    // hangi değerin aralık dışına çıktığı + motor durduruldu mu.
    const lines = violations.map((v) => {
      const stopNote = v.stop_motor ? " — MOTOR DURDURULDU" : "";
      if (v.error) {
        return `${v.rule_name} (${v.source_label}): DEĞER OKUNAMADI — ${v.error}${stopNote}`;
      }
      return `${v.rule_name} (${v.source_label}): ${v.value} — izin verilen [${v.min ?? "-"}, ${v.max ?? "-"}]${stopNote}`;
    });
    banner.textContent = "⚠ " + lines.join("  |  ");
    banner.style.display = "block";
  }
  const skippedEl = document.getElementById("rules-skipped-info");
  if (skippedEl) {
    skippedEl.textContent = skipped.length === 0
      ? "Okunamayan kural yok."
      : "Şu an okunamayan kurallar: " + skipped.map((s) => `${s.rule_name} (${s.reason})`).join(", ");
  }
}

// ==========================================================================
//  BESLEME BAŞLAT — son girilen hızın µs gecikme + hız kapasitesi %'sine
//  karşılığı (29-09-2026 eklendi). Hesap backend'de (motion_calc.py
//  speed_pct_of_max) yapılıyor, burada SADECE metne dökülüyor — Operatör ve
//  Admin ekranlarında aynı formatı kullanıyor, ayrı kopya yazmayalım diye
//  ortak. Sadece en son BAŞARILI /motor/feed-start çağrısından sonra
//  çağrılır (elementId'nin başlangıç metni boş kalır).
// ==========================================================================

function renderFeedSpeedInfo(elementId, speedMms, stepCalc) {
  const el = document.getElementById(elementId);
  if (!el) return;
  el.textContent = `Son girilen mm/s → ${speedMms} mm/s → ${stepCalc.delay_us} µs gecikmeli hareket → motorun hız kapasitesinin %${stepCalc.speed_pct_of_max.toFixed(1)}'i`;
}

// ==========================================================================
//  İVME TİK'İ — "İvme (mm/s²)" input'u varsayılan DISABLED (29-09-2026
//  eklendi, Fatih'in kararı: çoğu operatör rampasız/direkt hız kullanıyor,
//  ivme istisnai bir durum — kazayla dolu bir değer gönderilmesin diye
//  tik işaretlenmeden input pasif kalır). Operator ve Admin'de aynı
//  #feed-accel/#feed-accel-enable id'leri kullanılıyor, ortak.
// ==========================================================================

function toggleFeedAccelEnabled() {
  const enabled = document.getElementById("feed-accel-enable").checked;
  const input = document.getElementById("feed-accel");
  if (input) input.disabled = !enabled;
}

// ==========================================================================
//  6 MOTORLU SİSTEM (21-10-2026) — NEMA23 + Yardımcı çift + Rotasyon çifti + DC.
//  Besleme Başlat paneli, bağımsız Rotasyon/Yardımcı çift panelleri, hareket
//  durum lambası ve kanal bazlı hız doğrulaması Operatör ve Admin'de AYNI id'lerle
//  çalışıyor; ortak mantık ve ortak HTML burada (iki kopya zamanla sapmasın diye).
//  Fiziksel birim -> ham komut dönüşümü SUNUCUDA (motion_calc.py), burada sadece
//  alan toplama, doğrulama ve gösterim var. Birimler: NEMA23 mm/s, Yardımcı çift
//  mm/s (çubuk yüzey hızı), Rotasyon çifti çubuk RPM, DC çubuk RPM.
// ==========================================================================

const PAIR_DIR_LABELS = { 0: "Duruyor", 1: "Saat Yönü", 2: "Saat Yönünün Tersi" };  // durum satırı rot/assist/dc alanları
const FEED_PANEL_STORAGE_KEY = "feedvision_feed-panel-v2";
// Seçimleri ve motor hızlarını/yönlerini sayfa yenilenince hatırla (NEMA23 mesafe/ivme bilerek hatırlanmaz).
const FEED_PANEL_PERSISTED_IDS = [
  "feed-use-assist", "feed-use-rot", "feed-use-dc",
  "feed-assist-speed", "feed-rot-speed", "feed-dc-speed", "feed-rot-dir", "feed-dc-dir",
  "rot-rpm", "assist-speed",
];

// Hız kutusu id -> sunucudaki izinli aralık anahtarı (GET /motion-limits) + gösterim bilgisi.
const SPEED_FIELDS = {
  "feed-speed": { key: "step_mms", unit: "mm/s", name: "NEMA23 hızı" },
  "feed-distance": { key: "distance_mm", unit: "mm", name: "Mesafe" },
  "feed-assist-speed": { key: "assist_mms", unit: "mm/s", name: "Yardımcı çift hızı" },
  "feed-rot-speed": { key: "rot_rpm", unit: "RPM", name: "Rotasyon çifti hızı" },
  "feed-dc-speed": { key: "dc_rpm", unit: "RPM", name: "DC hızı" },
  "rot-rpm": { key: "rot_rpm", unit: "RPM", name: "Rotasyon çifti hızı" },
  "assist-speed": { key: "assist_mms", unit: "mm/s", name: "Yardımcı çift hızı" },
};
let motionLimits = null;  // GET /motion-limits sonucu; alınamazsa null (sunucu yine de doğrular)

function elById(id) { return document.getElementById(id); }

// Birim çevirisi yok: sadece okunaklı sayı (en çok 3 anlamlı ondalık).
function fmtNum(n) { return Number(n).toLocaleString("tr-TR", { maximumFractionDigits: 3 }); }

async function loadMotionLimits() {
  try {
    const r = await fetch("/motion-limits");
    if (!r.ok) return;
    motionLimits = (await r.json()).limits;
  } catch (e) {
    return;  // aralık bilgisi yoksa yalnızca "0'dan büyük" kontrolü yapılır, sunucu aralığı zaten doğrular
  }
  applyLimitsToInputs();
}

function applyLimitsToInputs() {
  if (!motionLimits) return;
  for (const [id, f] of Object.entries(SPEED_FIELDS)) {
    const lim = motionLimits[f.key];
    const input = elById(id);
    if (!lim || !input) continue;
    input.min = lim.min;
    input.max = lim.max;
    const hint = elById(id + "-hint");
    if (hint) hint.textContent = `izinli: ${fmtNum(lim.min)} – ${fmtNum(lim.max)} ${f.unit}`;
  }
}

// Bir hız kutusunu doğrular; hata varsa anlamlı Türkçe mesaj, yoksa null.
function validateSpeedField(id) {
  const f = SPEED_FIELDS[id];
  const input = elById(id);
  if (!f || !input) return null;
  const raw = input.value.trim();
  if (raw === "") return `${f.name} boş — bir değer girin.`;
  const v = Number(raw);
  if (!Number.isFinite(v)) return `${f.name} geçerli bir sayı değil.`;
  if (!(v > 0)) return `${f.name} 0'dan büyük olmalı.`;
  const lim = motionLimits && motionLimits[f.key];
  if (lim && (v < lim.min || v > lim.max)) {
    return `${f.name} ${fmtNum(v)} ${f.unit} izinli aralığın dışında (${fmtNum(lim.min)} – ${fmtNum(lim.max)} ${f.unit}).`;
  }
  return null;
}

// Sunucu hata gövdesindeki detail: metin (400/502) ya da liste (422) olabilir.
function formatApiDetail(detail, status) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((e) => `${(e.loc || []).slice(1).join(".") || "istek"}: ${e.msg}`).join("; ");
  }
  return `sunucu ${status}`;
}

// --------------------------------------------------------------------------
//  Ortak stil (iki sayfada da aynı görünsün; sayfaların kendi CSS'ine dokunmuyor)
// --------------------------------------------------------------------------
function injectSharedStyles() {
  if (elById("fv-shared-style")) return;
  const style = document.createElement("style");
  style.id = "fv-shared-style";
  style.textContent = `
    .fv-section-label { font-size: 12.5px; color: #8a8; margin: 6px 0 6px; letter-spacing: .02em; }
    .fv-hint { font-size: 11.5px; color: #888; align-self: center; }
    .fv-presets { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
    .fv-preset { min-height: 46px; padding: 8px 14px; border: 2px solid #3a3a3a; background: #262626; color: #ddd; font-size: 14px; border-radius: 10px; }
    .fv-preset.active { border-color: #2a5; background: #1d3a29; color: #fff; font-weight: 700; }
    .fv-motor-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px; margin-bottom: 12px; }
    .fv-card { background: #141414; border: 2px solid #2d2d2d; border-radius: 10px; padding: 10px 12px; }
    .fv-card.on { border-color: #2a5; }
    .fv-card-title { display: flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 700; color: #ddd; cursor: pointer; margin-bottom: 8px; }
    .fv-card-title input { width: 22px; height: 22px; }
    .fv-card-body { display: flex; flex-direction: column; gap: 6px; }
    .fv-card-body label { font-size: 12.5px; color: #aaa; }
    .fv-card-body input[type=number], .fv-card-body select { width: 100%; }
    .fv-card-body .fv-hint { align-self: flex-start; }
    .fv-card:not(.on) .fv-card-body { opacity: .38; pointer-events: none; }
    .fv-lamp-row { display: flex; align-items: center; gap: 14px; margin-top: 12px; padding: 10px 12px; background: #141414; border-radius: 10px; }
    .fv-lamp { width: 34px; height: 34px; border-radius: 50%; flex: none; background: #555; box-shadow: inset 0 0 6px #0008; }
    .fv-lamp-idle { background: #555; }
    .fv-lamp-starting { background: #e0a800; box-shadow: 0 0 10px #e0a80088; }
    .fv-lamp-stalled { background: #e03; box-shadow: 0 0 14px #e03a; }
    .fv-lamp-moving { background: #2ecc71; box-shadow: 0 0 14px #2ecc7188; animation: fv-lamp-pulse var(--fv-period, 1s) ease-in-out infinite; }
    @keyframes fv-lamp-pulse { 0%, 100% { opacity: .35; } 50% { opacity: var(--fv-peak, 1); } }
    #motion-lamp-title { font-size: 14px; font-weight: 700; color: #ddd; }
    #motion-lamp-diag { font-size: 13px; color: #aaa; margin-top: 2px; }
    #motion-lamp-diag.warn { color: #f90; }
    #motion-lamp-diag.bad { color: #f66; }
    .fv-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
    .fv-chip { font-size: 12.5px; padding: 4px 10px; border-radius: 12px; background: #222; color: #888; border: 1px solid #333; }
    .fv-chip.on { background: #1b2a3c; color: #9cf; border-color: #36a; }
    .fv-note { font-size: 11.5px; color: #777; margin-top: 6px; }
    .fv-pair-status { font-size: 13px; color: #aaa; margin-top: 6px; }
  `;
  document.head.appendChild(style);
}

// --------------------------------------------------------------------------
//  Besleme Başlat paneli (Operatör + Admin ortak HTML)
// --------------------------------------------------------------------------
const FEED_PRESETS = {
  nema23: { assist: false, rot: false, dc: false, label: "Sadece NEMA23" },
  nema23_assist: { assist: true, rot: false, dc: false, label: "NEMA23 + Yardımcı" },
  nema23_rot: { assist: false, rot: true, dc: false, label: "NEMA23 + Rotasyon" },
  nema23_assist_rot: { assist: true, rot: true, dc: false, label: "NEMA23 + Yardımcı + Rotasyon" },
  all: { assist: true, rot: true, dc: true, label: "Hepsi (DC dahil)" },
};

function feedPanelHtml() {
  const presetButtons = Object.entries(FEED_PRESETS)
    .map(([key, p]) => `<button type="button" class="fv-preset" data-preset="${key}" onclick="applyFeedPreset('${key}')">${p.label}</button>`)
    .join("");
  return `
    <h2>Besleme Başlat</h2>
    <div class="fv-section-label">NEMA23 (ana besleme) — her zaman çalışır; süreyi mesafe ve hız belirler</div>
    <div class="row-group">
      <label>Yön:</label>
      <select id="feed-step-dir"><option value="1">Geri</option><option value="0">İleri</option></select>
      <label>Hız (mm/s):</label>
      <input id="feed-speed" type="number" value="1" min="0.1" step="any">
      <span class="fv-hint" id="feed-speed-hint"></span>
      <label>Mesafe (mm):</label>
      <input id="feed-distance" type="number" value="50" min="0.1" step="any">
      <span class="fv-hint" id="feed-distance-hint"></span>
      <label>İvme (mm/s²):</label>
      <input id="feed-accel" type="number" value="0" min="0" step="0.1" disabled>
      <label style="display:flex;align-items:center;gap:4px;">
        <input type="checkbox" id="feed-accel-enable" onchange="toggleFeedAccelEnabled()">
        İvme kullan
      </label>
    </div>
    <div class="fv-section-label">Kısa yol — sadece aşağıdaki kutucukları işaretler, başlatmaz</div>
    <div class="fv-presets" id="feed-presets">${presetButtons}</div>
    <div class="fv-motor-grid">
      <div class="fv-card" id="feed-card-assist">
        <label class="fv-card-title"><input type="checkbox" id="feed-use-assist" onchange="onFeedSelectionChange()">Yardımcı çift (2× NEMA17)</label>
        <div class="fv-card-body">
          <label>Yön: NEMA23'ü izler (ileri = saat yönü; Admin'den ters çevrilebilir)</label>
          <label>Çubuk hızı (mm/s):</label>
          <input id="feed-assist-speed" type="number" value="10" min="0.1" step="any">
          <span class="fv-hint" id="feed-assist-speed-hint"></span>
        </div>
      </div>
      <div class="fv-card" id="feed-card-rot">
        <label class="fv-card-title"><input type="checkbox" id="feed-use-rot" onchange="onFeedSelectionChange()">Rotasyon çifti (2× NEMA17)</label>
        <div class="fv-card-body">
          <label>Yön:</label>
          <select id="feed-rot-dir"><option value="cw">Saat Yönü</option><option value="ccw">Saat Yönünün Tersi</option></select>
          <label>Çubuk hızı (RPM):</label>
          <input id="feed-rot-speed" type="number" value="10" min="0.1" step="any">
          <span class="fv-hint" id="feed-rot-speed-hint"></span>
        </div>
      </div>
      <div class="fv-card" id="feed-card-dc">
        <label class="fv-card-title"><input type="checkbox" id="feed-use-dc" onchange="onFeedSelectionChange()">DC motor</label>
        <div class="fv-card-body">
          <label>Yön:</label>
          <select id="feed-dc-dir"><option value="forward">Saat Yönü</option><option value="backward">Saat Yönünün Tersi</option></select>
          <label>Çubuk hızı (RPM):</label>
          <input id="feed-dc-speed" type="number" value="10" min="0.1" step="any">
          <span class="fv-hint" id="feed-dc-speed-hint"></span>
        </div>
      </div>
    </div>
    <div class="row-group">
      <button id="feed-start-btn" onclick="sendFeedStart()">Besleme Başlat</button>
      <button data-no-drag-scroll id="feed-start-stop-btn" onclick="stopAllMotors()">Dur</button>
    </div>
    <div id="feed-start-result"></div>
    <div class="fv-lamp-row">
      <div id="motion-lamp" class="fv-lamp fv-lamp-idle"></div>
      <div>
        <div id="motion-lamp-title">Çubuk hareketi (encoder)</div>
        <div id="motion-lamp-diag">Boşta.</div>
      </div>
    </div>
    <div class="fv-chips">
      <span class="fv-chip" id="chip-assist">Yardımcı çift: —</span>
      <span class="fv-chip" id="chip-rot">Rotasyon çifti: —</span>
      <span class="fv-chip" id="chip-dc">DC: —</span>
    </div>
    <div class="fv-note">Lamba yalnızca NEMA23/çubuk hareketini encoder'dan doğrular. Rotasyon, Yardımcı çift ve DC için encoder yok: yukarıdaki etiketler sadece kontrol kartına "komut gitti / çalışıyor" bilgisidir, hareketi doğrulamaz.</div>
  `;
}

function selectedFeedMotors() {
  return {
    assist: !!(elById("feed-use-assist") && elById("feed-use-assist").checked),
    rot: !!(elById("feed-use-rot") && elById("feed-use-rot").checked),
    dc: !!(elById("feed-use-dc") && elById("feed-use-dc").checked),
  };
}

// Kısa yol düğmesi: SADECE kutucukları işaretler, başlatmaz.
function applyFeedPreset(key) {
  const p = FEED_PRESETS[key];
  if (!p) return;
  elById("feed-use-assist").checked = p.assist;
  elById("feed-use-rot").checked = p.rot;
  elById("feed-use-dc").checked = p.dc;
  onFeedSelectionChange();
}

// Kutucuklar değişince: kart vurgusu, hangi kısa yolun aktif olduğu, kalıcı kayıt.
function onFeedSelectionChange() {
  const sel = selectedFeedMotors();
  for (const name of ["assist", "rot", "dc"]) {
    const card = elById("feed-card-" + name);
    if (card) card.classList.toggle("on", sel[name]);
  }
  document.querySelectorAll("#feed-presets .fv-preset").forEach((btn) => {
    const p = FEED_PRESETS[btn.dataset.preset];
    btn.classList.toggle("active", p.assist === sel.assist && p.rot === sel.rot && p.dc === sel.dc);
  });
  saveFeedPanelState();
}

function saveFeedPanelState() {
  try {
    const state = {};
    for (const id of FEED_PANEL_PERSISTED_IDS) {
      const el = elById(id);
      if (el) state[id] = el.type === "checkbox" ? el.checked : el.value;
    }
    localStorage.setItem(FEED_PANEL_STORAGE_KEY, JSON.stringify(state));
  } catch (e) {
    // localStorage kapalı/erişilemez (gizli mod vb.) — sadece bu oturumda geçerli.
  }
}

function restoreFeedPanelState() {
  let state = null;
  try {
    state = JSON.parse(localStorage.getItem(FEED_PANEL_STORAGE_KEY) || "null");
  } catch (e) {
    state = null;
  }
  if (!state || typeof state !== "object") return;
  for (const id of FEED_PANEL_PERSISTED_IDS) {
    const el = elById(id);
    if (!el || !(id in state)) continue;
    if (el.type === "checkbox") el.checked = state[id] === true;
    else if (typeof state[id] === "string") el.value = state[id];
  }
}

// --------------------------------------------------------------------------
//  Bağımsız Rotasyon çifti / Yardımcı çift panelleri
// --------------------------------------------------------------------------
const PAIR_KINDS = {
  rot: {
    title: "Rotasyon çifti (2× NEMA17)", speedId: "rot-rpm", speedLabel: "Çubuk hızı (RPM):", speedDefault: 10,
    endpoint: "/motor/rot", bodyField: "rpm", liveId: "rot-live-status", infoId: "rot-speed-info", calcKey: "rot_calc",
  },
  assist: {
    title: "Yardımcı çift (2× NEMA17)", speedId: "assist-speed", speedLabel: "Çubuk hızı (mm/s):", speedDefault: 10,
    endpoint: "/motor/assist", bodyField: "speed_mms", liveId: "assist-live-status", infoId: "assist-speed-info", calcKey: "assist_calc",
  },
};

function pairPanelHtml(kind) {
  const k = PAIR_KINDS[kind];
  return `
    <h2>${k.title}</h2>
    <div class="row-group">
      <label>${k.speedLabel}</label>
      <input id="${k.speedId}" type="number" value="${k.speedDefault}" min="0.1" step="any">
      <span class="fv-hint" id="${k.speedId}-hint"></span>
      <button onclick="sendAux('${kind}', 'cw', 'Saat Yönü')">Saat Yönü</button>
      <button onclick="sendAux('${kind}', 'ccw', 'Saat Yönünün Tersi')">Saat Yönünün Tersi</button>
      <button data-no-drag-scroll onclick="sendAux('${kind}', 'stop', 'Dur')">Dur</button>
    </div>
    <div id="${k.infoId}" style="font-size:12.5px;color:#888;margin-top:6px"></div>
    <div class="fv-pair-status">Durum: <span id="${k.liveId}">—</span></div>
  `;
}

// Sayfa açılışında çağrılır: ortak panelleri yerleştirir, son seçimi geri yükler, izinli aralıkları çeker.
function initControlPanels() {
  injectSharedStyles();
  const feed = elById("feed-start-panel");
  if (feed) feed.innerHTML = feedPanelHtml();
  for (const kind of Object.keys(PAIR_KINDS)) {
    const holder = elById(kind + "-panel");
    if (holder) holder.innerHTML = pairPanelHtml(kind);
  }
  if (feed) {
    restoreFeedPanelState();
    onFeedSelectionChange();
    for (const id of FEED_PANEL_PERSISTED_IDS) {
      const el = elById(id);
      if (el && el.type !== "checkbox") el.addEventListener("change", saveFeedPanelState);
    }
  }
  loadMotionLimits();
}

// Rotasyon/Yardımcı çift komutu (Saat Yönü / Saat Yönünün Tersi / Dur). Sonuç sayfaya özgü
// göstericiye gider (Admin: renderCmdReply, Operatör: logCmdResult). Sunucu reddederse
// ("Reddedildi: ...") konsola ve panelin bilgi satırına düşer.
function reportCmd(label, data) {
  if (typeof renderCmdReply === "function") renderCmdReply(label, data);
  else if (typeof logCmdResult === "function") logCmdResult(label, data);
}

async function sendAux(kind, dir, label) {
  const k = PAIR_KINDS[kind];
  const body = { dir };
  if (dir !== "stop") {
    const err = validateSpeedField(k.speedId);
    if (err) {
      logToConsole(label, `${k.title} komutu hazırla`, `${err} Gönderilmedi.`, true);
      const infoEl = elById(k.infoId);
      if (infoEl) infoEl.textContent = err;
      return null;
    }
    body[k.bodyField] = Number(elById(k.speedId).value);
  }
  let response;
  try {
    response = await fetch(k.endpoint, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    logToConsole(label, `POST ${k.endpoint}`, `Ağ hatası: ${e.message}`, true);
    return null;
  }
  let data;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const detail = formatApiDetail(data && data.detail, response.status);
    logToConsole(label, `POST ${k.endpoint}`, `Reddedildi: ${detail}`, true);
    const infoEl = elById(k.infoId);
    if (infoEl && dir !== "stop") infoEl.textContent = `Reddedildi: ${detail}`;
    return null;
  }
  const calc = data[k.calcKey];
  const infoEl = elById(k.infoId);
  if (calc && infoEl) {
    const unit = kind === "rot" ? "RPM" : "mm/s";
    infoEl.textContent = `Girilen ${body[k.bodyField]} ${unit} → ${calc.delay_us} µs gecikmeli darbe → hız kapasitesinin %${calc.speed_pct_of_max.toFixed(1)}'i`;
  }
  reportCmd(label, data);
  return data;
}

// Büyük kırmızı Dur: NEMA23 + Yardımcı çift + Rotasyon çifti + DC. Tek istek, sunucu
// dört kanalı bağımsız durdurur (biri hata verse diğerleri yine denenir).
async function stopAllMotors() {
  const statusEl = elById("stop-status");
  if (statusEl) statusEl.textContent = "Durduruluyor…";
  let data = null;
  try {
    const r = await fetch("/motor/stop-all", { method: "POST" });
    data = await r.json();
  } catch (e) {
    if (statusEl) statusEl.textContent = `Durdurma komutu gönderilemedi: ${e.message}`;
    logToConsole("DUR", "Tüm motorları durdur", `Ağ hatası: ${e.message}`, true);
    return;
  }
  const failed = Object.entries(data.results || {}).filter(([, res]) => !res.sent).map(([name]) => ({ step: "NEMA23", assist: "Yardımcı çift", rot: "Rotasyon çifti", dc: "DC" }[name]));
  const text = failed.length
    ? `Durdurma komutu şu motorlara GİTMEDİ: ${failed.join(", ")} — kontrol kartı bağlantısını kontrol edin.`
    : "Tüm motorlar durduruldu (NEMA23, Yardımcı çift, Rotasyon çifti, DC).";
  if (statusEl) statusEl.textContent = text;
  const resultEl = elById("feed-start-result");
  if (resultEl && typeof renderFeedStartResult === "function") renderFeedStartResult(text, failed.length > 0);
  logToConsole("DUR", "Tüm motorları durdur", text, failed.length > 0);
}

// --------------------------------------------------------------------------
//  Besleme Başlat gönderimi
// --------------------------------------------------------------------------
function renderFeedStartResult(text, isError) {
  const el = elById("feed-start-result");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("error", !!isError);
  el.classList.toggle("success", !isError);
}

async function sendFeedStart() {
  if (typeof lastStatus !== "undefined" && lastStatus && lastStatus.running) {
    renderFeedStartResult("Hareket bitmeden yeni besleme başlatılamaz. Hareketi durdurun veya bitmesini bekleyin.", true);
    return;
  }
  const sel = selectedFeedMotors();
  const errors = [];
  for (const id of ["feed-speed", "feed-distance"]) {
    const e = validateSpeedField(id);
    if (e) errors.push(e);
  }
  if (sel.assist) { const e = validateSpeedField("feed-assist-speed"); if (e) errors.push(e); }
  if (sel.rot) { const e = validateSpeedField("feed-rot-speed"); if (e) errors.push(e); }
  if (sel.dc) { const e = validateSpeedField("feed-dc-speed"); if (e) errors.push(e); }
  // İvme tik'i işaretli değilse input disabled olsa da değerini OKUMUYORUZ,
  // her zaman 0 gönderiyoruz (backend accel_mms2<=0 => rampasız yorumluyor).
  const accelOn = elById("feed-accel-enable").checked;
  const accel_mms2 = accelOn ? Number(elById("feed-accel").value) : 0;
  if (accelOn && !(accel_mms2 >= 0)) errors.push("İvme 0 ya da daha büyük olmalı.");
  if (errors.length) {
    renderFeedStartResult("Gönderilmedi:\n• " + errors.join("\n• "), true);
    return;
  }

  const dir = parseInt(elById("feed-step-dir").value);
  const speed_mms = Number(elById("feed-speed").value);
  const distance_mm = Number(elById("feed-distance").value);
  // Her motor KENDİ kutusundaki hızla gider (tek ortak RPM kutusu yok).
  const body = {
    dir, speed_mms, distance_mm, accel_mms2,
    use_assist: sel.assist, use_rot: sel.rot, use_dc: sel.dc,
    rot_dir: elById("feed-rot-dir").value, dc_dir: elById("feed-dc-dir").value,
  };
  if (sel.assist) body.assist_speed_mms = Number(elById("feed-assist-speed").value);
  if (sel.rot) body.rot_rpm = Number(elById("feed-rot-speed").value);
  if (sel.dc) body.dc_rpm = Number(elById("feed-dc-speed").value);

  let response;
  try {
    response = await fetch("/motor/feed-start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    renderFeedStartResult(`Ağ hatası: ${e.message}`, true);
    logToConsole("Besleme Başlat", "POST /motor/feed-start", `Ağ hatası: ${e.message}`, true);
    return;
  }
  let data;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const detail = formatApiDetail(data && data.detail, response.status);
    renderFeedStartResult(`Reddedildi: ${detail}`, true);
    logToConsole("Besleme Başlat", "POST /motor/feed-start", `Reddedildi: ${detail}`, true);
    return;
  }
  if (!data.success) {
    renderFeedStartResult(
      `NEMA23 başlatılamadı (${data.reason || data.stage}) — diğer motorlara hiç dokunulmadı.`, true);
    logToConsole("Besleme Başlat", "POST /motor/feed-start", "NEMA23 başarısız, diğer motorlar gönderilmedi.", true);
    return;
  }

  const sc = data.step_calc;
  const lines = [`Başlatıldı — NEMA23: ${sc.steps} adım, ${sc.delay_us}µs gecikme, ${sc.accel_steps} adım rampa`];
  const aux = [];
  if (data.assist_calc) aux.push(`Yardımcı çift: ${data.assist_calc.delay_us}µs (${data.assist_dir === "cw" ? "saat yönü" : "saat yönünün tersi"})`);
  if (data.rot_calc) aux.push(`Rotasyon çifti: ${data.rot_calc.delay_us}µs, tekerlek ${data.rot_calc.wheel_rpm.toFixed(1)} RPM (${body.rot_dir === "cw" ? "saat yönü" : "saat yönünün tersi"})`);
  if (data.dc_calc) aux.push(`DC: %${data.dc_calc.duty} güç (${body.dc_dir === "forward" ? "saat yönü" : "saat yönünün tersi"})`);
  if (aux.length) {
    lines.push(aux.join(" | "));
    lines.push(`NEMA23 bitince otomatik duracak: ${aux.map((a) => a.split(":")[0]).join(", ")}.`);
  } else {
    lines.push("Yalnızca NEMA23 çalışıyor.");
  }
  const warnings = data.warnings || [];
  if (warnings.length) lines.push("⚠ " + warnings.join("\n⚠ "));
  renderFeedStartResult(lines.join("\n"), warnings.length > 0);
  renderFeedSpeedInfo("feed-speed-info", speed_mms, sc);
  logToConsole("Besleme Başlat", "POST /motor/feed-start",
    `started=${(data.started || []).join("+")} steps=${sc.steps} delay_us=${sc.delay_us}` +
    (data.assist_calc ? ` assist_delay_us=${data.assist_calc.delay_us}` : "") +
    (data.rot_calc ? ` rot_delay_us=${data.rot_calc.delay_us}` : "") +
    (data.dc_calc ? ` duty=${data.dc_calc.duty}` : ""), warnings.length > 0);
  if (typeof onFeedStartSuccess === "function") onFeedStartSuccess(data);  // sayfaya özgü ek iş (Admin: hareket analizi grafiği)
}

// --------------------------------------------------------------------------
//  Canlı durum: bağımsız panellerin satırları, hareket lambası, yardımcı motor etiketleri
// --------------------------------------------------------------------------
function renderPairStatus(status, field, delayField, elementId) {
  const el = elById(elementId);
  if (!el) return;
  if (status[field] === undefined) {
    el.textContent = "—";  // firmware henüz bu alanı göndermiyor
    return;
  }
  const label = PAIR_DIR_LABELS[status[field]] ?? `bilinmeyen (${status[field]})`;
  el.textContent = status[field] ? `${label} — ${status[delayField]} µs` : label;
}

function renderPairStatuses(status) {
  if (!status) return;
  renderPairStatus(status, "rot", "rdelay", "rot-live-status");
  renderPairStatus(status, "rot", "rdelay", "s-rot");
  renderPairStatus(status, "assist", "adelay", "assist-live-status");
  renderPairStatus(status, "assist", "adelay", "s-assist");
}

function encoderMmPerCount(n) {
  // 600 PPR x4 kuadratür = 2400 sayım/tur; teker çapı Admin'de girilir (localStorage, varsayılan 40).
  let d = 40;
  try { d = parseFloat(localStorage.getItem("feedvision_wheel-enc" + n)) || 40; } catch (e) { /* varsayılan */ }
  return (Math.PI * d) / (600 * 4);
}

const LAMP_TITLES = {
  idle: "Boşta.",
  disconnected: "Kontrol kartı bağlı değil — komut gitmez.",
  cmd_not_sent: "Komut gitmedi (kod/bağlantı sorunu).",
  starting: "Komut gitti, hareket bekleniyor…",
  moving: "Encoder hareketi var.",
  moving_no_step: "Encoder hareketi var (NEMA23 komutu yok — diğer motorlar ya da elle).",
  stalled: "Komut gitti ama hareket yok — kablaj, sürücü veya mekanik kontrol edin.",
};

function renderMotionLamp(motion) {
  const lamp = elById("motion-lamp");
  if (!lamp) return;
  const m = motion || { state: "idle", diag: "idle" };
  lamp.className = `fv-lamp fv-lamp-${m.state}`;
  let text = LAMP_TITLES[m.diag] || LAMP_TITLES.idle;
  if (m.diag === "cmd_not_sent" && m.reason) text = `Komut gitmedi (kod/bağlantı): ${m.reason}.`;
  if (m.state === "moving") {
    // Çubuk hızı (mm/s): iki encoder'dan hızlı olan. Nabız hızı ve parlaklık ölçülen hıza
    // orantılı; hız aralığı çok geniş (~0,1 – 300 mm/s) olduğu için logaritmik ölçek
    // (yavaş hızlar da fark edilsin): 0 = en yavaş desteklenen hız, 1 = en hızlı.
    const v = Math.max(Math.abs(m.enc1_cps || 0) * encoderMmPerCount(1), Math.abs(m.enc2_cps || 0) * encoderMmPerCount(2));
    const lim = (motionLimits && motionLimits.step_mms) || { min: 0.1, max: 300 };
    const ratio = Math.min(Math.max(Math.log(Math.max(v, lim.min) / lim.min) / Math.log(lim.max / lim.min), 0), 1);
    lamp.style.setProperty("--fv-period", `${(1.4 - 1.05 * ratio).toFixed(2)}s`);  // hızlandıkça daha sık yanıp söner
    lamp.style.setProperty("--fv-peak", (0.5 + 0.5 * ratio).toFixed(2));  // hızlandıkça daha parlak
    text += ` ~${v.toFixed(1)} mm/s`;
  }
  const diagEl = elById("motion-lamp-diag");
  if (diagEl) {
    diagEl.textContent = text;
    diagEl.className = m.diag === "stalled" ? "bad" : (m.diag === "cmd_not_sent" || m.diag === "disconnected" ? "warn" : "");
  }
}

function renderAuxChips(status) {
  const s = status || {};
  const chips = [
    ["chip-assist", "Yardımcı çift", s.assist, s.adelay === undefined ? "" : ` — ${s.adelay} µs`],
    ["chip-rot", "Rotasyon çifti", s.rot, s.rdelay === undefined ? "" : ` — ${s.rdelay} µs`],
    ["chip-dc", "DC", s.dc, s.dcSpeed === undefined ? "" : ` — %${s.dcSpeed} güç`],
  ];
  for (const [id, name, value, extra] of chips) {
    const el = elById(id);
    if (!el) continue;
    if (value === undefined) {
      el.textContent = `${name}: —`;
      el.classList.remove("on");
    } else if (!value) {
      el.textContent = `${name}: duruyor`;
      el.classList.remove("on");
    } else {
      el.textContent = `${name}: komut aktif, ${PAIR_DIR_LABELS[value] ?? value}${extra}`;
      el.classList.add("on");
    }
  }
}

// Besleme Başlat sonrası: girilen NEMA23 hızı (renderFeedSpeedInfo ile aynı yerde, ortak çağrı).
function renderLiveMotorViews(data) {
  renderPairStatuses(data.status);
  renderMotionLamp(data.motion);
  renderAuxChips(data.status);
}
