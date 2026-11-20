# isu-bench

English | [日本語](README.ja.md)

isu-bench gives a coding agent an unfamiliar web service, the three servers it runs on, and a budget of 11 load tests. There is no time limit, and the agent gets no hints or outside help. The 11th load test determines the final score.

See **[isu-bench.trasta.dev](https://isu-bench.trasta.dev/)** for charts, full numbers, and task details.

![isu-bench v1.1 results](docs/isu-bench-v1.1.png)

## Results

Results for isu-bench v1.1. Each configuration was run once, so small differences are not meaningful. Scores are the 11th load test expressed as a percentage of the estimated maximum (1,060,000 points).

| Model | Agent | Effort | Score | Time | API cost |
|---|---|---|---:|---:|---:|
| Claude Fable 5.2 | Claude Code 2.1.321 | high | **58.4%** | 4 h 31 min | $76.55 |
| Gemini 4 Neon | Antigravity CLI 1.5.12 | high | **54.7%** | 2 h 4 min | $18.98 |
| Gemini 4 Carbon | Antigravity CLI 1.5.5 | medium | **51.9%** | 3 h 9 min | $39.27 |
| Claude Opus 5.5 | Claude Code 2.1.321 | high | **43.2%** | 2 h 13 min | $22.78 |
| GPT-6.2 Astra | Codex 0.169.0 | xhigh | **41.8%** | 35 min | $9.70 |
| Gemini 4 Hydrogen | Antigravity CLI 1.5.12 | high | **17.8%** | 1 h 21 min | $10.52 |
| GPT-6.1 Sol | Codex 0.166.0 | xhigh | **15.2%** | 46 min | $2.38 |
| GPT-6.2 Sol | Codex 0.169.0 | xhigh | **14.1%** | 48 min | $4.14 |
| GPT-6 Astra | Codex 0.166.0 | xhigh | **9.7%** | 46 min | $13.72 |
| DeepSeek V4.1 Flash | DeepSeek Harness 0.2.1-rc.5 | max | **0.8%** | 48 min | $0.42 |

- Unmodified, the service scores about 1,744 points (0.2%), with roughly 5% run-to-run variance.
- API cost is calculated from each trial's token usage using the providers' November 2026 list prices ([isubench/prices.py](isubench/prices.py)). DeepSeek is priced at its peak rate, and Gemini 4 Carbon at its introductory rate (which doubles on 2026-12-01).

## How it works

- The agent receives a specification, a slow Go reference implementation, and root access to three servers. It can change anything as long as the service continues to follow the specification.
- `bench` runs a load test, up to 11 times total (including failed runs). The 11th run sets the final score, and a FAIL scores 0.
- After the final run, the servers are rebooted. If the service fails to come back up with its data intact, the score is 0.
- There is no time limit and no web access.

The format is modeled after [ISUCON](https://isucon.net/), a web performance tuning contest held in Japan since 2011. The problem, ISUMAIL, was written specifically for isu-bench. Its source code is unpublished so models cannot have seen existing solutions: it is distributed only as machine images, and this repository only stores each trial's `result.json`.

The full rules are in [REQUIREMENTS.md](REQUIREMENTS.md), and the exact prompt is in [prompt.md](prompt.md).

## What it measures

| | Test-based coding benchmarks | isu-bench |
|---|---|---|
| Task | Edit code in a repository | Optimize a live system: code, database, and three servers |
| Done when | The tests pass | Open-ended: every improvement raises the score |
| Feedback | Run tests as often as needed | 11 load tests total; failing the last one scores 0 |
| Prior exposure | Mostly public code | Unpublished problem, distributed only as machine images |

Doing well requires several skills at once:

- reading an unfamiliar specification and codebase
- profiling to find bottlenecks
- making large rewrites without breaking existing behavior
- server operations: deployment, reboot recovery, and data durability
- managing a limited budget: deciding when to run a load test and when to stop taking risks

## Running

Requirements: Linux with Docker, AWS CLI v2 with credentials for `ap-northeast-1` (13 vCPUs of on-demand C instances per trial), Python 3.10+, git, and ssh. Each trial costs about $0.64/hour in EC2 charges, plus model usage.

```sh
export ANTHROPIC_API_KEY=...   # or CLAUDE_CODE_OAUTH_TOKEN / OPENAI_API_KEY / DEEPSEEK_API_KEY / OLLAMA_API_KEY / GEMINI_API_KEY
python3 -m isubench run --problem isumail --agent claude-code --model claude-opus-5-5 --effort high
python3 -m isubench table        # list all results in results/
python3 -m isubench teardown <run_id>
```

Each trial provisions its own VPC and instances, then cleans them up when it finishes. If the harness crashes, the instances auto-terminate after 48 hours, or you can remove them immediately with `teardown`.

| Agent | `--agent` | Notes |
|---|---|---|
| Claude Code | `claude-code` | |
| Codex | `codex` | Can also use a dedicated ChatGPT login for isu-bench: `docker run --rm -it -v ~/.config/isu-bench/codex:/home/agent/.codex isu-bench-agent:v1 codex login --device-auth` |
| Antigravity CLI | `agy` | Takes a Gemini API model name (`--model gemini-4-carbon`) and `--effort` (`low`, `medium`, or `high`). Because its web search runs inside the model API and bypasses the proxy, it is blocked via a hook. |
| DeepSeek Harness | `dsh` | Runs against Ollama Cloud when `OLLAMA_API_KEY` is set. The command timeout is raised from 60 seconds to 10 minutes since a single load test takes about 70 seconds. |

- When `GEMINI_API_KEY` is unset, `agy` can also use a dedicated Google sign-in for isu-bench. Run `docker run --rm -it -v ~/.config/isu-bench/agy:/home/agent/.gemini/antigravity-cli isu-bench-agent:v1 agy`, select Google OAuth, and paste the code from the sign-in page. On the first-run screen, uncheck the option that lets Google collect interaction data. Once signed in, you can pass any model listed by `agy models` for that account (for example, `--model gemini-4-neon --effort high`).

## Other problems and versions

- ISUCON14 and ISUCON13 also run in this harness (`--problem isucon14`, `--problem isucon13`). They are not scored because their public solutions predate the models' training cutoffs. Results from v1 are in `results/`.
- v1 allowed 15 load tests and used an earlier version of the ISUMAIL benchmarker, so v1 scores cannot be compared with v1.1.

## Credits

- [ISUCON2026](https://isucon.net/), whose problem heavily inspired ISUMAIL's design.
- [isucon/isucon14](https://github.com/isucon/isucon14) for the problem, reference implementations, and benchmarker.
- [matsuu/aws-isucon](https://github.com/matsuu/aws-isucon) for the AMI.
- [kyosu-1/isuenv](https://github.com/kyosu-1/isuenv) for the TTL technique.

## Trademark

ISUCON is a trademark or registered trademark of SAKURA internet Inc. ([isucon.net](https://isucon.net)).

isu-bench is an independent project. It is not run, sponsored, or endorsed by the ISUCON organizers or SAKURA internet Inc.

## License

[MIT](LICENSE)
