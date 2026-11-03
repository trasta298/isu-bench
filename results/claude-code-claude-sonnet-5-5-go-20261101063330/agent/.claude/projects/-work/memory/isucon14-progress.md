---
name: isucon14-progress
description: ISUCON14 tuning session state - architecture, run history, bench dynamics learned, what to do next
metadata:
  type: project
---

Task: maximize score of the 15th `bench` run (accidental `bench --help` consumed run 1 - NEVER call bench except for real runs). Final run FAIL=0. Servers reboot after run 15.

**Deployed architecture:** Go rewrite in /work/webapp/go (in-memory state under global RWMutex, async write-behind to MySQL via dirty sets/multi-row upserts, SSE notifications, event-driven threshold-aware matcher, SYNCHRONOUS payment with Idempotency-Key retries). s1: app (:8080, GOGC=400 via systemd drop-in) + tuned MySQL + isucon-nat.service (nat-guard.sh: DNAT :443 -> s2/s3 nginx TLS terminators, falls back to local nginx). s2/s3: nginx (configs /work/conf: nginx.final.conf + isuride.final.conf) proxy to s1:8080. isuride-matcher disabled everywhere; isuride-go disabled on s2/s3. Deploy app: /work/tools/deploy.sh. Smoke test: /work/tools/smoke.py (copy to s1:/tmp, TARGET=10.0.1.71). Reboot of all 3 nodes verified OK (r10->11).

**Run history (score):** r1 baseline accident; r2 FAIL async payment (bench checks payment right after evaluation -> must be sync); r3 FAIL CODE=30 (fixed: hide chair from nearby 1.5s after COMPLETED, NEARBY_GRACE_MS); r4 384734; r5 362679; r6 284013 (Cache-Control on static = worse); r7 347438; r8 84960 (nginx limit_req 2000/s on static = disaster); r9 284480 (limit_req 10k/s = worse); r10 366121 (threshold-aware matcher; pickup dissatisfaction 32%->26%, score same). Natural config family = 365k +-20k.

**Bench dynamics learned:** user growth is invitation-exponential; bench host CPU is shared bottleneck (payment mock latency 30ms->65ms at peak shows it); user actions (evaluate, new ride) are gated by bench user-loop iteration (page view + think); faster static makes loops spin and saturates bench (r6), slower static starves rides (r8/r9) -> natural speed is best, don't touch static serving. Payment mock fails ~70% randomly (don't hedge/parallelize: dup risk). Satisfaction model fitted: pickup unhappy iff ceil(d/speed)>10 steps (30.7ms/step nominal), travel unhappy iff real time >~1.6s; chair step = 0.0307s*ceil(L/v) when bench unsaturated, stretches to ~100ms+ at peak. 500 dataset chairs are inactive (never activate them).

**Later results:** r11 385404 (post-reboot), r12 398708 (tauHat reset on initialize), r13 370326 (nginx http2: no gain, bench does negotiate h2, reverted), r14 385127 (final-config rehearsal). FINAL CONFIG = nginx.final.conf+isuride.final.conf on s2/s3 (http1.1, no access log), app at HEAD (commit 598a63a + tauHat reset), no further changes before r15.

**Old remaining plan (done):** r11 validate final config post-reboot; maybe small experiments r12-13; r14 rehearsal; r15 final = best known config. Before final: no PROFILE, no nginx access log; PROFILE override removed already.
