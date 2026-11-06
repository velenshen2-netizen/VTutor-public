# pron.py 測試：用假 Azure、假 LLM，不調用任何 API。

import datetime
import io
import json
import wave
from pathlib import Path

import pytest

# test_builder 會把 src 加進 sys.path；clear_sessions 是自動啟用的 fixture
from test_builder import Clock, FakeLLM, base_config, clear_sessions, make_agent  # noqa: F401

import builder  # noqa: E402
import pron  # noqa: E402
from db import DB  # noqa: E402

PASSAGE = ["I thought about the very old tree.", "We walked to the river.", "Three birds sang there."]
DAY = 86400


def pron_config(**override):
    cfg = base_config()
    cfg["pronunciation"] = builder.parse_pron_config({"stage": "auto", **override})
    return cfg


def azure(reference, scores=None, prosody=None, status="Success", phonemes=None):
    # 假的 Azure 回傳：scores 是 {單字: 準確度}（-1 表示漏唸），沒寫的字 95 分。
    scores, phonemes = scores or {}, phonemes or {}
    words = []
    for t in pron.tokens(reference):
        n = pron.norm(t)
        acc = scores.get(n, 95)
        err = "Omission" if acc < 0 else ("Mispronunciation" if acc < 60 else "None")
        words.append({"Word": n, "AccuracyScore": max(acc, 0), "ErrorType": err,
                      "Phonemes": [{"Phoneme": p, "AccuracyScore": a} for p, a in phonemes.get(n, [])]})
    accs = [w["AccuracyScore"] for w in words]
    acc = sum(accs) / len(accs)
    completeness = 100 * sum(w["ErrorType"] != "Omission" for w in words) / len(words)
    nbest = {"Display": reference, "AccuracyScore": acc, "FluencyScore": 90, "CompletenessScore": completeness,
             "PronScore": acc, "Words": words}
    if prosody is not None:
        nbest["ProsodyScore"] = prosody
    return {"RecognitionStatus": status, "NBest": [nbest]}


class FakeSpeech:
    # 假 Azure：plan 是 {參考文字: 分數字典}，按參考文字給分；記錄每次評估的參考文字。
    def __init__(self, plan=None, prosody=90):
        self.plan, self.prosody, self.assessed, self.synth = plan or {}, prosody, [], []

    def assess(self, wav, reference):
        self.assessed.append(reference)
        scores = self.plan.get(reference, {})
        if callable(scores):
            scores = scores()
        return pron.parse_assessment(azure(reference, scores, self.prosody), reference)

    def synthesize(self, text, voice, rate=None):
        self.synth.append((text, voice, rate))
        return b"ID3fake"


WAV = b"RIFF" + b"\x00" * 100


def make_course(tmp_path, speech=None, chat=None, db=None, clock=None, config=None, topic="Nature"):
    chat = chat or FakeLLM(reply="說得不錯！", json_result={"title": "The Tree", "sentences": PASSAGE})
    agent = make_agent(config=config or pron_config(), chat=chat, db=db or DB(":memory:"), clock=clock)
    session = agent.start_session(topic, mode="pron")
    return pron.Course(session, speech=speech or FakeSpeech(), clock=clock or Clock(),
                       rec_dir=tmp_path / "rec", tts_dir=tmp_path / "tts")


def read_all(course, step):
    for i in range(len(PASSAGE)):
        course.assess(step, i, WAV)
    return course.advance("finish_reading")


# ---------------- 配置 ----------------

def test_pron_config_defaults_and_validation():
    cfg = builder.parse_pron_config({"voice": "en-GB-SoniaNeural"})
    assert cfg["pass_score"] == 80 and cfg["max_attempts"] == 3 and cfg["voice"] == "en-GB-SoniaNeural"
    assert builder.parse_pron_config({}) == {}
    for bad in ({"stage": 4}, {"focus_words": [5, 3]}, {"max_record_seconds": 90}, {"pass_score": 0}):
        with pytest.raises(builder.AgentConfigError, match="pronunciation"):
            builder.parse_pron_config(bad)


def test_pron_mode_needs_config():
    agent = make_agent()
    with pytest.raises(builder.AgentConfigError, match="pronunciation"):
        agent.start_session("x", mode="pron")


def test_pron_session_has_no_timer():
    agent = make_agent(config=pron_config())
    session = agent.start_session("x", mode="pron")
    assert session.tick(now=session.started_at + 99999) is None and not session.ended


# ---------------- Azure 結果解析 ----------------

def test_parse_assessment_colors_and_alignment():
    ref = "I thought, very well."
    data = azure(ref, {"thought": 45, "very": 70}, prosody=77)
    data["NBest"][0]["Words"].insert(1, {"Word": "um", "AccuracyScore": 0, "ErrorType": "Insertion"})
    r = pron.parse_assessment(data, ref)
    assert [w["text"] for w in r["words"]] == ["I", "thought,", "very", "well."]
    assert [w["color"] for w in r["words"]] == ["good", "bad", "fair", "good"]
    assert r["prosody"] == 77


def test_parse_assessment_sdk_style_and_omission():
    ref = "We walked home"
    data = {"RecognitionStatus": "Success", "NBest": [{"PronunciationAssessment": {
        "AccuracyScore": 50, "FluencyScore": 60, "CompletenessScore": 66, "PronScore": 55},
        "Words": [{"Word": "we", "PronunciationAssessment": {"AccuracyScore": 90, "ErrorType": "None"}},
                  {"Word": "walked", "PronunciationAssessment": {"AccuracyScore": 0, "ErrorType": "Omission"}},
                  {"Word": "home", "PronunciationAssessment": {"AccuracyScore": 85, "ErrorType": "None"},
                   "Feedback": {"Prosody": {"Break": {"ErrorTypes": ["UnexpectedBreak"]},
                                            "Intonation": {"ErrorTypes": ["None"]}}}}]}]}
    r = pron.parse_assessment(data, ref)
    assert r["score"] == 55 and r["prosody"] is None
    assert r["words"][1]["error"] == "Omission" and r["words"][1]["color"] == "bad"
    assert r["words"][2]["prosody_errors"] == ["UnexpectedBreak"]


def test_parse_real_azure_response():
    # fixtures/azure_pa.json：Azure 真實回傳（錄音唸的是 "I thought about the very old tree."，
    # 參考文字故意寫成 fought / berry / cold）
    data = json.loads((Path(__file__).parent / "fixtures" / "azure_pa.json").read_text(encoding="utf-8"))
    r = pron.parse_assessment(data, "I fought about the berry cold tree.")
    assert r["score"] == 79.3 and r["prosody"] == 87.6 and r["completeness"] == 71
    colors = {pron.norm(w["text"]): w["color"] for w in r["words"]}
    assert colors["berry"] == colors["cold"] == "bad"
    # 整字 94 分，但 /f/ 只有 18 分（唸成了 th）：要標紅
    fought = r["words"][1]
    assert fought["accuracy"] == 94 and fought["phonemes"][0] == {"phoneme": "f", "accuracy": 18}
    assert colors["fought"] == "bad"
    assert colors["about"] == colors["tree"] == "good"
    # 每個字都帶的 Monotone 不算字的錯誤；整句韻律 87.6 不算語調太平
    assert all(not w["prosody_errors"] for w in r["words"]) and r["monotone"] is False


def test_weak_phoneme_makes_word_yellow():
    assert pron.word_color(95, "None", 50) == "fair"
    assert pron.word_color(95, "None", 20) == "bad"
    assert pron.word_color(95, "None") == "good"


def test_parse_assessment_no_speech():
    r = pron.parse_assessment({"RecognitionStatus": "InitialSilenceTimeout"}, "Hello there")
    assert r["score"] == 0 and r["completeness"] == 0 and all(w["error"] == "Omission" for w in r["words"])


def test_summarize_weights_by_words():
    a = {"score": 100, "accuracy": 100, "fluency": 100, "completeness": 100, "prosody": None, "words": [1] * 3}
    b = {"score": 60, "accuracy": 60, "fluency": 60, "completeness": 60, "prosody": None, "words": [1]}
    s = pron.summarize([a, b])
    assert s["score"] == 90 and s["prosody"] is None


# ---------------- 錯誤本 ----------------

def results_for(scores_by_sentence):
    return [pron.parse_assessment(azure(s, sc), s) for s, sc in zip(PASSAGE, scores_by_sentence)]


def test_error_book_counts_and_removal_after_three_correct():
    db = DB(":memory:")
    pron.update_error_book(db, results_for([{"thought": 40, "the": 10}, {}, {}]), "2026-10-01")
    pron.update_error_book(db, results_for([{"thought": 30}, {}, {}]), "2026-10-05")
    words = {w["word"]: w for w in db.get_pron_words()}
    assert set(words) == {"thought"}   # 功能詞 the 不記
    assert words["thought"]["err_count"] == 2 and words["thought"]["last_wrong_at"] == "2026-10-05"
    for day in ("2026-10-06", "2026-10-07"):
        pron.update_error_book(db, results_for([{}, {}, {}]), day)
    assert db.get_pron_words()[0]["correct_streak"] == 2
    pron.update_error_book(db, results_for([{"thought": 50}, {}, {}]), "2026-10-08")   # 又錯：清零
    assert db.get_pron_words()[0]["correct_streak"] == 0
    for day in ("2026-10-09", "2026-10-10", "2026-10-11"):
        pron.update_error_book(db, results_for([{}, {}, {}]), day)
    assert db.get_pron_words() == []


def test_error_book_counts_phonemes():
    db = DB(":memory:")
    res = [pron.parse_assessment(azure(PASSAGE[0], {}, phonemes={"thought": [("θ", 30), ("ɔ", 90)]}), PASSAGE[0])]
    pron.update_error_book(db, res, "2026-10-01")
    pron.update_error_book(db, res, "2026-10-02")
    assert db.get_pron_phonemes() == [{"phoneme": "θ", "err_count": 2, "last_wrong_at": "2026-10-02",
                                       "correct_streak": 0}]


def phoneme_results(scores):
    # 一句話裡 /t/ 出現在多個字：scores 是各個字裡 /t/ 的分數
    words = {f"w{i}": [("t", s)] for i, s in enumerate(scores)}
    text = " ".join(words) + "."
    return [pron.parse_assessment(azure(text, {}, phonemes=words), text)]


def test_phoneme_removed_after_three_clean_readings():
    db = DB(":memory:")
    pron.update_error_book(db, phoneme_results([30, 40, 90, 90]), "2026-10-01")    # 錯 2 次：記下
    assert db.get_pron_phonemes()[0]["err_count"] == 2
    pron.update_error_book(db, phoneme_results([90] * 9 + [40]), "2026-10-02")     # 10 次裡錯 1 次：偶爾失誤，算沒問題
    pron.update_error_book(db, phoneme_results([90, 85]), "2026-10-03")
    assert db.get_pron_phonemes()[0]["correct_streak"] == 2
    pron.update_error_book(db, phoneme_results([50, 90]), "2026-10-04")            # 2 次裡錯 1 次（50%）：又錯，清零
    assert db.get_pron_phonemes()[0] == {"phoneme": "t", "err_count": 3, "last_wrong_at": "2026-10-04",
                                         "correct_streak": 0}
    for day in ("2026-10-05", "2026-10-06", "2026-10-07"):
        pron.update_error_book(db, phoneme_results([90, 95]), day)
    assert db.get_pron_phonemes() == []


def test_single_slip_does_not_add_new_phoneme():
    db = DB(":memory:")
    pron.update_error_book(db, phoneme_results([90] * 9 + [40]), "2026-10-01")
    assert db.get_pron_phonemes() == []


def test_pick_focus_prefers_frequent_and_recent():
    words = [{"word": "old", "err_count": 4, "last_wrong_at": "2026-08-01"},
             {"word": "recent", "err_count": 2, "last_wrong_at": "2026-10-19"},
             {"word": "once", "err_count": 1, "last_wrong_at": "2026-10-19"}]
    phonemes = [{"phoneme": "v", "err_count": 1, "last_wrong_at": "2026-10-20"},
                {"phoneme": "θ", "err_count": 5, "last_wrong_at": "2026-08-01"},   # 錯很多次但很久沒錯了
                {"phoneme": "r", "err_count": 3, "last_wrong_at": "2026-10-18"},
                {"phoneme": "æ", "err_count": 9, "last_wrong_at": ""}]            # 舊數據沒有日期：當作 30 天前
    picked, top = pron.pick_focus(words, phonemes, "2026-10-20", 2)
    assert picked == ["recent", "once"] and top == ["r", "æ"]


def test_decide_stage():
    assert pron.decide_stage([]) == 1
    assert pron.decide_stage([{"accuracy": 70, "prosody": 90}]) == 1
    assert pron.decide_stage([{"accuracy": 85, "prosody": 70}]) == 2
    assert pron.decide_stage([{"accuracy": 85, "prosody": None}]) == 2
    assert pron.decide_stage([{"accuracy": 60, "prosody": 60}] + [{"accuracy": 90, "prosody": 85}] * 3) == 3
    assert pron.decide_stage([{"accuracy": 90, "prosody": 90}], fixed=1) == 1


def test_level_up_caps_at_c2():
    assert pron.base_level("B1-B2") == "B1" and pron.level_up("A2") == "B1" and pron.level_up("C2") == "C2"


# ---------------- ④ 練習計劃 ----------------

def test_plan_practice_lowest_sentences_in_text_order():
    cfg = pron_config(practice_sentences=2)["pronunciation"]
    res = results_for([{"thought": 10, "very": 20, "old": 30}, {"walked": 10, "river": 20}, {"three": 75}])
    plan = pron.plan_practice(res, cfg)
    assert plan["mode"] == "sentences"
    assert [it["sentence"] for it in plan["items"]] == [0, 1]
    assert plan["items"][1]["words"] == ["walked", "river"]


def test_plan_practice_words_only_when_sentences_pass():
    cfg = pron_config()["pronunciation"]
    res = results_for([{"thought": 70}, {}, {"three": 65}])
    assert all(r["score"] >= 80 for r in res)
    plan = pron.plan_practice(res, cfg)
    assert plan["mode"] == "words"
    assert [it["words"] for it in plan["items"]] == [["Three"], ["thought"]]
    assert not any(it["whole"] for it in plan["items"])


def test_plan_practice_none_when_all_good():
    assert pron.plan_practice(results_for([{}, {}, {}]), pron_config()["pronunciation"])["mode"] == "none"


def test_practice_cursor_rules():
    cfg = pron_config()["pronunciation"]
    cur = pron.new_cursor({"mode": "sentences", "items": [{"sentence": 0, "words": ["thought"], "whole": True},
                                                           {"sentence": 2, "words": [], "whole": True}]})
    assert pron.current_target(cur, PASSAGE) == {"kind": "word", "text": "thought", "sentence": 0}
    said = [{"error": "Mispronunciation"}]
    bad = {"score": 40, "completeness": 0, "words": said}   # Azure：唸錯的單字完整度是 0，但確實唸了
    assert not pron.record_try(cur, {"score": 0, "completeness": 0, "words": [{"error": "Omission"}]}, cfg)   # 沒聽到不計
    assert pron.record_try(cur, bad, cfg) and pron.record_try(cur, bad, cfg) and pron.record_try(cur, bad, cfg)
    assert cur["finished"] and not cur["passed"] and cur["tries"] == 3
    assert not pron.record_try(cur, bad, cfg)   # 結束後不再計
    assert pron.next_target(cur) and pron.current_target(cur, PASSAGE)["kind"] == "sentence"
    assert pron.record_try(cur, {"score": 85, "completeness": 100, "words": said}, cfg) and cur["passed"] and cur["finished"]
    assert pron.next_target(cur) and pron.current_target(cur, PASSAGE)["text"] == PASSAGE[2]
    assert not pron.next_target(cur)


def test_uncounted_tries_cannot_block_forever():
    cfg = pron_config()["pronunciation"]
    cur = pron.new_cursor({"mode": "words", "items": [{"sentence": 0, "words": ["sits"], "whole": False}]})
    silent = {"score": 0, "completeness": 0, "words": [{"error": "Omission"}]}
    assert not pron.record_try(cur, silent, cfg) and not pron.record_try(cur, silent, cfg)
    assert pron.record_try(cur, silent, cfg) and cur["tries"] == 1   # 連續兩次不計之後一定計入
    assert not pron.record_try(cur, silent, cfg)                     # 計入後重新算
    half = {"score": 50, "completeness": 50, "words": [{"error": "None"}, {"error": "Omission"}]}
    assert pron.record_try(cur, half, cfg)                           # 唸出一半就計


def test_word_practice_scores_by_accuracy(tmp_path):
    # 真實 Azure：單字唸錯時完整度 0、總分被壓到 20 左右；應該計入次數，分數用準確度
    def mispronounced():
        r = azure("thought", {"thought": 46})
        r["NBest"][0].update(PronScore=20.7, FluencyScore=0, CompletenessScore=0)
        return r

    class Speech(FakeSpeech):
        def assess(self, wav, reference):
            if reference == "thought":
                return pron.parse_assessment(mispronounced(), reference)
            return super().assess(wav, reference)

    c = make_course(tmp_path, Speech({PASSAGE[0]: {"thought": 10, "old": 20, "tree": 30}}))
    c.start()
    c.advance("start_reading")
    st = read_all(c, "read1")
    assert st["practice"]["target"]["text"] == "thought"
    out = c.assess("practice", -1, WAV)
    assert out["counted"] and out["result"]["score"] == 46 and out["practice"]["tries"] == 1


# ---------------- 短文生成 ----------------

def test_passage_prompt_has_focus_level_and_stage():
    cfg = pron_config()["pronunciation"]
    system, msgs = pron.passage_messages(cfg, "A2", 2, "Food", ["thought", "very"], ["θ"])
    assert "A2" in system and "150 to 172 words" in system and "read in the past tense" in system
    user = msgs[0]["content"]
    assert "thought, very" in user and "/θ/" in user and "linked phrases" in user
    assert "Chinese speakers" in user   # 少於 3 個字時加入冷啟動的難點


def test_generate_passage_retries_missing_words():
    calls = []

    class LLM(FakeLLM):
        def json(self, system, messages, schema):
            calls.append(messages[0]["content"])
            words = PASSAGE if len(calls) == 1 else PASSAGE + ["My vehicle is red."]
            return {"title": "T", "sentences": words}
    p = pron.generate_passage(LLM(), pron_config(passage_words=15)["pronunciation"], "B1", 1, "x", ["vehicle"], [])
    assert len(calls) == 2 and "left out these required words, include them: vehicle" in calls[1]
    assert p["sentences"][-1] == "My vehicle is red."


def test_generate_passage_retries_when_too_short():
    calls = []

    class LLM(FakeLLM):
        def json(self, system, messages, schema):
            calls.append(messages[0]["content"])
            return {"title": "T", "sentences": PASSAGE * (1 if len(calls) == 1 else 7)}
    p = pron.generate_passage(LLM(), pron_config(passage_words=100)["pronunciation"], "A2", 1, "x", [], [])
    assert len(calls) == 2 and "It has only 16 words; write 100 to 115 words" in calls[1]
    assert len(p["sentences"]) == 21


def test_generate_passage_failure_is_clear():
    with pytest.raises(pron.PronError, match="passage"):
        pron.generate_passage(FakeLLM(fail_json=True), pron_config()["pronunciation"], "B1", 1, "x", [], [])


# ---------------- 整節課 ----------------

def test_full_lesson_with_sentence_practice(tmp_path):
    attempts = iter([{"thought": 40}, {}])   # 「thought」第一次錯，第二次對
    speech = FakeSpeech({PASSAGE[0]: {"thought": 10, "very": 65, "old": 20, "tree": 30}, "thought": lambda: next(attempts)})
    c = make_course(tmp_path, speech)
    st = c.start()
    assert st["step"] == "demo" and st["level"] == "A2" and st["stage"] == 1 and len(st["sentences"]) == 3
    assert c.advance("start_reading")["step"] == "read1"

    st = read_all(c, "read1")
    assert st["step"] == "practice" and st["summary1"]["score"] < 80
    assert st["feedback"]["read1"] == "說得不錯！"
    assert {w["word"] for w in c.db.get_pron_words()} == {"thought", "old", "tree"}   # very 是黃色，不記
    p = st["practice"]
    assert p["mode"] == "sentences" and p["target"] == {"kind": "word", "text": "thought", "sentence": 0}

    out = c.assess("practice", -1, WAV)
    assert out["counted"] and not out["practice"]["passed"] and out["practice"]["tries"] == 1
    assert c.feedback(out["attempt"]) == "說得不錯！"
    out = c.assess("practice", -1, WAV)
    assert out["practice"]["passed"] and out["practice"]["finished"]
    assert (tmp_path / "rec" / str(c.session.id) / f"{out['attempt']}.wav").exists()

    st = c.advance("next")   # 第二個問題字 old
    assert st["practice"]["target"]["text"] == "old"
    st = c.advance("next")   # 第三個問題字 tree
    assert st["practice"]["target"]["text"] == "tree"
    st = c.advance("next")   # 整句
    assert st["practice"]["target"]["kind"] == "sentence"
    st = c.advance("next")   # 只有一句要練，練完進 ⑤
    assert st["step"] == "read2"

    speech.plan[PASSAGE[0]] = {}
    st = read_all(c, "read2")
    assert st["step"] == "done" and st["ended"] and st["summary2"]["score"] > st["summary1"]["score"]
    assert pron.take_ended_notice(c.session.id) and not pron.take_ended_notice(c.session.id)


def test_all_good_offers_choice_and_harder_round(tmp_path):
    c = make_course(tmp_path)
    c.start()
    c.advance("start_reading")
    st = read_all(c, "read1")
    assert st["step"] == "choice" and st["feedback"]["choice"]
    st = c.advance("harder")
    assert st["round"] == 2 and st["level"] == "B1" and st["step"] == "demo" and not st["ended"]
    assert st["read1"] == [None, None, None]   # 新的一輪從頭開始
    c.advance("start_reading")
    read_all(c, "read1")
    st = c.advance("end")
    assert st["ended"] and st["step"] == "done"


def test_words_only_practice_then_read2(tmp_path):
    speech = FakeSpeech({PASSAGE[2]: {"three": 70}})
    c = make_course(tmp_path, speech)
    c.start()
    c.advance("start_reading")
    st = read_all(c, "read1")
    assert st["practice"]["mode"] == "words" and st["practice"]["target"]["text"] == "Three"
    assert c.advance("next")["step"] == "read2"


def test_rerecord_uses_latest_and_unfinished_reading_rejected(tmp_path):
    speech = FakeSpeech({PASSAGE[1]: {"walked": 10}})
    c = make_course(tmp_path, speech)
    c.start()
    c.advance("start_reading")
    c.assess("read1", 0, WAV)
    c.assess("read1", 1, WAV)
    with pytest.raises(pron.PronError, match="not recorded"):
        c.advance("finish_reading")
    speech.plan[PASSAGE[1]] = {}
    c.assess("read1", 1, WAV)   # 重錄
    c.assess("read1", 2, WAV)
    assert c.state()["read1"][1]["score"] > 90


def test_wrong_step_is_rejected(tmp_path):
    c = make_course(tmp_path)
    c.start()
    with pytest.raises(pron.PronError):
        c.assess("read1", 0, WAV)   # 還在示範
    with pytest.raises(pron.PronError):
        c.advance("next")
    with pytest.raises(pron.PronError, match="format"):
        c.assess("demo", 0, b"nope")


def test_state_survives_restart(tmp_path):
    db = DB(":memory:")
    c = make_course(tmp_path, db=db)
    c.start()
    c.advance("start_reading")
    c.assess("read1", 0, WAV)
    builder._sessions.clear()   # 模擬重啟：從數據庫恢復
    session = builder.open_session(c.session.id, db=db)
    st = pron.Course(session, speech=FakeSpeech(), rec_dir=tmp_path).state()
    assert st["step"] == "read1" and st["read1"][0] is not None and st["read1"][1] is None


def test_stage_follows_history(tmp_path):
    db = DB(":memory:")
    for _ in range(2):
        c = make_course(tmp_path, db=db)
        c.start()
        c.advance("start_reading")
        read_all(c, "read1")
        c.advance("end")
    c = make_course(tmp_path, db=db)
    st = c.start()
    assert st["stage"] == 3 and len(st["trend"]) == 2   # 前兩節準確度和韻律都 >= 80


def test_today_and_trend_dates(tmp_path):
    clock = Clock(datetime.datetime(2026, 10, 20, 12).timestamp())
    c = make_course(tmp_path, FakeSpeech({PASSAGE[0]: {"thought": 20}}), clock=clock)
    c.start()
    c.advance("start_reading")
    read_all(c, "read1")
    assert c.db.get_pron_words()[0]["last_wrong_at"] == "2026-10-20"


def test_tts_is_cached(tmp_path):
    speech = FakeSpeech()
    c = make_course(tmp_path, speech)
    a = c.tts("Hello there", slow=True)
    b = c.tts("Hello there", slow=True)
    c.tts("Hello there")
    assert a == b and a.read_bytes() == b"ID3fake"
    assert speech.synth == [("Hello there", "en-US-AvaNeural", "-30%"), ("Hello there", "en-US-AvaNeural", None)]


def wav_of(samples, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\1\0" * samples)
    return buf.getvalue()


def test_chat_with_teacher_after_reading(tmp_path):
    chat = FakeLLM(reply="韻律分低是因為語調太平。", json_result={"title": "The Tree", "sentences": PASSAGE})
    c = make_course(tmp_path, FakeSpeech({PASSAGE[0]: {"thought": 10, "old": 20, "tree": 30}}), chat=chat)
    c.start()
    with pytest.raises(pron.PronError, match="after reading"):
        c.chat("我的分數怎麼樣？")
    c.advance("start_reading")
    read_all(c, "read1")
    out = c.chat("為什麼我的 thought 是紅色？")
    assert out["reply"] == "韻律分低是因為語調太平。"
    assert out["chat"] == [{"role": "user", "content": "為什麼我的 thought 是紅色？"},
                           {"role": "assistant", "content": "韻律分低是因為語調太平。"}]
    _, system, messages = chat.calls[-1]
    assert "## 課堂資料" in system and '"weak_words_first"' in system and "thought" in system
    assert messages[-1] == {"role": "user", "content": "為什麼我的 thought 是紅色？"}
    c.chat("那要怎麼練？")
    assert len(chat.calls[-1][2]) == 3          # 帶上之前的聊天
    assert len(c.state()["chat"]) == 4
    with pytest.raises(pron.PronError, match="empty"):
        c.chat("  ")


def test_reading_audio_joins_latest_takes(tmp_path):
    c = make_course(tmp_path)
    c.start()
    c.advance("start_reading")
    assert c.state()["audio"]["read1"] is False
    c.assess("read1", 0, wav_of(1000))
    c.assess("read1", 1, wav_of(500))
    c.assess("read1", 1, wav_of(2000))   # 重錄：以這次為準
    c.assess("read1", 2, wav_of(3000))
    assert c.state()["audio"] == {"read1": True, "read2": False}
    assert c.state()["audio_durations"] == {"read1": [0.062, 0.125, 0.188], "read2": [None, None, None]}
    with wave.open(io.BytesIO(c.reading_audio("read1")), "rb") as w:
        assert w.getnframes() == 1000 + 2000 + 3000 + 2 * int(16000 * 0.4)
    with wave.open(io.BytesIO(c.reading_audio("read1", 1)), "rb") as w:
        assert w.getnframes() == 2000
    with pytest.raises(pron.PronError):
        c.reading_audio("read2")
