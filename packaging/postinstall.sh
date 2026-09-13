#!/bin/sh
cat <<'MSG'

=====================================================
 kde-voice-input 安裝完成!

 每個使用者第一次使用前(或從 deepgram-dictate 升級後),
 請在終端機執行:

     kde-voice-input --setup

 它會帶你完成:
   1. 建立設定檔並填入辨識服務的 API key
      (從 deepgram-dictate 升級會自動轉移舊設定)
   2. 註冊 KDE 全域快捷鍵 Meta+H(升級則沿用舊快捷鍵)
   3. 建立 KWin 規則,記住 popup 視窗位置

 設定檔位置:~/.config/kde-voice-input/config.ini
 (可調整:辨識服務、語言、錄音時暫停音樂/靜音 Discord、自動貼上)
=====================================================

MSG
exit 0
