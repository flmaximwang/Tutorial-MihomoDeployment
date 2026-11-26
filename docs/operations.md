# 日常操作与故障处理

## 三个入口

| 入口 | 用途 |
| --- | --- |
| `metaxdctl.py install` | 首次装机、换机场、改运行时骨架 |
| `metaxdctl.py sync` | 收敛 GitHub 覆写（定时器自动跑） |
| `metaxdctl.py status --verify` | 只读体检 |

`status` 不带 `--verify` 只查容器与漂移，很快；带 `--verify` 会做 DNS 与出口探测，需要几十秒。

## 改规则

改 GitHub 仓库里的 `shared-rules.yaml`，然后：

```bash
sudo python3 /opt/metacubexd-prod/scripts/metaxdctl.py sync
```

推送后也可以等定时器，最多一小时。想知道下一次同步有没有落地，看两处：

- `systemctl status metacubexd-rules-sync.timer` 的 `Trigger:` 时间。
- `status` 的 `merge_drift` 是否为 `null`。

同步成功后节点选择会被恢复，不需要重新选。

### 规则写错了怎么办

`check_shared` 和 `mihomo -t` 两道闸，任一没过都不会写入。日志里会给出具体那一条：

- `shared-rules.yaml 只能包含 rules, rule-providers, proxy-groups` —— 多写了键。
- `rules 必须以唯一的 MATCH 收尾` —— MATCH 不在最后，或有两条。
- `策略组「X」引用了未定义的组「Y」` —— 组之间引用不闭包。
- `规则指向未定义的策略组「Y」：DOMAIN,example.com,Y` —— 规则指向了不存在的组。
- `mihomo 拒绝了候选配置：` 后面跟三行内核原生报错，同时写进 `validation-error.log`。

### 已经在线上坏了怎么办

```bash
sudo python3 /opt/metacubexd-prod/scripts/metaxdctl.py rollback
```

退回上一次成功同步前的 Merge 快照（`merge-before-last-sync.yaml`），重新激活并验收。

注意 `rollback` 只能退一步，而且只退 Merge。要退更远，直接改仓库再 `sync` —— 因为同步是收敛式的，把文件改对就会回到正确状态。

## 改运行时骨架

DNS、sniffer、TUN、CORS、`mode` 这些在 `config/merge-base.yaml`。改完要重新装机：

```bash
sudo python3 /opt/metacubexd-prod/scripts/metaxdctl.py install --force \
    --secret "$(grep '^CLASH_SECRET=' /opt/metacubexd-prod/runtime.env | cut -d= -f2)" \
    --airport-name '机场名' --airport-url '订阅链接' \
    --repo owner/repo
```

`--force` 会重建容器，但 **Merge 与节点选择在重建前会重新写入并恢复**。如果只想换 DNS 而不想动别的，更轻的做法是直接改 Merge 后重新激活（面板里点一下），但那就不在脚本管理范围内了 —— 下次 `sync` 只会覆盖三个键，DNS 是你自己改的，不会被覆盖，也不会被记录。

## 换机场

```bash
sudo python3 /opt/metacubexd-prod/scripts/metaxdctl.py install --force \
    --secret '...' --airport-name '新机场' --airport-url '新订阅链接' --repo owner/repo
```

- 旧机场的基础配置会被 `save_profile` 覆盖同名项；名字不同则会留下一个孤儿 profile，可以在面板里删。
- 节点选择会尝试恢复，能匹配上的保留，匹配不上的回默认。
- 地区组会自动按新机场的节点名重新筛（`use: [active]` + `filter`，不需要改规则）。

## 故障处理

### 容器不在跑

```bash
docker ps -a --filter name=metacubexd-prod
docker logs --tail 50 metacubexd-prod
```

容器 `--restart unless-stopped`，正常会自己起来。起不来看日志里 `mihomo` 的报错 —— 多半是配置语法（此时 `validate` 会在写入前就拦住，能进到这一步通常是磁盘或权限问题）。

### 容器健康但没节点

`status` 里 `nodes` 为 0：

- 订阅 URL 失效或过期 —— 面板里看基础配置的 `proxy-providers.active` 状态。
- 出口不通 —— 容器拉订阅走 `proxy: DIRECT`，需要宿主机本身能出去。
- 间隔太长 —— `interval: 21600`，改完 URL 要等或手动刷新。

手动刷新 provider（节点是内核自己拉的，不需要外部脚本）：

```bash
# 面板 provider 页面点刷新，或 path 留空按原 URL 重拉
curl -X PUT http://127.0.0.1:9090/providers/proxies/active \
     -H "Authorization: Bearer $CLASH_SECRET"
```

缓存文件是 `data/airport.yaml`（相对 `data/` 的 `path`），内核自己写，删掉也会重拉；不要去编辑它。

### DNS 不通

`verify` 报 `DNS +notcp 查询失败`：

```bash
dig @127.0.0.1 -p 1053 discord.com +short
```

- 端口没监听：`ss -lntup | grep 1053`，看容器端口映射是否还在。
- 有应答但为空：内核的 `nameserver` 在 `merge-base.yaml`，确认上游可达。
- 只有 UDP 通、TCP 不通：`dns.listen` 必须是 `0.0.0.0:1053`（容器内），写 `127.0.0.1:1053` 时容器外 TCP 打不进来。

### 出口探测失败

`verify` 报 `出口探测失败：https://...`：

- 先手动验：`curl -x http://127.0.0.1:7890 -o /dev/null -w '%{http_code}' https://www.gstatic.com/generate_204`
- 返回 `000`：节点不可用或规则把流量送进了坏组。
- 返回 `200` 但验收要的是 `204`：出口是透明代理或中间设备改写了响应，不一定是故障。
- 只想跳过探测：`sync --skip-traffic` / `status --verify` 无此开关，用 `sync --skip-traffic` 确认收敛即可。

### GitHub 拉不到

```
拉取 GitHub 失败（5 次）：URLError: <urlopen error ...>
```

- 私有仓库没 token：`export GH_TOKEN=...`，或确保机器上有 `gh auth login` 的登录态（root 跑脚本时会 `runuser -u ubuntu` 借登录态）。
- 网络问题：`METAXD_GITHUB_PROXY` 默认 `http://127.0.0.1:7890`，脚本在该代理与直连之间交替；代理本身挂了就设成空串走直连。
- 只是想验证脚本：本地快照兜底 —— `install` 拉不到 GitHub 时会用仓库里的 `config/shared-rules.yaml`，但 `sync` 不会兜底（同步的语义就是「和 GitHub 一致」）。

### 同步报 `写入期间激活配置发生变化`

同步过程中有人在面板切了基础配置。这是保护：脚本拒绝把 Merge 写到一个它不认识的激活状态上。重跑一次 `sync` 即可。

## 日志位置

| 内容 | 位置 |
| --- | --- |
| 同步与定时器 | `journalctl -u metacubexd-rules-sync` |
| 容器与内核 | `docker logs metacubexd-prod` |
| 校验失败详情 | `/opt/metacubexd-prod/validation-error.log` |
| 上次同步前的 Merge | `/opt/metacubexd-prod/merge-before-last-sync.yaml` |
| 节点选择快照 | `/opt/metacubexd-prod/selections.json` |

## 第一次上线前值得做的检查

1. `status --verify` 全绿。
2. 故意在仓库里加一条错规则，推上去，确认定时器把错误拦住且生产没变（这是验证闸门真的在工作）。
3. 面板里切一个地区组的节点，手动 `sync` 一次，确认选择被保留。
4. 重启容器（`docker restart metacubexd-prod`），确认节点与选择都回来了。

## 历史工具

`docs/legacy/` 下是迁移期用的一次性脚本：拉两家机场订阅、生成候选规则 CSV、切组后核指纹与出口、模拟订阅源验地区组重筛、独立 mihomo 的 active 切换、产出 6000+ 条规则的产线。

它们硬编码了当时的路径（`/opt/sub-store/data/sub-store.json`、`/etc/mihomo-prod/config.yaml`、`/opt/mihomo`），机场名也已替换成占位值。**留作方法参考，不要直接跑**。今天要做的事，`metaxdctl.py` 四个子命令已经覆盖。
