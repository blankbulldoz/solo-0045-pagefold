# Pagefold 本机部署与升级

这套配置面向可信单机环境：Python 3.11+、SQLite 3.37+，无第三方 Python 包。
状态库、WAL、封存候选和凭证都保存在同一个 SQLite 状态库中。

## 一键演示两个时序

在干净检出目录运行：

```sh
python3 -m compileall -q pagefold examples deploy
python3 deploy/demo_upgrade.py
```

默认产物写入 `.demo-runs/<时间戳>/`；也可用 `--workdir /tmp/pagefold-demo`
指定目录。脚本全部使用真实 HTTP 入口和真实子进程，包含：

- 一个有 revision 1 历史、待重演 revision 2 的开放计划；
- 一个已封存并归档、约 3.8 MiB 的可下载候选；归档只禁止继续写入，不影响凭证/下载读取；
- 一个在 `BEGIN IMMEDIATE` 写事务内延迟的真实重演；
- 一个限速读取大下载的客户端；
- 用第二个 SQLite 连接观察 `database is locked`。

时序如下：

| 时序 | 操作 | 预期结果 |
| --- | --- | --- |
| A. 停接入后排空 | 重演持锁、封存排队、下载在飞时发 `SIGUSR1`，保持进程存活并轮询 `active_writes=0`，然后 `SIGTERM` | `/readyz` 变 503，新写入立即得到 `503 draining`；仅停接入后重演仍提交、封存仍原子落盘，旧进程内历史已为 `[2,1]`、计划已 `sealed`。随后 `SIGTERM` 让慢下载完整完成，进程退出码 0。重启后状态/历史/凭证保持，另一个开放计划可继续封存；排空期间和重启后的下载都匹配凭证。 |
| B. 直接强杀 | 重演持锁、下载在飞时 `SIGKILL` | 重演没有成功响应；未提交的 revision 2 被 SQLite 回滚，历史仍为 `[1]`；写锁随进程死亡释放。重启后计划仍为 `open/revision=2`，可重新重演并封存；旧封存和大文件下载不变。被中断的 TCP 下载只留下不匹配凭证的部分字节，重新下载可通过 SHA-256 和 `integrity_check`。 |

## 无根机常驻：`pagefold_ctl.py`

```sh
# 启动；默认状态库 .runtime/pagefold-state.sqlite，监听 127.0.0.1:8745
python3 deploy/pagefold_ctl.py start

# 查看 /-/status 中的 mode、活动请求/写入/下载计数
python3 deploy/pagefold_ctl.py status

# 改完代码或同步新版本后：
python3 deploy/pagefold_ctl.py upgrade

# 仅停止
python3 deploy/pagefold_ctl.py stop
```

常用参数：

```sh
python3 deploy/pagefold_ctl.py --runtime "$HOME/.local/state/pagefold" --port 8745 start
python3 deploy/pagefold_ctl.py upgrade --write-timeout 60 --stop-timeout 90
```

控制器先发送 `SIGUSR1` 停止接收新写入并等待 `active_writes=0`，再发送
`SIGTERM`。服务收到 `SIGTERM` 后关闭监听 socket，并等待已进入处理器的请求，
包括大下载；超过 `--drain-timeout` 才以 124 退出。

## systemd 本机服务

干净的 Linux/systemd 主机可运行：

```sh
sudo deploy/install_systemd.sh
```

它会：

1. 检查 SQLite `serialize/deserialize` 能力；
2. 若旧服务在运行，先 `systemctl reload pagefold`（即 `SIGUSR1`）并轮询
   `/-/status` 到 `active_writes=0`，尽早停止新操作；
3. 把当前检出复制到 `/opt/pagefold/releases/<时间戳>`，编译检查后原子切换
   `/opt/pagefold/current`；
4. 安装 `deploy/systemd/pagefold.service`，执行 `daemon-reload` 和
   `systemctl restart pagefold`；
5. 等待 `/healthz`。

手工运维：

```sh
systemctl status pagefold
curl -s http://127.0.0.1:8745/-/status
sudo systemctl reload pagefold     # SIGUSR1：停止新写入，继续读/下载
sudo systemctl restart pagefold    # SIGTERM 排空后重启
journalctl -u pagefold -f
```

单元中 `PAGEFOLD_DRAIN_TIMEOUT=45`，`TimeoutStopSec=60`；务必保持 systemd
停止宽限大于应用排空超时。

## 运行时 HTTP 端点

- `GET /healthz`：进程存活信息，始终返回 JSON。
- `GET /-/status`：生命周期模式和活动计数。
- `GET /readyz`：`running` 时 200，写排空/停止中为 503，可供本机接入层摘流。

`/-/status` 示例：

```json
{
  "mode": "writes_draining",
  "accepting": true,
  "accepting_writes": false,
  "active_requests": 2,
  "active_writes": 1,
  "active_downloads": 1,
  "drain_timeout_seconds": 45.0
}
```

## 信号和一致性边界

- `SIGUSR1`：进入 `writes_draining`。新的 `POST` 在进入业务逻辑前收到
  `503` 和 `code="draining"`；历史读取、凭证读取和已有/新的 GET 下载仍可继续。
- `SIGTERM`：进入 `stopping`，拒绝新请求，关闭监听 socket，并等待所有已接纳
  请求。接纳和活动计数在同一把锁内完成，因此请求不会落在“既被拒绝又被计数”
  的缝隙中。
- `SIGKILL`/断电：依赖 SQLite WAL 和事务恢复。`rehearse` 的候选与
  `attempts` 记录在同一写事务中；`seal` 的 `releases` 插入和计划状态更新也在
  同一写事务中。未提交事务回滚，已提交历史和封存对象保留。

当前下载响应是固定候选的完整 `Content-Length` 响应，尚未提供 Range/断点续传。
因此强杀时正在传输的那一条 HTTP 连接不能继续；这不是候选损坏。客户端应始终用
凭证中的 `candidate_digest` 校验完整文件；若传输中断，重新请求
`/api/plans/{id}/download` 即可得到同一个不可变候选。

## 文件说明

- `demo_upgrade.py`：两个时序的可重复断言演示。
- `pagefold_ctl.py`：无根机启动、状态查看、排空和升级重启。
- `install_systemd.sh`：systemd 干净安装/升级脚本。
- `systemd/pagefold.service`：本机 systemd 单元。
- `slow_demo_server.py`：仅演示使用的延迟重演接缝；生产入口仍是
  `python3 -m pagefold`。
