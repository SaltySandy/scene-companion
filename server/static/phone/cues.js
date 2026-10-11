/**
 * Proximity cues for feat/proximity: tones + optional vibrate, 1 s LOST watchdog.
 *
 * Silence never means "clear". The engine stays dark until the first proximity
 * message so a main-branch server does not play sensor-lost forever.
 */
(function (root, factory) {
  const api = factory();
  root.SawCues = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const CueState = { CLEAR: "clear", NEAR: "near", DANGER: "danger", LOST: "lost" };

  const PATTERNS = {
    clear: { freqHz: 440, freq2Hz: 440, onMs: 25, periodMs: 2000, gain: 0.12, vibrateMs: 0 },
    near: { freqHz: 880, freq2Hz: 880, onMs: 70, periodMs: 350, gain: 0.5, vibrateMs: 30 },
    danger: { freqHz: 1320, freq2Hz: 1320, onMs: 120, periodMs: 150, gain: 0.9, vibrateMs: 100 },
    lost: { freqHz: 660, freq2Hz: 330, onMs: 500, periodMs: 1000, gain: 0.7, vibrateMs: 400 },
  };

  function CuePolicy(watchdogMs) {
    this.watchdogMs = watchdogMs == null ? 1000 : watchdogMs;
    this.last = null;
    this.lastAtMs = 0;
    this.connected = false;
    this.armed = false;
    this.currentlyLost = true;
    this.lostSinceMs = 0;
    this.spokenLost = true;
    this.lastAckedKey = null;
  }

  CuePolicy.prototype.onProximity = function (p, nowMs) {
    this.last = p;
    this.lastAtMs = nowMs;
    this.armed = true;
  };

  CuePolicy.prototype.onConnection = function (isConnected) {
    this.connected = isConnected;
    if (!isConnected) this.last = null;
  };

  CuePolicy.prototype.takeAck = function (cue) {
    if (!cue || cue.frameKey == null) return null;
    if (cue.frameKey === this.lastAckedKey) return null;
    this.lastAckedKey = cue.frameKey;
    return cue.frameKey;
  };

  CuePolicy.prototype.cue = function (nowMs) {
    if (!this.armed) return null;
    const p = this.last;
    if (!this.connected || !p || nowMs - this.lastAtMs > this.watchdogMs || !p.known) {
      return { state: CueState.LOST, pan: 0, frameKey: p && p.frameKey };
    }
    let state = CueState.CLEAR;
    if (p.level === "danger") state = CueState.DANGER;
    else if (p.level === "near") state = CueState.NEAR;
    return { state: state, pan: this._pan(p.levels), frameKey: p.frameKey };
  };

  CuePolicy.prototype.announcement = function (cue, nowMs) {
    if (!cue) return null;
    const lost = cue.state === CueState.LOST;
    if (lost !== this.currentlyLost) {
      this.currentlyLost = lost;
      this.lostSinceMs = nowMs;
    }
    if (lost && !this.spokenLost && nowMs - this.lostSinceMs >= 1000) {
      this.spokenLost = true;
      return "sensor lost";
    }
    if (!lost && this.spokenLost && nowMs - this.lostSinceMs >= 500) {
      this.spokenLost = false;
      return "sensor on";
    }
    return null;
  };

  CuePolicy.prototype._severity = function (level) {
    if (level === "danger") return 2;
    if (level === "near") return 1;
    return 0;
  };

  CuePolicy.prototype._pan = function (levels) {
    levels = levels || {};
    const v = (this._severity(levels.right) - this._severity(levels.left)) / 2;
    return Math.max(-1, Math.min(1, v));
  };

  /**
   * Web Audio tone loop. iOS has no navigator.vibrate; the tones are the cue.
   */
  function TonePlayer(getContext) {
    this._getContext = getContext;
    this._timer = null;
    this._playing = false;
    this._osc = null;
    this._gain = null;
    this._panner = null;
    this._pattern = null;
    this._cycleStart = 0;
    this._onVibrate = null;
  }

  TonePlayer.prototype.start = function (policy, hooks) {
    const self = this;
    if (this._timer) return;
    hooks = hooks || {};
    this._onVibrate = hooks.onVibrate;
    this._timer = setInterval(function () {
      const now = typeof performance !== "undefined" ? performance.now() : Date.now();
      const cue = policy.cue(now);
      if (!cue) {
        self._stopTone();
        return;
      }
      const pattern = PATTERNS[cue.state] || PATTERNS.lost;
      self._tick(pattern, cue.pan, now);
      const spoken = policy.announcement(cue, now);
      if (spoken && hooks.onAnnounce) hooks.onAnnounce(spoken);
      const ackKey = policy.takeAck(cue);
      if (ackKey != null && hooks.onAck) {
        hooks.onAck(ackKey, Date.now(), cue.state);
      }
    }, 20);
  };

  TonePlayer.prototype.stop = function () {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
    this._stopTone();
  };

  TonePlayer.prototype._tick = function (pattern, pan, now) {
    const ctx = this._getContext && this._getContext();
    if (!ctx) return;
    if (this._pattern !== pattern) {
      this._pattern = pattern;
      this._cycleStart = now;
      this._ensureNodes(ctx, pan);
      if (pattern.vibrateMs > 0 && this._onVibrate) this._onVibrate(pattern.vibrateMs);
    }
    this._setPan(pan);
    const t = (now - this._cycleStart) % pattern.periodMs;
    const on = t < pattern.onMs;
    const freq = t < pattern.onMs / 2 ? pattern.freqHz : pattern.freq2Hz;
    if (this._osc) {
      try {
        this._osc.frequency.setValueAtTime(freq, ctx.currentTime);
      } catch (_e) {
        /* ignore */
      }
    }
    if (this._gain) {
      const fade = Math.min(pattern.onMs / 4, 5);
      let env = 0;
      if (on) {
        env = Math.min(1, t / fade, (pattern.onMs - t) / fade);
      }
      try {
        this._gain.gain.setTargetAtTime(pattern.gain * env, ctx.currentTime, 0.005);
      } catch (_e2) {
        this._gain.gain.value = pattern.gain * env;
      }
    }
  };

  TonePlayer.prototype._ensureNodes = function (ctx, pan) {
    if (this._osc) return;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    gain.gain.value = 0;
    let dest = gain;
    if (ctx.createStereoPanner) {
      this._panner = ctx.createStereoPanner();
      this._panner.pan.value = pan || 0;
      gain.connect(this._panner);
      this._panner.connect(ctx.destination);
    } else {
      gain.connect(ctx.destination);
    }
    osc.type = "sine";
    osc.connect(gain);
    osc.start();
    this._osc = osc;
    this._gain = gain;
    void dest;
  };

  TonePlayer.prototype._setPan = function (pan) {
    if (this._panner) {
      try {
        this._panner.pan.value = pan || 0;
      } catch (_e) {
        /* ignore */
      }
    }
  };

  TonePlayer.prototype._stopTone = function () {
    if (this._osc) {
      try {
        this._osc.stop();
      } catch (_e) {
        /* ignore */
      }
      try {
        this._osc.disconnect();
      } catch (_e2) {
        /* ignore */
      }
    }
    if (this._gain) {
      try {
        this._gain.disconnect();
      } catch (_e3) {
        /* ignore */
      }
    }
    if (this._panner) {
      try {
        this._panner.disconnect();
      } catch (_e4) {
        /* ignore */
      }
    }
    this._osc = null;
    this._gain = null;
    this._panner = null;
    this._pattern = null;
  };

  function vibrateOrTone(ms) {
    if (typeof navigator !== "undefined" && typeof navigator.vibrate === "function") {
      try {
        navigator.vibrate(ms);
        return "vibrate";
      } catch (_e) {
        /* iOS throws / missing */
      }
    }
    return "tone";
  }

  return {
    CueState: CueState,
    PATTERNS: PATTERNS,
    CuePolicy: CuePolicy,
    TonePlayer: TonePlayer,
    vibrateOrTone: vibrateOrTone,
  };
});
