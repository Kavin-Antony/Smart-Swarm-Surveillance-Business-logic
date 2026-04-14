# Smart Swarm Surveillance - Project Summary

## 1) Project Overview
This project is a decentralized smart surveillance platform that prioritizes critical video streams across multiple edge nodes without a central bandwidth controller.

The design combines:
- Edge AI detection per node (person detection + motion awareness)
- Peer-to-peer importance exchange over MQTT
- Decentralized bandwidth negotiation and adaptive quality selection
- Live monitoring dashboards and multi-camera video wall playback

Based on the presentation (`STREAMING1.pptx`) and code, the core objective is to reduce latency, preserve privacy (edge-side processing), improve resilience, and use network bandwidth more efficiently in distributed camera environments.

---

## 2) Architecture Split
Per your project split:
- Backend: everything in the root folder (`Smart-Swarm-Surveillance-Business-logic-main`), excluding the frontend subfolder
- Frontend: `Smart-Swarm-Surveillance-Frontend/`

---

## 3) Backend Summary (Root Folder)

### 3.1 Backend Tech Stack
- Python 3.12
- Flask (HTTP dashboard/API)
- Ultralytics YOLO (`yolov8n.pt`) for edge inference
- OpenCV + NumPy for frame processing and motion metric
- Paho MQTT for peer coordination
- FFmpeg for webcam-to-RTSP pushing
- MediaMTX for RTSP/WebRTC/HLS serving
- Mosquitto as MQTT broker
- Nix/devenv for reproducible setup

### 3.2 Backend Functionalities
- Node startup and runtime configuration via environment variables and API startup payload.
- Video ingestion from webcam or RTSP source.
- Periodic object detection with YOLO and person-class filtering.
- Motion factor calculation using frame differencing.
- Importance score computation from:
  - person count
  - average confidence
  - motion factor
- MQTT publish/subscribe loop for peer score exchange.
- Decentralized bandwidth negotiation every second.
- Quality tier mapping (LOW, MEDIUM, HIGH) with bitrate/FPS/resolution profiles.
- MJPEG local preview stream and box overlay metadata for UI.
- Optional RTSP output stream via FFmpeg into local MediaMTX.
- Liveness/metrics/status endpoints for observability.
- Peer timeout handling and automatic re-allocation on node failure.

### 3.3 Backend Features
- Fully decentralized negotiation (no central orchestrator).
- Threaded pipeline separation:
  - video analytics thread
  - MQTT thread
  - negotiation thread
  - Flask main thread
- Automatic MQTT reconnect behavior.
- Dynamic topic rebuild based on node identity.
- Runtime startup mode through dashboard (`/api/start`) or `AUTO_START` env.
- Bounding-box telemetry exposed to UI.
- Development scripts via `devenv.nix` (`setup`, `run`, `mtx`).

### 3.4 Backend Performance Characteristics
- Detection is frame-skipped (`DETECTION_INTERVAL`) to reduce CPU load.
- Negotiation cadence is 1 Hz, giving near-real-time adaptation with low control overhead.
- Status polling from dashboard occurs at high frequency (200 ms), yielding responsive UI but higher API call volume.
- Streaming adaptation uses coarse quality tiers, which is robust and lightweight but less granular than continuous bitrate control.
- Fault response is driven by `NODE_TIMEOUT` (default ~8s), balancing false positives vs failover speed.
- `yolov8n` default favors edge CPU feasibility and lower inference cost.

---

## 4) Frontend Summary (`Smart-Swarm-Surveillance-Frontend/`)

### 4.1 Frontend Tech Stack
- Elixir / Phoenix 1.8
- Phoenix LiveView
- Ash Framework + AshPostgres + AshAuthentication
- PostgreSQL (camera and auth persistence)
- Tailwind CSS + daisyUI
- JavaScript with LiveView hooks
- `hls.js` for HLS playback fallback
- MediaMTX REST API integration through Elixir services

### 4.2 Frontend Functionalities
- User authentication routes and sessions.
- Camera management interface:
  - add camera (name + RTSP URL)
  - validate RTSP URL format
  - validate stream reachability via `ffprobe`
  - delete camera
- Automatic stream key generation (`cam-001`, `cam-002`, ...).
- MediaMTX path lifecycle management tied to camera CRUD.
- Video Wall page with multi-camera selection and responsive grid modes (1x1 to 4x4).
- Real-time stream playback in browser:
  - primary: WebRTC WHEP
  - fallback: HLS (hls.js or native)
- Resolution badges, loading/error overlays, retry logic.
- Camera status tracking (online/offline/error) using periodic MediaMTX health checks.

### 4.3 Frontend Features
- PubSub-driven UI updates for camera list/status changes.
- Serialized MediaMTX operations via a dedicated GenServer manager to avoid race conditions.
- Startup sync and retry strategy with exponential backoff when MediaMTX API is unavailable.
- YAML fallback path-writing when MediaMTX REST API is down.
- Dashboard/admin/dev tooling in development mode.
- Modern themed UI with responsive layouts for camera administration and wall monitoring.

### 4.4 Frontend Performance Characteristics
- LiveView minimizes full-page reloads and keeps updates incremental.
- WebRTC-first playback reduces live latency relative to segment-based HLS.
- HLS low-latency settings provide resilience for unsupported WebRTC paths.
- MediaMTX manager serialization favors consistency and reliability under concurrent camera operations.
- Health checks run on a 30-second cadence, reducing backend overhead while still refreshing status periodically.

---

## 5) End-to-End Feature Matrix
- Decentralized adaptive bandwidth allocation: implemented in Python backend.
- Edge AI scene scoring: implemented in Python backend (YOLO + motion).
- Multi-camera registry and management: implemented in Phoenix frontend domain.
- Real-time browser wall playback: implemented in Phoenix LiveView + JS hooks.
- Stream orchestration/storage bridge: implemented through MediaMTX manager + health monitor.
- Fault tolerance (peer timeout and retries): implemented in both backend negotiation and frontend MediaMTX manager.

---

## 6) Alignment with Presentation (`STREAMING1.pptx`)
The implemented code aligns strongly with the presentation themes:
- Decentralized surveillance and edge processing
- Importance-aware adaptive streaming
- Peer coordination over a shared messaging fabric
- Fault tolerance via peer absence detection and re-negotiation

Notable implementation choices vs slides:
- Current code uses YOLOv8 (`yolov8n.pt`) rather than YOLOv11.
- Current adaptive control is quality-tier based and updates encoder parameters in software state; full low-level dynamic encoder control is partially staged for deeper pipeline integration.
- MQTT is the active peer bus in code.

---

## 7) Practical Strengths and Constraints

### Strengths
- Clear decentralized control loop with practical edge tooling.
- Strong modular split between node analytics, coordination, and dashboarding.
- Good operational resilience with retries, stale-peer pruning, and fallback paths.
- Full-stack observability through status endpoints and UI indicators.

### Constraints / Improvement Opportunities
- Dashboard status polling at 200 ms can be heavy at scale.
- Stream key generation based on count can race under high concurrency (despite identity constraint protection).
- Encoder adaptation is currently tiered/discrete rather than fully continuous.
- Production hardening (security, auth boundaries, deployment profiles) can be expanded for large-scale real deployments.

---

## 8) Conclusion
This project is a well-structured decentralized surveillance platform that combines edge AI analytics, P2P-style coordination, and adaptive streaming. The root backend implements the autonomous node intelligence and negotiation loop, while the `Smart-Swarm-Surveillance-Frontend` subfolder provides camera lifecycle management and low-latency monitoring UX on top of MediaMTX.
