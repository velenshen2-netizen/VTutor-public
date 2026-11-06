"""SQLite 建表 + 讀寫。只被 builder.py 調用。

四張表：sessions（課堂）、messages（對話）、corrections（糾錯記錄）、summaries（總結報告）。
發音課另有：pron_lessons（每篇文章的進度）、pron_attempts（每次評分）、pron_words / pron_phonemes（錯誤本）、
pron_chat（結果頁的老師聊天）。
數據庫文件：data/vtutor.db，第一次啟動時自動創建。
"""

import json
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "vtutor.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name  TEXT NOT NULL,
    agent_json  TEXT NOT NULL,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    topic       TEXT,
    status      TEXT NOT NULL DEFAULT 'active',  -- active / ended
    deleted_at  REAL,                            -- 學生在界面上刪除的時間（軟刪除，數據保留）
    mode        TEXT NOT NULL DEFAULT 'text'     -- text（文字課）/ voice（語音課）
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    role        TEXT NOT NULL,                   -- agent / user
    content     TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'normal',  -- normal / nudge / intro / summary / report / farewell
    created_at  REAL NOT NULL,
    via         TEXT NOT NULL DEFAULT 'text'     -- 學生的話：text（打字）/ voice（語音轉寫）
);
CREATE TABLE IF NOT EXISTS corrections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL REFERENCES messages(id),
    session_id      INTEGER NOT NULL REFERENCES sessions(id),
    category        TEXT NOT NULL,
    original        TEXT NOT NULL,
    corrected       TEXT NOT NULL,
    explanation_en  TEXT NOT NULL,
    severity        TEXT NOT NULL,               -- minor / major
    mention         TEXT NOT NULL DEFAULT ''     -- explain / recast / ''（老師沒在對話中提）
);
CREATE TABLE IF NOT EXISTS summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL UNIQUE REFERENCES sessions(id),
    report_json TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_corrections_session ON corrections(session_id);

-- 發音課（pron.py）
CREATE TABLE IF NOT EXISTS pron_lessons (
    session_id   INTEGER NOT NULL REFERENCES sessions(id),
    round        INTEGER NOT NULL,                -- 同一節課裡第幾篇（挑戰更難的一篇會開新一輪）
    level        TEXT NOT NULL,                   -- 這篇文章的 CEFR 難度
    stage        INTEGER NOT NULL,                -- 發音練習重點：1 單音 / 2 字重音、連音 / 3 句子重音、語調
    passage_json TEXT NOT NULL,                   -- {title, sentences, focus_words, focus_phonemes}
    step         TEXT NOT NULL,                   -- demo / read1 / practice / read2 / choice / done
    state_json   TEXT NOT NULL,                   -- 各步驟的進度和小結
    updated_at   REAL NOT NULL,
    PRIMARY KEY (session_id, round)
);
CREATE TABLE IF NOT EXISTS pron_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions(id),
    round        INTEGER NOT NULL,
    step         TEXT NOT NULL,                   -- read1 / practice / read2
    sentence_idx INTEGER NOT NULL,                -- 第幾句；練單字時是這個字所在的句子
    kind         TEXT NOT NULL,                   -- sentence / word
    target       TEXT NOT NULL,                   -- 評估時的參考文字
    score        REAL NOT NULL,
    result_json  TEXT NOT NULL,
    counted      INTEGER NOT NULL DEFAULT 1,      -- ④ 裡是否計入次數（沒唸完的不計）
    feedback     TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pron_words (       -- 錯誤本：錯過的字
    word           TEXT PRIMARY KEY,
    err_count      INTEGER NOT NULL,
    last_wrong_at  TEXT NOT NULL,                 -- 最近一次出錯日期 YYYY-MM-DD
    correct_streak INTEGER NOT NULL DEFAULT 0     -- 之後的文章裡連續唸對幾次，到 3 就移除
);
CREATE TABLE IF NOT EXISTS pron_phonemes (    -- 錯誤本：錯過的音素
    phoneme        TEXT PRIMARY KEY,
    err_count      INTEGER NOT NULL,
    last_wrong_at  TEXT NOT NULL DEFAULT '',      -- 最近一次出錯日期 YYYY-MM-DD（舊數據為空）
    correct_streak INTEGER NOT NULL DEFAULT 0     -- 之後的文章裡連續幾次沒問題，到 3 就移除
);
CREATE TABLE IF NOT EXISTS pron_chat (        -- 發音課結果頁上學生和老師的文字聊天
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    role        TEXT NOT NULL,                    -- user / assistant
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pron_attempts_session ON pron_attempts(session_id, round);
"""


class DB:
    def __init__(self, path=DB_PATH):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Gradio 在多個線程裡調用，所以關掉同線程檢查，自己加鎖
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            # 舊數據庫缺少後來新增的列時補上
            self._add_column("sessions", "deleted_at", "REAL")
            self._add_column("sessions", "mode", "TEXT NOT NULL DEFAULT 'text'")
            self._add_column("messages", "via", "TEXT NOT NULL DEFAULT 'text'")
            self._add_column("pron_phonemes", "last_wrong_at", "TEXT NOT NULL DEFAULT ''")
            self._add_column("pron_phonemes", "correct_streak", "INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()

    def _add_column(self, table, column, definition):
        cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _execute(self, sql, params=()):
        with self.lock, self.conn:
            return self.conn.execute(sql, params)

    def _query(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ---- sessions ----

    def create_session(self, agent_name, agent_config, topic, started_at, mode="text"):
        cur = self._execute(
            "INSERT INTO sessions (agent_name, agent_json, started_at, topic, mode) VALUES (?, ?, ?, ?, ?)",
            (agent_name, json.dumps(agent_config, ensure_ascii=False), started_at, topic, mode),
        )
        return cur.lastrowid

    def end_session(self, session_id, ended_at):
        self._execute(
            "UPDATE sessions SET status = 'ended', ended_at = ? WHERE id = ?",
            (ended_at, session_id),
        )

    def hide_session(self, session_id, deleted_at):
        # 軟刪除：只打標記，歷史列表不再顯示，數據全部保留。
        self._execute("UPDATE sessions SET deleted_at = ? WHERE id = ?", (deleted_at, session_id))

    def get_session(self, session_id):
        rows = self._query("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return rows[0] if rows else None

    def list_sessions(self, limit=100):
        return self._query(
            "SELECT id, agent_name, topic, started_at, ended_at, status, mode "
            "FROM sessions WHERE deleted_at IS NULL ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )

    # ---- messages ----

    def add_message(self, session_id, role, content, kind, created_at, via="text"):
        cur = self._execute(
            "INSERT INTO messages (session_id, role, content, kind, created_at, via) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, role, content, kind, created_at, via),
        )
        return cur.lastrowid

    def get_messages(self, session_id):
        return self._query(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
        )

    # ---- corrections ----

    def add_corrections(self, session_id, message_id, corrections):
        with self.lock, self.conn:
            self.conn.executemany(
                "INSERT INTO corrections (message_id, session_id, category, original, corrected, "
                "explanation_en, severity, mention) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (message_id, session_id, c["category"], c["original"], c["corrected"],
                     c["explanation_en"], c["severity"], c.get("mention", ""))
                    for c in corrections
                ],
            )

    def get_corrections(self, session_id):
        return self._query(
            "SELECT * FROM corrections WHERE session_id = ? ORDER BY id", (session_id,)
        )

    # ---- summaries ----

    def save_summary(self, session_id, report, created_at):
        self._execute(
            "INSERT OR REPLACE INTO summaries (session_id, report_json, created_at) VALUES (?, ?, ?)",
            (session_id, json.dumps(report, ensure_ascii=False), created_at),
        )

    def get_summary(self, session_id):
        rows = self._query("SELECT report_json FROM summaries WHERE session_id = ?", (session_id,))
        return json.loads(rows[0]["report_json"]) if rows else None

    # ---- 長期記憶 ----

    def recent_memory(self, agent_name, n, exclude_session_id=None):
        # 最近 n 節課的話題 + 這幾節課裡各類錯誤的次數。
        sessions = self._query(
            "SELECT id, topic FROM sessions WHERE agent_name = ? AND id != ? "
            "ORDER BY started_at DESC LIMIT ?",
            (agent_name, exclude_session_id or -1, n),
        )
        if not sessions:
            return {"topics": [], "error_counts": {}}
        ids = [s["id"] for s in sessions]
        placeholders = ",".join("?" * len(ids))
        counts = self._query(
            f"SELECT category, COUNT(*) AS n FROM corrections WHERE session_id IN ({placeholders}) "
            "GROUP BY category ORDER BY n DESC",
            ids,
        )
        return {
            "topics": [s["topic"] for s in sessions if s["topic"]],
            "error_counts": {c["category"]: c["n"] for c in counts},
        }

    # ---- 發音課 ----

    def save_pron_lesson(self, session_id, round_, level, stage, passage, step, state, updated_at):
        # 新增或覆蓋一輪的進度（短文、步驟、各步驟的小結和 ④ 的進度都在 passage / state 裡）
        self._execute(
            "INSERT OR REPLACE INTO pron_lessons (session_id, round, level, stage, passage_json, step, "
            "state_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, round_, level, stage, json.dumps(passage, ensure_ascii=False), step,
             json.dumps(state, ensure_ascii=False), updated_at),
        )

    def get_pron_lessons(self, session_id=None):
        # 一節課的各輪（按輪次）；不給 session_id 時返回全部（算進步趨勢用，按時間順序）。
        where, params = ("WHERE session_id = ?", (session_id,)) if session_id is not None else ("", ())
        rows = self._query(f"SELECT * FROM pron_lessons {where} ORDER BY session_id, round", params)
        for r in rows:
            r["passage"] = json.loads(r.pop("passage_json"))
            r["state"] = json.loads(r.pop("state_json"))
        return rows

    def add_pron_attempt(self, session_id, round_, step, sentence_idx, kind, target, score, result,
                         counted, created_at):
        # 記一次評分，返回記錄 id（學生錄音以這個 id 存檔：data/pron/{課堂 id}/{id}.wav）
        cur = self._execute(
            "INSERT INTO pron_attempts (session_id, round, step, sentence_idx, kind, target, score, result_json, "
            "counted, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, round_, step, sentence_idx, kind, target, score,
             json.dumps(result, ensure_ascii=False), int(counted), created_at),
        )
        return cur.lastrowid

    @staticmethod
    def _attempt(row):
        # 評分記錄的 result_json 解析成 result
        row["result"] = json.loads(row.pop("result_json"))
        return row

    def get_pron_attempts(self, session_id, round_):
        # 一輪裡的全部評分（按時間），同一句重錄時後面的是最新的
        rows = self._query("SELECT * FROM pron_attempts WHERE session_id = ? AND round = ? ORDER BY id",
                           (session_id, round_))
        return [self._attempt(r) for r in rows]

    def get_pron_attempt(self, attempt_id):
        rows = self._query("SELECT * FROM pron_attempts WHERE id = ?", (attempt_id,))
        return self._attempt(rows[0]) if rows else None

    def set_pron_feedback(self, attempt_id, feedback):
        # ④ 老師對這次錄音的回饋（第一次請求時生成，之後直接讀）
        self._execute("UPDATE pron_attempts SET feedback = ? WHERE id = ?", (feedback, attempt_id))

    def get_pron_words(self):
        # 錯誤本：錯過的字
        return self._query("SELECT * FROM pron_words ORDER BY err_count DESC, last_wrong_at DESC")

    def get_pron_phonemes(self):
        # 錯誤本：錯過的音素
        return self._query("SELECT * FROM pron_phonemes ORDER BY err_count DESC")

    def add_pron_chat(self, session_id, role, content, created_at):
        # 結果頁聊天的一則訊息（role：user / assistant）
        self._execute("INSERT INTO pron_chat (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                      (session_id, role, content, created_at))

    def get_pron_chat(self, session_id):
        return self._query("SELECT role, content FROM pron_chat WHERE session_id = ? ORDER BY id", (session_id,))

    def update_pron_error_book(self, word_rows, removed_words, phoneme_rows=(), removed_phonemes=()):
        # 一次寫入錯誤本：*_rows 是要新增 / 更新的字和音素（整行），removed_* 是要移除的。
        with self.lock, self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO pron_words (word, err_count, last_wrong_at, correct_streak) "
                "VALUES (?, ?, ?, ?)",
                [(w["word"], w["err_count"], w["last_wrong_at"], w["correct_streak"]) for w in word_rows])
            self.conn.executemany("DELETE FROM pron_words WHERE word = ?", [(w,) for w in removed_words])
            self.conn.executemany(
                "INSERT OR REPLACE INTO pron_phonemes (phoneme, err_count, last_wrong_at, correct_streak) "
                "VALUES (?, ?, ?, ?)",
                [(p["phoneme"], p["err_count"], p["last_wrong_at"], p["correct_streak"]) for p in phoneme_rows])
            self.conn.executemany("DELETE FROM pron_phonemes WHERE phoneme = ?", [(p,) for p in removed_phonemes])
