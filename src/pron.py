"""發音課：Azure 發音評估、Azure 語音合成（示範）、錯誤本、短文生成、課堂流程。

流程（每篇文章是一輪 round，「挑戰更難的一篇」在同一節課裡開新一輪）：
    ① 生成短文   開課時從錯誤本挑最常錯的字和音，讓老師的 LLM 寫進約 150 字的短文
    ② AI 示範    瀏覽器逐句請求 /tts 播放                                        step = demo
    ③ 讀全文     逐句錄音、背景評分，讀完一次顯示（這是「真實能力」，只用它更新錯誤本）  step = read1
    ④ 練習       plan_practice() 按 ③ 的結果決定：練最差的句子 / 只練字 / 不用練    step = practice / choice
    ⑤ 再讀全文   同 ③，讀完顯示和 ③ 的對比，課堂結束                              step = read2 → done
    結果頁       分數和對比、老師示範和兩次錄音重聽（reading_audio）、右下角和老師聊天（chat）

兩個獨立的「等級」：文章難度用 CEFR（level，挑戰更難時升一級）；發音練習重點用 stage
（1 單音 / 2 字重音、連音 / 3 句子重音、語調），由最近幾節課 ③ 的分數決定。
Azure 的分數不對應 CEFR，只用來看進步趨勢。

錯誤本（update_error_book）記錯過的字和音素，連續 3 篇沒問題就移除；新文章優先放出錯多、最近還在錯的（pick_focus）。

瀏覽器（static/pron.js）通過 app.py 的 HTTP 接口調用這裡（Course 的方法），狀態都存數據庫，刷新頁面可以續上。
文件分區：常數 → 文字工具 → Azure → 錯誤本 → ④ 練習計劃 → LLM 提示詞 → 課堂流程（Course）。

Azure 配置（.env）：AZURE_SPEECH_KEY、AZURE_SPEECH_REGION（例如 eastasia）。
發音評估用短音頻 REST 接口（每段 ≤ 60 秒，錄音上限 30 秒），不需要 Azure SDK。
"""

import base64
import datetime
import hashlib
import io
import json
import logging
import os
import re
import threading
import time
import wave
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

import builder

log = logging.getLogger("vtutor.pron")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TTS_DIR = DATA_DIR / "pron_tts"   # 示範語音快取：{hash}.mp3
REC_DIR = DATA_DIR / "pron"       # 學生錄音：{課堂 id}/{評分記錄 id}.wav

KEY_ENV, REGION_ENV = "AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION"

# ---- 評分 ----
GOOD, FAIR = 80, 60            # 字的顏色：>= 80 綠，60-79 黃，< 60 紅（pron.js 的 cls() 用同樣的門檻）
SUBSTITUTED = 30               # 字裡某個音素低於這個分數，算明顯唸成了別的音（如 th 唸成 f），整個字標紅
# ④ 裡唸出來的字少於這個比例（明顯沒唸完 / 錄錯）不計次數。不用 Azure 的完整度：它把唸錯的字也算成沒唸，
# 單字練習一唸錯完整度就是 0，學生越唸不好越永遠不計次數，會卡在同一個字
MIN_SAID_RATIO = 0.5
MAX_UNCOUNTED = 2              # 同一個目標連續幾次不計次數之後，再錄就一定計入（保證不會卡住）

# ---- 錯誤本 ----
STREAK_TO_REMOVE = 3           # 錯誤本裡的字 / 音素在之後的文章裡連續唸對幾次就移除
PHONEME_WEAK_MISSES = 2        # 一篇裡某個音錯幾次以上算「這次錯了」
PHONEME_WEAK_RATE = 0.25       # 或者錯誤率達到這個比例

# ---- 錄音、示範、聊天 ----
MAX_WAV_BYTES = 4 * 1024 * 1024   # 一段學生錄音的上限（30 秒 16kHz WAV 約 1MB）
MAX_TTS_CHARS = 1000           # 示範文字上限（都是逐句合成，一句遠用不到這麼長，只防止濫用接口）
REC_GAP_SECONDS = 0.4          # 整篇錄音裡句與句之間的靜音（pron.js 的 REC_GAP 要一致）
MAX_CHAT_CHARS = 500           # 學生一則聊天訊息的長度上限
CHAT_HISTORY = 20              # 帶給 LLM 的最近幾則聊天
TREND_POINTS = 7               # 結果頁小走勢圖顯示最近幾次

# ---- 等級和顯示文字 ----
LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]
STAGES = {1: "單音", 2: "字重音和連音", 3: "句子重音和語調"}   # 給 LLM 的提示詞用
# 頁面上顯示的固定文字用英文（教學內容如老師回饋仍是中文）
STAGE_LABELS = {1: "Individual sounds", 2: "Word stress & linking", 3: "Sentence stress & intonation"}
STEP_LABELS = {"demo": "2 · Listen", "read1": "3 · Read", "practice": "4 · Practice", "choice": "4 · Practice",
               "read2": "5 · Read again", "done": "Done"}

# 不放進錯誤本、不拿出來單獨練的功能詞（太短，評分噪聲大）
FUNCTION_WORDS = frozenset(
    "a an the to of and or but in on at for is are was were be am i you he she it we they my your his her "
    "its our their me him us them this that with as by from do does did not no so if".split())


class PronError(Exception):
    # 顯示在發音課面板上的錯誤（英文），app.py 轉成 HTTP 400。
    pass


# =====================================================================
# 文字工具
# =====================================================================

def norm(word: str) -> str:
    # 比較用的形式：小寫、去掉標點（保留撇號，如 don't）。
    return re.sub(r"[^a-z0-9']", "", word.lower().replace("’", "'")).strip("'")


def tokens(text: str) -> list[str]:
    # 句子按空格切成顯示用的詞（帶標點），去掉純標點。
    return [t for t in text.split() if norm(t)]


def clean_word(token: str) -> str:
    # 顯示用的單字：去掉前後標點，保留大小寫。
    return re.sub(r"^[^\w']+|[^\w']+$", "", token)


def base_level(level: str) -> str:
    # 老師 JSON 的 level（如 "A2" 或 "B1-B2"）→ 一個 CEFR 等級。
    m = builder.LEVEL_RE.search(level or "")
    return m.group() if m else "B1"


def level_up(level: str) -> str:
    # 挑戰更難的一篇：CEFR 升一級，C2 封頂。
    i = LEVELS.index(level) if level in LEVELS else LEVELS.index("B1")
    return LEVELS[min(i + 1, len(LEVELS) - 1)]


# =====================================================================
# Azure：發音評估 + 語音合成
# =====================================================================

def check_ready(agent):
    # 開發音課前檢查：老師配置了 pronunciation，.env 填了 Azure Key 和 Region。
    if not agent.has_pron:
        raise builder.AgentConfigError(f"{agent.title} 沒有配置 pronunciation，不能上發音課")
    missing = [k for k in (KEY_ENV, REGION_ENV) if not os.getenv(k)]
    if missing:
        raise builder.AgentConfigError(f"發音課需要 Azure Speech，請在 .env 填寫 {' 和 '.join(missing)}")


class AzureSpeech:
    """Azure Speech 的 REST 接口。assess() 返回 parse_assessment() 的統一結構，synthesize() 返回 mp3。
    測試時換成假的（實現同樣兩個方法即可）。"""

    def __init__(self, language="en-US", key=None, region=None, timeout=30):
        self.key, self.region = key or os.getenv(KEY_ENV), region or os.getenv(REGION_ENV)
        if not self.key or not self.region:
            raise PronError(f"Azure Speech is not configured: set {KEY_ENV} and {REGION_ENV} in .env")
        self.language, self.timeout = language, timeout
        self.prosody = True   # 這個區域 / 語言不支持韻律評估時，第一次出錯後關掉

    def _check(self, resp, what):
        # HTTP 錯誤轉成給學生看的 PronError（常見的 Key / 額度問題給明確提示）
        if resp.status_code in (401, 403):
            raise PronError(f"Azure {what} failed: invalid key or wrong region (HTTP {resp.status_code})")
        if resp.status_code == 429:
            raise PronError(f"Azure {what} failed: too many requests or quota used up (HTTP 429)")
        if resp.status_code >= 400:
            raise PronError(f"Azure {what} failed (HTTP {resp.status_code}): {resp.text[:200]}")

    def assess(self, wav: bytes, reference: str) -> dict:
        # 發音評估：參考文字和評估選項 base64 後放在 Pronunciation-Assessment header，錄音放在 body
        url = (f"https://{self.region}.stt.speech.microsoft.com/speech/recognition/conversation/"
               "cognitiveservices/v1")
        params = {"ReferenceText": reference, "GradingSystem": "HundredMark", "Granularity": "Phoneme",
                  "Dimension": "Comprehensive", "EnableMiscue": True, "PhonemeAlphabet": "IPA"}
        if self.prosody:
            params["EnableProsodyAssessment"] = True
        try:
            resp = self._post_assess(url, params, wav)
            if resp.status_code == 400 and self.prosody:
                log.warning("Azure 不接受韻律評估參數，改為不評韻律：%s", resp.text[:200])
                self.prosody = False
                params.pop("EnableProsodyAssessment")
                resp = self._post_assess(url, params, wav)
        except httpx.HTTPError as e:
            raise PronError(f"Cannot reach Azure pronunciation assessment: {e}") from e
        self._check(resp, "pronunciation assessment")
        return parse_assessment(resp.json(), reference)

    def _post_assess(self, url, params, wav):
        header = base64.b64encode(json.dumps(params).encode()).decode()
        return httpx.post(url, params={"language": self.language, "format": "detailed"}, content=wav,
                          timeout=self.timeout,
                          headers={"Ocp-Apim-Subscription-Key": self.key, "Pronunciation-Assessment": header,
                                   "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
                                   "Accept": "application/json"})

    def synthesize(self, text: str, voice: str, rate: str | None = None) -> bytes:
        # 語音合成（mp3）；rate 是慢速示範的 SSML 語速，如 "-30%"
        lang = "-".join(voice.split("-")[:2])   # 聲線名的前兩段就是語言，如 en-US-AvaNeural → en-US
        body = escape(text)
        if rate:
            body = f'<prosody rate="{escape(rate)}">{body}</prosody>'
        ssml = (f'<speak version="1.0" xml:lang="{lang}"><voice name="{escape(voice)}">{body}</voice></speak>')
        try:
            resp = httpx.post(f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1",
                              content=ssml.encode("utf-8"), timeout=self.timeout,
                              headers={"Ocp-Apim-Subscription-Key": self.key,
                                       "Content-Type": "application/ssml+xml",
                                       "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
                                       "User-Agent": "VTutor"})
        except httpx.HTTPError as e:
            raise PronError(f"Cannot reach Azure speech synthesis: {e}") from e
        self._check(resp, "speech synthesis")
        return resp.content


def _pa(obj) -> dict:
    # REST 接口把分數直接放在對象上，SDK 的 JSON 放在 PronunciationAssessment 裡，兩種都認。
    return obj.get("PronunciationAssessment") or obj


def _num(x):
    # Azure 的分數 → 保留一位小數；缺少或不是數字時返回 None
    return round(float(x), 1) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def word_color(accuracy, error, weakest=100) -> str:
    # weakest：字裡最低的音素分數。實測 th 唸成 f 時，整字仍有 94 分、/θ/ 只有 18 分，只看整字會漏掉，
    # 所以有音素明顯唸錯就標紅，有音素偏弱就最多算黃。
    if error in ("Omission", "Mispronunciation") or accuracy < FAIR or weakest < SUBSTITUTED:
        return "bad"
    return "fair" if accuracy < GOOD or weakest < FAIR else "good"


def _word(text, az) -> dict:
    # 原文的一個詞 + 對應的 Azure 評分（az 為 None 表示學生漏唸）。
    if az is None:
        return {"text": text, "accuracy": 0, "error": "Omission", "phonemes": [], "prosody_errors": [],
                "color": "bad"}
    p = _pa(az)
    error = p.get("ErrorType") or "None"
    accuracy = _num(p.get("AccuracyScore")) or 0
    phonemes = [{"phoneme": ph.get("Phoneme", ""), "accuracy": _num(_pa(ph).get("AccuracyScore")) or 0}
                for ph in az.get("Phonemes") or []]
    # 字級韻律問題只取停頓（UnexpectedBreak / MissingBreak）。Monotone 是整句的標記（每個字都一樣），
    # 實測標準讀音也會帶，由 parse_assessment() 結合整句韻律分處理
    brk = ((az.get("Feedback") or {}).get("Prosody") or {}).get("Break") or {}
    prosody_errors = [e for e in brk.get("ErrorTypes") or [] if e != "None"]
    weakest = min((ph["accuracy"] for ph in phonemes), default=100)
    return {"text": text, "accuracy": accuracy, "error": error, "phonemes": phonemes,
            "prosody_errors": prosody_errors, "color": word_color(accuracy, error, weakest)}


def align(toks: list[str], az_words: list[dict]) -> list[dict]:
    # 把 Azure 的詞對回原文的詞。開了 EnableMiscue 後兩邊基本一一對應（漏唸的詞標 Omission），
    # 數字、連字號等寫法不同時，往後找兩個詞，找不到就按位置對。
    out, j = [], 0
    for t in toks:
        n = norm(t)
        k = next((k for k in range(j, min(j + 3, len(az_words))) if norm(az_words[k].get("Word", "")) == n), None)
        if k is None and j < len(az_words):
            k = j
        out.append(_word(t, az_words[k] if k is not None else None))
        if k is not None:
            j = k + 1
    return out


def parse_assessment(data: dict, reference: str) -> dict:
    """Azure 回傳 → 統一結構：
    {"score", "accuracy", "fluency", "completeness", "prosody"（不支持時 None）, "recognized",
     "monotone"（整句語調太平：Azure 標了 Monotone 且韻律分 < 80）,
     "words": [{"text", "accuracy", "error", "color", "phonemes": [{"phoneme", "accuracy"}], "prosody_errors"}]}
    words 和原文的詞一一對應（多唸的詞 Insertion 不顯示）。沒識別到聲音時全部算漏唸、0 分。"""
    toks = tokens(reference)
    nbest = (data.get("NBest") or [None])[0] if data.get("RecognitionStatus", "Success") == "Success" else None
    if not nbest:
        return {"score": 0, "accuracy": 0, "fluency": 0, "completeness": 0, "prosody": None, "recognized": "",
                "monotone": False, "words": [_word(t, None) for t in toks]}
    pa = _pa(nbest)
    az_words = [w for w in nbest.get("Words") or [] if _pa(w).get("ErrorType") != "Insertion"]
    words = align(toks, az_words)
    accuracy = _num(pa.get("AccuracyScore"))
    if accuracy is None:
        accuracy = round(sum(w["accuracy"] for w in words) / len(words), 1) if words else 0
    score = _num(pa.get("PronScore"))
    prosody = _num(pa.get("ProsodyScore"))
    flagged = any("Monotone" in ((((w.get("Feedback") or {}).get("Prosody") or {}).get("Intonation") or {})
                                 .get("ErrorTypes") or []) for w in az_words)
    return {"score": accuracy if score is None else score, "accuracy": accuracy,
            "fluency": _num(pa.get("FluencyScore")) or 0, "completeness": _num(pa.get("CompletenessScore")) or 0,
            "prosody": prosody, "recognized": nbest.get("Display") or "",
            "monotone": flagged and prosody is not None and prosody < GOOD, "words": words}


def summarize(results: list[dict | None]) -> dict:
    # 全文的分數：各句按詞數加權平均。
    rs = [r for r in results if r]

    def avg(key):
        vals = [(r[key], max(1, len(r["words"]))) for r in rs if r.get(key) is not None]
        return round(sum(v * w for v, w in vals) / sum(w for _, w in vals), 1) if vals else None
    return {k: avg(k) for k in ("score", "accuracy", "fluency", "completeness", "prosody")}


# =====================================================================
# 錯誤本
# =====================================================================

def phoneme_is_weak(misses: int, total: int) -> bool:
    # 一篇文章裡某個音算「這次錯了」：錯了 2 次以上，或錯誤率達到 25%。
    # 常見的音（如 /t/）一篇會出現很多次，偶爾失誤一次不算，否則永遠不會從錯誤本移除。
    return misses >= PHONEME_WEAK_MISSES or (misses > 0 and misses / total >= PHONEME_WEAK_RATE)


def update_error_book(db, results: list[dict], today: str) -> dict:
    """用 ③ 的結果更新錯誤本。錯的字：次數 +1、記下日期、連續唸對清零；
    錯誤本裡已有、這次唸對的字：連續唸對 +1，到 STREAK_TO_REMOVE 就移除。
    音素同樣處理：這次算錯（phoneme_is_weak）就加上錯的次數、記下日期、清零；錯誤本裡已有、這次沒問題的連續 +1，到 3 移除。
    功能詞（a、the 等）不記字，但其中的音素照樣統計。"""
    book = {w["word"]: w for w in db.get_pron_words()}
    ph_book = {p["phoneme"]: p for p in db.get_pron_phonemes()}
    wrong, right, ph_seen = set(), set(), {}   # ph_seen：音素 → [出現次數, 分數 < 60 的次數]
    for r in results:
        for w in r["words"]:
            for ph in w["phonemes"]:
                if ph["phoneme"]:
                    seen = ph_seen.setdefault(ph["phoneme"], [0, 0])
                    seen[0] += 1
                    seen[1] += ph["accuracy"] < FAIR
            n = norm(w["text"])
            if not n or n in FUNCTION_WORDS:
                continue
            if w["color"] == "bad":
                wrong.add(n)
            elif w["color"] == "good":
                right.add(n)
    rows, removed = [], []
    for n in sorted(wrong):
        old = book.get(n)
        rows.append({"word": n, "err_count": (old["err_count"] if old else 0) + 1, "last_wrong_at": today,
                     "correct_streak": 0})
    for n in sorted(right - wrong):
        old = book.get(n)
        if old is None:
            continue
        if old["correct_streak"] + 1 >= STREAK_TO_REMOVE:
            removed.append(n)
        else:
            rows.append({**old, "correct_streak": old["correct_streak"] + 1})
    ph_rows, ph_removed, ph_wrong = [], [], []
    for p, (total, misses) in sorted(ph_seen.items()):
        old = ph_book.get(p)
        if phoneme_is_weak(misses, total):
            ph_wrong.append(p)
            ph_rows.append({"phoneme": p, "err_count": (old["err_count"] if old else 0) + misses,
                            "last_wrong_at": today, "correct_streak": 0})
        elif old is not None:
            if old["correct_streak"] + 1 >= STREAK_TO_REMOVE:
                ph_removed.append(p)
            else:
                ph_rows.append({**old, "correct_streak": old["correct_streak"] + 1})
    db.update_pron_error_book(rows, removed, ph_rows, ph_removed)
    log.info("錯誤本：新錯的字 %s，移除的字 %s；錯的音 %s，移除的音 %s", sorted(wrong), removed, ph_wrong, ph_removed)


def pick_focus(words: list[dict], phonemes: list[dict], today: str, most: int) -> tuple[list[str], list[str]]:
    # 挑要放進新文章的字和 2 個音素：都是出錯次數多、最近還在錯的優先（每過一週權重減半左右）。
    d0 = datetime.date.fromisoformat(today)

    def priority(item):
        # 舊數據的音素沒有記錄日期，當作 30 天前
        days = (max(0, (d0 - datetime.date.fromisoformat(item["last_wrong_at"])).days)
                if item.get("last_wrong_at") else 30)
        return item["err_count"] / (1 + days / 7)
    ranked = sorted(words, key=priority, reverse=True)[:most]
    top_phonemes = [p["phoneme"] for p in sorted(phonemes, key=priority, reverse=True)[:2]]
    return [w["word"] for w in ranked], top_phonemes


def decide_stage(history: list[dict], fixed="auto") -> int:
    # 發音練習重點：看最近 3 次 ③ 的平均分。準確度 < 80 → 1 單音；韻律 < 80（或沒有韻律分）→ 2；都 >= 80 → 3。
    if fixed in (1, 2, 3):
        return fixed
    recent = [h for h in history if h.get("accuracy") is not None][-3:]
    if not recent:
        return 1
    if sum(h["accuracy"] for h in recent) / len(recent) < GOOD:
        return 1
    prosody = [h["prosody"] for h in recent if h.get("prosody") is not None]
    if not prosody or sum(prosody) / len(prosody) < GOOD:
        return 2
    return 3


# =====================================================================
# ④ 練習計劃
# =====================================================================

def problem_words(result: dict, limit: int) -> list[dict]:
    # 一句話裡唸得不好（紅 / 黃）的實詞，最差的在前，同一個字只算一次。
    seen, out = set(), []
    for w in sorted(result["words"], key=lambda w: w["accuracy"]):
        n = norm(w["text"])
        if w["color"] == "good" or not n or n in FUNCTION_WORDS or n in seen:
            continue
        seen.add(n)
        out.append({"word": clean_word(w["text"]), "accuracy": w["accuracy"]})
    return out[:limit]


def plan_practice(results: list[dict], cfg: dict) -> dict:
    """按 ③ 的結果決定 ④ 練什麼：
    sentences：有句子 < pass_score → 挑最低的幾句（按原文順序），每句先練問題字（最多 3 個）再練整句
    words    ：句子都過關但有紅 / 黃的字 → 只練這些字（最差的幾個）
    none     ：都沒問題 → 不練，讓學生選結束或挑戰更難的一篇"""
    ps = cfg["pass_score"]
    low = sorted((r["score"], i) for i, r in enumerate(results) if r["score"] < ps)[:cfg["practice_sentences"]]
    if low:
        items = [{"sentence": i, "words": [w["word"] for w in problem_words(results[i], 3)], "whole": True}
                 for i in sorted(i for _, i in low)]
        return {"mode": "sentences", "items": items}
    words, seen = [], set()
    for i, r in enumerate(results):
        for w in problem_words(r, 99):
            words.append((w["accuracy"], i, w["word"]))
    items = []
    for _, i, w in sorted(words):
        if w.lower() not in seen and len(items) < cfg["practice_words"]:
            seen.add(w.lower())
            items.append({"sentence": i, "words": [w], "whole": False})
    return {"mode": "words", "items": items} if items else {"mode": "none", "items": []}


def new_cursor(plan: dict) -> dict:
    # ④ 的進度：第幾項、正在練字（word）還是整句（sentence）、已計入的次數、是否結束（過關或用完次數）。
    cur = {**plan, "item": 0, "word": 0, "phase": "word", "tries": 0, "passed": False, "finished": False,
           "last_attempt": None}
    if plan["items"] and not plan["items"][0]["words"]:
        cur["phase"] = "sentence"
    return cur


def current_target(cur: dict, sentences: list[str]) -> dict | None:
    # ④ 現在要練的目標：{"kind": word / sentence, "text", "sentence": 所在句子}；全部練完返回 None
    if cur["item"] >= len(cur["items"]):
        return None
    item = cur["items"][cur["item"]]
    if cur["phase"] == "word":
        return {"kind": "word", "text": item["words"][cur["word"]], "sentence": item["sentence"]}
    return {"kind": "sentence", "text": sentences[item["sentence"]], "sentence": item["sentence"]}


def said_ratio(result: dict) -> float:
    # 參考文字裡被唸出來的字的比例（唸錯也算唸了，只有 Omission 算沒唸）。
    words = result["words"]
    return sum(w["error"] != "Omission" for w in words) / len(words) if words else 0.0


def record_try(cur: dict, result: dict, cfg: dict) -> bool:
    """記一次練習，返回是否計入。已經結束的目標再練不改進度。
    唸出來的字不到一半（沒聽到聲音、錄音太早結束）不計次數；但連續 MAX_UNCOUNTED 次之後一定計入，保證不卡住。"""
    if cur["finished"]:
        return False
    if said_ratio(result) < MIN_SAID_RATIO and cur.get("uncounted", 0) < MAX_UNCOUNTED:
        cur["uncounted"] = cur.get("uncounted", 0) + 1
        return False
    cur["uncounted"] = 0
    cur["tries"] += 1
    cur["passed"] = result["score"] >= cfg["pass_score"]
    cur["finished"] = cur["passed"] or cur["tries"] >= cfg["max_attempts"]
    return True


def next_target(cur: dict) -> bool:
    # 進入下一個目標：同一句的下一個字 → 這句整句 → 下一句。全部練完返回 False。
    item = cur["items"][cur["item"]]
    if cur["phase"] == "word" and cur["word"] + 1 < len(item["words"]):
        cur["word"] += 1
    elif cur["phase"] == "word" and item["whole"]:
        cur["phase"] = "sentence"
    else:
        cur["item"] += 1
        cur["word"] = 0
        nxt = cur["items"][cur["item"]] if cur["item"] < len(cur["items"]) else None
        cur["phase"] = "word" if nxt and nxt["words"] else "sentence"
    cur.update(tries=0, uncounted=0, passed=False, finished=False, last_attempt=None)
    return cur["item"] < len(cur["items"])


# =====================================================================
# LLM：短文和回饋
# =====================================================================

PASSAGE_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "sentences": {"type": "array", "items": {"type": "string"}}},
    "required": ["title", "sentences"],
    "additionalProperties": False,
}

DEFAULT_PASSAGE_PROMPT = (
    "You write short read-aloud passages for pronunciation practice. The reader is an adult whose first "
    "language is Chinese.\nRules:\n"
    "- {words} to {max_words} words in {min_sentences}-{max_sentences} sentences, at CEFR level {level}: only vocabulary "
    "and sentence structures a {level} learner knows.\n"
    "- One coherent, natural mini-story or description about the topic. Each sentence is complete and at most "
    "20 words long.\n"
    "- Spell out every number as words. No hyphenated words, abbreviations, initials, symbols, quotation marks, "
    "parentheses or hard-to-pronounce names.\n"
    "- Avoid words whose pronunciation depends on meaning or tense (for example read in the past tense, live, "
    "lead, wind, tear, close, record, present, use as a noun): the pronunciation checker assumes one fixed "
    "pronunciation and would mark a correct reading as wrong.\n"
    "- Return a short title and the passage as an array of sentences.")

STAGE_PASSAGE_RULES = {
    1: "Pronunciation focus: individual sounds. Use plenty of common words that contain the target sounds.",
    2: "Pronunciation focus: word stress and linking. Also include 2-3 multi-syllable words with clear word "
       "stress (for example 'comfortable', 'photographer') and 2-3 common linked phrases (for example "
       "'want to', 'pick it up', 'a lot of').",
    3: "Pronunciation focus: sentence stress and intonation. Also include at least two questions (one yes/no "
       "question and one wh- question) and one or two sentences with contrastive emphasis (for example "
       "'I said Tuesday, not Thursday').",
}

COLD_START_SOUNDS = ("sounds Chinese speakers often find hard: th (/θ/, /ð/), /v/ vs /w/, /l/ vs /r/, "
                     "final consonants (for example the d in 'need'), and /ɪ/ vs /iː/")

DEFAULT_FEEDBACK_PROMPT = (
    "你是一位親切、專業的英語發音老師，學生是華語母語的成人。根據系統給你的發音評估結果，用繁體中文給學生回饋，"
    "英文單字和音標照寫。\n規則：\n"
    "- 總共不超過 60 個字，用純文字，不要用列表、標題、表情符號或 Markdown 符號。\n"
    "- 學生最關心的是建議，所以肯定只用開頭一個很短的詞組（不超過 6 個字，例如「有進步，」「這次很清楚，」"
    "「好多了，」），不要展開描述哪裡好、別人聽不聽得懂、進步多少，接著直接給建議。\n"
    "- 建議只挑最重要的一個字或音，給一個具體可操作的發音要領"
    "（舌頭位置、嘴型、送氣、尾音、重音在哪個音節、哪裡該停頓、語調上揚或下降等）。\n"
    "- 評量標準是「別人聽不聽得懂」，不是像不像母語者。口音明顯但能聽懂的地方不要批評。\n"
    "- 不要說出分數數字，不要提 CEFR 等級。\n"
    "- 不好的例子（鼓勵太長）：你這次的發音非常清晰流暢，句子裡的每個字別人都能聽得懂，進步很多喔！"
    "最後一個字 fast 的 /st/ 尾音可以再多一點空氣感，把舌尖輕輕頂在上牙齦後面發出 /t/ 的聲音會更完美。\n"
    "- 好的例子：有進步，最後一個字 fast 的 /st/ 尾音可以再多一點空氣感，把舌尖輕輕頂在上牙齦後面發出 /t/ 的聲音會更完美。")

STAGE_FEEDBACK_HINTS = {
    1: "優先講個別母音、子音（例如 th、v、l / r、尾音）。",
    2: "除了個別的音，也留意多音節字的重音位置和連音。",
    3: "優先講句子重音（該強調的字）、停頓和語調（例如疑問句語尾上揚）。",
}


def _fill(template: str, **fields) -> str:
    # 把提示詞裡的 {欄位} 換成值（不用 str.format：提示詞裡可能有其他大括號）
    for k, v in fields.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def passage_messages(cfg, level, stage, topic, focus_words, focus_phonemes, problems=None):
    # 生成短文的 (system, messages)。
    words = cfg["passage_words"]
    min_s = max(3, words // 18)
    system = _fill(cfg["prompts"].get("passage") or DEFAULT_PASSAGE_PROMPT, words=words, level=level,
                   max_words=round(words * 1.15),
                   min_sentences=min_s, max_sentences=max(min_s + 1, words // 12))
    lines = [f"Topic: {topic or 'everyday life'}."]
    if focus_words:
        lines.append("Use each of these words naturally at least once, in exactly this form: "
                     + ", ".join(focus_words) + ".")
    if focus_phonemes:
        lines.append("Also use several common words that contain these sounds (IPA): "
                     + ", ".join(f"/{p}/" for p in focus_phonemes) + ".")
    if len(focus_words) < cfg["focus_words"][0]:
        lines.append(f"Also include words with {COLD_START_SOUNDS}.")
    lines.append(STAGE_PASSAGE_RULES[stage])
    if problems:
        lines.append("Fix these problems of your previous draft: " + " ".join(problems))
    return system, [{"role": "user", "content": "\n".join(lines)}]


def generate_passage(provider, cfg, level, stage, topic, focus_words, focus_phonemes) -> dict:
    # 讓 LLM 寫短文；要求的字沒寫進去、或明顯太短（實測 A2 時只寫到約六成字數）就再試一次，第二次仍不符合就算了。
    problems, data = None, None
    for _ in range(2):
        system, messages = passage_messages(cfg, level, stage, topic, focus_words, focus_phonemes, problems)
        try:
            data = provider.json(system, messages, PASSAGE_SCHEMA)
        except Exception as e:
            log.exception("短文生成失敗")
            raise PronError(f"The teacher could not write a passage, please try again ({e})") from e
        sentences = [s.strip() for s in data.get("sentences") or [] if isinstance(s, str) and tokens(s)]
        if len(sentences) < 3:
            raise PronError("The passage came back in a bad format, please start a new lesson")
        data["sentences"] = sentences
        used = {norm(t) for s in sentences for t in tokens(s)}
        missing = [w for w in focus_words if norm(w) not in used]
        count = sum(len(tokens(s)) for s in sentences)
        problems = []
        if missing:
            problems.append(f"It left out these required words, include them: {', '.join(missing)}.")
        if count < cfg["passage_words"] * 0.8:
            problems.append(f"It has only {count} words; write {cfg['passage_words']} to "
                            f"{round(cfg['passage_words'] * 1.15)} words (add more sentences).")
        if not problems:
            break
    return {"title": str(data.get("title") or topic or "Reading").strip(), "sentences": data["sentences"],
            "focus_words": focus_words, "focus_phonemes": focus_phonemes}


def describe(result: dict, target: str) -> dict:
    # 給 LLM 寫回饋用的評估摘要：整體分數 + 最弱的幾個字（含分數 < 60 的音素和韻律問題）。
    weak = sorted((w for w in result["words"] if w["color"] != "good" or w["prosody_errors"]),
                  key=lambda w: w["accuracy"])[:4]
    return {
        "target": target,
        "scores": {k: result.get(k) for k in ("score", "accuracy", "fluency", "completeness", "prosody")},
        "intonation_too_flat": bool(result.get("monotone")),
        "weak_words": [{"word": clean_word(w["text"]), "accuracy": w["accuracy"],
                        "error": {"Omission": "漏唸", "Mispronunciation": "唸錯"}.get(w["error"], ""),
                        "weak_sounds": [p["phoneme"] for p in w["phonemes"] if p["accuracy"] < FAIR],
                        "prosody_problems": w["prosody_errors"]} for w in weak],
    }


def write_feedback(provider, cfg, stage, situation: str, data) -> str:
    # 老師的一兩句中文回饋。situation 說明這是什麼時候的回饋，data 是評估摘要；生成失敗返回空字符串
    system =(cfg["prompts"].get("feedback") or DEFAULT_FEEDBACK_PROMPT) + \
        f"\n本篇的發音練習重點是「{STAGES[stage]}」：{STAGE_FEEDBACK_HINTS[stage]}"
    content = situation + "\n評估結果：" + json.dumps(data, ensure_ascii=False)
    try:
        text = "".join(provider.stream(system, [{"role": "user", "content": content}])).strip()
    except Exception:
        log.exception("老師回饋生成失敗")
        text = ""
    return text


CHAT_PROMPT = (
    "## 現在的情境\n"
    "這是一節英語發音課的結果頁。學生看完分數後，想就這節課的結果問你問題。學生是華語母語的成人。\n"
    "規則：\n"
    "- 用繁體中文回答，英文單字、例句和音標照寫。\n"
    "- 根據下面的課堂資料回答，不要編造資料裡沒有的分數、字或錯誤；資料裡沒有的就直說不知道。\n"
    "- 回答簡潔，一般兩到四句，不超過 120 字；學生要求詳細解釋時可以多寫一些。\n"
    "- 純文字，不要 Markdown、列表符號或表情符號。\n"
    "- 講發音要領時要具體（舌頭位置、嘴型、送氣、尾音、重音在哪個音節、哪裡停頓、語調上揚或下降）。\n"
    "- 評量標準是「別人聽不聽得懂」，不是像不像母語者。\n"
    "- 分數的意思：Overall 總分，綜合各項；Accuracy 準確度，每個音唸得準不準；Fluency 流利度，停頓和速度是否自然；"
    "Completeness 完整度，有沒有漏唸（唸錯太多的字也會被算成沒唸到）；Prosody 韻律，重音、語調和節奏。"
    "字的顏色：綠色 80 分以上，黃色 60 到 79，紅色低於 60 或漏唸。\n"
    "- 學生看到的結果頁上有這些標記，學生問到時照這裡解釋：\n"
    "  紅色的字：這個字需要練習（低於 60 分或漏唸）；某個字唸錯可能只是其中一個音不準（例如 th 唸成 s），整個字也會標紅。"
    "頁面上沒有波浪線。\n"
    "  字下面的細點線（虛線）：這是本篇特別放進去的目標字（就是課堂資料裡 target_words 列出的字），"
    "是從學生的錯誤本挑出來、以前常唸錯的字，刻意寫進文章讓他再練；虛線和分數無關，字的顏色才是這次唸得好不好。\n"
    "  每句句末的小數字：這一句的總分。\n"
    "  分數卡片右邊的小走勢圖：最近幾節課第一次讀全文的分數變化，最右邊的點是這次。\n"
    "  Words you missed the first time：第一次讀全文時標紅的字，箭頭後面是第二次讀的結果（fixed 已改好、better 有進步、still red 還沒好）。\n"
    "  Sounds you missed the first time：第一次讀全文時唸得不好的音標（音素低於 60 分），後面是出錯次數和出錯的字，"
    "箭頭後面是第二次讀同一個位置的結果（fixed、better、still weak）。\n"
    "- 分數用來看自己的進步，不對應 CEFR 等級；學生問「我是什麼程度」時要說明這一點。\n"
    "- 只聊這節課和英語發音學習相關的話題；學生問別的，簡短回應後帶回發音學習。\n"
    "- 學生看不到「課堂資料」，回答時不要提到課堂資料、JSON 或 target_words、first_score 這類欄位名稱，"
    "直接用一般的話說（例如「這篇特別放進去的字」「你第一次讀的分數」）。")

def chat_context(st: dict) -> dict:
    # 給聊天用的課堂資料（從 Course.state() 取）：兩次朗讀的分數、每句分數和弱的字、練習了什麼、老師的回饋。
    def weak(result):
        return describe(result, "")["weak_words"] if result else []
    practice = st.get("practice") or {}
    return {
        "title": st["title"], "passage_level": st["level"], "pronunciation_focus": STAGES[st["stage"]],
        "target_words": st["focus_words"],   # 頁面上有細點線的字：從錯誤本挑出來特別放進文章的字
        "first_reading": st.get("summary1"), "second_reading": st.get("summary2"),
        "sentences": [{"text": s["text"],
                       "first_score": (st["read1"][i] or {}).get("score"),
                       "second_score": (st["read2"][i] or {}).get("score"),
                       "weak_words_first": weak(st["read1"][i]), "weak_words_second": weak(st["read2"][i])}
                      for i, s in enumerate(st["sentences"])],
        "practiced": [w for item in practice.get("items", []) for w in item["words"]]
                     + [st["sentences"][item["sentence"]]["text"] for item in practice.get("items", [])
                        if item.get("whole")],
        "teacher_feedback": st.get("feedback", {}),
    }


# =====================================================================
# 課堂流程
# =====================================================================

_locks: dict[int, threading.RLock] = {}
_ended_notice: set[int] = set()   # 由瀏覽器端流程結束的課，等 app.py 的計時器刷新頁面


def take_ended_notice(session_id) -> bool:
    # 給 app.py：這節課是不是剛在發音課流程裡結束（是的話刷新按鈕、歷史列表）。
    if session_id in _ended_notice:
        _ended_notice.discard(session_id)
        return True
    return False


def get_course(session_id) -> "Course":
    # app.py 的接口入口：取出這節發音課（不存在或不是發音課時拋 PronError）
    try:
        session = builder.open_session(session_id)
    except KeyError as e:
        raise PronError("Lesson not found") from e
    if session.mode != "pron":
        raise PronError("This is not a pronunciation lesson")
    return Course(session)


def step_label(session_id) -> str:
    # 頁面頂部狀態條上顯示的當前步驟
    rows =builder.get_db().get_pron_lessons(session_id)
    return STEP_LABELS.get(rows[-1]["step"], "") if rows else "1 · Passage"


class Course:
    """一節發音課的操作（每次請求創建一個，狀態都在數據庫裡）。同一節課的修改用同一把鎖串行，
    Azure 評估不在鎖裡（③ 的多句錄音可以同時評分）。speech / clock / 目錄可注入，測試時用假的。"""

    def __init__(self, session, speech=None, clock=time.time, rec_dir=REC_DIR, tts_dir=TTS_DIR):
        self.session, self.agent, self.db = session, session.agent, session.db
        self.cfg = session.cfg["pronunciation"]
        self._speech = speech
        self.clock, self.rec_dir, self.tts_dir = clock, Path(rec_dir), Path(tts_dir)
        self.lock = _locks.setdefault(session.id, threading.RLock())

    @property
    def speech(self):
        if self._speech is None:
            self._speech = AzureSpeech(self.cfg["language"])
        return self._speech

    def _today(self):
        return datetime.date.fromtimestamp(self.clock()).isoformat()

    def _lesson(self) -> dict | None:
        rows = self.db.get_pron_lessons(self.session.id)
        return rows[-1] if rows else None

    def _save(self, ls):
        self.db.save_pron_lesson(self.session.id, ls["round"], ls["level"], ls["stage"], ls["passage"],
                                 ls["step"], ls["state"], self.clock())

    def _read_history(self) -> list[dict]:
        # 歷次 ③ 的全文分數（按時間），用來判斷練習重點和畫進步趨勢。
        out = []
        for r in self.db.get_pron_lessons():
            s = r["state"].get("summary1")
            if s:
                out.append({**s, "at": r["state"].get("summary1_at")})
        return sorted(out, key=lambda h: h["at"] or 0)

    # ---------------- ① 開課 / 新一輪 ----------------

    def start(self, level=None, round_=1) -> dict:
        # 挑重點、讓老師寫短文，進入 ② 示範。
        level = level or base_level(self.session.cfg["level"])
        stage = decide_stage(self._read_history(), self.cfg["stage"])
        focus_words, focus_phonemes = pick_focus(self.db.get_pron_words(), self.db.get_pron_phonemes(),
                                                 self._today(), self.cfg["focus_words"][1])
        passage = generate_passage(self.agent.provider("chat"), self.cfg, level, stage, self.session.topic,
                                   focus_words, focus_phonemes)
        ls = {"round": round_, "level": level, "stage": stage, "passage": passage, "step": "demo", "state": {}}
        with self.lock:
            self._save(ls)
        return self.state()

    # ---------------- 給瀏覽器的狀態 ----------------

    def state(self) -> dict:
        # 瀏覽器需要的全部狀態（每次操作後整份返回，pron.js 按它重繪）
        ls = self._lesson()
        if ls is None:
            return {"ready": False, "ended": self.session.ended}
        sentences = ls["passage"]["sentences"]
        attempts = self.db.get_pron_attempts(self.session.id, ls["round"])
        latest = self._latest_reads(ls, attempts)
        reads = {step: [a["result"] if a else None for a in latest[step]] for step in latest}
        st = ls["state"]
        practice = None
        if st.get("practice"):
            cur = st["practice"]
            last = next((a for a in attempts if a["id"] == cur["last_attempt"]), None)
            practice = {**cur, "target": current_target(cur, sentences),
                        "last": {"attempt": last["id"], "result": last["result"], "counted": bool(last["counted"]),
                                 "feedback": last["feedback"]} if last else None}
        cfg = self.cfg
        return {
            "ready": True, "ended": self.session.ended, "round": ls["round"], "level": ls["level"],
            "stage": ls["stage"], "stage_label": STAGE_LABELS[ls["stage"]], "step": ls["step"],
            "title": ls["passage"]["title"],
            "sentences": [{"text": s, "tokens": tokens(s)} for s in sentences],
            "focus_words": ls["passage"]["focus_words"],
            "read1": reads["read1"], "read2": reads["read2"],
            # 第一次 / 第二次讀全文的錄音是否都還在（結果頁可以重聽）
            "audio": {step: all(a and self._recording_path(a["id"]).exists() for a in latest[step])
                      for step in latest},
            # 各句錄音的秒數：重聽整篇時，用來算出現在唸到第幾句（整篇是各句按順序接起來，句間 REC_GAP_SECONDS）
            "audio_durations": {step: [self._duration(a) for a in latest[step]] for step in latest},
            "summary1": st.get("summary1"), "summary2": st.get("summary2"),
            "feedback": st.get("feedback", {}), "practice": practice,
            "chat": self.db.get_pron_chat(self.session.id),
            # 最近 TREND_POINTS 次第一次讀全文（真實能力）的各項分數，結果頁分數卡片裡的小走勢圖用
            "trend": [{k: h.get(k) for k in ("at", "score", "accuracy", "fluency", "completeness", "prosody")}
                      for h in self._read_history()[-TREND_POINTS:]],
            "config": {"pass_score": cfg["pass_score"], "max_attempts": cfg["max_attempts"],
                       "auto_stop_ms": cfg["auto_stop_silence_ms"], "max_seconds": cfg["max_record_seconds"]},
        }

    # ---------------- 錄音評分 ----------------

    def assess(self, step: str, sentence: int, wav: bytes) -> dict:
        # ③ / ⑤：評第 sentence 句；④：評當前練習目標（字或整句）。
        if not wav.startswith(b"RIFF") or len(wav) > MAX_WAV_BYTES:
            raise PronError("Bad recording format or recording too long")
        with self.lock:
            ls = self._lesson()
            if ls is None or self.session.ended or ls["step"] != step:
                raise PronError("The lesson has moved on, please refresh the page")
            sentences = ls["passage"]["sentences"]
            if step in ("read1", "read2"):
                if not 0 <= sentence < len(sentences):
                    raise PronError("Invalid sentence number")
                target = {"kind": "sentence", "text": sentences[sentence], "sentence": sentence}
            elif step == "practice":
                target = current_target(ls["state"]["practice"], sentences)
            else:
                target = None
            if target is None:
                raise PronError("Nothing to record right now")
        result = self.speech.assess(wav, target["text"])
        if target["kind"] == "word":
            # 單字用準確度當分數：Azure 的總分把流利度、完整度也算進去，對單個字沒有意義
            # （實測唸錯時被雙重扣分：準確度 46、總分 20；唸得普通時反而被抬高：準確度 60、總分 78）
            result["score"] = result["accuracy"]
        with self.lock:
            ls = self._lesson()
            counted = True
            cur = ls["state"].get("practice")
            # 評分期間（不在鎖裡）學生可能已經按了下一個：目標還是同一個才記進練習進度
            same = (step == "practice" and ls["step"] == "practice"
                    and current_target(cur, ls["passage"]["sentences"]) == target)
            if step == "practice":
                counted = same and record_try(cur, result, self.cfg)
            aid = self.db.add_pron_attempt(self.session.id, ls["round"], step, target["sentence"], target["kind"],
                                           target["text"], result["score"], result, counted, self.clock())
            if same:
                cur["last_attempt"] = aid
                self._save(ls)
        self._save_recording(aid, wav)
        out = {"attempt": aid, "result": result, "counted": counted}
        if step == "practice":
            out["practice"] = self.state()["practice"]
        return out

    def _recording_path(self, aid) -> Path:
        return self.rec_dir / str(self.session.id) / f"{aid}.wav"

    def _duration(self, attempt) -> float | None:
        # 一次錄音的秒數；沒有錄音或讀不了返回 None
        path = self._recording_path(attempt["id"]) if attempt else None
        if path is None or not path.exists():
            return None
        try:
            with wave.open(str(path), "rb") as w:
                return round(w.getnframes() / w.getframerate(), 3)
        except (wave.Error, EOFError, OSError):
            return None

    def _save_recording(self, aid, wav):
        try:
            path = self._recording_path(aid)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(wav)
        except OSError:
            log.exception("保存學生錄音失敗")

    @staticmethod
    def _latest_reads(ls, attempts) -> dict:
        # ③ / ⑤ 每句最新的一次錄音（重錄時以最新的為準）：{"read1": [評分記錄或 None, ...], "read2": [...]}
        n = len(ls["passage"]["sentences"])
        latest = {"read1": [None] * n, "read2": [None] * n}
        for a in attempts:
            if a["step"] in latest and a["kind"] == "sentence" and 0 <= a["sentence_idx"] < n:
                latest[a["step"]][a["sentence_idx"]] = a
        return latest

    def reading_audio(self, step: str, sentence: int | None = None) -> bytes:
        """第一次（read1）/ 第二次（read2）讀全文的錄音（WAV）。sentence 為 None 時把各句按順序拼成一段，
        句與句之間留 REC_GAP_SECONDS 靜音；給了 sentence 就只返回那一句。"""
        ls = self._lesson()
        if ls is None or step not in ("read1", "read2"):
            raise PronError("Recording not found")
        latest = self._latest_reads(ls, self.db.get_pron_attempts(self.session.id, ls["round"]))[step]
        picked = latest if sentence is None else latest[sentence:sentence + 1] if 0 <= sentence < len(latest) else []
        paths = [self._recording_path(a["id"]) for a in picked if a]
        if not paths or len(paths) != len(picked) or not all(p.exists() for p in paths):
            raise PronError("Recording not found")
        if len(paths) == 1:
            return paths[0].read_bytes()
        frames, params = [], None
        for p in paths:
            with wave.open(str(p), "rb") as w:
                params = params or w.getparams()
                if frames:
                    frames.append(b"\0" * int(params.framerate * REC_GAP_SECONDS) * params.sampwidth * params.nchannels)
                frames.append(w.readframes(w.getnframes()))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setnchannels(params.nchannels)
            out.setsampwidth(params.sampwidth)
            out.setframerate(params.framerate)
            out.writeframes(b"".join(frames))
        return buf.getvalue()

    def feedback(self, attempt_id: int) -> str:
        # ④ 每次評分後的老師回饋（和評分分開請求，分數先顯示）。生成過就直接返回。
        a = self.db.get_pron_attempt(attempt_id)
        if a is None or a["session_id"] != self.session.id:
            raise PronError("Recording not found")
        if a["feedback"]:
            return a["feedback"]
        ls = self._lesson()
        stage = ls["stage"] if ls else 1
        what = "單字" if a["kind"] == "word" else "句子"
        if not a["counted"]:
            situation = (f"學生練習{what}「{a['target']}」時，系統幾乎沒聽到他唸出來（可能錄音太早結束或聲音太小），"
                         "這次不計次數。請溫和地提醒他靠近麥克風、完整地再唸一次，不要評論發音。")
        else:
            tries = sum(1 for x in self.db.get_pron_attempts(self.session.id, a["round"])
                        if x["step"] == "practice" and x["counted"] and x["target"] == a["target"]
                        and x["id"] <= a["id"])
            passed = a["score"] >= self.cfg["pass_score"]
            situation = (f"學生正在練習{what}「{a['target']}」，這是第 {tries} 次（最多 {self.cfg['max_attempts']} 次），"
                         + ("這次過關了：簡短肯定一下，還有可以更好的地方就給一個要領。" if passed
                            else "這次還沒過關：給一個最關鍵的改進要領。"))
        text = write_feedback(self.agent.provider("chat"), self.cfg, stage, situation,
                              describe(a["result"], a["target"]))
        if not text:
            text = "很好，繼續保持！" if a["score"] >= self.cfg["pass_score"] else "再聽一次示範，注意紅色的字，慢慢唸。"
        self.db.set_pron_feedback(attempt_id, text)
        return text

    # ---------------- 推進步驟 ----------------

    def advance(self, action: str) -> dict:
        # 推進步驟。action：start_reading（② → ③）、finish_reading（③ / ⑤ 讀完）、next（④ 下一個目標）、
        # end（全部過關後結束）、harder（全部過關後挑戰更難的一篇）。當前步驟不允許的操作拋 PronError。
        with self.lock:
            ls = self._lesson()
            if ls is None or self.session.ended:
                raise PronError("This lesson has ended")
            step = ls["step"]
            if action == "start_reading" and step == "demo":
                ls["step"] = "read1"
                self._save(ls)
            elif action == "finish_reading" and step in ("read1", "read2"):
                self._finish_reading(ls)
            elif action == "next" and step == "practice":
                if not next_target(ls["state"]["practice"]):
                    ls["step"] = "read2"
                self._save(ls)
            elif action == "end" and step == "choice":
                ls["step"] = "done"
                self._save(ls)
                self._end()
            elif action == "harder" and step == "choice":
                ls["step"] = "done"
                self._save(ls)
                return self.start(level_up(ls["level"]), ls["round"] + 1)
            else:
                raise PronError("This action is not available now, please refresh the page")
        return self.state()

    def _finish_reading(self, ls):
        # ③ 讀完：算全文分數、更新錯誤本、決定 ④ 練什麼、老師寫一段回饋。⑤ 讀完：算分數、課堂結束。
        step = ls["step"]
        results = self.state()[step]
        if any(r is None for r in results):
            raise PronError("Some sentences are not recorded or still being scored")
        st = ls["state"]
        summary = summarize(results)
        if step == "read1":
            st["summary1"], st["summary1_at"] = summary, self.clock()
            update_error_book(self.db, results, self._today())
            plan = plan_practice(results, self.cfg)
            if plan["mode"] == "none":
                situation = ("學生剛朗讀完整篇短文，每一句、每一個字都唸得很好，沒有需要練的地方。"
                             "用一句簡短的話鼓勵他，再問他想結束今天的課，還是挑戰一篇更難的文章。")
                ls["step"] = "choice"
            else:
                what = "分數最低的幾句（先練句中的問題字，再練整句）" if plan["mode"] == "sentences" else "幾個唸得不好的字"
                situation = (f"學生剛朗讀完整篇短文，這是他目前的真實水平。指出最需要改的一個地方並給要領，"
                             f"再告訴他接下來要練{what}。")
                st["practice"] = new_cursor(plan)
                ls["step"] = "practice"
            weakest = min(results, key=lambda r: r["score"])
            data = {"overall": summary, "weakest_sentence": describe(weakest, "")}
            # 回饋存在 feedback.read1（要練習）或 feedback.choice（全部過關），結果頁和聊天都會用到
            st.setdefault("feedback", {})["choice" if ls["step"] == "choice" else "read1"] = \
                write_feedback(self.agent.provider("chat"), self.cfg, ls["stage"], situation, data)
        else:
            # 結果頁只顯示分數和對比，不再寫一段細節建議；學生有疑問用右下角的聊天問老師
            st["summary2"] = summary
            ls["step"] = "done"
        self._save(ls)
        if ls["step"] == "done":
            self._end()

    def _end(self):
        # 課堂結束（寫庫），並通知 app.py 的計時器刷新頁面上的按鈕和歷史列表
        for _ in self.session.finish():
            pass
        _ended_notice.add(self.session.id)

    # ---------------- 結果頁：和老師聊天 ----------------

    def chat(self, text: str) -> dict:
        """學生看完結果後問老師問題（文字）。老師帶著這節課的資料回答；課堂結束後也能聊。
        返回 {"reply": 老師的回答, "chat": 全部聊天記錄}。"""
        text = (text or "").strip()
        if not text or len(text) > MAX_CHAT_CHARS:
            raise PronError(f"Message is empty or longer than {MAX_CHAT_CHARS} characters")
        st = self.state()
        if not st.get("ready") or not st.get("summary1"):
            raise PronError("You can chat with your teacher after reading the passage")
        history = self.db.get_pron_chat(self.session.id)[-CHAT_HISTORY:]
        system = (self.session.cfg["persona"] + "\n\n" + CHAT_PROMPT + "\n\n## 課堂資料\n"
                  + json.dumps(chat_context(st), ensure_ascii=False))
        messages = history + [{"role": "user", "content": text}]
        try:
            reply = "".join(self.agent.provider("chat").stream(system, messages)).strip()
        except Exception as e:
            log.exception("老師聊天回覆失敗")
            raise PronError(f"Your teacher could not reply, please try again ({e})") from e
        if not reply:
            raise PronError("Your teacher could not reply, please try again")
        with self.lock:
            now = self.clock()
            self.db.add_pron_chat(self.session.id, "user", text, now)
            self.db.add_pron_chat(self.session.id, "assistant", reply, now)
        return {"reply": reply, "chat": self.db.get_pron_chat(self.session.id)}

    # ---------------- 示範語音 ----------------

    def tts(self, text: str, slow=False) -> Path:
        # 示範的 mp3，按（聲線, 語速, 文字）快取到 TTS_DIR，同一句再聽不會再呼叫 Azure。
        text = (text or "").strip()
        if not text or len(text) > MAX_TTS_CHARS:
            raise PronError("Text to read is empty or too long")
        voice = self.cfg["voice"]
        rate = self.cfg["slow_rate"] if slow else None
        key = hashlib.sha1(f"{voice}|{rate}|{text}".encode("utf-8")).hexdigest()
        path = self.tts_dir / f"{key}.mp3"
        if not path.exists():
            audio = self.speech.synthesize(text, voice, rate)
            self.tts_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(audio)
            os.replace(tmp, path)
        return path
