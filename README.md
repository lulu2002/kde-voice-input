# kde-voice-input

KDE Wayland 語音輸入工具,可選 [Deepgram](https://deepgram.com)、[Soniox](https://soniox.com)、
[OpenAI](https://platform.openai.com) 作為辨識服務。

按全域快捷鍵(預設 `Meta+H`)→ 跳出聆聽 popup → 講話 →
按 **Enter**(或再按一次快捷鍵)→ 文字進剪貼簿,`Ctrl+V` 貼到任何視窗。`Esc` 取消。

錄音期間會自動暫停正在播放的音樂、把 Discord 等 app 的麥克風靜音,結束後恢復。

## 安裝

從 [Releases](../../releases) 下載對應的套件:

```bash
# Fedora / RHEL 系
sudo dnf install ./kde-voice-input-*.rpm

# Ubuntu / Debian 系
sudo apt install ./kde-voice-input_*.deb
```

依賴(PySide6、websockets、wl-clipboard、PipeWire 工具)會自動安裝。

### 首次設定(每個使用者跑一次)

```bash
kde-voice-input --setup
```

會引導你:

1. 建立設定檔並填入 API key
2. 註冊 KDE 全域快捷鍵 `Meta+H`(可到 系統設定 → 快捷鍵 修改)
3. 建立 KWin 視窗規則:popup 拖到哪,下次就出現在哪

### 從 deepgram-dictate 升級

新套件會取代舊的 `deepgram-dictate`。安裝後執行一次 `kde-voice-input --setup`:
設定檔、快捷鍵、KWin 視窗規則會自動轉移。舊設定檔 `~/.config/deepgram-dictate/` 不會被刪除。

## 選擇辨識服務

| provider | 模式 | 特點 |
|---|---|---|
| `deepgram` | 串流,即時字幕 | 預設 `nova-3` |
| `soniox` | 串流,即時字幕 | 同一句中英夾雜的辨識表現好 |
| `openai` | 錄完整段才辨識 | `gpt-transcribe`,沒有即時字幕,結束後多等 1–2 秒 |

哪個最準取決於你的口音和用詞,建議用自己的錄音比較。先把想比較的 provider 都填好 API key,然後:

```bash
# 錄幾段平常講話的內容(Ctrl+C 結束)
pw-record --rate 16000 --channels 1 --format s16 clip1.wav

# 同時送給所有已填 key 的 provider,並排列出結果與收尾等待時間
kde-voice-input --compare clip*.wav
```

## 設定

`~/.config/kde-voice-input/config.ini`:

| 區段 | 選項 | 說明 |
|---|---|---|
| `general` | `provider` | `deepgram` / `soniox` / `openai` |
| `general` | `language` | BCP-47,預設 `zh-TW`;soniox / openai 會自動加英文提示 |
| `general` | `keywords` | 常講的專有名詞(逗號分隔),提示辨識模型 |
| `general` | `newline_on_pause` | 講話停頓(一句結束)後自動換行,預設開(僅串流 provider) |
| `general` | `traditional_chinese` | `zh-TW`/`zh-HK` 時用 opencc 轉成當地繁體用語(需安裝 opencc) |
| `deepgram` / `soniox` / `openai` | `api_key`、`model` | 各服務的 key 與模型;key 也可用環境變數 `DEEPGRAM_API_KEY` 等 |
| `openai` | `prompt` | 描述你通常在講什麼,幫助模型判斷用詞 |
| `focus` | `pause_media` | 錄音時暫停正在播放的媒體(MPRIS,需要 `playerctl`) |
| `focus` | `mute_mic_apps` | 錄音時靜音這些 app 的麥克風串流(比對程式名稱),預設 `legcord, discord, vesktop` |
| `ui` | `opacity` | 背景不透明度 0.0–1.0,文字不受影響 |
| `ui` | `show_live_transcript` | `false` 時不即時顯示字幕,只顯示聆聽狀態與音量 |
| `paste` | `auto_paste` | `true` 時透過 ydotool 自動按 Ctrl+V(見下方注意事項) |
| `paste` | `key_sequence` | 自動貼上的按鍵序列,終端機需改 Ctrl+Shift+V(見檔內註解) |
| `paste` | `delay_ms` | popup 關閉後等焦點回到原視窗的毫秒數 |

### 關於 Discord 麥克風靜音

靜音是在 PipeWire 層把該 app 的錄音串流設為 mute,對方聽不到你,
但 **Discord 介面上的靜音圖示不會亮**。只會恢復錄音前沒有被靜音的串流;
程式若被強制結束沒來得及恢復,下次啟動時會自動補做。

## 自動貼上與 keyd 使用者注意

預設 **不** 自動貼上(Enter 後自己按 `Ctrl+V`),因此不需要任何 root daemon。

若想開 `auto_paste = true`:

1. 安裝並啟用 ydotool:`sudo dnf install ydotool && sudo systemctl enable --now ydotool.service`
2. **如果你有用 keyd / kanata 等按鍵 remap 工具**:它們會攔截 ydotool 的虛擬鍵盤並二次改寫按鍵
   (例如 Ctrl/Meta 互換的設定會把 Ctrl+V 變成 Meta+V)。
   需在 keyd 的 `[ids]` 排除 ydotool 虛擬裝置的 `vendor:product`
   (ydotoold 跑起來後從 `/proc/bus/input/devices` 查)。

## 開發與發版

- 純 Python 單檔(`dictate.py`),GUI 用 PySide6,錄音直接吃 PipeWire 的 `pw-record`。
- 新增辨識服務:繼承 `Provider`,實作 `transcribe(audio, sink)`,加進 `PROVIDERS`。
- 打包用 [nfpm](https://nfpm.goreleaser.com/),一份 `nfpm.yaml` 同時出 RPM 與 DEB:
  `packaging/build.sh <version>`
- CI:push 到 `main` 後由 semantic-release 依 [Conventional Commits](https://www.conventionalcommits.org/)
  自動判版(`feat:` → minor、`fix:` → patch、`BREAKING CHANGE` → major),
  建 GitHub Release 並附上 RPM/DEB。**commit message 不符合規範就不會觸發發版。**

## 需求

- KDE Plasma 6(Wayland),PipeWire
- 至少一個辨識服務的 API key(Deepgram 有免費額度)
