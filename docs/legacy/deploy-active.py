import copy
import fcntl
import grp
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

import yaml

os.umask(0o077)
root = Path(__file__).resolve().parent
config_path = Path('/etc/mihomo-prod/config.yaml')
data_dir = Path('/opt/mihomo')
binary = data_dir / 'mihomo'
cache_name = 'active-airport.yaml'
sync_timer = 'mihomo-sync-sub.timer'
watchdog_timer = 'mihomo-watchdog.timer'
regions = {
    '香港节点': r'🇭🇰|香港|Hong Kong|\|HK\|',
    '台湾节点': r'🇹🇼|台湾|台灣|\|TW\|',
    '新加坡节点': r'🇸🇬|新加坡|\|SG\|',
    '日本节点': r'🇯🇵|日本|\|JP\|',
    '美国节点': r'🇺🇸|🇺🇲|美国|美國|\|US\|',
}
opener = build_opener(ProxyHandler({}))


def run(*args, timeout=30):
    return subprocess.run(args, check=True, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def active(name):
    return subprocess.run(('systemctl', 'is-active', '--quiet', name), check=False).returncode == 0


def api(config, path, body=None):
    request = Request('http://' + config['external-controller'] + path,
                      data=json.dumps(body).encode() if body is not None else None,
                      method='PUT' if body is not None else 'GET',
                      headers={'Authorization': 'Bearer ' + config['secret'], 'Content-Type': 'application/json'})
    with opener.open(request, timeout=15 if body is not None else 2) as response:
        return None if response.status == 204 else json.load(response)


def atomic_install(source, target, owner, group, mode):
    pending = target.with_name(target.name + '.active-deploy-staged')
    run('install', '-o', owner, '-g', group, '-m', mode, str(source), str(pending))
    os.replace(pending, target)


def ports():
    sockets = [socket.socket() for _ in range(3)]
    try:
        for item in sockets:
            item.bind(('127.0.0.1', 0))
        return [item.getsockname()[1] for item in sockets]
    finally:
        for item in sockets:
            item.close()


def wait_ready(config, count, process=None):
    for _ in range(60):
        if process is not None and process.poll() is not None:
            raise RuntimeError('canary exited')
        try:
            available = api(config, '/providers/proxies')['providers'].get('active', {}).get('proxies', [])
            rules = api(config, '/rules')['rules']
            proxies = api(config, '/proxies')['proxies']
            if available and len(rules) == count and all(name in proxies for name in regions):
                nodes = {item['name'] for item in available}
                assert set(proxies['手动选择']['all']) == nodes
                sizes = {}
                for name, pattern in regions.items():
                    expected = {node for node in nodes if re.search(pattern, node)}
                    assert expected and set(proxies[name]['all']) == expected
                    sizes[name] = len(expected)
                assert any(rule['payload'] == 'accounts.google.com' and rule['proxy'] == '美国节点' for rule in rules)
                return len(nodes), sizes
        except OSError:
            pass
        time.sleep(0.25)
    raise RuntimeError('controller or active provider failed readiness check')


def select(config, selections):
    groups = api(config, '/proxies')['proxies']
    count = 0
    for name, target in selections.items():
        if target in groups.get(name, {}).get('all', []):
            api(config, '/proxies/' + quote(name, safe=''), {'name': target})
            assert api(config, '/proxies/' + quote(name, safe=''))['now'] == target
            count += 1
    return count


def probe(config, url, expected=None):
    result = subprocess.run(['curl', '-q', '-sS', '--connect-timeout', '4', '--max-time', '8',
                             '--noproxy', '', '-x', f'http://127.0.0.1:{config["mixed-port"]}',
                             '-o', '/dev/null', '-w', '%{http_code}', url],
                            capture_output=True, text=True, timeout=10)
    status = result.stdout
    if result.returncode or (status != expected if expected else status == '000' or len(status) != 3):
        raise RuntimeError(f'HTTPS probe failed ({status or "no status"}, curl={result.returncode})')
    return status


def verify_traffic(config):
    return {
        'Discord': probe(config, 'https://discord.com/api/v10/gateway', '200'),
        'GitHub': probe(config, 'https://github.com/', '200'),
        'Google OAuth endpoint': probe(config, 'https://accounts.google.com/'),
    }


def main():
    original_bytes = config_path.read_bytes()
    original = yaml.safe_load(original_bytes)
    original_mode = oct(config_path.stat().st_mode & 0o777)[2:]
    managed = yaml.safe_load((root / 'mihomo-managed-rules.yaml').read_text())
    assert len(managed['rules']) == 6285 and set(original['rules']) <= set(managed['rules'])
    assert original.get('proxy-providers') is None and not (data_dir / cache_name).exists()
    assert active('mihomo.service') and not active('mihomo-sync-sub.service')
    old_selections = {name: group['now'] for name, group in api(original, '/proxies')['proxies'].items()
                      if group.get('type') == 'Selector' or name in regions}
    old_by_name = {node['name']: node for node in original['proxies']}
    source = next(item for item in json.loads(Path('/opt/sub-store/data/sub-store.json').read_text())['subs']
                  if item['name'] == 'airport_a')
    assert source['url'].startswith('https://')
    updated = copy.deepcopy(original)
    updated.pop('proxies')
    updated.update(managed)
    updated['proxy-providers'] = {'active': {'type': 'http', 'url': source['url'],
        'path': './' + cache_name, 'interval': 21600, 'proxy': 'DIRECT',
        'header': {'User-Agent': ['mihomo/1.19.33']},
        'health-check': {'enable': True, 'url': 'https://www.gstatic.com/generate_204', 'interval': 60}}}
    for group in updated['proxy-groups']:
        name = group['name']
        group['proxies'] = [item for item in group.get('proxies', []) if item not in old_by_name]
        if name in regions or name == '手动选择':
            group['use'] = ['active']
        if name in regions:
            group.update(type='select', filter=regions[name])
            for key in ('url', 'interval', 'tolerance'):
                group.pop(key, None)
        if not group['proxies']:
            group.pop('proxies')
    override = yaml.safe_load(Path('/etc/mihomo-prod/overrides/antigravity-rules.yaml').read_text())
    assert set(override['rules']) <= set(updated['rules'])

    backup = config_path.with_name('config.yaml.bak-before-active-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    atomic_install(config_path, backup, 'root', 'mihomo', '0640')
    print('Backup:', backup, flush=True)
    scratch = root / ('active-deploy-' + str(os.getpid()))
    scratch.mkdir()
    staged = scratch / 'config.yaml'
    staged.write_text(yaml.safe_dump(updated, allow_unicode=True, sort_keys=False))
    canary_dir = scratch / 'canary'
    canary_dir.mkdir()
    for name in ('Country.mmdb', 'geoip.dat', 'geosite.dat'):
        (canary_dir / name).symlink_to(data_dir / name)
    shutil.copytree(data_dir / 'ruleset', canary_dir / 'ruleset')
    canary = copy.deepcopy(updated)
    mixed, controller, dns = ports()
    canary.update({'mixed-port': mixed, 'external-controller': f'127.0.0.1:{controller}',
                   'allow-lan': False, 'bind-address': '127.0.0.1'})
    canary['dns']['listen'] = f'127.0.0.1:{dns}'
    canary['tun']['enable'] = False
    for provider in canary['rule-providers'].values():
        if (canary_dir / provider['path']).is_file():
            provider['type'] = 'file'
            provider.pop('url', None)
    canary_file = canary_dir / 'config.yaml'
    canary_file.write_text(yaml.safe_dump(canary, allow_unicode=True, sort_keys=False))
    run(str(binary), '-t', '-d', str(canary_dir), '-f', str(staged), timeout=30)
    with (scratch / 'canary.log').open('w') as log:
        process = subprocess.Popen([str(binary), '-d', str(canary_dir), '-f', str(canary_file)], stdout=log, stderr=log)
        try:
            print('Canary nodes and regions:', wait_ready(canary, len(updated['rules']), process), flush=True)
            nodes = yaml.safe_load((canary_dir / cache_name).read_text())['proxies']
            mapped = {}
            for group, current in old_selections.items():
                if current in old_by_name:
                    previous = old_by_name[current]
                    match = next((node['name'] for node in nodes if all(node.get(key) == previous.get(key)
                                  for key in ('type', 'server', 'port'))), None)
                    if match:
                        mapped[group] = match
                else:
                    mapped[group] = current
            assert mapped.get('手动选择') and mapped.get('美国节点')
            print('Canary selections preserved:', select(canary, mapped), flush=True)
            print('Canary HTTPS:', verify_traffic(canary), flush=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    assert config_path.read_bytes() == original_bytes
    was_sync_active = active(sync_timer)
    was_watchdog_active = active(watchdog_timer)
    was_sync_enabled = subprocess.run(['systemctl', 'is-enabled', '--quiet', sync_timer]).returncode == 0
    installed = False
    try:
        run('systemctl', 'stop', sync_timer, watchdog_timer)
        assert not active('mihomo-sync-sub.service')
        run('systemctl', 'stop', 'mihomo-watchdog.service')
        assert config_path.read_bytes() == original_bytes
        atomic_install(canary_dir / cache_name, data_dir / cache_name, 'mihomo', 'mihomo', '0640')
        atomic_install(staged, config_path, 'root', 'mihomo', '0640')
        installed = True
        run('systemctl', 'restart', 'mihomo.service', timeout=35)
        print('Production nodes and regions:', wait_ready(updated, len(updated['rules'])), flush=True)
        print('Production selections preserved:', select(updated, mapped), flush=True)
        print('Production HTTPS:', verify_traffic(updated), flush=True)
        assert active('mihomo.service') and config_path.read_bytes() == staged.read_bytes()
        run('systemctl', 'disable', sync_timer)
        if was_watchdog_active:
            run('systemctl', 'start', watchdog_timer)
        print('DEPLOYED: active=airport_a, old sync timer disabled, config sha256=' + hashlib.sha256(staged.read_bytes()).hexdigest(), flush=True)
    except BaseException as error:
        print('CUTOVER FAILED:', type(error).__name__, flush=True)
        if installed:
            atomic_install(backup, config_path, 'root', 'mihomo', original_mode)
            run('systemctl', 'restart', 'mihomo.service', timeout=35)
            deadline = time.monotonic() + 15
            while True:
                try:
                    select(original, {key: value for key, value in old_selections.items() if key not in regions})
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.3)
        (data_dir / cache_name).unlink(missing_ok=True)
        if was_sync_enabled:
            run('systemctl', 'enable', sync_timer)
        if was_sync_active:
            run('systemctl', 'start', sync_timer)
        if was_watchdog_active:
            run('systemctl', 'start', watchdog_timer)
        print('ROLLBACK: Mihomo active=' + str(active('mihomo.service')), flush=True)
        raise


if __name__ == '__main__':
    try:
        with open('/run/lock/mihomo-active-deploy.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            main()
    except Exception as error:
        import traceback
        locations = [f'{frame.name}:{frame.lineno}' for frame in traceback.extract_tb(error.__traceback__)]
        print(f'Deploy stopped: {type(error).__name__} at {", ".join(locations)}', file=sys.stderr, flush=True)
        sys.exit(1)
