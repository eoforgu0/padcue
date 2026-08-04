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
    int held_ms = 0;
    bool long_fired = false;
    for (;;) {
        bool down = gpio_get_level(PADCTL_PIN_BUTTON) == 0;
        if (down) {
            held_ms += POLL_MS;
            if (held_ms >= LONG_PRESS_MS && !long_fired) {
                long_fired = true;  // 押しっぱなしで長押しは1回だけ発火
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

bool app_button_is_pressed(void)
{
    return gpio_get_level(PADCTL_PIN_BUTTON) == 0;
}

void app_button_init(app_button_cb_t on_short, app_button_cb_t on_long)
{
    s_on_short = on_short;
    s_on_long = on_long;
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << PADCTL_PIN_BUTTON,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    gpio_config(&io);
    xTaskCreate(btn_task, "btn", 2048, NULL, 5, NULL);
}
