// «Вуха» через браузер (працює на будь-якій ОС): сторінка пари сама збирає звук учасників
// і віддає його програмі — 16 кГц, моно, 16 біт, шматками по ~0,5 с.
//
// Звідки береться звук:
// - Meet програє голоси учасників через <audio> з потоком WebRTC — підключаємо ці потоки;
// - вхідні доріжки RTCPeerConnection — про всяк випадок теж (і як запасний шлях);
// - програвачі на Web Audio (Zoom у браузері, а часто й сам Meet) — перехоплюємо їхнє підключення до динаміків.
// Справжній Meet буває, що віддає доріжки WebRTC тихими (нулі), а голоси грає через Web Audio, — тож слухаємо
// всі шляхи, а віддаємо той, де справді є звук («смуга»), щоб той самий голос не потрапив двічі.
// Усе це лише слухає: звук, який чуєш ти, не змінюється, а мікрофон не чіпаємо взагалі.
//
// Працює лише на сторінках Meet і Zoom, і лише у вкладці, де програма сама зайшла на пару
// (вона викликає enable()). YouTube чи будь-що інше, навіть у цьому ж браузері, вухам не потрапляє.
(() => {
  const MEETING_HOSTS = /(^|\.)meet\.google\.com$|(^|\.)zoom\.us$|(^|\.)zoom\.com$/;
  if (window.top !== window || window.__autoparaTap || !MEETING_HOSTS.test(location.hostname)) return;
  const RATE = 16000;
  const SEND_SAMPLES = RATE / 2;
  const state = {
    enabled: false, ctx: null, mix: null, tracks: new Map(), webAudio: new Map(), pending: [], pendingLength: 0,
    lane: null, voiced: {}, sent: 0, tab: null,
  };
  window.__autoparaTap = {
    enable() {
      state.enabled = true;
      scan();
      return true;
    },
    // Найнадійніше: звук самої вкладки (як «поділитися вкладкою зі звуком»), — хоч би як сторінка його грала.
    // Браузер програми запущено так, що дозвіл дається без запитання. suppress — не грати в колонки («Без звуку»).
    async captureTab(suppress, again) {
      if (state.tab && state.tab.readyState === "live" && !again) return "уже";
      if (again) {  // сторож вух: звуку давно немає — захоплюємо вкладку наново
        if (state.tab) state.tab.stop();
        if (state.tabCtx) state.tabCtx.close().catch(() => {});
        state.tab = null;
        state.tabCtx = null;
      }
      try {
        const stream = await navigator.mediaDevices.getDisplayMedia({
          video: true, audio: true, preferCurrentTab: true, selfBrowserSurface: "include",
          suppressLocalAudioPlayback: !!suppress, systemAudio: "exclude",
        });
        stream.getVideoTracks().forEach((track) => track.stop());
        const track = stream.getAudioTracks()[0];
        if (!track) return "без звукової доріжки";
        state.tab = track;
        // Окремий контекст одразу на 16 кГц: Chrome сам якісно перетворює звук (з фільтром), і високі
        // частоти не накладаються на мову, як від простого проріджування.
        const ctx = new AudioContext({ sampleRate: RATE });
        if (ctx.state === "suspended") ctx.resume().catch(() => {});
        state.tabCtx = ctx;
        ctx.createMediaStreamSource(new MediaStream([track])).connect(processor(ctx, "tab"));
        return "ок";
      } catch (e) {
        return `не вийшло: ${e.name}`;
      }
    },
    // Розбудити звук сторінки: програвачі, які браузер призупинив, і аудіоконтексти (сторож вух).
    wake() {
      let woken = 0;
      for (const el of document.querySelectorAll("audio, video")) {
        if (el.paused && (el.srcObject || el.src)) { el.play().catch(() => {}); woken += 1; }
        if (el.muted && el.tagName === "AUDIO") { el.muted = false; woken += 1; }
      }
      for (const ctx of [state.ctx, state.tabCtx]) {
        if (ctx && ctx.state === "suspended") { ctx.resume().catch(() => {}); woken += 1; }
      }
      return woken;
    },
    stats() {  // для журналу в autopara -v: звідки береться звук
      const now = Date.now();
      const voiced = Object.fromEntries(Object.entries(state.voiced).map(([lane, at]) => [lane, Math.round((now - at) / 1000)]));
      return { enabled: state.enabled, tab: !!(state.tab && state.tab.readyState === "live"),
               tracks: state.tracks.size, webAudio: state.webAudio.size, lane: state.lane,
               secondsSinceVoice: voiced, sent: state.sent, context: state.ctx && state.ctx.state };
    },
  };

  // Яку смугу віддавати: ту, де звук був щойно; поки вона звучить — не перемикаємось.
  function chosen() {
    if (state.tab && state.tab.readyState === "live") return "tab";  // там уже все, що грає вкладка
    const now = Date.now();
    const recent = (lane) => state.voiced[lane] && now - state.voiced[lane] < 3000;
    if (state.lane && recent(state.lane)) return state.lane;
    for (const lane of Object.keys(state.voiced)) {
      if (recent(lane)) {
        state.lane = lane;
        return lane;
      }
    }
    return state.lane || (state.tracks.size ? "tracks" : null);
  }

  let webLanes = 0;
  function processor(ctx, lane) {
    // Простий фільтр: середнє по вікну перед проріджуванням до 16 кГц прибирає частоти, яких whisper не чує.
    const node = ctx.createScriptProcessor(4096, 1, 1);
    const step = ctx.sampleRate / RATE;
    const down = { phase: 0, sum: 0, count: 0 };
    node.onaudioprocess = (event) => {
      if (!state.enabled) return;
      const input = event.inputBuffer.getChannelData(0);
      let energy = 0;
      for (let i = 0; i < input.length; i += 4) energy += input[i] * input[i];
      if (energy / (input.length / 4) > 1e-7) state.voiced[lane] = Date.now();
      const current = chosen();
      if (current !== null ? current !== lane : lane !== "tracks") return;
      const out = new Int16Array(Math.ceil(input.length / step) + 1);
      let n = 0;
      for (let i = 0; i < input.length; i++) {
        down.sum += input[i];
        down.count += 1;
        down.phase += 1;
        if (down.phase >= step) {
          down.phase -= step;
          const value = Math.max(-1, Math.min(1, down.sum / down.count));
          out[n++] = value < 0 ? value * 0x8000 : value * 0x7fff;
          down.sum = 0;
          down.count = 0;
        }
      }
      push(out.subarray(0, n));
    };
    const silent = ctx.createGain();
    silent.gain.value = 0;  // процесор має бути підключений до виходу, щоб працювати, але чути його не треба
    node.connect(silent);
    connect.call(silent, ctx.destination);
    return node;
  }

  function push(samples) {
    if (!samples.length) return;
    state.pending.push(samples);
    state.pendingLength += samples.length;
    if (state.pendingLength >= SEND_SAMPLES) flush();
  }

  function flush() {
    const all = new Int16Array(state.pendingLength);
    let offset = 0;
    for (const part of state.pending) {
      all.set(part, offset);
      offset += part.length;
    }
    state.pending = [];
    state.pendingLength = 0;
    const bytes = new Uint8Array(all.buffer);
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    state.sent += 1;
    if (typeof window.__autoparaAudio === "function") window.__autoparaAudio(btoa(binary)).catch(() => {});
  }

  function context() {
    if (!state.ctx) {
      state.ctx = new AudioContext();
      state.mix = state.ctx.createGain();
      state.mix.connect(processor(state.ctx, "tracks"));
    }
    if (state.ctx.state === "suspended") state.ctx.resume().catch(() => {});
    return state.ctx;
  }

  function attach(track, keepAlive) {
    if (!track || track.kind !== "audio" || track.readyState === "ended" || state.tracks.has(track.id)) return;
    const stream = new MediaStream([track]);
    const source = context().createMediaStreamSource(stream);
    source.connect(state.mix);
    let element = null;
    if (keepAlive) {
      // Chrome віддає звук доріжки WebRTC у Web Audio, лише якщо її ще й програє якийсь елемент.
      element = new Audio();
      element.muted = true;
      element.srcObject = stream;
      element.play().catch(() => {});
    }
    state.tracks.set(track.id, { source, element });
    track.addEventListener("ended", () => detach(track.id));
  }

  function detach(id) {
    const entry = state.tracks.get(id);
    if (!entry) return;
    entry.source.disconnect();
    if (entry.element) entry.element.srcObject = null;
    state.tracks.delete(id);
  }

  function scan() {
    for (const element of document.querySelectorAll("audio, video")) {
      const stream = element.srcObject;
      if (stream && typeof stream.getAudioTracks === "function") stream.getAudioTracks().forEach((track) => attach(track, false));
    }
    for (const [id, entry] of state.tracks) {
      if (entry.source.mediaStream.getAudioTracks()[0].readyState === "ended") detach(id);
    }
    if (state.ctx && state.ctx.state === "suspended") state.ctx.resume().catch(() => {});
    if (state.pendingLength && !state.tracks.size && !state.webAudio.size) flush();
  }

  // Вхідні доріжки WebRTC.
  const Original = window.RTCPeerConnection;
  if (Original) {
    window.RTCPeerConnection = new Proxy(Original, {
      construct(target, args, newTarget) {
        const connection = Reflect.construct(target, args, newTarget);
        connection.addEventListener("track", (event) => attach(event.track, true));
        return connection;
      },
    });
  }

  // Програвачі на Web Audio: що підключається до динаміків чужого AudioContext, підключаємо ще й до себе.
  const connect = AudioNode.prototype.connect;
  AudioNode.prototype.connect = function (destination, ...rest) {
    const result = connect.call(this, destination, ...rest);
    try {
      if (destination instanceof AudioDestinationNode && this.context !== state.ctx) {
        let tap = state.webAudio.get(this.context);
        if (!tap) {
          tap = processor(this.context, `web${++webLanes}`);
          state.webAudio.set(this.context, tap);
        }
        connect.call(this, tap);
      }
    } catch (e) { /* чужий вузол — не ламаємо сторінку */ }
    return result;
  };

  setInterval(scan, 2000);
})();
