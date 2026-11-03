import json
import subprocess
import time
import urllib.request
from pathlib import Path

REGION = 'ap-northeast-1'
# An AZ ID names the same zone in every account, unlike ap-northeast-1a.
ZONE_ID = 'apne1-az4'
TAG = 'isu-bench:run'

TTL_SCRIPT = '''#!/bin/sh
echo @EXPIRES@ > /var/lib/isu-bench-expires-at
cat <<'CRON' > /etc/cron.d/isu-bench-ttl
* * * * * root [ "$(date +\\%s)" -ge "$(cat /var/lib/isu-bench-expires-at)" ] && /sbin/shutdown -P now "isu-bench TTL expired"
CRON
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


def permissions(rules, sg, client_ip):
    peer = lambda source: ({'IpRanges': [{'CidrIp': client_ip + '/32'}]} if source == 'client' else
                           {'UserIdGroupPairs': [{'GroupId': sg[source]}]})
    return [{'IpProtocol': '-1', **peer(source)} if port is None else
            {'IpProtocol': 'tcp', 'FromPort': port, 'ToPort': port, **peer(source)}
            for source, port in rules]


def check_types(problem, images):
    types = {t['InstanceType']: t for t in aws('ec2', 'describe-instance-types', '--instance-types',
                                                *set(problem.types.values()))['InstanceTypes']}
    for role, itype in problem.types.items():
        arch = images[problem.amis[role]]['Architecture']
        if arch not in types[itype]['ProcessorInfo']['SupportedArchitectures']:
            raise RuntimeError(f'{role}: {itype} cannot run the {arch} image {problem.amis[role]}')
        need = 1 + len(problem.secondary_addresses.get(role, ()))
        if types[itype]['NetworkInfo']['Ipv4AddressesPerInterface'] < need:
            raise RuntimeError(f'{role}: {itype} cannot hold {need} addresses on one interface')


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

    def provision(self, public_key: Path, ttl_hours: int, problem):
        s = self.state
        if 'client_ip' not in s:
            self.save('client_ip', urllib.request.urlopen('https://checkip.amazonaws.com', timeout=20).read().decode().strip())
        if 'vpc' not in s:
            self.save('vpc', aws('ec2', 'create-vpc', '--cidr-block', '10.0.0.0/16',
                                 '--tag-specifications', self.tags('vpc', 'vpc'))['Vpc']['VpcId'])
        if 'subnet' not in s:
            self.save('subnet', aws('ec2', 'create-subnet', '--vpc-id', s['vpc'], '--cidr-block', '10.0.1.0/24',
                                    '--availability-zone-id', ZONE_ID,
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
            names = sorted(set(problem.groups.values()), key=lambda g: (g != 'app', g))
            sg = {role: aws('ec2', 'create-security-group', '--vpc-id', s['vpc'], '--group-name', f'{self.run_id}-{role}',
                            '--description', f'isu-bench {role}',
                            '--tag-specifications', self.tags('security-group', f'sg-{role}'))['GroupId']
                  for role in names}
            self.save('sg', sg)
            for name in names:
                if problem.ingress.get(name):
                    aws('ec2', 'authorize-security-group-ingress', '--group-id', sg[name],
                        '--ip-permissions', json.dumps(permissions(problem.ingress[name], sg, s['client_ip'])))
            for name, targets in problem.egress.items():
                # Competition hosts reach the internet only through the allowlist proxy on the benchmark host.
                aws('ec2', 'revoke-security-group-egress', '--group-id', sg[name],
                    '--ip-permissions', json.dumps([{'IpProtocol': '-1', 'IpRanges': [{'CidrIp': '0.0.0.0/0'}]}]))
                aws('ec2', 'authorize-security-group-egress', '--group-id', sg[name],
                    '--ip-permissions', json.dumps(permissions([(t, None) for t in targets], sg, s['client_ip'])))
        if 'key' not in s:
            aws('ec2', 'import-key-pair', '--key-name', self.run_id, '--public-key-material', f'fileb://{public_key}',
                '--tag-specifications', self.tags('key-pair', 'key'))
            self.save('key', self.run_id)
        amis = problem.amis
        if 'image' not in s:
            keep = ('ImageId', 'Name', 'OwnerId', 'CreationDate', 'Architecture')
            images = {i['ImageId']: i for i in aws('ec2', 'describe-images', '--image-ids', *set(amis.values()))['Images']}
            self.save('image', {k: images[amis['s1']][k] for k in keep[:4]})
            if len(images) > 1:
                self.save('images', {role: {k: images[ami][k] for k in keep} for role, ami in amis.items()})
                check_types(problem, images)
        expires = int(time.time()) + ttl_hours * 3600
        instances = dict(s.get('instances', {}))
        for role, itype in problem.types.items():
            if role in instances:
                continue
            user_data = TTL_SCRIPT.replace('@EXPIRES@', str(expires))
            group = s['sg'][problem.groups[role]]
            if role in problem.addresses:
                addresses = [problem.addresses[role], *problem.secondary_addresses.get(role, ())]
                network = ['--network-interfaces', json.dumps([{
                    'DeviceIndex': 0, 'SubnetId': s['subnet'], 'Groups': [group], 'AssociatePublicIpAddress': True,
                    'DeleteOnTermination': True,
                    'PrivateIpAddresses': [{'PrivateIpAddress': a, 'Primary': i == 0} for i, a in enumerate(addresses)]}])]
            else:
                network = ['--security-group-ids', group, '--subnet-id', s['subnet']]
            res = aws('ec2', 'run-instances', '--image-id', amis[role], '--instance-type', itype, '--count', '1',
                      '--key-name', s['key'], *network,
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

    def close_client_ssh(self, problem):
        for name in sorted({problem.groups[r] for r in problem.apps}):
            if ('client', 22) in problem.ingress.get(name, []):
                aws('ec2', 'revoke-security-group-ingress', '--group-id', self.state['sg'][name],
                    '--ip-permissions', json.dumps(permissions([('client', 22)], self.state['sg'], self.state['client_ip'])))

    def audit(self, problem, ssh_closed=False):
        s = self.state
        diffs = problem.security_group_differences(
            aws('ec2', 'describe-security-groups', '--group-ids', *s['sg'].values())['SecurityGroups'],
            s['sg'], s['client_ip'], ssh_closed)
        diffs += problem.interface_differences(
            aws('ec2', 'describe-network-interfaces', '--filters', f"Name=vpc-id,Values={s['vpc']}")['NetworkInterfaces'],
            s['sg'], s['instances'])
        vpc = aws('ec2', 'describe-vpcs', '--vpc-ids', s['vpc'])['Vpcs'][0]
        if vpc.get('Ipv6CidrBlockAssociationSet'):
            diffs.append('the VPC has an IPv6 CIDR')
        for table in aws('ec2', 'describe-route-tables', '--filters', f"Name=vpc-id,Values={s['vpc']}")['RouteTables']:
            for route in table['Routes']:
                if not (route.get('GatewayId') == 'local' or
                        (route.get('GatewayId') == s['igw'] and route.get('DestinationCidrBlock') == '0.0.0.0/0')):
                    diffs.append(f'unexpected route {route}')
        return diffs

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
