import csv
import os
from collections import Counter
from pathlib import Path

import yaml

os.umask(0o077)
root = Path(__file__).parent
production = yaml.safe_load(Path('/etc/mihomo-prod/config.yaml').read_text())
local_rules = production['rules']
groups = {group['name'] for group in production['proxy-groups']}

policy_map = {
    'DIRECT': 'DIRECT', '🎯 全球直连': 'DIRECT', 'REJECT': '广告拦截',
    '机场甲.com': '选择代理', '🔥ChatGPT': 'AI服务',
    '🚀 节点选择': '选择代理', '🌍 国外媒体': '选择代理', '🤖 AI': 'AI服务',
    '🎶 网易云音乐': 'DIRECT', '📢 谷歌FCM': '谷歌服务', '🎥 Netflix奈飞': 'Netflix',
    '🍎 苹果服务': '苹果服务', '🎮 游戏平台': '选择代理', '🌏 国内媒体': 'DIRECT',
    '📺 哔哩哔哩': '哔哩哔哩', '📲 Telegram电报': 'Telegram', '⛓️ 微软云盘': '微软服务',
    '📱 Twitter推特': 'Twitter', '📹 Youtube油管': 'Youtube', '📺 巴哈姆特': '巴哈姆特',
    '📧 邮件客户端': '选择代理', '🛩 特殊网站': '选择代理', '📀 EMBY影视': '选择代理',
}
# Existing service groups own service-specific exceptions. Preserve the incumbent
# direct recommendation for the other shared direct/proxy matches.
conflict_policy = {
    **dict.fromkeys(('aaplimg.com', 'akadns.net', 'apple-cloudkit.com', 'apple-mapkit.com',
                     'apple.com', 'cdn-apple.com', 'icloud-content.com', 'icloud.com',
                     'me.com', 'mzstatic.com'), '苹果服务'),
    **dict.fromkeys(('bilibili.com', 'bilivideo.com'), '哔哩哔哩'),
    **dict.fromkeys(('steamcdn-a.akamaihd.net', 'dyndns.org', 'dytt8.net', 'epicgames.com',
                     'steamserver.net', 'unrealengine.com', 'youku.com', 'zoho.com'), 'DIRECT'),
    'mmstat.com': '广告拦截',
}
promotional = {'airport-a.invalid', 'airport-a2.invalid', 'airport-b.invalid', 'airport-b2.invalid',
               'airport-b3.invalid', 'airport-b4.invalid'}
priority = [f'DOMAIN-SUFFIX,{domain},选择代理' for domain in
            ('discord.com', 'discord.gg', 'discordapp.com', 'discordapp.net', 'discord.co', 'discord.media')]
local_matches = {tuple(rule.split(',')[:2]) for rule in local_rules + priority}
counts = Counter()
added = []
with (root / 'rule-candidates.csv').open(encoding='utf-8-sig') as file:
    rows = csv.reader(file)
    next(rows)
    for kind, domain, args, airport_a, airport_b, conflict in rows:
        if kind not in {'DOMAIN', 'DOMAIN-SUFFIX'}:
            counts['excluded_kind'] += 1
            continue
        assert not args
        if (kind, domain) in local_matches:
            counts['duplicate_local'] += 1
            continue
        source_policies = [item for column in (airport_a, airport_b) for item in column.split(' / ') if item]
        if '🌐 访问官网' in source_policies or any(domain == name or domain.endswith('.' + name) for name in promotional):
            counts['promotional'] += 1
            continue
        # Avoid importing country-wide proxy defaults and a conflicting shared CDN suffix.
        if ('.' not in domain and domain not in {'cn', 'lan', 'local', 'localhost'}) or domain == 'edgekey.net':
            counts['broad_or_ambiguous'] += 1
            continue
        mapped = list(dict.fromkeys(policy_map[item] for item in source_policies))
        if conflict:
            target = conflict_policy[domain]
            counts['resolved_conflicts'] += 1
        else:
            target = next((item for item in mapped if item not in {'选择代理', 'DIRECT'}), mapped[0])
        assert target in groups or target == 'DIRECT'
        added.append((kind, domain, target))

# An exact hostname beats suffix matches; longer suffixes beat their parents.
added.sort(key=lambda item: (item[0] != 'DOMAIN', -item[1].count('.'), item[1]))
assert counts['resolved_conflicts'] == len(conflict_policy)
assert local_rules[-2:] == ['GEOIP,cn,DIRECT', 'MATCH,Final']
first_provider = next(i for i, rule in enumerate(local_rules) if rule.startswith('RULE-SET,'))
rules = (local_rules[:first_provider] + priority + local_rules[first_provider:-2]
         + [','.join(item) for item in added] + local_rules[-2:])
assert len(rules) == len(set(rules))
assert all(rule in rules for rule in local_rules)
rules_path = root / 'mihomo-managed-rules.yaml'
rules_path.write_text(
    '# Replace the top-level rules and rule-providers sections in the Mihomo configuration.\n'
    '# Existing proxy-group names and provider paths are required.\n'
    + yaml.safe_dump({'rules': rules, 'rule-providers': production['rule-providers']},
                     allow_unicode=True, sort_keys=False), encoding='utf-8')
assert yaml.safe_load(rules_path.read_text())['rules'] == rules
runtime = root / 'runtime'
config = yaml.safe_load((runtime / 'config.yaml').read_text())
config['rules'] = rules
(runtime / 'managed-config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
# Check that the rules also load with the incumbent groups, without the trial's selectors.
incumbent = dict(config)
incumbent.pop('proxy-providers')
incumbent['proxies'] = production['proxies']
incumbent['proxy-groups'] = production['proxy-groups']
(runtime / 'managed-incumbent.yaml').write_text(yaml.safe_dump(incumbent, allow_unicode=True, sort_keys=False))
print(f'{len(rules)} total, {len(added)} extra domain rules, {len(priority)} Discord rules, {len(production["rule-providers"])} providers')
print(dict(counts))
print('supplemental targets:', dict(Counter(target for _, _, target in added)))
