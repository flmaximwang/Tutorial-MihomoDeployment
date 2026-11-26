#!/usr/bin/env python3
"""MetaCubeXD All-in-one 部署与同步 CLI。

    install   首次装机：建目录、生成运行时密钥、导入基础配置与 Merge、启动容器
    sync      从 GitHub 拉取覆写并收敛（幂等；每小时由 systemd 触发）
    status    只读体检：容器、激活配置、规则/规则组/地区组/出口
    rollback  把 Merge 恢复到上一次成功同步前的快照并重新激活

所有子命令都只做“收敛到期望状态”，不做增量补丁：期望状态 = merge-base.yaml
加上 GitHub 仓库里的 shared-rules.yaml，二者都由仓库唯一决定。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import metaxd

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / 'config'
UNITS_DIR = REPO_ROOT / 'systemd'

IMAGE = os.environ.get(
    'METAXD_IMAGE',
    'ghcr.io/metacubex/metacubexd-server@sha256:'
    'b34d29f7bc50d0aeb4d211e276467cdcd5459b8c1ef7bbf217acdbce1bbd7bfb',
)
GH = os.environ.get('METAXD_GH', 'gh')

# api.github.com 直连偶发 TLS 阻断，代理走 HTTP/1.1 更稳；两种方式交替重试。
GITHUB_PROXIES = (os.environ.get('METAXD_GITHUB_PROXY', 'http://127.0.0.1:7890'), '')
MAX_ATTEMPTS = 5


def log(message: str) -> None:
    print(message, flush=True)


# --------------------------------------------------------------------------- github

def github_token() -> str:
    """优先环境变量；否则借用 gh 的登录态。公开仓库两者都可以为空。"""
    for key in ('GH_TOKEN', 'GITHUB_TOKEN'):
        if os.environ.get(key):
            return os.environ[key]
    candidates = [shutil.which(GH), '/usr/bin/gh', '/usr/local/bin/gh', '/home/ubuntu/.local/bin/gh']
    gh = next((path for path in candidates if path and Path(path).exists()), None)
    if not gh:
        return ''
    commands = [[gh, 'auth', 'token']]
    if os.geteuid() == 0:
        commands.insert(0, ['runuser', '-u', 'ubuntu', '--', 'env', 'HOME=/home/ubuntu', gh, 'auth', 'token'])
    for command in commands:
        try:
            token = subprocess.run(command, capture_output=True, text=True, timeout=10,
                                   check=False).stdout.strip()
            if token:
                return token
        except (OSError, subprocess.TimeoutExpired):
            continue
    return ''


def github_get(url: str, raw=False) -> str:
    """同一个请求在“走代理 / 走直连”之间交替，最多 5 次。"""
    token = github_token()
    last = 'unknown error'
    for attempt in range(MAX_ATTEMPTS):
        proxy = GITHUB_PROXIES[attempt % len(GITHUB_PROXIES)]
        opener = build_opener(ProxyHandler({'https': proxy} if proxy else {}))
        headers = {'Accept': 'application/vnd.github.raw+json' if raw else 'application/vnd.github+json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        try:
            with opener.open(Request(url, headers=headers), timeout=25) as response:
                return response.read().decode()
        except (HTTPError, URLError, OSError) as error:
            last = f'{type(error).__name__}: {error}'
    raise RuntimeError(f'拉取 GitHub 失败（{MAX_ATTEMPTS} 次）：{last}')


SHARED_PATHS = ('config/shared-rules.yaml', 'shared-rules.yaml')


def fetch_shared(repo: str) -> tuple[dict, str]:
    """先取 commit SHA，再按 SHA 读文件，避免读到新旧混杂的内容。

    路径兼容两种仓库布局：本仓库把覆写放在 config/ 下，独立规则仓库放根目录。
    """
    commit = json.loads(github_get(f'https://api.github.com/repos/{repo}/commits/main'))['sha']
    last = 'not found'
    for path in SHARED_PATHS:
        try:
            raw = github_get(f'https://api.github.com/repos/{repo}/contents/{path}?ref={commit}', raw=True)
        except RuntimeError as error:
            last = str(error)
            continue
        shared = metaxd.load_yaml(raw)
        check_shared(shared)
        return shared, commit
    raise RuntimeError(f'{repo} 在 {commit} 上找不到 shared-rules.yaml（试过 {", ".join(SHARED_PATHS)}）：{last}')


def rule_target(rule: str) -> str:
    """规则末段是策略组，但 GEOIP/IP-CIDR 类规则尾部可能还有 no-resolve 等选项。"""
    fields = [field.strip() for field in rule.split(',')]
    for field in reversed(fields[1:]):
        if field in ('no-resolve', 'src'):
            continue
        return field
    return fields[-1]


def check_shared(shared) -> None:
    if not isinstance(shared, dict) or set(shared) != set(metaxd.MERGE_KEYS):
        raise RuntimeError(f'shared-rules.yaml 只能包含 {", ".join(metaxd.MERGE_KEYS)}')
    rules, providers, groups = shared['rules'], shared['rule-providers'], shared['proxy-groups']
    if not (isinstance(rules, list) and rules and all(isinstance(r, str) for r in rules)):
        raise RuntimeError('rules 必须是非空字符串列表')
    if not rules[-1].startswith('MATCH,') or any(r.startswith('MATCH,') for r in rules[:-1]):
        raise RuntimeError('rules 必须以唯一的 MATCH 收尾')
    if not isinstance(providers, dict) or not providers:
        raise RuntimeError('rule-providers 必须是非空映射')
    if not (isinstance(groups, list) and groups and all(isinstance(g, dict) and 'name' in g for g in groups)):
        raise RuntimeError('proxy-groups 必须是带 name 的映射列表')
    named = {group['name'] for group in groups}
    providers_used = set()
    for group in groups:
        for target in group.get('proxies') or []:
            if target in ('DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'COMPATIBLE') or target in named:
                continue
            raise RuntimeError(f'策略组「{group["name"]}」引用了未定义的组「{target}」')
        # `use` 指向基础配置里的 provider：不写 filter = 取该 provider 全部节点，是合法写法。
        if group.get('use') is not None:
            if not (isinstance(group['use'], list) and group['use'] and all(isinstance(u, str) for u in group['use'])):
                raise RuntimeError(f'策略组「{group["name"]}」的 use 必须是非空字符串列表')
            providers_used.update(group['use'])
        if group.get('filter') is not None:
            try:
                re.compile(group['filter'])
            except re.error as error:
                raise RuntimeError(f'策略组「{group["name"]}」的 filter 不是合法正则：{error}') from None
        if group.get('type') == 'url-test' and not group.get('url'):
            raise RuntimeError(f'测速组「{group["name"]}」缺少 url')
        if group.get('type') not in ('select', 'url-test', 'fallback', 'load-balance', 'relay'):
            raise RuntimeError(f'策略组「{group["name"]}」类型「{group.get("type")}」不受支持')
    if providers_used != {'active'}:
        raise RuntimeError(f'use 只能引用本机的 active provider，实际为 {sorted(providers_used)}')
    for rule in rules:
        target = rule_target(rule)
        if target in ('DIRECT', 'REJECT', 'REJECT-DROP', 'PASS') or target == 'GLOBAL':
            continue
        if target not in named:
            raise RuntimeError(f'规则指向未定义的策略组「{target}」：{rule}')


# --------------------------------------------------------------------------- install

def write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), 'w') as handle:
        handle.write(content)


def install(args) -> None:
    if (metaxd.ROOT / 'runtime.env').exists() and not args.force:
        raise SystemExit(f'{metaxd.ROOT} 已存在；要重装请加 --force')
    metaxd.DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(metaxd.ROOT, 0o700)
    os.chmod(metaxd.DATA, 0o700)

    source = Path(args.mihomo_bin)
    if not source.exists():
        raise SystemExit(f'找不到宿主机 mihomo 二进制：{source}')
    shutil.copy2(source, metaxd.DATA / 'mihomo')
    for name in ('Country.mmdb', 'geoip.dat', 'geosite.dat', 'asn.mmdb'):
        candidate = source.parent / name
        if candidate.exists():
            shutil.copy2(candidate, metaxd.DATA / name)

    if not args.secret:
        raise SystemExit('必须提供 --secret（原 mihomo 配置里的 secret，或新生成的随机串）')
    runtime = [
        f'CONTROL_TOKEN={secrets.token_urlsafe(36)}',
        f'CLASH_SECRET={args.secret}',
        f'MIHOMO_BIN={args.mihomo_path}',
        f'TZ={args.timezone}',
    ]
    write(metaxd.ROOT / 'runtime.env', '\n'.join(runtime) + '\n')

    # 装机时优先用 GitHub 上的最新覆写；拉不到再用仓库里带的快照，保证离线也能装。
    try:
        shared, commit = fetch_shared(args.repo)
        log(f'已取 GitHub {commit} 的覆写')
    except RuntimeError as error:
        shared = metaxd.load_file(CONFIG_DIR / 'shared-rules.yaml')
        commit = 'local-snapshot'
        log(f'拉取 GitHub 失败（{error}），改用仓库内快照')
    merge = metaxd.load_file(CONFIG_DIR / 'merge-base.yaml') | shared
    metaxd.validate({**merge, **_airport_stub(args)})
    log('候选 Merge 通过 mihomo -t 校验')

    install_files()
    start_container()
    wait_for_profiles()
    save_profile(metaxd.MERGE_NAME, 'merge', merge)
    merge_id = metaxd.merge_profile()['id']

    provider = {
        'type': 'http',
        'url': args.airport_url,
        'path': './airport.yaml',
        'interval': int(args.airport_interval),
        'proxy': 'DIRECT',
        'header': {'User-Agent': ['mihomo/1.19.33']},
        'health-check': {'enable': True, 'url': 'https://www.gstatic.com/generate_204', 'interval': 60},
    }
    save_profile(args.airport_name, 'local', {'proxy-providers': {'active': provider}})
    metaxd.activate(metaxd.airport_profile(args.airport_name)['id'])

    metaxd.provider_nodes()
    summary = metaxd.verify(merge, traffic=not args.skip_traffic)
    metaxd.save_state(metaxd.snapshot())
    write_state_file(args.airport_name, merge_id, args.repo)

    if not args.skip_units:
        install_units()
    log(json.dumps(summary, ensure_ascii=False, indent=2))
    log('装机完成；systemd 定时器已启用（每小时同步 GitHub 覆写）')


def install_files() -> None:
    """把脚本与静态配置拷进部署目录，systemd 单元因此不依赖仓库路径。"""
    for source, target in (
        (REPO_ROOT / 'scripts' / 'metaxd.py', metaxd.ROOT / 'scripts' / 'metaxd.py'),
        (REPO_ROOT / 'scripts' / 'metaxdctl.py', metaxd.ROOT / 'scripts' / 'metaxdctl.py'),
        (CONFIG_DIR / 'merge-base.yaml', metaxd.ROOT / 'config' / 'merge-base.yaml'),
        (CONFIG_DIR / 'shared-rules.yaml', metaxd.ROOT / 'config' / 'shared-rules.yaml'),
    ):
        write(target, source.read_text(), 0o755 if source.suffix == '.py' else 0o644)


def _airport_stub(args) -> dict:
    """校验时需要占位 provider，真实节点在容器启动后才拉到。"""
    return {
        'proxy-providers': {'active': {'type': 'http', 'url': args.airport_url, 'path': './airport.yaml'}},
    }


def start_container() -> None:
    if shutil.which('docker') is None:
        raise SystemExit('缺少 docker')
    subprocess.run(['docker', 'rm', '-f', metaxd.CONTAINER], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([
        'docker', 'run', '-d', '--name', metaxd.CONTAINER, '--restart', 'unless-stopped',
        '--env-file', str(metaxd.ROOT / 'runtime.env'),
        '-e', f'DEFAULT_BACKEND_URL=http://127.0.0.1:{metaxd.API_PORT}',
        '--mount', f'type=bind,src={metaxd.DATA},dst=/data',
        '-p', f'127.0.0.1:{metaxd.UI_PORT}:8080',
        '-p', f'127.0.0.1:{metaxd.API_PORT}:9090',
        '-p', f'127.0.0.1:{metaxd.PROXY_PORT}:7890',
        '-p', f'127.0.0.1:{metaxd.PROXY_PORT}:7890/udp',
        '-p', f'127.0.0.1:{metaxd.DNS_PORT}:1053',
        '-p', f'127.0.0.1:{metaxd.DNS_PORT}:1053/udp',
        IMAGE,
    ], check=True, stdout=subprocess.DEVNULL)


def wait_for_profiles() -> None:
    # 新装实例的配置列表就是空数组，不能用真值判断就绪，只能看接口是否 200。
    metaxd.wait_for(lambda: metaxd.control('/profiles') is not None, 'All-in-one 控制面未就绪', timeout=90)


def save_profile(name: str, kind: str, content: dict) -> None:
    existing = [p for p in metaxd.profiles() if p['name'] == name and p['type'] == kind]
    if existing:
        metaxd.control(f'/profiles/{existing[0]["id"]}', 'PUT', {'content': metaxd.dump_yaml(content)})
    else:
        metaxd.control('/profiles', 'POST', {'name': name, 'type': kind, 'content': metaxd.dump_yaml(content)})


def write_state_file(airport: str, merge_id: str, repo: str) -> None:
    path = metaxd.ROOT / 'deployment.json'
    state = json.loads(path.read_text()) if path.exists() else {}
    state.update({'airport': airport, 'merge_id': merge_id, 'repo': repo})
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')


UNIT_DIR = Path(os.environ.get('METAXD_UNIT_DIR', '/etc/systemd/system'))


def install_units() -> None:
    for unit in ('metacubexd-rules-sync.service', 'metacubexd-rules-sync.timer'):
        text = (UNITS_DIR / unit).read_text().replace('{{ROOT}}', str(metaxd.ROOT))
        write(UNIT_DIR / unit, text, 0o644)
    if UNIT_DIR != Path('/etc/systemd/system'):
        log(f'已写入 {UNIT_DIR}（非默认目录，跳过 systemctl）')
        return
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'metacubexd-rules-sync.timer'], check=True)
    log('已安装并启用 metacubexd-rules-sync.timer')


# --------------------------------------------------------------------------- sync

def desired_merge(shared: dict) -> dict:
    return metaxd.load_file(CONFIG_DIR / 'merge-base.yaml') | shared


def sync(args) -> None:
    deployment = metaxd.load_state('deployment.json')
    repo = args.repo or deployment.get('repo')
    if not repo:
        raise SystemExit('缺少 --repo owner/name（或 install 时用 --repo 写进 deployment.json）')
    shared, commit = fetch_shared(repo)
    current = metaxd.load_yaml(metaxd.read_merge())
    running = metaxd.running_config()

    if all(current.get(key) == shared[key] and running.get(key) == shared[key]
           for key in metaxd.MERGE_KEYS):
        metaxd.verify(shared, traffic=False)
        log(f'GitHub {commit}：覆写与生产一致，无需写入')
        return

    candidate = running | desired_merge(shared)
    metaxd.validate(candidate)

    active = metaxd.active_profile()
    snapshot_path = metaxd.ROOT / 'merge-before-last-sync.yaml'
    write(snapshot_path, metaxd.read_merge())
    selection = metaxd.snapshot()

    try:
        metaxd.write_merge(desired_merge(shared))
        if metaxd.active_profile()['id'] != active['id']:
            raise RuntimeError('写入期间激活配置发生变化')
        metaxd.activate(active['id'])
        applied = metaxd.running_config()
        for key in metaxd.MERGE_KEYS:
            if applied.get(key) != shared[key]:
                raise RuntimeError(f'重新激活后 {key} 与 GitHub 不一致')
        metaxd.provider_nodes()
        summary = metaxd.verify(shared, traffic=not args.skip_traffic)
        metaxd.restore(selection)
    except Exception:
        _rollback(snapshot_path, running)
        raise

    metaxd.save_state(metaxd.snapshot())
    log(json.dumps(summary | {'github': commit}, ensure_ascii=False, indent=2))


def _rollback(snapshot_path: Path, running: dict) -> None:
    if not snapshot_path.exists():
        return
    log('同步失败，回滚到上一份 Merge')
    metaxd.write_merge(metaxd.load_yaml(snapshot_path.read_text()))
    metaxd.activate(metaxd.active_profile()['id'])
    metaxd.verify(running, traffic=False)


def rollback(args) -> None:
    snapshot = metaxd.ROOT / 'merge-before-last-sync.yaml'
    if not snapshot.exists():
        raise SystemExit('没有可回滚的快照')
    metaxd.write_merge(metaxd.load_yaml(snapshot.read_text()))
    metaxd.activate(metaxd.active_profile()['id'])
    log(json.dumps(metaxd.verify(metaxd.running_config(), traffic=not args.skip_traffic),
                   ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- status

def status(args) -> None:
    container = subprocess.run(
        ['docker', 'inspect', '--format', '{{.State.Status}} {{.State.Health.Status}}', metaxd.CONTAINER],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    running = metaxd.running_config()
    merge = metaxd.load_yaml(metaxd.read_merge())
    drift = [key for key in metaxd.MERGE_KEYS if merge.get(key) != running.get(key)]
    report = {
        'container': container,
        'active_profile': metaxd.active_profile()['name'],
        'merge': metaxd.MERGE_NAME,
        'merge_drift': drift or None,
        'providers': sorted(running.get('proxy-providers', {})),
        'nodes': len(metaxd.provider_nodes()),
    }
    if drift:
        report['warning'] = 'Merge 与运行配置不一致，说明有未收敛的改动'
    log(json.dumps(report, ensure_ascii=False, indent=2))
    if args.verify:
        log(json.dumps(metaxd.verify(running, traffic=True), ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description='MetaCubeXD All-in-one 部署与同步')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('install', help='首次装机')
    p.add_argument('--secret', required=True, help='内核 secret（CLASH_SECRET）')
    p.add_argument('--airport-name', required=True, help='基础配置显示名，如 狗狗加速')
    p.add_argument('--airport-url', required=True, help='机场订阅 URL')
    p.add_argument('--airport-interval', default='21600', help='订阅刷新秒数，默认 6 小时')
    p.add_argument('--mihomo-bin', default='/opt/mihomo/mihomo', help='宿主机 mihomo 二进制')
    p.add_argument('--mihomo-path', default='/data/mihomo', help='容器内二进制路径')
    p.add_argument('--timezone', default='Asia/Shanghai')
    p.add_argument('--repo', required=True, help='GitHub 仓库 owner/name（同步源）')
    p.add_argument('--skip-traffic', action='store_true', help='跳过出口探测')
    p.add_argument('--skip-units', action='store_true', help='不安装 systemd 单元（测试用）')
    p.add_argument('--force', action='store_true')
    p.set_defaults(func=install)

    p = sub.add_parser('sync', help='从 GitHub 拉取覆写并收敛')
    p.add_argument('--repo', help='GitHub 仓库 owner/name，默认取 deployment.json')
    p.add_argument('--skip-traffic', action='store_true')
    p.set_defaults(func=sync)

    p = sub.add_parser('status', help='只读体检')
    p.add_argument('--verify', action='store_true', help='同时做行为验收')
    p.set_defaults(func=status)

    p = sub.add_parser('rollback', help='回滚到上次同步前')
    p.add_argument('--skip-traffic', action='store_true')
    p.set_defaults(func=rollback)

    args = parser.parse_args()
    try:
        args.func(args)
    except RuntimeError as error:
        log(f'失败：{error}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
