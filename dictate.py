#!/usr/bin/env python3
"""Deepgram 語音輸入:按快捷鍵跳出 popup,講話即時辨識,Enter 後貼到目前視窗。

用法:
    deepgram-dictate          啟動聆聽 popup(已在跑則等同按 Enter,做成快捷鍵 toggle)
    deepgram-dictate --setup  首次設定:建立設定檔、註冊 KDE 全域快捷鍵 Meta+H

設定:~/.config/deepgram-dictate/config.ini
"""

import asyncio
import configparser
import html
import json
import os
import subprocess
import sys
import threading
from array import array
from pathlib import Path

APP_ID = "deepgram-dictate"
CONFIG_PATH = Path.home() / ".config" / APP_ID / "config.ini"
SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # 100ms @ 16kHz s16le mono
DEFAULT_HOTKEY = "Meta+H"

CONFIG_TEMPLATE = """\
[deepgram]
; 到 https://console.deepgram.com 取得 API key
api_key = YOUR_API_KEY_HERE
; 中文用 nova-2 + zh-TW;純英文可改 model = nova-3, language = en
model = nova-2
language = zh-TW

[ui]
; 背景不透明度 0.0-1.0(文字不受影響)
opacity = 0.88
; 是否即時顯示辨識中的字幕(false 只顯示聆聽狀態與音量條)
show_live_transcript = true

[paste]
; false:Enter 後文字放進剪貼簿,自行 Ctrl+V 貼上(不需要 ydotool)
; true:自動模擬 Ctrl+V(需要 ydotool + ydotoold;若有用 keyd 等
;       remap 工具,需將 ydotool 虛擬裝置排除,否則按鍵會被二次改寫)
auto_paste = false
; 模擬貼上的按鍵序列(預設 Ctrl+V)。
; 終端機(Konsole 等)要 Ctrl+Shift+V:29:1 42:1 47:1 47:0 42:0 29:0
key_sequence = 29:1 47:1 47:0 29:0
; popup 關閉後等焦點回到原視窗的毫秒數
delay_ms = 250
"""


def ensure_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        return True
    return False


def load_config():
    cfg = configparser.ConfigParser()
    cfg.read_string(CONFIG_TEMPLATE)
    if CONFIG_PATH.exists():
        cfg.read(CONFIG_PATH)
    key = os.environ.get("DEEPGRAM_API_KEY") or cfg["deepgram"]["api_key"]
    return cfg, key.strip()


def find_ydotool_socket():
    if os.environ.get("YDOTOOL_SOCKET"):
        return os.environ["YDOTOOL_SOCKET"]
    candidates = [
        Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / ".ydotool_socket",
        Path("/tmp/.ydotool_socket"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


def run_cmd(cmd, **kw):
    """跑外部指令;指令不存在時回傳 None 而不是炸掉(setup 不應因缺某工具而中斷)。"""
    try:
        return subprocess.run(cmd, check=False, **kw)
    except FileNotFoundError:
        return None


def register_kwin_rule():
    """KWin 視窗規則:記住 popup 位置(Wayland 下 app 無法自己定位)。"""
    import uuid

    rules_path = Path.home() / ".config" / "kwinrulesrc"
    if rules_path.exists() and APP_ID in rules_path.read_text(encoding="utf-8", errors="ignore"):
        print("KWin 視窗規則已存在(記住位置)")
        return
    r = run_cmd(
        ["kreadconfig6", "--file", "kwinrulesrc", "--group", "General", "--key", "rules"],
        capture_output=True, text=True)
    existing = r.stdout.strip() if r else ""
    rid = str(uuid.uuid4())
    entries = {
        "Description": f"{APP_ID}: remember position",
        "wmclass": APP_ID,
        "wmclassmatch": "1",
        "position": "680,320",
        "positionrule": "4",  # 4 = Remember(記住使用者拖到哪)
    }
    ok = True
    for k, v in entries.items():
        r = run_cmd(
            ["kwriteconfig6", "--file", "kwinrulesrc", "--group", rid, "--key", k, v])
        ok = ok and r is not None and r.returncode == 0
    if not ok:
        print("找不到 kwriteconfig6,略過 KWin 視窗規則(位置不會被記住)")
        return
    new_rules = f"{existing},{rid}" if existing else rid
    run_cmd(
        ["kwriteconfig6", "--file", "kwinrulesrc", "--group", "General",
         "--key", "rules", new_rules])
    for cmd in (
        ["busctl", "--user", "call", "org.kde.KWin", "/KWin", "org.kde.KWin", "reconfigure"],
        ["qdbus6", "org.kde.KWin", "/KWin", "org.kde.KWin.reconfigure"],
        ["qdbus", "org.kde.KWin", "/KWin", "org.kde.KWin.reconfigure"],
    ):
        r = run_cmd(cmd, capture_output=True)
        if r and r.returncode == 0:
            break
    print("已加入 KWin 視窗規則:popup 位置移動後會被記住")


def setup():
    """首次設定:設定檔 + KDE 全域快捷鍵(不需要 root)。"""
    created = ensure_config()
    print(("已建立設定檔:" if created else "設定檔已存在:") + str(CONFIG_PATH))

    cfg, key = load_config()
    if (not key or key == "YOUR_API_KEY_HERE") and sys.stdin.isatty():
        entered = input("貼上 Deepgram API key(直接按 Enter 跳過):").strip()
        if entered:
            text = CONFIG_PATH.read_text(encoding="utf-8")
            text = text.replace("api_key = YOUR_API_KEY_HERE", f"api_key = {entered}", 1)
            CONFIG_PATH.write_text(text, encoding="utf-8")
            print("API key 已寫入設定檔。")

    r = run_cmd(
        ["kreadconfig6", "--file", "kglobalshortcutsrc",
         "--group", "services", "--group", f"{APP_ID}.desktop", "--key", "_launch"],
        capture_output=True, text=True)
    existing = r.stdout.strip() if r else ""
    if existing:
        print(f"全域快捷鍵已存在:{existing}")
    else:
        r = run_cmd(
            ["kwriteconfig6", "--file", "kglobalshortcutsrc",
             "--group", "services", "--group", f"{APP_ID}.desktop",
             "--key", "_launch", DEFAULT_HOTKEY])
        if r and r.returncode == 0:
            run_cmd(["systemctl", "--user", "restart",
                     "plasma-kglobalaccel.service"])
            print(f"已註冊全域快捷鍵 {DEFAULT_HOTKEY}(可到 系統設定 → 快捷鍵 修改)")
        else:
            print("找不到 kwriteconfig6,請手動到 系統設定 → 快捷鍵 綁定 deepgram-dictate")

    register_kwin_rule()
    print("完成。按快捷鍵開始語音輸入;Enter 結束並複製,Esc 取消。")
    print(f"進階選項(字幕顯示、透明度、自動貼上)見:{CONFIG_PATH}")
    return 0


# ---------------------------------------------------------------- GUI 部分

from PySide6.QtCore import QObject, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)


class Bridge(QObject):
    transcript = Signal(str, str)  # (已確定文字, 進行中文字)
    level = Signal(int)            # 0-100 音量
    finished = Signal(str)         # 最終文字
    error = Signal(str)


class Transcriber:
    """在背景 thread 跑 asyncio:pw-record 錄音 → Deepgram live WS → 回傳字幕。"""

    def __init__(self, bridge, api_key, model, language):
        self.bridge = bridge
        self.api_key = api_key
        self.model = model
        self.language = language
        self.loop = None
        self.stop_event = None
        self.finals = []
        self.interim = ""
        self.thread = threading.Thread(target=self._run_thread, daemon=True)

    def start(self):
        self.thread.start()

    def request_finish(self):
        if self.loop and self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)

    def _joiner(self):
        return "" if self.language.lower().startswith(("zh", "ja")) else " "

    def _combined(self):
        parts = [t for t in self.finals if t]
        if self.interim:
            parts.append(self.interim)
        return self._joiner().join(parts).strip()

    def _run_thread(self):
        try:
            asyncio.run(self._main())
        except Exception as e:  # noqa: BLE001
            self.bridge.error.emit(f"{type(e).__name__}: {e}")

    async def _main(self):
        import websockets

        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()

        params = (
            f"model={self.model}&language={self.language}"
            f"&encoding=linear16&sample_rate={SAMPLE_RATE}&channels=1"
            "&interim_results=true&smart_format=true&punctuate=true"
        )
        uri = f"wss://api.deepgram.com/v1/listen?{params}"
        headers = {"Authorization": f"Token {self.api_key}"}
        try:
            ws = await websockets.connect(uri, additional_headers=headers)
        except TypeError:
            ws = await websockets.connect(uri, extra_headers=headers)

        proc = await asyncio.create_subprocess_exec(
            "pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1",
            "--format", "s16", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def sender():
            try:
                while not self.stop_event.is_set():
                    read = asyncio.create_task(proc.stdout.read(CHUNK_BYTES))
                    stop = asyncio.create_task(self.stop_event.wait())
                    done, pending = await asyncio.wait(
                        {read, stop}, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    if stop in done:
                        break
                    chunk = read.result()
                    if not chunk:
                        break
                    samples = array("h", chunk)
                    peak = max((abs(s) for s in samples), default=0)
                    self.bridge.level.emit(min(100, peak * 100 // 12000))
                    await ws.send(chunk)
            finally:
                if proc.returncode is None:
                    proc.terminate()
                try:
                    await ws.send(json.dumps({"type": "CloseStream"}))
                except Exception:  # noqa: BLE001
                    pass

        async def receiver():
            async for msg in ws:
                data = json.loads(msg)
                if data.get("type") != "Results":
                    continue
                alt = data["channel"]["alternatives"][0]
                text = alt.get("transcript", "")
                if data.get("is_final"):
                    if text:
                        self.finals.append(text)
                    self.interim = ""
                else:
                    self.interim = text
                self.bridge.transcript.emit(
                    self._joiner().join(self.finals), self.interim)

        send_task = asyncio.create_task(sender())
        recv_task = asyncio.create_task(receiver())
        await send_task
        try:
            # CloseStream 之後等 Deepgram 吐完剩下的 final 結果
            await asyncio.wait_for(recv_task, timeout=3)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            recv_task.cancel()
        await ws.close()
        self.bridge.finished.emit(self._combined())


class Popup(QWidget):
    def __init__(self, cfg, api_key):
        super().__init__()
        self.cfg = cfg
        self.finishing = False

        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("語音輸入")
        self.setFixedWidth(520)

        try:
            alpha = min(1.0, max(0.0, cfg["ui"].getfloat("opacity")))
        except ValueError:
            alpha = 0.88
        frame = QWidget(self)
        frame.setObjectName("frame")
        frame.setStyleSheet(f"""
            #frame {{ background: rgba(35, 38, 43, {alpha:.2f}); border-radius: 14px; }}
            QLabel {{ color: #e8e8e8; }}
            QLabel#hint {{ color: #7f8894; font-size: 11px; }}
            QLabel#status {{ color: #ff5f57; font-weight: bold; }}
            QProgressBar {{ background: #33363c; border: none; border-radius: 3px;
                           max-height: 6px; }}
            QProgressBar::chunk {{ background: #4caf82; border-radius: 3px; }}
        """)

        self.status = QLabel("● 聆聽中…")
        self.status.setObjectName("status")
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)

        header = QHBoxLayout()
        header.addWidget(self.status)
        header.addWidget(self.meter, 1)

        self.text = QLabel("")
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.RichText)
        self.text.setMinimumHeight(64)
        self.text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.text.setStyleSheet("font-size: 15px;")

        self.show_live = cfg["ui"].getboolean("show_live_transcript", fallback=True)
        if not self.show_live:
            self.text.setMinimumHeight(0)
            self.text.hide()

        if cfg["paste"].getboolean("auto_paste"):
            hint_text = "Enter / 再按一次快捷鍵 → 貼上輸入 · Esc → 取消"
        else:
            hint_text = "Enter / 再按一次快捷鍵 → 複製到剪貼簿(自行 Ctrl+V)· Esc → 取消"
        hint = QLabel(hint_text)
        hint.setObjectName("hint")

        inner = QVBoxLayout(frame)
        inner.setContentsMargins(18, 14, 18, 12)
        inner.addLayout(header)
        inner.addWidget(self.text)
        inner.addWidget(hint)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)

        model = cfg["deepgram"]["model"]
        lang = cfg["deepgram"]["language"]
        self.bridge = Bridge()
        self.bridge.transcript.connect(self.on_transcript)
        self.bridge.level.connect(self.meter.setValue)
        self.bridge.finished.connect(self.on_finished)
        self.bridge.error.connect(self.on_error)
        self.transcriber = Transcriber(self.bridge, api_key, model, lang)
        self.transcriber.start()

    def on_transcript(self, final, interim):
        if not self.show_live:
            return
        parts = []
        if final:
            parts.append(html.escape(final))
        if interim:
            parts.append(f'<span style="color:#8a93a0">{html.escape(interim)}</span>')
        joiner = "" if self.cfg["deepgram"]["language"].lower().startswith(("zh", "ja")) else " "
        self.text.setText(joiner.join(parts))

    def on_error(self, msg):
        self.text.show()
        self.status.setText("✕ 錯誤")
        self.text.setText(f'<span style="color:#ff5f57">{html.escape(msg)}</span>')

    def finish(self):
        if self.finishing:
            return
        self.finishing = True
        self.status.setText("● 處理中…")
        self.transcriber.request_finish()
        # 保險:Deepgram 沒回應也要能離開
        QTimer.singleShot(4000, lambda: self.on_finished(self.transcriber._combined())
                          if self.finishing else None)

    def on_finished(self, text):
        if not self.finishing:
            self.finishing = True
        self.hide()
        delay = int(self.cfg["paste"]["delay_ms"])
        # 等 KWin 把焦點還給原本的視窗再貼上
        QTimer.singleShot(delay, lambda: self.paste_and_quit(text))

    def paste_and_quit(self, text):
        if text:
            subprocess.run(["wl-copy"], input=text.encode(), check=False)
            if self.cfg["paste"].getboolean("auto_paste"):
                sock = find_ydotool_socket()
                if sock:
                    env = dict(os.environ, YDOTOOL_SOCKET=sock)
                    keys = self.cfg["paste"]["key_sequence"].split()
                    r = subprocess.run(["ydotool", "key", *keys], env=env, check=False)
                    if r.returncode != 0:
                        self.notify_fallback()
                else:
                    self.notify_fallback()
        QApplication.quit()

    @staticmethod
    def notify_fallback():
        subprocess.run(
            ["notify-send", "-a", "語音輸入", "ydotool 無法使用",
             "文字已複製到剪貼簿,請手動 Ctrl+V 貼上"],
            check=False)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.windowHandle().startSystemMove()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.finish()
        elif ev.key() == Qt.Key_Escape:
            QApplication.quit()
        else:
            super().keyPressEvent(ev)


def main():
    if "--setup" in sys.argv[1:]:
        return setup()

    # 單一實例:已在跑就送 toggle(等同按 Enter),讓快捷鍵可以「按一次開始、再按一次結束」
    probe = QLocalSocket()
    probe.connectToServer(APP_ID)
    if probe.waitForConnected(200):
        probe.write(b"toggle")
        probe.waitForBytesWritten(500)
        return 0

    app = QApplication(sys.argv)
    # Wayland app_id 要對上 KWin 視窗規則與 .desktop 檔名
    app.setDesktopFileName(APP_ID)
    ensure_config()
    cfg, api_key = load_config()

    if not api_key or api_key == "YOUR_API_KEY_HERE":
        subprocess.run(
            ["notify-send", "-u", "critical", "-a", "語音輸入", "缺少 Deepgram API key",
             f"請執行 deepgram-dictate --setup 或編輯 {CONFIG_PATH}"],
            check=False)
        return 1

    QLocalServer.removeServer(APP_ID)
    server = QLocalServer()
    server.listen(APP_ID)

    popup = Popup(cfg, api_key)
    server.newConnection.connect(popup.finish)
    popup.show()

    code = app.exec()
    server.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
