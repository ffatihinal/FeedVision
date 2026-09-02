/* ============================================================================
 * FeedVision Test Firmware — Core/Src/main.c'ye eklenecek kullanıcı kodları
 * Kart : NUCLEO-G031K8 (STM32G031K8T6)
 * ----------------------------------------------------------------------------
 * KULLANIM: Aşağıda 8 numaralı blok var. Her blok, main.c içindeki AYNI İSİMLİ
 *           "USER CODE BEGIN xxx" ve "USER CODE END xxx" satırlarının ARASINA
 *           yapıştırılacak. Blok isimlerini karıştırma.
 *           Bu dosyanın kendisi derlenmez, sadece kopyalama kaynağıdır.
 *
 * Not (02-09): Bu dosya `Core/Src/main.c`'nin (repodaki gerçek çalışan kod)
 *              güncel halini yansıtır — kod tabanı genelinde tanımlayıcılar
 *              (fonksiyon/değişken isimleri) İngilizce, sadece yorumlar
 *              Türkçe tutulur.
 * ============================================================================ */


/* ############################################################################
 * BLOK 1  →  main.c :  USER CODE BEGIN Includes
 * ######################################################################## */

#include <string.h>
#include <stdio.h>
#include <stdlib.h>


/* ############################################################################
 * BLOK 2  →  main.c :  USER CODE BEGIN PD        (Private Defines)
 * ######################################################################## */

/* --- Pin tanımları -------------------------------------------------------
 * CubeMX'te User Label yazdıysan bu tanımlar zaten üretilmiştir ve aşağıdaki
 * #ifndef blokları atlanır. Yazmadıysan bunlar devreye girer.               */
#ifndef STEP_Pin
  #define STEP_Pin            GPIO_PIN_15
  #define STEP_GPIO_Port      GPIOA
#endif
#ifndef DIR_Pin
  #define DIR_Pin             GPIO_PIN_1
  #define DIR_GPIO_Port       GPIOB
#endif
#ifndef DC_IA1_Pin
  #define DC_IA1_Pin          GPIO_PIN_8
  #define DC_IA1_GPIO_Port    GPIOB
#endif
#ifndef DC_IB1_Pin
  #define DC_IB1_Pin          GPIO_PIN_9
  #define DC_IB1_GPIO_Port    GPIOB
#endif

/* --- Step motor sınırları ------------------------------------------------
 * delay = iki darbe arasındaki toplam süre (mikrosaniye).
 * Alt sınır 20 us: DM556 datasheet'i 200 kHz'e (5 us periyot) kadar cevap
 * verdiğini söylüyor; biz 4 kat güvenlik payı bırakıp 50 kHz'de duruyoruz.
 * Üst sınır 60000 us: timer 1 MHz'de çalıştığı için yarım periyot 65535'i
 * (16 bit sayacın tavanı) geçemez.                                          */
#define STEP_MIN_DELAY_US     20U
#define STEP_MAX_DELAY_US     60000U

/* --- Encoder / mesafe hesabı sabitleri -----------------------------------
 * SAHADA DOĞRULA: ENC_PPR encoder etiketinden, WHEEL_DIAMETER_MM kumpasla.  */
#define ENC_PPR               600.0f     /* encoder etiketinde yazan darbe/tur */
#define ENC_EDGE_MULTIPLIER   4.0f       /* timer A ve B'nin 4 kenarını da sayar */
#define WHEEL_DIAMETER_MM     40.0f      /* VARSAYIM - gerçek tekerlekle güncelle */

/* --- PC'ye durum gönderme hızı ------------------------------------------- */
#define STATUS_PERIOD_MS      50U        /* 50 ms = saniyede 20 satır */

/* --- Seri porttan gelen komut satırı tamponu ----------------------------- */
#define RX_LINE_MAX            96


/* ############################################################################
 * BLOK 3  →  main.c :  USER CODE BEGIN PV        (Private Variables)
 * ######################################################################## */

/* --- Step darbe üreteci durumu ------------------------------------------
 * "volatile": bu değişkenleri hem ana döngü hem kesme (interrupt) değiştiriyor,
 * derleyiciye "değeri önbelleğe alma, her seferinde bellekten oku" diyoruz.  */
typedef struct {
  volatile uint32_t remaining;  /* atılmayı bekleyen darbe sayısı */
  volatile uint8_t  level;      /* STEP pini şu an 1 mi 0 mı */
  volatile uint8_t  running;    /* 1 = hareket sürüyor */
} step_generator_t;

static step_generator_t g_step = {0, 0, 0};

/* --- Encoder okuma durumu ------------------------------------------------
 * Timer'ın sayacı 16 bit (0..65535) ve dolunca başa dönüyor. Toplam mesafeyi
 * kaybetmemek için her okumada FARKI alıp 32 bitlik bir toplayıcıda biriktiriyoruz. */
typedef struct {
  TIM_HandleTypeDef *htim;
  uint16_t           last_raw;  /* en son okunan 16 bit sayaç değeri */
  int32_t            total;     /* başlangıçtan beri toplam sayım (işaretli) */
} encoder_t;

static encoder_t g_enc1;        /* TIM1 - motorlu tekerlek */
static encoder_t g_enc2;        /* TIM3 - boşta tekerlek */

/* --- DC motor durumu: 0=dur, 1=ileri, 2=geri ---------------------------- */
static uint8_t g_dc_state = 0;

/* --- Seri port alım tamponları ------------------------------------------ */
static uint8_t           g_rx_byte;                    /* kesmede tek tek gelen karakter */
static char               g_rx_line[RX_LINE_MAX];      /* birikmekte olan satır */
static volatile uint16_t g_rx_length = 0;
static char               g_command[RX_LINE_MAX];      /* tamamlanmış komut satırı */
static volatile uint8_t  g_command_ready = 0;

/* --- Durum gönderme zamanlayıcısı --------------------------------------- */
static uint32_t g_last_status_ms = 0;


/* ############################################################################
 * BLOK 4  →  main.c :  USER CODE BEGIN PFP       (Private Function Prototypes)
 * ######################################################################## */

/* --- Encoder fonksiyonları --- */
static void     encoder_init(encoder_t *e, TIM_HandleTypeDef *htim);   // encoder'ı sıfırdan başlatır (donanım sayacını çalıştırır)
static void     encoder_update(encoder_t *e);                          // sayacı okuyup toplam mesafeye ekler (periyodik çağrılır)
static void     encoder_reset(encoder_t *e);                           // sayacı ve toplamı 0'a resetler
static int32_t  encoder_to_micrometers(int32_t count);                 // ham sayımı mikrometreye (mm'nin 1000'de biri) çevirir

/* --- Step motor fonksiyonları --- */
static void     step_start(uint8_t dir, uint32_t count, uint32_t delay_us);  // step motoru başlatır: yön + kaç adım + ne hızda
static void     step_stop(void);                                              // step motoru anında durdurur

/* --- DC motor fonksiyonu --- */
static void     dc_set(uint8_t state);  // DC motoru ileri/geri/dur durumuna sokar

/* --- Seri port (UART) fonksiyonları --- */
static void     uart_send(const char *s);              // bir metni Mac'e (seri port üzerinden) gönderir
static void     send_status(void);                      // encoder/motor durumunu JSON olarak Mac'e yollar
static void     process_command(const char *line);      // Mac'ten gelen bir JSON komut satırını yorumlayıp uygular

/* --- JSON okuma yardımcıları (kütüphanesiz, elle yazılmış basit ayrıştırıcı) --- */
static int      json_read_int(const char *json, const char *key, int32_t *out);                 // JSON'dan bir sayı değeri çeker (örn: "delay":500)
static int      json_read_str(const char *json, const char *key, char *out, uint16_t out_size);  // JSON'dan bir metin değeri çeker (örn: "cmd":"step")


/* ############################################################################
 * BLOK 5  →  main.c :  USER CODE BEGIN 0         (fonksiyon gövdeleri)
 * ######################################################################## */

/* ==========================================================================
 *  ENCODER — sayaç okuma ve mesafeye çevirme
 * ========================================================================== */

static void encoder_init(encoder_t *e, TIM_HandleTypeDef *htim)
{
  e->htim = htim;
  HAL_TIM_Encoder_Start(htim, TIM_CHANNEL_ALL);   /* donanım saymaya başlasın */
  __HAL_TIM_SET_COUNTER(htim, 0);
  e->last_raw = 0;
  e->total    = 0;
}

/* Ana döngüde periyodik çağrılır. 16 bit sayacın taşmasını doğru şekilde işler. */
static void encoder_update(encoder_t *e)
{
  uint16_t raw = (uint16_t)__HAL_TIM_GET_COUNTER(e->htim);

  /* İki 16 bit sayının farkını int16_t olarak almak, sayaç 65535'ten 0'a
   * atladığında da doğru sonucu verir (ör: 5 - 65530 = 11, geriye değil ileriye). */
  int16_t diff = (int16_t)(raw - e->last_raw);

  e->last_raw  = raw;
  e->total    += (int32_t)diff;
}

static void encoder_reset(encoder_t *e)
{
  __HAL_TIM_SET_COUNTER(e->htim, 0);
  e->last_raw = 0;
  e->total    = 0;
}

/* --------------------------------------------------------------------------
 *  5 mm'lik mesafe nasıl hesaplanıyor  (asıl istenen mantık burada)
 * --------------------------------------------------------------------------
 *  1) Encoder bir turda kaç sayım üretir?
 *        counts_per_rev = ENC_PPR * 4 = 600 * 4 = 2400
 *     ("* 4" nereden geliyor: timer'ı "Encoder Mode TI1 and TI2" kurduk, yani
 *      A kanalının hem yükselen hem düşen kenarını, B kanalının da ikisini
 *      sayıyor. Böylece çözünürlük 4 katına çıkıyor.)
 *
 *  2) Tekerlek bir turda kaç mm yol alır?
 *        circumference_mm = pi * WHEEL_DIAMETER_MM = 3.14159 * 40.0 = 125.664 mm
 *
 *  3) Bir sayım kaç mm eder?
 *        mm_per_count = 125.664 / 2400 = 0.05236 mm  (yani 52.36 mikrometre)
 *
 *  4) 5 mm ilerlemek için kaç sayım gerekir?
 *        5.0 / 0.05236 = 95.5 count
 *     Yani "5 mm ilerledi" demek icin encoder sayacinin ~96 artmasi lazim.
 *     Bu, 5 mm'lik bir olcumu +/- 0.05 mm hassasiyetle gorebiliyoruz demek —
 *     bu uygulama icin fazlasiyla yeterli.
 *
 *  5) Patinaj (kayma) tespiti:
 *        Encoder 1 = motorlu tekerlek, Encoder 2 = bosta klavuz tekerlek.
 *        Ikisi ayni cubugu olcuyor. Aralarindaki mm farki = kayan miktar.
 *        Fark buyurse cubuk motorun altinda patinaj yapiyor demektir.
 *
 *  NOT: WHEEL_DIAMETER_MM su an 40.0 mm VARSAYIM. Gercek tekerlek gelince
 *       olcup degistir, yoksa buradaki tum mm degerleri orantili sekilde
 *       yanlis olur.
 * -------------------------------------------------------------------------- */

/* Sayımı mikrometreye çevirir.
 * Neden mm değil mikrometre: CubeIDE varsayılan ayarında printf ondalık sayı
 * basamıyor (%f çöp üretir). Tam sayı gönderip PC'de 1000'e bölüyoruz. */
static int32_t encoder_to_micrometers(int32_t count)
{
  const float counts_per_rev   = ENC_PPR * ENC_EDGE_MULTIPLIER;         /* 2400 */
  const float circumference_um = 3.14159265f * WHEEL_DIAMETER_MM * 1000.0f; /* 125663.7 um */
  const float um_per_count     = circumference_um / counts_per_rev;     /* 52.36 um */

  return (int32_t)((float)count * um_per_count);
}


/* ==========================================================================
 *  STEP MOTOR — TIM16 kesmesi ile sabit hızda darbe üretimi
 * ========================================================================== */

/* dir      : 0 veya 1 (DIR pininin seviyesi)
 * count    : atılacak tam darbe sayısı
 * delay_us : iki darbe arası toplam süre (mikrosaniye) - küçük değer = hızlı */
static void step_start(uint8_t dir, uint32_t count, uint32_t delay_us)
{
  if (count == 0U) {
    return;
  }

  /* Kullanıcı saçma bir hız girerse sınırların içine çek */
  if (delay_us < STEP_MIN_DELAY_US) delay_us = STEP_MIN_DELAY_US;
  if (delay_us > STEP_MAX_DELAY_US) delay_us = STEP_MAX_DELAY_US;

  step_stop();   /* önceki hareket varsa temizle */

  /* Yön pinini darbelerden ÖNCE ayarla, sürücünün okuması için 1 ms bekle */
  HAL_GPIO_WritePin(DIR_GPIO_Port, DIR_Pin, dir ? GPIO_PIN_SET : GPIO_PIN_RESET);
  HAL_Delay(1);

  HAL_GPIO_WritePin(STEP_GPIO_Port, STEP_Pin, GPIO_PIN_RESET);
  g_step.level     = 0;
  g_step.remaining = count;
  g_step.running   = 1;

  /* Timer 1 MHz'de sayıyor (CubeMX'te Prescaler=63 ayarladık), yani 1 tık = 1 us.
   * Kesme her YARIM periyotta bir gelecek: bir kesmede pini kaldır, sonrakinde indir.
   * Böylece tam bir darbe delay_us kadar sürer. */
  __HAL_TIM_SET_AUTORELOAD(&htim16, (delay_us / 2U) - 1U);
  __HAL_TIM_SET_COUNTER(&htim16, 0);
  __HAL_TIM_CLEAR_FLAG(&htim16, TIM_FLAG_UPDATE);  /* bekleyen eski bayrağı sil */

  HAL_TIM_Base_Start_IT(&htim16);
}

static void step_stop(void)
{
  HAL_TIM_Base_Stop_IT(&htim16);
  g_step.running   = 0;
  g_step.remaining = 0;
  g_step.level     = 0;
  HAL_GPIO_WritePin(STEP_GPIO_Port, STEP_Pin, GPIO_PIN_RESET);
}


/* ==========================================================================
 *  DC MOTOR — L9110, sadece yön ve dur (hız kontrolü yok)
 * ========================================================================== */

/* L9110'da ayrı bir "enable" pini yok:
 *   IA1=1, IB1=0 -> forward (ileri)
 *   IA1=0, IB1=1 -> backward (geri)
 *   IA1=0, IB1=0 -> stop (dur)
 * (İkisini birden 1 yapmak yasak - sürücüyü kısa devre eder.) */
static void dc_set(uint8_t state)
{
  switch (state) {
    case 1: /* forward */
      HAL_GPIO_WritePin(DC_IA1_GPIO_Port, DC_IA1_Pin, GPIO_PIN_SET);
      HAL_GPIO_WritePin(DC_IB1_GPIO_Port, DC_IB1_Pin, GPIO_PIN_RESET);
      break;
    case 2: /* backward */
      HAL_GPIO_WritePin(DC_IA1_GPIO_Port, DC_IA1_Pin, GPIO_PIN_RESET);
      HAL_GPIO_WritePin(DC_IB1_GPIO_Port, DC_IB1_Pin, GPIO_PIN_SET);
      break;
    default: /* stop */
      HAL_GPIO_WritePin(DC_IA1_GPIO_Port, DC_IA1_Pin, GPIO_PIN_RESET);
      HAL_GPIO_WritePin(DC_IB1_GPIO_Port, DC_IB1_Pin, GPIO_PIN_RESET);
      state = 0;
      break;
  }
  g_dc_state = state;
}


/* ==========================================================================
 *  JSON — kütüphanesiz, sadece ihtiyacımız olan kadar basit ayrıştırıcı
 * ========================================================================== */

/* {"delay":500} içinden 500'ü çeker. Bulursa 1, bulamazsa 0 döner. */
static int json_read_int(const char *json, const char *key, int32_t *out)
{
  char pattern[24];
  const char *p;

  snprintf(pattern, sizeof(pattern), "\"%s\"", key);
  p = strstr(json, pattern);
  if (p == NULL) return 0;

  p = strchr(p, ':');
  if (p == NULL) return 0;

  *out = (int32_t)strtol(p + 1, NULL, 10);
  return 1;
}

/* {"cmd":"step"} içinden step'i çeker. */
static int json_read_str(const char *json, const char *key, char *out, uint16_t out_size)
{
  char pattern[24];
  const char *p;
  uint16_t i = 0;

  snprintf(pattern, sizeof(pattern), "\"%s\"", key);
  p = strstr(json, pattern);
  if (p == NULL) return 0;

  p = strchr(p + strlen(pattern), ':');
  if (p == NULL) return 0;

  p = strchr(p, '"');          /* değerin açılış tırnağı */
  if (p == NULL) return 0;
  p++;

  while (*p != '\0' && *p != '"' && i < (out_size - 1U)) {
    out[i++] = *p++;
  }
  out[i] = '\0';
  return 1;
}


/* ==========================================================================
 *  SERİ PORT — gönderme ve komut işleme
 * ========================================================================== */

static void uart_send(const char *s)
{
  HAL_UART_Transmit(&huart2, (uint8_t *)s, (uint16_t)strlen(s), 100);
}

static void send_status(void)
{
  char buf[160];

  snprintf(buf, sizeof(buf),
           "{\"t\":%lu,\"e1\":%ld,\"e2\":%ld,\"um1\":%ld,\"um2\":%ld,"
           "\"remaining\":%lu,\"running\":%u,\"dc\":%u}\r\n",
           (unsigned long)HAL_GetTick(),
           (long)g_enc1.total,
           (long)g_enc2.total,
           (long)encoder_to_micrometers(g_enc1.total),
           (long)encoder_to_micrometers(g_enc2.total),
           (unsigned long)g_step.remaining,
           (unsigned)g_step.running,
           (unsigned)g_dc_state);

  uart_send(buf);
}

static void process_command(const char *line)
{
  char    cmd[16];
  char    dir_str[12];
  int32_t dir_i = 0, delay_i = 500, steps_i = 0;

  if (!json_read_str(line, "cmd", cmd, sizeof(cmd))) {
    uart_send("{\"err\":\"missing cmd field\"}\r\n");
    return;
  }

  /* ---- Step motoru hareket ettir ---- */
  if (strcmp(cmd, "step") == 0) {
    json_read_int(line, "dir",   &dir_i);
    json_read_int(line, "delay", &delay_i);
    json_read_int(line, "steps", &steps_i);

    if (steps_i <= 0) {
      uart_send("{\"err\":\"steps must be > 0\"}\r\n");
      return;
    }
    step_start((uint8_t)(dir_i ? 1 : 0), (uint32_t)steps_i, (uint32_t)delay_i);
    uart_send("{\"ok\":\"step\"}\r\n");
  }

  /* ---- Step motoru anında durdur ---- */
  else if (strcmp(cmd, "stop") == 0) {
    step_stop();
    uart_send("{\"ok\":\"stop\"}\r\n");
  }

  /* ---- DC motor ---- */
  else if (strcmp(cmd, "dc") == 0) {
    if (!json_read_str(line, "dir", dir_str, sizeof(dir_str))) {
      uart_send("{\"err\":\"missing dir field\"}\r\n");
      return;
    }
    if      (strcmp(dir_str, "forward")  == 0) dc_set(1);
    else if (strcmp(dir_str, "backward") == 0) dc_set(2);
    else                                        dc_set(0);
    uart_send("{\"ok\":\"dc\"}\r\n");
  }

  /* ---- Encoder sayaçlarını sıfırla ---- */
  else if (strcmp(cmd, "reset") == 0) {
    encoder_reset(&g_enc1);
    encoder_reset(&g_enc2);
    uart_send("{\"ok\":\"reset\"}\r\n");
  }

  /* ---- Bağlantı testi ---- */
  else if (strcmp(cmd, "ping") == 0) {
    uart_send("{\"ok\":\"pong\"}\r\n");
  }

  else {
    uart_send("{\"err\":\"unknown cmd\"}\r\n");
  }
}


/* ############################################################################
 * BLOK 6  →  main.c :  USER CODE BEGIN 2
 *            (MX_..._Init() çağrılarından SONRA, while(1) döngüsünden ÖNCE)
 * ######################################################################## */

  /* Motor çıkışlarını güvenli başlangıç durumuna al */
  HAL_GPIO_WritePin(STEP_GPIO_Port,   STEP_Pin,   GPIO_PIN_RESET);
  HAL_GPIO_WritePin(DIR_GPIO_Port,    DIR_Pin,    GPIO_PIN_RESET);
  HAL_GPIO_WritePin(DC_IA1_GPIO_Port, DC_IA1_Pin, GPIO_PIN_RESET);
  HAL_GPIO_WritePin(DC_IB1_GPIO_Port, DC_IB1_Pin, GPIO_PIN_RESET);

  /* İki encoder'ın donanım sayacını başlat */
  encoder_init(&g_enc1, &htim1);   /* Encoder 1 - motorlu tekerlek  (PA8 / PA9) */
  encoder_init(&g_enc2, &htim3);   /* Encoder 2 - boşta tekerlek    (PA6 / PA7) */

  /* Seri porttan ilk karakteri beklemeye başla (bundan sonrası kesmede zincirlenir) */
  HAL_UART_Receive_IT(&huart2, &g_rx_byte, 1);

  uart_send("{\"ok\":\"feedvision test firmware ready\"}\r\n");

  g_last_status_ms = HAL_GetTick();


/* ############################################################################
 * BLOK 7  →  main.c :  USER CODE BEGIN 3
 *            (while(1) döngüsünün İÇİ)
 * ######################################################################## */

    /* --- 1) PC'den tam bir komut satırı geldiyse işle --- */
    if (g_command_ready) {
      char copy[RX_LINE_MAX];
      strncpy(copy, g_command, sizeof(copy) - 1U);
      copy[sizeof(copy) - 1U] = '\0';
      g_command_ready = 0;              /* kopyaladıktan SONRA temizle */
      process_command(copy);
    }

    /* --- 2) Her 50 ms'de bir encoder'ları oku ve durumu PC'ye gönder --- */
    if ((HAL_GetTick() - g_last_status_ms) >= STATUS_PERIOD_MS) {
      g_last_status_ms = HAL_GetTick();

      encoder_update(&g_enc1);
      encoder_update(&g_enc2);
      send_status();

      /* Kart üstündeki LED yanıp sönüyorsa firmware yaşıyor demektir */
#ifdef LD3_GPIO_Port
      HAL_GPIO_TogglePin(LD3_GPIO_Port, LD3_Pin);
#endif
    }


/* ############################################################################
 * BLOK 8  →  main.c :  USER CODE BEGIN 4
 *            (dosyanın sonu - kesme geri çağrı fonksiyonları)
 * ######################################################################## */

/* --------------------------------------------------------------------------
 * TIM16 kesmesi: step darbesini üreten yer.
 * Her çağrılışında pini bir kez değiştirir. İki çağrı = bir tam darbe.
 * Burada uzun iş yapılmaz (printf, HAL_Delay vb. YASAK) - zamanlama bozulur.
 * -------------------------------------------------------------------------- */
void HAL_TIM_PeriodElapsedCallback(TIM_HandleTypeDef *htim)
{
  if (htim->Instance != TIM16) {
    return;
  }

  if (!g_step.running) {
    HAL_TIM_Base_Stop_IT(&htim16);
    return;
  }

  if (g_step.level == 0U) {
    /* Darbenin yükselen kenarı - sürücü adımı burada atar */
    HAL_GPIO_WritePin(STEP_GPIO_Port, STEP_Pin, GPIO_PIN_SET);
    g_step.level = 1U;
  } else {
    /* Darbenin düşen kenarı - bir darbe tamamlandı */
    HAL_GPIO_WritePin(STEP_GPIO_Port, STEP_Pin, GPIO_PIN_RESET);
    g_step.level = 0U;

    if (g_step.remaining > 0U) {
      g_step.remaining--;
    }
    if (g_step.remaining == 0U) {
      g_step.running = 0U;
      HAL_TIM_Base_Stop_IT(&htim16);
    }
  }
}

/* --------------------------------------------------------------------------
 * USART2 kesmesi: PC'den gelen karakterleri satır olacak şekilde biriktirir.
 * -------------------------------------------------------------------------- */
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
  if (huart->Instance != USART2) {
    return;
  }

  char c = (char)g_rx_byte;

  if (c == '\n' || c == '\r') {
    /* Satır bitti - ana döngü önceki komutu henüz almadıysa bunu atla */
    if ((g_rx_length > 0U) && (g_command_ready == 0U)) {
      memcpy(g_command, g_rx_line, g_rx_length);
      g_command[g_rx_length] = '\0';
      g_command_ready = 1U;
    }
    g_rx_length = 0U;
  } else if (g_rx_length < (RX_LINE_MAX - 1U)) {
    g_rx_line[g_rx_length++] = c;
  } else {
    g_rx_length = 0U;   /* satır çok uzun - baştan başla */
  }

  /* Bir sonraki karakteri beklemeye devam et */
  HAL_UART_Receive_IT(&huart2, &g_rx_byte, 1);
}

/* --------------------------------------------------------------------------
 * USART2 hata kesmesi.
 * Bu fonksiyon OLMAZSA: seri portta tek bir "overrun" hatası olduğunda HAL
 * dinlemeyi tamamen bırakır ve kart bir daha hiçbir komut duymaz. Burada
 * dinlemeyi yeniden başlatarak bunu engelliyoruz.
 * -------------------------------------------------------------------------- */
void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)
{
  if (huart->Instance != USART2) {
    return;
  }

  __HAL_UART_CLEAR_OREFLAG(huart);
  __HAL_UART_CLEAR_NEFLAG(huart);
  __HAL_UART_CLEAR_FEFLAG(huart);
  __HAL_UART_CLEAR_PEFLAG(huart);

  g_rx_length = 0U;
  HAL_UART_Receive_IT(&huart2, &g_rx_byte, 1);
}
