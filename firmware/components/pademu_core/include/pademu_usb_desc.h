// pademu_usb_desc: USB のディスクリプターの定数(Pro コントローラー方式)
//
// 出典と原則: docs/design/procon-protocol.md §1。レポートディスクリプターは
// 実機実測のバイト列をそのまま複製する(再構成しない)。コンフィグディスクリプターの
// wDescriptorLength は必ず PADEMU_PROCON_HID_DESC_LEN から生成し、
// 手打ちの二重管理をしないこと。
#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PADEMU_PROCON_VID 0x057E
#define PADEMU_PROCON_PID 0x2009
#define PADEMU_PROCON_HID_DESC_LEN 203

extern const uint8_t pademu_procon_hid_report_desc[PADEMU_PROCON_HID_DESC_LEN];

#ifdef __cplusplus
}
#endif
