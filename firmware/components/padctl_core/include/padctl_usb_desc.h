// USB ディスクリプタ定数(プロコン方式 / HID パッド方式)
//
// 出典と原則: docs/design/procon-protocol.md §1。レポートディスクリプタは
// 実機実測のバイト列をそのまま複製する(再構成しない)。コンフィグ記述子の
// wDescriptorLength は必ず PADCTL_PROCON_HID_DESC_LEN から生成し、
// 手打ちの二重管理をしないこと。
#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// ---- プロコン方式(本命)----
#define PADCTL_PROCON_VID 0x057E
#define PADCTL_PROCON_PID 0x2009
#define PADCTL_PROCON_HID_DESC_LEN 203

extern const uint8_t padctl_procon_hid_report_desc[PADCTL_PROCON_HID_DESC_LEN];

// ---- HID ゲームパッド方式(保険モード。HORIPAD NSW-001 相当)----
// 注意: このディスクリプタは公開実装で広く使われている形だが、本プロジェクト
// では一次資料と未照合(使用前に要確認)。
#define PADCTL_HIDPAD_VID 0x0F0D
#define PADCTL_HIDPAD_PID 0x0092
#define PADCTL_HIDPAD_HID_DESC_LEN 86

extern const uint8_t padctl_hidpad_hid_report_desc[PADCTL_HIDPAD_HID_DESC_LEN];

#ifdef __cplusplus
}
#endif
