# 3-Node Resolution Guide

This project uses a per-node adaptive stream policy. The resolution is not chosen by the number of cameras alone. It is chosen from the live detection state on each node, then ranked across the active 3-node group.

## Current Tier Mapping

| Quality | Resolution | FPS | Bitrate |
|---|---|---:|---:|
| LOW | 480p | 10 | 500 kbps |
| MEDIUM | 720p | 20 | 1500 kbps |
| HIGH | 1080p | 30 | 4000 kbps |

## How The 3-Node Policy Works

Each node computes:

1. `person_count`
2. `importance` score
3. peer scores from the other nodes

For the 3-node setup, the nodes are ranked by:

1. Higher person count first
2. Higher importance score next
3. Node ID as a final tie-breaker

Then the quality is assigned like this:

| Rank in group | Resulting quality |
|---|---|
| 1st | HIGH |
| 2nd | MEDIUM |
| 3rd | LOW |

If a node has no person detected, it drops to LOW.

If a node has exactly one person, it can rise to MEDIUM if it is the top-ranked active node.

If a node has more than one person, it can rise to HIGH if it is the top-ranked active node.

## Practical Examples

### Case 1: No person in any node

| Node | Person count | Resolution |
|---|---:|---|
| Node 1 | 0 | 480p |
| Node 2 | 0 | 480p |
| Node 3 | 0 | 480p |

### Case 2: One node sees one person

| Node | Person count | Resolution |
|---|---:|---|
| Node 1 | 1 | 720p |
| Node 2 | 0 | 480p |
| Node 3 | 0 | 480p |

If Node 2 has the better importance score, Node 2 gets 720p instead.

### Case 3: One node sees many people, others see none

| Node | Person count | Resolution |
|---|---:|---|
| Node 1 | 3 | 1080p |
| Node 2 | 0 | 480p |
| Node 3 | 0 | 480p |

### Case 4: Two nodes have activity

| Node | Person count | Typical result |
|---|---:|---|
| Node 1 | 4 | 1080p |
| Node 2 | 1 | 720p |
| Node 3 | 0 | 480p |

If Node 2 has the stronger importance score, it can become 1080p and Node 1 can fall back to 720p.

### Case 5: All 3 nodes have people

| Node | Person count | Typical result |
|---|---:|---|
| Node 1 | 5 | 1080p |
| Node 2 | 2 | 720p |
| Node 3 | 1 | 480p |

This gives the highest resolution to only one node when all 3 are competing.

## What You Should Expect On The Frontend

The dashboard shows:

- current quality
- current resolution
- bitrate
- FPS

The video player itself is resized by the backend stream settings, so the actual feed follows the selected tier instead of only showing it as text.

## Recommended Setup For 3 Nodes

- Run one camera source per node if possible.
- Keep all 3 nodes on the same MQTT broker.
- Use the same bandwidth budget on each node.
- Let the nodes negotiate automatically.

## Important Note

If you want the rule to be stricter, you can change the policy so only one node is ever allowed to use 1080p at a time, which is what the current 3-node ranking already does.
