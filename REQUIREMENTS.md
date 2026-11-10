# isu-bench v1.1 requirements

isu-bench measures how far a coding agent can improve an ISUCON web service on its first encounter. The score is ISUMAIL, an original problem whose source is not published; ISUCON14 (ISURIDE) and ISUCON13 (ISUPipe) run as extras.

## Problem and infrastructure

- Upstream: [isucon/isucon14](https://github.com/isucon/isucon14) (MIT), fetched at a pinned commit. Not vendored.
- AMI: [matsuu/aws-isucon](https://github.com/matsuu/aws-isucon/tree/main/isucon14) `ami-0fcf9e8e8675a9ee4` (`isucon14-20260818100152`, owner 839726181030), pinned. The official AMI from the upstream README no longer exists. Its webapp matches upstream `53f8b627e040c30ebec600457c6c97da008b84b0` and it ships the benchmarker at `/home/isucon/bench`.
- AWS ap-northeast-1. Three competition hosts c5.large and one benchmark host c5.2xlarge (8 vCPU / 16 GiB), each gp3 20 GiB. The contest benchmarker ran on Fargate 8 vCPU / 8 GB, but with 8 GiB the benchmarker itself ran out of memory when an application stalled, so the benchmark host keeps its full 16 GiB.
- The harness drives the AWS CLI for the whole lifecycle of a run: create a dedicated VPC/subnet/IGW/SG/key, launch the 4 instances, wait for health, run the agent, collect results, terminate the instances, delete the network and key. Every resource is tagged with the run ID and deleted by explicit ID, never by tag sweep. Several runs can go in parallel within the EC2 vCPU quota (14 vCPU per run).
- Cost safety net: instances launch with `instance-initiated-shutdown-behavior=terminate` and a cron entry that shuts down after an absolute expiry time (48 h), so they disappear even if the harness dies, and a reboot does not cancel it. This is not part of scoring; runs that hit it are recorded as aborted.
- Security groups: competition hosts accept SSH and HTTPS from the caller's public IP and anything from the VPC hosts; the benchmark host accepts SSH from the caller and anything from the competition hosts. Competition hosts have no direct egress outside the VPC.
- Two keys: an agent key installed for `isucon` on the competition hosts, and a harness-only key that is the only key accepted by the benchmark host. The app services on the benchmark host are stopped.
- Credentials come from the caller's environment (`AWS_PROFILE` etc.). No account ID, profile name or local path is committed.
- Design reference: [kyosu-1/isuenv](https://github.com/kyosu-1/isuenv) (MIT) for the TTL cron and the bench command line.
- Language defaults to Go and is selectable with `--lang`. The harness switches the initial systemd service on all three hosts to the chosen implementation before the agent starts.

## Problems

Each problem is a definition in [isubench/problems.py](isubench/problems.py): AMI, instance types, upstream commit, service name, benchmark command, result parsing and setup hooks. The prompt is the same template with the problem's name, service and benchmark note filled in.

- ISUCON14 (ISURIDE): matsuu/aws-isucon AMI, upstream `53f8b627`. The AMI's payment mock is stopped on the benchmark host.
- ISUCON13 (ISUPipe): official AMI `ami-041289d910c114864`, upstream `5895cf1b` (the commit the AMI was built from). The certificate in the AMI has expired, so the harness issues a CA and a `*.u.isucon.dev` certificate per run, installs it on the three servers and trusts the CA on the benchmark host. Each server's PowerDNS records point at its own private IP; the benchmarker resolves names through s1 (`--nameserver s1 --webapp s2 --webapp s3`). The initial Go implementation scores about 3,400.
- ISUCON12 and ISUCON11: the official AMIs no longer exist; supporting them needs an AMI build.
- ISUMAIL: an original problem whose source stays private; it ships as four public AMIs (app x86_64 for s1, app arm64 for s2 and s3, router, benchmark host) built outside this repository. The workspace is `webapp/` and `docs/` copied from s1 at the start of a run. Five instances: s1 c5.large, s2 c7g.medium, s3 c6g.medium, router c6g.medium (no agent access), benchmark host c7g.2xlarge; 13 vCPU and about $0.64 per hour. Go only. `winner` is the estimated theoretical maximum and `initial` comes from `calibrate` of the reference implementation.

## Harness features used by ISUMAIL

Each is a `Problem` attribute or hook whose default keeps the v1 behavior.

- Per-role AMIs (`amis`), checked against each instance type's architecture and per-interface address limit before launch.
- Fixed private addresses and extra addresses on one interface (`addresses`, `secondary_addresses`), launched with `--network-interfaces`.
- Security groups from a matrix (`groups`, `ingress`, `egress`); `audit_network` compares groups, interfaces, VPC IPv6 and routes with it through the AWS API after provisioning, when SSH is closed and after the reboot check.
- `harness_jump`: the harness reaches the apps through the benchmark host, so the caller's SSH rule can be removed for the agent alone.
- `ssh_throttle` and `ssh_lockdown`: the sidecar proxy delays and paces the agent's SSH and logs its bytes; from the start of the last run it refuses and closes SSH, the caller's SSH rule is revoked and the agent's sshd sessions are killed, and the agent and proxy containers are removed before the reboot check.
- Structured run results (`outcome`): pass, score, infra, run ID and state hash. Infra runs are not counted, up to `infra_limit` per session, after which the session is aborted. `run_command` gives each attempt its own state file and the previous counted state.
- `before_run` and `after_run` checks (router rules, clocks against Amazon Time Sync) whose failure makes the attempt uncounted.
- `verify_reboot`: the problem's own post-reboot check (here: all apps rebooted, s1 answers the probe, persist-check against the last counted run).
- `calibrate`: provision, optionally deploy another implementation, run N benchmarks and the reboot check without an agent, tear down. `quota`: compare the On-Demand Standard vCPU quota with N parallel trials.

## Agent sandbox

- Any CLI agent runs inside a local Docker container with a clean HOME (no user config, memory, hooks or plugins).
- Mounted into the container: the initial webapp and docs as a git repo, an SSH config and key for the three competition hosts, the `bench` command, and the prompt.
- Not available to the agent: the benchmark host, the benchmarker source, the upstream repo outside `webapp/` and `docs/`.
- The agent may rewrite anything, build its own load tools and use subagents.
- Forbidden: looking up ISUCON14 solutions on the web, and running the benchmarker other than through `bench`.
- Enforced by the harness, not only by the prompt:
  - Built-in web search and fetch tools are disabled: Claude Code `--disallowedTools WebSearch,WebFetch`, Codex `web_search="disabled"`, DeepSeek Harness `tool-web` and its providers.
  - Egress allowlist proxy ([isubench/proxy.py](isubench/proxy.py)). The container sits on an internal Docker network and reaches outside only through a sidecar proxy that allows the agent's model API, the package registries in [registries.txt](isubench/registries.txt) and SSH to the three competition hosts. The competition hosts reach outside only through the same proxy running on the benchmark host, which allows the package registries. Every request is logged and published, including denied ones.

## `bench` command

- Talks to a host-side daemon through a mounted Unix socket. The daemon holds the benchmark host key, so the agent cannot reach the benchmarker directly.
- One run at a time. Every invocation that starts counts toward the limit, including FAIL and application errors such as a failed `/api/initialize`.
- The benchmarker runs detached on the benchmark host with its output in files; when the SSH session breaks, the daemon reconnects and resumes the output where it left off, so the run continues.
- Exception: when the benchmark host stays unreachable for 15 minutes, it was lost rather than the application failing. That attempt is not counted, its logs are kept as `infra-NN`, the harness reboots the benchmark host, and the agent is told so.
- Prints only the contestant-facing benchmarker output (stdout: pass/fail, score, error summary) to the agent. The operator-facing output (stderr) is saved and published but not shown. After the 11th run it refuses further invocations.

## Scoring and termination

- The run ends when the 11th benchmark finishes (the 15th in v1). No wall-clock limit.
- If the agent exits before using 11 runs, the harness resumes it up to 3 times with "Continue. N of 11 benchmark runs remain." If it still stops, the last completed run is scored and the result is flagged as stopped early.
- After the 11th run the harness reboots the three competition hosts and checks that the service responds. If it does not, the score is 0.
- Score = result of the 11th run. FAIL scores 0. The isu-bench score is the ISUMAIL score divided by its estimated theoretical maximum (`winner`); problems are marked `scored` in the problem definition.

## Recorded per run

- Agent CLI and version, model, reasoning effort (required on the command line), language, model provider.
- Score and full log of each of the 11 benchmark runs, with timestamps.
- Wall time from agent start to the 11th run.
- Token usage (uncached input, output, cache read/write), summed from the agent transcripts, since the CLI's own totals are lost when the harness stops it at the last run.
- Agent transcripts (CLI JSON output and session logs) with credential values redacted.
- Proxy logs from the container and the competition hosts.
- Final git diff of the workspace.

## Publishing

- Public repo: harness, `bench` daemon, Dockerfile, prompt, rules, result schema, and results.
- Results table: agent, model, language, the 11 scores, final score, wall time, tokens, cost. Benchmark logs are published; final code is optional.
- Known limitations stated in the README:
  - One trial per configuration because of EC2 cost; variance between trials is not measured.
  - Models may have seen public ISUCON14 solutions during training.
  - Data persistence after reboot is checked only as "service responds", not by comparing written data.

## Trials

One trial each, language Go.

- ISUMAIL (v1.1, 11 runs): Claude Code + Claude Opus 5.5 (high), Codex + GPT-6 Astra (xhigh), Codex + GPT-6.1 Sol (xhigh), DeepSeek Harness + DeepSeek V4.1 Flash (max)
- ISUMAIL as of v1 (`isumail-v1`, 15 runs): the same four and Claude Code + Claude Fable 5.2 (high)

The trials below ran under v1, with 15 runs.

- ISUCON14 and ISUCON13: Claude Code + Claude Opus 5.5 (high), Codex + GPT-6 Astra (xhigh), Codex + GPT-6.1 Sol (xhigh), DeepSeek Harness + DeepSeek V4.1 Flash (`deepseek-v4.1-flash:1017` on Ollama Cloud, max)
- ISUCON14 only: Claude Code + Claude Opus 5.5 (xhigh), Claude Code + Claude Sonnet 5.5 (xhigh)

## Scoring

isu-bench score = geometric mean over problems of `max(final, initial) / winner`, where `winner` is the contest winner's official final score and `initial` the initial Go implementation's median first-run score in this environment. The floor makes a FAIL count as no progress instead of zeroing the mean. The geometric mean keeps a problem whose winning score was low (ISUCON14: 58,153, beaten tenfold by post-contest tuning and by these agents) from outweighing the others, as an arithmetic mean of percentages would. It is computed only for configurations run on every problem.

## Prompt

[prompt.md](prompt.md). It states facts and rules only, with no strategy hints. `{LANG}` and `{lang}` are filled from `--lang`, `{RUNS}` from the run limit (11).

## To verify during implementation

- DeepSeek Harness: resuming with `--session-id`. (Verified: the request carries the pinned model, thinking enabled and effort `max` with no web tools; usage from the zstd session logs matches dsh's own totals.)
- Codex: `exec resume --last` picks up the session. (`web_search="disabled"` verified: no web search tool in the session's tool list.)
