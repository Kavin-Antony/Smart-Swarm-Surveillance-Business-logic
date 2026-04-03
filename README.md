# Swarm Surveillance — Decentralized Edge Node

A fully decentralized smart surveillance system where each laptop runs an independent camera node. Nodes communicate over MQTT to negotiate bandwidth dynamically — **no central controller required**.

---

## Architecture

```
Laptop A (node_1)          Laptop B (node_2)          Laptop C (node_3)
┌──────────────────┐       ┌──────────────────┐       ┌──────────────────┐
│  Thread 1: YOLO  │       │  Thread 1: YOLO  │       │  Thread 1: YOLO  │
│  Thread 2: MQTT  │──────▶│  Thread 2: MQTT  │──────▶│  Thread 2: MQTT  │
│  Thread 3: Nego  │◀──────│  Thread 3: Nego  │◀──────│  Thread 3: Nego  │
│  Main: Flask     │       │  Main: Flask     │       │  Main: Flask     │
└──────────────────┘       └──────────────────┘       └──────────────────┘
         │                          │                          │
         └──────────────────────────┴──────────────────────────┘
                         MQTT broker (any laptop)
                         vms/node/+/importance
```

Every node independently:
1. Detects persons with YOLOv8
2. Computes an importance score
3. Publishes score over MQTT
4. Reads peer scores and negotiates bandwidth share
5. Adapts streaming quality accordingly

---

## File Layout

```
swarm_surveilance_bussiness_logic/
├── node.py           ← main edge node (this repo)
├── devenv.nix        ← dev environment (Python + mosquitto)
├── .env.example      ← environment variable template
└── README.md
```

---

## Quick Start

### Step 1 — Enter devenv (on every laptop)

```bash
devenv shell
```

### Step 2 — Install Python deps (first time only)

```bash
setup
```

### Step 3 — Choose ONE laptop as MQTT broker

```bash
# On the broker laptop only:
mosquitto -d -p 1883
# or with config:
mosquitto -c /etc/mosquitto/mosquitto.conf -d
```

> All laptops must be on the **same Wi-Fi / LAN**.

### Step 4 — Configure each laptop

Copy `.env.example` to `.env` and edit:

```bash
cp .env.example .env
nano .env   # or vim, code, etc.
```

| Variable | Node 1 | Node 2 | Node 3 |
|---|---|---|---|
| `NODE_ID` | `node_1` | `node_2` | `node_3` |
| `MQTT_BROKER` | `192.168.1.50` | `192.168.1.50` | `192.168.1.50` |
| `RTSP_URL` | `rtsp://cam1/...` | `rtsp://cam2/...` | *(empty = webcam)* |
| `FLASK_PORT` | `5000` | `5001` | `5002` |
| `TOTAL_BANDWIDTH` | `10.0` | `10.0` | `10.0` |

### Step 5 — Run each node

```bash
run
```

The `run` script automatically loads `.env` if present.

---

## Manual env var usage (without .env file)

```bash
NODE_ID=node_1 \
MQTT_BROKER=192.168.1.50 \
RTSP_URL=rtsp://192.168.1.100:554/main \
TOTAL_BANDWIDTH=10.0 \
DETECTION_INTERVAL=5 \
FLASK_PORT=5001 \
python node.py
```

---

## REST Endpoints (per node)

| Endpoint | Description |
|---|---|
| `GET /` | Full node status JSON |
| `GET /metrics` | Same as `/` (Prometheus-ready stub) |
| `GET /health` | Liveness probe |

### Example response

```json
{
  "node_id": "node_1",
  "importance": 0.712,
  "person_count": 2,
  "avg_confidence": 0.874,
  "motion_factor": 0.198,
  "peer_scores": {
    "node_2": 0.431,
    "node_3": 0.155
  },
  "peer_count": 2,
  "allocated_bandwidth": 5.53,
  "quality": "HIGH",
  "resolution": "1080p",
  "bitrate_kbps": 4000,
  "fps": 30,
  "total_bandwidth_mbps": 10.0,
  "mqtt_broker": "192.168.1.50"
}
```

---

## Importance Score Formula

```
importance = min(1.0,
    person_count  × 0.3 +
    avg_confidence × 0.4 +
    motion_factor  × 0.3
)
```

- **person_count** — raw count of detected persons (scales sub-linearly beyond ~3)
- **avg_confidence** — mean YOLO detection confidence [0, 1]
- **motion_factor** — normalised pixel-difference metric [0, 1]

---

## Bandwidth Negotiation

```
total_score  = Σ importance_scores (all active nodes)

my_ratio     = my_score / total_score   # or 1/N if total == 0

my_bandwidth = my_ratio × TOTAL_BANDWIDTH
```

Quality mapping:

| Allocated bandwidth | Quality | Resolution | Bitrate | FPS |
|---|---|---|---|---|
| < 1 Mbps | LOW | 480p | 500 kbps | 10 |
| 1 – 3 Mbps | MEDIUM | 720p | 1500 kbps | 20 |
| > 3 Mbps | HIGH | 1080p | 4000 kbps | 30 |

---

## Fault Tolerance

- If a peer stops publishing for `NODE_TIMEOUT` seconds (default 8 s), it is removed.
- Bandwidth is immediately recalculated among remaining nodes.
- Nodes reconnect to MQTT automatically on disconnection.

---

## Tuning Tips

| Parameter | Recommended range | Effect |
|---|---|---|
| `DETECTION_INTERVAL` | 3–10 | Lower = more accurate, higher CPU |
| `TOTAL_BANDWIDTH` | match your uplink | e.g. 20 for gigabit LAN |
| `NODE_TIMEOUT` | 5–15 s | Lower = faster failover |
| `YOLO_MODEL` | `yolov8n` / `yolov8s` | nano is fastest on CPU |

---

## MQTT Topics

| Direction | Topic |
|---|---|
| Publish (own score) | `vms/node/{node_id}/importance` |
| Subscribe (all peers) | `vms/node/+/importance` |

Payload schema:
```json
{ "node_id": "node_1", "importance": 0.712, "timestamp": 1710000000.0 }
```

---

## Future Extension Points

### GStreamer encoder control
In `node.py`, replace `_apply_encoder_params()` body:
```python
# current: updates dict only (simulated)
# future: GStreamer dynamic pipeline update
encoder.set_property("bitrate", params["bitrate_kbps"] * 1000)
pipeline.get_by_name("caps-filter").set_property("caps", ...)
```

### ONVIF PTZ camera control
In `video_processing_thread()`, replace:
```python
cap = cv2.VideoCapture(source)
```
with an ONVIF session that returns an OpenCV-compatible stream handle.

---

## Dependencies

All installed via `setup` inside devenv:

```
flask
paho-mqtt>=2.0
ultralytics        ← includes YOLOv8 + torch
opencv-python
numpy
```

MQTT broker:
```
mosquitto          ← available via devenv (nixpkgs)
```
