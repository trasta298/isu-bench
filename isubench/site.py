import json
import re
from pathlib import Path

from . import prices
from .problems import PROBLEMS
from .run import agent_seconds

ROOT = Path(__file__).resolve().parent.parent
AGENT_NAMES = {'claude-code': 'Claude Code', 'codex': 'Codex', 'dsh': 'DeepSeek Harness', 'agy': 'Antigravity CLI'}
MODEL_NAMES = {'claude-fable-5-2': 'Claude Fable 5.2', 'claude-opus-5-5': 'Claude Opus 5.5', 'claude-sonnet-5-5': 'Claude Sonnet 5.5',
               'gpt-6-astra': 'GPT-6 Astra', 'gpt-6.1-sol': 'GPT-6.1 Sol', 'gpt-6.2-astra': 'GPT-6.2 Astra', 'gpt-6.2-sol': 'GPT-6.2 Sol',
               'gemini-4-carbon': 'Gemini 4 Carbon', 'gemini-4-hydrogen': 'Gemini 4 Hydrogen', 'gemini-4-neon': 'Gemini 4 Neon',
               'deepseek-v4.1-flash:1017': 'DeepSeek V4.1 Flash'}
# ISUMAIL leads: it is the problem no model can have seen solved.
ORDER = ['isumail', 'isucon14', 'isucon13', 'isumail-v1']
# Palette slots of site.html in this order, so that the close colors go to lines that never run near each other.
# Configurations sharing a slot never run on the same problem, or one of them starts off on the page (OLD).
COLORS = [['claude-opus-5-5 xhigh', 'gpt-6.2-sol xhigh'], ['claude-fable-5-2 high'], ['gemini-4-carbon medium'],
          ['gpt-6-astra xhigh', 'gemini-4-neon high'], ['deepseek-v4.1-flash:1017 max'], ['gpt-6.1-sol xhigh', 'gemini-4-hydrogen high'], ['claude-opus-5-5 high'],
          ['claude-sonnet-5-5 xhigh', 'gpt-6.2-astra xhigh']]
# Replaced by a newer version of the same line; the page starts with them off where the newer one ran.
OLD = {'gpt-6-astra': 'gpt-6.2-astra', 'gpt-6.1-sol': 'gpt-6.2-sol'}


def build(out):
    results = [r for r in (json.loads(f.read_text()) for f in sorted((ROOT / 'results').glob('*/result.json')))
               if r.get('status') == 'completed' and PROBLEMS[r['problem']].winner]
    colors = {c: i % 8 + 1 for i, slot in enumerate(COLORS) for c in slot}
    for i, c in enumerate(sorted({f"{r['model']} {r['effort']}" for r in results} - colors.keys()), len(COLORS)):
        colors[c] = i % 8 + 1
    problems = []
    # Only the scored problem appears on the page; the others keep their results in the repository.
    shown = {r['problem'] for r in results if PROBLEMS[r['problem']].scored}
    for name in sorted(shown, key=lambda p: ORDER.index(p) if p in ORDER else len(ORDER)):
        problem = PROBLEMS[name]
        ran = {r['model'] for r in results if r['problem'] == name}
        rows = []
        for r in sorted((r for r in results if r['problem'] == name), key=lambda r: -r['final_score']):
            runs = [x['score'] if x['passed'] else None for x in r['runs']]
            version = re.search(r'\d+\.\d+\.\d+[\w.-]*', r['agent_version'])
            u = r.get('usage', {})
            rows.append({
                'config': f"{r['model']} {r['effort']}", 'name': MODEL_NAMES.get(r['model'], r['model']), 'effort': r['effort'],
                'agent': f"{AGENT_NAMES.get(r['agent'], r['agent'])} {version.group() if version else ''}".strip(),
                'final': r['final_score'], 'runs': runs, 'fails': runs.count(None),
                'agent_seconds': agent_seconds(r),
                'cost': prices.cost(r['model'], u), 'prices': prices.PRICES.get(r['model']),
                'usage': {k: u.get(f'{k}_tokens', 0) for k in ('input', 'output', 'cache_read', 'cache_write')},
                'pace': r.get('pace') or [], 'old': OLD.get(r['model']) in ran,
            })
        problems.append({
            'title': problem.title, 'winner': problem.winner, 'initial': problem.initial,
            'runs': max(len(r['runs']) for r in rows),
            'results': rows,
        })
    html = (Path(__file__).parent / 'site.html').read_text()
    # The page's own text is about the first problem.
    for key in ('runs', 'winner', 'initial'):
        html = html.replace('{%s}' % key.upper(), f'{problems[0][key]:,}')
    data = json.dumps({'colors': colors, 'problems': problems}).replace('<', '\\u003c')
    html = html.replace('/*DATA*/', data)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'index.html').write_text(html)
    print(out / 'index.html')
