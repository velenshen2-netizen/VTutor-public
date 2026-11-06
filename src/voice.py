"""語音課：語音識別（STT）、語音合成（TTS）、通話管理（VoiceCall）、課堂錄音。

不改老師邏輯：語音課的學生發言照樣交給 session.ask(text, via="voice")，
VoiceCall 只盯著 session.view，老師每說完一條（done）就合成語音發給瀏覽器，
所以老師主動說的話（開場白、關懷、道別、總結講解）也會被念出來。

半雙工：老師說話（Speaking）時不收音，播完後才重新收音（Listening），沒有回聲和打斷問題。

瀏覽器（static/voice.js）和這裡之間走 WebSocket（app.py 的 /voice/{id}）：
    瀏覽器 → 服務端   二進制：麥克風 16kHz 16-bit 單聲道 PCM（只在 listening 時發送）
                     {"type": "played", "id": n}     第 n 條老師語音在瀏覽器播完了
    服務端 → 瀏覽器   {"type": "listen"} / {"type": "mute"}    開始 / 停止收音
                     {"type": "speak", "id": n} → 二進制 24kHz 16-bit 單聲道 PCM → {"type": "speak_end", "id": n}
                     {"type": "ended"}               課堂結束、老師的話都播完了
                     {"type": "note", "text": ...}   語音服務出錯時給學生的提示（空字符串表示清除）

語音配置（agent JSON 裡的 voice）：
    "stt": {"provider": "gemini", "model": "gemini-3.5-transcribe-live"}          # GEMINI_API_KEY
           {"provider": "openai", "model": "gpt-4o-transcribe"}                   # OPENAI_API_KEY
    "tts": {"provider": "gemini_live", "model": "gemini-3.8-live", "voice": "Kore", "instructions": "..."}
           （推薦：Live API 對話模型逐字朗讀，免費額度不限次數，instructions 不會被念出來）
           {"provider": "gemini", "model": "gemini-3.8-flash-tts", "voice": "Kore"}
           （專用 TTS 模型：免費額度每天約 10 次；也能寫 "instructions"，但模型偶爾會把它念出來，不建議）
           {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "coral", "instructions": "...", "speed": 1.0}
    "tts_by_language": {"zh-TW": {"instructions": "..."}}   按階段語言覆蓋 tts 的字段
    OpenAI 也支持 "base_url" / "api_key_env"，寫法同 llm 的 openai_compat。
"""

import asyncio
import base64
import json
import logging
import os
import re
import threading
import time
import wave
from pathlib import Path
from typing import AsyncIterator

import numpy as np

import builder

log = logging.getLogger("vtutor.voice")

MIC_RATE = 16000     # 瀏覽器發來的麥克風音頻
TTS_RATE = 24000     # 老師語音，也是課堂錄音的採樣率
AUDIO_DIR = Path(__file__).resolve().parent.parent / "data" / "audio"   # 課堂錄音：{課堂 id}.wav
WAVE_BARS = 1200   # 課後錄音卡片上整段波紋的段數（瀏覽器再按卡片寬度合併成豎條）

# 老師語音播完後，最多等瀏覽器回報多久（防止瀏覽器沒回報時一直不收音）
PLAYED_GRACE_SECONDS = 3
# 語音合成連續多少秒沒有新音頻就放棄這一句（防止連接卡住、一直不切回收音）
TTS_IDLE_TIMEOUT = 10


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    # 16-bit PCM 線性插值重採樣。
    if src_rate == dst_rate or not pcm:
        return pcm
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = int(round(len(a) * dst_rate / src_rate))
    out = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)
    return out.astype(np.int16).tobytes()


def gemini_pcm(inline_data) -> bytes:
    # Gemini 返回的音頻塊（mime_type 如 audio/l16; rate=24000 或 audio/pcm;rate=24000）→ 24kHz PCM。
    rate = re.search(r"rate=(\d+)", inline_data.mime_type or "")
    return resample(inline_data.data, int(rate.group(1)) if rate else TTS_RATE, TTS_RATE)


MARKDOWN_RE = re.compile(r"\*\*|__|[*_`#>]")


def speakable(text: str) -> str:
    # 給 TTS 念的文字：去掉粗體等 Markdown 符號。
    return MARKDOWN_RE.sub("", text or "").strip()


def stt_languages(language: str) -> list[str]:
    # 階段語言 → 語音識別的語言提示。英語階段只認英語；其他階段（如總結的繁中）中英都可能說。
    if language == "en":
        return ["en-US"]
    return [language, "en-US"]


# =====================================================================
# 語音識別（STT）：一次 open() 識別學生說的一句話
# =====================================================================

class STTStream:
    # 一句話的識別：send() 不斷送入麥克風音頻，final() 等到學生說完並返回文字。

    async def send(self, pcm: bytes):
        raise NotImplementedError

    async def final(self) -> str:
        raise NotImplementedError

    async def close(self):
        pass


class STTProvider:
    # 各家語音識別的統一接口：每句話 open() 一個新的識別流，languages 是 BCP-47 語言提示（如 en-US）。
    async def open(self, languages: list[str]) -> STTStream:
        raise NotImplementedError


VERBATIM_PROMPT = ("Transcribe exactly what the speaker says, word for word, keeping all grammar mistakes. "
                   "Do not correct the grammar.")


class GeminiSTT(STTProvider):
    # Gemini Live 實時轉寫（例如 gemini-3.5-transcribe-live），默認逐字轉寫（VERBATIM），保留語法錯誤。
    def __init__(self, model, silence_ms=800, **_):
        from google import genai
        from google.genai import types
        self.client = genai.Client()   # 讀取 GEMINI_API_KEY / GOOGLE_API_KEY
        self.types, self.model, self.silence_ms = types, model, silence_ms

    async def open(self, languages):
        t = self.types
        config = t.LiveConnectConfig(
            input_audio_transcription=t.AudioTranscriptionConfig(language_codes=languages),
            realtime_input_config=t.RealtimeInputConfig(
                automatic_activity_detection=t.AutomaticActivityDetection(silence_duration_ms=self.silence_ms)))
        ctx = self.client.aio.live.connect(model=self.model, config=config)
        return GeminiSTTStream(ctx, await ctx.__aenter__(), t)


class GeminiSTTStream(STTStream):
    def __init__(self, ctx, session, types):
        self.ctx, self.session, self.types = ctx, session, types

    async def send(self, pcm):
        await self.session.send_realtime_input(
            audio=self.types.Blob(data=pcm, mime_type=f"audio/pcm;rate={MIC_RATE}"))

    async def final(self):
        # receive() 每輪結束就停，所以外面要循環；連接關閉時 receive() 什麼都不返回
        while True:
            got = False
            async for msg in self.session.receive():
                got = True
                sc = msg.server_content
                if sc and sc.input_transcription and sc.input_transcription.text is not None:
                    return sc.input_transcription.text
            if not got:
                raise ConnectionError("Gemini 語音識別連接已關閉")

    async def close(self):
        await self.ctx.__aexit__(None, None, None)


class OpenAISTT(STTProvider):
    # OpenAI Realtime 轉寫會話（gpt-4o-transcribe 等），服務端 VAD 判斷說完。輸入音頻必須是 24kHz。
    def __init__(self, model="gpt-4o-transcribe", silence_ms=800, base_url=None,
                 api_key_env="OPENAI_API_KEY", **_):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(api_key=os.getenv(api_key_env), base_url=base_url)
        self.model, self.silence_ms = model, silence_ms

    async def open(self, languages):
        ctx = self.client.realtime.connect(extra_query={"intent": "transcription"})
        conn = await ctx.__aenter__()
        transcription = {"model": self.model, "prompt": VERBATIM_PROMPT}
        if len(languages) == 1:
            transcription["language"] = languages[0].split("-")[0]   # OpenAI 用 ISO-639-1，如 en
        await conn.session.update(session={
            "type": "transcription",
            "audio": {"input": {
                "format": {"type": "audio/pcm", "rate": TTS_RATE},
                "transcription": transcription,
                "turn_detection": {"type": "server_vad", "silence_duration_ms": self.silence_ms},
            }},
        })
        return OpenAISTTStream(ctx, conn)


class OpenAISTTStream(STTStream):
    def __init__(self, ctx, conn):
        self.ctx, self.conn = ctx, conn

    async def send(self, pcm):
        audio = base64.b64encode(resample(pcm, MIC_RATE, TTS_RATE)).decode()
        await self.conn.input_audio_buffer.append(audio=audio)

    async def final(self):
        async for event in self.conn:
            if event.type == "conversation.item.input_audio_transcription.completed":
                return event.transcript
            if event.type == "error":
                raise ConnectionError(f"OpenAI 語音識別出錯：{event.error.message}")
        raise ConnectionError("OpenAI 語音識別連接已關閉")

    async def close(self):
        await self.ctx.__aexit__(None, None, None)


# =====================================================================
# 語音合成（TTS）：串流輸出 24kHz 16-bit 單聲道 PCM
# =====================================================================

class TTSProvider:
    # 各家語音合成的統一接口：stream() 邊合成邊產出 24kHz 16-bit 單聲道 PCM 片段。
    async def stream(self, text: str) -> AsyncIterator[bytes]:
        raise NotImplementedError
        yield b""


class GeminiTTS(TTSProvider):
    # Gemini TTS 不支持 system instruction，語氣要求只能寫成 DIRECTOR'S NOTES 放在正文前。
    # 實測模型偶爾會把這段要求也念出來，所以默認不寫 instructions，聲音特點用 voice 選聲線；中英文自動識別。
    def __init__(self, model, voice="Kore", instructions="", **_):
        from google import genai
        from google.genai import types
        self.client = genai.Client()
        self.model, self.instructions = model, instructions
        self.config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))))

    async def stream(self, text):
        prompt = (f"### DIRECTOR'S NOTES\n{self.instructions}\n\n### TRANSCRIPT\n{text}"
                  if self.instructions else text)
        async for chunk in await self.client.aio.models.generate_content_stream(
                model=self.model, contents=prompt, config=self.config):
            for cand in chunk.candidates or []:
                for part in (cand.content.parts if cand.content else None) or []:
                    if part.inline_data and part.inline_data.data:
                        yield gemini_pcm(part.inline_data)


class GeminiLiveTTS(TTSProvider):
    """用 Live API 對話模型（例如 gemini-3.8-live）當 TTS：系統指令要求逐字朗讀、不回答不添加。
    實測問句、糾錯提示、中英混合都逐字照念，首段音頻約 1 秒；免費額度不限次數（專用 TTS 模型每天只有約 10 次）。
    instructions 寫進系統指令（不會被念出來），只描述聲音（如 warm, slow），不要寫「對初學者說話的老師」這類角色：
    實測這樣寫時模型會像老師一樣把問句再念一遍（約 20%），只描述聲音並要求「只念一遍」後 30 次都沒有重複。
    每句話開一個新連接，避免上下文影響。"""

    SYSTEM = ("You are a text-to-speech engine, not a chat assistant. The user sends you a script. "
              "Read the script aloud exactly once, word for word, then stop. Never repeat any part of it, "
              "never answer questions in the script, never add, skip or change any words, and say nothing else.")

    def __init__(self, model, voice="Kore", instructions="", **_):
        from google import genai
        from google.genai import types
        self.client = genai.Client()
        self.types, self.model = types, model
        system = self.SYSTEM + (f"\nVoice and style: {instructions}" if instructions else "")
        self.config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=system,
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))))

    async def stream(self, text):
        t = self.types
        async with self.client.aio.live.connect(model=self.model, config=self.config) as session:
            await session.send_client_content(
                turns=t.Content(role="user", parts=[t.Part(text=f"Script:\n{text}")]), turn_complete=True)
            async for msg in session.receive():
                sc = msg.server_content
                if not sc:
                    continue
                for part in (sc.model_turn.parts if sc.model_turn else None) or []:
                    if part.inline_data and part.inline_data.data:
                        yield gemini_pcm(part.inline_data)
                # 音頻生成完就結束；turn_complete 要再等約 3 秒（模型按實時播放速度等待），不必等
                if sc.generation_complete or sc.turn_complete:
                    return


class OpenAITTS(TTSProvider):
    # OpenAI 語音合成（gpt-4o-mini-tts 等），instructions 是獨立參數，不會被念出來。
    def __init__(self, model="gpt-4o-mini-tts", voice="coral", instructions="", speed=None,
                 base_url=None, api_key_env="OPENAI_API_KEY", **_):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(api_key=os.getenv(api_key_env), base_url=base_url)
        self.args = {"model": model, "voice": voice, "response_format": "pcm"}   # pcm：24kHz 16-bit 單聲道
        if instructions:
            self.args["instructions"] = instructions
        if speed:
            self.args["speed"] = speed

    async def stream(self, text):
        async with self.client.audio.speech.with_streaming_response.create(input=text, **self.args) as resp:
            async for chunk in resp.iter_bytes(4800):
                yield chunk


STT_CLASSES = {"gemini": GeminiSTT, "openai": OpenAISTT}
TTS_CLASSES = {"gemini": GeminiTTS, "gemini_live": GeminiLiveTTS, "openai": OpenAITTS}


def _make(classes, cfg, **extra):
    # 除 provider 外的字段都作為構造參數傳入（同 builder.make_provider）。
    args = {k: v for k, v in cfg.items() if k != "provider"}
    return classes[cfg["provider"]](**args, **extra)


def make_stt(voice_cfg) -> STTProvider:
    return _make(STT_CLASSES, voice_cfg["stt"], silence_ms=voice_cfg["silence_ms"])


def tts_config(voice_cfg, language) -> dict:
    # tts 配置，用 tts_by_language 裡當前階段語言的字段覆蓋。
    return {**voice_cfg["tts"], **voice_cfg["tts_by_language"].get(language, {})}


def make_tts(cfg) -> TTSProvider:
    return _make(TTS_CLASSES, cfg)


# =====================================================================
# 課堂錄音：學生說話（收音時）和老師說話按時間順序寫進同一個文件
# =====================================================================

class Recorder:
    # 往 data/audio/{課堂 id}.pcm 追加音頻。
    # 所有 Recorder 共用一把鎖：刷新頁面重連時，新舊兩個通話可能短暫地同時寫同一個文件。
    lock = threading.Lock()

    def __init__(self, session_id, audio_dir=None):
        self.path = Path(audio_dir or AUDIO_DIR) / f"{session_id}.pcm"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add_mic(self, pcm):
        self._write(resample(pcm, MIC_RATE, TTS_RATE))

    def add_teacher(self, pcm):
        self._write(pcm)

    def _write(self, pcm):
        with self.lock, open(self.path, "ab") as f:
            f.write(pcm)


def recording_path(session_id, audio_dir=None) -> Path | None:
    # 課堂錄音的 WAV 文件；錄音有更新時重新從 PCM 生成。沒有錄音返回 None。
    pcm = Path(audio_dir or AUDIO_DIR) / f"{session_id}.pcm"
    wav = pcm.with_suffix(".wav")
    if pcm.exists() and (not wav.exists() or wav.stat().st_mtime < pcm.stat().st_mtime):
        tmp = wav.with_suffix(".tmp")
        with open(pcm, "rb") as src, wave.open(str(tmp), "wb") as dst:
            dst.setnchannels(1)
            dst.setsampwidth(2)
            dst.setframerate(TTS_RATE)
            while chunk := src.read(1 << 20):
                dst.writeframes(chunk)
        os.replace(tmp, wav)
    return wav if wav.exists() else None


def recording_peaks(wav: Path, bars=WAVE_BARS) -> list[int]:
    """課堂錄音整段的波紋（課後播放卡片用）：分成 bars 段，每段的峰值 0–100。
    按各段峰值的 98 分位歸一，偶爾一下很大的聲音（喀聲）不會把其他段壓得看不見。
    算一次存成 {課堂 id}.peaks.json，錄音更新後重算。"""
    cache = wav.with_suffix(".peaks.json")
    stamp = wav.stat().st_mtime_ns
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data["stamp"] == stamp and len(data["peaks"]) == bars:
            return data["peaks"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    with wave.open(str(wav), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    peaks = [0] * bars
    if pcm.size:
        amp = np.abs(pcm.astype(np.int32))
        starts = np.minimum(np.linspace(0, pcm.size, bars, endpoint=False).astype(int), pcm.size - 1)
        top = np.maximum.reduceat(amp, starts).astype(float)
        scale = max(float(np.percentile(top, 98)), 1.0)
        peaks = [int(v) for v in np.round(np.minimum(top / scale, 1.0) * 100)]
    cache.write_text(json.dumps({"stamp": stamp, "peaks": peaks}), encoding="utf-8")
    return peaks


# =====================================================================
# 通話管理
# =====================================================================

_calls: dict[int, "VoiceCall"] = {}   # 正在連接的語音課
_spoken: dict[int, int] = {}          # 每節課已經念到 view 的第幾條（刷新頁面重連後不重念）


def prepare(session):
    # 新開的語音課：從第一條（開場白）開始念。沒調用過的課（例如重啟後打開）重連時只念新的話。
    _spoken[session.id] = 0


def wait_finished(session_id, timeout) -> bool:
    # 給 app.py 用（同步）：課堂結束後，等老師最後的話（總結、道別）播完再切換頁面。
    call = _calls.get(session_id)
    return call.finished.wait(timeout) if call else True


class VoiceCall:
    """一節語音課的通話。run() 是主循環：有老師的新話就念；都念完、播完後打開語音識別收音；
    識別到學生說完一句，就在線程裡調用 session.ask()。stt / tts_factory / recorder 可注入，測試時用假的。"""

    def __init__(self, session, send_json, send_bytes, stt=None, tts_factory=None, recorder=None,
                 poll=0.1, clock=time.monotonic):
        self.session, self.send_json, self.send_bytes = session, send_json, send_bytes
        voice_cfg = session.cfg["voice"]
        self.stt = stt or make_stt(voice_cfg)
        self._tts_factory = tts_factory or (lambda lang: make_tts(tts_config(voice_cfg, lang)))
        self._tts: dict[str, TTSProvider] = {}
        self.recorder = recorder or Recorder(session.id)
        self.poll, self.clock = poll, clock
        self.spoken = _spoken.get(session.id, len(session.view))
        self.next_id = 0
        self.unplayed: dict[int, float] = {}   # 已發出、瀏覽器還沒播完的老師語音：id → 最晚播完時刻
        self.stream: STTStream | None = None   # 正在收音的識別流
        self.listening = False
        self.busy = False                      # 學生說完一句，老師正在糾錯和回覆
        self.closed = False
        self.failing: set[str] = set()         # 正在出錯（頁面上顯示著提示）的語音服務：stt / tts
        self.finished = threading.Event()      # run() 結束（下課播完 / 斷線）
        self._final_task = None

    # ---------------- 主循環 ----------------

    async def run(self):
        try:
            while not self.closed:
                entry = self._next_entry()
                if entry is not None:
                    await self._stop_listening()
                    await self._speak(entry)
                    continue
                now = self.clock()
                for mid in [m for m, deadline in self.unplayed.items() if deadline < now]:
                    log.warning("瀏覽器沒有回報第 %s 條語音播完，按超時處理", mid)
                    del self.unplayed[mid]
                # 老師還在生成 / 還在播，或者學生的話還在處理：先不收音
                if self.busy or self.unplayed or self.session._lock.locked():
                    await asyncio.sleep(self.poll)
                    continue
                if self.session.ended:
                    await self.send_json({"type": "ended"})
                    break
                if not self.listening:
                    await self._start_listening()
                await asyncio.sleep(self.poll)
        except Exception:
            if not self.closed:
                log.exception("語音通話出錯")
        finally:
            self.closed = True
            await self._stop_listening(notify=False)
            self.finished.set()

    def close(self):
        self.closed = True

    def _next_entry(self):
        # 下一條要念的老師訊息。還沒說完（沒有 done）的要等，保證按順序念；報告卡片和學生的話跳過。
        view = self.session.view
        while self.spoken < len(view):
            m = view[self.spoken]
            if m["role"] == "assistant" and m["kind"] != "report":
                if not m.get("done"):
                    return None
                self._advance()
                return m
            self._advance()
        return None

    def _advance(self):
        self.spoken += 1
        _spoken[self.session.id] = self.spoken

    # ---------------- 老師說話 ----------------

    def _tts_for(self, language) -> TTSProvider:
        if language not in self._tts:
            self._tts[language] = self._tts_factory(language)
        return self._tts[language]

    async def _speak(self, entry):
        text = speakable(entry["content"])
        if not text:
            return
        self.next_id += 1
        mid = self.next_id
        self.unplayed[mid] = float("inf")
        await self.send_json({"type": "speak", "id": mid})
        sent, rest = 0, b""
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(TTS_IDLE_TIMEOUT) as idle:
                async for chunk in self._tts_for(self.session.current_phase()["language"]).stream(text):
                    idle.reschedule(loop.time() + TTS_IDLE_TIMEOUT)   # 每收到一塊就重新計時
                    chunk = rest + chunk
                    cut = len(chunk) - len(chunk) % 2   # 16-bit 對齊，半個採樣留到下一塊
                    chunk, rest = chunk[:cut], chunk[cut:]
                    if chunk:
                        await self.send_bytes(chunk)
                        self.recorder.add_teacher(chunk)
                        sent += len(chunk)
        except Exception as e:
            if self.closed:
                raise
            log.exception("語音合成失敗：%s", text[:60])
            await self._notify("tts", "The teacher's voice could not be played", e)
        else:
            await self._recovered("tts")
        await self.send_json({"type": "speak_end", "id": mid})
        if mid in self.unplayed:
            # 瀏覽器正常會在播完時回報；這裡給個最晚時刻（音頻時長 + 寬限），防止一直等
            self.unplayed[mid] = self.clock() + sent / 2 / TTS_RATE + PLAYED_GRACE_SECONDS

    def on_message(self, msg: dict):
        if msg.get("type") == "played":
            self.unplayed.pop(msg.get("id"), None)

    async def _notify(self, service, what, error):
        # 語音服務（stt / tts）出錯時在頁面上提示學生（Listening / Speaking 下方），否則學生只會覺得老師沒聲音
        if isinstance(error, TimeoutError):
            reason = "the voice service did not respond in time"
        elif "429" in str(error) or "RESOURCE_EXHAUSTED" in str(error) or "quota" in str(error).lower():
            reason = "the voice service quota is used up (check your API plan)"
        else:
            reason = "the voice service returned an error (see the server log)"
        self.failing.add(service)
        await self.send_json({"type": "note", "text": f"{what}: {reason}."})

    async def _recovered(self, service):
        # 出錯的語音服務恢復正常：都恢復後清掉頁面上的提示
        if service in self.failing:
            self.failing.discard(service)
            if not self.failing:
                await self.send_json({"type": "note", "text": ""})

    # ---------------- 學生說話 ----------------

    async def _start_listening(self):
        try:
            self.stream = await self.stt.open(stt_languages(self.session.current_phase()["language"]))
        except Exception as e:
            log.exception("打開語音識別失敗，稍後重試")
            await self._notify("stt", "Speech recognition is not available", e)
            await asyncio.sleep(5)
            return
        self.listening = True
        await self._recovered("stt")
        self._final_task = asyncio.create_task(self._await_final(self.stream))
        await self.send_json({"type": "listen"})

    async def _stop_listening(self, notify=True):
        if not self.listening:
            return
        self.listening = False
        stream, self.stream = self.stream, None
        task, self._final_task = self._final_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if notify:
            await self.send_json({"type": "mute"})
        try:
            await stream.close()
        except Exception:
            log.debug("關閉語音識別流出錯", exc_info=True)

    async def on_audio(self, pcm: bytes):
        # 麥克風音頻：只在收音時處理（錄音 + 送去識別），老師說話時收到的直接丟掉。
        if not self.listening or self.stream is None:
            return
        self.recorder.add_mic(pcm)
        try:
            await self.stream.send(pcm)
        except Exception:
            log.exception("發送音頻到語音識別失敗")
            await self._stop_listening()

    async def _await_final(self, stream):
        try:
            text = (await stream.final() or "").strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("語音識別失敗，重新收音")
            text = ""
        if stream is not self.stream:
            return
        self.busy = bool(text)   # 先標記，避免主循環在 ask 開始前又打開收音
        await self._stop_listening()
        if not text:
            return
        log.info("學生（語音）：%s", text)
        try:
            await asyncio.to_thread(self._ask, text)
        finally:
            self.busy = False

    def _ask(self, text):
        try:
            for _ in self.session.ask(text, via="voice"):
                pass
        except Exception:
            log.exception("處理學生發言失敗")


async def serve(ws, session_id: int):
    # app.py 的 WebSocket 入口（ws 是 Starlette WebSocket）：收麥克風音頻和播放回報，驅動 VoiceCall。
    await ws.accept()
    try:
        session = builder.open_session(session_id)
    except KeyError:
        await ws.close()
        return
    if session.mode != "voice" or session.ended:
        await ws.send_json({"type": "ended"})
        await ws.close()
        return
    old = _calls.get(session_id)
    if old is not None:   # 同一節課重新連接（例如刷新頁面）：停掉舊的
        old.close()
        await asyncio.to_thread(old.finished.wait, 5)
    try:
        call = VoiceCall(session, ws.send_json, ws.send_bytes)
    except Exception:
        log.exception("創建語音通話失敗")
        await ws.close()
        return
    _calls[session_id] = call
    runner = asyncio.create_task(call.run())
    try:
        while not call.closed:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                await call.on_audio(msg["bytes"])
            elif msg.get("text"):
                call.on_message(json.loads(msg["text"]))
    except Exception:
        log.debug("語音 WebSocket 斷開", exc_info=True)
    finally:
        call.close()
        await runner
        if _calls.get(session_id) is call:
            del _calls[session_id]
        try:
            await ws.close()
        except Exception:
            pass
