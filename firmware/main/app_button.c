#include "app_button.h"

#include "app_config.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define POLL_MS 20
#define LONG_PRESS_MS 1500

static app_button_cb_t s_on_short;
static app_button_cb_t s_on_long;

static void btn_task(void *arg)
{
    (void)arg;
    int held_ms = 0;
    bool long_fired = false;
    for (;;) {
        bool down = gpio_get_level(PADEMU_PIN_BUTTON) == 0;
        if (down) {
            held_ms += POLL_MS;
            if (held_ms >= LONG_PRESS_MS && !long_fired) {
                long_fired = true;  // 押しっぱなしで長押しは 1 回だけ発火
                if (s_on_long) {
                    s_on_long();
                }
            }
        } else {
            if (held_ms >= POLL_MS * 2 && !long_fired && s_on_short) {
                s_on_short();
            }
            held_ms = 0;
            long_fired = false;
        }
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    }
}

bool app_button_held_at_boot(void)
{
    // プルアップを有効にした直後は、線の容量を充電しきる前に 0 を読みうる。押して
    // いないのに押したと読むと、診断モードに入ってコントローラーとして接続しない。
    // 少し待ってから読み、間を空けてもう一度押されているときだけ押したとみなす
    vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    if (gpio_get_level(PADEMU_PIN_BUTTON) != 0) {
        return false;
    }
    vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    return gpio_get_level(PADEMU_PIN_BUTTON) == 0;
}

void app_button_init(app_button_cb_t on_short, app_button_cb_t on_long)
{
    s_on_short = on_short;
    s_on_long = on_long;
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << PADEMU_PIN_BUTTON,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&io);
    // スタックは 4096 バイトにする。長押しは記録を出しながら実行を停止して操作権を
    // 手放し、その経路で約 1100 バイト使う(記録の出力の _vfprintf_r だけで
    // 800 バイト。ELF を逆アセンブルして測った)。さらに、タスクの初期文脈と、
    // 割り込みが入ったときの文脈がこのスタックに積まれる。残る約 3000 バイトは
    // 内訳を測った値ではなく余裕である
    xTaskCreate(btn_task, "btn", 4096, NULL, 5, NULL);
}
