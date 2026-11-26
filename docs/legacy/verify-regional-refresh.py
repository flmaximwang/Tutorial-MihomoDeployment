import json
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

import yaml

REGIONS = {'美国节点': r'🇺🇸|美国|美國|\|US\|', '日本节点': r'🇯🇵|日本|\|JP\|'}
SNAPSHOTS = {
    'a-snapshot-1': ['甲机场 🇺🇸 旧', '甲机场 日本 01'],
    'a-snapshot-2': ['甲机场 🇺🇸 新', '甲机场 日本 01', '甲机场 |JP| 02'],
    'airport_b': ['乙机场 |US| 01', '乙机场 |JP| 01'],
}
state = {'airport_a': 'a-snapshot-1', 'airport_b': 'airport_b', 'active': 'a-snapshot-1'}


class MockSubscriptions(BaseHTTPRequestHandler):
    def do_GET(self):
        names = SNAPSHOTS[state[self.path.removeprefix('/')]]
        body = yaml.safe_dump({'proxies': [
            {'name': name, 'type': 'socks5', 'server': '127.0.0.1', 'port': 9} for name in names
        ]}, allow_unicode=True).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def run(directory, subscriptions):
    secret = secrets.token_hex(24)
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        controller = '127.0.0.1:' + str(reservation.getsockname()[1])
    config = {
        'external-controller': controller, 'secret': secret, 'log-level': 'warning',
        'proxy-providers': {
            name: {'type': 'http', 'url': f'http://127.0.0.1:{subscriptions.server_port}/{name}',
                   'path': f'./{name}.yaml', 'interval': 2, 'proxy': 'DIRECT'}
            for name in state
        },
        'proxy-groups': [
            {'name': '当前机场', 'type': 'select', 'proxies': ['airport_a', 'airport_b']},
            *({'name': name, 'type': 'select', 'use': [name]} for name in ('airport_a', 'airport_b')),
            *({'name': f'{region}（双来源）', 'type': 'select', 'use': ['airport_a', 'airport_b'], 'filter': pattern}
              for region, pattern in REGIONS.items()),
            *({'name': region, 'type': 'select', 'use': ['active'], 'filter': pattern}
              for region, pattern in REGIONS.items()),
        ],
        'rules': ['DOMAIN,accounts.google.com,美国节点', 'MATCH,当前机场'],
    }
    path = directory / 'config.yaml'
    path.write_text(yaml.safe_dump(config, allow_unicode=True))
    opener = build_opener(ProxyHandler({}))

    def query(path, body=None):
        request = Request(f'http://{controller}{path}', data=json.dumps(body).encode() if body else None,
                          method='PUT' if body else 'GET',
                          headers={'Authorization': f'Bearer {secret}', 'Content-Type': 'application/json'})
        with opener.open(request, timeout=2) as response:
            return json.load(response) if response.status != 204 else None

    def group(name):
        return query('/proxies/' + quote(name, safe=''))

    def wait_members(name, names):
        for _ in range(50):
            if process.poll() is not None:
                raise RuntimeError('Mihomo exited during verification')
            try:
                if set(group(name)['all']) == set(names):
                    return
            except OSError:
                pass
            time.sleep(0.2)
        raise AssertionError((name, names, group(name)['all']))

    with (directory / 'mihomo.log').open('w') as log:
        process = subprocess.Popen(['/opt/mihomo/mihomo', '-d', str(directory), '-f', str(path)],
                                   stdout=log, stderr=log)
        try:
            wait_members('美国节点（双来源）', ['甲机场 🇺🇸 旧', '乙机场 |US| 01'])
            wait_members('美国节点', ['甲机场 🇺🇸 旧'])
            rules = query('/rules')['rules']
            state.update(airport_a='a-snapshot-2', active='a-snapshot-2')
            wait_members('美国节点（双来源）', ['甲机场 🇺🇸 新', '乙机场 |US| 01'])
            wait_members('美国节点', ['甲机场 🇺🇸 新'])
            wait_members('日本节点', ['甲机场 日本 01', '甲机场 |JP| 02'])
            print('Automatic subscription refresh removed old nodes and added new ones to both region groups.')
            query('/proxies/' + quote('当前机场', safe=''), {'name': 'airport_b'})
            assert group('当前机场')['now'] == 'airport_b'
            wait_members('美国节点（双来源）', ['甲机场 🇺🇸 新', '乙机场 |US| 01'])
            wait_members('美国节点', ['甲机场 🇺🇸 新'])
            print('Changing the native selector did not change provider membership.')
            state['active'] = 'airport_b'
            wait_members('美国节点', ['乙机场 |US| 01'])
            wait_members('日本节点', ['乙机场 |JP| 01'])
            assert query('/rules')['rules'] == rules
            print('Switching the single active subscription source rebuilt both region groups with the selected airport only.')
        finally:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
    with ThreadingHTTPServer(('127.0.0.1', 0), MockSubscriptions) as subscriptions:
        worker = threading.Thread(target=subscriptions.serve_forever)
        worker.start()
        try:
            run(Path(directory), subscriptions)
        finally:
            subscriptions.shutdown()
            worker.join()
