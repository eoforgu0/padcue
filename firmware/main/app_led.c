#include "app_led.h"

#include "app_config.h"
#include "esp_log.h"
#include "led_strip.h"

static const char *TAG = "app_led";
static led_strip_handle_t s_strip;

void app_led_init(void)
{
    led_strip_config_t strip_cfg = {
        .strip_gpio_num = PADEMU_PIN_LED,
        .max_leds = 1,
        .led_model = LED_MODEL_WS2812,
        .color_component_format = LED_STRIP_COLOR_COMPONENT_FMT_GRB,
    };
    led_strip_rmt_config_t rmt_cfg = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000,
    };
    esp_err_t err = led_strip_new_rmt_device(&strip_cfg, &rmt_cfg, &s_strip);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "LED 初期化失敗: %s", esp_err_to_name(err));
        s_strip = NULL;
        return;
    }
    app_led_set(0, 0, 0);
}

void app_led_set(uint8_t r, uint8_t g, uint8_t b)
{
    if (!s_strip) {
        return;
    }
    led_strip_set_pixel(s_strip, 0, r, g, b);
    led_strip_refresh(s_strip);
}
