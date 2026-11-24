cmake_minimum_required(VERSION 3.16)

# イメージに含めるバージョン(起動ログの App version)を、装置が報告するバージョン
# PADEMU_FW_VERSION に合わせる。書かないと ESP-IDF は git describe の値を使い、
# git のない所では "1" になる
file(STRINGS "${CMAKE_CURRENT_LIST_DIR}/main/app_config.h" fw_version_line
     REGEX "^#define PADEMU_FW_VERSION ")
string(REGEX REPLACE "^#define PADEMU_FW_VERSION \"([^\"]*)\".*$" "\\1"
       PROJECT_VER "${fw_version_line}")
if(NOT PROJECT_VER MATCHES "^[0-9]+\\.[0-9]+\\.[0-9]+$")
    message(FATAL_ERROR "main/app_config.h から PADEMU_FW_VERSION を読めません")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(pademu)
