#!/usr/bin/env python3
"""KDE 語音輸入:按快捷鍵跳出 popup,講話辨識,Enter 後貼到目前視窗。

用法:
    kde-voice-input                   啟動聆聽 popup(已在跑則等同按 Enter,做成快捷鍵 toggle)
    kde-voice-input --setup           首次設定:建立設定檔、註冊 KDE 全域快捷鍵 Meta+H
    kde-voice-input --compare a.wav…  用所有已填 API key 的 provider 辨識同一批錄音,並排比較

設定:~/.config/kde-voice-input/config.ini
"""

import asyncio
import configparser
import html
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from array import array
from pathlib import Path
from urllib.parse import urlencode

APP_ID = "kde-voice-input"
LEGACY_APP_ID = "deepgram-dictate"
CONFIG_PATH = Path.home() / ".config" / APP_ID / "config.ini"
LEGACY_CONFIG_PATH = Path.home() / ".config" / LEGACY_APP_ID / "config.ini"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
FOCUS_STATE_PATH = RUNTIME_DIR / f"{APP_ID}-focus.json"
SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # 100ms @ 16kHz s16le mono
DEFAULT_HOTKEY = "Meta+H"

CONFIG_TEMPLATE = """\
[general]
; 辨識服務,API key 填在下方對應區段:
;   deepgram:串流,有即時字幕
;   soniox  :串流,有即時字幕,中英夾雜表現好
;   openai  :錄完整段才辨識(沒有即時字幕,結束後多等 1-2 秒)
; 不確定哪個準?錄幾段自己講話,用 kde-voice-input --compare 比較
provider = deepgram
; 語言(BCP-47),例如 zh-TW、en;soniox / openai 會自動再加上英文提示
language = zh-TW
; 常講的專有名詞,逗號分隔,提示辨識模型(deepgram 僅 nova-3 支援)
keywords =
; 講話停頓(一句結束)後自動換行(僅串流 provider)
newline_on_pause = true
; language 為 zh-TW / zh-HK 時,輸出轉成當地繁體用語(需要 opencc 指令,沒裝則略過)
traditional_chinese = true

[deepgram]
; https://console.deepgram.com(也可用環境變數 DEEPGRAM_API_KEY)
api_key =
model = nova-3

[soniox]
; https://console.soniox.com(也可用環境變數 SONIOX_API_KEY)
api_key =
model = stt-rt-v5

[openai]
; https://platform.openai.com/api-keys(也可用環境變數 OPENAI_API_KEY)
api_key =
model = gpt-transcribe
; 描述你通常在講什麼,幫助模型判斷用詞(選填)
prompt =

[focus]
; 錄音時暫停正在播放的音樂/影片(需要 playerctl),結束後恢復
pause_media = true
; 錄音時把這些 app 的麥克風串流靜音(比對程式名稱,逗號分隔,留空不靜音),結束後恢復
mute_mic_apps = legcord, discord, vesktop

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

# 舊版 deepgram-dictate 設定 → 新設定的欄位對應:(舊 section, 舊 key, 新 section, 新 key)
LEGACY_CONFIG_MAP = [
    ("deepgram", "api_key", "deepgram", "api_key"),
    ("deepgram", "model", "deepgram", "model"),
    ("deepgram", "language", "general", "language"),
    ("deepgram", "newline_on_pause", "general", "newline_on_pause"),
    ("ui", "opacity", "ui", "opacity"),
    ("ui", "show_live_transcript", "ui", "show_live_transcript"),
    ("paste", "auto_paste", "paste", "auto_paste"),
    ("paste", "key_sequence", "paste", "key_sequence"),
    ("paste", "delay_ms", "paste", "delay_ms"),
]


def set_ini_value(text, section, key, value):
    """改寫 ini 文字裡某個 key 的值,保留註解與排版(configparser 寫回會吃掉註解)。"""
    out, current, done = [], None, False
    for line in text.splitlines(keepends=True):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            current = s[1:-1]
        elif (not done and current == section and "=" in s and not s.startswith((";", "#"))
              and s.split("=", 1)[0].strip() == key):
            line = f"{key} = {value}\n"
            done = True
        out.append(line)
    return "".join(out)


def ensure_config():
    """建立設定檔;有舊版 deepgram-dictate 設定就搬過來。回傳 "created" / "migrated" / None。"""
    if CONFIG_PATH.exists():
        return None
    text = CONFIG_TEMPLATE
    status = "created"
    if LEGACY_CONFIG_PATH.exists():
        old = configparser.ConfigParser(interpolation=None)
        old.read(LEGACY_CONFIG_PATH, encoding="utf-8")
        for old_sec, old_key, new_sec, new_key in LEGACY_CONFIG_MAP:
            value = old.get(old_sec, old_key, fallback="").strip()
            if value and value != "YOUR_API_KEY_HERE":
                text = set_ini_value(text, new_sec, new_key, value)
        status = "migrated"
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(text, encoding="utf-8")
    return status


def load_config():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string(CONFIG_TEMPLATE)
    if CONFIG_PATH.exists():
        cfg.read(CONFIG_PATH, encoding="utf-8")
    return cfg


def api_key(cfg, provider):
    key = os.environ.get(f"{provider.upper()}_API_KEY") or cfg.get(provider, "api_key", fallback="")
    key = key.strip()
    return "" if key == "YOUR_API_KEY_HERE" else key


def find_ydotool_socket():
    if os.environ.get("YDOTOOL_SOCKET"):
        return os.environ["YDOTOOL_SOCKET"]
    candidates = [RUNTIME_DIR / ".ydotool_socket", Path("/tmp/.ydotool_socket")]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


def run_cmd(cmd, **kw):
    """跑外部指令;指令不存在時回傳 None 而不是炸掉(缺某個工具不應讓整個流程中斷)。"""
    try:
        return subprocess.run(cmd, check=False, **kw)
    except FileNotFoundError:
        return None


def make_zh_converter(cfg):
    """回傳把辨識結果轉成當地繁體用語的函式(有些模型只吐簡體)。沒裝 opencc 就原樣輸出。"""
    table = {"zh-tw": "s2twp.json", "zh-hk": "s2hk.json"}.get(
        cfg["general"]["language"].strip().lower())
    if (not table or not cfg["general"].getboolean("traditional_chinese", fallback=True)
            or not shutil.which("opencc")):
        return lambda text: text

    def opencc(config, text):
        r = run_cmd(["opencc", "-c", config], input=text, capture_output=True, text=True)
        if not r or r.returncode != 0:
            return None
        out = r.stdout
        return out[:-1] if out.endswith("\n") and not text.endswith("\n") else out

    def convert(text):
        # 已經是繁體就不要再轉:s2twp 會把繁體的「干擾」改成「幹擾」、「實例」改成「例項」。
        # 含有繁體專用字(t2s 會改動)就視為繁體
        if not text or opencc("t2s.json", text) != text:
            return text
        out = opencc(table, text)
        # opencc 一律用「臺」,台灣日常寫法是「台」
        return text if out is None else out.replace("臺", "台")

    return convert


# ---------------------------------------------------------------- 錄音期間的干擾排除

def playing_players():
    r = run_cmd(["playerctl", "-a", "metadata", "--format", "{{playerInstance}}\t{{status}}"],
                capture_output=True, text=True)
    if not r or r.returncode != 0:
        return []
    rows = (line.split("\t") for line in r.stdout.splitlines())
    return [row[0] for row in rows if len(row) == 2 and row[1] == "Playing"]


def mic_streams(apps):
    """找出程式名稱符合 apps 的麥克風錄音串流(PipeWire node id)。"""
    r = run_cmd(["pw-dump"], capture_output=True, text=True)
    if not r or r.returncode != 0:
        return []
    try:
        objects = json.loads(r.stdout)
    except ValueError:
        return []
    ids = []
    for obj in objects:
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Stream/Input/Audio":
            continue
        # Electron 系的 Discord client 的 application.name 常是 "Chromium",要靠執行檔名稱分辨
        names = " ".join(str(props.get(k, "")) for k in (
            "application.process.binary", "application.name", "node.name")).lower()
        if any(app in names for app in apps):
            ids.append(obj["id"])
    return ids


def is_muted(node_id):
    r = run_cmd(["wpctl", "get-volume", str(node_id)], capture_output=True, text=True)
    return r is None or r.returncode != 0 or "[MUTED]" in r.stdout


class FocusGuard:
    """錄音期間暫停媒體、靜音其他 app(例如 Discord)的麥克風,結束後只恢復自己動過的東西。

    狀態寫在 XDG_RUNTIME_DIR:程式若被砍掉沒機會恢復,下次啟動時由 recover() 補做。
    """

    def __init__(self, cfg):
        self.pause_media = cfg["focus"].getboolean("pause_media", fallback=True)
        self.mute_apps = [a.strip().lower() for a in cfg["focus"]["mute_mic_apps"].split(",")
                          if a.strip()]
        self.state = None

    def engage(self):
        state = {"players": [], "streams": [], "apps": self.mute_apps}
        if self.pause_media:
            for player in playing_players():
                run_cmd(["playerctl", "-p", player, "pause"])
                state["players"].append(player)
        if self.mute_apps:
            for node_id in mic_streams(self.mute_apps):
                if not is_muted(node_id):
                    run_cmd(["wpctl", "set-mute", str(node_id), "1"])
                    state["streams"].append(node_id)
        self.state = state
        if state["players"] or state["streams"]:
            FOCUS_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")

    def release(self):
        if self.state is not None:
            self._restore(self.state)
            self.state = None
        FOCUS_STATE_PATH.unlink(missing_ok=True)

    @classmethod
    def recover(cls):
        try:
            state = json.loads(FOCUS_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        cls._restore(state)
        FOCUS_STATE_PATH.unlink(missing_ok=True)

    @staticmethod
    def _restore(state):
        for player in state.get("players", []):
            r = run_cmd(["playerctl", "-p", player, "status"], capture_output=True, text=True)
            # 使用者在錄音期間自己換成 Stopped 或關掉播放器就不要亂播
            if r and r.stdout.strip() == "Paused":
                run_cmd(["playerctl", "-p", player, "play"])
        if state.get("streams"):
            # node id 可能已被回收給別的串流,只解除仍然符合的
            alive = set(mic_streams(state.get("apps", [])))
            for node_id in state["streams"]:
                if node_id in alive:
                    run_cmd(["wpctl", "set-mute", str(node_id), "0"])


# ---------------------------------------------------------------- setup

def register_kwin_rule():
    """KWin 視窗規則:記住 popup 位置(Wayland 下 app 無法自己定位)。"""
    rules_path = Path.home() / ".config" / "kwinrulesrc"
    rules_text = rules_path.read_text(encoding="utf-8", errors="ignore") if rules_path.exists() else ""

    # 舊版 deepgram-dictate 建的規則:改掉 wmclass 沿用,保留使用者拖好的位置
    if LEGACY_APP_ID in rules_text:
        rules = configparser.ConfigParser(interpolation=None, strict=False)
        rules.optionxform = str
        try:
            rules.read_string(rules_text)
        except configparser.Error:
            rules = None
        migrated = False
        for group in (rules.sections() if rules else []):
            if rules[group].get("wmclass") == LEGACY_APP_ID:
                for k, v in (("wmclass", APP_ID), ("Description", f"{APP_ID}: remember position")):
                    run_cmd(["kwriteconfig6", "--file", "kwinrulesrc", "--group", group, "--key", k, v])
                migrated = True
        if migrated:
            reconfigure_kwin()
            print("已將舊版 KWin 視窗規則轉移到新名稱")
            return

    if APP_ID in rules_text:
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
    reconfigure_kwin()
    print("已加入 KWin 視窗規則:popup 位置移動後會被記住")


def reconfigure_kwin():
    for cmd in (
        ["busctl", "--user", "call", "org.kde.KWin", "/KWin", "org.kde.KWin", "reconfigure"],
        ["qdbus6", "org.kde.KWin", "/KWin", "org.kde.KWin.reconfigure"],
        ["qdbus", "org.kde.KWin", "/KWin", "org.kde.KWin.reconfigure"],
    ):
        r = run_cmd(cmd, capture_output=True)
        if r and r.returncode == 0:
            break


def read_shortcut(app_id):
    r = run_cmd(
        ["kreadconfig6", "--file", "kglobalshortcutsrc",
         "--group", "services", "--group", f"{app_id}.desktop", "--key", "_launch"],
        capture_output=True, text=True)
    return r.stdout.strip() if r else ""


def write_shortcut(app_id, value):
    cmd = ["kwriteconfig6", "--file", "kglobalshortcutsrc",
           "--group", "services", "--group", f"{app_id}.desktop", "--key", "_launch"]
    r = run_cmd(cmd + (["--delete"] if value is None else [value]))
    return r is not None and r.returncode == 0


def setup():
    """首次設定:設定檔 + KDE 全域快捷鍵(不需要 root)。"""
    status = ensure_config()
    print({"created": "已建立設定檔:",
           "migrated": f"已從 {LEGACY_CONFIG_PATH} 轉移設定到:"}.get(status, "設定檔已存在:")
          + str(CONFIG_PATH))

    cfg = load_config()
    provider = cfg["general"]["provider"].strip()
    if provider in PROVIDERS and not api_key(cfg, provider) and sys.stdin.isatty():
        entered = input(f"貼上 {provider} API key(直接按 Enter 跳過):").strip()
        if entered:
            text = CONFIG_PATH.read_text(encoding="utf-8")
            CONFIG_PATH.write_text(set_ini_value(text, provider, "api_key", entered),
                                   encoding="utf-8")
            print("API key 已寫入設定檔。")

    existing = read_shortcut(APP_ID)
    legacy = read_shortcut(LEGACY_APP_ID)
    if existing:
        print(f"全域快捷鍵已存在:{existing}")
    elif write_shortcut(APP_ID, legacy.split(",")[0] if legacy else DEFAULT_HOTKEY):
        if legacy:
            write_shortcut(LEGACY_APP_ID, None)
        run_cmd(["systemctl", "--user", "restart", "plasma-kglobalaccel.service"])
        hotkey = legacy.split(",")[0] if legacy else DEFAULT_HOTKEY
        print(f"已註冊全域快捷鍵 {hotkey}(可到 系統設定 → 快捷鍵 修改)")
    else:
        print(f"找不到 kwriteconfig6,請手動到 系統設定 → 快捷鍵 綁定 {APP_ID}")

    register_kwin_rule()
    if cfg["general"]["language"].strip().lower() in ("zh-tw", "zh-hk") and not shutil.which("opencc"):
        print("提示:安裝 opencc(Fedora: opencc-tools)可確保中文輸出為繁體")
    if cfg["focus"]["mute_mic_apps"].strip() or cfg["focus"].getboolean("pause_media"):
        missing = [t for t in ("playerctl", "pw-dump", "wpctl") if not shutil.which(t)]
        if missing:
            print(f"提示:錄音時暫停音樂/靜音其他 app 需要:{', '.join(missing)}")
    print("完成。按快捷鍵開始語音輸入;Enter 結束並複製,Esc 取消。")
    print(f"切換辨識服務、字幕顯示、自動貼上等選項見:{CONFIG_PATH}")
    return 0


# ---------------------------------------------------------------- 辨識 provider

class TextSink:
    """累積辨識結果:lines 是因停頓斷開的句子,finals 是目前這行已確定的片段。"""

    def __init__(self, joiner="", newline_on_pause=True):
        self.joiner = joiner
        self.newline_on_pause = newline_on_pause
        self.lines = []
        self.finals = []
        self.interim = ""

    def update(self, final, interim):
        """final:新確定的文字(可為空);interim:目前進行中、還可能變動的文字。"""
        if final:
            self.finals.append(final)
        self.interim = interim
        self.changed()

    def pause(self):
        """provider 判定一句話結束。"""
        if self.newline_on_pause and self.finals:
            self.lines.append(self.joiner.join(self.finals).strip())
            self.finals = []
            self.changed()

    def final_text(self):
        parts = list(self.lines)
        if self.finals:
            parts.append(self.joiner.join(self.finals).strip())
        return "\n".join(parts)

    def combined(self):
        cur = [t for t in self.finals if t]
        if self.interim:
            cur.append(self.interim)
        lines = self.lines + ([self.joiner.join(cur).strip()] if cur else [])
        return "\n".join(lines).strip()

    def changed(self):
        pass


async def ws_connect(uri, headers=None):
    import websockets

    # 結果收齊才會關連線,不值得讓使用者等伺服器回 close frame(Soniox 要 1 秒多)
    try:
        return await websockets.connect(uri, additional_headers=headers, close_timeout=0.2)
    except TypeError:  # websockets < 14
        return await websockets.connect(uri, extra_headers=headers, close_timeout=0.2)


async def run_streaming(ws, audio, end_of_stream, receiver):
    """串流 provider 共用流程:一邊送音訊一邊收結果,音訊結束後等伺服器吐完剩下的 final。"""

    async def sender():
        try:
            async for chunk in audio:
                await ws.send(chunk)
        finally:
            try:
                await ws.send(end_of_stream)
            except Exception:  # noqa: BLE001
                pass

    send_task = asyncio.create_task(sender())
    recv_task = asyncio.create_task(receiver())
    try:
        done, _ = await asyncio.wait({send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
        if recv_task in done:
            recv_task.result()  # 伺服器回錯誤就直接丟出來
            raise RuntimeError("辨識服務提前關閉了連線")
        send_task.result()
        try:
            await asyncio.wait_for(recv_task, timeout=3)
        except Exception:  # noqa: BLE001
            pass  # 收尾階段出錯就用目前已有的文字,不要讓使用者整段白講
    finally:
        send_task.cancel()
        recv_task.cancel()
        await ws.close()


class Provider:
    name = ""
    streaming = True

    def __init__(self, cfg, key):
        self.cfg = cfg
        self.api_key = key
        self.language = cfg["general"]["language"].strip()
        self.keywords = [k.strip() for k in cfg["general"]["keywords"].split(",") if k.strip()]

    @property
    def joiner(self):
        return ""

    def language_hints(self):
        """主要語言 + 英文,讓模型預期中英夾雜。"""
        return list(dict.fromkeys([self.language.lower(), "en"]))

    async def transcribe(self, audio, sink):
        """audio:16kHz s16le mono 的 PCM chunk async iterator,使用者結束錄音時停止。"""
        raise NotImplementedError


class DeepgramProvider(Provider):
    name = "deepgram"

    @property
    def joiner(self):
        return "" if self.language.lower().startswith(("zh", "ja")) else " "

    async def transcribe(self, audio, sink):
        model = self.cfg["deepgram"]["model"].strip()
        params = [
            ("model", model), ("language", self.language),
            ("encoding", "linear16"), ("sample_rate", SAMPLE_RATE), ("channels", 1),
            ("interim_results", "true"), ("smart_format", "true"), ("punctuate", "true"),
        ]
        if model.startswith("nova-3"):
            params += [("keyterm", k) for k in self.keywords]
        ws = await ws_connect(f"wss://api.deepgram.com/v1/listen?{urlencode(params)}",
                              {"Authorization": f"Token {self.api_key}"})

        async def receiver():
            async for msg in ws:
                data = json.loads(msg)
                if data.get("type") != "Results":
                    continue
                text = data["channel"]["alternatives"][0].get("transcript", "")
                if data.get("is_final"):
                    sink.update(text, "")
                    if data.get("speech_final"):
                        sink.pause()
                else:
                    sink.update("", text)

        await run_streaming(ws, audio, json.dumps({"type": "CloseStream"}), receiver)


class SonioxProvider(Provider):
    name = "soniox"

    def language_hints(self):
        # Soniox 的 language_hints 只吃 ISO 639-1
        return list(dict.fromkeys([self.language.split("-")[0].lower(), "en"]))

    async def transcribe(self, audio, sink):
        ws = await ws_connect("wss://stt-rt.soniox.com/transcribe-websocket")
        config = {
            "api_key": self.api_key,
            "model": self.cfg["soniox"]["model"].strip(),
            "audio_format": "pcm_s16le",
            "sample_rate": SAMPLE_RATE,
            "num_channels": 1,
            "language_hints": self.language_hints(),
            "enable_endpoint_detection": True,
        }
        if self.keywords:
            config["context"] = {"terms": self.keywords}
        await ws.send(json.dumps(config))

        async def receiver():
            async for msg in ws:
                data = json.loads(msg)
                if data.get("error_code"):
                    raise RuntimeError(f"Soniox {data['error_code']}: {data.get('error_message', '')}")
                # final token 只會送一次;non-final token 每則訊息都是完整的最新版本
                final, interim = [], []
                for tok in data.get("tokens", []):
                    text = tok.get("text", "")
                    if text == "<fin>":  # finalize 完成,剩下的字都已確定
                        sink.update("".join(final), "")
                        return
                    if text == "<end>":
                        sink.update("".join(final), "")
                        final = []
                        sink.pause()
                    elif tok.get("is_final"):
                        final.append(text)
                    else:
                        interim.append(text)
                sink.update("".join(final), "".join(interim))
                if data.get("finished"):
                    return

        # 不用空 frame 收尾:那樣伺服器要約 20 秒才送 finished;
        # finalize 會在 0.3 秒內把待定的字確定並回 <fin>
        await run_streaming(ws, audio, json.dumps({"type": "finalize"}), receiver)


def pcm_to_wav(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def encode_multipart(fields, file_field, filename, data, content_type):
    boundary = uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in fields:
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                  f"{value}\r\n".encode())
    out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
              f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode())
    out.write(data)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


class OpenAIProvider(Provider):
    name = "openai"
    streaming = False

    async def transcribe(self, audio, sink):
        pcm = bytearray()
        async for chunk in audio:
            pcm += chunk
        if len(pcm) < SAMPLE_RATE * 2 // 4:  # 不到 0.25 秒,不值得送
            return
        sink.update(await asyncio.to_thread(self._request, bytes(pcm)), "")

    def _request(self, pcm):
        model = self.cfg["openai"]["model"].strip()
        fields = [("model", model), ("response_format", "json")]
        prompt = self.cfg["openai"]["prompt"].strip()
        if prompt:
            fields.append(("prompt", prompt))
        if model.startswith(("gpt-transcribe", "gpt-live-transcribe")):
            fields += [("languages[]", lang) for lang in self.language_hints()]
            fields += [("keywords[]", k) for k in self.keywords]
        else:  # whisper-1 / gpt-4o-transcribe 只吃單一 ISO 639-1 語言
            fields.append(("language", self.language.split("-")[0].lower()))
        body, content_type = encode_multipart(fields, "file", "audio.wav", pcm_to_wav(pcm), "audio/wav")
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions", data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": content_type})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp).get("text", "").strip()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(detail)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                detail = detail[:300]
            raise RuntimeError(f"OpenAI HTTP {e.code}: {detail}") from None


PROVIDERS = {p.name: p for p in (DeepgramProvider, SonioxProvider, OpenAIProvider)}


async def mic_chunks(stop_event, on_level):
    """pw-record 錄音,直到 stop_event 被設定。"""
    proc = await asyncio.create_subprocess_exec(
        "pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "s16", "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        while not stop_event.is_set():
            read = asyncio.create_task(proc.stdout.read(CHUNK_BYTES))
            stop = asyncio.create_task(stop_event.wait())
            try:
                done, _ = await asyncio.wait({read, stop}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                read.cancel()
                stop.cancel()
            if stop in done:
                break
            chunk = read.result()
            if not chunk:
                break
            peak = max((abs(s) for s in array("h", chunk)), default=0)
            on_level(min(100, peak * 100 // 12000))
            yield chunk
    finally:
        if proc.returncode is None:
            proc.terminate()


# ---------------------------------------------------------------- --compare

def read_wav(path):
    with wave.open(str(path), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError("需要 16kHz / mono / 16-bit WAV,可用 "
                             "ffmpeg -i 原檔 -ar 16000 -ac 1 -sample_fmt s16 輸出.wav 轉換")
        return w.readframes(w.getnframes())


async def compare_one(provider, pcm):
    """回傳 (文字, 音訊送完後等了幾秒, 錯誤)。串流 provider 以實際說話速度送音訊。"""
    audio_end = None

    async def paced():
        nonlocal audio_end
        for i in range(0, len(pcm), CHUNK_BYTES):
            yield pcm[i:i + CHUNK_BYTES]
            if provider.streaming:
                await asyncio.sleep(CHUNK_BYTES / (SAMPLE_RATE * 2))
        audio_end = time.monotonic()

    sink = TextSink(provider.joiner)
    try:
        await provider.transcribe(paced(), sink)
        error = None
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    waited = time.monotonic() - audio_end if audio_end else 0.0
    return sink.combined(), waited, error


def compare(paths):
    if not paths:
        print("用法:kde-voice-input --compare 錄音1.wav 錄音2.wav …\n"
              "錄音:pw-record --rate 16000 --channels 1 --format s16 錄音1.wav(Ctrl+C 結束)")
        return 2
    ensure_config()
    cfg = load_config()
    providers = [cls(cfg, key) for name, cls in PROVIDERS.items() if (key := api_key(cfg, name))]
    if not providers:
        print(f"沒有任何 provider 填了 API key,請編輯 {CONFIG_PATH}")
        return 1
    convert = make_zh_converter(cfg)
    print(f"比較:{', '.join(p.name for p in providers)}(語言 {cfg['general']['language']})\n")

    async def all_providers(pcm):
        return await asyncio.gather(*(compare_one(p, pcm) for p in providers))

    for path in paths:
        try:
            pcm = read_wav(path)
        except (OSError, ValueError, wave.Error) as e:
            print(f"## {path}\n\n略過:{e}\n")
            continue
        print(f"## {path}({len(pcm) / (SAMPLE_RATE * 2):.1f} 秒)\n")
        for p, (text, waited, error) in zip(providers, asyncio.run(all_providers(pcm))):
            result = f"錯誤 {error}" if error else convert(text).replace("\n", " / ")
            print(f"- **{p.name}**(收尾 {waited:.1f}s):{result}")
        print()
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


class Transcriber(TextSink):
    """在背景 thread 跑 asyncio:pw-record 錄音 → provider → 透過 Bridge 回傳字幕。"""

    def __init__(self, bridge, provider, newline_on_pause=True):
        super().__init__(provider.joiner, newline_on_pause)
        self.bridge = bridge
        self.provider = provider
        self.loop = None
        self.stop_event = None
        self.thread = threading.Thread(target=self._run_thread, daemon=True)

    def start(self):
        self.thread.start()

    def request_finish(self):
        if self.loop and self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)

    def changed(self):
        self.bridge.transcript.emit(self.final_text(), self.interim)

    def _run_thread(self):
        try:
            asyncio.run(self._main())
        except Exception as e:  # noqa: BLE001
            self.bridge.error.emit(f"{type(e).__name__}: {e}")

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        await self.provider.transcribe(mic_chunks(self.stop_event, self.bridge.level.emit), self)
        self.bridge.finished.emit(self.combined())


class Popup(QWidget):
    def __init__(self, cfg, provider, guard):
        super().__init__()
        self.cfg = cfg
        self.provider = provider
        self.guard = guard
        self.convert = make_zh_converter(cfg)
        self.finishing = False
        self.errored = False

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
        if self.show_live and not provider.streaming:
            self.text.setText(f'<span style="color:#8a93a0">{provider.name}:'
                              "錄完才辨識,沒有即時字幕</span>")
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

        self.bridge = Bridge()
        self.bridge.transcript.connect(self.on_transcript)
        self.bridge.level.connect(self.meter.setValue)
        self.bridge.finished.connect(self.on_finished)
        self.bridge.error.connect(self.on_error)
        self.transcriber = Transcriber(
            self.bridge, provider,
            newline_on_pause=cfg["general"].getboolean("newline_on_pause", fallback=True))
        self.transcriber.start()

    def on_transcript(self, final, interim):
        if not self.show_live:
            return
        parts = []
        if final:
            parts.append(html.escape(self.convert(final)).replace("\n", "<br>"))
        if interim:
            parts.append(f'<span style="color:#8a93a0">{html.escape(self.convert(interim))}</span>')
        self.text.setText(self.provider.joiner.join(parts))

    def on_error(self, msg):
        self.errored = True
        self.finishing = False
        self.guard.release()
        self.text.show()
        self.status.setText("✕ 錯誤")
        self.text.setText(f'<span style="color:#ff5f57">{html.escape(msg)}</span>')

    def finish(self):
        # 錯誤狀態下再收到 toggle/Enter 就直接退出,
        # 不然卡住的實例會佔著 single-instance socket,吃掉之後所有啟動請求
        if self.errored:
            QApplication.quit()
            return
        if self.finishing:
            return
        self.finishing = True
        self.status.setText("● 處理中…" if self.provider.streaming else "● 辨識中…")
        self.transcriber.request_finish()
        # 保險:provider 沒回應也要能離開(batch provider 要等整段上傳辨識,給比較久)
        timeout = 4000 if self.provider.streaming else 65000
        QTimer.singleShot(timeout, lambda: self.on_finished(self.transcriber.combined())
                          if self.finishing else None)

    def on_finished(self, text):
        if not self.finishing:
            self.finishing = True
        self.hide()
        self.guard.release()
        delay = int(self.cfg["paste"]["delay_ms"])
        # 等 KWin 把焦點還給原本的視窗再貼上
        QTimer.singleShot(delay, lambda: self.paste_and_quit(self.convert(text)))

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


def notify_error(title, body):
    subprocess.run(["notify-send", "-u", "critical", "-a", "語音輸入", title, body], check=False)


def main():
    args = sys.argv[1:]
    if "--setup" in args:
        return setup()
    if "--compare" in args:
        return compare([a for a in args if a != "--compare"])

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
    cfg = load_config()

    name = cfg["general"]["provider"].strip().lower()
    if name not in PROVIDERS:
        notify_error(f"未知的 provider:{name}", f"可用:{', '.join(PROVIDERS)},請編輯 {CONFIG_PATH}")
        return 1
    key = api_key(cfg, name)
    if not key:
        notify_error(f"缺少 {name} API key",
                     f"請執行 {APP_ID} --setup 或編輯 {CONFIG_PATH}")
        return 1

    # 上一次被砍掉沒來得及恢復的音樂/麥克風,先恢復
    FocusGuard.recover()

    QLocalServer.removeServer(APP_ID)
    server = QLocalServer()
    server.listen(APP_ID)

    guard = FocusGuard(cfg)
    popup = Popup(cfg, PROVIDERS[name](cfg, key), guard)
    server.newConnection.connect(popup.finish)
    popup.show()
    QTimer.singleShot(0, guard.engage)

    try:
        return app.exec()
    finally:
        guard.release()  # Esc 取消等任何離開路徑都要恢復
        server.close()


if __name__ == "__main__":
    sys.exit(main())
