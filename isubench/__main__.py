import argparse

from . import agents, run

parser = argparse.ArgumentParser(prog='python3 -m isubench')
sub = parser.add_subparsers(dest='command', required=True)
p = sub.add_parser('run', help='provision, run one agent until its 15th benchmark, verify, tear down')
p.add_argument('--agent', required=True, choices=agents.AGENTS)
p.add_argument('--model', required=True)
p.add_argument('--effort', required=True)
p.add_argument('--lang', default='go', choices=run.LANGS)
p.add_argument('--keep', action='store_true', help='leave the AWS resources running for inspection')
p = sub.add_parser('teardown', help='delete the AWS resources of a run')
p.add_argument('run_id')
sub.add_parser('table', help='print the results table')

args = parser.parse_args()
if args.command == 'run':
    run.run(args.agent, args.model, args.effort, args.lang, args.keep)
elif args.command == 'teardown':
    run.teardown(args.run_id)
else:
    run.table()
