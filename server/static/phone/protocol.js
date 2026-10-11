/**
 * SAW protocol v1 — binary envelope + JSON control messages.
 * Must match server/protocol.py and android/.../Protocol.kt.
 *
 * Works in the browser and in Node (tests).
 */
(function (root, factory) {
  const api = factory();
  root.SawProtocol = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const PROTO_VERSION = 1;
  const HEADER_SIZE = 20;
  const MAGIC = new Uint8Array([0x53, 0x41, 0x57, 0x31]); // SAW1

  const KIND_JPEG = 0x01;
  const KIND_MIC = 0x02;
  const KIND_AUDIO = 0x03;

  const FLAG_WAV = 0;
  const FLAG_MP3 = 1;
  const FLAG_PCM = 2;

  const TYPE_HELLO = "hello";
  const TYPE_HELLO_ACK = "hello_ack";
  const TYPE_PING = "ping";
  const TYPE_PONG = "pong";
  const TYPE_SAY = "say";
  const TYPE_CONFIG = "config";
  const TYPE_STATUS = "status";
  const TYPE_STATS = "stats";
  const TYPE_MIC = "mic";
  const TYPE_SPEAKING = "speaking";
  const TYPE_TRANSCRIPT = "transcript";
  const TYPE_ERROR = "error";
  const TYPE_PROXIMITY = "proximity";
  const TYPE_ACK = "ack";
  const TYPE_TTS_START = "tts_start";
  const TYPE_SYNC = "sync";
  const TYPE_FLASH = "flash";
  const TYPE_LATENCY = "latency";
  const TYPE_AUDIO_EVENT = "audio_event";
  const TYPE_AUDIO_OUT = "audio_out";

  const LEVELS = ["clear", "near", "danger"];
  const SECTORS = ["left", "center", "right"];

  /** Types the laptop may send that we understand or safely ignore. */
  const KNOWN_INBOUND = {
    hello_ack: "ignore",
    say: "say",
    status: "status",
    pong: "pong",
    ping: "ping",
    transcript: "transcript",
    proximity: "proximity",
    stats: "ignore",
    error: "ignore",
    flash: "ignore",
    latency: "ignore",
    audio_out: "ignore",
  };

  function nowMs() {
    return Date.now();
  }

  function setUint64BE(view, offset, value) {
    const big = BigInt(value);
    view.setUint32(offset, Number((big >> 32n) & 0xffffffffn));
    view.setUint32(offset + 4, Number(big & 0xffffffffn));
  }

  function getUint64BE(view, offset) {
    const hi = view.getUint32(offset);
    const lo = view.getUint32(offset + 4);
    return hi * 0x100000000 + lo;
  }

  function encodeBinary(kind, payload, timestampMs, flags) {
    if (flags === undefined) flags = 0;
    if (timestampMs === undefined || timestampMs === null) timestampMs = nowMs();
    const bytes = payload instanceof Uint8Array ? payload : new Uint8Array(payload);
    const out = new Uint8Array(HEADER_SIZE + bytes.length);
    const view = new DataView(out.buffer, out.byteOffset, out.byteLength);
    out[0] = MAGIC[0];
    out[1] = MAGIC[1];
    out[2] = MAGIC[2];
    out[3] = MAGIC[3];
    view.setUint8(4, kind);
    view.setUint8(5, flags & 0xff);
    view.setUint16(6, 0);
    setUint64BE(view, 8, timestampMs);
    view.setUint32(16, bytes.length);
    out.set(bytes, HEADER_SIZE);
    return out;
  }

  function decodeBinary(data) {
    const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
    if (bytes.length < HEADER_SIZE) {
      throw new Error("binary frame too short: " + bytes.length);
    }
    if (bytes[0] !== MAGIC[0] || bytes[1] !== MAGIC[1] || bytes[2] !== MAGIC[2] || bytes[3] !== MAGIC[3]) {
      throw new Error("bad magic");
    }
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const kind = view.getUint8(4);
    const flags = view.getUint8(5);
    const timestampMs = getUint64BE(view, 8);
    const payloadLen = view.getUint32(16);
    if (bytes.length < HEADER_SIZE + payloadLen) {
      throw new Error("truncated payload");
    }
    if (kind !== KIND_JPEG && kind !== KIND_MIC && kind !== KIND_AUDIO) {
      throw new Error("unknown kind: " + kind);
    }
    return {
      kind: kind,
      flags: flags,
      timestampMs: timestampMs,
      payload: bytes.subarray(HEADER_SIZE, HEADER_SIZE + payloadLen),
    };
  }

  function encodeJson(type, fields) {
    const body = Object.assign({ v: PROTO_VERSION, type: type }, fields || {});
    return JSON.stringify(body);
  }

  function decodeJson(text) {
    const obj = JSON.parse(text);
    if (obj === null || typeof obj !== "object" || Array.isArray(obj)) {
      throw new Error("JSON message must be an object");
    }
    if (obj.v !== PROTO_VERSION) {
      throw new Error("unsupported protocol version: " + obj.v);
    }
    if (!obj.type) {
      throw new Error("JSON message missing type");
    }
    return obj;
  }

  function mimeForFlag(flags) {
    if (flags === FLAG_MP3) return "audio/mpeg";
    if (flags === FLAG_PCM) return "audio/l16";
    return "audio/wav";
  }

  function parseProximity(obj) {
    const levels = {};
    const sectorsM = {};
    const rawLevels = obj.levels || {};
    const rawSectors = obj.sectors_m || {};
    for (let i = 0; i < SECTORS.length; i++) {
      const s = SECTORS[i];
      if (rawLevels[s] != null) levels[s] = String(rawLevels[s]);
      if (rawSectors[s] != null) sectorsM[s] = Number(rawSectors[s]);
    }
    const state = obj.state || "unknown";
    const level = obj.level == null ? null : String(obj.level);
    return {
      seq: Number(obj.seq || 0),
      state: state,
      level: level,
      levels: levels,
      sectorsM: sectorsM,
      frameKey: obj.frame_key == null ? null : Number(obj.frame_key),
      ageMs: obj.age_ms == null ? null : Number(obj.age_ms),
      known: state === "ok" && LEVELS.indexOf(level) !== -1,
    };
  }

  /**
   * Classify an inbound JSON message. The web client plays say / audio / proximity
   * cues and ignores unknown types so it works on main, feat/proximity, and PR #3.
   */
  function classifyInbound(obj) {
    const type = obj && obj.type;
    if (!type) return { action: "invalid", type: type };
    if (KNOWN_INBOUND[type]) return { action: KNOWN_INBOUND[type], type: type };
    return { action: "unknown", type: type };
  }

  function defaultStreamUrl(locationLike) {
    const loc = locationLike || (typeof location !== "undefined" ? location : null);
    if (!loc) return "ws://127.0.0.1:8765/v1/stream";
    const proto = loc.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + loc.host + "/v1/stream";
  }

  /**
   * Accept a full ws/wss URL, host:port, or host. Empty uses the page origin.
   */
  function resolveStreamUrl(override, locationLike) {
    const raw = (override || "").trim();
    if (!raw) return defaultStreamUrl(locationLike);
    if (/^wss?:\/\//i.test(raw)) {
      return raw.replace(/\/$/, "");
    }
    if (/^https?:\/\//i.test(raw)) {
      const u = raw.replace(/^http/i, "ws").replace(/\/$/, "");
      return /\/v1\/stream$/i.test(u) ? u : u + "/v1/stream";
    }
    const loc = locationLike || (typeof location !== "undefined" ? location : { protocol: "http:" });
    const proto = loc.protocol === "https:" ? "wss:" : "ws:";
    const host = raw.replace(/^\/+/, "");
    if (/\/v1\/stream$/i.test(host)) return proto + "//" + host;
    return proto + "//" + host + "/v1/stream";
  }

  return {
    PROTO_VERSION: PROTO_VERSION,
    HEADER_SIZE: HEADER_SIZE,
    MAGIC: MAGIC,
    KIND_JPEG: KIND_JPEG,
    KIND_MIC: KIND_MIC,
    KIND_AUDIO: KIND_AUDIO,
    FLAG_WAV: FLAG_WAV,
    FLAG_MP3: FLAG_MP3,
    FLAG_PCM: FLAG_PCM,
    TYPE_HELLO: TYPE_HELLO,
    TYPE_HELLO_ACK: TYPE_HELLO_ACK,
    TYPE_PING: TYPE_PING,
    TYPE_PONG: TYPE_PONG,
    TYPE_SAY: TYPE_SAY,
    TYPE_CONFIG: TYPE_CONFIG,
    TYPE_STATUS: TYPE_STATUS,
    TYPE_STATS: TYPE_STATS,
    TYPE_MIC: TYPE_MIC,
    TYPE_SPEAKING: TYPE_SPEAKING,
    TYPE_TRANSCRIPT: TYPE_TRANSCRIPT,
    TYPE_ERROR: TYPE_ERROR,
    TYPE_PROXIMITY: TYPE_PROXIMITY,
    TYPE_ACK: TYPE_ACK,
    TYPE_TTS_START: TYPE_TTS_START,
    TYPE_SYNC: TYPE_SYNC,
    TYPE_FLASH: TYPE_FLASH,
    TYPE_LATENCY: TYPE_LATENCY,
    TYPE_AUDIO_EVENT: TYPE_AUDIO_EVENT,
    TYPE_AUDIO_OUT: TYPE_AUDIO_OUT,
    LEVELS: LEVELS,
    SECTORS: SECTORS,
    KNOWN_INBOUND: KNOWN_INBOUND,
    nowMs: nowMs,
    encodeBinary: encodeBinary,
    decodeBinary: decodeBinary,
    encodeJson: encodeJson,
    decodeJson: decodeJson,
    mimeForFlag: mimeForFlag,
    parseProximity: parseProximity,
    classifyInbound: classifyInbound,
    defaultStreamUrl: defaultStreamUrl,
    resolveStreamUrl: resolveStreamUrl,
  };
});
