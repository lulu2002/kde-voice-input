#!/bin/sh
cat <<'EOF'

=====================================================
 deepgram-dictate 安裝完成!

 每個使用者第一次使用前,請在終端機執行:

     deepgram-dictate --setup

 它會帶你完成:
   1. 建立設定檔並填入 Deepgram API key
      (https://console.deepgram.com 免費申請)
   2. 註冊 KDE 全域快捷鍵 Meta+H
   3. 建立 KWin 規則,記住 popup 視窗位置

 設定檔位置:~/.config/deepgram-dictate/config.ini
 (可調整:語言/模型、即時字幕開關、透明度、自動貼上)
=====================================================

EOF
exit 0
