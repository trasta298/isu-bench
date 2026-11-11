import json
import re
from pathlib import Path

from . import prices
from .problems import PROBLEMS
from .run import agent_seconds

ROOT = Path(__file__).resolve().parent.parent
AGENT_NAMES = {'claude-code': 'Claude Code', 'codex': 'Codex', 'dsh': 'DeepSeek Harness', 'agy': 'Antigravity CLI'}
# ISUMAIL leads: it is the problem no model can have seen solved.
ORDER = ['isumail', 'isucon14', 'isucon13', 'isumail-v1']
# Palette slots of site.html in this order, so that the close colors go to lines that never run near each other.
COLORS = ['claude-opus-5-5 xhigh', 'claude-fable-5-2 high', 'gemini-4-carbon medium', 'gpt-6-astra xhigh',
          'deepseek-v4.1-flash:1017 max', 'gpt-6.1-sol xhigh', 'claude-opus-5-5 high', 'claude-sonnet-5-5 xhigh']


def build(out):
    results = [r for r in (json.loads(f.read_text()) for f in sorted((ROOT / 'results').glob('*/result.json')))
               if r.get('status') == 'completed' and PROBLEMS[r['problem']].winner]
    configs = COLORS + sorted({f"{r['model']} {r['effort']}" for r in results} - set(COLORS))
    problems = []
    for name in sorted({r['problem'] for r in results}, key=lambda p: ORDER.index(p) if p in ORDER else len(ORDER)):
        problem = PROBLEMS[name]
        rows = []
        for r in sorted((r for r in results if r['problem'] == name), key=lambda r: -r['final_score']):
            runs = [x['score'] if x['passed'] else None for x in r['runs']]
            version = re.search(r'\d+\.\d+\.\d+[\w.-]*', r['agent_version'])
            u = r.get('usage', {})
            rows.append({
                'config': f"{r['model']} {r['effort']}",
                'agent': f"{AGENT_NAMES.get(r['agent'], r['agent'])} {version.group() if version else ''}".strip(),
                'final': r['final_score'], 'runs': runs, 'best': max((v for v in runs if v is not None), default=0),
                'fails': runs.count(None), 'uncounted': len(r.get('infra_failures', [])),
                'agent_seconds': agent_seconds(r), 'output_tokens': u.get('output_tokens', 0),
                'cost': prices.cost(r['model'], u), 'prices': prices.PRICES.get(r['model']),
                'usage': {k: u.get(f'{k}_tokens', 0) for k in ('input', 'output', 'cache_read', 'cache_write')},
                'cache_read_tokens': u.get('cache_read_tokens', 0), 'pace': r.get('pace') or [],
            })
        problems.append({
            'name': name, 'title': problem.title, 'winner': problem.winner, 'initial': problem.initial,
            'reference': 'theory' if name.startswith('isumail') else 'winner',
            'extra': not problem.scored, 'retired': problem.retired, 'runs': max(len(r['runs']) for r in rows),
            'results': rows,
        })
    data = json.dumps({'configs': configs, 'problems': problems}).replace('<', '\\u003c')
    html = (Path(__file__).parent / 'site.html').read_text().replace('/*DATA*/', data)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'index.html').write_text(html)
    print(out / 'index.html')
