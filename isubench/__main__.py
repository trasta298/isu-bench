import argparse

from . import agents, run, site
from .problems import LANGS, PROBLEMS

parser = argparse.ArgumentParser(prog='python3 -m isubench')
sub = parser.add_subparsers(dest='command', required=True)
p = sub.add_parser('run', help='provision, run one agent until its last benchmark, verify, tear down')
p.add_argument('--problem', default='isucon14', choices=PROBLEMS)
p.add_argument('--agent', required=True, choices=agents.AGENTS)
p.add_argument('--model', required=True)
p.add_argument('--effort', required=True)
p.add_argument('--lang', default='go', choices=LANGS)
p.add_argument('--keep', action='store_true', help='leave the AWS resources running for inspection')
p = sub.add_parser('teardown', help='delete the AWS resources of a run')
p.add_argument('run_id')
sub.add_parser('table', help='print the results table')
sub.add_parser('pace', help='add the time and work before each run to results/*/result.json from the transcripts')
p = sub.add_parser('site', help='write the results page')
p.add_argument('--out', default='site')
p = sub.add_parser('calibrate', help='provision without an agent, run the benchmark N times and the reboot check, tear down')
p.add_argument('--problem', default='isucon14', choices=PROBLEMS)
p.add_argument('-n', type=int, default=5, help='number of counted benchmark runs')
p.add_argument('--deploy', help='tarball of an implementation to install with the problem\'s deploy hook')
p.add_argument('--keep', action='store_true', help='leave the AWS resources running for inspection')
p = sub.add_parser('quota', help='compare the On-Demand Standard vCPU quota with what parallel trials need')
p.add_argument('--problem', default='isucon14', choices=PROBLEMS)
p.add_argument('--trials', type=int, default=1)

args = parser.parse_args()
if args.command == 'run':
    run.run(args.problem, args.agent, args.model, args.effort, args.lang, args.keep)
elif args.command == 'teardown':
    run.teardown(args.run_id)
elif args.command == 'calibrate':
    run.calibrate(args.problem, args.n, args.deploy, args.keep)
elif args.command == 'quota':
    run.quota(args.problem, args.trials)
elif args.command == 'site':
    site.build(args.out)
elif args.command == 'pace':
    run.backfill_pace()
else:
    run.table()
