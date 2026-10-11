/* global SawProtocol, SawCues */
/**
 * Scene Companion phone web client.
 * Speaks SAW protocol v1 the same way the Android app does.
 */
(function () {
  "use strict";

  const proto = window.SawProtocol;
  const cues = window.SawCues;

  const WIDTH = 640;
  const HEIGHT = 480;
  const FPS = 8;
  const QUALITY = 0.7;
  const MIC_RATE = 16000;
  const MIC_CHUNK_SAMPLES = MIC_RATE / 10; // 100 ms
  const APP = "saw-web";
  const APP_VER = "0.1.0";

  const $ = function (id) { return document.getElementById(id); };
  const statusEl = $("status");
  const startBtn = $("startStop");
  const listenBtn = $("listen");
  const wsInput = $("wsUrl");
  const transcriptEl = $("transcript");
  const preview = $("preview");

  const state = {
    running: false,
    connected: false,
    connecting: false,
    listenOn: false,
    lastSpoken: "",
    lastSay: "",
    lastAudioEvent: "",
    lastTranscript: "",
    framesSent: 0,
    micChunks: 0,
    acksSent: 0,
    handledTypes: [],
    unknownTypes: [],
    helloAck: false,
    wsUrl: "",
    sendInFlight: false,
    pendingUrgentClip: false,
    pendingClipId: null,
    backoffMs: 1000,
    speaking: false,
  };

  let ws = null;
  let reconnectTimer = null;
  let pingTimer = null;
  let cameraTimer = null;
  let cameraStream = null;
  let micStream = null;
  let micContext = null;
  let micNode = null;
  let audioCtx = null;
  let wakeLock = null;
  let canvas = null;
  let canvasCtx = null;
  let lastFrameAttempt = 0;
  const speech = new SpeechQueue();
  const policy = new cues.CuePolicy(1000);
  const tones = new cues.TonePlayer(function () { return audioCtx; });

  function hook() {
    window.__saw = {
      state: state,
      lastSay: function () { return state.lastSay; },
      lastAudioEvent: function () { return state.lastAudioEvent; },
      framesSent: function () { return state.framesSent; },
      protocol: proto,
      policy: policy,
    };
  }
  hook();

  function loadSettings() {
    try {
      const saved = localStorage.getItem("saw.wsUrl");
      if (saved) wsInput.value = saved;
      state.listenOn = localStorage.getItem("saw.listen") === "1";
    } catch (_e) {
      /* private mode */
    }
    const params = new URLSearchParams(location.search);
    if (params.get("ws")) wsInput.value = params.get("ws");
    renderListen();
  }

  function persistSettings() {
    try {
      localStorage.setItem("saw.wsUrl", wsInput.value.trim());
      localStorage.setItem("saw.listen", state.listenOn ? "1" : "0");
    } catch (_e) {
      /* ignore */
    }
  }

  function setStatus(text, kind, speakIt) {
    statusEl.textContent = titleCase(text);
    statusEl.dataset.kind = kind || "warn";
    if (speakIt && text && text !== state.lastSpoken) {
      speech.enqueueSay(text, true);
    }
    if (text && text !== state.lastSpoken) {
      state.lastSpoken = text;
      announce(text);
    }
  }

  function titleCase(text) {
    if (!text) return "";
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function announce(text) {
    // aria-live already speaks on VoiceOver/TalkBack; this covers engines that miss it.
    statusEl.setAttribute("aria-label", "Connection status, " + text);
  }

  function renderRunning() {
    startBtn.dataset.running = state.running ? "1" : "0";
    startBtn.textContent = state.running ? "Stop" : "Start";
    startBtn.setAttribute(
      "aria-label",
      state.running ? "Stop streaming and disconnect" : "Start streaming the camera to the laptop",
    );
    wsInput.disabled = state.running;
  }

  function renderListen() {
    listenBtn.setAttribute("aria-pressed", state.listenOn ? "true" : "false");
    listenBtn.textContent = state.listenOn ? "Listening" : "Listen";
    listenBtn.setAttribute(
      "aria-label",
      state.listenOn
        ? "Listening. Microphone streaming is on. Turn off to stop sending your voice to the laptop."
        : "Listen. Microphone streaming, off by default. Turn on to send your voice to the laptop.",
    );
  }

  async function unlockAudio() {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    if (!audioCtx) audioCtx = new AC();
    try {
      await audioCtx.resume();
    } catch (_e) {
      /* ignore */
    }
    try {
      const buf = audioCtx.createBuffer(1, 1, audioCtx.sampleRate || 22050);
      const src = audioCtx.createBufferSource();
      src.buffer = buf;
      src.connect(audioCtx.destination);
      src.start(0);
    } catch (_e2) {
      /* ignore */
    }
    if (window.speechSynthesis) {
      try {
        const u = new SpeechSynthesisUtterance(" ");
        u.volume = 0;
        window.speechSynthesis.speak(u);
      } catch (_e3) {
        /* ignore */
      }
    }
    speech.attachContext(audioCtx);
  }

  async function requestWakeLock() {
    try {
      if (navigator.wakeLock && navigator.wakeLock.request) {
        wakeLock = await navigator.wakeLock.request("screen");
        wakeLock.addEventListener("release", function () {
          if (state.running && document.visibilityState === "visible") {
            requestWakeLock();
          }
        });
      }
    } catch (_e) {
      /* iOS older than 16.4, or denied — keep trying on visibility */
    }
  }

  function releaseWakeLock() {
    if (wakeLock) {
      try { wakeLock.release(); } catch (_e) { /* ignore */ }
      wakeLock = null;
    }
  }

  async function startCamera() {
    stopCameraTracks();
    const constraints = {
      audio: false,
      video: {
        facingMode: { ideal: "environment" },
        width: { ideal: WIDTH },
        height: { ideal: HEIGHT },
      },
    };
    cameraStream = await navigator.mediaDevices.getUserMedia(constraints);
    preview.srcObject = cameraStream;
    try { await preview.play(); } catch (_e) { /* autoplay quirks */ }
    if (!canvas) {
      canvas = document.createElement("canvas");
      canvas.width = WIDTH;
      canvas.height = HEIGHT;
      canvasCtx = canvas.getContext("2d", { alpha: false });
    }
    const minInterval = 1000 / FPS;
    if (cameraTimer) clearInterval(cameraTimer);
    cameraTimer = setInterval(function () {
      captureFrame(minInterval);
    }, Math.max(20, minInterval / 2));
  }

  function captureFrame(minInterval) {
    if (!state.running || !state.connected || !cameraStream || !canvasCtx) return;
    const now = proto.nowMs();
    if (now - lastFrameAttempt < minInterval) return;
    if (state.sendInFlight) return;
    if (ws && ws.bufferedAmount > 0) return;
    lastFrameAttempt = now;
    try {
      canvasCtx.drawImage(preview, 0, 0, WIDTH, HEIGHT);
    } catch (_e) {
      return;
    }
    if (typeof canvas.toBlob !== "function") {
      const dataUrl = canvas.toDataURL("image/jpeg", QUALITY);
      const raw = dataUrl.split(",")[1];
      const bin = atob(raw);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      sendJpeg(bytes, now);
      return;
    }
    state.sendInFlight = true;
    canvas.toBlob(function (blob) {
      if (!blob) {
        state.sendInFlight = false;
        return;
      }
      blob.arrayBuffer().then(function (buf) {
        sendJpeg(new Uint8Array(buf), now);
      }).catch(function () {
        state.sendInFlight = false;
      });
    }, "image/jpeg", QUALITY);
  }

  function sendJpeg(bytes, timestampMs) {
    try {
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        state.sendInFlight = false;
        return;
      }
      if (ws.bufferedAmount > 0) {
        state.sendInFlight = false;
        return;
      }
      ws.send(proto.encodeBinary(proto.KIND_JPEG, bytes, timestampMs, 0));
      state.framesSent += 1;
    } catch (_e) {
      /* drop */
    }
    state.sendInFlight = false;
  }

  function stopCameraTracks() {
    if (cameraTimer) {
      clearInterval(cameraTimer);
      cameraTimer = null;
    }
    if (cameraStream) {
      cameraStream.getTracks().forEach(function (t) { t.stop(); });
      cameraStream = null;
    }
    preview.srcObject = null;
  }

  async function startMic() {
    stopMic();
    if (!state.listenOn || !state.running) return;
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        channelCount: 1,
      },
      video: false,
    });
    const AC = window.AudioContext || window.webkitAudioContext;
    micContext = new AC({ sampleRate: MIC_RATE });
    const source = micContext.createMediaStreamSource(micStream);
    const bufferSize = 2048;
    const node = micContext.createScriptProcessor(bufferSize, 1, 1);
    let pending = new Float32Array(0);
    const nativeRate = micContext.sampleRate;
    node.onaudioprocess = function (ev) {
      if (!state.listenOn || state.speaking || !ws || ws.readyState !== WebSocket.OPEN) return;
      const input = ev.inputBuffer.getChannelData(0);
      const resampled = resample(input, nativeRate, MIC_RATE);
      const merged = new Float32Array(pending.length + resampled.length);
      merged.set(pending, 0);
      merged.set(resampled, pending.length);
      let offset = 0;
      while (merged.length - offset >= MIC_CHUNK_SAMPLES) {
        const slice = merged.subarray(offset, offset + MIC_CHUNK_SAMPLES);
        ws.send(proto.encodeBinary(proto.KIND_MIC, floatToPcm16(slice), proto.nowMs(), 0));
        state.micChunks += 1;
        offset += MIC_CHUNK_SAMPLES;
      }
      pending = merged.subarray(offset);
    };
    source.connect(node);
    node.connect(micContext.destination);
    micNode = node;
    try { await micContext.resume(); } catch (_e) { /* ignore */ }
  }

  function stopMic() {
    if (micNode) {
      try { micNode.disconnect(); } catch (_e) { /* ignore */ }
      micNode = null;
    }
    if (micContext) {
      try { micContext.close(); } catch (_e2) { /* ignore */ }
      micContext = null;
    }
    if (micStream) {
      micStream.getTracks().forEach(function (t) { t.stop(); });
      micStream = null;
    }
  }

  function resample(input, fromRate, toRate) {
    if (fromRate === toRate) return input;
    const ratio = fromRate / toRate;
    const outLen = Math.max(1, Math.round(input.length / ratio));
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const src = i * ratio;
      const i0 = Math.floor(src);
      const i1 = Math.min(i0 + 1, input.length - 1);
      const frac = src - i0;
      out[i] = input[i0] * (1 - frac) + input[i1] * frac;
    }
    return out;
  }

  function floatToPcm16(floats) {
    const out = new Uint8Array(floats.length * 2);
    const view = new DataView(out.buffer);
    for (let i = 0; i < floats.length; i++) {
      const s = Math.max(-1, Math.min(1, floats[i]));
      view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return out;
  }

  function sendJson(type, fields) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try {
      ws.send(proto.encodeJson(type, fields));
    } catch (_e) {
      /* ignore */
    }
  }

  function sendSpeaking(active) {
    if (state.speaking === active) return;
    state.speaking = active;
    sendJson(proto.TYPE_SPEAKING, { active: active });
  }

  function reportAudio(id, event, text) {
    state.lastAudioEvent = event;
    const fields = { event: event };
    if (id) fields.id = id;
    if (text) fields.text = text;
    sendJson(proto.TYPE_AUDIO_EVENT, fields);
  }

  function connect() {
    if (!state.running || state.connecting) return;
    state.connecting = true;
    const url = proto.resolveStreamUrl(wsInput.value, location);
    state.wsUrl = url;
    const spoken = state.lastSpoken === "connected" || state.lastSpoken === "connection lost, retrying"
      ? "connection lost, retrying"
      : "connecting";
    setStatus(spoken, "warn", spoken === "connection lost, retrying");
    try {
      ws = new WebSocket(url);
    } catch (err) {
      state.connecting = false;
      scheduleReconnect();
      return;
    }
    ws.binaryType = "arraybuffer";
    ws.onopen = function () {
      state.connecting = false;
      state.connected = true;
      state.backoffMs = 1000;
      state.helloAck = false;
      policy.onConnection(true);
      sendJson(proto.TYPE_HELLO, { role: "phone", app: APP, app_ver: APP_VER });
      sendJson(proto.TYPE_CONFIG, {
        width: WIDTH,
        height: HEIGHT,
        fps: FPS,
        quality: 70,
        mic: state.listenOn,
      });
      setStatus("connected", "good", true);
      try { cues.vibrateOrTone(40); } catch (_e) { /* ignore */ }
      startPing();
    };
    ws.onmessage = function (ev) {
      if (typeof ev.data === "string") {
        handleText(ev.data);
        return;
      }
      handleBinary(ev.data);
    };
    ws.onclose = function () {
      onDrop();
    };
    ws.onerror = function () {
      /* close follows */
    };
  }

  function startPing() {
    stopPing();
    pingTimer = setInterval(function () {
      sendJson(proto.TYPE_PING, { t: proto.nowMs(), t0: proto.nowMs() });
    }, 5000);
  }

  function stopPing() {
    if (pingTimer) {
      clearInterval(pingTimer);
      pingTimer = null;
    }
  }

  function handleText(text) {
    let obj;
    try {
      obj = proto.decodeJson(text);
    } catch (err) {
      return;
    }
    const classified = proto.classifyInbound(obj);
    state.handledTypes.push(classified.type);
    if (classified.action === "unknown") {
      state.unknownTypes.push(classified.type);
      return;
    }
    if (classified.action === "say") {
      state.lastSay = String(obj.text || "");
      appendLine(state.lastSay, true);
      if (obj.id) reportAudio(obj.id, "received", state.lastSay);
      speech.enqueueSay(state.lastSay, !!obj.urgent, obj.capture_ts, obj.id);
      return;
    }
    if (classified.action === "status") {
      if (obj.msg === "audio") {
        state.pendingUrgentClip = !!obj.urgent;
        state.pendingClipId = obj.id || null;
      }
      return;
    }
    if (classified.action === "proximity") {
      const p = proto.parseProximity(obj);
      policy.onProximity(p, typeof performance !== "undefined" ? performance.now() : Date.now());
      tones.start(policy, {
        onAnnounce: function (msg) { speech.enqueueSay(msg, true); },
        onAck: function (frameKey, cueStartTs, cueState) {
          sendJson(proto.TYPE_ACK, {
            frame_key: frameKey,
            cue_start_ts: cueStartTs,
            state: cueState,
          });
          state.acksSent += 1;
        },
        onVibrate: function (ms) { cues.vibrateOrTone(ms); },
      });
      return;
    }
    if (classified.action === "ping") {
      sendJson(proto.TYPE_PONG, { t: obj.t, t0: obj.t0, tp: proto.nowMs() });
      return;
    }
    if (classified.action === "pong") {
      if (obj.t0 != null && obj.t1 != null && obj.t2 != null) {
        sendJson(proto.TYPE_SYNC, {
          t0: obj.t0,
          t1: obj.t1,
          t2: obj.t2,
          t3: proto.nowMs(),
        });
      }
      return;
    }
    if (classified.action === "transcript") {
      state.lastTranscript = String(obj.text || "");
      appendLine(state.lastTranscript, !!obj.final);
    }
    if (obj.type === proto.TYPE_HELLO_ACK) {
      state.helloAck = true;
    }
  }

  function handleBinary(data) {
    let msg;
    try {
      msg = proto.decodeBinary(data);
    } catch (_e) {
      return;
    }
    if (msg.kind === proto.KIND_AUDIO) {
      const urgent = state.pendingUrgentClip;
      const clipId = state.pendingClipId;
      state.pendingUrgentClip = false;
      state.pendingClipId = null;
      if (clipId) reportAudio(clipId, "received", "");
      speech.enqueueClip(msg.payload, msg.flags, urgent);
    }
  }

  function appendLine(text, isFinal) {
    if (!text) return;
    if (!isFinal) {
      let pending = document.getElementById("partialLine");
      if (!pending) {
        pending = document.createElement("div");
        pending.id = "partialLine";
        pending.className = "partial";
        transcriptEl.appendChild(pending);
      }
      pending.textContent = text;
      return;
    }
    const pending = document.getElementById("partialLine");
    if (pending) pending.remove();
    const row = document.createElement("div");
    row.className = "final";
    row.textContent = text;
    transcriptEl.appendChild(row);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }

  function onDrop() {
    const was = state.connected;
    state.connected = false;
    state.connecting = false;
    state.helloAck = false;
    policy.onConnection(false);
    stopPing();
    if (!state.running) return;
    if (was) {
      try { cues.vibrateOrTone(80); } catch (_e) { /* ignore */ }
    }
    setStatus("connection lost, retrying", "bad", true);
    scheduleReconnect();
  }

  function scheduleReconnect() {
    if (!state.running || reconnectTimer) return;
    const delay = state.backoffMs;
    state.backoffMs = Math.min(state.backoffMs * 2, 15000);
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      if (state.running) connect();
    }, delay);
  }

  function closeSocket() {
    stopPing();
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (ws) {
      try { ws.onclose = null; ws.close(); } catch (_e) { /* ignore */ }
      ws = null;
    }
    state.connected = false;
    state.connecting = false;
  }

  async function startStreaming() {
    persistSettings();
    state.running = true;
    state.backoffMs = 1000;
    state.framesSent = 0;
    renderRunning();
    setStatus("starting", "warn", false);
    announce("starting");
    try {
      await unlockAudio();
    } catch (_e) {
      /* continue */
    }
    try {
      await startCamera();
    } catch (err) {
      state.running = false;
      renderRunning();
      setStatus("Camera permission is required to start", "bad", true);
      return;
    }
    await requestWakeLock();
    if (state.listenOn) {
      try {
        await startMic();
      } catch (_e2) {
        state.listenOn = false;
        renderListen();
        speech.enqueueSay("Microphone permission denied. Streaming camera only.", true);
      }
    }
    connect();
  }

  function stopStreaming() {
    state.running = false;
    policy.onConnection(false);
    tones.stop();
    closeSocket();
    stopMic();
    stopCameraTracks();
    releaseWakeLock();
    speech.stopAll();
    renderRunning();
    setStatus("stopped", "warn", false);
  }

  async function onVisibility() {
    if (document.visibilityState !== "visible") return;
    if (!state.running) return;
    await requestWakeLock();
    if (audioCtx && audioCtx.state === "suspended") {
      try { await audioCtx.resume(); } catch (_e) { /* ignore */ }
    }
    try {
      await startCamera();
    } catch (_e2) {
      setStatus("camera paused, retrying", "warn", true);
    }
    if (state.listenOn) {
      try { await startMic(); } catch (_e3) { /* keep camera */ }
    }
    if (!state.connected && !state.connecting) {
      state.backoffMs = 1000;
      connect();
    }
  }

  startBtn.addEventListener("click", function () {
    if (state.running) stopStreaming();
    else startStreaming();
  });

  listenBtn.addEventListener("click", async function () {
    const turnOn = !state.listenOn;
    if (turnOn) {
      try {
        await unlockAudio();
        const probe = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
        probe.getTracks().forEach(function (t) { t.stop(); });
      } catch (_e) {
        speech.enqueueSay("Microphone permission denied. Listening stays off.", true);
        state.listenOn = false;
        renderListen();
        persistSettings();
        return;
      }
    }
    state.listenOn = turnOn;
    renderListen();
    persistSettings();
    announce(turnOn ? "Listening" : "Listen off");
    if (state.running) {
      if (turnOn) {
        try { await startMic(); } catch (_e2) { /* ignore */ }
      } else {
        stopMic();
      }
      sendJson(proto.TYPE_CONFIG, {
        width: WIDTH,
        height: HEIGHT,
        fps: FPS,
        quality: 70,
        mic: state.listenOn,
      });
    }
  });

  // "What is this?" -> POST /ask on the laptop (Gemini looks at the newest frame,
  // Nimble adds web facts). The answer comes back as a normal `say`.
  const askBtn = $("askWhat");
  function askUrl() {
    const stream = proto.resolveStreamUrl(wsInput.value, location);
    return stream.replace(/^ws/i, "http").replace(/\/v1\/stream$/i, "/ask");
  }
  if (askBtn) {
    askBtn.addEventListener("click", async function () {
      try { await unlockAudio(); } catch (_e) { /* ignore */ }
      if (!state.running || !state.connected) {
        speech.enqueueSay("Press Start first.", false);
        return;
      }
      announce("Looking");
      try {
        const r = await fetch(askUrl(), {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ question: "What is this?" }),
        });
        const j = await r.json();
        if (!j.ok) speech.enqueueSay("The describe feature is off on the laptop.", false);
      } catch (_e2) {
        speech.enqueueSay("Could not reach the laptop.", false);
      }
    });
  }

  wsInput.addEventListener("change", persistSettings);
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("pageshow", onVisibility);
  window.addEventListener("pagehide", function () {
    if (cameraStream) {
      cameraStream.getTracks().forEach(function (t) { t.stop(); });
    }
  });

  function SpeechQueue() {
    this.queue = [];
    this.busy = false;
    this.ctx = null;
    this.current = null;
  }

  SpeechQueue.prototype.attachContext = function (ctx) {
    this.ctx = ctx;
  };

  SpeechQueue.prototype.enqueueSay = function (text, urgent, captureTs, audioId) {
    const clean = String(text || "").trim();
    if (!clean) return;
    if (urgent) this.flush();
    this.queue.push({ kind: "say", text: clean, captureTs: captureTs, audioId: audioId });
    this.pump();
  };

  SpeechQueue.prototype.enqueueClip = function (bytes, flags, urgent) {
    if (!bytes || !bytes.length) return;
    if (urgent) this.flush();
    this.queue.push({ kind: "clip", bytes: bytes, flags: flags });
    this.pump();
  };

  SpeechQueue.prototype.flush = function () {
    this.queue = [];
    this.stopCurrent();
  };

  SpeechQueue.prototype.stopAll = function () {
    this.flush();
    sendSpeaking(false);
  };

  SpeechQueue.prototype.stopCurrent = function () {
    if (window.speechSynthesis) {
      try { window.speechSynthesis.cancel(); } catch (_e) { /* ignore */ }
    }
    if (this.current && this.current.stop) {
      try { this.current.stop(); } catch (_e2) { /* ignore */ }
    }
    this.current = null;
    this.busy = false;
    sendSpeaking(false);
  };

  SpeechQueue.prototype.pump = function () {
    if (this.busy) return;
    const item = this.queue.shift();
    if (!item) {
      sendSpeaking(false);
      return;
    }
    this.busy = true;
    const self = this;
    if (item.kind === "say") this._speak(item, function () { self._done(); });
    else this._playClip(item, function () { self._done(); });
  };

  SpeechQueue.prototype._done = function () {
    this.busy = false;
    this.current = null;
    this.pump();
  };

  SpeechQueue.prototype._speak = function (item, done) {
    sendSpeaking(true);
    const startFields = {
      tts_start_ts: proto.nowMs(),
      say_recv_ts: proto.nowMs(),
    };
    if (item.captureTs != null) startFields.capture_ts = item.captureTs;
    if (item.audioId) startFields.id = item.audioId;
    if (item.text) startFields.text = item.text;
    sendJson(proto.TYPE_TTS_START, startFields);
    reportAudio(item.audioId, "playing", item.text);
    if (!window.speechSynthesis) {
      reportAudio(item.audioId, "error", item.text);
      done();
      return;
    }
    const u = new SpeechSynthesisUtterance(item.text);
    u.rate = 1.05;
    u.onend = function () {
      reportAudio(item.audioId, "ended", item.text);
      done();
    };
    u.onerror = function () {
      reportAudio(item.audioId, "error", item.text);
      done();
    };
    window.speechSynthesis.speak(u);
  };

  SpeechQueue.prototype._playClip = function (item, done) {
    const self = this;
    sendSpeaking(true);
    const ctx = this.ctx || audioCtx;
    if (!ctx) {
      done();
      return;
    }
    decodeClip(ctx, item.bytes, item.flags).then(function (buffer) {
      const src = ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(ctx.destination);
      src.onended = done;
      self.current = src;
      src.start(0);
    }).catch(function () {
      done();
    });
  };

  function decodeClip(ctx, bytes, flags) {
    if (flags === proto.FLAG_PCM) {
      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      const samples = bytes.byteLength / 2;
      const buffer = ctx.createBuffer(1, samples, 16000);
      const ch = buffer.getChannelData(0);
      for (let i = 0; i < samples; i++) {
        ch[i] = view.getInt16(i * 2, true) / 0x8000;
      }
      return Promise.resolve(buffer);
    }
    const copy = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    return ctx.decodeAudioData(copy);
  }

  loadSettings();
  renderRunning();
  setStatus("stopped", "warn", false);
})();
