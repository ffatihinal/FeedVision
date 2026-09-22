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
      logToConsole(
        v.rule_name,
        `Kontrol Kriteri (${v.source_label})`,
        `⚠ Aralık dışı (${v.value}, izin verilen [${v.min ?? "-"}, ${v.max ?? "-"}]) — motor durduruldu`,
        true,
      );
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
