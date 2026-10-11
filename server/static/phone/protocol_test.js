/* Node unit tests for SAW protocol v1 (the same file the browser loads). */
"use strict";

const assert = require("assert");
const proto = require("./protocol.js");
const cues = require("./cues.js");

const MINI_JPEG = Buffer.from(
  "ffd8ffe000104a46494600010100000100010000ffdb004300" +
    "0808080808080808080808080808080808080808080808080808080808080808" +
    "080808080808080808080808080808080808080808080808080808080808ffd9",
  "hex",
);

function testRoundtripJpeg() {
  const raw = proto.encodeBinary(proto.KIND_JPEG, MINI_JPEG, 1700000000123, 0);
  const msg = proto.decodeBinary(raw);
  assert.strictEqual(msg.kind, proto.KIND_JPEG);
  assert.strictEqual(msg.flags, 0);
  assert.strictEqual(msg.timestampMs, 1700000000123);
  assert.deepStrictEqual(Buffer.from(msg.payload), MINI_JPEG);
  assert.strictEqual(raw[0], 0x53);
  assert.strictEqual(raw.length, proto.HEADER_SIZE + MINI_JPEG.length);
}

function testMicAndAudioFlags() {
  const pcm = Uint8Array.from([1, 2, 3, 4]);
  const mic = proto.decodeBinary(proto.encodeBinary(proto.KIND_MIC, pcm, 9));
  assert.strictEqual(mic.kind, proto.KIND_MIC);
  assert.deepStrictEqual(Array.from(mic.payload), [1, 2, 3, 4]);
  const wav = Uint8Array.from([82, 73, 70, 70]);
  const clip = proto.decodeBinary(proto.encodeBinary(proto.KIND_AUDIO, wav, 42, proto.FLAG_WAV));
  assert.strictEqual(clip.kind, proto.KIND_AUDIO);
  assert.strictEqual(proto.mimeForFlag(clip.flags), "audio/wav");
}

function testJson() {
  const text = proto.encodeJson(proto.TYPE_SAY, { text: "door on the left", urgent: true });
  const obj = proto.decodeJson(text);
  assert.strictEqual(obj.v, 1);
  assert.strictEqual(obj.type, "say");
  assert.strictEqual(obj.text, "door on the left");
  assert.strictEqual(obj.urgent, true);
  assert.throws(() => proto.decodeJson('{"v":2,"type":"say"}'), /unsupported/);
  assert.throws(() => proto.decodeJson('{"v":1}'), /missing type/);
}

function testClassifyInbound() {
  const cases = [
    ["say", "say"],
    ["status", "status"],
    ["proximity", "proximity"],
    ["ping", "ping"],
    ["pong", "pong"],
    ["transcript", "transcript"],
    ["hello_ack", "ignore"],
    ["flash", "ignore"],
    ["latency", "ignore"],
    ["audio_out", "ignore"],
    ["stats", "ignore"],
    ["not_a_real_type", "unknown"],
  ];
  for (const [type, action] of cases) {
    assert.strictEqual(proto.classifyInbound({ v: 1, type }).action, action, type);
  }
}

function testProximityParseAndWatchdog() {
  const p = proto.parseProximity({
    v: 1,
    type: "proximity",
    seq: 3,
    state: "ok",
    level: "danger",
    levels: { left: "clear", center: "danger", right: "near" },
    sectors_m: { left: 4, center: 0.4, right: 1.2 },
    frame_key: 1700000001000,
    age_ms: 12,
  });
  assert.strictEqual(p.known, true);
  assert.strictEqual(p.level, "danger");
  const policy = new cues.CuePolicy(1000);
  assert.strictEqual(policy.cue(0), null, "no tones until first heartbeat");
  policy.onConnection(true);
  policy.onProximity(p, 5000);
  const now = policy.cue(5200);
  assert.strictEqual(now.state, "danger");
  const lost = policy.cue(6201);
  assert.strictEqual(lost.state, "lost");
  const ack = policy.takeAck(now);
  assert.strictEqual(ack, 1700000001000);
  assert.strictEqual(policy.takeAck(now), null);
}

function testStreamUrl() {
  const https = { protocol: "https:", host: "box.tailnet.ts.net" };
  const http = { protocol: "http:", host: "127.0.0.1:8765" };
  assert.strictEqual(proto.defaultStreamUrl(https), "wss://box.tailnet.ts.net/v1/stream");
  assert.strictEqual(proto.defaultStreamUrl(http), "ws://127.0.0.1:8765/v1/stream");
  assert.strictEqual(proto.resolveStreamUrl("", http), "ws://127.0.0.1:8765/v1/stream");
  assert.strictEqual(
    proto.resolveStreamUrl("wss://other.example/v1/stream", http),
    "wss://other.example/v1/stream",
  );
  assert.strictEqual(
    proto.resolveStreamUrl("https://other.example", https),
    "wss://other.example/v1/stream",
  );
  assert.strictEqual(proto.resolveStreamUrl("10.0.0.4:8765", http), "ws://10.0.0.4:8765/v1/stream");
}

function main() {
  testRoundtripJpeg();
  testMicAndAudioFlags();
  testJson();
  testClassifyInbound();
  testProximityParseAndWatchdog();
  testStreamUrl();
  console.log("ok: protocol.js + cues.js");
}

main();
