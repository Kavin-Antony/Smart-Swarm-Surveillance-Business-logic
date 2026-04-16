#!/usr/bin/env python3
"""
=============================================================================
Decentralized Edge Node — Smart Swarm Surveillance System
=============================================================================
Author   : Swarm Node
Purpose  : Fully decentralized MQTT-based swarm camera node with:
           - YOLOv8 person detection
           - Importance scoring
           - Bandwidth negotiation
           - Adaptive streaming decisions
           - Flask monitoring dashboard
=============================================================================

Module Layout
─────────────
  [A] Configuration & Globals
  [B] Shared State
  [C] Importance Score Engine
  [D] Video Processing Thread  (Thread 1)
  [E] MQTT Handler Thread      (Thread 2)
  [F] Negotiation Loop Thread  (Thread 3)
  [G] Flask Dashboard          (Main Thread)
  [H] Entry Point
"""

# ===========================================================================
# Standard Library
# ===========================================================================
import os
import time
import json
import logging
import threading
import subprocess
from datetime import datetime

# ===========================================================================
# Third-party Imports
# ===========================================================================
import cv2
import numpy as np
from ultralytics import YOLO
import paho.mqtt.client as mqtt
from flask import Flask, jsonify, render_template, Response, request

# ===========================================================================
# [A] CONFIGURATION & GLOBALS
# ===========================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("SwarmNode")

# ── Identity ──────────────────────────────────────────────────────────────
NODE_ID: str = os.environ.get("NODE_ID", "node_1")

# ── Video Source ──────────────────────────────────────────────────────────
RTSP_URL: str = os.environ.get("RTSP_URL", "")          # empty → webcam
WEBCAM_INDEX: int = int(os.environ.get("WEBCAM_INDEX", "0"))
DETECTION_INTERVAL: int = int(os.environ.get("DETECTION_INTERVAL", "5"))

# ── MQTT ──────────────────────────────────────────────────────────────────
MQTT_BROKER: str = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT: int = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_KEEPALIVE: int = 60
TOPIC_PUBLISH: str = f"vms/node/{NODE_ID}/importance"
TOPIC_SUBSCRIBE: str = "vms/node/+/importance"

# ── Bandwidth ─────────────────────────────────────────────────────────────
TOTAL_BANDWIDTH: float = float(os.environ.get("TOTAL_BANDWIDTH", "10.0"))  # Mbps
# 0.0 => pure fair-share, 1.0 => pure priority-share
PRIORITY_WEIGHT: float = float(os.environ.get("PRIORITY_WEIGHT", "0.7"))

# ── Fault Tolerance ───────────────────────────────────────────────────────
NODE_TIMEOUT: float = float(os.environ.get("NODE_TIMEOUT", "8.0"))   # seconds

# ── Flask ─────────────────────────────────────────────────────────────────
FLASK_PORT: int = int(os.environ.get("FLASK_PORT", "5001"))
AUTO_START: bool = os.environ.get("AUTO_START", "0") == "1"

# ── YOLO ─────────────────────────────────────────────────────────────────
YOLO_MODEL: str = os.environ.get("YOLO_MODEL", "yolov8n.pt")
YOLO_PERSON_CLASS: int = 0          # COCO class 0 = person
YOLO_CONF_THRESHOLD: float = 0.4

# ── Streaming quality tiers ───────────────────────────────────────────────
QUALITY_TIERS: dict = {
    "LOW":    {"resolution": "480p",  "width": 854,  "height": 480,  "bitrate_kbps": 500,  "fps": 10},
    "MEDIUM": {"resolution": "720p",  "width": 1280, "height": 720,  "bitrate_kbps": 1500, "fps": 20},
    "HIGH":   {"resolution": "1080p", "width": 1920, "height": 1080, "bitrate_kbps": 4000, "fps": 30},
}

# ── Worker startup guard ───────────────────────────────────────────────────
WORKERS_STARTED: bool = False
WORKERS_LOCK = threading.Lock()


def _rebuild_topics() -> None:
    """Refresh MQTT topics when NODE_ID changes at runtime startup config."""
    global TOPIC_PUBLISH, TOPIC_SUBSCRIBE
    TOPIC_PUBLISH = f"vms/node/{NODE_ID}/importance"
    TOPIC_SUBSCRIBE = "vms/node/+/importance"


def _build_ffmpeg_cmd(params: dict) -> list[str]:
    """Build ffmpeg command from active encoder parameters."""
    return [
        "ffmpeg",
        "-y",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-r",
        str(params["fps"]),
        "-i",
        "-",
        "-vf",
        f"scale={params['width']}:{params['height']}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-b:v",
        f"{params['bitrate_kbps']}k",
        "-f",
        "rtsp",
        "rtsp://localhost:8554/webcam",
    ]


def _start_ffmpeg_process(params: dict) -> subprocess.Popen:
    """Start ffmpeg process for streaming to MediaMTX using current quality tier."""
    cmd = _build_ffmpeg_cmd(params)
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop_ffmpeg_process(proc: subprocess.Popen | None) -> None:
    """Gracefully stop ffmpeg process if present."""
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _restart_stream_with_params(params: dict) -> None:
    """Restart ffmpeg stream to apply updated bitrate/fps/resolution."""
    with state.lock:
        old_proc = state.ffmpeg_proc
        state.is_streaming = False
        state.ffmpeg_proc = None

    _stop_ffmpeg_process(old_proc)

    try:
        new_proc = _start_ffmpeg_process(params)
        with state.lock:
            state.ffmpeg_proc = new_proc
            state.is_streaming = True
        log.info(
            "[Stream] Reconfigured encoder → %s @ %dfps, %dkbps",
            params["resolution"],
            params["fps"],
            params["bitrate_kbps"],
        )
    except Exception as exc:
        log.error("[Stream] Failed to reconfigure ffmpeg stream: %s", exc)
# ===========================================================================
# [B] SHARED STATE  (thread-safe via locks)
# ===========================================================================

class SharedState:
    """
    Central mutable state shared across all threads.
    All access must go through `state_lock`.

    Future extension:
     - Replace `encoder_params` with GStreamer pipeline control
     - Replace camera source with ONVIF-managed PTZ camera
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.started: bool = False
        self.source_type: str = "rtsp" if RTSP_URL else "webcam"
        self.stream_source: str = RTSP_URL if RTSP_URL else f"webcam:{WEBCAM_INDEX}"

        # ── Importance score computed locally ──────────────────────────
        self.importance_score: float = 0.0

        # ── Latest detection results ───────────────────────────────────
        self.person_count: int = 0
        self.avg_confidence: float = 0.0
        self.motion_factor: float = 0.0

        # ── Peer scores: {node_id: {"importance": float, "ts": float}} ─
        self.peer_scores: dict = {}

        # ── Bandwidth allocation ───────────────────────────────────────
        self.allocated_bandwidth: float = TOTAL_BANDWIDTH  # Mbps
        self.quality: str = "HIGH"

        # ── Encoder parameters (GStreamer-ready placeholder) ────────────
        self.encoder_params: dict = QUALITY_TIERS["HIGH"].copy()

        # ── Frame stats for dashboard ──────────────────────────────────
        self.frame_count: int = 0
        self.last_frame_ts: float = 0.0

        # ── Frontend Visuals ───────────────────────────────────────────
        self.latest_frame: bytes = b''
        self.boxes: list = []
        self.frame_dims: tuple = (0, 0)
        
        # ── Stream Control ─────────────────────────────────────────────
        self.is_streaming: bool = False
        self.ffmpeg_proc: subprocess.Popen | None = None


state = SharedState()

# ===========================================================================
# [C] IMPORTANCE SCORE ENGINE
# ===========================================================================

def compute_importance(person_count: int, avg_conf: float, motion_factor: float) -> float:
    """
    Compute normalised importance score in [0.0, 1.0].

    Formula:
        importance = min(1.0,
            person_count  * 0.3 +
            avg_conf      * 0.4 +
            motion_factor * 0.3
        )

    Arguments:
        person_count  — number of detected persons in current frame
        avg_conf      — mean YOLO confidence of person detections
        motion_factor — normalised frame-difference metric [0, 1]

    Returns:
        float in [0.0, 1.0]
    """
    raw = (person_count * 0.3) + (avg_conf * 0.4) + (motion_factor * 0.3)
    return min(1.0, raw)


def _motion_metric(prev_gray: np.ndarray | None, curr_gray: np.ndarray) -> float:
    """
    Simple pixel-difference motion factor.
    Returns a value normalised to [0, 1].
    """
    if prev_gray is None:
        return 0.0
    diff = cv2.absdiff(prev_gray, curr_gray)
    mean_diff = float(np.mean(diff)) / 255.0   # 0-1
    # amplify slightly so small movement registers
    return min(1.0, mean_diff * 8.0)


# ===========================================================================
# [D] VIDEO PROCESSING THREAD  (Thread 1)
# ===========================================================================

def video_processing_thread():
    """
    Captures frames from RTSP or webcam, runs YOLOv8 every
    DETECTION_INTERVAL frames, updates SharedState.

    Future ONVIF hook:
        Replace `cv2.VideoCapture(source)` with an ONVIF PTZ-managed
        camera stream; all downstream logic remains unchanged.
    """
    log.info("[Video] Loading YOLO model: %s", YOLO_MODEL)
    model = YOLO(YOLO_MODEL)

    source = RTSP_URL if RTSP_URL else WEBCAM_INDEX
    log.info("[Video] Opening source: %s", source)

    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        log.error("[Video] Cannot open source '%s'. Exiting thread.", source)
        return

    frame_idx: int = 0
    prev_gray: np.ndarray | None = None

    while True:
        ret, frame = cap.read()
        if not ret:
            log.warning("[Video] Frame read failed — attempting reconnect…")
            time.sleep(1.0)
            cap.release()
            cap = cv2.VideoCapture(source)
            continue

        frame_idx += 1
        
        # ── Encode Frame for Frontend MJPEG Stream ─────────────────────
        ret_jpg, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        frame_bytes = buffer.tobytes() if ret_jpg else b''

        curr_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        motion = _motion_metric(prev_gray, curr_gray)
        prev_gray = curr_gray

        # ── Run YOLO only every DETECTION_INTERVAL frames ──────────────
        if frame_idx % DETECTION_INTERVAL == 0:
            results = model(frame, verbose=False, conf=YOLO_CONF_THRESHOLD)[0]

            person_boxes = [
                b for b in results.boxes
                if int(b.cls[0]) == YOLO_PERSON_CLASS
            ]
            p_count = len(person_boxes)
            confs = [float(b.conf[0]) for b in person_boxes]
            avg_conf = float(np.mean(confs)) if confs else 0.0
            
            # Extract box coordinates [x1, y1, x2, y2]
            current_boxes = []
            for b in person_boxes:
                try:
                    x1, y1, x2, y2 = b.xyxy[0].tolist()
                    current_boxes.append({"x1": round(x1, 2), "y1": round(y1, 2), "x2": round(x2, 2), "y2": round(y2, 2)})
                except Exception:
                    pass

            score = compute_importance(p_count, avg_conf, motion)

            with state.lock:
                state.person_count     = p_count
                state.avg_confidence   = avg_conf
                state.motion_factor    = motion
                state.importance_score = score
                state.frame_count      = frame_idx
                state.last_frame_ts    = time.time()
                state.boxes            = current_boxes
                state.frame_dims       = (frame.shape[1], frame.shape[0])

            log.info(
                "[Video] persons=%d  conf=%.3f  motion=%.3f  → importance=%.4f",
                p_count, avg_conf, motion, score,
            )
        
        # Always update latest_frame so the stream is continuous
        with state.lock:
            state.latest_frame = frame_bytes
            
            # Pipe to FFmpeg if streaming is active
            if state.is_streaming and state.ffmpeg_proc is not None:
                if state.ffmpeg_proc.poll() is None:
                    try:
                        state.ffmpeg_proc.stdin.write(frame_bytes)
                    except Exception as e:
                        log.error("[Video] Failed to write to FFmpeg stdin: %s", e)
                        state.is_streaming = False
                        state.ffmpeg_proc = None
                else:
                    log.warning("[Video] FFmpeg process died unexpectedly.")
                    state.is_streaming = False
                    state.ffmpeg_proc = None

    cap.release()


# ===========================================================================
# [E] MQTT HANDLER THREAD  (Thread 2)
# ===========================================================================

def _on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        log.info("[MQTT] Connected to broker %s:%d", MQTT_BROKER, MQTT_PORT)
        client.subscribe(TOPIC_SUBSCRIBE)
        log.info("[MQTT] Subscribed → %s", TOPIC_SUBSCRIBE)
    else:
        log.error("[MQTT] Connection failed, rc=%d", rc)


def _on_message(client, userdata, msg):
    """
    Receive peer importance messages and update the peer scores dict.
    Stale nodes are pruned by the negotiation thread.
    """
    try:
        payload = json.loads(msg.payload.decode())
        peer_id = payload.get("node_id")
        importance = float(payload.get("importance", 0.0))
        ts = float(payload.get("timestamp", time.time()))

        if peer_id and peer_id != NODE_ID:
            with state.lock:
                state.peer_scores[peer_id] = {
                    "importance": importance,
                    "ts": ts,
                }
            log.debug("[MQTT] Peer update: %s → %.4f", peer_id, importance)

    except Exception as exc:
        log.warning("[MQTT] Bad message on %s: %s", msg.topic, exc)


def _on_disconnect(client, userdata, rc, properties=None):
    log.warning("[MQTT] Disconnected (rc=%d). Will auto-reconnect.", rc)


def mqtt_thread():
    """
    MQTT communication thread.
    Publishes own importance score at 1 Hz.
    """
    client = mqtt.Client(
        client_id=f"swarm-{NODE_ID}",
        protocol=mqtt.MQTTv5,
    )
    client.on_connect    = _on_connect
    client.on_message    = _on_message
    client.on_disconnect = _on_disconnect

    while True:
        try:
            client.connect(MQTT_BROKER, MQTT_PORT, MQTT_KEEPALIVE)
            break
        except Exception as exc:
            log.error("[MQTT] Cannot connect: %s — retrying in 3s…", exc)
            time.sleep(3.0)

    client.loop_start()  # non-blocking network loop

    while True:
        with state.lock:
            score = state.importance_score

        payload = json.dumps({
            "node_id":   NODE_ID,
            "importance": round(score, 6),
            "timestamp":  time.time(),
        })
        client.publish(TOPIC_PUBLISH, payload, qos=0, retain=False)
        log.debug("[MQTT] Published importance=%.4f", score)

        time.sleep(1.0)


# ===========================================================================
# [F] NEGOTIATION + ADAPTATION LOOP  (Thread 3)
# ===========================================================================

def _resolve_quality(mbps: float) -> str:
    """Map allocated bandwidth (Mbps) → quality tier label."""
    if mbps < 1.0:
        return "LOW"
    elif mbps <= 3.0:
        return "MEDIUM"
    else:
        return "HIGH"


def _apply_encoder_params(quality: str):
    """
    Update shared encoder parameters without restarting the stream.

    Future GStreamer hook:
        Replace this function body with GStreamer dynamic pipeline
        property updates (e.g. `encoder.set_property("bitrate", ...)`)
        while keeping the calling interface identical.
    """
    params = QUALITY_TIERS[quality].copy()
    with state.lock:
        state.encoder_params = params
        state.quality = quality
        restart_stream = state.is_streaming
    log.info(
        "[Encoder] → %s | %s | %d kbps | %d fps",
        quality,
        params["resolution"],
        params["bitrate_kbps"],
        params["fps"],
    )

    if restart_stream:
        _restart_stream_with_params(params)

def _prune_stale_peers() -> bool:
    """
    Remove peers that haven't published within NODE_TIMEOUT seconds.
    Returns True if any peer was removed (triggers recalculation).
    """
    now = time.time()
    removed = False
    with state.lock:
        stale = [
            pid for pid, info in state.peer_scores.items()
            if now - info["ts"] > NODE_TIMEOUT
        ]
        for pid in stale:
            del state.peer_scores[pid]
            log.warning("[Negotiation] Peer timed out and removed: %s", pid)
            removed = True
    return removed


def negotiation_thread():
    """
    Decentralised bandwidth negotiation loop.

    Every 1 second:
      1. Prune stale peers.
      2. Gather all scores (own + peers).
      3. Compute my_share_ratio.
      4. Allocate bandwidth.
      5. Decide quality tier and update encoder if changed.

    No central controller — each node independently runs this same logic.
    """
    previous_quality: str = ""

    while True:
        time.sleep(1.0)

        # ── 1. Prune timed-out peers ──────────────────────────────────
        _prune_stale_peers()

        # ── 2. Collect scores ─────────────────────────────────────────
        with state.lock:
            my_score   = state.importance_score
            peer_data  = dict(state.peer_scores)  # shallow copy

        all_scores = {NODE_ID: my_score}
        all_scores.update({pid: v["importance"] for pid, v in peer_data.items()})

        total_score = sum(all_scores.values())
        node_count = max(len(all_scores), 1)

        # ── 3. Calculate share ratio (hybrid fair-share + priority) ──
        fair_ratio = 1.0 / node_count
        if total_score == 0.0:
            priority_ratio = fair_ratio
        else:
            priority_ratio = my_score / total_score

        alpha = max(0.0, min(1.0, PRIORITY_WEIGHT))
        my_share_ratio = ((1.0 - alpha) * fair_ratio) + (alpha * priority_ratio)

        # ── 4. Allocate bandwidth ─────────────────────────────────────
        allocated = my_share_ratio * TOTAL_BANDWIDTH

        with state.lock:
            state.allocated_bandwidth = allocated

        # ── 5. Quality adaptation ─────────────────────────────────────
        new_quality = _resolve_quality(allocated)

        if new_quality != previous_quality:
            log.info(
                "[Negotiation] share=%.2f%%  bw=%.2f Mbps  quality: %s → %s",
                my_share_ratio * 100,
                allocated,
                previous_quality or "N/A",
                new_quality,
            )
            _apply_encoder_params(new_quality)
            previous_quality = new_quality

        log.debug(
            "[Negotiation] nodes=%d  my_score=%.4f  total=%.4f  "
            "share=%.2f%%  alloc=%.2f Mbps  quality=%s",
            len(all_scores), my_score, total_score,
            my_share_ratio * 100, allocated, new_quality,
        )


# ===========================================================================
# [G] FLASK DASHBOARD  (Main Thread)
# ===========================================================================

app = Flask(__name__)

# silence Flask's default request logs to keep console clean
flask_log = logging.getLogger("werkzeug")
flask_log.setLevel(logging.WARNING)


def _build_status() -> dict:
    """Assemble current node status into a serialisable dict."""
    with state.lock:
        peer_summary = {
            pid: round(v["importance"], 6)
            for pid, v in state.peer_scores.items()
        }
        return {
            "started":             state.started,
            "node_id":             NODE_ID,
            "importance":          round(state.importance_score, 6),
            "person_count":        state.person_count,
            "avg_confidence":      round(state.avg_confidence, 4),
            "motion_factor":       round(state.motion_factor, 4),
            "peer_scores":         peer_summary,
            "peer_count":          len(peer_summary),
            "allocated_bandwidth": round(state.allocated_bandwidth, 4),
            "quality":             state.quality,
            "resolution":          state.encoder_params.get("resolution", "N/A"),
            "bitrate_kbps":        state.encoder_params.get("bitrate_kbps", 0),
            "fps":                 state.encoder_params.get("fps", 0),
            "frame_count":         state.frame_count,
            "last_updated":        datetime.utcfromtimestamp(
                                       state.last_frame_ts
                                   ).isoformat() + "Z" if state.last_frame_ts else None,
            "total_bandwidth_mbps": TOTAL_BANDWIDTH,
            "mqtt_broker":         MQTT_BROKER,
            "mqtt_port":           MQTT_PORT,
            "source_type":         state.source_type,
            "stream_source":       state.stream_source,
            "boxes":               state.boxes,
            "frame_dims":          state.frame_dims,
            "is_streaming":        state.is_streaming,
        }


@app.route("/", methods=["GET"])
def index():
    """Root endpoint — returns the Web Dashboard HTML."""
    return render_template("index.html")


@app.route("/api/status", methods=["GET"])
def api_status():
    """Returns node status as JSON, including bounding boxes."""
    return jsonify(_build_status())


def _start_workers_once() -> bool:
    """Start all worker threads exactly once."""
    global WORKERS_STARTED
    with WORKERS_LOCK:
        if WORKERS_STARTED:
            return False
        _start_daemon(video_processing_thread, "Thread-VideoYOLO")
        _start_daemon(mqtt_thread,             "Thread-MQTT")
        _start_daemon(negotiation_thread,      "Thread-Negotiation")
        WORKERS_STARTED = True
        return True


@app.route("/api/start", methods=["POST"])
def api_start():
    """Receive startup config from frontend and start processing workers."""
    global NODE_ID, MQTT_BROKER, MQTT_PORT, RTSP_URL, WEBCAM_INDEX

    payload = request.get_json(silent=True) or {}

    node_id = str(payload.get("node_id", "")).strip()
    mqtt_broker = str(payload.get("mqtt_broker", "")).strip()
    source_type = str(payload.get("source_type", "webcam")).strip().lower()
    rtsp_url = str(payload.get("rtsp_url", "")).strip()
    mqtt_port_val = payload.get("mqtt_port", MQTT_PORT)

    if not node_id:
        return jsonify({"ok": False, "error": "Node name is required."}), 400
    if not mqtt_broker:
        return jsonify({"ok": False, "error": "MQTT broker IP is required."}), 400

    try:
        mqtt_port = int(mqtt_port_val)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "MQTT port must be an integer."}), 400

    if mqtt_port < 1 or mqtt_port > 65535:
        return jsonify({"ok": False, "error": "MQTT port must be between 1 and 65535."}), 400
    if source_type not in {"webcam", "rtsp", "webrtc"}:
        return jsonify({"ok": False, "error": "Source type must be webcam or rtsp/webrtc."}), 400
    if source_type in {"rtsp", "webrtc"} and not rtsp_url:
        return jsonify({"ok": False, "error": "RTSP/WebRTC URL is required for stream mode."}), 400

    # Webcam page supports "start RTSP" by supplying a link.
    if source_type == "webcam" and rtsp_url:
        source_type = "rtsp"

    webcam_index_val = payload.get("webcam_index", WEBCAM_INDEX)
    try:
        webcam_index = int(webcam_index_val)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Webcam index must be an integer."}), 400

    NODE_ID = node_id
    MQTT_BROKER = mqtt_broker
    MQTT_PORT = mqtt_port
    WEBCAM_INDEX = webcam_index
    RTSP_URL = rtsp_url if source_type in {"rtsp", "webrtc"} else ""
    _rebuild_topics()

    with state.lock:
        state.started = True
        state.source_type = source_type
        state.stream_source = RTSP_URL if RTSP_URL else f"webcam:{WEBCAM_INDEX}"

    just_started = _start_workers_once()
    if just_started:
        log.info(
            "[Startup] Node=%s | MQTT=%s:%d | source=%s",
            NODE_ID,
            MQTT_BROKER,
            MQTT_PORT,
            state.stream_source,
        )

    return jsonify({"ok": True, "started": True, "already_running": not just_started})

def generate_frames():
    """Generator for MJPEG stream."""
    while True:
        with state.lock:
            frame_bytes = state.latest_frame
            
        if frame_bytes:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        else:
            time.sleep(0.1)
            
        time.sleep(0.04)  # throttle to ~25 FPS

@app.route("/video_feed", methods=["GET"])
def video_feed():
    """Serves the MJPEG video feed."""
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route("/api/stream/start", methods=["POST"])
def stream_start():
    with state.lock:
        if state.is_streaming:
            return jsonify({"status": "already_streaming"}), 400

        params = dict(state.encoder_params)

    try:
        proc = _start_ffmpeg_process(params)
        with state.lock:
            state.ffmpeg_proc = proc
            state.is_streaming = True
        log.info(
            "[Stream] Started FFmpeg stream to MediaMTX on rtsp://localhost:8554/webcam "
            "(%s, %dfps, %dkbps)",
            params["resolution"],
            params["fps"],
            params["bitrate_kbps"],
        )
        return jsonify({"status": "started"}), 200
    except Exception as e:
        log.error("[Stream] Failed to start FFmpeg: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/stream/stop", methods=["POST"])
def stream_stop():
    with state.lock:
        if not state.is_streaming or state.ffmpeg_proc is None:
            return jsonify({"status": "not_streaming"}), 400

        proc = state.ffmpeg_proc
        state.is_streaming = False
        state.ffmpeg_proc = None

    _stop_ffmpeg_process(proc)
    log.info("[Stream] Stopped FFmpeg stream")
    return jsonify({"status": "stopped"}), 200


@app.route("/metrics", methods=["GET"])
def metrics():
    """Prometheus-style metrics endpoint (JSON for now)."""
    return jsonify(_build_status())


@app.route("/health", methods=["GET"])
def health():
    """Liveness probe."""
    with state.lock:
        started = state.started
    return jsonify({"status": "ok", "started": started, "node_id": NODE_ID}), 200


# ===========================================================================
# [H] ENTRY POINT
# ===========================================================================

def _start_daemon(fn, name: str):
    """Create and start a daemon thread."""
    t = threading.Thread(target=fn, name=name, daemon=True)
    t.start()
    return t


def main():
    log.info("=" * 60)
    log.info("  Swarm Surveillance Node  ·  ID: %s", NODE_ID)
    log.info("  MQTT Broker : %s:%d", MQTT_BROKER, MQTT_PORT)
    log.info("  Bandwidth   : %.1f Mbps total", TOTAL_BANDWIDTH)
    log.info("  YOLO Model  : %s", YOLO_MODEL)
    log.info("  Flask port  : %d", FLASK_PORT)
    log.info("=" * 60)

    if AUTO_START:
        with state.lock:
            state.started = True
            state.source_type = "rtsp" if RTSP_URL else "webcam"
            state.stream_source = RTSP_URL if RTSP_URL else f"webcam:{WEBCAM_INDEX}"
        _start_workers_once()
        log.info("[Startup] AUTO_START=1 enabled: workers started from environment config")
    else:
        log.info("[Startup] Waiting for frontend setup at / before starting workers")

    # ── Main thread: Flask server ─────────────────────────────────────
    log.info("[Flask] Dashboard at http://0.0.0.0:%d/", FLASK_PORT)
    app.run(host="0.0.0.0", port=FLASK_PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    _rebuild_topics()
    main()
