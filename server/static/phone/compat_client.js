#!/usr/bin/env node
/**
 * Drive the same JS protocol the browser uses against a live /v1/stream.
 * Prints one JSON object to stdout.
 */
"use strict";

const proto = require("./protocol.js");

const MINI_JPEG = Buffer.from(
  "ffd8ffe000104a46494600010100000100010000ffdb004300" +
    "0808080808080808080808080808080808080808080808080808080808080808" +
    "080808080808080808080808080808080808080808080808080808080808ffd9",
  "hex",
);

function arg(name, fallback) {
  const i = process.argv.indexOf("--" + name);
  if (i === -1 || i + 1 >= process.argv.length) return fallback;
  return process.argv[i + 1];
}

async function main() {
  const url = arg("url", "ws://127.0.0.1:8765/v1/stream");
  const frames = Number(arg("frames", "8"));
  const waitMs = Number(arg("wait-ms", "4000"));
  const result = {
    hello_ack: false,
    frames_sent: 0,
    says: [],
    transcripts: [],
    proximity: [],
    acks_sent: 0,
    audio_clips: 0,
    pings: 0,
    pongs: 0,
    unknown: [],
    handled: [],
    error: null,
  };

  const ws = new WebSocket(url);
  await new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("connect timeout")), 5000);
    ws.addEventListener("open", () => {
      clearTimeout(t);
      resolve();
    });
    ws.addEventListener("error", (ev) => {
      clearTimeout(t);
      reject(ev.error || new Error("ws error"));
    });
  });

  ws.send(proto.encodeJson(proto.TYPE_HELLO, { role: "phone", app: "saw-web-compat", app_ver: "0.1.0" }));
  ws.send(
    proto.encodeJson(proto.TYPE_CONFIG, {
      width: 640,
      height: 480,
      fps: 8,
      quality: 70,
      mic: false,
    }),
  );

  ws.addEventListener("message", (ev) => {
    if (typeof ev.data !== "string") {
      const buf = ev.data instanceof ArrayBuffer ? new Uint8Array(ev.data) : new Uint8Array(Buffer.from(ev.data));
      try {
        const msg = proto.decodeBinary(buf);
        if (msg.kind === proto.KIND_AUDIO) result.audio_clips += 1;
      } catch (_e) {
        /* ignore */
      }
      return;
    }
    let obj;
    try {
      obj = proto.decodeJson(ev.data);
    } catch (_e) {
      return;
    }
    const classified = proto.classifyInbound(obj);
    result.handled.push(classified.type);
    if (classified.action === "unknown") result.unknown.push(classified.type);
    if (obj.type === proto.TYPE_HELLO_ACK) result.hello_ack = true;
    if (classified.action === "say") result.says.push(obj);
    if (classified.action === "transcript") result.transcripts.push(obj);
    if (classified.action === "proximity") {
      result.proximity.push(proto.parseProximity(obj));
      if (obj.frame_key != null) {
        ws.send(
          proto.encodeJson(proto.TYPE_ACK, {
            frame_key: obj.frame_key,
            cue_start_ts: proto.nowMs(),
            state: obj.level || "lost",
          }),
        );
        result.acks_sent += 1;
      }
    }
    if (classified.action === "ping") {
      result.pings += 1;
      ws.send(proto.encodeJson(proto.TYPE_PONG, { t: obj.t, t0: obj.t0, tp: proto.nowMs() }));
    }
    if (classified.action === "pong") result.pongs += 1;
  });

  for (let i = 0; i < frames; i++) {
    ws.send(proto.encodeBinary(proto.KIND_JPEG, MINI_JPEG, proto.nowMs(), 0));
    result.frames_sent += 1;
    await new Promise((r) => setTimeout(r, 80));
  }

  await new Promise((r) => setTimeout(r, waitMs));
  ws.close();
  process.stdout.write(JSON.stringify(result) + "\n");
}

main().catch((err) => {
  process.stdout.write(JSON.stringify({ error: String(err && err.message ? err.message : err) }) + "\n");
  process.exit(1);
});
