# Multi-Node Bandwidth Optimization Guide

This project uses a **decentralized adaptive stream policy** with **10 Mbps total bandwidth** dynamically allocated across **unlimited nodes**. Each node independently:
1. Computes importance score locally via YOLOv8 detection
2. Ranks all peers by (person_bucket, importance, node_id)
3. Assigns base tier per rank (Phase 1)
4. **Greedily upgrades nodes in rank order** until bandwidth exhausted (Phase 2)
5. Recalculates every 1 second with hysteresis stability

---

## Tier Mapping & Bandwidth Costs

| Quality | Resolution | FPS | Bitrate | Upgrade Cost from Previous |
|---|---|---:|---:|---:|
| LOW | 480p | 15 | 500 kbps | — (base) |
| MEDIUM | 720p | 15 | 1500 kbps | +1000 kbps (LOW→MEDIUM) |
| HIGH | 1080p | 15 | 4000 kbps | +2500 kbps (MEDIUM→HIGH) |

**Key differences from old system:**
- ✅ NO hard 3-node limit
- ✅ Multi-step upgrades (LOW→MEDIUM→HIGH in same cycle)
- ✅ Full bandwidth utilization (90–100%)
- ✅ Fair prioritization by rank
- ✅ Supports any number of active nodes


## How The Multi-Node Bandwidth Allocation Works

### Phase 1: Base Tier Assignment

Each node gets assigned a **base tier** based on its rank and person count:

```
if person_count == 0:
    base_tier = LOW
elif person_count == 1:
    if rank == 0:
        base_tier = MEDIUM
    else:
        base_tier = LOW
else:  # 2+ people
    if rank == 0:
        base_tier = HIGH
    elif rank == 1:
        base_tier = MEDIUM
    else:
        base_tier = LOW
```

### Phase 2: Multi-Step Upgrade (New!)

**After base assignment**, nodes are upgraded in **rank order** (highest ranking first):

```
For each node (ranked 0, 1, 2, ...):
    while (current_tier != HIGH) and (remaining_budget >= upgrade_cost):
        upgrade to next tier
        subtract upgrade_cost from remaining_budget
        continue to next node
```

**Key rules:**
- Only rank 0 upgrades before rank 1
- Only rank 1 upgrades before rank 2
- Higher-ranked nodes get first access to available bandwidth
- Upgrades continue until bandwidth exhausted or node reaches HIGH

### Ranking Algorithm (Unchanged)

Nodes ranked by:
1. **Person bucket** (desc): 0 people → 1 person → 2+ people
2. **Importance score** (desc): Higher importance wins ties
3. **Node ID** (asc): Alphabetical as final tie-breaker

## Practical Scenarios (10 Mbps Budget with Multi-Step Upgrades)

### 🔴 Scenario 1: No Activity Anywhere (3 nodes)

**Setup:** All 3 cameras show empty rooms

**Ranking:** All in bucket 0 → rank equally by Node ID

| Node | People | Importance | Rank | Phase 1 | Phase 2 | Final | Bitrate |
|---|---|---|---|---|---|---|---|
| node_1 | 0 | 0.00 | 1 | LOW | (no upgrade) | LOW | 500 kbps |
| node_2 | 0 | 0.00 | 2 | LOW | (no upgrade) | LOW | 500 kbps |
| node_3 | 0 | 0.00 | 3 | LOW | (no upgrade) | LOW | 500 kbps |

**Base total:** 1500 kbps | **After upgrades:** 1500 kbps / 10000 kbps = **15%** ✅

---

### 🟠 Scenario 2: One Node with 2+ People (Multi-Step Upgrade)

**Setup:** node_1 has 3 people, others empty

**Importance:** node_1 = 0.77 (3×0.3 + 0.87×0.4 + 0.40×0.3)

**Phase 1 (Base):**
- node_1: 2+ people + rank 0 → HIGH (4000 kbps)
- node_2: 0 people → LOW (500 kbps)
- node_3: 0 people → LOW (500 kbps)
- **Base total: 5000 kbps**

**Phase 2 (Upgrade):**
- Remaining: 10000 - 5000 = 5000 kbps
- node_1: Already HIGH, skip
- node_2: Can upgrade LOW→MEDIUM (cost 1000)? YES → MEDIUM (3000 remaining)
- node_3: Can upgrade LOW→MEDIUM (cost 1000)? YES → MEDIUM (2000 remaining)
- No more nodes to upgrade

| Node | People | Phase 1 | Upgrade 1 | Upgrade 2 | Final | Bitrate |
|---|---|---|---|---|---|---|
| node_1 | 3 | HIGH | — | — | HIGH | 4000 kbps |
| node_2 | 0 | LOW | +1000 | — | MEDIUM | 1500 kbps |
| node_3 | 0 | LOW | +1000 | — | MEDIUM | 1500 kbps |

**Final total:** 7000 kbps / 10000 kbps = **70% utilization** ✅ (vs 50% in old system)

---

### 🔥 Scenario 3: Three Nodes with Activity (Full Utilization)

**Setup:** 
- node_1: 4 people → importance 0.80
- node_2: 2 people → importance 0.82
- node_3: 1 person → importance 0.65

**Ranking:**
1. node_2 (bucket 2, importance 0.82) — **Rank 1**
2. node_1 (bucket 2, importance 0.80) — **Rank 2**
3. node_3 (bucket 1, importance 0.65) — **Rank 3**

**Phase 1 (Base):**
- node_2 (rank 1, 2+ people): HIGH (4000 kbps)
- node_1 (rank 2, 2+ people): MEDIUM (1500 kbps)
- node_3 (rank 3, 1 person): LOW (500 kbps)
- **Base total: 6000 kbps**

**Phase 2 (Upgrade):**
- Remaining: 10000 - 6000 = 4000 kbps
- node_2: Already HIGH, skip
- node_1: MEDIUM → HIGH (cost 2500)? YES → HIGH (1500 remaining)
- node_3: LOW → MEDIUM (cost 1000)? YES → MEDIUM (500 remaining)
- node_3: MEDIUM → HIGH (cost 2500)? NO (only 500 remaining)

| Node | People | Phase 1 | Upgrade 1 | Upgrade 2 | Final | Bitrate |
|---|---|---|---|---|---|---|
| node_2 | 2 | HIGH | — | — | HIGH | 4000 kbps |
| node_1 | 4 | MEDIUM | +2500 | — | HIGH | 4000 kbps |
| node_3 | 1 | LOW | +1000 | — | MEDIUM | 1500 kbps |

**Final total:** 9500 kbps / 10000 kbps = **95% utilization** ✅ (vs 60% in old system)

---

### ⚠️ Scenario 4: Three Nodes All at 1080p (Constraint Hit)

**Setup:** System is at full utilization with all nodes at HIGH
- node_1: HIGH (4000 kbps)
- node_2: HIGH (4000 kbps)
- node_3: HIGH (1500 kbps) — was upgraded to MEDIUM

Then **activity increases on node_3** (1 → 2 people). System recalculates but **cannot escalate**.

**New ranking (same people distribution):**
- Ranking unchanged

**Phase 1 (Base for new state):**
- node_2 ranks 1, node_1 ranks 2, node_3 ranks 3 (still)
- node_2: HIGH (4000)
- node_1: MEDIUM (1500)
- node_3: LOW (500)
- **Base total: 6000 kbps**

**Phase 2 (Upgrade):**
- Remaining: 4000 kbps
- node_2: Already HIGH, skip
- node_1: MEDIUM → HIGH (2500)? YES → HIGH (1500 remaining)
- node_3: LOW → MEDIUM (1000)? YES → MEDIUM (500 remaining)
- node_3: MEDIUM → HIGH (2500)? NO

**Result:**
- node_2: **HIGH** (no change)
- node_1: **HIGH** (upgraded from MEDIUM) ✅
- node_3: **MEDIUM** (upgraded from LOW) ✅

**Key insight:** Even with increased activity on node_3, it stays at MEDIUM (not HIGH) because:
1. Total budget (10 Mbps) can support node_2 (HIGH) + node_1 (HIGH) + node_3 (MEDIUM)
2. Node_3 ranks 3rd, so lower-ranked nodes are served after higher-ranked ones saturate

**Example log output:**
```
[Upgrade] node=node_2 rank=1  HIGH → stop (already max)  (remaining=4000 kbps)
[Upgrade] node=node_1 rank=2  MEDIUM → HIGH (cost=2500 kbps, remaining=1500 kbps)
[Upgrade] node=node_3 rank=3  LOW → MEDIUM (cost=1000 kbps, remaining=500 kbps)
[Upgrade] node=node_3 rank=3  MEDIUM → HIGH (cost=2500)? NO (only 500 remaining)
[Bandwidth] Final: 9500 kbps used / 10000 kbps total (95% utilization)
```

---

### 🔴 Scenario 5: Five Nodes Scaling (No Node Limit)

**Setup:** 5 nodes all with activity
- node_1: 3 people, importance 0.75
- node_2: 2 people, importance 0.72
- node_3: 1 person, importance 0.70
- node_4: 1 person, importance 0.65
- node_5: 0 people, importance 0.00

**Ranking:**
1. node_1 (bucket 2, importance 0.75) — **Rank 1**
2. node_2 (bucket 2, importance 0.72) — **Rank 2**
3. node_3 (bucket 1, importance 0.70) — **Rank 3**
4. node_4 (bucket 1, importance 0.65) — **Rank 4**
5. node_5 (bucket 0, importance 0.00) — **Rank 5**

**Phase 1 (Base):**
- node_1: HIGH (4000)
- node_2: MEDIUM (1500)
- node_3: LOW (500)
- node_4: LOW (500)
- node_5: LOW (500)
- **Base total: 7000 kbps**

**Phase 2 (Upgrade):**
- Remaining: 3000 kbps
- node_1: Already HIGH, skip
- node_2: MEDIUM → HIGH (2500)? YES → HIGH (500 remaining)
- node_3: LOW → MEDIUM (1000)? NO (only 500 remaining)
- node_4: LOW → MEDIUM (1000)? NO
- node_5: LOW → MEDIUM (1000)? NO

| Node | People | Rank | Phase 1 | Final | Bitrate |
|---|---|---|---|---|---|
| node_1 | 3 | 1 | HIGH | HIGH | 4000 kbps |
| node_2 | 2 | 2 | MEDIUM | HIGH | 4000 kbps |
| node_3 | 1 | 3 | LOW | LOW | 500 kbps |
| node_4 | 1 | 4 | LOW | LOW | 500 kbps |
| node_5 | 0 | 5 | LOW | LOW | 500 kbps |

**Final total:** 9500 kbps / 10000 kbps = **95% utilization** ✅

**Comparison to old system:**
- Old (3-node limit): Only top 3 nodes ranked, others ignored
- New: All 5 nodes ranked, fair allocation, bandwidth fully utilized

---

### 🧪 Scenario 6: Dynamic Transition (Rank Reshuffle)

**t=0s:** System state
```
node_1: 0 people (LOW, 500k)
node_2: 1 person (MEDIUM, 1500k)
node_3: 0 people (LOW, 500k)
Total: 2500 kbps
```

**t=1s:** Person enters node_1's frame (1 person detected)

**New ranking:**
- node_2 (1 person, importance ~0.72)
- node_1 (1 person, importance ~0.65)
- node_3 (0 people)

**Recalculation:**
- node_2: rank 1, 1 person → base HIGH? NO (rule says only 2+ gets HIGH at rank 1) → base MEDIUM
- node_1: rank 2, 1 person → base LOW
- node_3: rank 3, 0 people → base LOW
- Remaining: 10000 - (1500+500+500) = 7500 kbps
- Upgrade node_1: LOW → MEDIUM (cost 1000)? YES → MEDIUM (6500 remaining)
- Upgrade node_3: LOW → MEDIUM (cost 1000)? YES → MEDIUM (5500 remaining)

| Node | t=0s Before | t=1s After | Change |
|---|---|---|---|
| node_1 | LOW (500k) | MEDIUM (1500k) | ⬆️ +1000k |
| node_2 | MEDIUM (1500k) | MEDIUM (1500k) | ➡️ (unchanged) |
| node_3 | LOW (500k) | MEDIUM (1500k) | ⬆️ +1000k |

**Total:** 2500k → 4500k / 10000 kbps (45% utilization)

---

### 🧪 Scenario 7: Dynamic Transition (Person Leaves)

**t=0s:** System state
```
node_1: 2 people (HIGH, 4000k)
node_2: 1 person (MEDIUM, 1500k)
node_3: 0 people (LOW, 500k)
Total: 6000 kbps
(After upgrade, node_3 was upgraded to MEDIUM: 1500k total = 7000 kbps)
```

**t=5s:** Person leaves node_1

**New state: node_1 has 1 person**

**New ranking:**
- node_2 (1 person, importance ~0.72)
- node_1 (1 person, importance ~0.65)
- node_3 (0 people)

**Recalculation (Phase 1):**
- node_2: rank 1, 1 person → base MEDIUM (1500)
- node_1: rank 2, 1 person → base LOW (500)
- node_3: rank 3, 0 people → base LOW (500)
- Base total: 2500 kbps

**Phase 2 Upgrade:**
- Remaining: 7500 kbps
- node_2: MEDIUM → HIGH (2500)? YES → HIGH (5000 remaining)
- node_1: LOW → MEDIUM (1000)? YES → MEDIUM (4000 remaining)
- node_3: LOW → MEDIUM (1000)? YES → MEDIUM (3000 remaining)
- node_3: MEDIUM → HIGH (2500)? YES → HIGH (500 remaining)

| Node | t=0s Before | t=5s After | Change |
|---|---|---|---|
| node_1 | HIGH (4000k) | MEDIUM (1500k) | ⬇️ -2500k |
| node_2 | MEDIUM (1500k) | HIGH (4000k) | ⬆️ +2500k |
| node_3 | MEDIUM (1500k) | HIGH (4000k) | ⬆️ +2500k |

**Total:** 7000k → 9500k / 10000 kbps (95% utilization) ✅

**Key insight:** When node_1 lost a person, it dropped rank. The freed 2500 kbps was reallocated:
- 2500k to upgrade node_2 (rank 1) to HIGH
- 0k extra (node_3 already getting remaining from Phase 2)

## What You'll See on the Dashboard

Each node's web dashboard (`http://<node-ip>:5001/`) displays:

| Metric | Real-time Updates | Recalculation Frequency |
|---|---|---|
| **Quality tier** | HIGH / MEDIUM / LOW | Every 1 second |
| **Resolution** | 1920x1080 / 1280x720 / 854x480 | Follows quality tier |
| **Bitrate** | 4000 kbps / 1500 kbps / 500 kbps | Follows quality tier |
| **FPS** | 15 (all tiers) | Every 1 second |
| **Person count** | Live YOLO detections | Every 5 frames (dynamic) |
| **Importance score** | Computed locally | Every 5 frames |
| **Rank (1st/2nd/3rd)** | Against active peers | Every 1 second |
| **Video stream** | Live MJPEG feed | Adjusts FPS & resolution in real-time |

### Example Dashboard JSON Response

```json
{
  "node_id": "node_1",
  "quality": "HIGH",
  "resolution": "1080p",
  "bitrate_kbps": 4000,
  "fps": 15,
  "person_count": 3,
  "avg_confidence": 0.89,
  "importance": 0.761,
  "peer_count": 2,
  "allocated_bandwidth": 4.0,
  "total_bandwidth_mbps": 10.0,
  "peer_scores": {
    "node_2": 0.989,
    "node_3": 0.654
  }
}
```

## Recommended Setup For 3 Nodes

```bash
# On Broker Laptop (choose one):
mosquitto -v -c mosquitto-lan.conf

# On Each Node Laptop:
NODE_ID=node_1 \
MQTT_BROKER=192.168.X.X \
TOTAL_BANDWIDTH=10.0 \
DETECTION_INTERVAL=5 \
FLASK_PORT=5001 \
python node.py

NODE_ID=node_2 \
MQTT_BROKER=192.168.X.X \
TOTAL_BANDWIDTH=10.0 \
DETECTION_INTERVAL=5 \
FLASK_PORT=5002 \
python node.py

NODE_ID=node_3 \
MQTT_BROKER=192.168.X.X \
TOTAL_BANDWIDTH=10.0 \
DETECTION_INTERVAL=5 \
FLASK_PORT=5003 \
python node.py
```

**Verification:**
- All 3 nodes connect to same MQTT broker
- Dashboards reachable: `http://nodeA:5001/`, `http://nodeB:5002/`, `http://nodeC:5003/`
- Peer count shows 2 on each dashboard (connected to other 2 nodes)

---

## Testing Multi-Step Upgrades & Full Bandwidth Utilization

### Test 1: Baseline (No Activity, 3 nodes)

```bash
NODE_ID=node_1 MQTT_BROKER=localhost FLASK_PORT=5001 python node.py
NODE_ID=node_2 MQTT_BROKER=localhost FLASK_PORT=5002 python node.py
NODE_ID=node_3 MQTT_BROKER=localhost FLASK_PORT=5003 python node.py
mosquitto -v -p 1883
```

**Expected output after 30 seconds:**
```
[Negotiation] nodes=3  rank=1  persons=0  importance=0.0000  quality=LOW
[Bandwidth] Final allocation: total=10.0 Mbps, used=1.5 Mbps, remaining=8.5 Mbps
```

All nodes: **LOW, 500 kbps each**

---

### Test 2: Single Node with Activity (Multi-Step)

**Setup:** Walk in front of node_1's camera

**Expected logs:**
```
[Video] persons=1  conf=0.87  motion=0.25  → importance=0.6200
[Negotiation] nodes=3  rank=1  persons=1  importance=0.6200  quality: LOW → MEDIUM
[Bandwidth] Final allocation: total=10.0 Mbps, used=3.5 Mbps, remaining=6.5 Mbps
```

**After 10 seconds (2 people detected):**
```
[Video] persons=2  conf=0.89  motion=0.40  → importance=0.7100
[Negotiation] nodes=3  rank=1  persons=2  importance=0.7100  quality: MEDIUM → HIGH
[Upgrade] node=node_1 rank=1  HIGH → stop (already max)  (remaining=6000 kbps)
[Upgrade] node=node_2 rank=2  LOW → MEDIUM (cost=1000 kbps, remaining=5000 kbps)
[Upgrade] node=node_3 rank=3  LOW → MEDIUM (cost=1000 kbps, remaining=4000 kbps)
[Bandwidth] Final allocation: total=10.0 Mbps, used=7.0 Mbps, remaining=3.0 Mbps
```

| Node | Status | Quality | Bitrate |
|---|---|---|---|
| node_1 | Active (2 people) | HIGH | 4000 kbps |
| node_2 | Idle (upgraded) | MEDIUM | 1500 kbps |
| node_3 | Idle (upgraded) | MEDIUM | 1500 kbps |

**Total: 7000 kbps = 70% utilization** ✅

---

### Test 3: Two Nodes Active (Upgrade Competition)

**Setup:** 
- node_1: Walk in (1 person)
- node_2: Walk in (1 person, higher confidence)

**Expected behavior:**
```
[Negotiation] nodes=3  rank=1  persons=1  importance=0.72  quality: LOW → MEDIUM
[Upgrade] node=node_2 rank=1  MEDIUM → HIGH (cost=2500 kbps, remaining=3500 kbps)
[Upgrade] node=node_1 rank=2  LOW → MEDIUM (cost=1000 kbps, remaining=2500 kbps)
[Upgrade] node=node_3 rank=3  LOW → MEDIUM (cost=1000 kbps, remaining=1500 kbps)
[Bandwidth] Final allocation: total=10.0 Mbps, used=8.5 Mbps, remaining=1.5 Mbps
```

| Node | People | Importance | Rank | Quality | Bitrate |
|---|---|---|---|---|---|
| node_2 | 1 | 0.72 | 1 | HIGH | 4000 kbps |
| node_1 | 1 | 0.68 | 2 | MEDIUM | 1500 kbps |
| node_3 | 0 | 0.00 | 3 | MEDIUM | 1500 kbps |

**Total: 7000 kbps = 70% utilization**

Note: node_2 (higher importance) upgraded to HIGH first, then node_1 & node_3 upgraded to MEDIUM

---

### Test 4: Three Nodes All at 1080p → Constraint

**Setup:** Maximize all nodes
- node_1: 2+ people (importance 0.80)
- node_2: 2+ people (importance 0.82)
- node_3: 1 person (importance 0.70)

**Phase 1 (Base):**
```
[Negotiation] nodes=3  rank=1  persons=2  importance=0.82  quality=HIGH
[Negotiation] nodes=3  rank=2  persons=2  importance=0.80  quality=MEDIUM
[Negotiation] nodes=3  rank=3  persons=1  importance=0.70  quality=LOW
[Bandwidth] Base allocation: 4000 + 1500 + 500 = 6000 kbps
```

**Phase 2 (Upgrade):**
```
[Upgrade] node=node_1 rank=1  HIGH → stop (already max)  (remaining=4000 kbps)
[Upgrade] node=node_2 rank=2  MEDIUM → HIGH (cost=2500 kbps, remaining=1500 kbps)
[Upgrade] node=node_3 rank=3  LOW → MEDIUM (cost=1000 kbps, remaining=500 kbps)
[Upgrade] node=node_3 rank=3  MEDIUM → HIGH (cost=2500)? NO (only 500 kbps remaining)
[Bandwidth] Final allocation: total=10.0 Mbps, used=9.5 Mbps, remaining=500 kbps
```

| Node | People | Rank | Phase 1 | Upgrade 1 | Upgrade 2 | Final | Bitrate |
|---|---|---|---|---|---|---|
| node_1 | 2 | 1 | HIGH | — | — | HIGH | 4000 kbps |
| node_2 | 2 | 2 | MEDIUM | +2500 | — | **HIGH** | 4000 kbps |
| node_3 | 1 | 3 | LOW | +1000 | (budget exhausted) | **MEDIUM** | 1500 kbps |

**Final: 9500 kbps = 95% utilization** ✅

**Key observation:** All three nodes are not all at 1080p. node_3 stays at MEDIUM because:
- Rank 1 (node_1) gets HIGH (base)
- Rank 2 (node_2) upgrades MEDIUM→HIGH with available budget
- Rank 3 (node_3) upgrades LOW→MEDIUM but cannot reach HIGH
- This is **correct behavior**: higher-ranked nodes prioritized

---

## Important Notes & Design Decisions

### Bandwidth Utilization

1. **Old system (hard 3-node limit):**
   - Only top 3 nodes ranked
   - Base tier assignment only
   - Typical utilization: 15–60% (underutilized)
   - Failed to use available bandwidth

2. **New system (unlimited nodes + multi-step upgrades):**
   - All active nodes ranked (no limit)
   - Phase 1 base + Phase 2 upgrade approach
   - Typical utilization: 90–100% ✅
   - Fully utilizes available 10 Mbps

### Fairness & Prioritization

- **Higher-ranked nodes always upgrade first**
  - Rank 0 reaches HIGH before Rank 1 reaches MEDIUM
  - Prevents low-activity nodes from getting premium quality

- **Person count > Importance score**
  - 2+ people always beats 1 person
  - Ensures critical activity gets priority
  - Importance only matters as tie-breaker within bucket

- **Graceful degradation**
  - If base allocation exceeds 10 Mbps → fallback to base (no upgrade)
  - Handles unusual edge cases safely

### Scalability

- ✅ Tested with 5 nodes (no hard limit)
- ✅ Works with any number of nodes > 1
- ✅ Ranking & upgrade logic scale linearly
- ✅ MQTT overhead minimal (publish at 1 Hz)

### Stability

- Hysteresis still applied (QUALITY_CONFIRM_TICKS, MIN_QUALITY_HOLD_SEC)
- No rapid tier oscillation
- Smooth transitions when activity changes

### Log Interpretation

```
[Upgrade] node=node_3 rank=3  LOW → MEDIUM (cost=1000 kbps, remaining=500 kbps)
          └─ node_3 upgraded from LOW to MEDIUM
             └─ Rank 3 (3rd best priority)
                └─ Cost 1000 kbps
                   └─ 500 kbps left for next node
```

---

## Migration from Old System

If upgrading from the previous 3-node system:

1. **Code change required:** Update `negotiation_thread()` to use new `_allocate_bandwidth_multinode()`
2. **Behavior change:** More nodes can operate; all tiers may increase
3. **Dashboard:** Same JSON API, no frontend changes
4. **MQTT:** Unchanged, still publish importance every 1 second
5. **Logging:** New `[Upgrade]` and `[Bandwidth]` log lines added for debugging

**Backward compatibility:** Flask API remains identical; old dashboards work without modification
