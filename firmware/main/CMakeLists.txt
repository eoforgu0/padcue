idf_component_register(
    SRCS "main.c" "app_led.c" "app_button.c" "app_wifi.c" "app_wifi_creds.c"
         "app_engine.c" "app_usb.c" "app_log.c" "app_state.c"
         "app_store.c" "app_ctrl.c" "app_discover.c" "app_provision.c"
         "app_owner.c"
    INCLUDE_DIRS "."
    REQUIRES esp_driver_gpio esp_driver_gptimer esp_driver_usb_serial_jtag
             nvs_flash esp_wifi pademu_core esp_timer spiffs mbedtls app_update
             lwip esp_hw_support
    # 前回異常終了した場所を HELLO で返すため(app_state.c)
    PRIV_REQUIRES espcoredump
)

# ESP32 固有の層。pademu_core と同じ厳しさで通す。-Werror にしてあるので、
# 警告を 1 つでも増やすとビルドが失敗する
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
