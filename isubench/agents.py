import datetime
import json
from pathlib import Path


def events(path: Path):
    for line in path.read_text(errors='replace').splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            continue


def epoch(text):
    return datetime.datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()


def add(total, **values):
    for key, value in values.items():
        total[key] = total.get(key, 0) + (value or 0)


class ClaudeCode:
    name = 'claude-code'
    domains = ['api.anthropic.com']
    credentials = ['ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN']
    env = {'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1'}
    version = ['claude', '--version']

    def command(self, model, effort, prompt, logs):
        cmd = ['claude', '-p', prompt, '--model', model, '--effort', effort, '--output-format', 'stream-json',
               '--verbose', '--dangerously-skip-permissions', '--disallowedTools', 'WebSearch,WebFetch']
        return cmd + (['--continue'] if logs else [])

    # Read from the transcripts: the CLI's own result event is lost when the harness stops it at the last run.
    def usage(self, home):
        total, seen = {}, set()
        for log in (home / '.claude' / 'projects').rglob('*.jsonl'):
            for e in events(log):
                m = e.get('message') if e.get('type') == 'assistant' else None
                # Each content block of one API response is written as its own line with the same usage.
                if not isinstance(m, dict) or not m.get('usage') or m.get('id') in seen:
                    continue
                seen.add(m.get('id'))
                u = m['usage']
                add(total, input_tokens=u.get('input_tokens'), output_tokens=u.get('output_tokens'),
                    cache_read_tokens=u.get('cache_read_input_tokens'),
                    cache_write_tokens=u.get('cache_creation_input_tokens'))
        return total

    def activity(self, home):
        seen = set()
        for log in (home / '.claude' / 'projects').rglob('*.jsonl'):
            for e in events(log):
                m = e.get('message') if e.get('type') == 'assistant' else None
                if not isinstance(m, dict) or not e.get('timestamp'):
                    continue
                calls = sum(1 for b in m.get('content') or [] if isinstance(b, dict) and b.get('type') == 'tool_use')
                tokens = 0 if m.get('id') in seen else (m.get('usage') or {}).get('output_tokens') or 0
                seen.add(m.get('id'))
                yield epoch(e['timestamp']), calls, tokens


class Codex:
    name = 'codex'
    domains = ['api.openai.com', 'chatgpt.com', 'auth.openai.com']
    credentials = ['OPENAI_API_KEY', 'CODEX_API_KEY']
    env = {}
    version = ['codex', '--version']
    # A ChatGPT login made for isu-bench only, so refresh-token rotation never touches the user's own login.
    auth_store = Path('~/.config/isu-bench/codex/auth.json').expanduser()
    auth_file = '.codex/auth.json'

    def auth_secrets(self, text):
        return [v for v in json.loads(text).get('tokens', {}).values() if isinstance(v, str) and len(v) > 20]

    def command(self, model, effort, prompt, logs):
        cmd = ['codex', 'exec'] + (['resume', '--last'] if logs else [])
        return cmd + ['--json', '-m', model, '--dangerously-bypass-approvals-and-sandbox', '--skip-git-repo-check',
                      '-c', 'web_search="disabled"', '-c', f'model_reasoning_effort="{effort}"', prompt]

    def usage(self, home):
        total = {}
        for log in (home / '.codex' / 'sessions').rglob('*.jsonl'):
            counts = [e['payload']['info'] for e in events(log)
                      if e.get('type') == 'event_msg' and e.get('payload', {}).get('type') == 'token_count'
                      and e['payload'].get('info')]
            if counts:
                u = counts[-1]['total_token_usage']
                # Codex counts cached tokens inside input_tokens; the other agents report them separately.
                add(total, input_tokens=u.get('input_tokens', 0) - u.get('cached_input_tokens', 0),
                    output_tokens=u.get('output_tokens'), cache_read_tokens=u.get('cached_input_tokens'),
                    cache_write_tokens=u.get('cache_write_input_tokens'),
                    reasoning_tokens=u.get('reasoning_output_tokens'))
        return total

    def activity(self, home):
        for log in (home / '.codex' / 'sessions').rglob('*.jsonl'):
            done = 0
            for e in events(log):
                p = e.get('payload') or {}
                if e.get('type') == 'response_item' and p.get('type') in ('custom_tool_call', 'function_call', 'local_shell_call'):
                    yield epoch(e['timestamp']), 1, 0
                elif e.get('type') == 'event_msg' and p.get('type') == 'token_count' and p.get('info'):
                    # Each count is the session's running total.
                    total = p['info']['total_token_usage'].get('output_tokens', 0)
                    yield epoch(e['timestamp']), 0, total - done
                    done = total


class DeepSeekHarness:
    name = 'dsh'
    domains = ['api.deepseek.com']
    credentials = ['DEEPSEEK_API_KEY']
    # Ollama Cloud accepts only bearer auth while dsh sends x-api-key, so the proxy sidecar adds the key on the way.
    upstream = {'credential': 'OLLAMA_API_KEY', 'host': 'ollama.com',
                'env': {'DEEPSEEK_BASE_URL': 'http://proxy:3129', 'DEEPSEEK_API_KEY': 'held-by-proxy'}}
    env = {'DSH_HOME': '/home/agent/.dsh', 'DSH_PERMISSION_MODE': 'danger-full-access'}
    version = ['dsh', '--version']
    patch = '/home/agent/dsh.patch.yml'

    def patch_text(self, model, effort):
        rows = ['- id: tool-web\n  disabled: true\n', '- id: web-search-deepseek\n  disabled: true\n',
                '- id: web-fetch-http\n  disabled: true\n', '- id: session-log-deepseek\n  disabled: true\n',
                # The default 60 s command timeout is shorter than one benchmark run.
                '- id: bash-sandbox\n  config:\n    timeoutMs: 600000\n']
        return ''.join(rows) + ('- id: agent-default-model\n  config:\n    provider: deepseek-official\n'
                                f'    model: {model}\n    reasoningEffort: {effort}\n')

    def command(self, model, effort, prompt, logs):
        cmd = ['dsh', '--profile', 'headless', '--patch', self.patch, '--json']
        if logs:
            session = next(e['sessionId'] for e in events(logs[0]) if e.get('type') == 'session')
            cmd += ['--session-id', session]
        return cmd + [prompt]

    def usage(self, home):
        total = {}
        for log in (home / '.dsh' / 'sessions').rglob('*.jsonl'):
            for e in events(log):
                u = (e.get('data') or {}).get('usage') if e.get('type') in ('assistant/message', 'compaction/summary') else None
                if u:
                    add(total, input_tokens=u.get('inputTokens'), output_tokens=u.get('outputTokens'),
                        cache_read_tokens=u.get('cacheReadTokens'), cache_write_tokens=u.get('cacheWriteTokens'))
        return total

    def activity(self, home):
        for log in (home / '.dsh' / 'sessions').rglob('*.jsonl'):
            for e in events(log):
                if e.get('type') == 'tool/call':
                    yield e['time'] / 1000, 1, 0
                elif e.get('type') in ('assistant/message', 'compaction/summary') and (e.get('data') or {}).get('usage'):
                    yield e['time'] / 1000, 0, e['data']['usage'].get('outputTokens') or 0


AGENTS = {a.name: a for a in (ClaudeCode(), Codex(), DeepSeekHarness())}
