---
name: isucon14-bench-learnings
description: ISUCON14 (ISURIDE) benchmarker behaviors learned empirically during the 15-run session on 2026-11-01
metadata:
  node_type: memory
  type: project
  originSessionId: 3addc29c-ba33-44a2-b228-51ec8c9ddccd
  modified: 2026-11-01T07:28:35.814Z
---

Empirical facts about the ISURIDE benchmarker (learned 2026-11-01, runs 1-10):
- Payment must complete BEFORE the evaluation response; async payment -> CODE=34 critical FAIL within seconds.
- Payment gateway injects 500/502/504 on ~70% of attempts, always succeeds by ~6th attempt with same Idempotency-Key; retry immediately.
- nearby-chairs is checked both ways: listing a chair the bench thinks is busy -> CODE=30; omitting a free chair -> CODE=31. Delaying empty responses 150ms -> 1789x CODE=31 FAIL.
- Guards that gave 0 warnings: hide chair 500ms after COMPLETED delivered; don't assign a chair within 500ms of it being listed (reserve/hide it meanwhile).
- Users spin on nearby (random point, distance=50) until >=1 chair, then request a ride there.
- Owners add chairs based on sales -> matcher maximizing score per chair-tick (favor long rides, overhead ~10 ticks) beat ETA-first matching.

**Why:** each bench run is scarce; these cost runs to discover.
**How to apply:** keep these constraints when changing nearby/payment/matching logic. Related: [[isucon14-topology]]
