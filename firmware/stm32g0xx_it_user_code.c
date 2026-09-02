/* ============================================================================
 * FeedVision Test Firmware — Core/Src/stm32g0xx_it.c
 * ----------------------------------------------------------------------------
 * KISA CEVAP: Bu dosyaya ELLE EKLENECEK BİR ŞEY YOK.
 *
 * Neden: CubeMX'te "TIM16 global interrupt" ve "USART2 global interrupt"
 * kutularını işaretlediğinde, gerekli kesme fonksiyonlarını CubeMX zaten
 * stm32g0xx_it.c içine kendisi yazıyor. O fonksiyonlar HAL'a haber veriyor,
 * HAL da main.c'ye yapıştırdığın geri çağrı (callback) fonksiyonlarını
 * çağırıyor. Zincir şöyle işliyor:
 *
 *     donanım kesmesi
 *       -> TIM16_IRQHandler()            (stm32g0xx_it.c - CubeMX yazdı)
 *          -> HAL_TIM_IRQHandler()       (HAL kütüphanesi)
 *             -> HAL_TIM_PeriodElapsedCallback()   (main.c BLOK 8 - senin kodun)
 *
 *     donanım kesmesi
 *       -> USART2_IRQHandler()           (stm32g0xx_it.c - CubeMX yazdı)
 *          -> HAL_UART_IRQHandler()      (HAL kütüphanesi)
 *             -> HAL_UART_RxCpltCallback()  /  HAL_UART_ErrorCallback()
 *                                        (main.c BLOK 8 - senin kodun)
 *
 * Yani senin kodun main.c'de kalıyor, bu dosya sadece "postacı".
 * ============================================================================ */


/* ============================================================================
 * KONTROL LİSTESİ — kod üretildikten sonra stm32g0xx_it.c'yi açıp bak.
 * Aşağıdaki İKİ fonksiyon dosyada VAR MI? Yoksa CubeMX'te ilgili NVIC kutusunu
 * işaretlemeyi unutmuşsundur; geri dön, işaretle, kodu yeniden ürettir.
 * ============================================================================ */

/* ---- 1) Bu fonksiyon dosyada olmalı ---------------------------------------
 * (TIM16 → Step darbesi üreteci. Yoksa motor hiç dönmez.)

void TIM16_IRQHandler(void)
{
  HAL_TIM_IRQHandler(&htim16);
}

 * ------------------------------------------------------------------------- */


/* ---- 2) Bu fonksiyon dosyada olmalı ---------------------------------------
 * (USART2 → PC'den komut alma. Yoksa kart veri gönderir ama komut duymaz.)

void USART2_IRQHandler(void)
{
  HAL_UART_IRQHandler(&huart2);
}

 * ------------------------------------------------------------------------- */


/* ============================================================================
 * OLMAMASI GEREKENLER
 * ============================================================================
 *  - TIM1 veya TIM3 için IRQHandler OLMAMALI. Encoder'ları kesmeyle değil,
 *    ana döngüde sayaç okuyarak takip ediyoruz. CubeMX'te bu iki timer'ın
 *    NVIC kutularını işaretlersen, encoder her darbede boş yere kesme üretip
 *    step darbelerinin zamanlamasını bozabilir.
 * ============================================================================ */


/* ============================================================================
 * SORUN GİDERME (opsiyonel)
 * ----------------------------------------------------------------------------
 * Kart açılışta donuyorsa / LED hiç yanmıyorsa, aşağıdaki bloğu
 * stm32g0xx_it.c içindeki HardFault_Handler'ın "USER CODE BEGIN
 * HardFault_IRQn 0" kısmına yapıştır: LED'i hızlı hızlı yakıp söndürür,
 * böylece "çöktü mü, hiç başlamadı mı" ayrımını gözle yapabilirsin.

    while (1)
    {
      HAL_GPIO_TogglePin(LD3_GPIO_Port, LD3_Pin);
      for (volatile uint32_t i = 0; i < 200000; i++) { }
    }

 * ============================================================================ */
