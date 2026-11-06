"""解析器：讀取 agents/*.json 構建老師（Agent），管理課堂（Session），調用 LLM，讀寫長期記憶。

app.py 只通過這裡的接口工作：
    配置    list_agents() / load_agent(name) / load_topics()
    開課    agent.start_session(topic, mode) -> session，然後 session.greet()
            mode：text（文字課）/ voice（語音課，需要 agent JSON 裡配置 voice，語音收發見 voice.py）
                  / pron（發音課，需要 agent JSON 裡配置 pronunciation，流程在 pron.py，不調用 greet）
    上課    session.ask(question, via)   學生發言：糾錯 → 老師回覆；via 為 voice 表示是語音轉寫；道別後不再回覆
            session.tick()          每秒調用：階段切換（及階段固定話術 intro）、停頓關懷、總結報告、下課前道別、下課
            session.finish()        學生主動結束：先總結再下課
            session.status()        狀態條數據
    歷史    list_sessions() / open_session(id) / delete_session(id)

會生成文字的方法都返回字符串迭代器，app.py 邊迭代邊刷新頁面，實現流式顯示。
老師的一次回覆可以按空行拆成多條訊息（agent JSON 的 max_bubbles），每條在 view 和數據庫裡都是獨立的一條。

LLM 配置（agent JSON 裡的 llm.chat / llm.corrector）：
    Claude    {"provider": "anthropic", "model": "claude-sonnet-5-5", "effort": "low"}    # ANTHROPIC_API_KEY
    Gemini    {"provider": "gemini", "model": "gemini-flash-lite-latest"}                 # GEMINI_API_KEY
    OpenAI    {"provider": "openai_compat", "model": "gpt-6.1-sol"}                       # OPENAI_API_KEY
    自建模型  {"provider": "openai_compat", "model": "gemma4:e4b",
               "base_url": "http://localhost:11434/v1"}                                  # 不需要 Key
              以本機 Ollama 運行 Gemma 4 為例：先安裝 Ollama，執行 ollama pull gemma4:e4b；
              其他 OpenAI 兼容的服務（vLLM、LM Studio 等）把 base_url 換成對應地址即可，
              需要 Key 時用 "api_key_env": "環境變量名" 指定從 .env 讀哪個變量。
"""

import copy
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from db import DB

log = logging.getLogger("vtutor")

AGENTS_DIR = Path(__file__).resolve().parent / "agents"
TOPICS_FILE = Path(__file__).resolve().parent / "topics.json"   # 話題列表，和老師無關


# =====================================================================
# Agent JSON 解析與校驗
# =====================================================================

class AgentConfigError(ValueError):
    # agent / topics JSON 寫錯時拋出，信息直接顯示給編寫 JSON 的人看。
    pass


# JSON 裡沒寫的字段用這些默認值補全（可選模塊默認關閉）
DEFAULTS = {
    "level": "B1",
    "greeting": "",   # 固定開場白；留空則由模型生成
    "max_bubbles": 1,  # 老師一次回覆最多拆成幾條訊息；1 表示不拆
    "bubble_pause_seconds": [1, 3],  # 兩條訊息之間隨機停頓的秒數範圍 [最短, 最長]
    "max_questions_in_a_row": 0,     # 老師連續幾輪以問句結尾後，提醒下一輪用陳述句結尾；0 表示不提醒
    "farewell_seconds": 0,           # 課程結束前幾秒老師主動道別；0 表示不道別
    "farewell": "",                  # 固定道別語；留空則由模型生成
    "correction": {"enabled": False, "max_per_turn": 2, "explain_limit_per_category": 2,
                   "categories": {"other": "其他"}},
    "nudge": {"enabled": False, "thresholds_seconds": [30, 60], "levels": []},
    "summary": {"enabled": False, "top_errors": 5},
    "memory": {"load_recent_sessions": 3},
    "prompts": {"chat_rules": "", "corrector": "", "summary": ""},
    "voice": {},   # 語音課配置（stt / tts 等，見 parse_voice_config）；留空表示這位老師只能上文字課
    "pronunciation": {},   # 發音課配置（見 parse_pron_config 和 pron.py）；留空表示這位老師不上發音課
}

PROVIDERS = ("anthropic", "gemini", "openai_compat")
# 語音識別 / 語音合成的廠商（Claude 沒有語音接口）。gemini_live：用 Live API 對話模型逐字朗讀，免費額度不限次數
VOICE_PROVIDERS = {"stt": ("openai", "gemini"), "tts": ("openai", "gemini", "gemini_live")}
MODES = ("text", "voice", "pron")

VOICE_DEFAULTS = {
    "tts_by_language": {},   # 按階段語言覆蓋 tts 的字段，例如 {"zh-TW": {"instructions": "..."}}
    "chat_rules": "",        # 語音課追加到系統提示詞的說話規則
    "silence_ms": 800,       # 學生停頓多少毫秒算說完一句
}

PRON_DEFAULTS = {
    "language": "en-US",                       # 發音評估和示範的語言
    "voice": "en-US-AvaNeural",                # 示範用的 Azure 聲線
    "slow_rate": "-30%",                       # 慢速示範的語速（SSML prosody rate）
    "passage_words": 150,                      # 短文大約多少字
    "focus_words": [3, 5],                     # 從錯誤本挑幾個字放進短文 [最少, 最多]
    "practice_sentences": 3,                   # ④ 最多練幾句
    "practice_words": 5,                       # 句子都過關、只練字時最多練幾個字
    "pass_score": 80,                          # 過關分數
    "max_attempts": 3,                         # ④ 每個字 / 每句最多試幾次
    "auto_stop_silence_ms": 1000,              # 錄音時說完後靜音多久自動停止
    "max_record_seconds": 30,                  # 單次錄音上限
    "stage": "auto",                           # 發音練習重點：auto 自動判斷，或固定 1 / 2 / 3
    "prompts": {"passage": "", "feedback": ""},  # 覆蓋 pron.py 裡默認的短文 / 回饋提示詞；留空用默認
}


def _merge(base, override):
    # 遞歸合併字典：override 裡有的字段覆蓋 base。
    out = copy.deepcopy(base)
    for k, v in override.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def parse_agent_config(raw: dict) -> dict:
    # 補全默認值並校驗。出錯時拋 AgentConfigError，信息直接給 RD 看。
    if not isinstance(raw, dict):
        raise AgentConfigError("agent JSON 頂層必須是對象 {...}")
    for key in ("name", "persona", "llm", "phases"):
        if key not in raw:
            raise AgentConfigError(f"缺少必填字段 \"{key}\"")
    cfg = _merge(DEFAULTS, raw)

    for role in ("chat", "corrector"):
        if role == "corrector" and not cfg["correction"]["enabled"]:
            continue
        llm = cfg["llm"].get(role)
        if not isinstance(llm, dict) or "provider" not in llm or "model" not in llm:
            raise AgentConfigError(f"llm.{role} 需要 provider 和 model 字段")
        if llm["provider"] not in PROVIDERS:
            raise AgentConfigError(f"llm.{role}.provider 必須是 {' / '.join(PROVIDERS)}，現在是 \"{llm['provider']}\"")

    phases = cfg["phases"]
    if not isinstance(phases, list) or not phases:
        raise AgentConfigError("phases 必須是非空列表")
    seen = set()
    for i, ph in enumerate(phases):
        where = f"phases[{i}]"
        for key in ("id", "minutes", "instruction"):
            if key not in ph:
                raise AgentConfigError(f"{where} 缺少 \"{key}\"")
        if ph["id"] in seen:
            raise AgentConfigError(f"{where}.id \"{ph['id']}\" 重複")
        seen.add(ph["id"])
        if not isinstance(ph["minutes"], (int, float)) or ph["minutes"] < 0:
            raise AgentConfigError(f"{where}.minutes 必須是 >= 0 的數字")
        if ph.get("on_enter") not in (None, "summarize"):
            raise AgentConfigError(f"{where}.on_enter 只支持 \"summarize\"")
        ph.setdefault("label", ph["id"])
        ph.setdefault("language", "en")
        ph.setdefault("correction", True)
        ph.setdefault("nudge", True)
        ph.setdefault("vary_endings", True)   # 本階段是否按 max_questions_in_a_row 提醒用陳述句結尾
        ph.setdefault("intro", "")            # 進入本階段時老師先說的固定話術；留空表示不說
        if not isinstance(ph["intro"], str):
            raise AgentConfigError(f"{where}.intro 必須是字符串（留空表示進入階段時不說固定話術）")
    if sum(ph["minutes"] for ph in phases) <= 0:
        raise AgentConfigError("phases 的總時長必須大於 0")

    corr = cfg["correction"]
    if not isinstance(corr["max_per_turn"], int) or corr["max_per_turn"] < 0:
        raise AgentConfigError("correction.max_per_turn 必須是 >= 0 的整數")
    if not isinstance(corr["explain_limit_per_category"], int) or corr["explain_limit_per_category"] < 0:
        raise AgentConfigError("correction.explain_limit_per_category 必須是 >= 0 的整數")
    if not isinstance(corr["categories"], dict) or not corr["categories"]:
        raise AgentConfigError("correction.categories 必須是 {\"類別id\": \"中文名\"} 形式的對象")
    corr["categories"].setdefault("other", "其他")

    if not isinstance(cfg["greeting"], str):
        raise AgentConfigError("greeting 必須是字符串（留空表示由模型生成開場白）")
    mb = cfg["max_bubbles"]
    if not isinstance(mb, int) or isinstance(mb, bool) or mb < 1:
        raise AgentConfigError("max_bubbles 必須是 >= 1 的整數（1 表示不拆成多條訊息）")
    pause = cfg["bubble_pause_seconds"]
    if (not isinstance(pause, list) or len(pause) != 2
            or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0 for x in pause)
            or pause[0] > pause[1]):
        raise AgentConfigError("bubble_pause_seconds 必須是 [最短, 最長] 兩個 >= 0 的數字，例如 [1, 3]")
    mq = cfg["max_questions_in_a_row"]
    if not isinstance(mq, int) or isinstance(mq, bool) or mq < 0:
        raise AgentConfigError("max_questions_in_a_row 必須是 >= 0 的整數（0 表示不提醒）")
    fs = cfg["farewell_seconds"]
    if not isinstance(fs, (int, float)) or isinstance(fs, bool) or fs < 0:
        raise AgentConfigError("farewell_seconds 必須是 >= 0 的數字（0 表示不主動道別）")
    if not isinstance(cfg["farewell"], str):
        raise AgentConfigError("farewell 必須是字符串（留空表示由模型生成道別語）")
    cfg["voice"] = parse_voice_config(cfg["voice"])
    cfg["pronunciation"] = parse_pron_config(cfg["pronunciation"])
    _apply_level(cfg)

    th = cfg["nudge"]["thresholds_seconds"]
    if not isinstance(th, list) or not all(isinstance(x, (int, float)) and x > 0 for x in th):
        raise AgentConfigError("nudge.thresholds_seconds 必須是正數列表，例如 [30, 60]")
    if th != sorted(th):
        raise AgentConfigError("nudge.thresholds_seconds 必須從小到大排列")
    if cfg["nudge"]["enabled"] and not cfg["nudge"]["levels"]:
        raise AgentConfigError("nudge.enabled 為 true 時需要 nudge.levels（每次關懷的提示）")

    return cfg


def parse_voice_config(voice) -> dict:
    # 校驗 voice 段並補全默認值；空對象表示不支持語音課。
    if not isinstance(voice, dict):
        raise AgentConfigError("voice 必須是對象 {...}（不需要語音課可以不寫）")
    if not voice:
        return {}
    voice = _merge(VOICE_DEFAULTS, voice)
    for role in ("stt", "tts"):
        v = voice.get(role)
        if not isinstance(v, dict) or "provider" not in v or "model" not in v:
            raise AgentConfigError(f"voice.{role} 需要 provider 和 model 字段")
        if v["provider"] not in VOICE_PROVIDERS[role]:
            raise AgentConfigError(f"voice.{role}.provider 必須是 {' / '.join(VOICE_PROVIDERS[role])}，"
                                   f"現在是 \"{v['provider']}\"")
    by_lang = voice["tts_by_language"]
    if not isinstance(by_lang, dict) or not all(isinstance(v, dict) for v in by_lang.values()):
        raise AgentConfigError("voice.tts_by_language 必須是 {\"語言\": {...}} 形式的對象")
    if not isinstance(voice["chat_rules"], str):
        raise AgentConfigError("voice.chat_rules 必須是字符串")
    ms = voice["silence_ms"]
    if not isinstance(ms, int) or isinstance(ms, bool) or ms <= 0:
        raise AgentConfigError("voice.silence_ms 必須是正整數（毫秒），例如 800")
    return voice


def _positive_int(x):
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def parse_pron_config(pron) -> dict:
    # 校驗 pronunciation 段並補全默認值；空對象表示不支持發音課。
    if not isinstance(pron, dict):
        raise AgentConfigError("pronunciation 必須是對象 {...}（不需要發音課可以不寫）")
    if not pron:
        return {}
    pron = _merge(PRON_DEFAULTS, pron)
    for key in ("language", "voice", "slow_rate"):
        if not isinstance(pron[key], str) or not pron[key].strip():
            raise AgentConfigError(f"pronunciation.{key} 必須是非空字符串")
    for key in ("passage_words", "practice_sentences", "practice_words", "max_attempts",
                "auto_stop_silence_ms", "max_record_seconds"):
        if not _positive_int(pron[key]):
            raise AgentConfigError(f"pronunciation.{key} 必須是正整數")
    if pron["max_record_seconds"] > 60:
        raise AgentConfigError("pronunciation.max_record_seconds 不能超過 60（Azure 短音頻評估的上限）")
    fw = pron["focus_words"]
    if not (isinstance(fw, list) and len(fw) == 2 and all(isinstance(x, int) and x >= 0 for x in fw)
            and fw[0] <= fw[1]):
        raise AgentConfigError("pronunciation.focus_words 必須是 [最少, 最多] 兩個 >= 0 的整數，例如 [3, 5]")
    ps = pron["pass_score"]
    if not isinstance(ps, (int, float)) or isinstance(ps, bool) or not 0 < ps <= 100:
        raise AgentConfigError("pronunciation.pass_score 必須是 1-100 的數字")
    if pron["stage"] not in ("auto", 1, 2, 3):
        raise AgentConfigError("pronunciation.stage 必須是 \"auto\" 或 1 / 2 / 3")
    prompts = pron["prompts"]
    if not isinstance(prompts, dict) or not all(isinstance(v, str) for v in prompts.values()):
        raise AgentConfigError("pronunciation.prompts 必須是 {\"passage\": \"...\", \"feedback\": \"...\"}")
    return pron


LEVEL_RE = re.compile(r"\b[ABC][12]\b")   # CEFR 等級 A1-C2


def _apply_level(cfg):
    # 學生等級只在 level 字段設定一次：提示詞裡用 {level} 佔位，這裡統一替換。
    level = cfg["level"]
    if not isinstance(level, str) or not level.strip():
        raise AgentConfigError("level 必須是非空字符串，例如 \"A2\" 或 \"B1-B2\"")

    def fill(where, text):
        found = LEVEL_RE.search(text)
        if found:
            raise AgentConfigError(f"{where} 裡寫死了等級 \"{found.group()}\"，請改用 {{level}}，等級只在 level 字段設定")
        return text.replace("{level}", level)

    cfg["persona"] = fill("persona", cfg["persona"])
    for key, text in cfg["prompts"].items():
        cfg["prompts"][key] = fill(f"prompts.{key}", text)
    for i, ph in enumerate(cfg["phases"]):
        ph["instruction"] = fill(f"phases[{i}].instruction", ph["instruction"])
    cfg["nudge"]["levels"] = [fill(f"nudge.levels[{i}]", t) for i, t in enumerate(cfg["nudge"]["levels"])]
    if cfg["voice"]:
        cfg["voice"]["chat_rules"] = fill("voice.chat_rules", cfg["voice"]["chat_rules"])


def list_agents(agents_dir=AGENTS_DIR) -> list[str]:
    return sorted(p.stem for p in Path(agents_dir).glob("*.json"))


def read_agent_file(name, agents_dir=AGENTS_DIR) -> dict:
    # 讀取並校驗 agents/{name}.json，返回補全默認值後的配置。
    path = Path(agents_dir) / f"{name}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AgentConfigError(f"找不到 agent 文件 {path}")
    except json.JSONDecodeError as e:
        raise AgentConfigError(f"{path.name} 不是合法 JSON：第 {e.lineno} 行第 {e.colno} 列 {e.msg}")
    try:
        return parse_agent_config(raw)
    except AgentConfigError as e:
        raise AgentConfigError(f"{path.name}：{e}")


# =====================================================================
# 話題列表（topics.json）
# =====================================================================

def load_topics(path=TOPICS_FILE) -> list[str]:
    # 讀取 topics.json：{"topics": ["Travel", "Food", ...]}
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AgentConfigError(f"找不到話題文件 {path}")
    except json.JSONDecodeError as e:
        raise AgentConfigError(f"{path.name} 不是合法 JSON：第 {e.lineno} 行第 {e.colno} 列 {e.msg}")
    topics = data.get("topics") if isinstance(data, dict) else None
    if not isinstance(topics, list) or not topics or not all(isinstance(t, str) and t.strip() for t in topics):
        raise AgentConfigError(f"{path.name}：topics 必須是非空的字符串列表")
    return [t.strip() for t in topics]


# =====================================================================
# LLM 適配器：統一成 stream()（流式文本）和 json()（結構化輸出）
# =====================================================================

class LLMError(RuntimeError):
    pass


class LLMProvider:
    # 各家 LLM 的統一接口。messages 格式：[{"role": "user" | "assistant", "content": str}]

    def stream(self, system: str, messages: list[dict]) -> Iterator[str]:
        raise NotImplementedError

    def json(self, system: str, messages: list[dict], schema: dict) -> dict:
        raise NotImplementedError


def _normalize(messages):
    # 合併相鄰同角色消息，並保證以 user 開頭（部分廠商要求）。
    out = []
    for m in messages:
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + m["content"]}
        else:
            out.append(dict(m))
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "[The lesson starts.]"})
    return out


def _loads_json(text):
    # 解析模型返回的 JSON，兼容外面包了 markdown 代碼塊的情況。
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"模型返回的不是合法 JSON：{e}") from e


class AnthropicProvider(LLMProvider):
    # 這些模型支持服務端拒答兜底（fallbacks: "default"）
    FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

    def __init__(self, model, effort="low", max_tokens=4096, **_):
        import anthropic
        self.client = anthropic.Anthropic()
        self.model, self.effort, self.max_tokens = model, effort, max_tokens

    def stream(self, system, messages):
        kwargs = dict(
            model=self.model, max_tokens=self.max_tokens, system=system,
            messages=_normalize(messages), output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
        )
        if self.model in self.FALLBACK_MODELS:
            ctx = self.client.beta.messages.stream(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            ctx = self.client.messages.stream(**kwargs)
        with ctx as s:
            yield from s.text_stream
            final = s.get_final_message()
        if final.stop_reason == "refusal":
            raise LLMError("模型拒絕回答")

    def json(self, system, messages, schema):
        resp = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system,
            messages=_normalize(messages),
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
        )
        if resp.stop_reason == "refusal":
            raise LLMError("模型拒絕回答")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return _loads_json(text)


class GeminiProvider(LLMProvider):
    def __init__(self, model, **_):
        from google import genai
        from google.genai import types
        self.client = genai.Client()  # 讀取 GEMINI_API_KEY / GOOGLE_API_KEY
        self.types = types
        self.model = model

    def _contents(self, messages):
        t = self.types
        return [t.Content(role="model" if m["role"] == "assistant" else "user",
                          parts=[t.Part(text=m["content"])]) for m in _normalize(messages)]

    def stream(self, system, messages):
        config = self.types.GenerateContentConfig(system_instruction=system)
        for chunk in self.client.models.generate_content_stream(
                model=self.model, contents=self._contents(messages), config=config):
            if chunk.text:
                yield chunk.text

    def json(self, system, messages, schema):
        config = self.types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json",
            response_json_schema=schema)
        resp = self.client.models.generate_content(
            model=self.model, contents=self._contents(messages), config=config)
        return _loads_json(resp.text or "")


class OpenAICompatProvider(LLMProvider):
    # OpenAI 官方，或任何 OpenAI 兼容接口（base_url 指向自建服務，如 Ollama）
    def __init__(self, model, base_url=None, api_key_env="OPENAI_API_KEY", **_):
        from openai import OpenAI
        # 自建服務通常不需要 Key，但 SDK 不允許 Key 為空，填一個佔位值
        api_key = os.getenv(api_key_env) or ("not-needed" if base_url else None)
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def stream(self, system, messages):
        stream = self.client.chat.completions.create(
            model=self.model, stream=True,
            messages=[{"role": "system", "content": system}] + _normalize(messages))
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def json(self, system, messages, schema):
        # 很多兼容廠商不支持 json_schema，統一用 json_object + 在提示裡給出 schema
        system = f"{system}\n\nReply with a JSON object only, matching this JSON Schema:\n{json.dumps(schema)}"
        resp = self.client.chat.completions.create(
            model=self.model, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}] + _normalize(messages))
        return _loads_json(resp.choices[0].message.content or "")


def make_provider(llm_cfg: dict) -> LLMProvider:
    # 按 JSON 裡的 llm 配置創建適配器；除 provider 外的字段都作為構造參數傳入。
    cls = {"anthropic": AnthropicProvider, "gemini": GeminiProvider,
           "openai_compat": OpenAICompatProvider}[llm_cfg["provider"]]
    args = {k: v for k, v in llm_cfg.items() if k != "provider"}
    return cls(**args)


# =====================================================================
# 結構化輸出的 schema
# =====================================================================

def corrector_schema(categories):
    # 糾錯結果的格式：{"errors": [{category, original, corrected, explanation_en, severity}]}
    return {
        "type": "object",
        "properties": {
            "errors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": list(categories)},
                        "original": {"type": "string"},
                        "corrected": {"type": "string"},
                        "explanation_en": {"type": "string"},
                        "severity": {"type": "string", "enum": ["minor", "major"]},
                    },
                    "required": ["category", "original", "corrected", "explanation_en", "severity"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["errors"],
        "additionalProperties": False,
    }


# 總結報告的格式（錯誤統計 stats 不讓模型算，由代碼根據糾錯記錄統計）
REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "key_errors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "original": {"type": "string"},
                    "corrected": {"type": "string"},
                    "explanation_zh": {"type": "string"},
                    "example": {"type": "string"},
                },
                "required": ["original", "corrected", "explanation_zh", "example"],
                "additionalProperties": False,
            },
        },
        "good_expressions": {"type": "array", "items": {"type": "string"}},
        "new_words": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"word": {"type": "string"}, "meaning_zh": {"type": "string"}},
                "required": ["word", "meaning_zh"],
                "additionalProperties": False,
            },
        },
        "overall_zh": {"type": "string"},
    },
    "required": ["topic", "key_errors", "good_expressions", "new_words", "overall_zh"],
    "additionalProperties": False,
}


BUBBLE_BREAK_RE = re.compile(r"\n\s*\n")   # 空行：分隔老師的多條訊息


def split_bubbles(text, max_bubbles) -> list[str]:
    # 把老師的回覆按空行拆成多條訊息（氣泡），去掉空段；超過上限的部分併入最後一條。max_bubbles=1 時原樣返回。
    if max_bubbles <= 1:
        return [text]
    parts = [p.strip() for p in BUBBLE_BREAK_RE.split(text) if p.strip()]
    if len(parts) > max_bubbles:
        parts[max_bubbles - 1:] = ["\n\n".join(parts[max_bubbles - 1:])]
    return parts


def clean_errors(data, categories):
    # 容錯：模型輸出缺字段 / 類別不認識 / 嚴重度不對時修正或丟棄。
    items = data.get("errors", []) if isinstance(data, dict) else data
    out = []
    for e in items if isinstance(items, list) else []:
        if not isinstance(e, dict):
            continue
        original, corrected = str(e.get("original", "")).strip(), str(e.get("corrected", "")).strip()
        if not original or not corrected or original == corrected:
            continue
        out.append({
            "category": e.get("category") if e.get("category") in categories else "other",
            "original": original,
            "corrected": corrected,
            "explanation_en": str(e.get("explanation_en", "")).strip(),
            "severity": e.get("severity") if e.get("severity") in ("minor", "major") else "minor",
        })
    return out


# =====================================================================
# Agent / Session
# =====================================================================

@dataclass
class Event:
    # tick() 返回給 app.py 的事件。chunks 不為空時需要流式顯示。
    kind: str                       # nudge / farewell / phase_change（有 intro 時帶 chunks）/ report / ended
    phase: str = ""
    chunks: Iterator[str] | None = None


_db = None
_sessions: dict[int, "Session"] = {}   # 內存中的課堂，app 多次 open 時拿到同一個對象


def get_db():
    # 全局數據庫連接（第一次使用時創建 data/vtutor.db）。
    global _db
    if _db is None:
        _db = DB()
    return _db


def load_agent(name, db=None, agents_dir=AGENTS_DIR) -> "Agent":
    return Agent(name, read_agent_file(name, agents_dir), db or get_db())


def list_sessions(db=None) -> list[dict]:
    return (db or get_db()).list_sessions()


def delete_session(session_id, db=None, clock=time.time):
    # 從歷史列表中刪除一節已結束的課（軟刪除，數據庫記錄保留）。正在上的課不能刪。
    db = db or get_db()
    session = _sessions.get(session_id)
    row = db.get_session(session_id)
    if (session is not None and not session.ended) or (row and row["status"] != "ended"):
        raise ValueError("正在上的課不能刪除，請先結束這節課")
    _sessions.pop(session_id, None)
    db.hide_session(session_id, clock())


def open_session(session_id, db=None, clock=time.time) -> "Session":
    # 取出一節課：在內存裡就直接返回，否則從數據庫恢復（例如重啟應用後點開歷史課堂）。
    if session_id in _sessions:
        return _sessions[session_id]
    db = db or get_db()
    row = db.get_session(session_id)
    if row is None:
        raise KeyError(f"找不到課堂 {session_id}")
    # 用開課時保存的 JSON 快照，保證和當時的配置一致
    agent = Agent(row["agent_name"], json.loads(row["agent_json"]), db, clock=clock)
    session = Session.restore(agent, row)
    _sessions[session_id] = session
    return session


class Agent:
    # 一位老師：JSON 配置 + LLM 適配器。providers / clock / sleep 可注入，測試時用假模型、假時鐘和假等待。

    def __init__(self, name, config, db, providers=None, clock=time.time, sleep=time.sleep):
        self.name, self.config, self.db, self.clock, self.sleep = name, config, db, clock, sleep
        self._providers = providers or {}

    @property
    def title(self):
        return self.config["name"]

    @property
    def has_voice(self):
        # JSON 裡配置了 voice 才能上語音課
        return bool(self.config["voice"])

    @property
    def has_pron(self):
        # JSON 裡配置了 pronunciation 才能上發音課（.get：舊課堂保存的配置快照裡沒有這個字段）
        return bool(self.config.get("pronunciation"))

    def provider(self, role) -> LLMProvider:
        # role：chat（回覆、關懷、總結）或 corrector（糾錯）。第一次使用時才創建。
        if role not in self._providers:
            self._providers[role] = make_provider(self.config["llm"][role])
        return self._providers[role]

    def start_session(self, topic=None, topics=None, mode="text") -> "Session":
        # topic：學生選的話題；沒選時從話題列表（默認 topics.json）裡推薦一個。mode：text / voice。
        if mode not in MODES:
            raise ValueError(f"mode 必須是 {' / '.join(MODES)}")
        if mode == "voice" and not self.has_voice:
            raise AgentConfigError(f"{self.title} 沒有配置 voice，只能上文字課（請在 Lesson 裡選 Text lesson）")
        if mode == "pron" and not self.has_pron:
            raise AgentConfigError(f"{self.title} 沒有配置 pronunciation，不能上發音課")
        memory = self.db.recent_memory(self.name, self.config["memory"]["load_recent_sessions"])
        if not topic:
            topics = topics if topics is not None else load_topics()
            # 老師推薦：避開最近上過的話題
            fresh = [t for t in topics if t not in memory["topics"]] or topics
            topic = random.choice(fresh) if fresh else None
        now = self.clock()
        sid = self.db.create_session(self.name, self.config, topic, now, mode)
        session = Session(self, sid, now, topic, memory, mode)
        _sessions[sid] = session
        return session


class Session:
    """一節課。對話狀態保存在內存（view 等），每句話同時寫入數據庫，重啟後可用 restore() 恢復。

    併發：Gradio 會同時調用 ask()（學生發言）和 tick()（計時器），
    所有生成文字的操作都要先拿到 _lock，保證老師一次只說一句話。
    """

    def __init__(self, agent: Agent, session_id, started_at, topic, memory, mode="text"):
        self.agent, self.cfg, self.db = agent, agent.config, agent.db
        self.id, self.started_at, self.topic, self.memory = session_id, started_at, topic, memory
        self.mode = mode                  # text / voice / pron，開課時確定，本節課不變
        # 給 UI 顯示：{"role", "content", "kind"}；學生的話另有 "via"，老師的話說完後有 "done"
        self.view: list[dict] = []
        self.corrections: list[dict] = []
        self.explained: dict[str, int] = {}
        self.phase_index = 0              # 已經通知過的階段
        self.ended = False
        self.report = None
        self.silence_since = started_at   # 老師最近一次正常發言的結束時間
        self.nudge_count = 0
        self.said_farewell = False        # 下課前的道別說過沒有
        self._lock = threading.Lock()     # 同一時間只允許一個 LLM 生成（回覆 / 關懷 / 總結 / 道別）

    @classmethod
    def restore(cls, agent, row):
        # 從數據庫重建課堂：對話、糾錯計數、停頓計時、報告、當前階段。
        db = agent.db
        s = cls(agent, row["id"], row["started_at"], row["topic"],
                db.recent_memory(agent.name, agent.config["memory"]["load_recent_sessions"],
                                 exclude_session_id=row["id"]),
                row["mode"])
        for m in db.get_messages(row["id"]):
            if m["role"] == "agent":
                s.view.append({"role": "assistant", "content": m["content"], "kind": m["kind"], "done": True})
            else:
                s.view.append({"role": "user", "content": m["content"], "kind": m["kind"], "via": m["via"]})
            if m["role"] == "agent" and m["kind"] != "nudge":
                s.silence_since = m["created_at"]
                s.nudge_count = 0
            elif m["kind"] == "nudge":
                s.nudge_count += 1
            if m["kind"] == "farewell":
                s.said_farewell = True
        s.corrections = db.get_corrections(row["id"])
        for c in s.corrections:
            if c["mention"] == "explain":
                s.explained[c["category"]] = s.explained.get(c["category"], 0) + 1
        s.report = db.get_summary(row["id"])
        s.ended = row["status"] == "ended"
        idx = s._phase_index_at(agent.clock())
        if idx is None:
            s.phase_index = len(s.phases)  # 時間已到，下一次 tick 會結束課堂
        elif idx > 0 and s.report is None and s.phases[idx].get("on_enter") == "summarize":
            s.phase_index = idx - 1        # 總結報告還沒生成，退一格讓下一次 tick 補上
        else:
            s.phase_index = idx
        return s

    # ---------------- 時間與階段 ----------------

    @property
    def phases(self):
        return self.cfg["phases"]

    @property
    def total_seconds(self):
        return sum(p["minutes"] for p in self.phases) * 60

    def _phase_index_at(self, now):
        # 返回 now 時刻所在階段的下標；課程時間結束返回 None。
        elapsed, acc = now - self.started_at, 0.0
        for i, ph in enumerate(self.phases):
            acc += ph["minutes"] * 60
            if elapsed < acc:
                return i
        return None

    def current_phase(self):
        return self.phases[min(self.phase_index, len(self.phases) - 1)]

    def status(self, now=None) -> dict:
        now = self.agent.clock() if now is None else now
        elapsed = max(0.0, now - self.started_at)
        ph = self.current_phase()
        return {
            "agent": self.agent.title,
            "topic": self.topic,
            "phase": "ended" if self.ended else ph["id"],
            "phase_label": "Ended" if self.ended else ph["label"],
            "elapsed": elapsed,
            "remaining": 0.0 if self.ended else max(0.0, self.total_seconds - elapsed),
            "total": self.total_seconds,
            "ended": self.ended,
        }

    # ---------------- 提示詞 ----------------

    def _system_prompt(self, phase):
        # 系統提示詞 = 人設 + 學生信息（等級、長期記憶）+ 本節話題和當前階段 + 對話規則。
        cfg = self.cfg
        lang = "English only." if phase["language"] == "en" else f"Language: {phase['language']}."
        lines = [cfg["persona"], "", "## Student", f"Level: {cfg['level']}."]
        if self.memory["topics"]:
            lines.append(f"Recent lessons covered: {', '.join(self.memory['topics'])}.")
        if self.memory["error_counts"]:
            freq = ", ".join(f"{k} x{v}" for k, v in self.memory["error_counts"].items())
            lines.append(f"Frequent error types in recent lessons: {freq}.")
        lines += ["", "## This lesson", f"Topic: {self.topic or 'free conversation'}.",
                  f"Current phase: {phase['instruction']}", lang]
        if cfg["prompts"]["chat_rules"]:
            lines += ["", "## Rules", cfg["prompts"]["chat_rules"]]
        if self.mode == "voice" and cfg["voice"]["chat_rules"]:
            lines += ["", "## Voice lesson", cfg["voice"]["chat_rules"]]
        return "\n".join(lines)

    def _history(self):
        # 給 LLM 的對話歷史（不含報告卡片）。
        return [{"role": m["role"], "content": m["content"]}
                for m in self.view if m["kind"] != "report"]

    # ---------------- 生成並保存 ----------------

    def _generate(self, system, messages, kind) -> Iterator[str]:
        # 流式生成老師的話，追加到 view，結束後入庫。調用方需持有 _lock。
        # 普通回覆邊流邊按空行拆成多條訊息；關懷不拆（恢復時按條數計關懷次數），總結不拆（本來就可能分段）。
        max_bubbles = self.cfg["max_bubbles"] if kind == "normal" else 1
        entries = [{"role": "assistant", "content": "", "kind": kind}]
        self.view.append(entries[0])
        raw = ""
        try:
            for chunk in self.agent.provider("chat").stream(system, messages):
                raw += chunk
                for i, part in enumerate(split_bubbles(raw, max_bubbles)):
                    if i == len(entries):
                        # 新的一條：上一條已經說完，先入庫（語音課可以馬上開始念），
                        # 讓它完整顯示，停頓一下再開始，像真人連發訊息
                        self._saved(entries[-1])
                        yield ""
                        self.agent.sleep(random.uniform(*self.cfg["bubble_pause_seconds"]))
                        entries.append({"role": "assistant", "content": "", "kind": kind})
                        self.view.append(entries[-1])
                    entries[i]["content"] = part
                yield chunk
        except Exception:
            log.exception("chat 生成失敗")
            msg = "(Sorry, I had a connection problem. Could you say that again?)"
            if not raw.strip():
                entries[0]["content"] = msg
                yield msg
            else:
                entries[-1]["content"] += " …"
        self._saved(entries[-1])

    def _saved(self, entry):
        # 老師的一句話說完後入庫，並（非關懷時）重新開始停頓計時。標記 done 後語音課才會念這一條。
        now = self.agent.clock()
        self.db.add_message(self.id, "agent", entry["content"], entry["kind"], now)
        entry["done"] = True
        if entry["kind"] != "nudge":
            self.silence_since = now
            self.nudge_count = 0

    def _say(self, text, kind) -> Iterator[str]:
        # 老師說一句 JSON 裡寫死的話（開場白 / 階段固定話術 / 道別語），{topic} 換成本節話題，不調用模型。
        # 調用方需持有 _lock。
        text = text.replace("{topic}", self.topic or "anything you like")
        entry = {"role": "assistant", "content": text, "kind": kind}
        self.view.append(entry)
        self._saved(entry)
        yield text

    def _locked(self, gen) -> Iterator[str]:
        # 包裝生成器：迭代期間一直持有 _lock。
        with self._lock:
            yield from gen

    # ---------------- 開場白 ----------------

    def greet(self) -> Iterator[str]:
        # 開場白：JSON 寫了 greeting 就直接用，否則讓模型生成。
        def run():
            greeting = self.cfg["greeting"]
            if greeting:
                yield from self._say(greeting, "normal")
                return
            phase = self.current_phase()
            note = (f"[The lesson starts now. Today's topic: {self.topic or 'free conversation'}. "
                    "Greet the student warmly and start the warm-up.]")
            yield from self._generate(self._system_prompt(phase),
                                      [{"role": "user", "content": note}], "normal")
        return self._locked(run())

    # ---------------- 一輪對話：糾錯 → 回覆 ----------------

    def ask(self, question: str, via="text") -> Iterator[str]:
        # 學生發言：先糾錯（入庫），再把要口頭指出的錯誤作為私密筆記交給老師，流式返回老師的回覆。
        # via：text（打字）/ voice（語音轉寫，糾錯時忽略拼寫和標點）。老師道別之後只記錄學生的話，不再回覆。
        def run():
            question_text = question.strip()
            if self.ended:
                yield "This lesson has ended. Click \"+ New Lesson\" to start a new one!"
                return
            now = self.agent.clock()
            phase = self.current_phase()
            msg_id = self.db.add_message(self.id, "user", question_text, "normal", now, via)
            history = self._history()
            self.view.append({"role": "user", "content": question_text, "kind": "normal", "via": via})
            if self.said_farewell:
                return   # 老師已經道別：只記錄學生的話，不再回覆

            notes, errors = [], []
            if self.cfg["correction"]["enabled"] and phase["correction"]:
                errors = self._correct(question_text, history, via)
                notes.append(self._correction_note(errors))
                if errors:
                    self.db.add_corrections(self.id, msg_id, errors)
                    self.corrections.extend(errors)
            notes.append(self._ending_note(phase, errors))

            note = "\n\n".join(n for n in notes if n)
            content = question_text + (f"\n\n{note}" if note else "")
            messages = history + [{"role": "user", "content": content}]
            yield from self._generate(self._system_prompt(phase), messages, "normal")
        return self._locked(run())

    # 語音轉寫的句子，拼寫和標點是識別模型決定的，不算學生的錯
    VOICE_CORRECTOR_NOTE = ("The student's message is a speech-to-text transcript of what they said aloud. "
                            "Ignore spelling, punctuation and capitalisation completely; never report "
                            "'spelling' errors. Judge only what the student actually said.")

    def _correct(self, question, history, via="text") -> list[dict]:
        # 調用糾錯模型分析學生這一句；失敗時返回空列表，不影響對話。
        cats = self.cfg["correction"]["categories"]
        # 老師上一輪可能分成了多條訊息，取末尾連續的老師訊息拼起來
        last_teacher = []
        for m in reversed(history):
            if m["role"] == "assistant":
                last_teacher.insert(0, m["content"])
            elif last_teacher:
                break
        last_teacher = "\n\n".join(last_teacher)
        prompt = (f"Teacher's previous message:\n{last_teacher or '(none)'}\n\n"
                  f"Student's latest message:\n{question}")
        system = (self.cfg["prompts"]["corrector"] +
                  "\n\nCategories: " + ", ".join(cats))
        if via == "voice":
            system += "\n\n" + self.VOICE_CORRECTOR_NOTE
        try:
            data = self.agent.provider("corrector").json(
                system, [{"role": "user", "content": prompt}], corrector_schema(cats))
        except Exception:
            # 糾錯失敗不影響對話繼續
            log.exception("糾錯失敗，本輪跳過")
            return []
        errors = clean_errors(data, cats)
        if via == "voice":
            errors = [e for e in errors if e["category"] != "spelling"]
        self._select_mentions(errors)
        return errors

    def _select_mentions(self, errors):
        """決定老師這輪口頭說哪些錯：最多 max_per_turn 個，優先 major 和中式英語。
        同類錯誤解釋次數到上限後只重述（recast）。直接在 errors 上寫 mention 字段。"""
        corr = self.cfg["correction"]
        ranked = sorted(errors, key=lambda e: (e["severity"] != "major", e["category"] != "chinglish"))
        for e in errors:
            e["mention"] = ""
        for e in ranked[:corr["max_per_turn"]]:
            n = self.explained.get(e["category"], 0)
            if n < corr["explain_limit_per_category"]:
                e["mention"] = "explain"
                self.explained[e["category"]] = n + 1
            else:
                e["mention"] = "recast"

    @staticmethod
    def _correction_note(errors):
        # 把要口頭指出的錯誤寫成方括號筆記，附在學生消息後交給老師（學生看不到）。
        mentioned = [e for e in errors if e["mention"]]
        if not mentioned:
            return "[Correction notes: nothing to correct this turn. Just reply naturally.]"
        lines = ["[Correction notes for this turn (the student cannot see this):"]
        for e in mentioned:
            # 模型容易「好心」繼續糾正，所以 RECAST 要明確禁止解釋和讓學生重說
            how = ("EXPLAIN" if e["mention"] == "explain" else
                   "RECAST (just use the correct form naturally, without bold; do NOT explain it, and do NOT ask "
                   "the student to repeat it or say a full sentence)")
            lines.append(f"- {how}: \"{e['original']}\" -> \"{e['corrected']}\" ({e['category']}: {e['explanation_en']})")
        lines.append("Do not mention any other errors.]")
        return "\n".join(lines)

    # 這兩類錯誤要「解釋」時，老師必須用問題結尾（請學生說完整句子 / 換個說法重問）
    QUESTION_CATEGORIES = ("incomplete_sentence", "misunderstood_question")

    def _questions_in_a_row(self):
        # 老師最近連續幾輪以問句結尾。每輪看最後一條訊息（後面緊跟學生發言，或是 view 的最後一條）。
        endings = []
        for i, m in enumerate(self.view):
            if m["role"] == "assistant" and m["kind"] == "normal" and (
                    i + 1 == len(self.view) or self.view[i + 1]["role"] == "user"):
                endings.append(m["content"].rstrip().endswith("?"))
        n = 0
        for is_question in reversed(endings):
            if not is_question:
                break
            n += 1
        return n

    def _ending_note(self, phase, errors):
        # 聊天不總是老師提問：連續問了 max_questions_in_a_row 輪後，提醒這輪用陳述句結尾。
        limit = self.cfg["max_questions_in_a_row"]
        if not limit or not phase["vary_endings"] or self._questions_in_a_row() < limit:
            return ""
        if any(e["mention"] == "explain" and e["category"] in self.QUESTION_CATEGORIES for e in errors):
            return ""
        return ("[Style note: your last few replies all ended with a question. This time end with a short "
                "statement instead, not a question - for example react to what the student said, or share a "
                "related thought or a bit about yourself - and let the student respond freely.]")

    # ---------------- 計時：階段切換、停頓關懷、道別、總結 ----------------

    def tick(self, now=None) -> Event | None:
        # app.py 每隔幾秒調用一次。正在生成時直接返回 None。發音課不計時，流程由 pron.py 推進。
        if self.ended or self.mode == "pron" or self._lock.locked():
            return None
        now = self.agent.clock() if now is None else now
        with self._lock:
            event = self._phase_event(now)
            if event is None:
                event = self._farewell_event(now)
            if event is None:
                event = self._nudge_event(now)
        return event

    def _farewell_event(self, now):
        # 課程結束前 farewell_seconds 秒，老師主動道別一次（之前的階段裡不說再見）。
        sec = self.cfg["farewell_seconds"]
        if not sec or self.said_farewell:
            return None
        remaining = self.total_seconds - (now - self.started_at)
        if remaining > sec:
            return None
        self.said_farewell = True
        phase = self.current_phase()
        farewell = self.cfg["farewell"]
        if farewell:
            gen = self._say(farewell, "farewell")
        else:
            note = (f"[The lesson ends in about {max(1, int(remaining))} seconds. Say a short, warm goodbye now "
                    "(for example: see you next time). Do not ask any more questions.]")
            messages = self._history() + [{"role": "user", "content": note}]
            gen = self._generate(self._system_prompt(phase), messages, "farewell")
        return Event("farewell", phase=phase["id"], chunks=self._locked(gen))

    def _phase_event(self, now):
        # 進入新階段時返回事件：先說本階段的固定話術（intro），總結階段接著生成報告和解讀；時間用完則下課。
        idx = self._phase_index_at(now)
        if idx is not None and idx <= self.phase_index:
            return None
        if idx is None:
            self._end(now)
            return Event("ended", phase="ended")
        self.phase_index = idx
        phase = self.phases[idx]
        summarize = phase.get("on_enter") == "summarize" and self.cfg["summary"]["enabled"]
        if not summarize and not phase["intro"]:
            return Event("phase_change", phase=phase["id"])

        def run():
            if phase["intro"]:
                yield from self._say(phase["intro"], "intro")
            if summarize:
                yield from self._summarize(phase)
        return Event("report" if summarize else "phase_change", phase=phase["id"], chunks=self._locked(run()))

    def _nudge_event(self, now):
        # 老師說完後學生沉默超過閾值，返回一次關懷；次數用完後不再打擾。
        nudge = self.cfg["nudge"]
        if not nudge["enabled"] or not self.current_phase()["nudge"] or self.said_farewell:
            return None
        if not self.view or self.view[-1]["role"] != "assistant":
            return None
        th = nudge["thresholds_seconds"]
        if self.nudge_count >= len(th):
            return None
        silent = now - self.silence_since
        if silent < th[self.nudge_count]:
            return None
        self.nudge_count += 1
        level = nudge["levels"][min(self.nudge_count, len(nudge["levels"])) - 1]
        note = (f"[The student has not replied for {int(silent)} seconds. "
                f"Gentle check-in #{self.nudge_count}. {level}]")
        return Event("nudge", phase=self.current_phase()["id"],
                     chunks=self._locked(self._nudge(note, len(self.view))))

    def _nudge(self, note, view_len) -> Iterator[str]:
        # 從 tick() 到真正生成之間學生可能已經回覆了（對話變長），這時不再關懷
        if len(self.view) != view_len:
            return
        messages = self._history() + [{"role": "user", "content": note}]
        yield from self._generate(self._system_prompt(self.current_phase()), messages, "nudge")

    def _end(self, now):
        # 下課：標記結束、寫庫，並移出內存（之後點開歷史時從數據庫恢復）。
        self.ended = True
        self.phase_index = len(self.phases)
        self.db.end_session(self.id, now)
        _sessions.pop(self.id, None)

    def finish(self) -> Iterator[str]:
        # 學生主動提前結束：還沒總結過就先給報告和總結，然後結束課堂。
        def run():
            if self.ended:
                return
            summary_idx = next((i for i, ph in enumerate(self.phases)
                                if ph.get("on_enter") == "summarize"), None)
            has_talk = any(m["role"] == "user" for m in self.view)
            if (self.report is None and has_talk and summary_idx is not None
                    and self.cfg["summary"]["enabled"]):
                self.phase_index = summary_idx
                yield from self._summarize(self.phases[summary_idx], early=True)
            self._end(self.agent.clock())
        return self._locked(run())

    def _summarize(self, phase, early=False) -> Iterator[str]:
        # 生成錯誤報告（存庫 + 作為報告卡片放進 view），然後流式輸出繁中學習解讀。
        self.report = self.build_report()
        now = self.agent.clock()
        self.db.save_summary(self.id, self.report, now)
        report_json = json.dumps(self.report, ensure_ascii=False)
        self.db.add_message(self.id, "agent", report_json, "report", now)
        self.view.append({"role": "assistant", "content": report_json, "kind": "report"})
        yield ""  # 讓 UI 先把報告卡片顯示出來
        note = ("[Lesson report (shown to the student as a card):\n" + report_json +
                "\nNow start the summary phase as instructed.]")
        if phase["intro"] and not early:
            # 剛說過固定話術，避免模型再說一遍「我們來總結一下」
            note = note[:-1] + (f" You have just said: \"{phase['intro']}\". Do not say it again or announce "
                                "the summary; go straight into the walkthrough.]")
        if early:
            note = note[:-1] + (" The student has to leave early, so keep it short, do not invite"
                                " questions, and end with a warm goodbye now.]")
        messages = self._history() + [{"role": "user", "content": note}]
        yield from self._generate(self._system_prompt(phase), messages, "summary")

    def build_report(self) -> dict:
        # 總結報告：錯誤統計由代碼計算，重點錯誤、好表達、生詞、點評由模型生成。
        cats = self.cfg["correction"]["categories"]
        counts: dict[str, int] = {}
        for c in self.corrections:
            counts[c["category"]] = counts.get(c["category"], 0) + 1
        stats = [{"category": k, "label": cats.get(k, k), "count": v}
                 for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]

        transcript = "\n".join(
            f"{'Teacher' if m['role'] == 'assistant' else 'Student'}: {m['content']}"
            for m in self._history())
        errors = [{k: c[k] for k in ("category", "original", "corrected", "explanation_en", "severity")}
                  for c in self.corrections]
        top = self.cfg["summary"]["top_errors"]
        prompt = (f"Transcript:\n{transcript}\n\nErrors found during the lesson:\n"
                  f"{json.dumps(errors, ensure_ascii=False)}\n\n"
                  f"Pick at most {top} key errors, at most 5 good expressions and at most 6 new words.")
        try:
            data = self.agent.provider("chat").json(
                self.cfg["prompts"]["summary"], [{"role": "user", "content": prompt}], REPORT_SCHEMA)
        except Exception:
            log.exception("總結報告生成失敗")
            data = {}
        return {
            "topic": data.get("topic") or self.topic or "",
            "stats": stats,
            "key_errors": (data.get("key_errors") or [])[:top],
            "good_expressions": data.get("good_expressions") or [],
            "new_words": data.get("new_words") or [],
            "overall_zh": data.get("overall_zh") or "",
        }
