// app_ctrl: PC からの制御を受ける TCP サーバ(コア0)
//
// 仕様: docs/specs/comm-protocol.md(PC 側の対応実装は pctool/switchctl/proto.py)
// ワイヤ形式: len u16 | type u8 | (json_len u16 | JSON | blob) | crc32
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define APP_CTRL_PORT 5555

esp_err_t app_ctrl_start(void);

#ifdef __cplusplus
}
#endif
