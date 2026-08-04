#include "app_discover.h"

#include <string.h>
#include <sys/socket.h>
#include <netinet/in.h>

#include "app_config.h"
#include "app_ctrl.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mdns.h"

static const char *TAG = "discover";

// 名前で呼べるようにする(padctl.local)。IP が変わっても名前は変わらないので、
// ルーターで固定 IP を設定する必要がなくなる。
static void start_mdns(void) {
    esp_err_t err = mdns_init();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "mDNS 初期化失敗: %s", esp_err_to_name(err));
        return;
    }
    mdns_hostname_set(APP_DISCOVER_HOSTNAME);
    mdns_instance_name_set("padctl");
    mdns_service_add(NULL, "_padctl", "_tcp", APP_CTRL_PORT, NULL, 0);
    ESP_LOGI(TAG, "名前で呼べます: %s.local", APP_DISCOVER_HOSTNAME);
}

static void discover_task(void *arg) {
    (void)arg;
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    char id[13];
    snprintf(id, sizeof(id), "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

    for (;;) {
        int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (sock < 0) {
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }
        int opt = 1;
        setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
        struct sockaddr_in addr = {
            .sin_family = AF_INET,
            .sin_port = htons(APP_DISCOVER_PORT),
            .sin_addr.s_addr = htonl(INADDR_ANY),
        };
        if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
            close(sock);
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }
        ESP_LOGI(TAG, "探索応答を開始(UDP %d)", APP_DISCOVER_PORT);

        char buf[64];
        char reply[192];
        for (;;) {
            struct sockaddr_in from;
            socklen_t flen = sizeof(from);
            int n = recvfrom(sock, buf, sizeof(buf) - 1, 0,
                             (struct sockaddr *)&from, &flen);
            if (n <= 0) break;
            buf[n] = '\0';
            if (strncmp(buf, APP_DISCOVER_PROBE, strlen(APP_DISCOVER_PROBE)) != 0) {
                continue;   // 自分宛の問い合わせでなければ黙る
            }
            int len = snprintf(reply, sizeof(reply),
                "{\"magic\":\"%s\",\"id\":\"%s\",\"fw\":\"%s\",\"port\":%d}",
                APP_DISCOVER_MAGIC, id, PADCTL_FW_VERSION, APP_CTRL_PORT);
            sendto(sock, reply, len, 0, (struct sockaddr *)&from, flen);
        }
        close(sock);
    }
}

esp_err_t app_discover_start(void) {
    start_mdns();
    if (xTaskCreatePinnedToCore(discover_task, "discover", 3072, NULL, 3, NULL, 0)
        != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}
