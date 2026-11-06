# builder.py 測試：用假 LLM 和假時鐘，不調用任何 API。

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import builder  # noqa: E402
from db import DB  # noqa: E402

# 測試用固定的 agent 配置，不受 src/agents/ 裡文件增刪改名影響
AGENTS_DIR = Path(__file__).resolve().parent / "fixtures"


class FakeLLM(builder.LLMProvider):
    def __init__(self, reply="Nice! What did you do there?", json_result=None, fail_json=False, chunks=None):
        self.reply, self.json_result, self.fail_json = reply, json_result or {"errors": []}, fail_json
        self.chunks = chunks   # 指定流式輸出的分塊；不指定則按空格切
        self.calls = []

    def stream(self, system, messages):
        self.calls.append(("stream", system, messages))
        if self.chunks is not None:
            yield from self.chunks
            return
        for word in self.reply.split(" "):
            yield word + " "

    def json(self, system, messages, schema):
        self.calls.append(("json", system, messages))
        if self.fail_json:
            raise builder.LLMError("boom")
        return copy.deepcopy(self.json_result)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def base_config():
    # teacher.json 的內容，但時長和關懷閾值固定，RD 調 JSON 不影響測試。
    cfg = builder.read_agent_file("teacher", AGENTS_DIR)
    for ph, minutes in zip(cfg["phases"], [3, 20, 2, 5]):
        ph["minutes"] = minutes
    cfg["nudge"]["thresholds_seconds"] = [30, 60]
    cfg["correction"].update(max_per_turn=2, explain_limit_per_category=2)
    return cfg


class Sleep:
    # 假等待：只記錄每次等待的秒數，不真的停。
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def make_agent(config=None, chat=None, corrector=None, db=None, clock=None, sleep=None):
    cfg = config or base_config()
    return builder.Agent("teacher", cfg, db or DB(":memory:"),
                         providers={"chat": chat or FakeLLM(), "corrector": corrector or FakeLLM()},
                         clock=clock or Clock(), sleep=sleep or Sleep())


@pytest.fixture(autouse=True)
def clear_sessions():
    builder._sessions.clear()
    yield
    builder._sessions.clear()


def drain(gen):
    return "".join(gen)


# ---------------- JSON 解析校驗 ----------------

def test_teacher_json_is_valid():
    cfg = builder.read_agent_file("teacher", AGENTS_DIR)
    assert [p["id"] for p in cfg["phases"]] == ["warm_up", "main", "wrap_up", "summary"]
    assert cfg["phases"][-1]["on_enter"] == "summarize"


def test_list_agents_finds_teacher():
    assert "teacher" in builder.list_agents(AGENTS_DIR)


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c.pop("persona"), "persona"),
    (lambda c: c["llm"]["chat"].update(provider="foo"), "provider"),
    (lambda c: c.update(phases=[]), "phases"),
    (lambda c: c["phases"][0].update(minutes=-1), "minutes"),
    (lambda c: c["phases"][1].update(id="warm_up"), "重複"),
    (lambda c: c["nudge"].update(thresholds_seconds=[60, 30]), "從小到大"),
    (lambda c: c["phases"][0].update(on_enter="dance"), "on_enter"),
    (lambda c: c.update(max_bubbles=0), "max_bubbles"),
    (lambda c: c.update(bubble_pause_seconds=[3, 1]), "bubble_pause_seconds"),
    (lambda c: c.update(bubble_pause_seconds=2), "bubble_pause_seconds"),
    (lambda c: c.update(max_questions_in_a_row=-1), "max_questions_in_a_row"),
    (lambda c: c.update(farewell_seconds="10"), "farewell_seconds"),
    (lambda c: c.update(farewell=None), "farewell"),
])
def test_invalid_config_gives_clear_error(mutate, message):
    raw = json.loads((AGENTS_DIR / "teacher.json").read_text(encoding="utf-8"))
    mutate(raw)
    with pytest.raises(builder.AgentConfigError, match=message):
        builder.parse_agent_config(raw)


def test_level_is_filled_into_all_prompts():
    raw = json.loads((AGENTS_DIR / "teacher.json").read_text(encoding="utf-8"))
    raw["level"] = "B2"
    cfg = builder.parse_agent_config(raw)
    texts = [cfg["persona"], *cfg["prompts"].values()]
    assert all("{level}" not in t for t in texts)
    assert "B2" in cfg["persona"] and "B2" in cfg["prompts"]["corrector"] and "B2" in cfg["prompts"]["summary"]


def test_hardcoded_level_in_prompt_is_rejected():
    raw = json.loads((AGENTS_DIR / "teacher.json").read_text(encoding="utf-8"))
    raw["prompts"]["chat_rules"] += " Speak at B1 level."
    with pytest.raises(builder.AgentConfigError, match="寫死了等級 \"B1\""):
        builder.parse_agent_config(raw)


def test_bad_json_file_reports_line(tmp_path):
    (tmp_path / "broken.json").write_text('{"name": "x",\n  oops }', encoding="utf-8")
    with pytest.raises(builder.AgentConfigError, match="第 2 行"):
        builder.read_agent_file("broken", tmp_path)


def test_optional_sections_get_defaults():
    cfg = builder.parse_agent_config({
        "name": "Buddy", "persona": "A friend.",
        "llm": {"chat": {"provider": "anthropic", "model": "m"}},
        "phases": [{"id": "chat", "minutes": 10, "instruction": "Chat."}],
    })
    assert cfg["correction"]["enabled"] is False
    assert cfg["phases"][0]["label"] == "chat"
    assert cfg["max_bubbles"] == 1


# ---------------- 課堂階段 ----------------

def test_phase_progression_and_end():
    clock = Clock()
    agent = make_agent(clock=clock)
    s = agent.start_session("Travel")
    assert s.status()["phase"] == "warm_up"

    clock.t += 3 * 60 + 1
    ev = s.tick()
    assert ev.kind == "phase_change" and ev.phase == "main"
    assert s.tick() is None  # 同一階段不重複通知

    clock.t += 22 * 60  # 25 分鐘 → 總結
    ev = s.tick()
    assert ev.kind == "report" and ev.phase == "summary"
    drain(ev.chunks)
    assert s.report is not None
    assert any(m["kind"] == "report" for m in s.view)

    clock.t += 5 * 60
    ev = s.tick()
    assert ev.kind == "ended" and s.ended
    assert agent.db.get_session(s.id)["status"] == "ended"
    assert "has ended" in drain(s.ask("hello"))


def test_status_remaining_time():
    clock = Clock()
    s = make_agent(clock=clock).start_session("Food")
    clock.t += 60
    st = s.status()
    assert st["total"] == 30 * 60 and st["remaining"] == pytest.approx(29 * 60)


# ---------------- 停頓關懷 ----------------

def test_nudges_follow_thresholds():
    clock = Clock()
    s = make_agent(clock=clock).start_session("Travel")
    drain(s.greet())

    clock.t += 29
    assert s.tick() is None
    clock.t += 2  # 31 秒
    ev = s.tick()
    assert ev.kind == "nudge"
    drain(ev.chunks)
    assert s.view[-1]["kind"] == "nudge"

    clock.t += 20  # 51 秒：還沒到 60
    assert s.tick() is None
    clock.t += 10  # 61 秒
    ev = s.tick()
    assert ev.kind == "nudge"
    drain(ev.chunks)

    clock.t += 60  # 超過次數上限後安靜（仍在熱身階段內）
    assert s.tick() is None


def test_custom_thresholds_and_reset_after_reply():
    cfg = base_config()
    cfg["nudge"]["thresholds_seconds"] = [10]
    clock = Clock()
    s = make_agent(config=cfg, clock=clock).start_session("Travel")
    drain(s.greet())
    clock.t += 11
    drain(s.tick().chunks)
    clock.t += 5
    drain(s.ask("I like it."))  # 學生回覆後重新計時
    clock.t += 9
    assert s.tick() is None
    clock.t += 2
    assert s.tick().kind == "nudge"


def test_no_nudge_in_summary_phase():
    clock = Clock()
    s = make_agent(clock=clock).start_session("Travel")
    clock.t += 25 * 60 + 1
    drain(s.tick().chunks)  # 進入總結
    clock.t += 120
    assert s.tick() is None


def test_nudge_skipped_if_student_replied_first():
    clock = Clock()
    s = make_agent(clock=clock).start_session("Travel")
    drain(s.greet())
    clock.t += 31
    ev = s.tick()
    drain(s.ask("Sorry, I was thinking."))
    assert drain(ev.chunks) == ""
    assert not any(m["kind"] == "nudge" for m in s.view)


# ---------------- 糾錯 ----------------

def tense_error(original="I go to Beijing yesterday"):
    return {"category": "tense", "original": original, "corrected": "I went to Beijing yesterday",
            "explanation_en": "Use past tense for finished actions.", "severity": "major"}


def test_correction_note_reaches_tutor_and_is_saved():
    chat = FakeLLM()
    corrector = FakeLLM(json_result={"errors": [tense_error()]})
    s = make_agent(chat=chat, corrector=corrector).start_session("Travel")
    drain(s.ask("I go to Beijing yesterday."))

    last_user = chat.calls[-1][2][-1]["content"]
    assert "EXPLAIN" in last_user and "I went to Beijing yesterday" in last_user
    saved = s.db.get_corrections(s.id)
    assert len(saved) == 1 and saved[0]["mention"] == "explain"
    # 歷史裡保存的是學生原話，不含糾錯筆記
    assert s.view[-2]["content"] == "I go to Beijing yesterday."


def test_max_per_turn_prefers_major_and_chinglish():
    errors = [
        {"category": "article", "original": "a", "corrected": "the", "explanation_en": "", "severity": "minor"},
        {"category": "chinglish", "original": "open the light", "corrected": "turn on the light",
         "explanation_en": "", "severity": "minor"},
        tense_error(),
    ]
    s = make_agent(corrector=FakeLLM(json_result={"errors": errors})).start_session("Travel")
    drain(s.ask("..."))
    mention = {c["category"]: c["mention"] for c in s.db.get_corrections(s.id)}
    assert mention == {"article": "", "chinglish": "explain", "tense": "explain"}


def test_explain_limit_then_recast():
    s = make_agent(corrector=FakeLLM(json_result={"errors": [tense_error()]})).start_session("Travel")
    for _ in range(3):
        drain(s.ask("I go to Beijing yesterday."))
    mentions = [c["mention"] for c in s.db.get_corrections(s.id)]
    assert mentions == ["explain", "explain", "recast"]


def test_corrector_output_is_cleaned():
    cats = {"tense": "Tense", "other": "Other"}
    data = {"errors": [
        {"category": "weird", "original": "x", "corrected": "y", "explanation_en": "e", "severity": "huge"},
        {"category": "tense", "original": "same", "corrected": "same"},
        "not a dict",
    ]}
    assert builder.clean_errors(data, cats) == [
        {"category": "other", "original": "x", "corrected": "y", "explanation_en": "e", "severity": "minor"}]


def test_corrector_failure_does_not_block_reply():
    s = make_agent(corrector=FakeLLM(fail_json=True)).start_session("Travel")
    assert "What did you do" in drain(s.ask("I go to Beijing yesterday."))
    assert s.db.get_corrections(s.id) == []


def test_correction_disabled_skips_corrector():
    cfg = base_config()
    cfg["correction"]["enabled"] = False
    corrector = FakeLLM()
    s = make_agent(config=cfg, corrector=corrector).start_session("Travel")
    drain(s.ask("hello"))
    assert corrector.calls == []


def test_loads_json_strips_code_fence():
    assert builder._loads_json('```json\n{"errors": []}\n```') == {"errors": []}


# ---------------- 總結報告 ----------------

def test_report_stats_computed_from_corrections():
    chat = FakeLLM(json_result={"topic": "Trip", "key_errors": [], "good_expressions": ["a"],
                                "new_words": [], "overall_zh": "很好"})
    corrector = FakeLLM(json_result={"errors": [tense_error()]})
    clock = Clock()
    s = make_agent(chat=chat, corrector=corrector, clock=clock).start_session("Travel")
    drain(s.ask("I go to Beijing yesterday."))
    drain(s.ask("I go to Beijing yesterday."))
    clock.t += 25 * 60 + 1
    drain(s.tick().chunks)
    assert s.report["stats"] == [{"category": "tense", "label": "時態", "count": 2}]
    assert s.db.get_summary(s.id)["overall_zh"] == "很好"
    assert s.view[-1]["kind"] == "summary"


# ---------------- 存儲與恢復 ----------------

def test_open_session_restores_state():
    db = DB(":memory:")
    clock = Clock()
    corrector = FakeLLM(json_result={"errors": [tense_error()]})
    s = make_agent(db=db, corrector=corrector, clock=clock).start_session("Travel")
    drain(s.greet())
    drain(s.ask("I go to Beijing yesterday."))
    builder._sessions.clear()  # 模擬重啟

    clock.t += 60
    restored = builder.open_session(s.id, db=db, clock=clock)
    assert [m["role"] for m in restored.view] == ["assistant", "user", "assistant"]
    assert restored.explained == {"tense": 1}
    assert restored.started_at == s.started_at  # 計時不重置
    assert restored.status()["phase"] == "warm_up"


def test_recent_memory_avoids_last_topic():
    topics = ["Travel", "Food"]
    db = DB(":memory:")
    make_agent(db=db).start_session("Travel")
    assert make_agent(db=db).start_session(topics=topics).topic == "Food"


def test_teacher_choice_uses_topics_json():
    s = make_agent().start_session()
    assert s.topic in builder.load_topics()


def test_topics_json_validation(tmp_path):
    bad = tmp_path / "topics.json"
    bad.write_text('{"topics": []}', encoding="utf-8")
    with pytest.raises(builder.AgentConfigError, match="非空"):
        builder.load_topics(bad)


def test_list_sessions_newest_first():
    db = DB(":memory:")
    clock = Clock()
    a = make_agent(db=db, clock=clock)
    first = a.start_session("Travel")
    clock.t += 100
    second = a.start_session("Food")
    assert [r["id"] for r in builder.list_sessions(db)] == [second.id, first.id]


# ---------------- 主動結束 ----------------

def test_finish_early_gives_summary_then_ends():
    chat = FakeLLM()
    clock = Clock()
    s = make_agent(chat=chat, clock=clock).start_session("Travel")
    drain(s.greet())
    drain(s.ask("I like travel."))
    clock.t += 5 * 60  # 還在主題對話階段
    drain(s.finish())
    assert s.ended and s.db.get_session(s.id)["status"] == "ended"
    assert s.report is not None and s.db.get_summary(s.id) is not None
    assert [m["kind"] for m in s.view[-2:]] == ["report", "summary"]
    assert "leave early" in chat.calls[-1][2][-1]["content"]
    assert s.tick() is None


INTRO = "快到下課時間了，我們來做個總結吧！"


def intro_config():
    cfg = base_config()
    cfg["phases"][-1]["intro"] = INTRO
    return cfg


def test_phase_intro_said_before_report():
    chat = FakeLLM()
    clock = Clock()
    s = make_agent(config=intro_config(), chat=chat, clock=clock).start_session("Travel")
    drain(s.ask("I like travel."))
    clock.t += 25 * 60 + 1
    ev = s.tick()
    assert ev.kind == "report"
    drain(ev.chunks)
    assert [m["kind"] for m in s.view[-3:]] == ["intro", "report", "summary"]
    assert s.view[-3]["content"] == INTRO
    assert s.db.get_messages(s.id)[-3]["content"] == INTRO
    assert "Do not say it again" in chat.calls[-1][2][-1]["content"]   # 總結講解不再重複這句


def test_phase_intro_on_normal_phase():
    cfg = base_config()
    cfg["phases"][1]["intro"] = "Let's talk about {topic} now."
    clock = Clock()
    chat = FakeLLM()
    s = make_agent(config=cfg, chat=chat, clock=clock).start_session("Travel")
    clock.t += 3 * 60 + 1
    ev = s.tick()
    assert ev.kind == "phase_change" and drain(ev.chunks) == "Let's talk about Travel now."
    assert s.view[-1]["kind"] == "intro" and chat.calls == []


def test_phase_without_intro_has_no_chunks():
    clock = Clock()
    s = make_agent(clock=clock).start_session("Travel")
    clock.t += 3 * 60 + 1
    assert s.tick().chunks is None


def test_no_intro_when_student_leaves_early():
    # 學生主動結束時不說「快到下課時間了」
    clock = Clock()
    s = make_agent(config=intro_config(), clock=clock).start_session("Travel")
    drain(s.ask("I like travel."))
    drain(s.finish())
    assert not any(m["kind"] == "intro" for m in s.view)


def test_intro_must_be_string():
    raw = json.loads((AGENTS_DIR / "teacher.json").read_text(encoding="utf-8"))
    raw["phases"][-1]["intro"] = 1
    with pytest.raises(builder.AgentConfigError, match="intro"):
        builder.parse_agent_config(raw)


def test_finish_in_summary_phase_does_not_repeat_report():
    clock = Clock()
    s = make_agent(clock=clock).start_session("Travel")
    drain(s.ask("hello"))
    clock.t += 25 * 60 + 1
    drain(s.tick().chunks)  # 正常進入總結
    n = len(s.view)
    drain(s.finish())
    assert s.ended and len(s.view) == n


def test_finish_without_talk_skips_report():
    s = make_agent().start_session("Travel")
    drain(s.greet())
    drain(s.finish())
    assert s.ended and s.report is None


# ---------------- 刪除（軟刪除） ----------------

def test_delete_hides_ended_session_but_keeps_data():
    db = DB(":memory:")
    corrector = FakeLLM(json_result={"errors": [tense_error()]})
    s = make_agent(db=db, corrector=corrector).start_session("Travel")
    drain(s.ask("I go to Beijing yesterday."))
    drain(s.finish())
    builder.delete_session(s.id, db=db)
    assert builder.list_sessions(db) == []
    assert db.get_session(s.id)["deleted_at"] is not None
    assert len(db.get_corrections(s.id)) == 1 and db.get_messages(s.id)


def test_active_session_cannot_be_deleted():
    db = DB(":memory:")
    s = make_agent(db=db).start_session("Travel")
    with pytest.raises(ValueError):
        builder.delete_session(s.id, db=db)
    assert [r["id"] for r in builder.list_sessions(db)] == [s.id]


def test_old_database_gets_deleted_at_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, agent_name TEXT NOT NULL, agent_json TEXT NOT NULL, "
                 "started_at REAL NOT NULL, ended_at REAL, topic TEXT, status TEXT NOT NULL DEFAULT 'active')")
    conn.execute("INSERT INTO sessions (agent_name, agent_json, started_at) VALUES ('a', '{}', 1)")
    conn.commit(); conn.close()
    assert [r["id"] for r in DB(path).list_sessions()] == [1]


# ---------------- 固定開場白 ----------------

def test_fixed_greeting_skips_llm():
    cfg = base_config()
    cfg["greeting"] = "Hi! Today let's talk about {topic}."
    chat = FakeLLM()
    s = make_agent(config=cfg, chat=chat).start_session("Food")
    assert drain(s.greet()) == "Hi! Today let's talk about Food."
    assert chat.calls == []
    assert s.db.get_messages(s.id)[0]["content"] == "Hi! Today let's talk about Food."


def test_empty_greeting_uses_llm():
    cfg = base_config()
    cfg["greeting"] = ""
    chat = FakeLLM()
    drain(make_agent(config=cfg, chat=chat).start_session("Food").greet())
    assert len(chat.calls) == 1


# ---------------- 多條訊息（氣泡） ----------------

def bubbles_config(n=3):
    cfg = base_config()
    cfg["max_bubbles"] = n
    return cfg


def teacher_entries(view):
    return [m["content"] for m in view if m["role"] == "assistant"]


@pytest.mark.parametrize("text, n, expected", [
    ("Hi there!", 3, ["Hi there!"]),
    ("Oh nice!\n\nWhat did you eat?", 3, ["Oh nice!", "What did you eat?"]),
    ("A\n \n\nB\n\n\n\nC", 3, ["A", "B", "C"]),
    ("A\n\nB\n\nC\n\nD", 3, ["A", "B", "C\n\nD"]),
    ("Line one\nline two", 3, ["Line one\nline two"]),   # 單個換行不拆
    ("\n\n  ", 3, []),
    ("A\n\nB ", 1, ["A\n\nB "]),                          # 1 = 不拆，原樣返回
])
def test_split_bubbles(text, n, expected):
    assert builder.split_bubbles(text, n) == expected


def test_reply_split_into_bubbles():
    chat = FakeLLM(chunks=["Oh nice!", "\n", "\nWhat did ", "you eat?"])   # 空行被拆在兩個分塊裡
    sleep = Sleep()
    s = make_agent(config=bubbles_config(), chat=chat, sleep=sleep).start_session("Food")
    drain(s.ask("I had dinner with my family."))
    assert teacher_entries(s.view) == ["Oh nice!", "What did you eat?"]
    rows = [m for m in s.db.get_messages(s.id) if m["role"] == "agent"]
    assert [m["content"] for m in rows] == ["Oh nice!", "What did you eat?"]
    # 兩條之間停頓一次，時長在配置範圍內
    assert len(sleep.calls) == 1 and 1 <= sleep.calls[0] <= 3


def test_pause_happens_after_previous_bubble_is_complete():
    # 停頓時，上一條已經完整顯示，下一條還沒出現
    chat = FakeLLM(chunks=["Oh nice!\n\nWhat ", "did you eat?"])
    seen = []
    s = None

    def sleep(seconds):
        seen.append(teacher_entries(s.view))

    s = make_agent(config=bubbles_config(), chat=chat, sleep=sleep).start_session("Food")
    drain(s.ask("I had dinner."))
    assert seen == [["Oh nice!"]]


def test_no_pause_without_split():
    sleep = Sleep()
    s = make_agent(config=bubbles_config(), chat=FakeLLM(), sleep=sleep).start_session("Food")
    drain(s.ask("I had dinner."))
    assert sleep.calls == []


def test_bubbles_off_by_default():
    chat = FakeLLM(chunks=["Oh nice!\n\nWhat did you eat?"])
    s = make_agent(chat=chat).start_session("Food")
    drain(s.ask("I had dinner."))
    assert teacher_entries(s.view) == ["Oh nice!\n\nWhat did you eat?"]


def test_nudge_and_summary_are_not_split():
    chat = FakeLLM(chunks=["Para one.\n\nPara two."])
    clock = Clock()
    s = make_agent(config=bubbles_config(), chat=chat, clock=clock).start_session("Food")
    drain(s.greet())
    clock.t += 31
    drain(s.tick().chunks)
    assert s.view[-1] == {"role": "assistant", "content": "Para one.\n\nPara two.", "kind": "nudge", "done": True}
    clock.t += 25 * 60
    drain(s.tick().chunks)
    assert s.view[-1]["kind"] == "summary" and s.view[-1]["content"] == "Para one.\n\nPara two."


def test_corrector_sees_all_bubbles_of_last_turn():
    chat = FakeLLM(chunks=["Oh nice!\n\nWhat did you eat?"])
    corrector = FakeLLM()
    s = make_agent(config=bubbles_config(), chat=chat, corrector=corrector).start_session("Food")
    drain(s.ask("I had dinner."))
    drain(s.ask("Noodles."))
    prompt = corrector.calls[-1][2][0]["content"]
    assert "Oh nice!\n\nWhat did you eat?" in prompt


def test_bubbles_survive_restore():
    db = DB(":memory:")
    chat = FakeLLM(chunks=["Oh nice!\n\nWhat did you eat?"])
    s = make_agent(config=bubbles_config(), db=db, chat=chat).start_session("Food")
    drain(s.ask("I had dinner."))
    builder._sessions.clear()
    restored = builder.open_session(s.id, db=db)
    assert teacher_entries(restored.view) == ["Oh nice!", "What did you eat?"]


# ---------------- 結尾句式：不總是問句 ----------------

STYLE = "end with a short statement"


def endings_config(limit=2):
    cfg = base_config()
    cfg["greeting"] = "Hi! How are you?"
    cfg["max_questions_in_a_row"] = limit
    return cfg


def test_statement_note_after_questions_in_a_row():
    chat = FakeLLM(reply="Nice! What did you do?")
    s = make_agent(config=endings_config(), chat=chat).start_session("Food")
    drain(s.greet())                      # 第 1 輪問句（開場白）
    drain(s.ask("I'm fine."))
    assert STYLE not in chat.calls[-1][2][-1]["content"]
    drain(s.ask("I cooked."))             # 前面已經連續 2 輪問句
    assert STYLE in chat.calls[-1][2][-1]["content"]


def test_statement_ending_resets_count():
    chat = FakeLLM(reply="Nice! What did you do?")
    s = make_agent(config=endings_config(), chat=chat).start_session("Food")
    drain(s.greet())
    chat.reply = "Oh, I love cooking too."
    drain(s.ask("I'm fine."))
    chat.reply = "Nice! What did you do?"
    drain(s.ask("I cooked."))
    assert STYLE not in chat.calls[-1][2][-1]["content"]


def test_statement_note_skipped_when_question_is_required():
    err = {"category": "incomplete_sentence", "original": "Pizza.", "corrected": "I like pizza.",
           "explanation_en": "Use a full sentence.", "severity": "minor"}
    chat = FakeLLM(reply="Nice! What did you do?")
    s = make_agent(config=endings_config(limit=1), chat=chat,
                   corrector=FakeLLM(json_result={"errors": [err]})).start_session("Food")
    drain(s.greet())
    drain(s.ask("Pizza."))
    assert STYLE not in chat.calls[-1][2][-1]["content"]


def test_statement_note_off_by_default_and_per_phase():
    chat = FakeLLM(reply="Nice! What did you do?")
    s = make_agent(chat=chat).start_session("Food")
    for _ in range(3):
        drain(s.ask("hello"))
    assert STYLE not in chat.calls[-1][2][-1]["content"]

    cfg = endings_config(limit=1)
    cfg["phases"][0]["vary_endings"] = False
    chat = FakeLLM(reply="Nice! What did you do?")
    s = make_agent(config=cfg, chat=chat).start_session("Food")
    drain(s.greet())
    drain(s.ask("hello"))
    assert STYLE not in chat.calls[-1][2][-1]["content"]


# ---------------- 下課前道別 ----------------

def farewell_config():
    cfg = base_config()
    cfg["farewell_seconds"] = 10
    return cfg


def test_farewell_near_end_only_once():
    chat = FakeLLM(reply="Bye! See you next time.")
    clock = Clock()
    s = make_agent(config=farewell_config(), chat=chat, clock=clock).start_session("Travel")
    drain(s.ask("hello"))
    clock.t += 25 * 60 + 1
    drain(s.tick().chunks)                # 進入總結
    clock.t += 5 * 60 - 12                # 還剩 11 秒：不道別
    assert s.tick() is None
    clock.t += 2                          # 還剩 9 秒
    ev = s.tick()
    assert ev.kind == "farewell"
    drain(ev.chunks)
    assert s.view[-1]["kind"] == "farewell"
    assert "goodbye" in chat.calls[-1][2][-1]["content"]
    assert s.tick() is None               # 只說一次
    clock.t += 10
    assert s.tick().kind == "ended"


def test_no_reply_after_farewell():
    cfg = farewell_config()
    cfg["farewell"] = "Oh! It's time to say goodbye. See you next time!"
    chat, corrector = FakeLLM(), FakeLLM()
    clock = Clock()
    s = make_agent(config=cfg, chat=chat, corrector=corrector, clock=clock).start_session("Travel")
    clock.t += 30 * 60 - 5
    s.phase_index = len(s.phases) - 1     # 直接跳到最後階段，略過總結
    drain(s.tick().chunks)                # 道別
    assert drain(s.ask("See you.")) == ""
    assert drain(s.ask("Bye bye!", via="voice")) == ""
    assert chat.calls == [] and corrector.calls == []
    assert [m["content"] for m in s.view[-2:]] == ["See you.", "Bye bye!"]       # 學生的話照樣記錄
    assert [m["role"] for m in s.db.get_messages(s.id)] == ["agent", "user", "user"]


def test_no_nudge_after_farewell():
    cfg = farewell_config()
    cfg["farewell"] = "Bye!"
    cfg["phases"][-1]["nudge"] = True
    clock = Clock()
    s = make_agent(config=cfg, clock=clock).start_session("Travel")
    clock.t += 30 * 60 - 9
    s.phase_index = len(s.phases) - 1
    drain(s.tick().chunks)                # 道別
    s.nudge_count = 0
    s.silence_since -= 100                # 學生沉默很久
    assert s.tick() is None


def test_no_reply_after_farewell_survives_restore():
    db = DB(":memory:")
    cfg = farewell_config()
    cfg["farewell"] = "Bye!"
    clock = Clock()
    s = make_agent(config=cfg, db=db, clock=clock).start_session("Travel")
    clock.t += 30 * 60 - 5
    s.phase_index = len(s.phases) - 1
    drain(s.tick().chunks)
    builder._sessions.clear()             # 模擬重啟
    restored = builder.open_session(s.id, db=db, clock=clock)
    restored.agent._providers = {"chat": FakeLLM(), "corrector": FakeLLM()}
    assert drain(restored.ask("See you.")) == ""


def test_fixed_farewell_skips_llm():
    cfg = farewell_config()
    cfg["farewell"] = "時間到了，今天聊 {topic} 很開心，下次見！"
    chat = FakeLLM()
    clock = Clock()
    s = make_agent(config=cfg, chat=chat, clock=clock).start_session("Travel")
    clock.t += 30 * 60 - 5
    s.phase_index = len(s.phases) - 1     # 直接跳到最後階段，略過總結
    assert drain(s.tick().chunks) == "時間到了，今天聊 Travel 很開心，下次見！"
    assert chat.calls == []
    assert s.view[-1]["kind"] == "farewell"
    assert s.db.get_messages(s.id)[-1]["content"] == "時間到了，今天聊 Travel 很開心，下次見！"


def test_farewell_off_by_default():
    clock = Clock()
    s = make_agent(clock=clock).start_session("Travel")
    clock.t += 25 * 60 + 1
    drain(s.tick().chunks)
    clock.t += 5 * 60 - 5
    assert s.tick() is None


def test_farewell_flag_restored():
    db = DB(":memory:")
    clock = Clock()
    s = make_agent(config=farewell_config(), db=db, clock=clock).start_session("Travel")
    clock.t += 30 * 60 - 5
    s.phase_index = len(s.phases) - 1     # 直接跳到最後階段，略過總結
    drain(s.tick().chunks)
    builder._sessions.clear()
    assert builder.open_session(s.id, db=db, clock=clock).said_farewell


def test_each_bubble_saved_before_next_starts():
    # 語音課只念說完（done）的訊息：上一條在停頓前就入庫，不用等整個回覆生成完
    seen = []
    chat = FakeLLM(chunks=["Oh nice!\n\nWhat ", "did you eat?"])
    s = None

    def sleep(seconds):
        seen.append([m.get("done") for m in s.view if m["role"] == "assistant"])

    s = make_agent(config=bubbles_config(), chat=chat, sleep=sleep).start_session("Food")
    drain(s.ask("I had dinner."))
    assert seen == [[True]]   # 停頓時第一條已經入庫
    assert all(m["done"] for m in s.view if m["role"] == "assistant")
    assert [m["content"] for m in s.db.get_messages(s.id) if m["role"] == "agent"] == ["Oh nice!", "What did you eat?"]


# ---------------- 語音課 ----------------

VOICE = {
    "stt": {"provider": "gemini", "model": "gemini-3.5-transcribe-live"},
    "tts": {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "coral"},
    "chat_rules": "Speak at {level} level, no bold.",
}


def voice_config():
    cfg = base_config()
    cfg["voice"] = builder.parse_voice_config(copy.deepcopy(VOICE))
    cfg["voice"]["chat_rules"] = "VOICE RULES"
    return cfg


def test_voice_config_defaults_and_level():
    raw = json.loads((AGENTS_DIR / "teacher.json").read_text(encoding="utf-8"))
    raw["voice"] = copy.deepcopy(VOICE)
    cfg = builder.parse_agent_config(raw)
    assert cfg["voice"]["silence_ms"] == 800 and cfg["voice"]["tts_by_language"] == {}
    assert cfg["voice"]["chat_rules"] == "Speak at A2 level, no bold."


def test_no_voice_section_means_text_only():
    cfg = builder.read_agent_file("teacher", AGENTS_DIR)
    assert cfg["voice"] == {}
    agent = make_agent(config=cfg)
    assert not agent.has_voice
    with pytest.raises(builder.AgentConfigError, match="Text lesson"):
        agent.start_session("Food", mode="voice")


@pytest.mark.parametrize("mutate, message", [
    (lambda v: v.pop("tts"), "voice.tts"),
    (lambda v: v["stt"].update(provider="anthropic"), "voice.stt.provider"),
    (lambda v: v["stt"].update(provider="gemini_live"), "voice.stt.provider"),   # 只能用來合成
    (lambda v: v["tts"].update(provider="anthropic"), "voice.tts.provider"),
    (lambda v: v["tts"].pop("model"), "voice.tts"),
    (lambda v: v.update(tts_by_language={"zh-TW": "x"}), "tts_by_language"),
    (lambda v: v.update(silence_ms=0), "silence_ms"),
    (lambda v: v.update(chat_rules=None), "chat_rules"),
])
def test_invalid_voice_config(mutate, message):
    raw = json.loads((AGENTS_DIR / "teacher.json").read_text(encoding="utf-8"))
    raw["voice"] = copy.deepcopy(VOICE)
    mutate(raw["voice"])
    with pytest.raises(builder.AgentConfigError, match=message):
        builder.parse_agent_config(raw)


def test_voice_mode_saved_and_restored():
    db = DB(":memory:")
    s = make_agent(config=voice_config(), db=db).start_session("Food", mode="voice")
    drain(s.ask("I go to school.", via="voice"))
    assert db.get_session(s.id)["mode"] == "voice"
    assert db.get_messages(s.id)[0]["via"] == "voice"
    builder._sessions.clear()
    restored = builder.open_session(s.id, db=db)
    assert restored.mode == "voice"
    assert restored.view[0]["via"] == "voice"
    assert all(m["done"] for m in restored.view if m["role"] == "assistant")


def test_voice_rules_only_in_voice_lessons():
    chat = FakeLLM()
    agent = make_agent(config=voice_config(), chat=chat)
    drain(agent.start_session("Food", mode="voice").ask("hello", via="voice"))
    assert "VOICE RULES" in chat.calls[-1][1]
    drain(agent.start_session("Food", mode="text").ask("hello"))
    assert "VOICE RULES" not in chat.calls[-1][1]


def test_voice_input_ignores_spelling():
    spelling = {"category": "spelling", "original": "scool", "corrected": "school",
                "explanation_en": "", "severity": "minor"}
    corrector = FakeLLM(json_result={"errors": [spelling, tense_error()]})
    s = make_agent(config=voice_config(), corrector=corrector).start_session("Food", mode="voice")
    drain(s.ask("I go to Beijing yesterday.", via="voice"))
    assert "speech-to-text transcript" in corrector.calls[-1][1]
    assert [c["category"] for c in s.db.get_corrections(s.id)] == ["tense"]


def test_text_input_keeps_spelling():
    spelling = {"category": "spelling", "original": "scool", "corrected": "school",
                "explanation_en": "", "severity": "minor"}
    corrector = FakeLLM(json_result={"errors": [spelling]})
    s = make_agent(corrector=corrector).start_session("Food")
    drain(s.ask("I go to scool."))
    assert "speech-to-text" not in corrector.calls[-1][1]
    assert [c["category"] for c in s.db.get_corrections(s.id)] == ["spelling"]


def test_old_database_gets_mode_and_via_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, agent_name TEXT NOT NULL, agent_json TEXT NOT NULL, "
                 "started_at REAL NOT NULL, ended_at REAL, topic TEXT, status TEXT NOT NULL DEFAULT 'active', "
                 "deleted_at REAL)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, role TEXT NOT NULL, "
                 "content TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'normal', created_at REAL NOT NULL)")
    conn.execute("INSERT INTO sessions (agent_name, agent_json, started_at) VALUES ('a', '{}', 1)")
    conn.execute("INSERT INTO messages (session_id, role, content, created_at) VALUES (1, 'user', 'hi', 1)")
    conn.commit(); conn.close()
    db = DB(path)
    assert db.get_session(1)["mode"] == "text"
    assert db.get_messages(1)[0]["via"] == "text"


def test_recast_note_forbids_asking_again():
    cfg = base_config()
    cfg["correction"]["explain_limit_per_category"] = 1
    err = {"category": "incomplete_sentence", "original": "Pizza.", "corrected": "I like pizza.",
           "explanation_en": "Use a full sentence.", "severity": "minor"}
    chat = FakeLLM()
    s = make_agent(config=cfg, chat=chat, corrector=FakeLLM(json_result={"errors": [err]})).start_session("Food")
    drain(s.ask("Pizza."))
    assert "EXPLAIN" in chat.calls[-1][2][-1]["content"]
    drain(s.ask("Pizza."))
    note = chat.calls[-1][2][-1]["content"]
    assert "RECAST" in note and "do NOT ask" in note
