# Tutorial: 在 Ubuntu 上部署一套可维护的 Mihomo

一台 Ubuntu 服务器，跑一个 MetaCubeXD All-in-one 容器，做成「谁改什么、怎么验证、怎么回滚」都写死的系统。

这套部署解决的是一个具体的分工问题：

- **机场只提供节点。** 订阅链接只写进 `proxy-providers`，机场给的规则、策略组、DNS 全部不用。
- **规则与策略组由你自己管。** 它们放在一个 GitHub 仓库里，机器每小时的 systemd 定时器去拉，一致性由脚本保证，不靠人手动贴。
- **每台机器的选择留在本机。** 「现在用哪个节点」这类状态存在容器里，同步规则不会重置它。

## 结构

```
一台机器
├── 容器 metacubexd-prod            ghcr.io/metacubex/metacubexd-server（digest 固定）
│   ├── 基础配置（一个机场）          proxy-providers.active → 机场订阅
│   └── Merge「自管规则与地区策略」     rules / rule-providers / proxy-groups     ← GitHub
└── /opt/metacubexd-prod
    ├── runtime.env                 CONTROL_TOKEN / CLASH_SECRET / MIHOMO_BIN / TZ（0600）
    ├── deployment.json             airport / merge_id / repo
    ├── config/merge-base.yaml      运行时设置（DNS、sniffer、TUN、CORS…）
    ├── config/shared-rules.yaml    覆写快照（GitHub 拉不到时的兜底）
    ├── scripts/metaxd.py           公共库：控制面、内核面、校验
    ├── scripts/metaxdctl.py        install / sync / status / rollback
    └── data/                       容器挂载点：二进制、geo 数据、profiles、active.yaml
```

端口只监听回环：

| 端口 | 用途 |
| --- | --- |
| `127.0.0.1:8788` | MetaCubeXD 面板（control API） |
| `127.0.0.1:9090` | Mihomo 内核 RESTful API |
| `127.0.0.1:7890` | mixed 代理（TCP + UDP） |
| `127.0.0.1:1053` | DNS（TCP + UDP） |

## 前置条件

- Ubuntu 24.04（22.04 也可），Docker 已装。
- 宿主机上有一个 `mihomo` 二进制和配套 geo 数据（`Country.mmdb`、`geoip.dat`、`geosite.dat`）。默认从 `/opt/mihomo/` 取，可用 `--mihomo-bin` 指定目录。
- 一个 GitHub 仓库，里面有一个 `shared-rules.yaml`，只含 `rules` / `rule-providers` / `proxy-groups` 三段。位置优先取 `config/shared-rules.yaml`，没有则取根目录 `shared-rules.yaml`，所以既可以指本仓库，也可以指一个只放规则的独立仓库。私有仓库需要一个能读它的 token（`GH_TOKEN` / `GITHUB_TOKEN`，或机器上已登录的 `gh`）。公开仓库两者都可以不配。
- 一个机场订阅 URL。

`shared-rules.yaml` 的校验规则在 `metaxdctl.py:check_shared()` 里，装机与每次同步都会跑：三键齐全、`rules` 非空且唯一的 `MATCH` 收尾、策略组引用的组必须存在、`filter` 必须能编译成正则、`url-test` 组必须有 `url`、`use` 只能引用本机的 `active` provider。

## 节点是怎么来的

基础配置里没有一条 `proxies`，只有一个 `proxy-providers`：

```yaml
proxy-providers:
  active:
    type: http
    url: https://机场订阅链接
    path: ./airport.yaml        # 相对 data/，内核自己写的缓存，不用维护
    interval: 21600             # 6 小时
    proxy: DIRECT               # 拉订阅本身不走代理
    header:
      User-Agent: [mihomo/1.19.33]
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 60
```

拉取是**内核自己做的**，不是面板的功能，也不存在外部下载脚本。定时器 `sync` 只写规则与策略组，不碰节点。

- 内核按 `interval` 拉 `url`，把结果落到 `path`。删掉那个文件，下次照样重拉。
- 从订阅里**只取 `proxies` 段**，机场给的 `rules`、`proxy-groups`、`dns` 全部丢弃——这就是「机场只提供节点」的实现方式。
- 策略组用 `use: [active]` 引用它，节点换了，组的成员自动跟着换，配置一个字都不用改。
- 面板 `/providers/proxies` 里 `active` 的 `vehicleType` 是 `HTTP`、`updatedAt` 是最后拉取时间；其余条目是策略组，`vehicleType` 显示 `Compatible`。

不必等 6 小时就能刷新：

```bash
# 面板里 provider 页面点刷新，或直接调内核 API（path 留空表示按原 URL 重拉）
curl -X PUT http://127.0.0.1:9090/providers/proxies/active \
     -H "Authorization: Bearer $CLASH_SECRET"
```

换机场 = 换这个 `url`（`install --force --airport-url ...`），规则、策略组、节点选择都不受影响。

## 装机

```bash
sudo python3 scripts/metaxdctl.py install \
    --secret '你的内核 secret' \
    --airport-name '机场名' \
    --airport-url 'https://机场订阅链接' \
    --repo owner/shared-rules-repo
```

必填的是 `--secret`、`--airport-name`、`--airport-url`、`--repo`。可选：

- `--airport-interval 21600` 订阅刷新间隔，默认 6 小时
- `--mihomo-bin /opt/mihomo/mihomo` 宿主机二进制
- `--mihomo-path /data/mihomo` 容器内路径
- `--timezone Asia/Shanghai`
- `--skip-traffic` 跳过出口探测（离线或节点未就绪时用）
- `--skip-units` 只装机，不装 systemd 单元（做隔离测试用）
- `--force` 已装过时强制重装

install 做的事，按顺序：

1. 建目录，权限 0700。
2. 拷二进制与 geo 数据进 `data/`。
3. 生成 `runtime.env`（`CONTROL_TOKEN` 随机 36 字节，`CLASH_SECRET` 用你给的 secret）。
4. **取 GitHub 当前 main 的 commit SHA，按 SHA 读 `shared-rules.yaml`**，拼成候选 Merge；拉不到就用仓库里带的快照，保证离线也能装。
5. `mihomo -t` 校验候选配置。容器还没起时用宿主机二进制的 `-d data` 就地校验（所以 geo 数据必须一起拷）。
6. 起容器，等 control API 返回 200（新实例的 profile 列表就是空数组，不能拿真值判断）。
7. 写入 Merge 与基础配置，激活基础配置，等 provider 拉到节点。
8. 跑完整验收（见下），把当前 35 条策略组选择存进 `state.json`，写 `deployment.json`。
9. 装 `metacubexd-rules-sync.timer`，每小时同步一次。

## 同步：只收敛，不打补丁

`sync` 的期望状态永远等于 `merge-base.yaml` 加 GitHub 上那份 `shared-rules.yaml`，没有增量修改这条路。

1. 取 GitHub main 的 SHA，按 SHA 读文件（避免读到新旧混杂的半套内容），跑 `check_shared`。
2. 比对 Merge、运行配置、GitHub 三者在这三个键上是否一致。一致就只做不花流量的验收，打印「无需写入」返回。
3. 不一致才往下走：`mihomo -t` 校验完整候选 → 把当前 Merge 存成 `merge-before-last-sync.yaml` → 记下全部策略组选择 → 写 Merge → 确认激活配置没被别人改过 → 重新激活 → 回读三个键 → 拉节点 → 完整验收 → 恢复选择。
4. 任一步抛出异常，回滚到 `merge-before-last-sync.yaml` 并重新激活，然后才把错误抛出去。

即：**规则是仓库的唯一真相，本机的节点与选择不被覆盖。**

`rollback` 是手动版的第 4 步：把 Merge 退回上次同步前的快照，重新激活并验收。

## 验收标准

`verify()` 不只看接口返回码，它检查的是行为：

- Merge 与运行配置的三个键逐段相等。
- 只有一个 provider 在用，且就是 `active`。
- 每个地区组的成员都是该 provider 节点的子集。
- 地区组类型是 `URLTest`，且 `use: [active]`。
- 规则条数与规则集数量对得上。
- DNS 同时响应 TCP 与 UDP。
- 经代理口探测三个出口：Discord gateway、GitHub、gstatic 204。

`status --verify` 是只读体检，输出容器状态、激活配置、Merge 漂移、provider、节点数，再加上面那套验收。

## 定时器

`systemd/metacubexd-rules-sync.service` 装进 `/etc/systemd/system/` 时，`{{ROOT}}` 会替换成实际部署目录，所以改部署路径不需要改单元。

```ini
ExecStart=/usr/bin/python3 {{ROOT}}/scripts/metaxdctl.py sync
```

`Type=oneshot`、`UMask=0077`、`TimeoutStartSec=5min`；timer 是 `OnCalendar=hourly`、`Persistent=true`。日志用 `journalctl -u metacubexd-rules-sync` 看。

## 改配置的正确姿势

| 想改什么 | 改哪里 | 怎么生效 |
| --- | --- | --- |
| 规则、规则集、策略组 | GitHub 仓库的 `shared-rules.yaml` | 等定时器，或手动 `sync` |
| DNS、sniffer、TUN、CORS 等运行时设置 | 本机 `config/merge-base.yaml` | 跑一次 `install --force`，或改 Merge 后重新激活 |
| 换机场 | `install --force --airport-url ...` | 重装基础配置，Merge 与选择保留 |
| 立刻刷新节点 | 面板 provider 页，或 `PUT /providers/proxies/active` | 内核重拉订阅，`interval` 只决定自动周期 |
| 单台机器的节点选择 | 面板 | 立即生效，同步不会覆盖 |

`merge-base.yaml` 与仓库里的 `shared-rules.yaml` 是分层关系：前者是运行时骨架（不含节点、不含规则），后者只覆盖三个阶段。两者用 `|` 合并，所以 `shared-rules.yaml` 里不要出现 `dns`、`tun` 这类键——`check_shared` 会直接拒掉。

## 隔离测试

整套脚本支持环境变量改路径与端口，可以在同一台机器上起第二套实例做验证：

```bash
export METAXD_ROOT=/tmp/mx-test METAXD_CONTAINER=mx-test \
       METAXD_UI_PORT=18788 METAXD_API_PORT=19090 \
       METAXD_PROXY_PORT=17899 METAXD_DNS_PORT=11553 \
       METAXD_UNIT_DIR=/tmp/mx-test/units
sudo -E python3 scripts/metaxdctl.py install ... --skip-units
```

可用变量：`METAXD_ROOT`、`METAXD_CONTAINER`、`METAXD_MERGE_NAME`、`METAXD_UI_PORT`、`METAXD_API_PORT`、`METAXD_PROXY_PORT`、`METAXD_DNS_PORT`、`METAXD_UNIT_DIR`、`METAXD_IMAGE`、`METAXD_GITHUB_PROXY`、`METAXD_GH`。

## 已知边界

- 镜像用 digest 固定，换版本要自己改 `IMAGE`，不跟着 `latest` 漂。
- `mihomo -t` 只校验语法与引用闭包，不校验节点是否可用；节点可用性靠出口探测。
- 私有仓库必须带 token；纯匿名只能读公开仓库。
- 同步只覆盖三个键。任何写在这三个键之外的、靠 Merge 层生效的设置，都不在同步范围内。
- 面板本身没有「从 GitHub 拉覆写」的能力，这套东西就是在它外面补这一层。

## 目录

```
config/merge-base.yaml      运行时骨架
config/shared-rules.yaml    覆写快照（兜底）
scripts/metaxd.py           公共库
scripts/metaxdctl.py        CLI
systemd/                    单元模板（{{ROOT}} 占位）
docs/architecture.md        为什么这样分层
docs/operations.md          日常操作与故障处理
docs/legacy/                历史迁移期的一次性工具（路径按当时环境，需改）
```
