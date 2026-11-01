import json
import subprocess
import time
import urllib.request
from pathlib import Path

REGION = 'ap-northeast-1'
ZONE = 'ap-northeast-1a'
AMI = 'ami-0fcf9e8e8675a9ee4'
TYPES = {'s1': 'c5.large', 's2': 'c5.large', 's3': 'c5.large', 'bench': 'c5.2xlarge'}
TAG = 'isu-bench:run'

TTL_SCRIPT = '''#!/bin/sh
echo @EXPIRES@ > /var/lib/isu-bench-expires-at
cat <<'CRON' > /etc/cron.d/isu-bench-ttl
* * * * * root [ "$(date +\\%s)" -ge "$(cat /var/lib/isu-bench-expires-at)" ] && /sbin/shutdown -P now "isu-bench TTL expired"
CRON
'''

# mem=9G yields about 8 GiB on Nitro because 3-4 GiB of the physical address space is a device hole.
MEM_SCRIPT = '''
if ! grep -qw mem=9G /proc/cmdline && [ ! -e /var/lib/isu-bench-mem ]; then
  mkdir -p /etc/default/grub.d
  echo 'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT mem=9G"' > /etc/default/grub.d/zz-isu-bench-mem.cfg
  touch /var/lib/isu-bench-mem
  update-grub && shutdown -r now
fi
'''


def aws_retry(*args, tries=12):
    for attempt in range(tries):
        try:
            return aws(*args)
        except RuntimeError as e:
            if 'DependencyViolation' not in str(e) or attempt == tries - 1:
                raise
            time.sleep(10)


def aws(*args):
    proc = subprocess.run(['aws', '--region', REGION, '--output', 'json', '--no-cli-pager', *args],
                          capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(f'aws {" ".join(args[:2])} failed: {proc.stderr.strip()}')
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def clean(permissions):
    keep = ('IpProtocol', 'FromPort', 'ToPort', 'IpRanges', 'Ipv6Ranges', 'PrefixListIds')
    return [{**{k: p[k] for k in keep if p.get(k) not in (None, [])},
             **({'UserIdGroupPairs': [{'GroupId': g['GroupId']} for g in p['UserIdGroupPairs']]}
                if p.get('UserIdGroupPairs') else {})} for p in permissions]


class Infra:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self.state = json.loads(path.read_text()) if path.exists() else {}

    def save(self, key, value):
        self.state[key] = value
        self.path.write_text(json.dumps(self.state, indent=2) + '\n')

    def tags(self, resource, name):
        return json.dumps([{'ResourceType': resource, 'Tags': [
            {'Key': 'Name', 'Value': f'{self.run_id}-{name}'}, {'Key': TAG, 'Value': self.run_id}]}])

    def provision(self, public_key: Path, ttl_hours: int):
        s = self.state
        if 'client_ip' not in s:
            self.save('client_ip', urllib.request.urlopen('https://checkip.amazonaws.com', timeout=20).read().decode().strip())
        if 'vpc' not in s:
            self.save('vpc', aws('ec2', 'create-vpc', '--cidr-block', '10.0.0.0/16',
                                 '--tag-specifications', self.tags('vpc', 'vpc'))['Vpc']['VpcId'])
        if 'subnet' not in s:
            self.save('subnet', aws('ec2', 'create-subnet', '--vpc-id', s['vpc'], '--cidr-block', '10.0.1.0/24',
                                    '--availability-zone', ZONE,
                                    '--tag-specifications', self.tags('subnet', 'subnet'))['Subnet']['SubnetId'])
            aws('ec2', 'modify-subnet-attribute', '--subnet-id', s['subnet'], '--map-public-ip-on-launch')
        if 'igw' not in s:
            self.save('igw', aws('ec2', 'create-internet-gateway',
                                 '--tag-specifications', self.tags('internet-gateway', 'igw'))['InternetGateway']['InternetGatewayId'])
            aws('ec2', 'attach-internet-gateway', '--internet-gateway-id', s['igw'], '--vpc-id', s['vpc'])
            table = aws('ec2', 'describe-route-tables', '--filters', f'Name=vpc-id,Values={s["vpc"]}')['RouteTables'][0]
            aws('ec2', 'create-route', '--route-table-id', table['RouteTableId'],
                '--destination-cidr-block', '0.0.0.0/0', '--gateway-id', s['igw'])
        if 'sg' not in s:
            sg = {role: aws('ec2', 'create-security-group', '--vpc-id', s['vpc'], '--group-name', f'{self.run_id}-{role}',
                            '--description', f'isu-bench {role}',
                            '--tag-specifications', self.tags('security-group', f'sg-{role}'))['GroupId']
                  for role in ('app', 'bench')}
            self.save('sg', sg)
            group = lambda g: [{'IpProtocol': '-1', 'UserIdGroupPairs': [{'GroupId': sg[g]}]}]
            client = lambda *ports: [{'IpProtocol': 'tcp', 'FromPort': p, 'ToPort': p,
                                      'IpRanges': [{'CidrIp': s['client_ip'] + '/32'}]} for p in ports]
            aws('ec2', 'authorize-security-group-ingress', '--group-id', sg['app'],
                '--ip-permissions', json.dumps(group('app') + group('bench') + client(22, 443)))
            aws('ec2', 'authorize-security-group-ingress', '--group-id', sg['bench'],
                '--ip-permissions', json.dumps(group('app') + client(22)))
            # Competition hosts reach the internet only through the allowlist proxy on the benchmark host.
            aws('ec2', 'revoke-security-group-egress', '--group-id', sg['app'],
                '--ip-permissions', json.dumps([{'IpProtocol': '-1', 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}]))
            aws('ec2', 'authorize-security-group-egress', '--group-id', sg['app'],
                '--ip-permissions', json.dumps(group('app') + group('bench')))
        if 'key' not in s:
            aws('ec2', 'import-key-pair', '--key-name', self.run_id, '--public-key-material', f'fileb://{public_key}',
                '--tag-specifications', self.tags('key-pair', 'key'))
            self.save('key', self.run_id)
        if 'image' not in s:
            image = aws('ec2', 'describe-images', '--image-ids', AMI)['Images'][0]
            self.save('image', {k: image[k] for k in ('ImageId', 'Name', 'OwnerId', 'CreationDate')})
        expires = int(time.time()) + ttl_hours * 3600
        instances = dict(s.get('instances', {}))
        for role, itype in TYPES.items():
            if role in instances:
                continue
            user_data = TTL_SCRIPT.replace('@EXPIRES@', str(expires)) + (MEM_SCRIPT if role == 'bench' else '')
            res = aws('ec2', 'run-instances', '--image-id', AMI, '--instance-type', itype, '--count', '1',
                      '--key-name', s['key'], '--security-group-ids', s['sg']['bench' if role == 'bench' else 'app'],
                      '--subnet-id', s['subnet'],
                      '--instance-initiated-shutdown-behavior', 'terminate',
                      '--metadata-options', 'HttpTokens=required,HttpEndpoint=enabled',
                      '--block-device-mappings', json.dumps([{'DeviceName': '/dev/sda1', 'Ebs': {
                          'VolumeSize': 20, 'VolumeType': 'gp3', 'DeleteOnTermination': True}}]),
                      '--user-data', user_data,
                      '--tag-specifications', json.dumps([json.loads(self.tags(r, role))[0] for r in ('instance', 'volume')]))
            instances[role] = res['Instances'][0]['InstanceId']
            self.save('instances', instances)
        aws('ec2', 'wait', 'instance-running', '--instance-ids', *instances.values())
        found = {i['InstanceId']: i for r in aws('ec2', 'describe-instances', '--instance-ids', *instances.values())['Reservations']
                 for i in r['Instances']}
        self.save('hosts', {role: {'public': found[iid]['PublicIpAddress'], 'private': found[iid]['PrivateIpAddress']}
                            for role, iid in instances.items()})

    def reboot(self, roles):
        aws('ec2', 'reboot-instances', '--instance-ids', *(self.state['instances'][r] for r in roles))

    def teardown(self):
        s = self.state
        if s.get('instances'):
            ids = list(s['instances'].values())
            aws('ec2', 'terminate-instances', '--instance-ids', *ids)
            aws('ec2', 'wait', 'instance-terminated', '--instance-ids', *ids)
            self.save('instances', {})
        if s.get('key'):
            aws('ec2', 'delete-key-pair', '--key-name', s['key'])
            self.save('key', None)
        if s.get('sg'):
            # The two groups reference each other, so neither can be deleted until both are emptied.
            for group in aws('ec2', 'describe-security-groups', '--group-ids', *s['sg'].values())['SecurityGroups']:
                if group['IpPermissions']:
                    aws('ec2', 'revoke-security-group-ingress', '--group-id', group['GroupId'],
                        '--ip-permissions', json.dumps(clean(group['IpPermissions'])))
                if group['IpPermissionsEgress']:
                    aws('ec2', 'revoke-security-group-egress', '--group-id', group['GroupId'],
                        '--ip-permissions', json.dumps(clean(group['IpPermissionsEgress'])))
            for group_id in s['sg'].values():
                aws_retry('ec2', 'delete-security-group', '--group-id', group_id)
            self.save('sg', None)
        if s.get('igw'):
            aws_retry('ec2', 'detach-internet-gateway', '--internet-gateway-id', s['igw'], '--vpc-id', s['vpc'])
            aws('ec2', 'delete-internet-gateway', '--internet-gateway-id', s['igw'])
            self.save('igw', None)
        if s.get('subnet'):
            aws_retry('ec2', 'delete-subnet', '--subnet-id', s['subnet'])
            self.save('subnet', None)
        if s.get('vpc'):
            aws_retry('ec2', 'delete-vpc', '--vpc-id', s['vpc'])
            self.save('vpc', None)
