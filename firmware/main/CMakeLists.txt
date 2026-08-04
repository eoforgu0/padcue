idf_component_register(
    SRCS "main.c" "app_led.c" "app_button.c" "app_wifi.c"
         "app_engine.c" "app_usb.c" "app_log.c" "app_state.c"
         "app_store.c" "app_ctrl.c" "app_discover.c"
    INCLUDE_DIRS "."
    REQUIRES driver nvs_flash esp_wifi padctl_core esp_timer
             json spiffs mbedtls app_update lwip esp_hw_support
)
