# 主持人结果清单 · 两人复合键裁决演习

本清单所有指纹均由真实 HTTP 入口实测得到（2026-11-25，Python 3.11 / SQLite 3.40）。
内容型指纹跨运行稳定；`plan_id` 每次新建随机，清单中以 `{plan_id}` 占位。

## 0. 演习前准备

```sh
python3 -m examples.make_images ./demo-images          # 生成三份（含 stress）示例镜像
python3 -m pagefold --state ./pagefold-state.sqlite    # 启动服务，默认 127.0.0.1:8745
```

在另一终端用 curl 登记并建计划（Windows PowerShell 请改用网页表单或 `drill_run.py`）：

```sh
BASE=$(curl -s -X POST localhost:8745/api/images -H 'Content-Type: application/octet-stream' \
  -H 'X-Image-Name: quartz' --data-binary @demo-images/quartz.sqlite | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
LEFT=$(curl -s -X POST localhost:8745/api/images -H 'Content-Type: application/octet-stream' \
  -H 'X-Image-Name: cerulean' --data-binary @demo-images/cerulean.sqlite | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
RIGHT=$(curl -s -X POST localhost:8745/api/images -H 'Content-Type: application/octet-stream' \
  -H 'X-Image-Name: umber' --data-binary @demo-images/umber.sqlite | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -X POST localhost:8745/api/plans -H 'Content-Type: application/json' \
  -d "{\"base\":\"$BASE\",\"left\":\"$LEFT\",\"right\":\"$RIGHT\"}"
```

也可以直接在首页点“载入示例并创建计划”，或 `POST /demo` 一键建样例计划。

## 1. 固定不变的输入与条目指纹

| 对象 | 指纹（SHA-256） |
| --- | --- |
| 祖本 quartz | `90438d5a76cd0744920d78acb009df1d19aa23baad0a7ed6775927b53e7a1a25` |
| 左支 cerulean | `ab1bceb83ee5599db0a06e4429977951f26378db082cbffa052f8215a14d6f75` |
| 右支 umber | `d6a547d0982d6150b30a8a78706c3db162c3d339c41ee918a718e46dce39a009` |
| 冲突条目 `node ('north',1)` | `3d9fecf2f8e0e841605f0984d234e878b149ba4a0758ce68606e4c9543b0c3bf` |
| 自动条目 `bench ('south')` → right | `836ae6cbe88b89d5ab4061b352a8bba0f34bdb4553b1417311b78d6f50eb4065` |
| 自动条目 `node ('south',2)` → left | `1ee048a2b874422c6d36d4111f8f45b9673030fa0388f262f92b7c2d85444276` |

冲突条目的三方行（JSON 类型编码 `t` 文本 / `i` 整数 / `r` 实数 / `b` BLOB 的 base64 / `n` NULL）：

- base：`[["t","north"],["i","1"],["t","祖本节点"],["b","ABQ="],["r","0x1.8p+0"]]`
- left：`[["t","north"],["i","1"],["t","左支覆写"],["b","ABQ="],["r","0x1.8p+0"]]`
- right：`[["t","north"],["i","1"],["t","祖本节点"],["b","/wA="],["r","0x1.2p+1"]]`

## 2. 逐步预期结果（主持人口令表）

| # | 口令 / 动作 | 请求要点 | 预期 HTTP | 预期响应关键字段 |
| --- | --- | --- | --- | --- |
| 1 | 建计划后查看 | `GET /api/plans/{id}` | 200 | `revision=0`、`unresolved=1`、`items` 长度 3 |
| 2 | 未决即重演 | `POST …/rehearse {"revision":0}` | **409** | `code=unresolved` |
| 3 | 第一组先提交 | `POST …/decisions {"revision":0,"item":"3d9f…","choice":"right"}` | 200 | `revision=1, choice=right` |
| 4 | 第二组旧页提交 | `POST …/decisions {"revision":0,"item":"3d9f…","choice":"left"}` | **409** | `code=stale_revision`，计划仍为 revision 1 |
| 5 | 第二组刷新 | `GET /api/plans/{id}` | 200 | `revision=1`，冲突条目 `choice=right` |
| 6 | 第二组重新提交 | `POST …/decisions {"revision":1,"item":"3d9f…","choice":"left"}` | 200 | `revision=2, choice=left` |
| 7 | 同值重复提交 | `POST …/decisions {"revision":2,"item":"3d9f…","choice":"left"}` | 200 | `revision=2`（幂等，不推进） |
| 8 | 重演当前版 | `POST …/rehearse {"revision":2}` | 200 | `status=passed, changed=3` |
| 9 | 封存前下载 | `GET …/download` | **404** | `code=unpublished` |
| 10 | 封存 | `POST …/seal {"revision":2}` | 200 | 凭证见下节 |
| 11 | 封存后再裁决 | `POST …/decisions {"revision":2,…}` | **409** | `code=closed_plan` |
| 12 | 下载并校验 | `GET …/download` | 200 | 字节 SHA-256 等于凭证 `candidate_digest` |

> 向自动条目发裁决会得到 `409 not_conflict`（“该条目不是可裁决的冲突”），
> 可在答疑时演示，说明自动行不接受人工改写。

## 3. 封存凭证中应出现的固定摘要

```
format            pagefold-certificate-v1
revision          2
schema            2850b20c7d73163e0e8f153ae064af159f3389cc6d3ed1a5c909218ba29fb6ac
plan_digest       a9d19d0259fa61f4fac8ab9119ecce992be8764d5e66df3b27808e00f4722f32
decision_digest   01672756d23e15b5bc68bd117637ce7e6cf648254a2f2427279e95ac3807a95a
candidate_digest  8d09772f0076e47abba9eee777569d717b3758e8ca92881f8758b66d40300da0
candidate_size    24576
```

`sources` 三个镜像 ID 必须与第 1 节一致。重演历史 `GET …/attempts` 只有 revision 2 一条
通过记录——revision 0/1 从未成功重演，不留候选。

## 4. 最终候选库必须恰好包含的 6 行

下载文件可独立用 SQLite 打开（`sqlite3 pagefold-*.sqlite`），预期：

**bench（2 行）**

| code | label | 来源性质 |
| --- | --- | --- |
| north | 玄石 | 三方无差异，祖本原样保留 |
| south | **右支更名** | 自动采纳右支（无人裁决） |

**node（3 行）**

| bench | slot | note | payload | weight | 来源性质 |
| --- | --- | --- | --- | --- | --- |
| north | 1 | **左支覆写** | `0014` | 1.5 | **人工裁决**：第二组刷新后提交的 left，revision 2 生效 |
| south | 1 | 稳定节点 | （空 BLOB） | 3.0 | 三方无差异，祖本原样保留 |
| south | 2 | 左支新增 | NULL | 4.0 | 自动采纳左支（祖本缺席的新增行） |

**binding（1 行）**：`(1, 'north', 1)`，外键仍指向裁决后的 `node ('north',1)`，
`PRAGMA foreign_key_check` 必须为空。

行性质核对口径：`changed=3` 只统计相对祖本发生变化的行——两条自动行 + 一条人工行；
`north/玄石`、`south/稳定节点`、binding 这三行三方一致，不进差异清单也不产生改写。

## 5. 裁决归属复盘（回答“谁的提交被接受过”）

| 提交 | 携带 revision | 选择 | 是否落库 | 留下的痕迹 |
| --- | --- | --- | --- | --- |
| 第一组首次 | 0 | right | 是 | 计划推进到 revision 1，曾是生效选择 |
| 第二组首次 | 0（过期） | left | **否** | 仅一条 `stale_revision` 409 响应，无版本、无裁决记录 |
| 第二组重试 | 1 | left | 是 | 计划推进到 revision 2，覆盖 right，成为最终选择 |
| 第二组重复 | 2 | left | 幂等 | 版本不变，响应仍返回 revision 2 |

关键结论：第一组的提交**被接受过但没有存活**；第二组第一次提交**从未被接受**。
系统以乐观版本号（CAS）保证后写入者必须基于最新版本，拒绝盲写覆盖。

## 6. 主持串场要点与参考答案

- 为什么自动行不需要裁决？单边改动（或两边同值）在 `merge.calculate` 阶段即确定去向，
  计划接口里 `automatic` 非空，裁决端点直接以 `not_conflict` 拒收。
- 过期提交为什么是 409 而不是排队？裁决写入要求请求体 `revision` 等于计划当前版本；
  不符即 `stale_revision`，强制人工刷新后重试，避免静默覆盖。
- 重演失败长什么样（可选演示）：用 `stress-left` + `stress-right` 另建计划，
  重演返回 `status=blocked, problem.code=constraint`，且不产生可下载候选。
- 两组各题的参考答案已隐含在第 2、4、5 节中；让参与者先自己答，再对照本清单。

## 7. 一键复核

```sh
python3 exercises/two-party-adjudication/drill_run.py
# 退出码 0 = 17 项断言全过；证据在 exercises/two-party-adjudication/evidence/drill-evidence.json
```
