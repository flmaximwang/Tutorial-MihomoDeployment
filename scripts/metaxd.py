#!/usr/bin/env python3
"""MetaCubeXD All-in-one 公共库。

所有自动化脚本只通过这个库访问控制面（/api/control）、内核面（RESTful API）
和容器内的 mihomo 校验能力，避免每个脚本各自拼一套请求与解析。

设计约束（见 docs/architecture.md）：
1. 覆写（Merge）只由仓库里的 shared-rules.yaml 决定，脚本只做收敛，不做增量补丁；
2. 任何写操作前必须先用容器内的 mihomo -t 校验完整候选配置；
3. 写操作失败必须回滚到写入前的 Merge 并重新激活，不允许留下半套配置。
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import yaml

ROOT = Path(os.environ.get('METAXD_ROOT', '/opt/metacubexd-prod'))
DATA = ROOT / 'data'
CONTAINER = os.environ.get('METAXD_CONTAINER', 'metacubexd-prod')
MERGE_NAME = os.environ.get('METAXD_MERGE_NAME', '自管规则与地区策略')

UI_PORT = int(os.environ.get('METAXD_UI_PORT', '8788'))
API_PORT = int(os.environ.get('METAXD_API_PORT', '9090'))
PROXY_PORT = int(os.environ.get('METAXD_PROXY_PORT', '7890'))
DNS_PORT = int(os.environ.get('METAXD_DNS_PORT', '1053'))

# Merge 只负责这三段，其余运行时设置留在 merge-base.yaml，节点与选择留在各机器。
MERGE_KEYS = ('rules', 'rule-providers', 'proxy-groups')
REGION_FILTER_HINT = 'use'          # 具备 use 的组由 provider 供节点，是本库要核验的地区组

OPENER = build_opener(ProxyHandler({}))


class MihomoLoader(yaml.SafeLoader):
    """mihomo 按 YAML 1.2 读配置；off/on 必须保持字符串。"""

    yaml_implicit_resolvers = {
        key: [(tag, pattern) for tag, pattern in resolvers
              if tag != 'tag:yaml.org,2002:bool' or key in 'tTfF']
        for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


def load_yaml(text: str):
    return yaml.load(text, Loader=MihomoLoader)


def load_file(path):
    return load_yaml(Path(path).read_text())


def dump_yaml(document) -> str:
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=4096)


def runtime_env() -> dict:
    path = ROOT / 'runtime.env'
    if not path.exists():
        raise RuntimeError(f'{path} 不存在，先运行 metaxdctl.py install')
    return dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)


def mihomo_bin() -> str:
    return runtime_env().get('MIHOMO_BIN', '/usr/local/bin/mihomo')


def request(port, path, token=None, method='GET', body=None, timeout=65):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = Request(
        f'http://127.0.0.1:{port}{path}',
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with OPENER.open(req, timeout=timeout) as response:
            payload = response.read()
            if 'application/json' in response.headers.get('Content-Type', '') and payload:
                return json.loads(payload)
            return None
    except HTTPError as error:
        raise RuntimeError(f'{method} {path} 返回 HTTP {error.code}') from None


def control(path, method='GET', body=None):
    return request(UI_PORT, '/api/control' + path, runtime_env()['CONTROL_TOKEN'], method, body)


def clash(path, method='GET', body=None):
    return request(API_PORT, path, runtime_env()['CLASH_SECRET'], method, body)


def profiles() -> list:
    return control('/profiles')


def merge_profile() -> dict:
    found = [p for p in profiles() if p['type'] == 'merge' and p['name'] == MERGE_NAME]
    if len(found) != 1:
        raise RuntimeError(f'找不到唯一的 Merge「{MERGE_NAME}」')
    return found[0]


def read_merge() -> str:
    return control('/profiles/' + merge_profile()['id'])['content']


def write_merge(document) -> None:
    profile = merge_profile()
    control('/profiles/' + profile['id'], 'PUT', {'content': dump_yaml(document)})


def airport_profile(name: str) -> dict:
    found = [p for p in profiles() if p['type'] == 'local' and p['name'] == name]
    if len(found) != 1:
        raise RuntimeError(f'找不到唯一的基础配置「{name}」')
    return found[0]


def active_profile() -> dict:
    found = [p for p in profiles() if p.get('active')]
    if len(found) != 1:
        raise RuntimeError('当前没有唯一处于激活状态的基础配置')
    return found[0]


def activate(profile_id: str) -> None:
    control(f'/profiles/{profile_id}/activate', 'POST', {})


def running_config() -> dict:
    return load_file(DATA / 'active.yaml')


def wait_for(action, description, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = action()
            if result:
                return result
        except (OSError, RuntimeError, KeyError, TypeError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(description)
        time.sleep(1)


def provider_nodes(provider='active') -> list:
    return wait_for(
        lambda: clash('/providers/proxies')['providers'].get(provider, {}).get('proxies'),
        f'机场 provider「{provider}」没有可用节点',
    )


def validate(candidate: dict) -> None:
    """用真实内核校验完整候选配置。失败只记录诊断，不影响运行中的实例。

    容器已运行时在容器内校验（与线上环境完全一致）；首次装机尚未启动容器时，
    直接用宿主机上的同一个二进制校验。
    """
    with tempfile.NamedTemporaryFile(
        mode='w', dir=DATA, prefix='.candidate-', suffix='.yaml', delete=False,
        encoding='utf-8',
    ) as handle:
        handle.write(dump_yaml(candidate))
        name = Path(handle.name).name
    try:
        if container_running():
            result = subprocess.run(
                ['docker', 'exec', CONTAINER, mihomo_bin(), '-t', '-d', '/data', '-f', '/data/' + name],
                capture_output=True, text=True, timeout=60,
            )
        else:
            result = subprocess.run(
                [str(DATA / 'mihomo'), '-t', '-d', str(DATA), '-f', str(DATA / name)],
                capture_output=True, text=True, timeout=60,
            )
    finally:
        (DATA / name).unlink(missing_ok=True)
    if result.returncode:
        diagnostic = (result.stdout + result.stderr).strip().splitlines()[-3:]
        (ROOT / 'validation-error.log').write_text('\n'.join(diagnostic) + '\n')
        raise RuntimeError('mihomo 拒绝了候选配置：\n' + '\n'.join(diagnostic))


def container_running() -> bool:
    status = subprocess.run(
        ['docker', 'inspect', '--format', '{{.State.Running}}', CONTAINER],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    return status == 'true'


def provider_groups(config: dict) -> list:
    return [group for group in config['proxy-groups'] if group.get('use')]


def verify(candidate: dict, traffic=True) -> dict:
    """结构 + 行为验收：规则、规则集、地区组、provider 节点，必要时探测真实出口。"""
    wait_for(lambda: clash('/version'), '内核 API 不可用')
    active = active_profile()
    actual = running_config()
    for key in MERGE_KEYS:
        if actual.get(key) != candidate.get(key):
            raise RuntimeError(f'{key} 与期望不一致')
    if set(actual.get('proxy-providers', {})) != {'active'}:
        raise RuntimeError('基础配置里应当只有一个名为 active 的 provider')

    nodes = {node['name'] for node in provider_nodes()}
    proxies = clash('/proxies')['proxies']
    summary = {'profile': active['name'], 'nodes': len(nodes), 'groups': len(candidate['proxy-groups'])}
    region_sizes = []
    for group in provider_groups(candidate):
        live = proxies.get(group['name'])
        if not live:
            raise RuntimeError(f'策略组「{group["name"]}」未加载')
        members = live.get('all') or []
        if not members or not set(members) <= nodes:
            raise RuntimeError(f'策略组「{group["name"]}」成员不是本机 provider 的节点')
        if group.get('type') == 'url-test' and live.get('type') != 'URLTest':
            raise RuntimeError(f'策略组「{group["name"]}」类型不是 URLTest')
        region_sizes.append(len(members))
    summary['provider_groups'] = region_sizes

    rules = clash('/rules')['rules']
    if len(rules) != len(candidate['rules']):
        raise RuntimeError(f'生效规则数 {len(rules)} 与期望 {len(candidate["rules"])} 不一致')
    providers = clash('/providers/rules')['providers']
    if set(providers) != set(candidate['rule-providers']):
        raise RuntimeError('生效规则集与期望不一致')
    summary['rules'] = len(rules)
    summary['rule_providers'] = len(providers)

    dns = []
    for mode in ('+notcp', '+tcp'):
        answer = subprocess.run(
            ['dig', '@127.0.0.1', '-p', str(DNS_PORT), 'discord.com', '+short', '+time=2', '+tries=2', mode],
            capture_output=True, text=True, timeout=15,
        ).stdout
        if not any(line.strip() for line in answer.splitlines()):
            raise RuntimeError(f'DNS {mode} 查询失败')
        dns.append(mode)
    summary['dns'] = dns

    if traffic:
        summary['traffic'] = {}
        # 只挑稳定返回固定码的端点：google 系根路径会随节点出口给 404/000，不具诊断价值。
        for url, expected in (
            ('https://discord.com/api/v10/gateway', {'200'}),
            ('https://github.com/', {'200'}),
            ('https://www.gstatic.com/generate_204', {'204'}),
        ):
            status = wait_for(
                lambda: _probe(url, expected), f'出口探测失败：{url}', timeout=20,
            )
            summary['traffic'][url] = status
    return summary


def _probe(url, expected):
    status = subprocess.run(
        ['curl', '-q', '-s', '--noproxy', '', '-x', f'http://127.0.0.1:{PROXY_PORT}',
         '--connect-timeout', '4', '--max-time', '10', '-o', '/dev/null', '-w', '%{http_code}', url],
        capture_output=True, text=True, timeout=20,
    ).stdout.strip()
    return status if status in expected else None


def snapshot() -> dict:
    """记录所有策略组的当前选择，供切换机场或重建配置后恢复。"""
    proxies = clash('/proxies')['proxies']
    return {
        name: entry['now']
        for name, entry in proxies.items()
        if entry.get('now') and entry.get('all')
    }


def restore(selection: dict) -> int:
    proxies = clash('/proxies')['proxies']
    restored = 0
    for name, target in selection.items():
        if target in proxies.get(name, {}).get('all', []):
            clash('/proxies/' + urllib.parse.quote(name, safe=''), 'PUT', {'name': target})
            restored += 1
    return restored


def select_group_names(config: dict) -> list:
    return [group['name'] for group in config['proxy-groups'] if group.get('type') == 'select']


def save_state(selection: dict, name='selections.json') -> None:
    (ROOT / name).write_text(json.dumps(selection, ensure_ascii=False, indent=2) + '\n')


def load_state(name='selections.json') -> dict:
    path = ROOT / name
    return json.loads(path.read_text()) if path.exists() else {}
