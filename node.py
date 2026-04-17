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
from urllib.parse import urlparse
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
TOPIC_PUBLISH_IMPORTANCE: str = f"vms/node/{NODE_ID}/importance"
TOPIC_PUBLISH_STATUS: str = f"vms/node/{NODE_ID}/status"
TOPIC_SUBSCRIBE_IMPORTANCE: str = "vms/node/+/importance"
TOPIC_SUBSCRIBE_STATUS: str = "vms/node/+/status"

# ── Bandwidth ─────────────────────────────────────────────────────────────
TOTAL_BANDWIDTH: float = float(os.environ.get("TOTAL_BANDWIDTH", "7.5"))  # Mbps
MAX_COORDINATED_NODES: int = int(os.environ.get("MAX_COORDINATED_NODES", "3"))
# 0.0 => pure fair-share, 1.0 => pure priority-share
PRIORITY_WEIGHT: float = float(os.environ.get("PRIORITY_WEIGHT", "0.7"))

# ── Fault Tolerance ───────────────────────────────────────────────────────
NODE_TIMEOUT: float = float(os.environ.get("NODE_TIMEOUT", "8.0"))   # seconds
QUALITY_CONFIRM_TICKS: int = int(os.environ.get("QUALITY_CONFIRM_TICKS", "1"))
MIN_QUALITY_HOLD_SEC: float = float(os.environ.get("MIN_QUALITY_HOLD_SEC", "2.0"))

# ── Flask ─────────────────────────────────────────────────────────────────
FLASK_PORT: int = int(os.environ.get("FLASK_PORT", "5001"))
AUTO_START: bool = os.environ.get("AUTO_START", "0") == "1"

# ── YOLO ─────────────────────────────────────────────────────────────────
YOLO_MODEL: str = os.environ.get("YOLO_MODEL", "yolov8n.pt")
YOLO_PERSON_CLASS: int = 0          # COCO class 0 = person
YOLO_CONF_THRESHOLD: float = 0.4

# ── Streaming quality tiers ───────────────────────────────────────────────
QUALITY_TIERS: dict = {
    "LOW":    {"resolution": "480p",  "width": 854,  "height": 480,  "bitrate_kbps": 500,  "fps": 15},
    "MEDIUM": {"resolution": "720p",  "width": 1280, "height": 720,  "bitrate_kbps": 1500, "fps": 15},
    "HIGH":   {"resolution": "1080p", "width": 1920, "height": 1080, "bitrate_kbps": 4000, "fps": 15},
}
RTSP_EXPORT_BASE: str = os.environ.get("RTSP_EXPORT_BASE", "rtsp://localhost:8554").rstrip("/")
RTSP_EXPORT_PATHS: dict = {
    "LOW": "webcam_low",
    "MEDIUM": "webcam_med",
    "HIGH": "webcam_high",
}

# ── Worker startup guard ───────────────────────────────────────────────────
WORKERS_STARTED: bool = False
WORKERS_LOCK = threading.Lock()


def _rebuild_topics() -> None:
    """Refresh MQTT topics when NODE_ID changes at runtime startup config."""
    global TOPIC_PUBLISH_IMPORTANCE, TOPIC_PUBLISH_STATUS
    global TOPIC_SUBSCRIBE_IMPORTANCE, TOPIC_SUBSCRIBE_STATUS
    TOPIC_PUBLISH_IMPORTANCE = f"vms/node/{NODE_ID}/importance"
    TOPIC_PUBLISH_STATUS = f"vms/node/{NODE_ID}/status"
    TOPIC_SUBSCRIBE_IMPORTANCE = "vms/node/+/importance"
    TOPIC_SUBSCRIBE_STATUS = "vms/node/+/status"


def _rtsp_url_for_quality(quality: str) -> str:
    path = RTSP_EXPORT_PATHS.get(quality, "webcam_med")
    return f"{RTSP_EXPORT_BASE}/{path}"


def _build_ffmpeg_cmd(params: dict, output_url: str) -> list[str]:
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
        "-g",
        str(max(10, int(params["fps"]) * 2)),
        "-keyint_min",
        str(max(10, int(params["fps"]))),
        "-sc_threshold",
        "0",
        "-b:v",
        f"{params['bitrate_kbps']}k",
        "-rtsp_transport",
        "tcp",
        "-f",
        "rtsp",
        output_url,
    ]


def _start_ffmpeg_process(params: dict, output_url: str) -> subprocess.Popen:
    """Start ffmpeg process for streaming to MediaMTX using current quality tier."""
    cmd = _build_ffmpeg_cmd(params, output_url)
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
    """Compatibility shim: three publishers stay up, no restart required on tier switch."""
    log.debug(
        "[Stream] Keeping RTSP publishers running; active quality switched to %s",
        params.get("resolution", "unknown"),
    )


def _person_bucket(person_count: int) -> int:
    """Convert a raw person count into a priority bucket."""
    if person_count <= 0:
        return 0
    if person_count == 1:
        return 1
    return 2


def _scaled_display_size(frame_width: int, frame_height: int, params: dict) -> tuple[int, int]:
    """Return the dashboard display size for the current encoder params."""
    target_width = int(params.get("width", frame_width) or frame_width)
    target_height = int(params.get("height", frame_height) or frame_height)
    return max(1, target_width), max(1, target_height)
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
        # ── Peer status: {node_id: {"quality": str, "allocated_bandwidth": float, ...}} ─
        self.peer_statuses: dict = {}

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
        self.ffmpeg_procs: dict[str, subprocess.Popen] = {}


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

        with state.lock:
            encoder_params = dict(state.encoder_params)

        display_width, display_height = _scaled_display_size(
            frame.shape[1],
            frame.shape[0],
            encoder_params,
        )

        if (frame.shape[1], frame.shape[0]) != (display_width, display_height):
            display_frame = cv2.resize(
                frame,
                (display_width, display_height),
                interpolation=cv2.INTER_AREA,
            )
        else:
            display_frame = frame
        
        # ── Encode Frames for dashboard and RTSP exporters ─────────────
        ret_jpg, buffer = cv2.imencode('.jpg', display_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        frame_bytes = buffer.tobytes() if ret_jpg else b''
        ret_raw_jpg, raw_buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        raw_frame_bytes = raw_buffer.tobytes() if ret_raw_jpg else b''

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
            procs = dict(state.ffmpeg_procs) if state.is_streaming else {}

        write_failed = False
        if raw_frame_bytes and procs:
            for quality, proc in procs.items():
                if proc.poll() is not None or proc.stdin is None:
                    log.warning("[Video] FFmpeg process for %s died unexpectedly.", quality)
                    write_failed = True
                    break
                try:
                    proc.stdin.write(raw_frame_bytes)
                except Exception as exc:
                    log.error("[Video] Failed to write to FFmpeg stdin (%s): %s", quality, exc)
                    write_failed = True
                    break

        if write_failed:
            stopped = _stop_rtsp_exporters()
            log.warning("[Video] RTSP exporters stopped after write failure (%d process(es)).", stopped)

    cap.release()


# ===========================================================================
# [E] MQTT HANDLER THREAD  (Thread 2)
# ===========================================================================

def _on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        log.info("[MQTT] Connected to broker %s:%d", MQTT_BROKER, MQTT_PORT)
        client.subscribe(TOPIC_SUBSCRIBE_IMPORTANCE)
        client.subscribe(TOPIC_SUBSCRIBE_STATUS)
        log.info("[MQTT] Subscribed → %s", TOPIC_SUBSCRIBE_IMPORTANCE)
        log.info("[MQTT] Subscribed → %s", TOPIC_SUBSCRIBE_STATUS)
    else:
        log.error("[MQTT] Connection failed, rc=%d", rc)


def _on_message(client, userdata, msg):
    """
    Receive peer importance/status messages and update shared peer state.
    Stale nodes are pruned by the negotiation thread.
    """
    try:
        payload = json.loads(msg.payload.decode())
        peer_id = payload.get("node_id")
        if not peer_id or peer_id == NODE_ID:
            return

        ts = float(payload.get("timestamp", time.time()))

        if msg.topic.endswith("/importance"):
            importance = float(payload.get("importance", 0.0))
            person_count = int(payload.get("person_count", 0))
            with state.lock:
                state.peer_scores[peer_id] = {
                    "importance": importance,
                    "person_count": person_count,
                    "ts": ts,
                }
            log.debug("[MQTT] Peer importance: %s → %.4f", peer_id, importance)
            return

        if msg.topic.endswith("/status"):
            quality = str(payload.get("quality", "LOW"))
            allocated_bandwidth = float(payload.get("allocated_bandwidth", 0.0))
            bitrate_kbps = int(payload.get("bitrate_kbps", 0))
            with state.lock:
                state.peer_statuses[peer_id] = {
                    "quality": quality,
                    "allocated_bandwidth": allocated_bandwidth,
                    "bitrate_kbps": bitrate_kbps,
                    "ts": ts,
                }
            log.debug(
                "[MQTT] Peer status: %s → quality=%s bw=%.3f Mbps",
                peer_id,
                quality,
                allocated_bandwidth,
            )
            return

    except Exception as exc:
        log.warning("[MQTT] Bad message on %s: %s", msg.topic, exc)


def _on_disconnect(client, userdata, rc, properties=None):
    log.warning("[MQTT] Disconnected (rc=%d). Will auto-reconnect.", rc)


def mqtt_thread():
    """
    MQTT communication thread.
    Publishes own importance and status at 1 Hz.
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
            person_count = state.person_count
            quality = state.quality
            allocated_bandwidth = state.allocated_bandwidth
            bitrate_kbps = int(state.encoder_params.get("bitrate_kbps", 0))

        ts_now = time.time()

        payload_importance = json.dumps({
            "node_id":   NODE_ID,
            "importance": round(score, 6),
            "person_count": int(person_count),
            "timestamp":  ts_now,
        })
        payload_status = json.dumps({
            "node_id": NODE_ID,
            "quality": quality,
            "allocated_bandwidth": round(float(allocated_bandwidth), 4),
            "bitrate_kbps": bitrate_kbps,
            "timestamp": ts_now,
        })

        client.publish(TOPIC_PUBLISH_IMPORTANCE, payload_importance, qos=0, retain=False)
        client.publish(TOPIC_PUBLISH_STATUS, payload_status, qos=0, retain=False)
        log.debug("[MQTT] Published importance=%.4f", score)

        time.sleep(2.0)


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
    log.info(
        "[Encoder] → %s | %s | %d kbps | %d fps",
        quality,
        params["resolution"],
        params["bitrate_kbps"],
        params["fps"],
    )


def _select_quality_tier(person_count: int, rank: int) -> str:
    """Select the BASE quality tier for this node given its rank among active nodes."""
    if person_count <= 0:
        return "LOW"
    if person_count == 1:
        return "MEDIUM" if rank == 0 else "LOW"
    if rank == 0:
        return "HIGH"
    if rank == 1:
        return "MEDIUM"
    return "LOW"


def _upgrade_tier(current_quality: str) -> str | None:
    """
    Return the next tier up, or None if already at max.
    LOW → MEDIUM → HIGH → None
    """
    if current_quality == "LOW":
        return "MEDIUM"
    elif current_quality == "MEDIUM":
        return "HIGH"
    return None


def _tier_cost_delta(from_tier: str, to_tier: str) -> float:
    """
    Return the additional bandwidth (kbps) needed to upgrade from one tier to another.
    Used for upgrade phase budget accounting.
    """
    from_rate = QUALITY_TIERS[from_tier]["bitrate_kbps"]
    to_rate = QUALITY_TIERS[to_tier]["bitrate_kbps"]
    return max(0, to_rate - from_rate)


def _allocate_bandwidth_multinode(
    all_nodes: dict,
    total_bandwidth_kbps: float,
) -> dict[str, str]:
    """
    Allocate quality tiers to all nodes using 2-phase allocation:
      Phase 1: Assign base tier to each node based on rank
      Phase 2: Greedily upgrade nodes in rank order until bandwidth exhausted

    Args:
        all_nodes: {node_id: {"importance": float, "person_count": int}}
        total_bandwidth_kbps: Total available bandwidth in kbps

    Returns:
        {node_id: final_quality_tier}
    """
    # ── Rank all nodes ───────────────────────────────────────────────
    ranked_nodes = sorted(
        all_nodes.items(),
        key=lambda item: (
            -_person_bucket(int(item[1]["person_count"])),
            -float(item[1]["importance"]),
            item[0],
        ),
    )

    # ── Special case: all nodes have 0 people ───────────────────────
    if ranked_nodes and all(_person_bucket(int(node[1]["person_count"])) == 0 for node in ranked_nodes):
        return {node_id: "LOW" for node_id, _ in ranked_nodes}

    # ── Phase 1: Assign base tier to each node ─────────────────────
    node_qualities = {}
    for rank, (node_id, node_data) in enumerate(ranked_nodes):
        person_count = int(node_data["person_count"])
        base_quality = _select_quality_tier(person_count, rank)
        node_qualities[node_id] = base_quality

    # ── Phase 2: Greedily upgrade nodes in rank order ───────────────
    total_base_kbps = sum(
        QUALITY_TIERS[node_qualities[node_id]]["bitrate_kbps"]
        for node_id, _ in ranked_nodes
    )

    if total_base_kbps > total_bandwidth_kbps:
        # Base allocation exceeds budget → fallback to base without upgrade
        log.debug(
            "[Bandwidth] Base allocation (%.1f Mbps) exceeds total (%.1f Mbps); no upgrades applied",
            total_base_kbps / 1000.0,
            total_bandwidth_kbps / 1000.0,
        )
        return node_qualities

    remaining_kbps = total_bandwidth_kbps - total_base_kbps

    for rank, (node_id, _) in enumerate(ranked_nodes):
        current_quality = node_qualities[node_id]

        # Try to upgrade this node as much as possible before moving to next
        while True:
            next_tier = _upgrade_tier(current_quality)
            if next_tier is None:
                # Already at HIGH
                break

            upgrade_cost = _tier_cost_delta(current_quality, next_tier)
            if remaining_kbps >= upgrade_cost:
                node_qualities[node_id] = next_tier
                remaining_kbps -= upgrade_cost
                current_quality = next_tier
                log.debug(
                    "[Upgrade] node=%s rank=%d  %s → %s  (cost=%.1f kbps, remaining=%.1f kbps)",
                    node_id,
                    rank + 1,
                    current_quality.replace(next_tier, ""),  # old quality
                    next_tier,
                    upgrade_cost,
                    remaining_kbps,
                )
            else:
                # Not enough remaining for this upgrade, try next node
                break

    log.debug(
        "[Bandwidth] Final allocation: total=%.1f Mbps, used=%.1f Mbps, remaining=%.1f Mbps",
        total_bandwidth_kbps / 1000.0,
        (total_bandwidth_kbps - remaining_kbps) / 1000.0,
        remaining_kbps / 1000.0,
    )

    return node_qualities

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
            # Keep peer status map consistent with live peers.
            state.peer_statuses.pop(pid, None)
            log.warning("[Negotiation] Peer timed out and removed: %s", pid)
            removed = True

        stale_status_only = [
            pid for pid, info in state.peer_statuses.items()
            if now - float(info.get("ts", 0.0)) > NODE_TIMEOUT
        ]
        for pid in stale_status_only:
            if pid not in state.peer_scores:
                del state.peer_statuses[pid]
                removed = True
    return removed


def negotiation_thread():
    """
    Decentralised bandwidth negotiation loop with multi-node upgrade support.

    Every 1 second:
      1. Prune stale peers.
      2. Gather all scores (own + peers) — NO node limit.
      3. Rank all nodes by (person_bucket, importance, node_id).
      4. Phase 1: Assign base tier to each node.
      5. Phase 2: Greedily upgrade nodes in rank order until bandwidth exhausted.
      6. Decide quality tier for this node and update encoder if changed.

    No central controller — each node independently runs this same logic.
    """
    previous_quality: str = ""
    candidate_quality: str = ""
    candidate_ticks: int = 0
    last_switch_ts: float = 0.0

    while True:
        time.sleep(2.0)

        # ── 1. Prune timed-out peers ──────────────────────────────────
        _prune_stale_peers()

        # ── 2. Collect scores ─────────────────────────────────────────
        with state.lock:
            my_score   = state.importance_score
            my_person_count = state.person_count
            peer_data  = dict(state.peer_scores)  # shallow copy

        all_nodes = {
            NODE_ID: {
                "importance": my_score,
                "person_count": my_person_count,
            }
        }
        for pid, info in peer_data.items():
            all_nodes[pid] = {
                "importance": float(info.get("importance", 0.0)),
                "person_count": int(info.get("person_count", 0)),
            }

        # ── 3. Rank ALL nodes (no MAX_COORDINATED_NODES limit) ────────
        ranked_nodes = sorted(
            all_nodes.items(),
            key=lambda item: (
                -_person_bucket(int(item[1]["person_count"])),
                -float(item[1]["importance"]),
                item[0],
            ),
        )

        node_count = len(ranked_nodes)

        # Find my rank after sorting
        ranked_lookup = {node_id: rank for rank, (node_id, _) in enumerate(ranked_nodes)}
        my_rank = ranked_lookup.get(NODE_ID, node_count)

        # ── 4 & 5. Allocate bandwidth (Phase 1 base + Phase 2 upgrade) ─
        total_bandwidth_kbps = TOTAL_BANDWIDTH * 1000.0
        node_qualities = _allocate_bandwidth_multinode(all_nodes, total_bandwidth_kbps)

        # Get this node's assigned quality
        new_quality = node_qualities.get(NODE_ID, "LOW")

        # ── 6. Update state and apply hysteresis ──────────────────────
        params = QUALITY_TIERS[new_quality]
        allocated = params["bitrate_kbps"] / 1000.0

        with state.lock:
            state.allocated_bandwidth = allocated

        now = time.time()
        should_switch = False

        if new_quality != previous_quality:
            if new_quality == candidate_quality:
                candidate_ticks += 1
            else:
                candidate_quality = new_quality
                candidate_ticks = 1

            hold_ok = (previous_quality == "") or ((now - last_switch_ts) >= MIN_QUALITY_HOLD_SEC)
            confirm_ok = candidate_ticks >= max(1, QUALITY_CONFIRM_TICKS)
            should_switch = hold_ok and confirm_ok
        else:
            candidate_quality = ""
            candidate_ticks = 0

        if should_switch:
            log.info(
                "[Negotiation] nodes=%d  rank=%d  persons=%d  importance=%.4f  quality: %s → %s",
                node_count,
                my_rank + 1 if my_rank < node_count else 0,
                my_person_count,
                my_score,
                previous_quality or "N/A",
                new_quality,
            )
            _apply_encoder_params(new_quality)
            previous_quality = new_quality
            candidate_quality = ""
            candidate_ticks = 0
            last_switch_ts = now

        log.debug(
            "[Negotiation] nodes=%d  my_score=%.4f  rank=%d  quality=%s  candidate=%s(%d)",
            node_count,
            my_score,
            my_rank + 1 if my_rank < node_count else 0,
            new_quality,
            candidate_quality or "-",
            candidate_ticks,
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
        peer_status_summary = {
            pid: {
                "quality": str(v.get("quality", "LOW")),
                "allocated_bandwidth": round(float(v.get("allocated_bandwidth", 0.0)), 4),
                "bitrate_kbps": int(v.get("bitrate_kbps", 0)),
                "last_seen_ts": float(v.get("ts", 0.0)),
            }
            for pid, v in state.peer_statuses.items()
        }
        return {
            "started":             state.started,
            "node_id":             NODE_ID,
            "importance":          round(state.importance_score, 6),
            "person_count":        state.person_count,
            "avg_confidence":      round(state.avg_confidence, 4),
            "motion_factor":       round(state.motion_factor, 4),
            "peer_scores":         peer_summary,
            "peer_statuses":       peer_status_summary,
            "peer_count":          len(peer_summary),
            "allocated_bandwidth": round(state.allocated_bandwidth, 4),
            "quality":             state.quality,
            "resolution":          state.encoder_params.get("resolution", "N/A"),
            "bitrate_kbps":        state.encoder_params.get("bitrate_kbps", 0),
            "fps":                 state.encoder_params.get("fps", 0),
            "max_nodes":           MAX_COORDINATED_NODES,
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
            "active_rtsp_url":     _rtsp_url_for_quality(state.quality),
            "rtsp_outputs": {
                q: _rtsp_url_for_quality(q) for q in ("LOW", "MEDIUM", "HIGH")
            },
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

    # OpenCV cannot ingest WebRTC signaling/playback URLs directly.
    # Keep UI compatibility by translating common MediaMTX WebRTC URLs to RTSP.
    if source_type == "webrtc" and rtsp_url:
        parsed = urlparse(rtsp_url)
        if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port == 8889:
            stream_path = parsed.path.rstrip("/") or "/camera"
            rtsp_url = f"rtsp://{parsed.hostname}:8554{stream_path}"
            log.warning(
                "[Startup] WebRTC URL provided for OpenCV input; using RTSP URL instead: %s",
                rtsp_url,
            )
        source_type = "rtsp"

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
        return jsonify({"ok": False, "error": "Stream URL is required for RTSP/WebRTC mode."}), 400

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

    export_started, export_status = _start_rtsp_export()
    if not export_started and export_status != "already_streaming":
        log.warning("[Stream] RTSP auto-start failed: %s", export_status)

    return jsonify(
        {
            "ok": True,
            "started": True,
            "already_running": not just_started,
            "rtsp_export": "started" if export_started else export_status,
        }
    )

def generate_frames():
    """Generator for MJPEG stream."""
    while True:
        with state.lock:
            frame_bytes = state.latest_frame
            fps = int(state.encoder_params.get("fps", 25) or 25)
            
        if frame_bytes:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        else:
            time.sleep(0.1)

        time.sleep(max(0.01, 1.0 / max(1, fps)))

@app.route("/video_feed", methods=["GET"])
def video_feed():
    """Serves the MJPEG video feed."""
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


def _start_rtsp_export() -> tuple[bool, str]:
    """Start three FFmpeg RTSP exporters if not already running."""
    with state.lock:
        if state.is_streaming:
            return False, "already_streaming"

    started: dict[str, subprocess.Popen] = {}

    try:
        for quality in ("LOW", "MEDIUM", "HIGH"):
            params = QUALITY_TIERS[quality].copy()
            output_url = _rtsp_url_for_quality(quality)
            started[quality] = _start_ffmpeg_process(params, output_url)

        with state.lock:
            state.ffmpeg_procs = started
            state.is_streaming = True

        log.info(
            "[Stream] Started RTSP publishers: LOW=%s, MEDIUM=%s, HIGH=%s",
            _rtsp_url_for_quality("LOW"),
            _rtsp_url_for_quality("MEDIUM"),
            _rtsp_url_for_quality("HIGH"),
        )
        return True, "started"
    except Exception as exc:
        for proc in started.values():
            _stop_ffmpeg_process(proc)
        log.error("[Stream] Failed to start FFmpeg: %s", exc)
        return False, str(exc)


def _stop_rtsp_exporters() -> int:
    """Stop all active RTSP exporter processes."""
    with state.lock:
        procs = list(state.ffmpeg_procs.values())
        state.ffmpeg_procs = {}
        state.is_streaming = False

    for proc in procs:
        _stop_ffmpeg_process(proc)
    return len(procs)


@app.route("/api/stream/start", methods=["POST"])
def stream_start():
    started, status = _start_rtsp_export()
    if started:
        return jsonify({"status": "started"}), 200
    if status == "already_streaming":
        return jsonify({"status": "already_streaming"}), 400
    return jsonify({"error": status}), 500

@app.route("/api/stream/stop", methods=["POST"])
def stream_stop():
    with state.lock:
        if not state.is_streaming or not state.ffmpeg_procs:
            return jsonify({"status": "not_streaming"}), 400

    stopped = _stop_rtsp_exporters()
    log.info("[Stream] Stopped FFmpeg RTSP publishers (%d process(es))", stopped)
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
