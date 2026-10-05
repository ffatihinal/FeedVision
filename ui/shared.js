// FeedVision — Operatör ve Admin sayfaları arasında PAYLAŞILAN JS.
//
// Ne var burada: konsol log yardımcısı (logToConsole), canlı durum
// WebSocket bağlantısı (startStatusWebSocket) ve Kontrol Kriterleri alarm
// banner'ı + sesli alarm (renderRuleAlarm/playAlarmSound). Bu üçü hem
// Operatör hem Admin ekranında birebir aynı davranmalı — ayrı kopyalar
// zamanla birbirinden sapardı (15-09-2026, G grubu: Operatör/Admin
// ayrımı sırasında ortak mantık buraya çıkarıldı).
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
//  DÖNÜŞ MOTORU (NEMA17 / DC / İkisi birden) — 05-10-2026 eklendi.
//  Besleme Başlat paneli + bağımsız "Dönüş Motoru (NEMA17)" paneli Operatör
//  ve Admin'de aynı id'leri kullanıyor, ortak mantık burada. Fiziksel birim
//  -> ham komut dönüşümü SUNUCUDA (motion_calc.compute_rot_command), burada
//  sadece alan toplama/gösterim var.
// ==========================================================================

const ROT_LABELS = { 0: "Duruyor", 1: "Saat Yönü", 2: "Saat Yönünün Tersi" };
const FEED_ROT_MOTOR_STORAGE_KEY = "feedvision_feed-rot-motor";
const FEED_ROT_MOTOR_VALUES = ["nema17", "dc", "both"];
const FEED_ROT_MOTOR_DEFAULT = "nema17";

function loadFeedRotMotor() {
  try {
    const stored = localStorage.getItem(FEED_ROT_MOTOR_STORAGE_KEY);
    if (FEED_ROT_MOTOR_VALUES.includes(stored)) return stored;
  } catch (e) {
    // localStorage kapalı/erişilemez (gizli mod vb.) — varsayılana düş.
  }
  return FEED_ROT_MOTOR_DEFAULT;
}

// Seçime göre ilgili alanları gösterir/gizler + RPM etiketini ayarlar.
// "display: contents": span'ler .row-group flex düzenine doğrudan katılsın.
function updateFeedRotMotorFields() {
  const mode = document.getElementById("feed-rot-motor").value;
  const show = (id, visible) => {
    const el = document.getElementById(id);
    if (el) el.style.display = visible ? "contents" : "none";
  };
  show("feed-rot-nema-fields", mode === "nema17" || mode === "both");
  show("feed-rot-dc-fields", mode === "dc" || mode === "both");
  show("feed-rot-dc-pct-fields", mode === "both");
  const label = document.getElementById("feed-rpm-label");
  if (label) {
    label.textContent = mode === "dc" ? "DC Hız (RPM):"
      : mode === "both" ? "NEMA17 Çubuk Hızı (RPM):"
      : "Çubuk Hızı (RPM):";
  }
}

function onFeedRotMotorChange() {
  const mode = document.getElementById("feed-rot-motor").value;
  try {
    localStorage.setItem(FEED_ROT_MOTOR_STORAGE_KEY, mode);
  } catch (e) {
    // Kaydedilemezse sadece bu oturumda geçerli — sessizce geç.
  }
  updateFeedRotMotorFields();
}

// Sayfa açılışında çağrılır: son seçimi geri yükler, alanları ayarlar.
function initFeedRotMotorSelect() {
  const select = document.getElementById("feed-rot-motor");
  if (!select) return;
  select.value = loadFeedRotMotor();
  updateFeedRotMotorFields();
}

// Besleme Başlat gövdesine eklenecek dönüş motoru alanlarını toplar.
// { fields } ya da { error } döner. "dc" modunda yalnızca rot_motor:"dc"
// gönderilir (eski davranışla birebir aynı gövde + rot_motor).
function collectFeedRotFields() {
  const rot_motor = document.getElementById("feed-rot-motor").value;
  const fields = { rot_motor };
  if (rot_motor === "dc" || rot_motor === "both") {
    fields.dc_dir = document.getElementById("feed-dc-dir").value;
  }
  if (rot_motor === "nema17" || rot_motor === "both") {
    fields.rot_dir = document.getElementById("feed-rot-dir").value;
  }
  if (rot_motor === "both") {
    const pct = Number(document.getElementById("feed-dc-speed-pct").value);
    if (!(pct > 0 && pct <= 100)) {
      return { error: "Geçersiz değer — DC güç % 0'dan büyük, en fazla 100 olmalı, gönderilmedi." };
    }
    fields.dc_speed_pct = pct;
  }
  return { fields };
}

const ROT_DIR_LABELS = { cw: "saat yönü", ccw: "saat yönünün tersi" };

// NEMA17 başlatma komutunun reddedildiğini/yanıtsız kaldığını anlatır; sorun
// yoksa null. (Sunucu step ok ise success:true döner, dönüş motoru yanıtını
// ayrıca rot_result'ta iletir — operatör sessiz başarısızlık görmesin.)
function describeRotFailure(rotResult) {
  if (!rotResult) return null;
  if (!rotResult.sent) return "NEMA17 komutu gönderilemedi";
  if (rotResult.timed_out) return "NEMA17 komutu gönderildi ama yanıt gelmedi (timeout)";
  if (rotResult.reply && rotResult.reply.err) return `NEMA17 komutu reddedildi (${rotResult.reply.err})`;
  return null;
}

// Besleme Başlat başarı metni (dönüş motoru kısmı).
function formatFeedStartRotSummary(data, fields) {
  const parts = [];
  if (data.rot_calc) {
    parts.push(
      `NEMA17: ${data.rot_calc.delay_us}µs gecikme, tekerlek ${data.rot_calc.wheel_rpm.toFixed(1)} RPM ` +
      `(${ROT_DIR_LABELS[fields.rot_dir]})`
    );
  }
  if (data.dc_calc) {
    const dcDir = fields.dc_dir === "forward" ? "saat yönü" : "saat yönünün tersi";
    parts.push(`DC: %${data.dc_calc.duty} duty (${dcDir})`);
  }
  const used = fields.rot_motor === "nema17" ? "NEMA17"
    : fields.rot_motor === "both" ? "NEMA17 ve DC"
    : "DC";
  return `${parts.join(" | ")}. Step bitince ${used} otomatik duracak.`;
}

// Bağımsız NEMA17 paneli / feed-start sonrası: girilen çubuk RPM'inin µs
// gecikme + hız kapasitesi %'sine karşılığı (renderFeedSpeedInfo'nun rot'u).
function renderRotSpeedInfo(elementId, rpmRod, rotCalc) {
  const el = document.getElementById(elementId);
  if (!el) return;
  el.textContent = `Son girilen çubuk hızı → ${rpmRod} RPM → ${rotCalc.delay_us} µs gecikmeli darbe → NEMA17 hız kapasitesinin %${rotCalc.speed_pct_of_max.toFixed(1)}'i`;
}

// Canlı durum: periyodik durumdaki rot (0/1/2) + rdelay (µs). Firmware henüz
// bu alanları göndermiyorsa "—" gösterir.
function renderRotStatus(status, elementId) {
  const el = document.getElementById(elementId);
  if (!el) return;
  if (status.rot === undefined) {
    el.textContent = "—";
    return;
  }
  const label = ROT_LABELS[status.rot] ?? `bilinmeyen (${status.rot})`;
  el.textContent = status.rot ? `${label} — ${status.rdelay} µs` : label;
}

// Bağımsız NEMA17 komutu (Saat Yönü / Saat Yönünün Tersi / Dur). reportFn:
// sayfaya özgü sonuç gösterici (Operatör: logCmdResult, Admin: renderCmdReply)
// — ikisi de (etiket, data) alıyor. dir "stop" ise rpm okunmaz. Sunucu
// reddederse (ör. RPM aralık dışı) "Reddedildi: ..." konsola düşer.
async function sendRot(dir, label, reportFn) {
  const body = { dir };
  if (dir !== "stop") {
    const rpm = Number(document.getElementById("rot-rpm").value);
    if (!(rpm > 0)) {
      logToConsole(label, "NEMA17 komutu hazırla", "Geçersiz değer — RPM 0'dan büyük olmalı, gönderilmedi.", true);
      return null;
    }
    body.rpm = rpm;
  }
  let response;
  try {
    response = await fetch("/motor/rot", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) {
    logToConsole(label, "POST /motor/rot", `Ağ hatası: ${e.message}`, true);
    return null;
  }
  let data;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const detail = data?.detail ?? `sunucu ${response.status}`;
    logToConsole(label, "POST /motor/rot", `Reddedildi: ${detail}`, true);
    const infoEl = document.getElementById("rot-speed-info");
    if (infoEl && dir !== "stop") infoEl.textContent = `Reddedildi: ${detail}`;
    return null;
  }
  if (data.rot_calc) renderRotSpeedInfo("rot-speed-info", body.rpm, data.rot_calc);
  reportFn(label, data);
  return data;
}
