// 語音課的瀏覽器端：麥克風收音、播放老師語音、顯示 Listening / Speaking；課後播放課堂錄音。協議見 voice.py 開頭。
// app.py 把這個文件注入頁面；隱藏輸入框 #vt-voice-ctl 的值變化時調用 vtVoice.sync(值)：
// 值以課堂 id 開頭就連接這節語音課（已連接則不動，斷線則重連），空字符串就掛斷。
// 文件分區：上課（Call：收音、播放、和服務端通信）→ Listening / Speaking 顯示 → 課後錄音卡片 → vtVoice
(() => {
  const MIC_RATE = 16000;      // 發給服務端的麥克風採樣率
  const TTS_RATE = 24000;      // 服務端發來的老師語音採樣率
  const MIC_CHUNK = 1600;      // 每 100ms 發一次

  const TAP_WORKLET = `
    class VtTap extends AudioWorkletProcessor {
      process(inputs) {
        const ch = inputs[0] && inputs[0][0];
        if (ch) this.port.postMessage(ch.slice(0));
        return true;
      }
    }
    registerProcessor("vt-tap", VtTap);`;

  class Call {
    constructor(sid) {
      this.sid = sid;
      this.listening = false;
      this.playing = 0;          // 正在播放的音頻片段數
      this.playAt = 0;           // 下一段老師語音的開始時刻
      this.pending = {};         // 老師語音 id → 還沒播完的片段數
      this.ended = {};           // 老師語音 id → 服務端已發完
      this.current = null;       // 正在接收的老師語音 id
      this.micBuf = new Int16Array(MIC_CHUNK);
      this.micLen = 0;
      this.micPos = 0;           // 降採樣的小數位置
      this.note = "";            // 顯示在 Listening / Speaking 下方的提示（出錯、斷線）
      this.closed = false;       // 本地主動掛斷
      this.failed = false;       // 麥克風或音頻初始化失敗
      this.done = false;         // 服務端通知正常下課
    }

    async start() {
      try {
        this.ctx = new AudioContext();
        await this.ctx.resume();
        this.mic = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
        });
      } catch (e) {
        console.error("[vtVoice] microphone", e);
        this.failed = true;
        this.note = "Microphone is not available. Please allow microphone access and reopen this lesson.";
        return;
      }
      if (this.closed) return this.cleanup();
      try {
        const url = URL.createObjectURL(new Blob([TAP_WORKLET], { type: "application/javascript" }));
        await this.ctx.audioWorklet.addModule(url);
        const source = this.ctx.createMediaStreamSource(this.mic);
        this.tap = new AudioWorkletNode(this.ctx, "vt-tap");
        this.tap.port.onmessage = (e) => this.onMic(e.data);
        const mute = this.ctx.createGain();
        mute.gain.value = 0;     // 接到輸出端才會被處理，但不能讓學生聽到自己的聲音
        source.connect(this.tap).connect(mute).connect(this.ctx.destination);
      } catch (e) {
        console.error("[vtVoice] audio setup", e);
        this.failed = true;
        this.note = "This browser cannot record audio here. Please use a recent Chrome or Edge.";
        return this.cleanup();
      }
      if (this.closed) return this.cleanup();

      const proto = location.protocol === "https:" ? "wss" : "ws";
      const base = location.pathname.replace(/\/$/, "");
      this.ws = new WebSocket(`${proto}://${location.host}${base}/voice/${this.sid}`);
      this.ws.binaryType = "arraybuffer";
      this.ws.onmessage = (e) => this.onServer(e.data);
      this.ws.onclose = () => {
        if (!this.closed && !this.done) this.note = "Voice connection lost. Click this lesson in Past Lessons to reconnect.";
        this.listening = false;
      };
    }

    // ---------- 學生說話 ----------

    onMic(samples) {
      if (!this.listening || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      // 降採樣到 16kHz：每個輸出點取對應區間的平均值
      const ratio = this.ctx.sampleRate / MIC_RATE;
      let pos = this.micPos;
      while (pos + ratio <= samples.length) {
        let sum = 0, n = 0;
        for (let i = Math.floor(pos); i < Math.floor(pos + ratio); i++) { sum += samples[i]; n++; }
        const v = Math.max(-1, Math.min(1, n ? sum / n : 0));
        this.micBuf[this.micLen++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        if (this.micLen === MIC_CHUNK) {
          this.ws.send(this.micBuf.slice(0).buffer);
          this.micLen = 0;
        }
        pos += ratio;
      }
      this.micPos = pos - samples.length;
    }

    // ---------- 服務端消息 ----------

    onServer(data) {
      if (data instanceof ArrayBuffer) return this.playChunk(data);
      const msg = JSON.parse(data);
      if (msg.type === "listen") {
        this.listening = true;
      } else if (msg.type === "mute") {
        this.listening = false;
        this.micLen = 0;
      } else if (msg.type === "speak") {
        this.current = msg.id;
        this.pending[msg.id] = 0;
      } else if (msg.type === "speak_end") {
        this.ended[msg.id] = true;
        this.checkPlayed(msg.id);
      } else if (msg.type === "note") {
        this.note = msg.text;
      } else if (msg.type === "ended") {
        this.listening = false;
        this.done = true;          // 正常下課，不是斷線
        this.ws.close();
      }
    }

    playChunk(buf) {
      const id = this.current;
      const pcm = new Int16Array(buf);
      if (!pcm.length) return;
      const audio = this.ctx.createBuffer(1, pcm.length, TTS_RATE);
      const ch = audio.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 0x8000;
      const node = this.ctx.createBufferSource();
      node.buffer = audio;
      node.connect(this.ctx.destination);
      const at = Math.max(this.playAt, this.ctx.currentTime + 0.05);
      node.start(at);
      this.playAt = at + audio.duration;
      this.playing++;
      this.pending[id] = (this.pending[id] || 0) + 1;
      node.onended = () => {
        this.playing--;
        this.pending[id]--;
        this.checkPlayed(id);
      };
    }

    checkPlayed(id) {
      // 一條老師語音發完並且播完，才告訴服務端（服務端據此重新收音）
      if (this.ended[id] && !this.pending[id]) {
        delete this.ended[id];
        delete this.pending[id];
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: "played", id }));
        }
      }
    }

    stop() {
      this.closed = true;
      if (this.ws) this.ws.close();
      this.cleanup();
    }

    cleanup() {
      if (this.mic) this.mic.getTracks().forEach((t) => t.stop());
      if (this.ctx) this.ctx.close();
    }

    get state() {
      return this.playing > 0 ? "Speaking" : "Listening";
    }
  }

  let call = null;

  function render() {
    // Gradio 重繪組件時元素會被替換，所以定時按 id 找到元素再寫入（課後錄音卡片也在這裡一起刷新）
    renderRec();
    const state = document.getElementById("vt-voice-state");
    const note = document.getElementById("vt-voice-note");
    if (!state || !call) return;
    const s = call.state;
    if (state.textContent !== s) state.textContent = s;
    state.dataset.state = s.toLowerCase();
    if (note && note.textContent !== call.note) note.textContent = call.note;
  }
  setInterval(render, 150);

  // ---------- 課後的課堂錄音卡片（app.py 的 recording_html()，樣式和發音課結果頁的播放卡片一致） ----------
  // 整段波紋（已播的部分用主題色）、播放 / 暫停、快退 / 快進 5 秒、調速（每次打開都從 1× 開始）；
  // 點擊或拖動波紋跳轉（沒在播時跳轉後直接開始播），滑鼠移上波紋時顯示那個位置的時間。
  // app.py 只給 <div class="vt-player" data-sid data-v data-duration data-peaks>，卡片內容由 cardHtml() 生成；
  // 頁面上同時只有一張卡片，所有卡片共用一個 Audio（rec）。

  const SKIP_SECONDS = 5;
  const SPEEDS = [0.75, 1, 1.25, 1.5];   // 調速鍵依序循環
  const BAR_W = 2, BAR_GAP = 1;          // 波紋豎條的寬度和間距（CSS 像素），條數按卡片寬度決定
  // 線條圖示（Lucide 風格，和 pron.js 的同名圖示一致）
  const ICONS = {
    play: '<polygon points="7 4 19 12 7 20 7 4"/>',
    pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
    back: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
    forward: '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>',
  };
  const icon = (name) => `<svg class="vt-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
  const clock = (sec) => `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;   // 秒 → m:ss

  const rec = new Audio();
  rec.preload = "metadata";
  let recSrc = "";         // rec 目前載入的錄音網址
  let recSeeking = false;  // 正在拖動波紋（這時播放進度不要蓋掉拖動的位置）

  const recCard = () => document.querySelector(".vt-player[data-sid]");
  const recUrl = (el) => `${location.pathname.replace(/\/$/, "")}/voice/${el.dataset.sid}/recording?v=${el.dataset.v}`;
  // 錄音長度：載入後用瀏覽器量到的，載入前用 app.py 給的
  const recDuration = (el) => (isFinite(rec.duration) && rec.duration) || +el.dataset.duration || 0;

  function drawWave(el, frac) {
    // 整段波紋畫在 canvas 上：圓角豎條、上下對稱，錄音多長都剛好放滿一行；
    // 已播放的部分（0–frac）用主題色，其餘用淡灰（文字淺色的 35%）
    const canvas = el.querySelector(".vt-wavebar canvas");
    if (!canvas || !canvas.clientWidth) return;   // 卡片隱藏中，量不到寬度
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    // 豎條的形狀只在卡片尺寸變化時算一次，存在元素上（Gradio 換了元素就重算）
    if (!el._bars || el._barsSize !== `${w}x${h}`) {
      const peaks = (el.dataset.peaks || "").split(",").filter(Boolean).map((p) => +p / 100);
      const n = Math.max(1, Math.floor((w + BAR_GAP) / (BAR_W + BAR_GAP)));
      const bars = new Path2D();
      const step = (w + BAR_GAP) / n;   // 讓最後一根剛好貼齊右邊
      for (let i = 0; i < n; i++) {
        // 這根豎條對應的那幾段波紋取平均，條數比波紋段數少時不會只剩尖峰
        const a = Math.floor((i * peaks.length) / n), b = Math.max(a + 1, Math.floor(((i + 1) * peaks.length) / n));
        let v = 0;
        for (let k = a; k < b; k++) v += peaks[k] || 0;
        v /= b - a;
        const bh = Math.max(BAR_W, v * h);
        bars.roundRect(i * step, (h - bh) / 2, BAR_W, bh, BAR_W / 2);
      }
      el._bars = bars;
      el._barsSize = `${w}x${h}`;
    }
    const style = getComputedStyle(canvas);   // 每次都讀：切換深色模式時顏色跟著變
    const played = style.getPropertyValue("--vt-accent").trim() || "#f97316";
    const rest = style.getPropertyValue("--vt-muted").trim() || "#9ca3af";
    const g = canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    // 先整條畫成淡灰，再只在 0 到播放位置之間畫主題色：
    // 按像素裁切，正在播的那根豎條也會被逐漸填滿，顏色連續地往前推，而不是一根一根地跳
    g.globalAlpha = 0.35;
    g.fillStyle = rest;
    g.fill(el._bars);
    g.globalAlpha = 1;
    if (frac > 0) {
      g.save();
      g.beginPath();
      g.rect(0, 0, frac * w, h);
      g.clip();
      g.fillStyle = played;
      g.fill(el._bars);
      g.restore();
    }
  }

  function cardHtml() {
    // 卡片內容：標題和時間、波紋（canvas + 滑鼠位置的細線）、控制鍵。按鍵的 data-vt-rec 是點擊時的動作
    const btn = (act, ic, title, text = "") =>
      `<button class="vt-btn icon" data-vt-rec="${act}" title="${title}">${ic ? icon(ic) : ""}<span>${text}</span></button>`;
    return `<div class="vt-rec-card">` +
      `<div class="row"><span class="label">Lesson recording</span><span class="time"></span></div>` +
      `<div class="vt-wavebar" title="Click or drag to seek"><canvas></canvas><div class="hover"><span></span></div></div>` +
      `<div class="vt-player-ctl">` +
      btn("back", "back", `Back ${SKIP_SECONDS}s`, SKIP_SECONDS) +
      `<button class="vt-btn icon round" data-vt-rec="toggle"></button>` +
      btn("forward", "forward", `Forward ${SKIP_SECONDS}s`, SKIP_SECONDS) +
      `<span class="vt-spacer"></span>` + btn("speed", "", "Playback speed") + `</div></div>`;
  }

  function renderRec() {
    // 卡片換了（切到別的課）或不見了就停止播放；否則同步播放鍵、波紋進度、時間和速度
    const el = recCard();
    const url = el && recUrl(el);
    if (url !== recSrc && !rec.paused) rec.pause();
    if (!el) return;
    if (el.dataset.built !== url) {   // 第一次看到這張卡片，或 Gradio 重繪後換了元素
      el.innerHTML = cardHtml();
      el.dataset.built = url;
    }
    const mine = url === recSrc;
    const on = mine && !rec.paused;
    const toggle = el.querySelector('[data-vt-rec="toggle"]');
    if (toggle.dataset.on !== String(on)) {
      toggle.dataset.on = String(on);
      toggle.title = on ? "Pause" : "Play";
      toggle.innerHTML = icon(on ? "pause" : "play");
    }
    el.querySelector(".vt-rec-card").classList.toggle("on", on);
    el.querySelector('[data-vt-rec="speed"] span').textContent = `${mine ? rec.playbackRate : 1}×`;
    if (recSeeking) return;
    const dur = recDuration(el), cur = mine ? rec.currentTime : 0;
    showProgress(el, dur ? cur / dur : 0);
  }

  function showProgress(el, frac) {
    // 波紋填色和時間顯示到 frac（0–1）處：播放中是目前位置，拖動中是拖到的位置
    const dur = recDuration(el);
    frac = Math.max(0, Math.min(1, frac));
    drawWave(el, frac);
    el.querySelector(".time").textContent = `${clock(frac * dur)} / ${clock(dur)}`;
  }

  function animateRec() {
    // 播放中每幀刷新，波紋的顏色平滑地往前推（timeupdate 一秒只有約 4 次）
    if (rec.paused) return;
    renderRec();
    requestAnimationFrame(animateRec);
  }
  rec.addEventListener("play", () => requestAnimationFrame(animateRec));

  function loadRec(el) {
    // 換一段錄音時從頭、1× 開始
    const url = recUrl(el);
    if (recSrc === url) return;
    rec.src = url;
    recSrc = url;
    rec.defaultPlaybackRate = rec.playbackRate = 1;
  }

  function seekRec(el, seconds, autoplay) {
    // 跳到第 seconds 秒；還沒載入時等拿到長度再跳
    loadRec(el);
    const go = () => { rec.currentTime = Math.max(0, Math.min(recDuration(el), seconds)); renderRec(); };
    if (rec.readyState >= 1) go();
    else rec.addEventListener("loadedmetadata", go, { once: true });
    if (autoplay) rec.play().catch(() => {});
  }

  ["play", "pause", "timeupdate", "ended", "loadedmetadata", "ratechange"].forEach((ev) => rec.addEventListener(ev, renderRec));

  // 卡片會被 Gradio 和 renderRec() 重繪，事件掛在 document 上，按 data-vt-rec 分派
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".vt-player [data-vt-rec]");
    if (!btn) return;
    const el = btn.closest(".vt-player");
    const act = btn.dataset.vtRec;
    const mine = recUrl(el) === recSrc;
    if (act === "toggle") {
      loadRec(el);
      if (rec.paused) rec.play().catch(() => {});
      else rec.pause();
    } else if (act === "back" || act === "forward") {
      const cur = mine ? rec.currentTime : 0;
      seekRec(el, cur + (act === "back" ? -SKIP_SECONDS : SKIP_SECONDS), false);
    } else if (act === "speed") {
      loadRec(el);
      const i = SPEEDS.indexOf(rec.playbackRate);
      rec.defaultPlaybackRate = rec.playbackRate = SPEEDS[(i + 1) % SPEEDS.length];
    }
  });

  const fracAt = (bar, ev) => {
    const r = bar.getBoundingClientRect();
    return Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
  };

  document.addEventListener("pointermove", (e) => {
    // 滑鼠移上波紋：顯示一條細線和那個位置的時間
    const bar = e.target.closest && e.target.closest(".vt-player .vt-wavebar");
    if (!bar) return;
    const el = bar.closest(".vt-player"), f = fracAt(bar, e);
    bar.style.setProperty("--h", `${f * 100}%`);
    bar.querySelector(".hover span").textContent = clock(f * recDuration(el));
  });

  document.addEventListener("pointerdown", (e) => {
    // 按住波紋拖動：進度跟著移動，放開時跳到那個位置並播放（點一下也一樣）
    const bar = e.target.closest(".vt-player .vt-wavebar");
    if (!bar) return;
    e.preventDefault();
    const el = bar.closest(".vt-player");
    const show = (ev) => showProgress(el, fracAt(bar, ev));
    recSeeking = true;
    show(e);
    const up = (ev) => {
      document.removeEventListener("pointermove", show);
      document.removeEventListener("pointerup", up);
      recSeeking = false;
      seekRec(el, fracAt(bar, ev) * recDuration(el), true);
    };
    document.addEventListener("pointermove", show);
    document.addEventListener("pointerup", up);
  });

  window.vtVoice = {
    // value 形如 "課堂id:隨機數"（隨機數保證每次都觸發）或空字符串
    sync(value) {
      const sid = String(value || "").split(":")[0].trim();
      // 還在請求麥克風（ws 未建立）也算連接中，避免重複開始
      const alive = call && !call.closed && !call.failed && (!call.ws || call.ws.readyState <= WebSocket.OPEN);
      if (call && call.sid === sid && alive) return;
      if (call) call.stop();
      call = null;
      if (sid) {
        call = new Call(sid);
        call.start();
      }
    },
  };
})();
