// 發音課的瀏覽器端：顯示文章和評分、播放示範、錄音（自動停止、上限、空白鍵、波形）、上傳評分。
// 流程和接口見 pron.py；app.py 把這個文件注入頁面，隱藏輸入框 #vt-pron-ctl 的值變化時調用 vtPron.sync(值)：
// 值以課堂 id 開頭就顯示這節發音課，空字符串就清空。
// 頁面上的固定文字（按鈕、標籤、提示）用英文；老師的回饋等教學內容由服務端生成，是中文。
//
// 文件分區：常數和小工具 → 播放 → 錄音（Recorder）→ 面板（Panel：狀態、錄音控制、播放、聊天、渲染）→ 全局事件
// 結果頁的播放：this.playingRec 記錄正在播哪段（model / read1 / read2 整篇，或 model:3 / read1:3 單句），
//   播放中用 setPlayingSentence() 高亮文章裡正在唸的句子；拖進度條、點文章句子都跳到句子開頭（jumpToSentence）。
(() => {
  // ---- 錄音 ----
  const MIC_RATE = 16000;         // 上傳給 Azure 的錄音採樣率
  const BLOCK_MS = 50;            // 音量檢測的時間塊
  const MIN_SPEECH_RMS = 0.008;   // 「開始說話」門檻的下限（實際門檻按環境底噪調整）
  const MIN_SILENCE_RMS = 0.004;  // 「說完了」門檻的下限
  const HIGHPASS_HZ = 80;         // 高通濾波：濾掉噴麥的低頻衝擊、桌面震動、冷氣轟聲（說話用不到這個頻段）
  const START_MUTE_MS = 120;      // 錄音開頭靜音的長度：去掉按空白鍵 / 點滑鼠的喀聲
  const LIMIT_KNEE = 0.8;         // 柔性限幅從這個音量開始壓縮
  // ---- 評分和播放 ----
  const GOOD = 80, FAIR = 60;     // 分數顏色：>= 80 綠，60-79 黃，< 60 紅（和 pron.py 一致）
  const REC_GAP = 0.4;            // 整篇錄音裡句與句之間的靜音秒數（和 pron.py 的 REC_GAP_SECONDS 一致）
  // ---- 流程條的五步：服務端 step → 第幾步（0 起）----
  const FLOW = ["Passage", "Listen", "Read", "Practice", "Read again"];
  const STEP_NO = { demo: 1, read1: 2, practice: 3, choice: 3, read2: 4, done: 5 };
  const SCORE_KEYS = [["score", "Overall", ""], ["accuracy", "Accuracy", "sounds"], ["fluency", "Fluency", ""],
                      ["completeness", "Completeness", ""], ["prosody", "Prosody", "intonation"]];

  // 線條圖示（Lucide 風格，24×24，跟隨文字顏色）
  const ICON_PATHS = {
    play: '<polygon points="7 4 19 12 7 20 7 4"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    mic: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M19 10v1a7 7 0 0 1-14 0v-1"/><line x1="12" y1="18" x2="12" y2="22"/>',
    redo: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
    next: '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
    volume: '<path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a10 10 0 0 1 0 14"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    up: '<polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/>',
    flag: '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/>',
    chat: '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>',
    close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    send: '<path d="M22 2 11 13"/><path d="M22 2 15 22 11 13 2 9 22 2z"/>',
    alert: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
  };
  const icon = (name) => `<svg class="vt-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON_PATHS[name]}</svg>`;

  const TAP_WORKLET = `
    class VtPronTap extends AudioWorkletProcessor {
      process(inputs) {
        const ch = inputs[0] && inputs[0][0];
        if (ch) this.port.postMessage(ch.slice(0));
        return true;
      }
    }
    registerProcessor("vt-pron-tap", VtPronTap);`;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const norm = (w) => String(w).toLowerCase().replace(/’/g, "'").replace(/[^a-z0-9']/g, "").replace(/^'+|'+$/g, "");
  const cls = (score) => (score >= GOOD ? "vt-good" : score >= FAIR ? "vt-fair" : "vt-bad");   // 分數 → 顏色 class
  const fmt = (v) => (v == null ? "–" : Math.round(v));                       // 分數顯示成整數
  const base = () => location.pathname.replace(/\/$/, "");                   // 接口網址前綴（Gradio 掛在 /）
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const clock = (sec) => `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;   // 秒 → m:ss
  const signed = (d) => (d > 0 ? `+${d}` : d < 0 ? `${d}` : "±0");           // 分數變化：+3 / -2 / ±0
  const deltaCls = (d) => (d > 0 ? "vt-good" : d < 0 ? "vt-bad" : "vt-same");

  function store(key, value) {
    // localStorage 讀（不給 value）/ 寫，只存個人偏好（慢速開關）
    try {
      if (value === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, value);
    } catch (e) { /* 無痕模式等情況下讀寫不了，用默認值 */ }
    return null;
  }

  // ---------------- 播放 ----------------

  const player = new Audio();
  let playToken = 0;

  function play(url) {
    // 播放一段音頻；再次調用或 stopPlay() 會打斷上一段。返回播完（或被打斷）時完成的 Promise。
    const token = ++playToken;
    return new Promise((resolve) => {
      const done = () => { player.onended = player.onerror = null; resolve(token === playToken); };
      player.onended = done;
      player.onerror = done;
      player.src = url;
      player.play().catch(done);
    });
  }

  function stopPlay() {
    playToken++;
    player.pause();
  }

  // ---------------- 錄音 ----------------

  class Recorder {
    // 麥克風收音 → 16kHz 16-bit PCM；檢測音量用於自動停止和波形。
    // 處理鏈：麥克風（瀏覽器降噪，不用自動增益）→ 80Hz 高通（去噴麥、桌面震動）→（必要時）低通 → 取樣
    //         → 柔性限幅（不會頂到最大值爆音）→ 錄完裁掉開頭的按鍵聲、太小聲時整段放大
    constructor() {
      this.on = false;
      this.levels = [];
    }

    async open() {
      if (this.ctx) return;
      const stream = await navigator.mediaDevices.getUserMedia({
        // autoGainControl 關掉：它在安靜時把音量調大（放大底噪），一開口又來不及調回（爆音）
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false, channelCount: 1 },
      });
      // 盡量讓瀏覽器直接以 16kHz 收音（內建的重採樣品質好）；Firefox 等不支持時退回默認採樣率，自己濾波後降採樣
      let ctx, source;
      try {
        ctx = new AudioContext({ sampleRate: MIC_RATE });
        source = ctx.createMediaStreamSource(stream);
      } catch (e) {
        if (ctx) ctx.close();
        ctx = new AudioContext();
        source = ctx.createMediaStreamSource(stream);
      }
      const url = URL.createObjectURL(new Blob([TAP_WORKLET], { type: "application/javascript" }));
      await ctx.audioWorklet.addModule(url);
      const filter = (type, freq) => {
        const f = ctx.createBiquadFilter();
        f.type = type;
        f.frequency.value = freq;
        f.Q.value = Math.SQRT1_2;
        return f;
      };
      let node = source.connect(filter("highpass", HIGHPASS_HZ));
      if (ctx.sampleRate > MIC_RATE) {
        // 降採樣前先濾掉 7kHz 以上（兩級，約 24dB/倍頻），否則高頻嘶聲會折疊進可聽頻段變成雜音
        node = node.connect(filter("lowpass", 7000)).connect(filter("lowpass", 7000));
      }
      const tap = new AudioWorkletNode(ctx, "vt-pron-tap");
      tap.port.onmessage = (e) => this.onSamples(e.data);
      const mute = ctx.createGain();
      mute.gain.value = 0;   // 接到輸出端才會被處理，但不讓學生聽到自己的聲音
      node.connect(tap).connect(mute).connect(ctx.destination);
      this.ctx = ctx;
      this.stream = stream;
    }

    async start({ autoStopMs, maxSeconds, onAutoStop }) {
      await this.open();
      await this.ctx.resume();
      this.pcm = [];
      this.pos = 0;
      this.levels = [];
      this.blockSum = 0;
      this.blockN = 0;
      this.heard = false;
      this.silentMs = 0;
      this.elapsedMs = 0;
      this.floor = null;          // 環境底噪（開頭一小段裡最安靜的音量），用來定「開始說話 / 說完」的門檻
      this.clipped = 0;           // 限幅前超過最大值的取樣數（太大聲、噴麥）
      this.autoStopMs = autoStopMs;
      this.maxMs = maxSeconds * 1000;
      this.onAutoStop = onAutoStop;
      this.on = true;
    }

    onSamples(samples) {
      if (!this.on) return;
      // 降採樣到 16kHz（已經是 16kHz 時 ratio = 1，直接取用）：每個輸出點取對應區間的平均值
      const ratio = this.ctx.sampleRate / MIC_RATE;
      const out = [];
      let pos = this.pos;
      while (pos + ratio <= samples.length) {
        let sum = 0, n = 0;
        for (let i = Math.floor(pos); i < Math.floor(pos + ratio); i++) { sum += samples[i]; n++; }
        let v = n ? sum / n : 0;
        if (Math.abs(v) >= 0.99) this.clipped++;
        v = softLimit(v);
        out.push(v < 0 ? v * 0x8000 : v * 0x7fff);
        pos += ratio;
      }
      this.pos = pos - samples.length;
      this.pcm.push(Int16Array.from(out));
      // 每 BLOCK_MS 算一次音量：畫波形、判斷說完（說過話之後靜音夠久）和時長上限
      for (const s of samples) { this.blockSum += s * s; this.blockN++; }
      if (this.blockN >= this.ctx.sampleRate * BLOCK_MS / 1000) {
        const rms = Math.sqrt(this.blockSum / this.blockN);
        this.blockSum = 0;
        this.blockN = 0;
        this.levels.push(rms);
        if (this.levels.length > 400) this.levels.shift();
        this.elapsedMs += BLOCK_MS;
        // 底噪取開頭 0.5 秒裡最安靜的一塊（跳過按鍵聲所在的最前面一段）
        if (this.elapsedMs > START_MUTE_MS && this.elapsedMs <= 500) {
          this.floor = this.floor == null ? rms : Math.min(this.floor, rms);
        }
        // 底噪最多算到 0.005：學生一按下就開口時，開頭量到的其實是說話聲，門檻不能因此被抬得太高
        const floor = Math.min(this.floor ?? 0.002, 0.005);
        const speechAt = Math.max(MIN_SPEECH_RMS, floor * 4);    // 比底噪大 12dB 算說話
        const silenceAt = Math.max(MIN_SILENCE_RMS, floor * 2);  // 回到底噪的 2 倍以內算靜音
        if (this.elapsedMs > START_MUTE_MS && rms > speechAt) { this.heard = true; this.silentMs = 0; }
        else if (this.heard && rms < silenceAt) this.silentMs += BLOCK_MS;
        if (this.heard && this.silentMs >= this.autoStopMs) this.onAutoStop("silence");
        else if (this.elapsedMs >= this.maxMs) this.onAutoStop("limit");
      }
    }

    stop() {
      // 結束錄音，返回 WAV（16kHz 16-bit 單聲道）和這次是否太大聲。
      this.on = false;
      const total = this.pcm.reduce((n, a) => n + a.length, 0);
      const data = new Int16Array(total);
      let o = 0;
      for (const a of this.pcm) { data.set(a, o); o += a.length; }
      this.pcm = [];
      // 開頭的按鍵 / 滑鼠聲：前 START_MUTE_MS 靜音，再用 30ms 淡入（結尾不裁：學生常一唸完就按停止，會切到尾音）
      const mute = Math.min(total, Math.round(MIC_RATE * START_MUTE_MS / 1000));
      const fade = Math.min(total - mute, Math.round(MIC_RATE * 0.03));
      data.fill(0, 0, mute);
      for (let i = 0; i < fade; i++) data[mute + i] = Math.round(data[mute + i] * (i / fade));
      // 整段太小聲（關掉自動增益後，安靜的麥克風可能這樣）：用同一個倍數放大，最多 4 倍、峰值放到約一半
      let peak = 0;
      for (let i = 0; i < total; i++) peak = Math.max(peak, Math.abs(data[i]));
      if (peak > 300 && peak < 0x7fff * 0.3) {
        const gain = Math.min(4, (0x7fff * 0.5) / peak);
        for (let i = 0; i < total; i++) data[i] = Math.round(data[i] * gain);
      }
      const buf = new ArrayBuffer(44 + total * 2);
      const v = new DataView(buf);
      const str = (off, s) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
      str(0, "RIFF"); v.setUint32(4, 36 + total * 2, true); str(8, "WAVE");
      str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
      v.setUint32(24, MIC_RATE, true); v.setUint32(28, MIC_RATE * 2, true); v.setUint16(32, 2, true);
      v.setUint16(34, 16, true); str(36, "data"); v.setUint32(40, total * 2, true);
      for (let i = 0; i < total; i++) v.setInt16(44 + i * 2, data[i], true);
      // 超過最大值的取樣多於約 2ms：算太大聲（噴麥或離麥克風太近）
      const tooLoud = this.clipped > MIC_RATE * 0.002;
      return { wav: new Blob([buf], { type: "audio/wav" }), heard: this.heard, tooLoud };
    }

    cancel() {
      this.on = false;
      this.pcm = [];
    }

    close() {
      this.on = false;
      if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
      if (this.ctx) this.ctx.close();
      this.ctx = null;
      this.stream = null;
    }
  }

  function softLimit(v) {
    // 柔性限幅：0.8 以下不動，以上平滑地壓向 1，永遠不會頂到最大值變成削波爆音
    const a = Math.abs(v);
    if (a <= LIMIT_KNEE) return v;
    return Math.sign(v) * (LIMIT_KNEE + (1 - LIMIT_KNEE) * Math.tanh((a - LIMIT_KNEE) / (1 - LIMIT_KNEE)));
  }

  function drawWave() {
    // 錄音中每幀畫音量條（最新的在右邊）。用平方根放大小音量，沒有自動增益時安靜的麥克風也看得到波形。
    const canvas = document.getElementById("vt-pron-wave");
    if (!panel || !panel.rec.on || !canvas) return;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== w) canvas.width = w;
    if (canvas.height !== h) canvas.height = h;
    const g = canvas.getContext("2d");
    g.clearRect(0, 0, w, h);
    g.fillStyle = getComputedStyle(canvas).getPropertyValue("--vt-rec").trim() || "#dc2626";
    const bar = 3, gap = 3, n = Math.floor(w / (bar + gap));
    const levels = panel.rec.levels.slice(-n);
    levels.forEach((lv, i) => {
      const bh = Math.max(2, Math.min(1, Math.sqrt(lv) * 2.2) * (h - 4));
      g.fillRect(w - (levels.length - i) * (bar + gap), (h - bh) / 2, bar, bh);
    });
    requestAnimationFrame(drawWave);
  }

  // ---------------- 面板 ----------------

  class Panel {
    constructor(sid) {
      this.sid = sid;
      this.st = null;
      this.closed = false;
      this.slow = store("vtPronSlow") === "1";
      this.playingSentence = -1;    // 結果頁播放時正在唸的句子（文章裡高亮）
      this.blobs = {};              // 已下載的學生錄音：key → 瀏覽器記憶體裡的網址（可以任意跳轉播放位置）
      this.seeking = false;         // 正在拖動進度條（這時播放進度不要蓋掉拖動的位置）
      this.pendingSeek = null;      // 錄音還沒載入完就拖了進度條：載入後跳到這個秒數
      this.playingRec = null;       // 結果頁正在播放的音檔：model（老師示範）/ read1 / read2 整篇，或 read1:3 這樣的單句
      this.chatDraft = "";          // 結果頁聊天輸入框裡還沒送出的文字（重繪面板時保留）
      this.chatPending = null;      // 已送出、等老師回覆的問題
      this.chatError = "";
      this.chatOpen = false;        // 右下角的聊天彈窗是否打開
      this.rec = new Recorder();
      this.recTarget = null;        // 正在錄的目標：{step, sentence}
      this.readIdx = 0;             // ③ / ⑤ 正在讀第幾句
      this.uploads = {};            // ③ / ⑤：句子 → 上傳評分中的 Promise
      this.failed = new Set();      // ③ / ⑤：評分失敗、需要重錄的句子
      this.showResults = false;     // ③ 讀完後先顯示結果，按 Start practice 才進入 ④
      this.playing = -1;            // 示範時正在播第幾句
      this.demoSeq = 0;             // 每次開始播示範加一，用來判斷舊的播放是否已被打斷
      this.rewound = false;         // ③ / ⑤ 學生按了重錄上一句，等他錄完前不自動往後跳
      this.busy = "";               // 等待服務端時顯示的文字
      this.msg = "";                // 錯誤提示
      this.notice = "";             // 錄音品質提醒（太大聲）
      this.feedbackFor = null;      // 正在請求回饋的評分記錄 id
      this.version = 0;
    }

    api(path, opts) {
      // 呼叫 /pron/{課堂 id}/... 接口，返回 JSON；出錯時拋出帶服務端說明的 Error
      return fetch(`${base()}/pron/${this.sid}/${path}`, opts).then(async (r) => {
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
        return data;
      });
    }

    tts(text, slow) {
      // 示範語音的網址（服務端按文字快取）
      const q = new URLSearchParams({ text, slow: slow ? 1 : 0 });
      return `${base()}/pron/${this.sid}/tts?${q}`;
    }

    audioUrl(key) {
      // 單段音檔的網址。key：model:3（老師示範第 4 句，Azure 合成、有快取）、read1 / read2（學生整篇）、
      // read1:3（學生第 4 句）。老師示範整篇不是一個檔案，由 playModel() 逐句播放
      const [step, sentence] = key.split(":");
      if (step === "model") return this.tts(this.st.sentences[+sentence].text, false);
      const q = new URLSearchParams({ step });
      if (sentence != null) q.set("sentence", sentence);
      return `${base()}/pron/${this.sid}/audio?${q}`;
    }

    async load() {
      try {
        this.setState(await this.api("state"));
      } catch (e) {
        this.msg = `Could not load the lesson: ${e.message}`;
      }
      if (this.st && !this.st.ready && !this.st.ended && !this.closed) {
        setTimeout(() => this.load(), 1500);   // 老師還在寫短文
      }
      this.render();
    }

    setState(st) {
      const prevStep = this.st && this.st.step;
      this.st = st;
      if (!st.ready) return;
      if (st.step === "read1" || st.step === "read2") {
        if (prevStep !== st.step) { this.uploads = {}; this.failed.clear(); }
        this.readIdx = this.nextReadIdx();
      }
    }

    close() {
      this.closed = true;
      this.rec.close();
      stopPlay();
      Object.values(this.blobs).forEach((u) => URL.revokeObjectURL(u));
      this.blobs = {};
      const box = document.getElementById("vt-pron-chat");
      if (box) box.innerHTML = "";
    }

    get recording() { return this.rec.on; }

    // ---------------- 錄音控制（按鈕和空白鍵） ----------------

    canRecord() {
      const st = this.st;
      if (!st || !st.ready || st.ended || this.busy) return false;
      if (st.step === "read1" || st.step === "read2") return this.readIdx < st.sentences.length;
      if (st.step === "practice" && !this.showResults) return !!st.practice.target && !st.practice.finished;
      return false;
    }

    async toggleRecord() {
      if (this.recording) return this.finishRecord();
      if (!this.canRecord()) return;
      this.stopPlayback();
      const st = this.st;
      this.recTarget = { step: st.step, sentence: st.step === "practice" ? -1 : this.readIdx };
      this.msg = "";
      try {
        await this.rec.start({
          autoStopMs: st.config.auto_stop_ms, maxSeconds: st.config.max_seconds,
          onAutoStop: () => this.finishRecord(),
        });
      } catch (e) {
        console.error("[vtPron] microphone", e);
        this.msg = "Microphone unavailable. Please allow microphone access (Chrome or Edge recommended).";
        this.render();
        return;
      }
      this.render();
      requestAnimationFrame(drawWave);
    }

    async restartRecord() {
      // Redo 錄音中：丟掉這次，馬上重新開始（不上傳、不計次）；③ / ⑤ 沒在錄時：回到上一句重錄。
      if (this.recording) {
        this.rec.cancel();
        this.recTarget = null;
        return this.toggleRecord();
      }
      const st = this.st;
      if ((st.step === "read1" || st.step === "read2") && this.readIdx > 0) {
        this.readIdx = Math.min(this.readIdx, st.sentences.length) - 1;
        this.rewound = true;
        this.render();
      }
    }

    finishRecord() {
      if (!this.recording) return;
      const { wav, heard, tooLoud } = this.rec.stop();
      const target = this.recTarget;
      this.recTarget = null;
      // 太大聲（噴麥、離麥克風太近）：錄音照常送出評分，只提醒一下，下一次錄完沒問題就消失
      this.notice = tooLoud ? "Too loud — move the mic a little further away or slightly to the side of your mouth." : "";
      if (!heard) {
        this.msg = "No voice detected. Move closer to the microphone and try again.";
        this.render();
        return;
      }
      if (target.step === "practice") return this.uploadPractice(wav);
      this.uploadSentence(target.step, target.sentence, wav);
    }

    // ---------------- ③ / ⑤ 讀全文 ----------------

    nextReadIdx() {
      // 第一句還沒有結果、也不在評分中的句子；都有了返回句子數。
      const st = this.st, results = st[st.step];
      const i = results.findIndex((r, k) => (!r || this.failed.has(k)) && !this.uploads[k]);
      return i < 0 ? results.length : i;
    }

    uploadSentence(step, i, wav) {
      this.failed.delete(i);
      const send = () => this.api(`assess?step=${step}&sentence=${i}`, { method: "POST", body: wav });
      const p = send().catch(() => send()).then((out) => {
        if (this.uploads[i] === p) { this.st[step][i] = out.result; delete this.uploads[i]; }
      }).catch((e) => {
        if (this.uploads[i] === p) {
          delete this.uploads[i];
          this.failed.add(i);
          this.msg = `Sentence ${i + 1} could not be scored (${e.message}). Please record it again.`;
        }
      }).finally(() => {
        if (!this.rewound) this.readIdx = this.nextReadIdx();
        this.maybeFinishReading();
        this.render();
      });
      this.uploads[i] = p;
      this.rewound = false;
      this.readIdx = this.nextReadIdx();
      this.render();
      this.maybeFinishReading();
    }

    async maybeFinishReading() {
      // 所有句子都錄完、評分都回來後，請老師看結果，進入下一步。
      const st = this.st;
      if (this.busy || this.recording || this.rewound || !(st.step === "read1" || st.step === "read2")) return;
      if (Object.keys(this.uploads).length || this.failed.size) return;
      if (st[st.step].some((r) => !r)) return;
      this.busy = "Your teacher is reviewing your reading…";
      this.render();
      try {
        this.setState(await this.api("advance?action=finish_reading", { method: "POST" }));
        this.showResults = this.st.step === "practice";
      } catch (e) {
        this.msg = e.message;
      }
      this.busy = "";
      this.render();
    }

    // ---------------- ④ 練習 ----------------

    async uploadPractice(wav) {
      this.busy = "Scoring…";
      this.render();
      try {
        const out = await this.api("assess?step=practice", { method: "POST", body: wav });
        this.st.practice = out.practice;
        this.busy = "";
        this.render();
        this.loadFeedback(out.attempt);
      } catch (e) {
        this.busy = "";
        this.msg = `Scoring failed: ${e.message}. Please record again (this try doesn't count).`;
        this.render();
      }
    }

    async loadFeedback(attempt) {
      // ④ 每次評分後另外請求老師回饋（分數先顯示，回饋生成比較慢）
      this.feedbackFor = attempt;
      try {
        const out = await this.api(`feedback?attempt=${attempt}`, { method: "POST" });
        const last = this.st.practice && this.st.practice.last;
        if (last && last.attempt === attempt) last.feedback = out.text;
      } catch (e) {
        console.error("[vtPron] feedback", e);
      }
      if (this.feedbackFor === attempt) this.feedbackFor = null;
      this.render();
    }

    // ---------------- 播放：② 示範、④ 聽目標、結果頁的示範和錄音 ----------------

    async playDemo(from = 0, only = false) {
      // 逐句播放示範並高亮正在念的句子；only 只播一句。播放被打斷（停止、錄音、再點一句）就結束。
      const seq = ++this.demoSeq;
      const sents = this.st.sentences;
      for (let i = from; i < (only ? from + 1 : sents.length); i++) {
        this.playing = i;
        this.render();
        if (i + 1 < sents.length && !only) fetch(this.tts(sents[i + 1].text, this.slow)).catch(() => {});   // 預先合成下一句
        const finished = await play(this.tts(sents[i].text, this.slow));
        if (!finished || seq !== this.demoSeq) break;
      }
      if (seq === this.demoSeq) {
        this.playing = -1;
        this.render();
      }
    }

    stopPlayback() {
      // 停止所有播放（示範、老師示範整篇、學生錄音），清掉播放狀態和句子高亮
      this.demoSeq++;
      stopPlay();
      this.playing = -1;
      this.playingRec = null;
      this.pendingSeek = null;
      player.ontimeupdate = null;
      this.setPlayingSentence(-1);
    }

    setPlayingSentence(i) {
      // 高亮正在唸的句子：直接改元素的 class，不重繪整個面板（播放中每秒會呼叫好幾次）
      if (this.playingSentence === i) return;
      this.playingSentence = i;
      document.querySelectorAll("#vt-pron .vt-sent[data-i]").forEach((el) => el.classList.toggle("cur", +el.dataset.i === i));
    }

    durations(step) {
      // 學生某次讀全文各句錄音的秒數（沒有錄音的句子算 0）
      return ((this.st.audio_durations || {})[step] || []).map((d) => d || 0);
    }

    sentenceAt(step, t) {
      // 整篇錄音播到 t 秒時唸到第幾句：各句秒數依序累加，句間的空白算進前一句
      const durs = this.durations(step);
      let end = 0;
      for (let i = 0; i < durs.length; i++) {
        end += durs[i] + REC_GAP;
        if (t < end) return i;
      }
      return durs.length - 1;
    }

    showRecProgress(key, frac, label) {
      if (this.seeking) return;   // 拖動中由拖動的位置決定
      const bar = document.getElementById(`vt-rec-bar-${key}`);
      const time = document.getElementById(`vt-rec-time-${key}`);
      if (bar) bar.style.width = `${Math.max(0, Math.min(1, frac)) * 100}%`;
      if (time) time.textContent = label;
    }

    async playRec(key, fromSentence = null) {
      // 播放老師示範 / 學生錄音；再按一次同一個就停止。播放時更新進度條、高亮正在唸的句子（直接改元素，不重繪整個面板）。
      // fromSentence 有值是拖進度條跳句（老師示範），不是再按一次停止
      if (this.playingRec === key && fromSentence == null) { this.stopPlayback(); return this.render(); }
      this.stopPlayback();
      this.playingRec = key;
      this.render();
      const [step, one] = key.split(":");
      if (step === "model" && one == null) return this.playModel(fromSentence ?? 0);
      if (one != null) this.setPlayingSentence(+one);
      let url;
      try {
        url = await this.blobUrl(key);
      } catch (e) {
        this.msg = `Could not load the recording: ${e.message}`;
        this.stopPlayback();
        return this.render();
      }
      if (this.playingRec !== key) return;   // 下載期間改播了別的
      const done = play(url);
      player.ontimeupdate = () => {
        if (!player.duration) return;
        if (this.pendingSeek != null) {   // 載入前拖過進度條：現在可以跳了
          player.currentTime = this.pendingSeek;
          this.pendingSeek = null;
        }
        if (one == null) this.setPlayingSentence(this.sentenceAt(step, player.currentTime));
        this.showRecProgress(key, player.currentTime / player.duration, `${clock(player.currentTime)} / ${clock(player.duration)}`);
      };
      await done;
      if (this.playingRec === key) { this.stopPlayback(); this.render(); }
    }

    async playModel(from) {
      // 老師示範整篇：逐句播放（示範時已經合成過、有快取），這樣知道正在唸哪一句
      const n = this.st.sentences.length, seq = this.demoSeq;
      for (let i = from; i < n; i++) {
        this.setPlayingSentence(i);
        if (i + 1 < n) fetch(this.audioUrl(`model:${i + 1}`)).catch(() => {});   // 預先準備下一句
        player.ontimeupdate = () => {
          if (player.duration) this.showRecProgress("model", (i + player.currentTime / player.duration) / n, `Sentence ${i + 1} / ${n}`);
        };
        this.showRecProgress("model", i / n, `Sentence ${i + 1} / ${n}`);
        const finished = await play(this.audioUrl(`model:${i}`));
        if (!finished || seq !== this.demoSeq) return;   // 被停止、或改播別的
      }
      this.stopPlayback();
      this.render();
    }

    async blobUrl(key) {
      // 學生錄音先整段下載到瀏覽器記憶體再播放：錄音接口不支援分段下載（HTTP Range），
      // 直接用網址播放時瀏覽器沒辦法跳到中間，設定播放位置會從頭開始
      if (!this.blobs[key]) {
        const r = await fetch(this.audioUrl(key));
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        this.blobs[key] = URL.createObjectURL(await r.blob());
      }
      return this.blobs[key];
    }

    dragSeek(track, e) {
      // 按住進度條拖動：進度條跟著移動，放開時跳到那個位置（點一下也一樣）。只對正在播放的音檔。
      const key = track.dataset.key;
      if (this.playingRec !== key) return;
      e.preventDefault();
      const bar = track.firstElementChild;
      const fracAt = (ev) => {
        const r = track.getBoundingClientRect();
        return Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
      };
      this.seeking = true;
      bar.style.width = `${fracAt(e) * 100}%`;
      const move = (ev) => { bar.style.width = `${fracAt(ev) * 100}%`; };
      const up = (ev) => {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
        this.seeking = false;
        this.seekTo(key, fracAt(ev));
      };
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
    }

    seekTo(key, frac) {
      // 拖動放開後跳到對應那一句的開頭（和老師示範一致）
      if (key === "model") return this.jumpToSentence(key, Math.min(this.st.sentences.length - 1, Math.floor(frac * this.st.sentences.length)));
      const durs = this.durations(key);
      const total = durs.reduce((a, d) => a + d, 0) + REC_GAP * Math.max(0, durs.length - 1);
      this.jumpToSentence(key, this.sentenceAt(key, frac * total));
    }

    jumpToSentence(key, i) {
      // 在正在播放的整篇音檔裡跳到第 i 句的開頭。老師示範是逐句播放：從那一句重新往下播；
      // 學生錄音是各句接成一段：用各句秒數算出那一句的開始時間（不用等錄音載入，也不必知道總長度）
      if (key === "model") return this.playRec("model", i);
      const start = this.durations(key).slice(0, i).reduce((a, d) => a + d + REC_GAP, 0);
      this.setPlayingSentence(i);
      if (player.duration) player.currentTime = start;
      else this.pendingSeek = start;   // 還沒載入完，等開始播放時再跳
    }

    clickSentence(i) {
      // 點文章裡的句子（方案 A：文章當目錄）：
      //   正在播整篇（老師示範 / 第一次 / 第二次）→ 在同一段裡跳到這一句
      //   正在播表格裡的單句 → 改播同一個來源的這一句
      //   沒在播 → 聽老師示範這一句
      const st = this.st;
      if (!(st.step === "demo" || st.step === "done" || st.ended)) return;
      const rec = this.playingRec;
      if (rec && !rec.includes(":")) return this.jumpToSentence(rec, i);
      if (rec) return this.playRec(`${rec.split(":")[0]}:${i}`);
      return this.playDemo(i, true);
    }

    playTarget() {
      // ④ 聽目標（字或整句）的示範
      const t = this.st.practice && this.st.practice.target;
      if (t) play(this.tts(t.text, this.slow));
    }

    // ---------------- 結果頁：和老師聊天 ----------------

    async sendChat() {
      const text = this.chatDraft.trim();
      if (!text || this.chatPending) return;
      this.chatPending = text;
      this.chatDraft = "";
      this.chatError = "";
      this.renderChat();
      try {
        const out = await this.api("chat", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
        });
        this.st.chat = out.chat;
      } catch (e) {
        this.chatError = e.message;
        if (!this.chatDraft) this.chatDraft = text;   // 送失敗把問題放回輸入框
      }
      this.chatPending = null;
      this.renderChat();
    }

    // ---------------- 按鈕 ----------------

    async action(name, arg) {
      if (name === "rec") return this.toggleRecord();
      if (name === "redo") return this.restartRecord();
      if (name === "slow") { this.slow = !this.slow; store("vtPronSlow", this.slow ? "1" : "0"); return this.render(); }
      if (name === "playRec") return this.playRec(arg);
      if (name === "chatSend") return this.sendChat();
      if (name === "chatToggle") {
        this.chatOpen = !this.chatOpen;
        this.renderChat();
        if (this.chatOpen) { const input = document.getElementById("vt-chat-input"); if (input) input.focus(); }
        return;
      }
      if (name === "demo") return this.playing >= 0 ? (this.stopPlayback(), this.render()) : this.playDemo();
      if (name === "sentence") return this.clickSentence(+arg);
      if (name === "listen") return this.playTarget();
      if (name === "startPractice") { this.showResults = false; return this.render(); }
      const calls = { startReading: "start_reading", next: "next", end: "end", harder: "harder" };
      if (!calls[name]) return;
      this.stopPlayback();
      this.busy = name === "harder" ? "Your teacher is writing a harder passage…"
        : name === "end" ? "Finishing the lesson…" : "Please wait…";
      this.render();
      try {
        this.setState(await this.api(`advance?action=${calls[name]}`, { method: "POST" }));
        this.showResults = false;
      } catch (e) {
        this.msg = e.message;
      }
      this.busy = "";
      this.render();
    }

    // ---------------- 渲染 ----------------

    render() {
      const root = document.getElementById("vt-pron");
      if (!root || this.closed) return;
      this.version++;
      root.dataset.v = `${this.sid}:${this.version}`;
      root.innerHTML = this.html();
      bindClicks(root);
      if (this.recording) requestAnimationFrame(drawWave);
      this.renderChat();
    }

    chatAvailable() {
      // 讀完一次全文、在結果頁時才能和老師聊
      const st = this.st;
      return !!(st && st.ready && st.summary1 && (st.ended || st.step === "done"));
    }

    renderChat() {
      // 右下角懸浮的聊天按鈕和彈窗。不放在面板裡：面板重繪時不影響正在輸入的文字，也不會被版面裁切。
      let box = document.getElementById("vt-pron-chat");
      if (!box) {
        box = document.createElement("div");
        box.id = "vt-pron-chat";
        box.className = "vt-pron-chat";
        // 放在 Gradio 最外層容器裡（主題色、深色模式的 CSS 變量掛在這一層），用 fixed 定位浮在右下角
        (document.querySelector(".gradio-container") || document.body).appendChild(box);
        bindClicks(box);
        box.addEventListener("input", (e) => {
          if (e.target.id === "vt-chat-input" && panel) { panel.chatDraft = e.target.value; fitInput(e.target); }
        });
        box.addEventListener("keydown", (e) => {
          // Enter 送出，Shift+Enter 換行；中文輸入法選字時按的 Enter 不算
          if (e.target.id === "vt-chat-input" && e.key === "Enter" && !e.shiftKey && !e.isComposing && panel) {
            e.preventDefault();
            panel.sendChat();
          }
        });
        box.addEventListener("wheel", (e) => {
          // 滑鼠在聊天窗上時，滾輪只捲聊天記錄（輸入框內容很長時捲輸入框），不讓後面的頁面跟著捲
          const pop = e.target.closest(".vt-chat-pop");
          if (!pop) return;
          const area = e.target.closest("textarea");
          const el = area && area.scrollHeight > area.clientHeight ? area : pop.querySelector(".vt-chat-log");
          e.preventDefault();
          if (el) el.scrollTop += e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;   // deltaMode 1：按行滾動
        }, { passive: false });
      }
      if (this.closed || !this.chatAvailable()) { box.innerHTML = ""; return; }
      // 重繪會替換掉輸入框：記下焦點和游標，畫完放回去
      const active = document.activeElement;
      const typing = active && active.id === "vt-chat-input" ? [active.selectionStart, active.selectionEnd] : null;
      box.innerHTML = this.chatHtml();
      const input = document.getElementById("vt-chat-input");
      if (input) {
        fitInput(input);
        if (typing) { input.focus(); input.setSelectionRange(...typing); }
      }
      const log = document.getElementById("vt-chat-log");
      if (log) log.scrollTop = log.scrollHeight;
      syncChatVisibility();
    }

    html() {
      const st = this.st;
      if (!st) return `<div class="vt-pron-loading">${esc(this.msg || "Loading…")}</div>`;
      if (!st.ready) {
        return `<div class="vt-pron-loading">${st.ended ? "This lesson has no content."
          : '<span class="vt-spin"></span>Your teacher is writing a passage…'}</div>`;
      }
      const parts = [this.flowHtml(), this.metaHtml()];
      // 按步驟選畫面：結果頁 / ② 示範 / ③⑤ 讀全文 / ③ 結果（或全部過關的選擇）/ ④ 練習
      let body;
      if (st.ended || st.step === "done") body = this.doneHtml();
      else if (st.step === "demo") body = this.demoHtml();
      else if (st.step === "read1" || st.step === "read2") body = this.readHtml();
      else if (st.step === "choice" || this.showResults) body = this.resultsHtml();
      else body = this.practiceHtml();
      parts.push(`<div class="vt-body">${body}</div>`);
      if (this.msg) parts.push(`<div class="vt-msg">${icon("alert")}<span>${esc(this.msg)}</span></div>`);
      if (this.notice && !st.ended && st.step !== "done") {
        parts.push(`<div class="vt-msg vt-notice">${icon("alert")}<span>${esc(this.notice)}</span></div>`);
      }
      if (this.busy) parts.push(`<div class="vt-busy"><span class="vt-spin"></span>${esc(this.busy)}</div>`);
      return parts.join("");
    }

    // ---------------- 流程條：每步完成後在下方顯示結果 ----------------

    flowSteps() {
      // 返回五步的 {state: done / cur / todo / skip, sub: 步驟下方的小字, tone: 分數顏色}
      const st = this.st, n = st.sentences.length;
      const words = st.sentences.reduce((k, s) => k + s.tokens.length, 0);
      const finished = st.ended || st.step === "done";
      const cur = finished ? 5 : STEP_NO[st.step];
      const p = st.practice;
      const s1 = st.summary1, s2 = st.summary2;
      const steps = FLOW.map((name, i) => ({ name, state: i < cur ? "done" : i === cur ? "cur" : "todo", sub: "", tone: "" }));

      steps[0].sub = `${words} words · ${st.level}`;
      steps[1].sub = steps[1].state === "done" ? "Done" : "Listen first";
      if (s1) { steps[2].sub = `Score ${fmt(s1.score)}`; steps[2].tone = cls(s1.score); }
      else if (steps[2].state === "cur") {
        const recorded = st.read1.filter(Boolean).length + Object.keys(this.uploads).length;
        steps[2].sub = `${Math.min(recorded, n)} / ${n} sentences`;
      }
      if (st.step === "choice" || (s1 && !p)) {
        steps[3].sub = "All passed";
        steps[3].tone = "vt-good";
        steps[3].state = st.step === "choice" && !finished ? "cur" : "done";
      } else if (p) {
        const unit = p.mode === "words" ? "word" : "sentence";
        steps[3].sub = steps[3].state === "cur" && !this.showResults
          ? `${Math.min(p.item + 1, p.items.length)} / ${p.items.length} ${unit}s`
          : plural(p.items.length, unit);
      }
      if (s2) {
        const d = Math.round(s2.score - s1.score);
        steps[4].sub = `Score ${fmt(s2.score)} (${signed(d)})`;
        steps[4].tone = cls(s2.score);
      } else if (steps[4].state === "cur") {
        const recorded = st.read2.filter(Boolean).length + Object.keys(this.uploads).length;
        steps[4].sub = `${Math.min(recorded, n)} / ${n} sentences`;
      } else if (finished && s1) {
        steps[4].state = "skip";
        steps[4].sub = "Not needed";
      }
      if (st.ended && st.step !== "done") {
        // 中途結束的課：當前步驟之後都算沒做
        const at = STEP_NO[st.step];
        steps.forEach((s, i) => { if (i >= at && !s.sub.startsWith("Score")) { s.state = "skip"; s.sub = i === at ? "Stopped" : ""; } });
      }
      return steps;
    }

    flowHtml() {
      // 頂部流程條：圓點 + 連線，連線填到目前這一步
      const steps = this.flowSteps();
      const last = steps.reduce((k, s, i) => (s.state === "done" || s.state === "cur" ? i : k), 0);
      const pct = (last / (steps.length - 1)) * 100;
      return `<div class="vt-flow" style="--vt-fill:${pct}%"><div class="vt-flow-track"><div></div></div>` +
        steps.map((s, i) => `<div class="vt-flow-step ${s.state}">` +
          `<span class="dot">${s.state === "done" ? icon("check") : i + 1}</span>` +
          `<span class="name">${s.name}</span><span class="sub ${s.tone}">${esc(s.sub)}</span></div>`).join("") +
        `</div>`;
    }

    metaHtml() {
      // 流程條下面的小標籤：第幾篇、文章難度、練習重點
      const st = this.st;
      const chips = [];
      if (st.round > 1) chips.push(`Passage ${st.round}`);
      chips.push(`Level <b>${esc(st.level)}</b>`);
      chips.push(`Focus <b>${esc(st.stage_label)}</b>`);
      return `<div class="vt-meta">${chips.map((c) => `<span class="vt-chip">${c}</span>`).join("")}</div>`;
    }

    // ---------------- 通用元件 ----------------

    passageHtml({ results = null, cur = -1, dim = null, clickable = false, badges = false } = {}) {
      const st = this.st;
      const focus = new Set(st.focus_words.map(norm));
      const sents = st.sentences.map((s, i) => {
        const r = results && results[i];
        const words = s.tokens.map((t, k) => {
          const w = r && r.words[k];
          const c = [w ? w.color : "", focus.has(norm(t)) ? "focus" : ""].join(" ").trim();
          return `<span class="vt-w ${c}" title="${w ? esc(wordTip(w)) : ""}">${esc(t)}</span>`;
        }).join(" ");
        const badge = badges && r ? `<span class="vt-badge ${cls(r.score)}">${fmt(r.score)}</span>` : "";
        const classes = ["vt-sent", i === cur ? "cur" : "", dim && dim(i) ? "dim" : "", clickable ? "clickable" : ""];
        return `<span class="${classes.join(" ")}" data-i="${i}" ${clickable ? `data-act="sentence" data-arg="${i}" title="Click to listen"` : ""}>` +
          `${words}${badge}</span>`;
      });
      return `<div class="vt-passage"><h3>${esc(st.title)}</h3>${sents.join(" ")}</div>`;
    }

    // 按鈕：點擊時呼叫 action(act, arg)
    btn(act, label, { ic = "", kind = "", disabled = false, title = "", arg = null } = {}) {
      return `<button class="vt-btn ${kind}" data-act="${act}" ${arg != null ? `data-arg="${esc(arg)}"` : ""} ` +
        `${disabled ? "disabled" : ""} ${title ? `title="${esc(title)}"` : ""}>${ic ? icon(ic) : ""}<span>${label}</span></button>`;
    }

    // 開關（目前只有「Slow」）
    switchHtml(act, label, on, title = "") {
      return `<span class="vt-switch ${on ? "on" : ""}" data-act="${act}" role="switch" aria-checked="${on}" ` +
        `${title ? `title="${esc(title)}"` : ""}><span class="knob"></span>${label}</span>`;
    }

    recButton() {
      const on = this.recording;
      return this.btn("rec", on ? "Stop" : "Record", { ic: on ? "stop" : "mic", kind: `rec ${on ? "on" : ""}`,
        disabled: !on && !this.canRecord() });
    }

    waveHtml() {
      return this.recording ? `<canvas id="vt-pron-wave" class="vt-wave"></canvas>` : "";
    }

    recHint() {
      const c = this.st.config;
      return `<div class="vt-hint"><kbd>Space</kbd> start / stop · auto-stops after ${c.auto_stop_ms / 1000}s of silence · ` +
        `max ${c.max_seconds}s</div>`;
    }

    // 老師的回饋框（③ 結果、④ 每次評分後）；loading 時顯示 Thinking…
    feedbackHtml(text, loading = false) {
      if (!text && !loading) return "";
      return `<div class="vt-feedback ${loading ? "loading" : ""}"><div class="who">Teacher</div>` +
        `<div class="say">${loading ? '<span class="vt-spin"></span>Thinking…' : esc(text)}</div></div>`;
    }

    scoresHtml(summary, before = null, spark = false) {
      // spark：卡片右側畫這一項最近幾次第一次讀全文的小走勢圖（結果頁用）
      return `<div class="vt-scores">${SCORE_KEYS.filter(([k]) => summary[k] != null).map(([k, label, note]) => {
        const d = before && before[k] != null ? Math.round(summary[k] - before[k]) : null;
        const delta = d == null ? "" : `<span class="d ${deltaCls(d)}">${signed(d)}</span>`;
        // 標題一行（說明是同一行的淺色字，放不下時省略、滑鼠懸停看全文）；下一行左邊分數、右邊小走勢圖
        return `<div class="vt-score"><div class="k" title="${label}${note ? ` · ${note}` : ""}">${label}` +
          `${note ? `<span class="note"> · ${note}</span>` : ""}</div>` +
          `<div class="row"><div class="v ${cls(summary[k])}">${fmt(summary[k])}${delta}</div>` +
          `${spark ? this.sparkHtml(k, label) : ""}</div></div>`;
      }).join("")}</div>`;
    }

    sparkHtml(key, label) {
      // 某一項分數最近幾次第一次讀全文的小走勢圖；少於兩次不畫
      const vals = this.st.trend.map((p) => p[key]).filter((v) => v != null);
      if (vals.length < 2) return "";
      const W = 56, H = 28, P = 3;
      let lo = Math.min(...vals), hi = Math.max(...vals);
      if (hi - lo < 10) { const mid = (hi + lo) / 2; lo = mid - 5; hi = mid + 5; }   // 變化很小時不要放大成大起大落
      const x = (i) => P + (i * (W - 2 * P)) / (vals.length - 1);
      const y = (v) => H - P - ((v - lo) * (H - 2 * P)) / (hi - lo);
      const pts = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
      const [lx, ly] = pts[pts.length - 1].split(",");
      const tip = `${label}, last ${vals.length} first readings: ${vals.map((v) => Math.round(v)).join(" → ")}`;
      return `<svg class="vt-spark" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(tip)}"><title>${esc(tip)}</title>` +
        `<polyline points="${pts.join(" ")}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>` +
        `<circle cx="${lx}" cy="${ly}" r="2.6" class="last"/></svg>`;
    }

    // 字的顏色說明
    legendHtml() {
      return `<div class="vt-legend"><span><i class="vt-dot vt-good"></i>Good</span><span><i class="vt-dot vt-fair"></i>Almost</span>` +
        `<span><i class="vt-dot vt-bad"></i>Needs practice</span><span class="vt-hint">Hover a word for details</span></div>`;
    }

    // ---------------- 各步驟 ----------------

    // ② 示範
    demoHtml() {
      return `<div class="vt-title">Listen to the model reading</div>` +
        this.passageHtml({ cur: this.playing, clickable: true }) +
        `<div class="vt-hint">Click any sentence to hear it on its own. Start reading when you're ready.</div>` +
        `<div class="vt-actions">` +
        this.btn("demo", this.playing >= 0 ? "Stop" : "Play all", { ic: this.playing >= 0 ? "stop" : "play" }) +
        this.switchHtml("slow", "Slow", this.slow) +
        `<span class="vt-spacer"></span>` +
        this.btn("startReading", "Start reading", { ic: "next", kind: "primary", disabled: !!this.busy }) + `</div>`;
    }

    // ③ / ⑤ 讀全文：中途不顯示分數，讀完一起顯示
    readHtml() {
      const st = this.st, n = st.sentences.length;
      const pending = Object.keys(this.uploads).length;
      const recorded = (i) => !!st[st.step][i] || !!this.uploads[i];
      const title = st.step === "read1" ? "Read the passage aloud" : "Read it again";
      const hint = st.step === "read1" ? "One sentence at a time. Your results appear after the last sentence."
        : "Read the whole passage once more to see your progress.";
      const progress = this.readIdx < n ? `Sentence ${this.readIdx + 1} of ${n}`
        : pending ? `Scoring… (${pending} left)` : "All sentences recorded";
      return `<div class="vt-title">${title}<span class="vt-count">${progress}</span></div>` +
        this.passageHtml({ cur: this.readIdx, dim: (i) => recorded(i) && i !== this.readIdx }) +
        `<div class="vt-hint">${hint}</div>` + this.waveHtml() +
        `<div class="vt-actions">${this.recButton()}` +
        this.btn("redo", this.recording ? "Redo" : "Redo previous", { ic: "redo", disabled: !this.recording && this.readIdx === 0,
          title: this.recording ? "Discard and record again" : "Go back and re-record the previous sentence" }) +
        `</div>` + this.recHint();
    }

    // ③ 結果（④ 開始前）或全部過關時的選擇
    resultsHtml() {
      const st = this.st;
      const choice = st.step === "choice";
      const p = st.practice;
      const plan = choice ? "Every sentence and word passed — great job!"
        : p.mode === "sentences" ? `Next: practise your ${plural(p.items.length, "lowest-scoring sentence")}.`
        : `All sentences passed. Next: practise ${plural(p.items.length, "word")} that need work.`;
      const btns = choice
        ? this.btn("end", "Finish lesson", { ic: "flag" }) + this.btn("harder", "Try a harder passage", { ic: "up", kind: "primary" })
        : this.btn("startPractice", "Start practice", { ic: "next", kind: "primary" });
      return `<div class="vt-title">Your reading</div>` + this.scoresHtml(st.summary1) +
        this.passageHtml({ results: st.read1, badges: true }) + this.legendHtml() +
        this.feedbackHtml(choice ? st.feedback.choice : st.feedback.read1) +
        `<div class="vt-actions"><span class="vt-plan">${plan}</span><span class="vt-spacer"></span>${btns}</div>`;
    }

    // ④ 練習
    practiceHtml() {
      const st = this.st, p = st.practice, t = p.target;
      if (!t) return `<div class="vt-pron-loading">Practice complete</div>`;
      const item = p.items[p.item];
      const where = p.mode === "sentences"
        ? `Sentence ${p.item + 1} of ${p.items.length} · ${t.kind === "word" ? `Word ${p.word + 1} of ${item.words.length}` : "Whole sentence"}`
        : `Word ${p.item + 1} of ${p.items.length}`;
      const last = p.last;
      let body = esc(t.text);
      let detail = "";
      let status = "";
      if (last && last.result) {
        const r = last.result;
        if (t.kind === "sentence") {
          body = r.words.map((w) => `<span class="vt-w ${w.color}" title="${esc(wordTip(w))}">${esc(w.text)}</span>`).join(" ");
        } else if (r.words[0]) {
          const w = r.words[0];
          body = `<span class="vt-w ${w.color}">${esc(t.text)}</span>`;
          detail = `<div class="vt-phonemes">${w.phonemes.map((ph) =>
            `<span class="${cls(ph.accuracy)}">/${esc(ph.phoneme)}/<b>${fmt(ph.accuracy)}</b></span>`).join("")}</div>`;
        }
        if (!last.counted && !p.finished) status = `<span class="vt-fair">We couldn't hear the whole ${t.kind} — this try doesn't count. Please record again, closer to the mic.</span>`;
        else if (p.passed) status = `<span class="vt-good">${icon("check")} Passed · ${fmt(r.score)}</span>`;
        else if (p.finished) status = `<span class="${cls(r.score)}">${fmt(r.score)}</span> — let's move on.`;
        else status = `<span class="${cls(r.score)}">${fmt(r.score)}</span> · target ${st.config.pass_score}. Listen again and retry.`;
      }
      const attempt = p.finished ? "Completed"
        : `Attempt ${Math.min(p.tries + 1, st.config.max_attempts)} of ${st.config.max_attempts}`;
      const dots = Array.from({ length: st.config.max_attempts }, (_, i) =>
        `<i class="${i < p.tries ? (p.passed && i === p.tries - 1 ? "pass" : "used") : ""}"></i>`).join("");
      const card = `<div class="vt-target"><div class="label"><span>${where}</span>` +
        `<span class="vt-tries">${dots}${attempt}</span></div>` +
        `<div class="text ${t.kind}">${body}</div>${detail}${status ? `<div class="vt-status-line">${status}</div>` : ""}</div>`;
      const fb = last ? this.feedbackHtml(last.feedback, !last.feedback && this.feedbackFor === last.attempt) : "";
      const lastInItem = t.kind === "sentence" || (!item.whole && p.word + 1 >= item.words.length);
      const nextLabel = p.item + 1 >= p.items.length && lastInItem ? "Finish practice" : "Next";
      return `<div class="vt-title">Practice</div>` +
        this.passageHtml({ results: st.read1, cur: t.sentence }) + card + fb + this.waveHtml() +
        `<div class="vt-actions">` +
        this.btn("listen", "Listen", { ic: "volume", disabled: this.recording }) + this.switchHtml("slow", "Slow", this.slow) +
        this.recButton() +
        this.btn("redo", "Redo", { ic: "redo", disabled: !this.recording, title: "Discard this recording and start again (doesn't count)" }) +
        `<span class="vt-spacer"></span>` +
        this.btn("next", nextLabel, { ic: "next", kind: "primary", disabled: !(p.finished && !this.recording && !this.busy) }) +
        `</div>` + this.recHint();
    }

    // 結果頁（⑤ 之後，或全部過關後結束；也用於回看歷史課堂）
    doneHtml() {
      const st = this.st;
      if (!st.summary1) {
        return this.passageHtml({ clickable: true }) + `<div class="vt-hint">This lesson ended before the reading step.</div>`;
      }
      const out = [];
      const sparkHint = st.trend.length >= 2
        ? `<div class="vt-hint">Mini charts: your last ${st.trend.length} first readings — for tracking progress, not a CEFR level.</div>` : "";
      if (st.summary2) {
        out.push(`<div class="vt-title">Second reading <span class="vt-count">compared with your first</span></div>`,
          this.scoresHtml(st.summary2, st.summary1, true), sparkHint,
          this.passageHtml({ results: st.read2, badges: true, clickable: true, cur: this.donePlaying() }), this.legendHtml(),
          this.recordingsHtml(), this.compareHtml());
      } else {
        out.push(`<div class="vt-title">Your reading</div>`, this.scoresHtml(st.summary1, null, true), sparkHint,
          this.passageHtml({ results: st.read1, badges: true, clickable: true, cur: this.donePlaying() }), this.legendHtml(),
          this.recordingsHtml(), this.soundsHtml());
      }
      return out.join("");
    }

    donePlaying() {
      // 結果頁正在唸的句子：播放錄音 / 老師示範時的 playingSentence，或點句子聽示範時的 playing
      return this.playingSentence >= 0 ? this.playingSentence : this.playing;
    }

    chatHtml() {
      // 看完分數後問老師問題（老師帶著這節課的資料用中文回答）
      const msgs = [...this.st.chat];
      if (this.chatPending) msgs.push({ role: "user", content: this.chatPending });
      const bubbles = msgs.map((m) => `<div class="vt-bubble ${m.role === "user" ? "me" : "teacher"}">` +
        `${m.role === "user" ? "" : '<div class="who">Teacher</div>'}<div class="say">${esc(m.content)}</div></div>`);
      if (this.chatPending) {
        bubbles.push(`<div class="vt-bubble teacher"><div class="who">Teacher</div><div class="say vt-thinking"><span class="vt-spin"></span>Thinking…</div></div>`);
      }
      const log = bubbles.length ? bubbles.join("")
        : `<div class="vt-chat-empty">Questions about your scores? Ask me — for example why a word is red, or how to practise a sound.</div>`;
      const pop = !this.chatOpen ? "" :
        `<div class="vt-chat-pop" role="dialog" aria-label="Ask your teacher">` +
        `<div class="vt-chat-head">Ask your teacher</div>` +
        `<div class="vt-chat-log" id="vt-chat-log">${log}</div>` +
        (this.chatError ? `<div class="vt-msg">${icon("alert")}<span>${esc(this.chatError)}</span></div>` : "") +
        `<div class="vt-chat-input"><textarea id="vt-chat-input" rows="1" maxlength="500" ` +
        `placeholder="Ask about your scores or pronunciation…">${esc(this.chatDraft)}</textarea>` +
        this.btn("chatSend", "", { ic: "send", kind: "primary icon-only", disabled: !!this.chatPending, title: "Send (Enter)" }) +
        `</div></div>`;
      const fab = `<button class="vt-chat-fab ${this.chatOpen ? "open" : ""}" data-act="chatToggle" ` +
        `title="${this.chatOpen ? "Close chat" : "Ask your teacher"}">${icon(this.chatOpen ? "close" : "chat")}</button>`;
      return `<div class="vt-pron vt-pron-float">${pop}${fab}</div>`;
    }

    recordingsHtml() {
      // 老師的示範和學生兩次讀全文的錄音，可以播放、點進度條跳轉
      const st = this.st;
      const cards = [["model", "Teacher's reading", null], ["read1", "First reading", st.summary1],
                     ["read2", "Second reading", st.summary2]]
        .filter(([step, , summary]) => step === "model" || (summary && st.audio[step]))
        .map(([step, label, summary]) => {
          const on = this.playingRec === step;
          const meta = summary ? `Score <b class="${cls(summary.score)}">${fmt(summary.score)}</b>` : "Model";
          // 老師示範用喇叭圖示（和逐句對比表的 Teacher 欄一致），學生錄音用播放圖示
          const idle = step === "model" ? "volume" : "play";
          return `<div class="vt-rec-card ${on ? "on" : ""}">` +
            `<button class="vt-btn icon round" data-act="playRec" data-arg="${step}" title="${on ? "Stop" : "Play"}">${icon(on ? "stop" : idle)}</button>` +
            `<div class="info"><div class="row"><span class="label">${label}</span>` +
            `<span class="time" id="vt-rec-time-${step}">${on ? "Loading…" : meta}</span></div>` +
            `<div class="vt-rec-track" data-key="${step}" title="Drag to seek"><div id="vt-rec-bar-${step}"></div></div></div></div>`;
        });
      return `<div class="vt-subtitle">Listen &amp; compare</div><div class="vt-recs">${cards.join("")}</div>` +
        `<div class="vt-hint">While a recording is playing, click any sentence in the passage to jump to it.</div>`;
    }

    recCell(step, i, score) {
      // 表格裡的單句分數 + 重聽這一句的小按鈕
      const key = `${step}:${i}`, on = this.playingRec === key;
      const btn = this.st.audio[step]
        ? `<button class="vt-btn icon mini ${on ? "on" : ""}" data-act="playRec" data-arg="${key}" title="${on ? "Stop" : "Listen to this take"}">${icon(on ? "stop" : "play")}</button>`
        : "";
      return `<td class="num"><span class="vt-cell">${btn}<span class="${cls(score)}">${fmt(score)}</span></span></td>`;
    }

    compareHtml() {
      // 結果頁：逐句比較表（老師 / 第一次 / 第二次 / 變化）、第一次唸錯的字和音
      const st = this.st;
      const rows = st.sentences.map((s, i) => {
        const a = st.read1[i], b = st.read2[i];
        if (!a || !b) return "";
        const d = Math.round(b.score - a.score);
        // 每一列：句子 → 老師 / 第一次 / 第二次 三個播放鍵排在一起，方便依序點擊對比
        const key = `model:${i}`, on = this.playingRec === key;
        const model = `<button class="vt-btn icon mini ${on ? "on" : ""}" data-act="playRec" data-arg="${key}" ` +
          `title="${on ? "Stop" : "Listen to the teacher"}">${icon(on ? "stop" : "volume")}</button>`;
        return `<tr><td class="idx">${i + 1}</td><td class="text">${esc(s.text)}</td><td class="num tch">${model}</td>` +
          this.recCell("read1", i, a.score) + this.recCell("read2", i, b.score) +
          `<td class="num"><span class="vt-delta ${deltaCls(d)}">${signed(d)}</span></td></tr>`;
      }).join("");
      // 第一次紅色的字，第二次怎麼樣
      const fixed = [];
      st.sentences.forEach((_, i) => {
        (st.read1[i] ? st.read1[i].words : []).forEach((w, k) => {
          const w2 = st.read2[i] && st.read2[i].words[k];
          if (w.color === "bad" && w2) {
            fixed.push(`<span class="vt-chip"><span class="vt-bad">${esc(stripPunct(w.text))}</span>` +
              `${icon("next")}<span class="vt-${w2.color}">${w2.color === "good" ? "fixed" : w2.color === "fair" ? "better" : "still red"}</span></span>`);
          }
        });
      });
      return `<div class="vt-subtitle">Sentence by sentence</div>` +
        `<div class="vt-table"><table class="vt-compare"><thead><tr><th class="idx">#</th><th>Sentence</th>` +
        `<th class="num tch">Teacher</th><th class="num">1st</th><th class="num">2nd</th><th class="num">Change</th></tr></thead><tbody>${rows}</tbody></table></div>` +
        (fixed.length ? `<div class="vt-subtitle">Words you missed the first time</div><div class="vt-meta">${fixed.join("")}</div>` : "") +
        this.soundsHtml();
    }

    soundsHtml() {
      // 第一次讀全文唸得不好的音（音素分數 < FAIR），按音合併：出錯次數、出錯的字，
      // 有第二次讀全文時，對照同一個字同一個位置的音的平均分，標出 fixed / better / still weak
      const st = this.st;
      const sounds = new Map();
      st.sentences.forEach((_, i) => {
        (st.read1[i] ? st.read1[i].words : []).forEach((w, k) => {
          w.phonemes.forEach((ph, j) => {
            if (!ph.phoneme || ph.accuracy >= FAIR) return;
            const e = sounds.get(ph.phoneme) || { phoneme: ph.phoneme, n: 0, words: [], first: [], second: [] };
            e.n += 1;
            e.first.push(ph.accuracy);
            const word = stripPunct(w.text).toLowerCase();
            if (!e.words.includes(word)) e.words.push(word);
            const w2 = st.read2[i] && st.read2[i].words[k];
            const ph2 = w2 && w2.phonemes[j];
            if (ph2 && ph2.phoneme === ph.phoneme) e.second.push(ph2.accuracy);
            sounds.set(ph.phoneme, e);
          });
        });
      });
      if (!sounds.size) return "";
      const avg = (a) => a.reduce((x, y) => x + y, 0) / a.length;
      const list = [...sounds.values()].sort((a, b) => b.n - a.n || avg(a.first) - avg(b.first)).slice(0, 8);
      const chips = list.map((e) => {
        const a1 = Math.round(avg(e.first));
        const a2 = e.second.length ? Math.round(avg(e.second)) : null;
        const status = a2 == null ? ""
          : `${icon("next")}<span class="${cls(a2)}">${a2 >= GOOD ? "fixed" : a2 >= FAIR ? "better" : "still weak"}</span>`;
        const tip = `/${e.phoneme}/ missed ${e.n}× (avg ${a1})${a2 != null ? `, second reading avg ${a2}` : ""} — in: ${e.words.join(", ")}`;
        return `<span class="vt-chip vt-sound" title="${esc(tip)}"><b class="vt-bad">/${esc(e.phoneme)}/</b>` +
          `${e.n > 1 ? `<span class="n">×${e.n}</span>` : ""}<span class="ex">${esc(e.words.slice(0, 3).join(", "))}</span>${status}</span>`;
      });
      const title = st.summary2 ? "Sounds you missed the first time" : "Sounds to work on";
      return `<div class="vt-subtitle">${title}</div><div class="vt-meta">${chips.join("")}</div>`;
    }
  }

  function bindClicks(el) {
    // 按鈕點擊統一交給 panel.action（data-act / data-arg）
    if (el._vtBound) return;
    el._vtBound = true;
    el.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-act]");
      if (btn && !btn.disabled && panel) panel.action(btn.dataset.act, btn.dataset.arg);
    });
    el.addEventListener("pointerdown", (e) => {
      const track = e.target.closest(".vt-rec-track");
      if (track && panel) panel.dragSeek(track, e);
    });
  }

  function syncChatVisibility() {
    // 發音課面板被隱藏（切到其他課）時，右下角的聊天也一起藏起來
    const box = document.getElementById("vt-pron-chat");
    const root = document.getElementById("vt-pron");
    if (box) box.style.display = root && root.offsetParent !== null ? "" : "none";
  }

  function fitInput(el) {
    // 聊天輸入框隨內容長高（最多約 5 行）
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 130)}px`;
  }

  function stripPunct(t) {
    // 去掉字前後的標點（顯示單字用）
    return String(t).replace(/^[^\w']+|[^\w']+$/g, "");
  }

  function wordTip(w) {
    // 滑鼠停在字上時的說明：準確度、唸錯 / 漏唸、弱的音、停頓問題
    const err = { Omission: "Missed", Mispronunciation: "Mispronounced" }[w.error] || "";
    const weak = w.phonemes.filter((p) => p.accuracy < FAIR).map((p) => `/${p.phoneme}/ ${Math.round(p.accuracy)}`);
    const prosody = { UnexpectedBreak: "Unexpected pause", MissingBreak: "Missing pause", Monotone: "Flat intonation" };
    const pr = (w.prosody_errors || []).map((e) => prosody[e] || e);
    return [`Accuracy ${Math.round(w.accuracy)}`, err, weak.length ? `Weak sounds: ${weak.join(" ")}` : "", pr.join(", ")]
      .filter(Boolean).join(" · ");
  }

  // ---------------- 全局 ----------------

  let panel = null;

  // Gradio 重繪組件時元素會被替換，定時檢查，元素換了就重新渲染
  setInterval(() => {
    const root = document.getElementById("vt-pron");
    if (root && panel && root.dataset.v !== `${panel.sid}:${panel.version}`) panel.render();
    syncChatVisibility();
  }, 300);

  document.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || e.repeat || !panel) return;
    const root = document.getElementById("vt-pron");
    if (!root || root.offsetParent === null) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    if (!panel.recording && !panel.canRecord()) return;
    e.preventDefault();
    panel.toggleRecord();
  });

  window.vtPron = {
    // value 形如 "課堂id:隨機數" 或空字符串
    sync(value) {
      const sid = String(value || "").split(":")[0].trim();
      if (panel && panel.sid === sid && !panel.closed) return;
      if (panel) panel.close();
      panel = null;
      const root = document.getElementById("vt-pron");
      if (root) {
        root.dataset.v = "";
        root.innerHTML = `<div class="vt-pron-loading"><span class="vt-spin"></span>Your teacher is writing a passage…</div>`;
      }
      if (sid) {
        panel = new Panel(sid);
        panel.load();
      }
    },
  };
})();
