cmake_minimum_required(VERSION 3.16)

# WiFi の接続情報は sdkconfig の中にあるが、あのファイルは作り直されると消える
# (2026-08-02 に実際に消えて、マイコンがネットワークから見えなくなった)。
# 消えない場所として sdkconfig.defaults.local を読む。git には入れない。
# 入れ方: cd pctool && python tools/set_wifi.py
set(SDKCONFIG_DEFAULTS "sdkconfig.defaults")
if(EXISTS "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults.local")
    list(APPEND SDKCONFIG_DEFAULTS "sdkconfig.defaults.local")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(pademu)
