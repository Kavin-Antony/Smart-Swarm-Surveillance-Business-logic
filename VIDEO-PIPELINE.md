# Video Pipeline & Resolution Switching

This document explains how video flows from the camera to the browser dashboard and how resolution/FPS changes propagate.

## Two Separate Pipelines

### Pipeline 1: Dashboard MJPEG Feed (Browser sees this)

```
┌─────────────────┐
│ Camera/RTSP/    │
│ Webcam Source   │  (OpenCV cv2.VideoCapture)
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────┐
│ video_processing_thread()       │  (Thread 1 in node.py)
│                                 │
│ 1. Read frame from source       │  Raw camera frame
│ 2. Check encoder_params         │  (from shared state)
│ 3. cv2.resize() if needed       │  ◄── Resolution switch happens here
│ 4. cv2.imencode() to JPEG       │
│ 5. Store in state.latest_frame  │
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│ generate_frames() generator     │  (Main thread, Flask route)
│                                 │
│ 1. Read state.latest_frame      │
│ 2. Get current FPS from state   │
│ 3. Yield JPEG bytes (MJPEG)     │  ◄── FPS pacing happens here
│ 4. Sleep 1/fps seconds          │
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│ /video_feed endpoint            │
│ (Flask Response with MJPEG)     │
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│ Browser native MJPEG player     │  (HTML <img> tag)
│ Displays real-time feed         │
└─────────────────────────────────┘
```

**Key Tools & Libraries:**

| Layer | Tool | Purpose |
|---|---|---|
| **Capture** | OpenCV `cv2.VideoCapture()` | Read raw frames from camera, RTSP, or webcam |
| **Resize** | OpenCV `cv2.resize(INTER_AREA)` | Downsample frames to selected resolution |
| **Encode** | OpenCV `cv2.imencode('.jpg')` | Compress resized frame to JPEG |
| **Stream** | Python generator + Flask `Response()` | Send JPEG frames as MJPEG stream |
| **Display** | Browser MJPEG player | Render stream natively |

---

### Pipeline 2: Optional FFmpeg → MediaMTX RTSP Stream

This is **optional** and separate from the dashboard feed.

```
┌─────────────────┐
│ Flask route     │
│ /api/stream/start│
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────┐
│ _build_ffmpeg_cmd(params)       │
│ Construct FFmpeg command with:  │
│ - Current resolution (width x height)
│ - Current bitrate (e.g. 4000k)  │
│ - Current FPS (e.g. 30)         │
│ - H.264 codec + ultrafast preset│
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│ subprocess.Popen(ffmpeg)        │
│                                 │
│ FFmpeg reads MJPEG on stdin     │
│ (from video_processing_thread)  │
│ Rescales + re-encodes to H.264  │
│ Streams to rtsp://localhost:8554│
└────────┬────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│ MediaMTX RTSP server            │
│ (optional, external process)    │
└─────────────────────────────────┘
```

---

## How Resolution & FPS Switching Works

### Negotiation Loop (Every 1 second)

```
negotiation_thread() in node.py:
  1. Collect person_count, importance from all nodes
  2. Rank nodes by person_count and importance
  3. Decide tier: NEW_QUALITY = "LOW" | "MEDIUM" | "HIGH"
  4. If tier changed:
       Call _apply_encoder_params(quality)
         ├─ Update state.encoder_params with new resolution/fps/bitrate
         ├─ Update state.quality
         └─ Restart FFmpeg if it was running
```

### Frame Processing (Every frame, ~25+ fps)

```
video_processing_thread():
  while True:
    1. cap.read() → raw_frame
    2. with state.lock:
         encoder_params = dict(state.encoder_params)
         ◄── Reads latest tier decision
    3. if raw_frame.size != encoder_params['width/height']:
         display_frame = cv2.resize(raw_frame, 
                                    (encoder_params['width'], 
                                     encoder_params['height']))
    4. cv2.imencode(display_frame) → JPEG bytes
    5. state.latest_frame = JPEG bytes
         ◄── MJPEG generator reads this next
```

### MJPEG Playback (Browser receives frames)

```
generate_frames() generator:
  while True:
    1. with state.lock:
         frame_bytes = state.latest_frame
         fps = state.encoder_params['fps']
    2. yield JPEG frame over MJPEG protocol
    3. time.sleep(1.0 / fps)
         ◄── If fps changes from 10 to 30,
             this sleep changes from 100ms to 33ms
```

---

## Example: Person enters frame → tier changes LOW to HIGH

### t=0s: No person (LOW quality)

```
state.encoder_params = {
  'resolution': '480p',
  'width': 854, 'height': 480,
  'fps': 10,
  'bitrate_kbps': 500
}

Browser sees: 854x480 @ 10 fps
```

### t=1s: Person detected, ranked #1, tier rises to HIGH

```
negotiation_thread() decides:
  new_quality = "HIGH"
  _apply_encoder_params("HIGH")
  
state.encoder_params = {
  'resolution': '1080p',
  'width': 1920, 'height': 1080,
  'fps': 30,
  'bitrate_kbps': 4000
}
```

### t=1.1s: Video processing thread reads new params

```
video_processing_thread():
  raw_frame = cap.read()  # Still 1920x1080 from camera
  encoder_params = {1920, 1080, 30, ...}
  # No resize needed if camera is already 1920x1080
  display_frame = raw_frame
  JPEG = cv2.imencode(display_frame)
  state.latest_frame = JPEG
```

### t=1.2s: MJPEG generator paces at new FPS

```
generate_frames():
  fps = 30  # Changed from 10
  yield JPEG
  time.sleep(1.0/30)  # 33ms, faster than before
  
Browser now receives:
  1920x1080 @ 30 fps
```

---

## Why Two Pipelines?

1. **Dashboard feed (Pipeline 1)** is for the browser UI:
   - Light, low-overhead
   - Uses native MJPEG
   - Resizes in Python (OpenCV)
   - No external process needed

2. **FFmpeg → RTSP (Pipeline 2)** is optional, for external tools:
   - Can be used by third-party RTSP clients
   - Re-encodes to H.264 for compatibility
   - Uses more CPU but gives better codec control
   - Only runs if you manually call `/api/stream/start`

---

## Summary

| Component | Handles |
|---|---|
| **OpenCV (cv2)** | Capture, resize, encode to JPEG |
| **MJPEG** | Browser-native streaming format |
| **FFmpeg** | Optional H.264 re-encoding for RTSP clients |
| **Python generator** | Paces MJPEG output by FPS |
| **Negotiation loop** | Decides tier every 1 second |
| **Browser** | Receives MJPEG, plays natively |

**Resolution change is seamless because:**
- Video processing thread continuously reads the latest `encoder_params`
- Frames are resized on-the-fly in OpenCV
- MJPEG generator immediately sends the new size
- Browser's MJPEG player adapts automatically
