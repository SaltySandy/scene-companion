# Use Scene Companion on an iPhone

The laptop server can serve a **phone web client** at `/phone`. It is the same live path as the Android app (rear camera JPEG stream, connection status, server TTS and audio, Listen/STT mic) but it runs in **Safari**. There is no APK and no App Store build.

Open this page on the phone:

```text
https://<laptop-magicdns>/phone
```

This was verified on stage hardware: the laptop used Tailscale Serve in front of the local server on **port 8767**, and Safari opened `https://<host>.ts.net/phone`.

The server accepts **one phone at a time**. A second `/phone` (or the Android app) connecting to the same laptop replaces the first socket.

Desktop Chrome on the laptop can use `http://127.0.0.1:<port>/phone` without HTTPS (`localhost` is already a secure context). That is for laptop testing only.

Full iPhone steps are below. The Android APK path is unchanged; see the [README](../README.md).

---

## Why HTTPS is required

iOS Safari only allows **camera, microphone, and motion** access in a [secure context](https://developer.mozilla.org/en-US/docs/Web/Security/Secure_Contexts):

- `https://…` (the Tailscale / Cloudflare paths below)
- `http://localhost` / `http://127.0.0.1` on the *same* device (the laptop’s own browser)

`http://192.168.x.x:8765/phone` on the hackathon Wi‑Fi **will not** get a camera. Safari fails `getUserMedia` and the page cannot stream. Use one of the two HTTPS fronts below.

Audio also needs a user gesture: the first tap on **Start** unlocks Web Audio and speech.

---

## Laptop side

You need the Scene Companion repo on the Windows laptop, Python 3.11+, and the laptop already on Tailscale (or `cloudflared` for the fallback).

### 1. Start the server

Default port is **8765**. `run_server.ps1` and `serve_phone.ps1` both honor `SAW_PORT`.

```powershell
.\run_server.ps1
```

Another port (this is the combination that was verified with Tailscale Serve):

```powershell
$env:SAW_PORT = "8767"
.\run_server.ps1
```

Leave that window open. Confirm the server answers:

```powershell
# change the port if you set SAW_PORT
Invoke-RestMethod http://127.0.0.1:8765/health
```

You want `"ok": true`. The banner also prints `Phone web: http://<lan-ip>:<port>/phone`.

### 2. Put HTTPS in front

`serve_phone.ps1` reads **`-Port`**, then **`SAW_PORT`**, then **8765**.

```powershell
.\serve_phone.ps1                 # 8765
.\serve_phone.ps1 -Port 8767      # same port the server is on
$env:SAW_PORT = "8767"; .\serve_phone.ps1
```

#### A. Tailscale Serve (primary, tailnet-only)

Verified command:

```powershell
tailscale serve --bg --https=443 http://127.0.0.1:8767
```

`serve_phone.ps1` (default `-Mode serve`) runs that with your chosen port. HTTPS on 443 is proxied to `http://127.0.0.1:<port>`. Only devices on **your tailnet** can open it.

On the iPhone:

1. Install the [Tailscale app](https://apps.apple.com/app/tailscale/id1470499037).
2. Sign in to the **same tailnet** as the laptop.
3. Leave Tailscale connected (VPN toggle on).

Find the laptop MagicDNS name (`<machine>.<tailnet>.ts.net`) with:

```powershell
tailscale status
tailscale serve status
```

Turn Serve off when you are done:

```powershell
tailscale serve --https=443 off
```

(`serve_phone.ps1` prints this. `tailscale serve reset` also clears the config.)

#### B. Cloudflare quick tunnel (fallback)

Use this if Tailscale Serve is blocked or the iPhone cannot join the tailnet. Needs [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) (`winget install Cloudflare.cloudflared`).

```powershell
.\serve_phone.ps1 -Mode cloudflare -Port 8767
```

Or by hand:

```powershell
cloudflared tunnel --url http://127.0.0.1:8767
```

It prints a `https://<random>.trycloudflare.com` URL. Open `/phone` on that host. The tunnel process must stay running. Anyone who has the URL can reach the camera page — treat it as a short-lived demo link.

---

## iPhone steps

1. In **Safari** (not Chrome), open:

   ```text
   https://<laptop-magicdns>/phone
   ```

   Example shape: `https://alessandro-laptop.tailnet-name.ts.net/phone`. After a Cloudflare tunnel: `https://<random>.trycloudflare.com/phone`.

2. Leave the WebSocket field **blank** unless you are pointing at another host. The page derives `wss://…/v1/stream` from its own origin.

3. Tap the huge **Start** button. Allow **Camera** (rear / environment). If you will use **Listen**, allow **Microphone** when Safari asks.

4. VoiceOver should hear **connected**. The laptop viewer at `http://127.0.0.1:<port>/` should show frames. The demo stub speaks *“frame received, N per second”* every few seconds.

5. **Listen** (under Start) is off until you tap it. It streams 16 kHz mic PCM for speech-to-text.

6. Optional: **Share → Add to Home Screen** so the page opens full-screen like an app.

7. Keep the screen awake. The page requests the Wake Lock API (iOS 16.4+). Turn Auto-Lock up (Settings → Display & Brightness) for the demo. If the screen locks or you switch apps, Safari pauses the camera; unlock and come back — the page reconnects.

Stop streaming with the red **Stop** button, or close the tab.

---

## One phone slot

`/v1/stream` has a single phone socket. If a second iPhone, a second Safari tab, or the Android app connects to the same server, the first connection is dropped (`replaced`). Use one phone per laptop, or run a second server on another `SAW_PORT`.

---

## Troubleshooting

**Safari says camera / mic permission denied, or Start fails immediately**

- The page is not HTTPS (or not localhost). Open the Tailscale / Cloudflare `https://` URL, not `http://192.168…`.
- Settings → Safari → Camera / Microphone is blocked for the site. Reset the site permissions and reload.
- You denied the prompt. Reload and tap **Start** again.

**Page loads but no frames / status stays Connecting**

- The laptop server is not running, or Serve is pointed at the wrong port. `SAW_PORT` for `run_server.ps1` and `-Port` / `SAW_PORT` for `serve_phone.ps1` must match. Check `http://127.0.0.1:<port>/health` on the laptop.
- Another phone or the Android app already holds the stream slot. Stop the other client and tap **Start** again.
- Tailscale is off on the iPhone, or it is signed into a different tailnet (Serve is tailnet-only).
- Cloudflare: the `cloudflared` process died; the trycloudflare hostname is gone.

**Certificate or HTTPS errors**

- Serve: wait a few seconds after `tailscale serve` and retry. Confirm `tailscale serve status` shows HTTPS → `http://127.0.0.1:<port>`.
- Date & Time on the iPhone is automatic.
- Do not open `https://127.0.0.1:8765` on the phone. Use the MagicDNS or trycloudflare hostname.
- Cloudflare: only use the `https://*.trycloudflare.com` URL it printed.

**Screen locks, stream dies, or you background Safari**

- Expected. iOS pauses the camera. Unlock, return to the tab, and wait for **connected** (or tap **Start** again).
- Raise Auto-Lock, keep the page in the foreground, Add to Home Screen if that helps you stay on it.
- Wake Lock is missing on iOS older than 16.4; the screen will still dim.

**Audio never talks**

- The first **Start** tap is required. Safari will not autoplay TTS or clips.
- Check Silent / Ring switch and volume.

**Desktop on the laptop works, iPhone does not**

- Laptop `http://127.0.0.1:<port>/phone` is a secure context. The phone over LAN HTTP is not. Use Serve or Cloudflare.

---

## iOS limits you cannot fully work around

- No camera or mic on plain `http://` LAN IPs.
- Audio unlocks only after **Start**.
- `navigator.vibrate` does not exist; proximity haptics (when a proximity server is in use) fall back to tones.
- Backgrounding is not an Android foreground service.
- Wake Lock needs iOS 16.4+.
