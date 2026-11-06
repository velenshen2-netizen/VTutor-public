"""聊天室：Gradio 頁面 + 語音課的 WebSocket。只負責顯示和收發消息，老師的邏輯都在 builder.py，
語音收發在 voice.py，數據庫只經 builder 訪問。

啟動：.venv/Scripts/python src/app.py
    FastAPI 為主程序：/ 是 Gradio 頁面，/voice/{課堂 id} 是語音課的 WebSocket（瀏覽器端見 static/voice.js），
    /voice/{課堂 id}/recording 是課後播放的課堂錄音，
    /pron/{課堂 id}/... 是發音課的 HTTP 接口（流程在 pron.py，瀏覽器端見 static/pron.js）。

頁面結構：
    左側欄    + New Lesson、Lesson（Voice lesson 語音課 / Text lesson 文字課 / Pronunciation lesson 發音課）、
              Teacher、Topic、Past Lessons（歷史課堂）
    頂部      狀態條（老師、階段、剩餘時間、進度）+ End Lesson / Delete 按鈕
    中間      文字課：對話；語音課上課中：Listening / Speaking；語音課結束後：對話記錄；發音課：發音課面板
    底部      文字課：輸入框 + Send；語音課結束後：課堂錄音卡片（波紋、快退快進、調速）
"""

import asyncio
import datetime
import html
import json
import logging
import threading
import time
import wave
import webbrowser
from pathlib import Path

import gradio as gr
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, Response
from starlette.routing import Mount

import builder
import pron
import voice

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

HOST, PORT = "127.0.0.1", 7860
TICK_SECONDS = 1                  # 計時器間隔：刷新倒計時，並驅動 session.tick()
RECOMMEND = "Teacher's choice"    # 話題下拉框的「老師推薦」選項
LESSON_MODES = [("Voice lesson", "voice"), ("Text lesson", "text"),
                ("Pronunciation lesson", "pron")]   # 課堂類型下拉框：(顯示文字, mode)
VOICE_END_WAIT = 300              # 語音課下課時，最多等老師最後的話（總結、道別）播完多少秒
STATIC = Path(__file__).resolve().parent / "static"
VOICE_JS = (STATIC / "voice.js").read_text(encoding="utf-8")
PRON_JS = (STATIC / "pron.js").read_text(encoding="utf-8")
PRON_CSS = (STATIC / "pron.css").read_text(encoding="utf-8")
VOICE_PANEL = ('<div class="vt-voice"><div id="vt-voice-state" data-state="listening">Listening</div>'
               '<div id="vt-voice-note"></div></div>')
PRON_PANEL = '<div id="vt-pron" class="vt-pron"><div class="vt-pron-loading">Your teacher is writing a passage…</div></div>'

CSS = """
.vt-status { padding: 10px 14px; border-radius: 10px; background: var(--block-background-fill);
             border: 1px solid var(--border-color-primary); }
.vt-status .row { display: flex; gap: 14px; flex-wrap: wrap; align-items: baseline; }
.vt-status .phase { font-weight: 600; }
.vt-status .muted { color: var(--body-text-color-subdued); }
.vt-bar { height: 6px; border-radius: 3px; background: var(--border-color-primary); margin-top: 8px; overflow: hidden; }
.vt-bar > div { height: 100%; background: var(--color-accent); transition: width .5s; }
/* 頂部：進度條和 End Lesson 按鈕並排、垂直居中 */
#vt-top { align-items: center; }
#vt-top > button { align-self: center; flex: 0 0 auto; height: auto; }
/* 歷史課堂列表：每項佔滿一行、寬度一致，話題太長時用省略號 */
#vt-history .wrap { flex-direction: column; flex-wrap: nowrap; }
#vt-history .wrap label { width: 100%; min-width: 0; }
#vt-history .wrap label span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 進行中的課：顯示文字以 LIVE_MARK（零寬空格）開頭，Gradio 把顯示文字寫在 label 的 data-testid 裡，
   據此在前面畫綠點（和發音課結果頁的顏色說明圓點一致，顏色變量在 pron.css）。升級 Gradio 後綠點消失先查這裡 */
#vt-history label[data-testid^="\\200b"] span::before { content: ""; display: inline-block; width: 8px; height: 8px;
                                                        border-radius: 50%; background: var(--vt-good); margin-right: 7px;
                                                        vertical-align: 1px; }
/* 側邊欄：頭部（標題、按鈕、老師、話題）固定，只有歷史課堂列表滾動。
   層級：.sidebar-content > .column > .form > #vt-history > .wrap */
#vt-sidebar .sidebar-content { flex: 1 1 auto; min-height: 0; overflow: hidden !important;
                               display: flex; flex-direction: column; }
#vt-sidebar .sidebar-content > .column { flex: 1 1 auto; min-height: 0; flex-wrap: nowrap; }
#vt-sidebar .sidebar-content > .column > * { flex-shrink: 0; }
#vt-sidebar .sidebar-content > .column > .form { flex: 1 1 auto !important; min-height: 0;
                                                 display: flex; flex-direction: column; flex-wrap: nowrap; }
#vt-sidebar .form > .block { flex-shrink: 0; }
#vt-sidebar #vt-history { flex: 1 1 auto !important; min-height: 0; display: flex; flex-direction: column; }
#vt-history > .wrap:not(.default) { flex: 1 1 auto; min-height: 0; overflow-y: auto; }
/* 語音課：上課中只顯示 Listening / Speaking */
.vt-voice { min-height: 300px; display: flex; flex-direction: column; align-items: center; justify-content: center;
            gap: 14px; border: 1px solid var(--border-color-primary); border-radius: 12px;
            background: var(--block-background-fill); }
#vt-voice-state { font-size: 48px; font-weight: 600; letter-spacing: .04em; color: var(--body-text-color-subdued); }
#vt-voice-state[data-state="speaking"] { color: var(--color-accent); }
#vt-voice-note { font-size: 14px; color: var(--body-text-color-subdued); text-align: center; padding: 0 16px; }
/* 語音課總結階段：Listening / Speaking 下方的總結卡片 */
#vt-voice-report { padding: 12px 18px; border: 1px solid var(--border-color-primary); border-radius: 12px;
                   background: var(--block-background-fill); max-height: 420px; overflow-y: auto; }
/* 給 voice.js / pron.js 傳課堂 id 的輸入框，不顯示 */
#vt-voice-ctl, #vt-pron-ctl { display: none !important; }
""" + PRON_CSS


# ---------------- 渲染 ----------------

def render_report(report: dict) -> str:
    # 把總結報告 JSON 渲染成對話裡的 Markdown 卡片。
    lines = [f"### 📋 本節課報告 · {report.get('topic') or ''}", ""]
    stats = report.get("stats") or []
    if stats:
        lines.append("**錯誤統計**：" + " · ".join(f"{s['label']} ×{s['count']}" for s in stats))
    else:
        lines.append("**錯誤統計**：本節沒有發現錯誤，很棒！")
    if report.get("key_errors"):
        lines += ["", "**重點錯誤**"]
        for i, e in enumerate(report["key_errors"], 1):
            lines.append(f"{i}. ~~{e['original']}~~ → **{e['corrected']}**  ")
            lines.append(f"   {e['explanation_zh']}  ")
            lines.append(f"   例句：*{e['example']}*")
    if report.get("good_expressions"):
        lines += ["", "**用得好的表達**：" + "；".join(report["good_expressions"])]
    if report.get("new_words"):
        lines += ["", "**生詞**：" + " · ".join(f"{w['word']}（{w['meaning_zh']}）" for w in report["new_words"])]
    if report.get("overall_zh"):
        lines += ["", f"**老師點評**：{report['overall_zh']}"]
    return "\n".join(lines)


def to_chat(session) -> list[dict]:
    """課堂內容 → Chatbot 的消息列表。每次都從 session.view 重新生成，
    這樣 ask() 和 tick() 同時刷新頁面時看到的是同一份數據。"""
    out = []
    for m in session.view:
        if m["kind"] == "report":
            content = render_report(json.loads(m["content"]))
        else:
            content = m["content"] or "…"
        out.append({"role": m["role"], "content": content})
    return out


def fmt_clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def status_html(session) -> str:
    if session is None:
        return ('<div class="vt-status"><span class="muted">Click "+ New Lesson" on the left to start, '
                'or pick a past lesson to review.</span></div>')
    st = session.status()
    if session.mode == "pron":
        # 發音課不計時，顯示當前步驟
        step = "Ended" if st["ended"] else pron.step_label(session.id)
        return ('<div class="vt-status"><div class="row">'
                f'<span><b>{html.escape(st["agent"])}</b></span>'
                f'<span class="phase">Pronunciation · {html.escape(step)}</span>'
                f'<span class="muted">Topic: {html.escape(st["topic"] or "Free talk")}</span></div></div>')
    pct = 100 if st["ended"] else min(100, st["elapsed"] / st["total"] * 100) if st["total"] else 0
    return (
        '<div class="vt-status"><div class="row">'
        f'<span><b>{html.escape(st["agent"])}</b></span>'
        f'<span class="phase">{html.escape(st["phase_label"])}</span>'
        + ("" if st["ended"] else f'<span>{fmt_clock(st["remaining"])} left</span>') +
        f'<span class="muted">Topic: {html.escape(st["topic"] or "Free talk")}</span>'
        f'</div><div class="vt-bar"><div style="width:{pct:.1f}%"></div></div></div>'
    )


def day_label(ts):
    d = datetime.datetime.fromtimestamp(ts)
    delta = (datetime.date.today() - d.date()).days
    day = "Today" if delta == 0 else "Yesterday" if delta == 1 else d.strftime("%m/%d")
    return f"{day} {d.strftime('%H:%M')}"


MODE_LABELS = {"voice": "Voice", "text": "Text", "pron": "Pron"}   # 歷史列表裡顯示的課堂類型
LIVE_MARK = "\u200b"   # 零寬空格：進行中的課的顯示文字前綴（看不見），CSS 據此畫綠點


def history_choices():
    # 歷史課堂列表：(顯示文字, 課堂 id)，顯示時間和課堂類型，進行中的課前面有綠點（見 CSS）。
    return [
        (f"{LIVE_MARK if s['status'] == 'active' else ''}{day_label(s['started_at'])} · "
         f"{MODE_LABELS.get(s['mode'], s['mode'])}", s["id"])
        for s in builder.list_sessions()
    ]


# input_state() 返回的控件更新順序，build_ui() 裡的 controls 列表必須和它一致
CONTROLS = ("msg", "send", "end", "delete", "chat", "voice", "report", "recording", "voice_ctl", "pron", "pron_ctl")
N_CONTROLS = len(CONTROLS)
REPORT_SLOT = CONTROLS.index("report")


def recording_html(session_id, path) -> str:
    # 語音課結束後的課堂錄音播放卡片：這裡只給數據（課堂 id、錄音版本、長度、整段波紋），
    # 卡片內容（波紋、播放鍵、快退快進、調速）由 voice.js 畫出來並負責播放。
    # data-v 是錄音文件的修改時間，錄音重新生成後網址會變，瀏覽器不會用舊的快取。
    with wave.open(str(path), "rb") as w:
        seconds = w.getnframes() / w.getframerate()
    peaks = ",".join(map(str, voice.recording_peaks(path)))
    return (f'<div class="vt-player" data-sid="{session_id}" data-v="{path.stat().st_mtime_ns}" '
            f'data-duration="{seconds:.2f}" data-peaks="{peaks}"></div>')


def report_card(session):
    # 語音課上課中（聊天記錄隱藏）有了總結報告時，在 Listening / Speaking 下方顯示總結卡片。
    show = (session is not None and session.mode == "voice" and not session.ended
            and session.report is not None)
    return gr.update(visible=show, value=render_report(session.report) if show else "")


def input_state(session, pron_ready=True):
    """返回 CONTROLS 裡各控件的更新（順序見 CONTROLS）。
    文字課：上課中可輸入，顯示 Send 和 End Lesson；已結束鎖住輸入，只顯示 Delete。
    語音課：上課中只顯示 Listening / Speaking（總結階段加總結卡片）和 End Lesson；
            已結束顯示對話記錄、課堂錄音和 Delete。
    發音課：只顯示發音課面板（pron.js 渲染，結束後是結果和對比）；pron_ready=False 表示短文還沒寫好。
    沒有課：按鈕全部隱藏。"""
    voice_lesson = session is not None and session.mode == "voice"
    pron_lesson = session is not None and session.mode == "pron"
    active = session is not None and not session.ended
    if voice_lesson or pron_lesson:
        msg = gr.update(visible=False)
    elif active:
        msg = gr.update(visible=True, interactive=True, placeholder="Reply in English, press Enter to send")
    else:
        placeholder = ("Start a new lesson first" if session is None else
                       "This lesson has ended. Click \"+ New Lesson\" to start another one.")
        msg = gr.update(visible=True, interactive=False, placeholder=placeholder)
    recording = voice.recording_path(session.id) if voice_lesson and not active else None
    return (
        msg,
        gr.update(visible=active and session.mode == "text"),            # Send
        gr.update(visible=active),                                         # End Lesson
        gr.update(visible=session is not None and not active),            # Delete
        gr.update(visible=not (voice_lesson and active) and not pron_lesson),   # 對話區
        gr.update(visible=voice_lesson and active),                        # Listening / Speaking
        report_card(session),                                              # 總結卡片
        gr.update(visible=recording is not None, value=recording_html(session.id, recording) if recording else ""),
        # 語音控制：值變化時 voice.js 連接 / 掛斷；加時間戳保證每次都觸發（斷線後點歷史可重連）
        f"{session.id}:{time.time_ns()}" if voice_lesson and active else "",
        gr.update(visible=pron_lesson),                                    # 發音課面板
        # 發音課控制：值變化時 pron.js 顯示這節課（課堂 id 沒變時不重載）
        f"{session.id}:{time.time_ns()}" if pron_lesson and pron_ready else "",
    )


def get_session(sid):
    return builder.open_session(sid) if sid is not None else None


# ---------------- 事件處理 ----------------
# 每個處理函數返回 / 產出的值，依次對應 build_ui() 裡綁定的 outputs 列表

def topic_choices():
    try:
        return [RECOMMEND] + builder.load_topics()
    except builder.AgentConfigError as e:
        gr.Warning(str(e))
        return [RECOMMEND]


def on_new_lesson(agent_name, topic, mode):
    # 開一節新課（mode：voice 語音課 / text 文字課），並流式顯示開場白。
    # 語音課的開場白同樣寫進對話，由 voice.py 念出來。
    try:
        agent = builder.load_agent(agent_name)
        if mode == "pron":
            pron.check_ready(agent)
        session = agent.start_session(None if topic == RECOMMEND else topic, mode=mode or "voice")
    except builder.AgentConfigError as e:
        raise gr.Error(str(e))
    if session.mode == "voice":
        voice.prepare(session)
    history = gr.update(choices=history_choices(), value=session.id)
    if session.mode == "pron":
        # 發音課：先顯示「正在準備短文」，老師寫好短文後再交給 pron.js
        yield session.id, [], status_html(session), history, *input_state(session, pron_ready=False)
        try:
            pron.Course(session).start()
        except pron.PronError as e:
            for _ in session.finish():
                pass
            gr.Warning(str(e))
        history = gr.update(choices=history_choices(), value=session.id)
        yield session.id, [], status_html(session), history, *input_state(session)
        return
    yield session.id, to_chat(session), status_html(session), history, *input_state(session)
    for _ in session.greet():
        yield session.id, to_chat(session), status_html(session), history, *input_state(session)


def on_send(text, sid):
    # 學生發言，流式顯示老師回覆。
    text = (text or "").strip()
    session = get_session(sid)
    if not text:
        yield gr.skip(), gr.skip(), gr.skip()
        return
    if session is None:
        gr.Warning("Click \"+ New Lesson\" on the left first.")
        yield gr.skip(), gr.skip(), gr.skip()
        return
    # 先把學生的話顯示出來，老師可能還在說上一句（ask 會排隊等待）
    yield "", to_chat(session) + [{"role": "user", "content": text}], status_html(session)
    for _ in session.ask(text):
        yield "", to_chat(session), status_html(session)
    yield "", to_chat(session), status_html(session)


def generating_updates(session, shown):
    """老師生成中（關懷、總結等）每次刷新時 CONTROLS 的更新：其他控件不動，只在總結報告剛生成時顯示卡片。
    shown 是一個列表，記錄卡片是否已經顯示過，避免每個分塊都重繪卡片。"""
    updates = [gr.skip()] * N_CONTROLS
    if not shown and session.report is not None and session.mode == "voice":
        updates[REPORT_SLOT] = report_card(session)
        shown.append(True)
    return updates


def wait_voice(session):
    # 語音課下課：等老師最後的話播完、錄音寫完，再切換到課後頁面。
    if session.mode == "voice":
        voice.wait_finished(session.id, VOICE_END_WAIT)


def on_tick(sid):
    # 計時器每秒觸發：刷新倒計時；有事件（關懷、總結、下課）時顯示出來。沒變化的組件用 gr.skip() 跳過。
    session = get_session(sid)
    if session is not None and session.mode == "pron":
        # 發音課不計時：刷新狀態條上的步驟；在面板裡結束時刷新按鈕和歷史列表
        if pron.take_ended_notice(session.id):
            yield (to_chat(session), status_html(session), *input_state(session),
                   gr.update(choices=history_choices(), value=session.id))
        elif not session.ended:
            yield gr.skip(), status_html(session), *(gr.skip(),) * (N_CONTROLS + 1)
        else:
            yield (gr.skip(),) * (N_CONTROLS + 3)
        return
    if session is None or session.ended:
        yield (gr.skip(),) * (N_CONTROLS + 3)
        return
    event = session.tick()
    if event is None:
        yield gr.skip(), status_html(session), *(gr.skip(),) * (N_CONTROLS + 1)
        return
    if event.chunks is not None:
        shown = []
        for _ in event.chunks:
            yield to_chat(session), status_html(session), *generating_updates(session, shown), gr.skip()
    history = gr.skip()
    if event.kind == "ended":
        wait_voice(session)
        history = gr.update(choices=history_choices(), value=session.id)
        gr.Info("The lesson is over. Well done!")
    yield to_chat(session), status_html(session), *input_state(session), history


def on_end(confirmed, sid):
    # 學生主動結束：先給本節總結，再結束課堂。
    session = get_session(sid)
    if not confirmed or session is None or session.ended:
        yield (gr.skip(),) * (N_CONTROLS + 3)
        return
    shown = []
    for _ in session.finish():
        # 語音課：報告一生成就顯示卡片；之後老師念總結期間保持語音課佈局，播完再切到課後頁面
        yield to_chat(session), status_html(session), *generating_updates(session, shown), gr.skip()
    wait_voice(session)
    pron.take_ended_notice(session.id)   # 發音課：這裡已經刷新頁面，計時器不用再刷
    gr.Info("The lesson is over. Well done!")
    yield (to_chat(session), status_html(session), *input_state(session),
           gr.update(choices=history_choices(), value=session.id))


def on_delete(confirmed, sid):
    # 從歷史列表刪除當前這節課（數據庫記錄保留），然後回到空白頁。
    if not confirmed or sid is None:
        return (gr.skip(),) * (N_CONTROLS + 4)
    try:
        builder.delete_session(sid)
    except ValueError:
        gr.Warning("You can't delete a lesson in progress. End it first.")
        return (gr.skip(),) * (N_CONTROLS + 4)
    return (None, [], status_html(None), *input_state(None),
            gr.update(choices=history_choices(), value=None))


def on_open_history(sid):
    # 點開一節歷史課堂（進行中的課可以接著上）。
    session = get_session(sid)
    return sid, to_chat(session), status_html(session), *input_state(session)


def on_load():
    # 每次打開 / 刷新頁面都重新讀取老師、話題和歷史，改了 JSON 刷新頁面即可生效。
    agents = builder.list_agents()
    return (gr.update(choices=agents, value=agents[0] if agents else None),
            gr.update(choices=topic_choices(), value=RECOMMEND),
            gr.update(choices=history_choices(), value=None))


# ---------------- 頁面 ----------------

def build_ui():
    with gr.Blocks(title="VTutor", fill_height=True) as demo:
        sid = gr.State(None)   # 當前顯示的課堂 id

        # 下拉框和歷史列表的內容由 on_load() 在頁面打開時填入
        with gr.Sidebar(width=300, elem_id="vt-sidebar"):
            gr.Markdown("## VTutor")
            new_btn = gr.Button("+ New Lesson", variant="primary")
            # filterable=False：只能從選項裡選，點開時不能打字
            mode_dd = gr.Dropdown(choices=LESSON_MODES, value="voice", label="Lesson", filterable=False)
            agent_dd = gr.Dropdown(label="Teacher", filterable=False)
            topic_dd = gr.Dropdown(choices=[RECOMMEND], value=RECOMMEND, label="Topic", filterable=False)
            history = gr.Radio(label="Past Lessons", elem_id="vt-history")

        with gr.Row(elem_id="vt-top"):
            status = gr.HTML(status_html(None), scale=1)
            end_btn = gr.Button("End Lesson", scale=0, min_width=110, variant="stop", visible=False)
            delete_btn = gr.Button("Delete", scale=0, min_width=80, variant="secondary", visible=False)
        with gr.Column() as chat_box:
            chatbot = gr.Chatbot(scale=1, height=560, group_consecutive_messages=False,
                                 placeholder="Click \"+ New Lesson\" on the left. Your teacher will say hello first.")
        voice_panel = gr.HTML(VOICE_PANEL, visible=False)
        voice_report = gr.Markdown(visible=False, elem_id="vt-voice-report")   # 語音課總結卡片
        pron_panel = gr.HTML(PRON_PANEL, visible=False)                         # 發音課面板（pron.js 渲染）
        with gr.Row():
            msg = gr.Textbox(show_label=False, scale=8, interactive=False,
                             placeholder="Start a new lesson first", submit_btn=False)
            send_btn = gr.Button("Send", scale=1, variant="primary", visible=False)
        recording = gr.HTML(visible=False)   # 語音課結束後的課堂錄音播放卡片（recording_html()）
        # 隱藏的勾選框：接收瀏覽器確認框的結果（確定 / 取消）
        end_confirmed = gr.Checkbox(visible=False)
        delete_confirmed = gr.Checkbox(visible=False)
        # 隱藏的輸入框（CSS 隱藏，不用 visible=False，保證值變化時前端事件能觸發）：通知 voice.js 連接 / 掛斷
        voice_ctl = gr.Textbox("", elem_id="vt-voice-ctl", show_label=False, container=False)
        pron_ctl = gr.Textbox("", elem_id="vt-pron-ctl", show_label=False, container=False)
        timer = gr.Timer(TICK_SECONDS)

        # 順序和 CONTROLS 一致（input_state() 按這個順序返回更新）
        controls = [msg, send_btn, end_btn, delete_btn, chat_box, voice_panel, voice_report, recording, voice_ctl,
                    pron_panel, pron_ctl]
        assert len(controls) == N_CONTROLS

        new_btn.click(on_new_lesson, [agent_dd, topic_dd, mode_dd], [sid, chatbot, status, history, *controls])
        for trigger in (msg.submit, send_btn.click):
            trigger(on_send, [msg, sid], [msg, chatbot, status], show_progress="hidden")
        # trigger_mode="once"：上一次 tick 還在流式輸出（關懷、總結）時，忽略新的觸發
        timer.tick(on_tick, sid, [chatbot, status, *controls, history],
                   show_progress="hidden", trigger_mode="once")
        history.input(on_open_history, history, [sid, chatbot, status, *controls])
        # 結束 / 刪除：先在瀏覽器彈確認框，結果寫進隱藏勾選框，再交給 Python 處理
        end_btn.click(None, None, end_confirmed,
                      js="() => confirm('End the lesson now? You will get a summary first.')").then(
            on_end, [end_confirmed, sid], [chatbot, status, *controls, history], show_progress="hidden")
        delete_btn.click(None, None, delete_confirmed,
                         js="() => confirm('Delete this lesson from your history?')").then(
            on_delete, [delete_confirmed, sid], [sid, chatbot, status, *controls, history])
        voice_ctl.change(None, voice_ctl, None, js="(v) => { window.vtVoice && window.vtVoice.sync(v); }")
        pron_ctl.change(None, pron_ctl, None, js="(v) => { window.vtPron && window.vtPron.sync(v); }")
        demo.load(on_load, None, [agent_dd, topic_dd, history])
    return demo


def create_app() -> FastAPI:
    # FastAPI 為主程序：先註冊語音 WebSocket，再把 Gradio 頁面掛到 /（掛載點會接管其餘所有路徑）。
    app = FastAPI()

    @app.websocket("/voice/{session_id}")
    async def voice_ws(ws: WebSocket, session_id: int):
        await voice.serve(ws, session_id)

    @app.get("/voice/{session_id}/recording")
    def voice_recording(session_id: int):
        # 課堂錄音（課後播放卡片用）。FileResponse 支持分段下載，瀏覽器可以直接跳到任意位置播放
        path = voice.recording_path(session_id)
        if path is None:
            raise HTTPException(404, "No recording for this lesson")
        return FileResponse(path, media_type="audio/wav")

    # 發音課接口（pron.js 調用）。同步函數由 FastAPI 放進線程池運行，LLM 和 Azure 調用不會卡住其他請求。
    def pron_call(fn):
        try:
            return fn()
        except pron.PronError as e:
            raise HTTPException(400, str(e))

    @app.get("/pron/{session_id}/state")
    def pron_state(session_id: int):
        return pron_call(lambda: pron.get_course(session_id).state())

    @app.get("/pron/{session_id}/tts")
    def pron_tts(session_id: int, text: str, slow: int = 0):
        path = pron_call(lambda: pron.get_course(session_id).tts(text, bool(slow)))
        return FileResponse(path, media_type="audio/mpeg")

    @app.get("/pron/{session_id}/audio")
    def pron_audio(session_id: int, step: str, sentence: int | None = None):
        # 學生第一次 / 第二次讀全文的錄音（不給 sentence 時是整篇）
        audio = pron_call(lambda: pron.get_course(session_id).reading_audio(step, sentence))
        return Response(audio, media_type="audio/wav")

    @app.post("/pron/{session_id}/assess")
    async def pron_assess(session_id: int, request: Request, step: str, sentence: int = -1):
        wav = await request.body()
        return await asyncio.to_thread(pron_call, lambda: pron.get_course(session_id).assess(step, sentence, wav))

    @app.post("/pron/{session_id}/feedback")
    def pron_feedback(session_id: int, attempt: int):
        return {"text": pron_call(lambda: pron.get_course(session_id).feedback(attempt))}

    @app.post("/pron/{session_id}/chat")
    async def pron_chat(session_id: int, request: Request):
        # 結果頁的老師聊天：body 是 {"text": 學生的問題}
        text = str((await request.json()).get("text", ""))
        return await asyncio.to_thread(pron_call, lambda: pron.get_course(session_id).chat(text))

    @app.post("/pron/{session_id}/advance")
    def pron_advance(session_id: int, action: str):
        return pron_call(lambda: pron.get_course(session_id).advance(action))

    demo = build_ui().queue(default_concurrency_limit=4)
    app = gr.mount_gradio_app(app, demo, path="/", css=CSS, head=f"<script>{VOICE_JS}</script><script>{PRON_JS}</script>",
                              footer_links=[])
    app.state.demo = demo
    # mount_gradio_app 另建了一個 Gradio 應用來服務頁面（沒有存回 demo），關閉時要通知的是它
    app.state.gradio_app = next(r.app for r in app.routes if isinstance(r, Mount))
    return app


class Server(uvicorn.Server):
    """按 Ctrl+C 時先讓 Gradio 結束頁面的長連接（心跳、事件隊列），再正常關閉。
    否則 uvicorn 會一直等這些連接自己斷開，只要瀏覽器頁面還開著就關不掉。
    （gr.Blocks.launch() 自己會這樣做，掛到 FastAPI 下之後要自己做。）"""

    async def shutdown(self, sockets=None):
        state = self.config.app.state
        state.gradio_app.stop_event.set()   # 心跳串流看到它就結束
        state.demo._queue.close()           # 事件隊列停止，等待中的事件結束
        await super().shutdown(sockets)


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    print(f"VTutor: {url}  (Ctrl+C to stop)")
    threading.Timer(1.5, webbrowser.open, [url]).start()
    # timeout_graceful_shutdown：還有連接沒斷開時，最多再等 3 秒就強制關閉
    server = Server(uvicorn.Config(create_app(), host=HOST, port=PORT, log_level="warning",
                                   timeout_graceful_shutdown=3))
    try:
        server.run()
    except KeyboardInterrupt:
        # uvicorn 關閉完成後會把 Ctrl+C 再拋出一次（uvicorn.run() 內部也是這樣接住的），不是錯誤
        pass
    print("VTutor stopped.")
