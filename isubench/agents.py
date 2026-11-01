import json
from pathlib import Path


def events(path: Path):
    for line in path.read_text(errors='replace').splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            continue


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
        cmd = ['claude', '-p', prompt, '--model', model, '--output-format', 'stream-json', '--verbose',
               '--dangerously-skip-permissions', '--disallowedTools', 'WebSearch,WebFetch']
        if logs:
            cmd.append('--continue')
        if effort:
            cmd += ['--effort', effort]
        return cmd

    def usage(self, logs, home):
        total = {}
        for log in logs:
            for e in events(log):
                if e.get('type') == 'result':
                    for u in (e.get('modelUsage') or {}).values():
                        add(total, input_tokens=u.get('inputTokens'), output_tokens=u.get('outputTokens'),
                            cache_read_tokens=u.get('cacheReadInputTokens'),
                            cache_write_tokens=u.get('cacheCreationInputTokens'), cost_usd=u.get('costUSD'))
        return total


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
        cmd += ['--json', '-m', model, '--dangerously-bypass-approvals-and-sandbox', '--skip-git-repo-check',
                '-c', 'web_search="disabled"']
        if effort:
            cmd += ['-c', f'model_reasoning_effort="{effort}"']
        return cmd + [prompt]

    def usage(self, logs, home):
        total = {}
        for log in logs:
            for e in events(log):
                if e.get('type') == 'turn.completed':
                    u = e.get('usage', {})
                    # Codex counts cached tokens inside input_tokens; the other agents report them separately.
                    add(total, input_tokens=u.get('input_tokens', 0) - u.get('cached_input_tokens', 0),
                        output_tokens=u.get('output_tokens'), cache_read_tokens=u.get('cached_input_tokens'),
                        cache_write_tokens=u.get('cache_write_input_tokens'),
                        reasoning_tokens=u.get('reasoning_output_tokens'))
        return total


class DeepSeekHarness:
    name = 'dsh'
    domains = ['api.deepseek.com']
    credentials = ['DEEPSEEK_API_KEY']
    env = {'DSH_HOME': '/home/agent/.dsh'}
    version = ['dsh', '--version']
    patch = '/home/agent/dsh.patch.yml'

    def patch_text(self, model, effort):
        rows = ['- id: tool-web\n  disabled: true\n', '- id: web-search-deepseek\n  disabled: true\n',
                '- id: web-fetch-http\n  disabled: true\n', '- id: session-log-deepseek\n  disabled: true\n']
        config = f'    provider: deepseek-official\n    model: {model}\n'
        if effort:
            config += f'    reasoningEffort: {effort}\n'
        return ''.join(rows) + f'- id: agent-default-model\n  config:\n{config}'

    def command(self, model, effort, prompt, logs):
        cmd = ['dsh', '--profile', 'headless', '--patch', self.patch, '--json',
               '--permission-mode', 'danger-full-access', '--approval-policy', 'never']
        if logs:
            session = next(e['sessionId'] for e in events(logs[0]) if e.get('type') == 'session')
            cmd += ['--session-id', session]
        return cmd + [prompt]

    def usage(self, logs, home):
        total = {}
        for log in (home / '.dsh' / 'sessions').rglob('*.jsonl'):
            for e in events(log):
                u = (e.get('data') or {}).get('usage') if e.get('type') in ('assistant/message', 'compaction/summary') else None
                if u:
                    add(total, input_tokens=u.get('inputTokens'), output_tokens=u.get('outputTokens'),
                        cache_read_tokens=u.get('cacheReadTokens'), cache_write_tokens=u.get('cacheWriteTokens'))
        return total


AGENTS = {a.name: a for a in (ClaudeCode(), Codex(), DeepSeekHarness())}
