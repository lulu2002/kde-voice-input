# deepgram-dictate

KDE Wayland 語音輸入工具,由 [Deepgram](https://deepgram.com) 提供即時語音辨識。

按全域快捷鍵(預設 `Meta+H`)→ 跳出聆聽 popup → 講話,即時顯示字幕 →
按 **Enter**(或再按一次快捷鍵)→ 文字進剪貼簿,`Ctrl+V` 貼到任何視窗。`Esc` 取消。

## 安裝

從 [Releases](../../releases) 下載對應的套件:

```bash
# Fedora / RHEL 系
sudo dnf install ./deepgram-dictate-*.rpm

# Ubuntu / Debian 系
sudo apt install ./deepgram-dictate_*.deb
```

依賴(PySide6、websockets、wl-clipboard、PipeWire 工具)會自動安裝。

### 首次設定(每個使用者跑一次)

```bash
deepgram-dictate --setup
```

會引導你:

1. 建立設定檔並填入 Deepgram API key([console.deepgram.com](https://console.deepgram.com) 免費申請)
2. 註冊 KDE 全域快捷鍵 `Meta+H`(可到 系統設定 → 快捷鍵 修改)
3. 建立 KWin 視窗規則:popup 拖到哪,下次就出現在哪

## 設定

`~/.config/deepgram-dictate/config.ini`:

| 區段 | 選項 | 說明 |
|---|---|---|
| `deepgram` | `api_key` | Deepgram API key(也可用環境變數 `DEEPGRAM_API_KEY`) |
| `deepgram` | `model` / `language` | 預設 `nova-2` / `zh-TW`;純英文可用 `nova-3` / `en` |
| `ui` | `opacity` | 背景不透明度 0.0–1.0,文字不受影響 |
| `ui` | `show_live_transcript` | `false` 時不即時顯示字幕,只顯示聆聽狀態與音量 |
| `paste` | `auto_paste` | `true` 時透過 ydotool 自動按 Ctrl+V(見下方注意事項) |
| `paste` | `key_sequence` | 自動貼上的按鍵序列,終端機需改 Ctrl+Shift+V(見檔內註解) |
| `paste` | `delay_ms` | popup 關閉後等焦點回到原視窗的毫秒數 |

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
- 打包用 [nfpm](https://nfpm.goreleaser.com/),一份 `nfpm.yaml` 同時出 RPM 與 DEB:
  `packaging/build.sh <version>`
- CI:push 到 `main` 後由 semantic-release 依 [Conventional Commits](https://www.conventionalcommits.org/)
  自動判版(`feat:` → minor、`fix:` → patch、`BREAKING CHANGE` → major),
  建 GitHub Release 並附上 RPM/DEB。**commit message 不符合規範就不會觸發發版。**

## 需求

- KDE Plasma 6(Wayland),PipeWire
- Deepgram API key(有免費額度)
