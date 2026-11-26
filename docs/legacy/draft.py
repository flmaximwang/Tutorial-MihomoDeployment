import csv
import os
import re
import secrets
import shutil
from collections import Counter, defaultdict
from pathlib import Path
import yaml

os.umask(0o077)
root = Path(__file__).parent
sources = {name: yaml.safe_load((root / f'{name}.yaml').read_text()) for name in ('airport_a', 'airport_b')}
base = yaml.safe_load(Path('/etc/mihomo-prod/config.yaml').read_text())

def collect(rules):
    by_match = defaultdict(list)
    for rule in rules:
        kind, *fields = rule.split(',')
        modifiers = []
        while fields and fields[-1] in {'no-resolve', 'src'}:
            modifiers.insert(0, fields.pop())
        policy = fields.pop()
        selector = ','.join(fields)
        if kind.startswith('DOMAIN'):
            selector = selector.lower()
        key = kind, selector, ','.join(modifiers)
        if policy not in by_match[key]:
            by_match[key].append(policy)
    return by_match

matches = {name: collect(config['rules']) for name, config in sources.items()}
union = set().union(*matches.values())

def route(policy):
    if policy in ('DIRECT', '🎯 全球直连'):
        return 'DIRECT'
    if policy in ('REJECT', 'REJECT-DROP'):
        return 'REJECT'
    return 'PROXY_GROUP'

conflicts = []
for key in sorted(union):
    labels = [matches[name].get(key, []) for name in sources]
    if all(labels) and set(map(route, labels[0])) != set(map(route, labels[1])):
        conflicts.append(key)

with (root / 'rule-candidates.csv').open('w', encoding='utf-8-sig', newline='') as file:
    writer = csv.writer(file)
    writer.writerow(['类型', '匹配条件', '参数', '机场甲策略', '机场乙策略', '直连／拒绝／代理组分歧'])
    for key in sorted(union):
        writer.writerow([*key, *(' / '.join(matches[name].get(key, [])) for name in sources), '是' if key in conflicts else ''])
print('unique', [(name, len(items)) for name, items in matches.items()], 'union', len(union), 'shared', len(set.intersection(*(set(x) for x in matches.values()))), 'differing policy classes', len(conflicts))
print('conflicts sample', [(key, *(matches[name][key] for name in sources)) for key in conflicts[:12]])
print('union types', dict(Counter(key[0] for key in union)))

runtime = root / 'runtime'
runtime.mkdir(exist_ok=True)
for data in Path('/opt/mihomo/geodata-pristine').iterdir():
    (runtime / data.name).symlink_to(data) if not (runtime / data.name).exists() else None
for name, provider in base.get('rule-providers', {}).items():
    path = Path(provider['path'])
    if not path.parts or path.parts[0] != 'ruleset' or len(path.parts) != 2:
        raise ValueError('Unexpected rule provider path')
    dest = runtime / path
    dest.parent.mkdir(exist_ok=True)
    source = Path('/opt/mihomo') / path
    if source.exists():
        shutil.copyfile(source, dest)
        provider['type'] = 'file'
        provider.pop('url', None)
    else:
        print('baseline provider without cached file:', name)
        # Leave its own original URL and download only into the isolated directory.

base['secret'] = secrets.token_hex(24)
base['external-controller'] = '127.0.0.1:19091'
base['mixed-port'] = 17891
base['allow-lan'] = False
base['bind-address'] = '127.0.0.1'
base['dns']['listen'] = '127.0.0.1:15391'
base['tun']['enable'] = False
base['geo-auto-update'] = False
base['profile'] = {'store-selected': True, 'store-fake-ip': False}
old_nodes = {node['name'] for node in base.pop('proxies')}
base['proxy-providers'] = {}
for name, source in sources.items():
    path = f'{name}-nodes.yaml'
    (runtime / path).write_text(yaml.safe_dump({'proxies': source['proxies']}, allow_unicode=True, sort_keys=False))
    base['proxy-providers'][name] = {'type': 'file', 'path': './' + path, 'override': {'additional-prefix': name + ' | '}}
    if os.environ.get('TRIAL_HTTP_PROVIDERS') == '1':
        import json
        subscriptions = json.loads(Path('/opt/sub-store/data/sub-store.json').read_text())['subs']
        url = next(item['url'] for item in subscriptions if item['name'] == name)
        base['proxy-providers'][name].update(type='http', url=url, path=f'./http-{name}-nodes.yaml', interval=21600, header={'User-Agent': ['mihomo/1.19.33']})

for group in base['proxy-groups']:
    original = group.get('proxies', [])
    group['proxies'] = [item for item in original if item not in old_nodes]
    if group['name'] == '选择代理':
        group['proxies'] = ['当前机场', *group['proxies']]
    for region, pattern in {'香港':'🇭🇰|香港|Hong Kong|\\|HK\\|','台湾':'🇹🇼|台湾|台灣|\\|TW\\|','新加坡':'🇸🇬|新加坡|\\|SG\\|','日本':'🇯🇵|日本|\\|JP\\|','美国':'🇺🇸|美国|美國|\\|US\\|'}.items():
        if group['name'] == region + '节点':
            group['use'] = list(sources)
            group['filter'] = pattern
    if group['name'] in ('手动选择', '自动选择', '故障转移'):
        group['use'] = list(sources)
    if not group['proxies']:
        group.pop('proxies')
base['proxy-groups'].extend([
    {'name': '当前机场', 'type': 'select', 'proxies': list(sources)},
    *({'name': name, 'type': 'select', 'use': [name]} for name in sources),
])
# Keep local high-priority rules and put the new explicit Discord policies ahead of community providers.
discord = [f'DOMAIN-SUFFIX,{domain},当前机场' for domain in ('discord.com', 'discord.gg', 'discordapp.com', 'discordapp.net', 'discord.co', 'discord.media')]
index = next(i for i, rule in enumerate(base['rules']) if rule.startswith('RULE-SET,'))
base['rules'][index:index] = discord
(runtime / 'config.yaml').write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False))
(root / 'discord-draft.yaml').write_text(yaml.safe_dump({'rules': discord}, allow_unicode=True, sort_keys=False))
