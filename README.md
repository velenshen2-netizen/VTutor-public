English Teacher Agent
技術架構：
fastapi + gradio + python + sqlite

目錄結構：
app.py 負責定義http頁面和功能，搭建起整個應用、業務邏輯相關的代碼都放在這裡；FastAPI 為主程序，掛載 Gradio 頁面和語音 WebSocket
builder.py 負責解析Json，建立Agent
voice.py 負責語音課：語音識別、語音合成、通話管理、課堂錄音
static/voice.js 語音課的瀏覽器端：麥克風收音、播放老師語音
pron.py 負責發音課：Azure 發音評估和示範語音、錯誤本、短文生成、課堂流程
static/pron.js、static/pron.css 發音課的瀏覽器端：文章和評分顯示、錄音、上傳評分
db.py 負責建表讀寫
agents/xxx.json 負責定義老師，prompt，llm，語音，糾錯，總結
topics.json 負責定義話題

如何啟動：
1、安裝python3.10以上版本
2、在VTutor目錄下，依次執行
    python -m venv .venv
    .\.venv\Scripts\python -m pip install -r requirements.txt
3、複製一份.env.example改名為.env，並在裡面填寫你的LLM API key
4、以後每次啟動，執行.\.venv\Scripts\python .\src\app.py 
5、瀏覽器自動彈出頁面http://127.0.0.1:7860

語音課：
1、側邊欄的 Lesson 選擇框默認是 Voice lesson，此時點 + New Lesson 進入語音課；選 Text lesson 則進入文字課。模式開課後不能切換
2、第一次上語音課時瀏覽器會詢問麥克風權限，請選允許（建議使用 Chrome 或 Edge）
3、上課時頁面只顯示 Listening / Speaking：老師說話時是 Speaking，此時不收音；其他時候是 Listening，直接開口說話即可，停頓約 0.8 秒算說完一句
4、下課後在 Past Lessons 打開這節課，可以看到完整的聊天記錄，底部是本節課錄音，可以播放和拖動
5、錄音保存在 data/audio/課堂id.wav
6、語音服務在 agent JSON 的 voice 段配置，支持 OpenAI 和 Gemini（Claude 沒有語音接口，但可以照常做老師的對話模型），寫法見 voice.py 開頭的說明。沒有 voice 段的老師只能上文字課
7、示例老師的語音合成用 gemini_live（Gemini 3.8 Live 逐字朗讀），免費額度不限次數；專用 TTS 模型（provider: gemini）免費額度每天只有約 10 次，一節課就會用完

發音課：
1、需要 Azure Speech：在 .env 填 AZURE_SPEECH_KEY 和 AZURE_SPEECH_REGION（發音評估和示範語音共用）。短文和老師回饋仍由老師 JSON 裡的 llm.chat 生成
2、側邊欄 Lesson 選 Pronunciation lesson，點 + New Lesson。流程：① 老師寫短文 → ② 聽示範 → ③ 讀全文 → ④ 練習 → ⑤ 再讀全文
3、③ 和 ⑤ 一句一句讀，錄完自動跳到下一句，讀完再一起顯示結果（綠：好；黃：還可以；紅：要練）。Redo previous 可以回到上一句重錄
4、④ 按 ③ 的結果決定：有句子低於 80 分就練最低的 3 句（先練句中的問題字，再練整句）；句子都過關但有字不好就只練那幾個字；全部都好就可以選結束或挑戰更難的一篇
5、④ 每個字 / 每句 80 分過關，最多 3 次；錄音中按 Redo 重錄、或沒唸完整的錄音，都不計次數
6、錄音：按 Record 或空白鍵開始 / 結束；說完停頓 1 秒自動結束，單次最長 30 秒
7、錯誤本只用 ③ 的結果更新：記錯過的字（次數、最近日期）和音素（次數）；某個字之後連續 3 篇都唸對就移除。新文章優先放出錯多、最近還在錯的字
8、兩種「等級」分開：文章難度用 CEFR（老師的 level，挑戰更難時升一級）；練習重點按最近 3 次 ③ 的分數自動選（單音 → 字重音和連音 → 句子重音和語調）。分數只用來看進步，不換算成 CEFR 等級
9、配置在 agent JSON 的 pronunciation 段，可調字段見 builder.py 的 PRON_DEFAULTS。沒有這一段的老師不能上發音課
10、學生錄音保存在 data/pron/課堂id/，示範語音快取在 data/pron_tts/。結果頁可以聽老師的示範和自己第一次、第二次讀全文的錄音（整篇或單句）
11、結果頁右下角有聊天按鈕，點開彈窗和老師文字聊天，問分數和發音的問題；老師根據這節課的評分資料用中文回答，聊天記錄存在 pron_chat 表
