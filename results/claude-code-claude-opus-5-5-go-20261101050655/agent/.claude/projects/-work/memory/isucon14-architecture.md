---
name: isucon14-architecture
description: "ISUCON14 (ISURIDE) tuning session outcome — final architecture, scores, and pitfalls learned"
metadata:
  node_type: memory
  type: project
  originSessionId: da3417c3-a3dc-4a3a-bc92-1fe7f1bdd5b2
  modified: 2026-11-01T05:49:40.229Z
---

ISUCON14 session (2026-11-01): baseline 1114 → ~450-477k with an in-memory Go rewrite (/work/webapp/go).

Final layout: s1 = nginx + iptables DNAT L4 split (isu-nat.service: 20% → s2:443, 65% of rest → s3:443); s2 = Go app (all state in memory, async ordered DB write-behind, unix socket for local nginx) + MySQL; s3 = nginx only. SSE notifications; matcher in-app every 50ms with pickup radius 100+50*wait_sec (prevents cross-region drift between Chair Town ~(0,0) and Koshikake ~(300,300)).

**Why:** nginx TLS termination was the CPU bottleneck; regions matter for matching.

**How to apply (pitfalls):**
- Payment MUST be synchronous before evaluation response — async payment FAILs the post-initialize integrity check (CODE=34).
- Nearby-chairs: exclude chairs for 2s after completion (else CODE=30 "already riding" warnings).
- Allow duplicate usernames (bench collides; expects 201).
- Disable unattended-upgrades/apt/man-db timers — they steal CPU during runs.
- Payment server fails ~70% of attempts (500/502/504); use Idempotency-Key and fast retries.
