# voice.py 測試：用假語音識別、假語音合成模擬瀏覽器，不調用任何 API。

import asyncio
import threading
import wave

import pytest

# test_builder 會把 src 加進 sys.path；clear_sessions 是自動啟用的 fixture
from test_builder import Clock as LessonClock, FakeLLM, clear_sessions, drain, make_agent, voice_config  # noqa: F401

import builder  # noqa: E402
import voice  # noqa: E402


@pytest.fixture(autouse=True)
def clear_voice():
    voice._calls.clear()
    voice._spoken.clear()
    yield
    voice._calls.clear()
    voice._spoken.clear()


# ---------------- 工具函數 ----------------

def test_speakable_strips_markdown():
    assert voice.speakable("Quick tip: we say 'I **went** to Beijing.'") == "Quick tip: we say 'I went to Beijing.'"
    assert voice.speakable("  `x` # y  ") == "x  y"


def test_stt_languages():
    assert voice.stt_languages("en") == ["en-US"]
    assert voice.stt_languages("zh-TW") == ["zh-TW", "en-US"]


def test_resample_keeps_duration():
    pcm = b"\x00\x01" * 16000   # 1 秒 16kHz
    assert len(voice.resample(pcm, 16000, 24000)) == 24000 * 2
    assert voice.resample(pcm, 16000, 16000) is pcm


def test_tts_config_overrides_by_language():
    cfg = builder.parse_voice_config({
        "stt": {"provider": "gemini", "model": "s"},
        "tts": {"provider": "gemini", "model": "t", "voice": "Kore", "instructions": "en style"},
        "tts_by_language": {"zh-TW": {"instructions": "中文語氣"}},
    })
    assert voice.tts_config(cfg, "en")["instructions"] == "en style"
    assert voice.tts_config(cfg, "zh-TW") == {"provider": "gemini", "model": "t", "voice": "Kore",
                                             "instructions": "中文語氣"}


def test_recording_wav_built_and_refreshed(tmp_path):
    rec = voice.Recorder(7, tmp_path)
    rec.add_mic(b"\x00\x00" * 16000)        # 學生 1 秒（16kHz → 24kHz）
    rec.add_teacher(b"\x00\x00" * 12000)    # 老師 0.5 秒
    path = voice.recording_path(7, tmp_path)
    with wave.open(str(path)) as w:
        assert w.getframerate() == 24000 and w.getnframes() / 24000 == pytest.approx(1.5)
    assert voice.recording_path(99, tmp_path) is None


def test_recording_peaks_cached_and_refreshed(tmp_path):
    rec = voice.Recorder(7, tmp_path)
    rec.add_teacher(b"\x00\x00" * 24000 + b"\x00\x40" * 24000)   # 1 秒靜音 + 1 秒有聲
    wav = voice.recording_path(7, tmp_path)
    peaks = voice.recording_peaks(wav, bars=10)
    assert peaks == [0] * 5 + [100] * 5
    assert wav.with_suffix(".peaks.json").exists()
    assert voice.recording_peaks(wav, bars=10) == peaks      # 讀快取
    rec.add_teacher(b"\x00\x40" * 48000)                      # 錄音更新後重算
    wav = voice.recording_path(7, tmp_path)
    assert voice.recording_peaks(wav, bars=10) == [0] * 2 + [100] * 8


# ---------------- 假語音服務 ----------------

class FakeStream(voice.STTStream):
    def __init__(self, languages):
        self.languages, self.sent, self.closed = languages, [], False
        self.text = asyncio.Queue()

    async def send(self, pcm):
        self.sent.append(pcm)

    async def final(self):
        return await self.text.get()

    async def close(self):
        self.closed = True


class FakeSTT(voice.STTProvider):
    def __init__(self):
        self.streams = []

    async def open(self, languages):
        self.streams.append(FakeStream(languages))
        return self.streams[-1]


class FakeTTS(voice.TTSProvider):
    def __init__(self, language, log):
        self.language, self.log = language, log

    async def stream(self, text):
        self.log.append((self.language, text))
        yield b"\x01"                    # 故意拆成奇數字節，檢查 16-bit 對齊
        yield b"\x02\x03\x04" * 1600


class Browser:
    # 模擬瀏覽器：記錄服務端發來的消息，可以回報播完。
    def __init__(self):
        self.json, self.bytes = [], []

    async def send_json(self, msg):
        self.json.append(msg)

    async def send_bytes(self, data):
        self.bytes.append(data)

    def types(self):
        return [m["type"] for m in self.json]


async def until(cond, timeout=3):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("timeout")
        await asyncio.sleep(0.005)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def voice_lesson(chat=None, **kw):
    cfg = voice_config()
    cfg["greeting"] = "Hi! How are you?"
    cfg["max_bubbles"] = 3
    session = make_agent(config=cfg, chat=chat or FakeLLM(reply="Oh nice!\n\nWhat did you do?"),
                         **kw).start_session("Food", mode="voice")
    voice.prepare(session)
    drain(session.greet())
    return session


def make_call(session, tmp_path, clock=None):
    browser, stt, said = Browser(), FakeSTT(), []
    call = voice.VoiceCall(session, browser.send_json, browser.send_bytes, stt=stt,
                           tts_factory=lambda lang: FakeTTS(lang, said),
                           recorder=voice.Recorder(session.id, tmp_path), poll=0.005,
                           clock=clock or Clock())
    return call, browser, stt, said


def ack_all(call, browser):
    for m in browser.json:
        if m["type"] == "speak_end":
            call.on_message({"type": "played", "id": m["id"]})


# ---------------- 通話流程 ----------------

def test_lesson_flow(tmp_path):
    async def main():
        session = voice_lesson()
        call, browser, stt, said = make_call(session, tmp_path)
        task = asyncio.create_task(call.run())

        # 1. 先念開場白；老師說話時不收音
        await until(lambda: "speak_end" in browser.types())
        assert said == [("en", "Hi! How are you?")]
        assert all(len(b) % 2 == 0 for b in browser.bytes)
        await asyncio.sleep(0.05)
        assert "listen" not in browser.types()       # 瀏覽器還沒播完
        await call.on_audio(b"\x00\x00" * 160)       # 這時的麥克風音頻被丟掉
        assert stt.streams == []

        # 2. 播完 → 收音
        ack_all(call, browser)
        await until(lambda: browser.types()[-1:] == ["listen"])
        await call.on_audio(b"\x00\x00" * 160)
        assert stt.streams[0].sent == [b"\x00\x00" * 160]

        # 3. 學生說完一句 → 停止收音 → 老師糾錯並回覆（兩條氣泡逐條念）
        await stt.streams[0].text.put("I go to school yesterday.")
        await until(lambda: len(said) == 3)
        assert stt.streams[0].closed and "mute" in browser.types()
        assert [m for m in session.view if m["role"] == "user"][0]["via"] == "voice"
        assert [t for _, t in said[1:]] == ["Oh nice!", "What did you do?"]

        # 4. 都播完才重新收音（新的識別流）
        n_listen = browser.types().count("listen")
        ack_all(call, browser)
        await until(lambda: browser.types().count("listen") == n_listen + 1)
        assert len(stt.streams) == 2

        # 5. 學生提前結束：總結講解用中文聲線念，報告卡片不念；播完後下課
        await asyncio.to_thread(drain, session.finish())
        await until(lambda: len(said) == 4)
        assert said[3][0] == "zh-TW" and any(m["kind"] == "report" for m in session.view)
        ack_all(call, browser)
        await until(lambda: "ended" in browser.types())
        await task
        assert call.finished.is_set()
        return session.id

    sid = asyncio.run(main())
    # 錄音 = 收音時學生的音頻（160 個採樣 → 24kHz 240 個）+ 老師 4 條語音（每條對齊後 2400 個採樣）
    with wave.open(str(voice.recording_path(sid, tmp_path))) as w:
        assert w.getnframes() == 240 + 4 * 2400


def test_empty_transcript_keeps_listening(tmp_path):
    async def main():
        session = voice_lesson()
        call, browser, stt, said = make_call(session, tmp_path)
        task = asyncio.create_task(call.run())
        await until(lambda: "speak_end" in browser.types())
        ack_all(call, browser)
        await until(lambda: len(stt.streams) == 1 and call.listening)
        await stt.streams[0].text.put("   ")             # 噪音、沒聽到話
        await until(lambda: len(stt.streams) == 2 and call.listening)
        assert not any(m["role"] == "user" for m in session.view)
        call.close()
        await task

    asyncio.run(main())


def test_reconnect_does_not_repeat_old_messages(tmp_path):
    async def main():
        session = voice_lesson()
        voice._spoken.clear()          # 模擬重啟後打開進行中的語音課
        call, browser, stt, said = make_call(session, tmp_path)
        task = asyncio.create_task(call.run())
        await until(lambda: call.listening)
        assert said == []
        call.close()
        await task

    asyncio.run(main())


def test_spoken_position_survives_page_refresh(tmp_path):
    async def main():
        session = voice_lesson()
        call, browser, _, said = make_call(session, tmp_path)
        task = asyncio.create_task(call.run())
        await until(lambda: "speak_end" in browser.types())
        call.close()
        await task
        call2, browser2, _, said2 = make_call(session, tmp_path)   # 刷新頁面重連
        task2 = asyncio.create_task(call2.run())
        await until(lambda: call2.listening)
        assert said2 == []
        call2.close()
        await task2

    asyncio.run(main())


def test_nudge_is_spoken(tmp_path):
    async def main():
        clock = LessonClock()
        session = voice_lesson(clock=clock)
        call, browser, stt, said = make_call(session, tmp_path)
        task = asyncio.create_task(call.run())
        await until(lambda: "speak_end" in browser.types())
        ack_all(call, browser)
        await until(lambda: call.listening)
        clock.t += 31                                     # 學生沉默 → 關懷
        await asyncio.to_thread(lambda: drain(session.tick().chunks))
        await until(lambda: len(said) == 2)
        assert not call.listening and stt.streams[0].closed
        call.close()
        await task

    asyncio.run(main())


def test_missing_played_ack_times_out(tmp_path):
    async def main():
        clock = Clock()
        session = voice_lesson()
        call, browser, stt, said = make_call(session, tmp_path, clock=clock)
        task = asyncio.create_task(call.run())
        await until(lambda: "speak_end" in browser.types())
        await asyncio.sleep(0.05)
        assert not call.listening
        clock.t += 60                                     # 瀏覽器一直沒回報
        await until(lambda: call.listening)
        call.close()
        await task

    asyncio.run(main())


class QuotaTTS(voice.TTSProvider):
    async def stream(self, text):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")
        yield b""


def test_tts_failure_shows_note_and_lesson_goes_on(tmp_path):
    async def main():
        session = voice_lesson()
        call, browser, stt, _ = make_call(session, tmp_path)
        call._tts_factory = lambda lang: QuotaTTS()
        task = asyncio.create_task(call.run())
        await until(lambda: "speak_end" in browser.types())
        notes = [m["text"] for m in browser.json if m["type"] == "note"]
        assert notes and "quota" in notes[0]
        ack_all(call, browser)
        await until(lambda: call.listening)            # 沒聲音也繼續上課
        assert [m for m in browser.json if m["type"] == "note"][-1]["text"] == notes[0]   # 提示保留
        call.close()
        await task

    asyncio.run(main())


class HangingTTS(voice.TTSProvider):
    async def stream(self, text):
        yield b"\x00\x00" * 100
        await asyncio.sleep(3600)   # 連接卡住，再也不返回


def test_hanging_tts_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "TTS_IDLE_TIMEOUT", 0.05)
    async def main():
        session = voice_lesson()
        call, browser, stt, _ = make_call(session, tmp_path)
        call._tts_factory = lambda lang: HangingTTS()
        task = asyncio.create_task(call.run())
        await until(lambda: "speak_end" in browser.types())
        assert "did not respond" in [m for m in browser.json if m["type"] == "note"][0]["text"]
        ack_all(call, browser)
        await until(lambda: call.listening)            # 不會一直卡在老師說話
        call.close()
        await task

    asyncio.run(main())


def test_wait_finished_without_call():
    assert voice.wait_finished(123, 0.01)


def test_wait_finished_waits_for_call(tmp_path):
    session = voice_lesson()
    call, *_ = make_call(session, tmp_path)
    voice._calls[session.id] = call
    threading.Timer(0.05, call.finished.set).start()
    assert voice.wait_finished(session.id, 2)
