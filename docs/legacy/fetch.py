import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener
import yaml

os.umask(0o077)
work = Path(__file__).parent
subscriptions = json.loads(Path('/opt/sub-store/data/sub-store.json').read_text())['subs']

def fetch(sub):
    name = sub['name']
    if name not in ('airport_a', 'airport_b'):
        return
    for proxy in ({}, {'https': 'http://127.0.0.1:7890', 'http': 'http://127.0.0.1:7890'}):
        try:
            opener = build_opener(ProxyHandler(proxy))
            request = Request(sub['url'], headers={'User-Agent': 'mihomo/1.19.33'})
            with opener.open(request, timeout=12) as response:
                data = response.read(8_000_001)
            if len(data) > 8_000_000:
                raise ValueError('subscription too large')
            config = yaml.safe_load(data)
            if not isinstance(config, dict) or not config.get('proxies'):
                raise ValueError('no inline nodes')
            (work / f'{name}.yaml').write_bytes(data)
            print(name, 'via proxy' if proxy else 'direct', len(config['proxies']), 'nodes', len(config.get('rules', [])), 'rules', [(k, v.get('type'), v.get('behavior'), v.get('format')) for k, v in config.get('rule-providers', {}).items()])
            return
        except Exception as error:
            print(name, 'via proxy' if proxy else 'direct', type(error).__name__, getattr(error, 'code', ''))

with ThreadPoolExecutor(max_workers=2) as executor:
    list(executor.map(fetch, subscriptions))
