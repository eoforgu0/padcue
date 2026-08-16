idf_component_register(
    SRCS "main.c" "app_led.c" "app_button.c" "app_wifi.c"
         "app_engine.c" "app_usb.c" "app_log.c" "app_state.c"
         "app_store.c" "app_ctrl.c" "app_discover.c"
    INCLUDE_DIRS "."
    REQUIRES driver nvs_flash esp_wifi pademu_core esp_timer
             json spiffs mbedtls app_update lwip esp_hw_support
)

# ESP32 固有の層。pademu_core と同じ厳しさで通す。-Werror にしてあるので、
# 警告を1つでも増やすとビルドが止まる
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
