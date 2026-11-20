# isu-bench

English | [日本語](README.ja.md)

isu-bench hands a coding agent a web service it has never seen, the three servers it runs on, and 11 load tests. There is no time limit, no hint and no one to ask. The 11th load test is its score.

The task in detail, the charts and every number are on **[isu-bench.trasta.dev](https://isu-bench.trasta.dev/)**.

![isu-bench v1.1 results](docs/isu-bench-v1.1.png)

## Results

isu-bench v1.1. Each configuration ran once, so small gaps mean little. The score is the 11th load test as a share of the estimated maximum, 1,060,000 points.

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

- The untouched service scores about 1,744 points (0.2%), and its score varies by about 5% from run to run.
- API cost prices each trial's tokens at the provider's list price in November 2026 ([isubench/prices.py](isubench/prices.py)). DeepSeek is at its peak rate, and Gemini 4 Carbon at its introductory price, which doubles on 2026-12-01.

## How it works

- The agent gets a specification, a slow reference implementation in Go and root on three servers. It may change anything, as long as the service keeps behaving as specified.
- `bench` runs a load test, at most 11 times, failed runs included. The 11th is the score, and a FAIL scores 0.
- The servers are then rebooted. If the service does not come back with its data, the score is 0.
- There is no time limit and no web access.

The format follows [ISUCON](https://isucon.net/), a web performance tuning contest held in Japan since 2011. The problem, ISUMAIL, was written for isu-bench, and its source is not published, so that no model has seen it solved. It ships only as machine images, and this repository holds no more of a trial than its `result.json`.

The full rules are in [REQUIREMENTS.md](REQUIREMENTS.md) and the exact prompt in [prompt.md](prompt.md).

## What it measures

| | Test-based coding benchmarks | isu-bench |
|---|---|---|
| The work | Change the code in a repository | Run a live system: code, database and three servers |
| Done when | The tests pass | Never. Every improvement raises the score |
| Feedback | Run the tests as often as needed | 11 load tests, and failing the last one scores 0 |
| Prior exposure | Mostly public code | An unpublished problem, shipped only as machine images |

Doing well takes several skills at once:

- reading an unfamiliar specification and codebase
- finding the bottleneck by measuring it
- carrying a large rewrite through without breaking behavior
- operating servers: deployment, recovery after a reboot, durable data
- judgment under a budget: when to spend a load test, and when to stop taking risks

## Running

Requirements: Linux with Docker, the AWS CLI v2 with credentials for ap-northeast-1 (13 vCPU of on-demand C instances per trial), Python 3.10+, git and ssh. A trial costs about $0.64 per hour of EC2, plus model usage.

```sh
export ANTHROPIC_API_KEY=...   # or CLAUDE_CODE_OAUTH_TOKEN / OPENAI_API_KEY / DEEPSEEK_API_KEY / OLLAMA_API_KEY / GEMINI_API_KEY
python3 -m isubench run --problem isumail --agent claude-code --model claude-opus-5-5 --effort high
python3 -m isubench table        # every result in results/
python3 -m isubench teardown <run_id>
```

A trial creates its own VPC and instances and deletes them when it ends. If the harness dies, the instances terminate themselves after 48 hours, and `teardown` deletes them right away.

| Agent | `--agent` | Notes |
|---|---|---|
| Claude Code | `claude-code` | |
| Codex | `codex` | Can use a ChatGPT login made for isu-bench only: `docker run --rm -it -v ~/.config/isu-bench/codex:/home/agent/.codex isu-bench-agent:v1 codex login --device-auth` |
| Antigravity CLI | `agy` | Takes a Gemini API model by name (`--model gemini-4-carbon`) and `--effort low`, `medium` or `high`. Its web search runs inside the model API, past the proxy, so a hook denies it |
| DeepSeek Harness | `dsh` | With `OLLAMA_API_KEY` it runs against Ollama Cloud. Its command timeout is raised from 60 seconds to 10 minutes, because one load test takes about 70 seconds |

- `agy` can also use a Google sign-in made for isu-bench only, used when `GEMINI_API_KEY` is unset: run `docker run --rm -it -v ~/.config/isu-bench/agy:/home/agent/.gemini/antigravity-cli isu-bench-agent:v1 agy`, choose Google OAuth and paste the code from the sign-in page. Its first-run screen checks the box that lets Google collect your interactions data; uncheck it. Signed in, `agy` takes the models that `agy models` lists for the account, such as `--model gemini-4-neon --effort high`.

## Other problems and versions

- ISUCON14 and ISUCON13 also run in the harness (`--problem isucon14`, `--problem isucon13`). They are not scored, because their public solutions predate the models' training data. Results from v1 are in `results/`.
- v1 gave 15 load tests and used an earlier ISUMAIL benchmarker, so its scores cannot be compared with v1.1.

## Credits

- [ISUCON2026](https://isucon.net/), whose problem ISUMAIL's design draws on heavily.
- [isucon/isucon14](https://github.com/isucon/isucon14) for the problem, reference implementations and benchmarker.
- [matsuu/aws-isucon](https://github.com/matsuu/aws-isucon) for the AMI.
- [kyosu-1/isuenv](https://github.com/kyosu-1/isuenv) for the TTL technique.

## Trademark

ISUCON is a trademark or registered trademark of SAKURA internet Inc. ([isucon.net](https://isucon.net))

isu-bench is an independent project. It is not run, sponsored or endorsed by the ISUCON organizers or SAKURA internet Inc.

## License

[MIT](LICENSE)
