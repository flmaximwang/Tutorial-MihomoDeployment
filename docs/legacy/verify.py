import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, ProxyHandler, build_opener
import yaml

runtime = Path(__file__).parent / 'runtime'
config = runtime / os.environ.get('TRIAL_CONFIG', 'config.yaml')
settings = yaml.safe_load(config.read_text())
secret = settings['secret']
api = build_opener(ProxyHandler({}))
base = 'http://127.0.0.1:19091'

def request(path, body=None):
    req = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                  headers={'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json'},
                  method='PUT' if body is not None else 'GET')
    with api.open(req, timeout=2) as response:
        return json.loads(response.read()) if response.status != 204 else None

with (runtime / 'test.log').open('w') as log:
    process = subprocess.Popen(['/opt/mihomo/mihomo', '-d', str(runtime), '-f', str(config)], stdout=log, stderr=log)
    try:
        for _ in range(32):
            if process.poll() is not None:
                raise RuntimeError('isolated Mihomo exited')
            try:
                request('/version')
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError('isolated controller not ready')
        policy = '/proxies/' + quote('当前机场')
        provider_counts = {}
        for name in ('airport_a', 'airport_b'):
            for _ in range(40):
                provider = request('/providers/proxies')['providers'].get(name, {'proxies': []})
                if provider['proxies']:
                    break
                time.sleep(0.25)
            provider_counts[name] = len(provider['proxies'])
            assert provider_counts[name] > 0, f'{name} initialized with no nodes'
            assert all(p['name'].startswith(name + ' | ') for p in provider['proxies'])
        print('providers:', provider_counts, flush=True)
        def active_rules():
            return [{key: rule[key] for key in ('type', 'payload', 'proxy')} for rule in request('/rules')['rules']]

        # The controller and proxy providers can be ready before the listener serves traffic.
        time.sleep(1)
        rules = active_rules()
        discord_target = next(rule.split(',')[2] for rule in settings['rules'] if rule.startswith('DOMAIN-SUFFIX,discord.com,'))
        if discord_target == '选择代理':
            request('/proxies/' + quote(discord_target), {'name': '当前机场'})
        assert any(r['payload'] == 'discord.com' and r['proxy'] == discord_target for r in rules)
        assert any(r['payload'] == 'accounts.google.com' and r['proxy'] == '美国节点' for r in rules)
        assert len(rules) == len(settings['rules'])
        print('active rules fingerprint:', hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()[:16], flush=True)
        with (runtime / 'test.log').open() as trace:
            trace.seek(0, 2)
            for name in ('airport_a', 'airport_b', 'airport_a'):
                request(policy, {'name': name})
                assert request(policy)['now'] == name
                assert active_rules() == rules
                selected = request('/proxies/' + name)['now']
                assert selected.startswith(name + ' | ')
                result = subprocess.run(['curl', '-q', '--noproxy', '', '-s', '--connect-timeout', '5', '--max-time', '8', '-o', '/dev/null', '-w', '%{http_code}', '-x', 'http://127.0.0.1:17891', 'https://discord.com/api/v10/gateway'], capture_output=True, text=True)
                time.sleep(0.1)
                event = [line for line in trace.readlines() if 'discord.com:443' in line and 'match DomainSuffix(discord.com)' in line and f'[{name} | ' in line]
                print(name, 'HTTP', result.stdout, 'curl exit', result.returncode, 'matched route', bool(event), flush=True)
                if not event:
                    raise RuntimeError('missing matching Discord route in runtime log')
        for region in ('JP', 'US', 'SG', 'HK'):
            candidates = request('/proxies/airport_b')['all']
            node = next((item for item in candidates if f'|{region}|' in item), None)
            if node:
                request(policy, {'name': 'airport_b'})
                request('/proxies/airport_b', {'name': node})
                assert request('/proxies/airport_b')['now'] == node
                result = subprocess.run(['curl', '-q', '--noproxy', '', '-s', '--connect-timeout', '4', '--max-time', '5', '-o', '/dev/null', '-w', '%{http_code}', '-x', 'http://127.0.0.1:17891', 'https://discord.com/api/v10/gateway'], capture_output=True, text=True)
                print('airport_b Discord probe', region, 'HTTP', result.stdout, 'curl exit', result.returncode, flush=True)
        request(policy, {'name': 'airport_a'})
        assert active_rules() == rules
        antigravity = request('/proxies/' + quote('美国节点'))
        print('Antigravity US group candidates:', len(antigravity['all']), flush=True)
        assert antigravity['all'] and antigravity['now'] in antigravity['all']
    finally:
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
