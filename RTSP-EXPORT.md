# RTSP Export & Dynamic Quality

When you export the stream as RTSP, **the resolution and FPS automatically follow the tier changes** based on importance score, just like the dashboard feed.

## How It Works

1. **Negotiation loop** (every 1 second):
   - Reads person count and importance across all nodes
   - Decides the tier: LOW, MEDIUM, or HIGH
   - If tier changes → calls `_apply_encoder_params(new_quality)`

2. **Encoder params update**:
   - Updates resolution, FPS, bitrate
   - If FFmpeg is currently streaming → **restarts it** with new params

3. **FFmpeg restarts dynamically**:
   - Stops the old FFmpeg process
   - Starts a new one with updated resolution/FPS
   - RTSP clients see the new stream parameters

**Example sequence:**

```
t=0s:  No person detected
       → quality: LOW
       → FFmpeg: 480p @ 10 fps, 500 kbps
       → RTSP clients see 480p

t=5s:  Person enters frame, this node ranks #1
       → importance score ↑
       → quality: HIGH
       → _apply_encoder_params() restarts FFmpeg
       → FFmpeg: 1080p @ 30 fps, 4000 kbps
       → RTSP clients see 1080p

t=10s: Person leaves, importance drops
       → quality: LOW
       → FFmpeg restarts again
       → FFmpeg: 480p @ 10 fps, 500 kbps
```

---

## Setup: RTSP Server (MediaMTX)

To actually stream the RTSP feed, you need an RTSP server listening. Here's how:

### Option 1: Use MediaMTX (Recommended)

MediaMTX is a lightweight RTSP server. Install and run it:

```bash
# macOS
brew install mediamtx

# or download from: https://github.com/bluenviron/mediamtx/releases

# Start it
mediamtx
```

This starts an RTSP server at `rtsp://localhost:8554` by default.

Then in your node.py, FFmpeg will push the stream to `rtsp://localhost:8554/webcam` (already configured).

### Option 2: Docker

```bash
docker run -d \
  -p 8554:8554 \
  -p 8888:8888 \
  bluenviron/mediamtx:latest
```

---

## Configure & Test

### Step 1: Start MediaMTX

```bash
mediamtx
```

You should see:
```
2026-04-16T15:25:00Z INFO conf.go:54 [RTSP] listener opened on [::]:8554
```

### Step 2: Start your node

```bash
NODE_ID=node_1 \
MQTT_BROKER=172.16.245.185 \
FLASK_PORT=5001 \
python node.py
```

### Step 3: Open the dashboard

Visit `http://localhost:5001/` and start the stream.

### Step 4: Manually start RTSP export

Once people/activity is detected, call:

```bash
curl -X POST http://localhost:5001/api/stream/start
```

You should see in the logs:
```
[Stream] Started FFmpeg stream to MediaMTX on rtsp://localhost:8554/webcam (480p, 10fps, 500kbps)
```

### Step 5: View RTSP stream

Use any RTSP player:

```bash
# VLC
open "rtsp://localhost:8554/webcam"

# macOS QuickTime
open "rtsp://localhost:8554/webcam"

# ffplay (from ffmpeg)
ffplay "rtsp://localhost:8554/webcam"
```

---

## Automatic vs Manual RTSP Start

Currently, RTSP export is **manual**:

1. Dashboard MJPEG feed starts automatically when you click "Start Stream & Detection"
2. RTSP export requires manual `/api/stream/start` call

**To make RTSP auto-start**, modify [node.py](node.py#L823) to call `/api/stream/start` automatically when workers start:

```python
# In api_start() after showing dashboard, add:
# _start_ffmpeg_process(state.encoder_params)  # Add this line
```

Or set environment variable:
```bash
AUTO_START_RTSP=1 python node.py
```

---

## Logging & Debugging

Watch the logs to see tier changes and restarts:

```python
# When tier changes:
[Negotiation] nodes=1  rank=1  persons=2  importance=0.7000  quality: LOW → HIGH

# When FFmpeg restarts:
[Stream] Reconfigured encoder → 1080p @ 30fps, 4000kbps

# When tier changes back:
[Negotiation] nodes=1  rank=1  persons=0  importance=0.0000  quality: HIGH → LOW
[Stream] Reconfigured encoder → 480p @ 10fps, 500kbps
```

---

## Important Notes

1. **RTSP needs MediaMTX (or similar RTSP server)**
   - FFmpeg pushes to `rtsp://localhost:8554/webcam`
   - This requires MediaMTX listening on port 8554
   - Dashboard MJPEG feed works without any external server

2. **Resolution changes are seamless**
   - FFmpeg stops and restarts (usually < 1 second)
   - Clients may briefly disconnect and reconnect
   - This is expected behavior

3. **Your error**
   ```
   [ERROR] [Video] Cannot open source 'rtsp://localhost:554'. Exiting thread.
   ```
   This means your input RTSP URL is wrong. Check:
   - Is there an RTSP camera at that address?
   - Is the port correct? (usually 554 or 8554)
   - Can you ping the camera?

   Use a working RTSP camera URL or switch to webcam mode:
   ```bash
   RTSP_URL=""  # Empty → uses webcam
   WEBCAM_INDEX=0
   ```

---

## Summary

| Feature | Dashboard MJPEG | RTSP Export |
|---|---|---|
| **No external server needed** | ✅ Yes | ❌ Needs MediaMTX |
| **Auto-start** | ✅ Yes | ❌ Manual start |
| **Dynamic resolution** | ✅ Yes | ✅ Yes |
| **Supported by all browsers** | ✅ Yes | ❌ Needs RTSP player |
| **Lower latency** | ❌ ~200ms | ✅ ~50ms |

For development, use the **dashboard MJPEG feed**. For external RTSP clients, set up **MediaMTX + auto-start**.
