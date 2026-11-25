# 下一位操作者入口

三个现场都在同一个状态库 `handover/handover-state.sqlite`。先启动工作区（默认仅监听
`127.0.0.1:8745`），浏览器或 `curl` 访问同一地址：

```sh
python3 -m pagefold --state ./handover/handover-state.sqlite
# 打开 http://127.0.0.1:8745/ ，首页“合流计划”区列出全部三个现场
```

计划 ID 每次重建（`python3 -m tools.build_handover`）都会变化，以下命令直接从
`handover/cases-index.json` 读取当前 ID，重建后依然可用。先在 shell 里准备：

```sh
BASE=http://127.0.0.1:8745
A=$(python3 -c "import json;print(json.load(open('handover/cases-index.json'))['cases'][0]['plan_id'])")
B=$(python3 -c "import json;print(json.load(open('handover/cases-index.json'))['cases'][1]['plan_id'])")
C=$(python3 -c "import json;print(json.load(open('handover/cases-index.json'))['cases'][2]['plan_id'])")
echo "A=$A B=$B C=$C"
```

只读复核不需要手点：另开终端跑 `python3 -I tools/verify_handover.py --live`。

## 现场 A —— 开放，唯一需要继续干活的计划

- 网页入口：`/plans/$A` ｜ JSON：`/api/plans/$A` ｜ 历史：`/api/plans/$A/attempts`
- 现状：`open`，第 3 版，`node ('north',1)` 冲突未裁决（`unresolved=1`）。
- **注意**：历史里 rev1 那条 `passed` 已随改判失效，不能封存。先裁决（版本会推进），
  再重演**当前版**，通过后才能封存。冲突条目 ID 由表名+主键指纹决定，重建也不变：

```sh
# 1) 看当前版本号和待裁决项
curl -s $BASE/api/plans/$A
# 2) 裁决（revision 必须等于当前版；item 是 automatic=null 条目的 id）
curl -s -X POST $BASE/api/plans/$A/decisions -H 'Content-Type: application/json' \
  -d '{"revision":3,"item":"3d9fecf2f8e0e841605f0984d234e878b149ba4a0758ce68606e4c9543b0c3bf","choice":"right"}'
# 3) 用返回的新版本号重演，再封存（把 <rev> 换成上一步返回的 revision）
curl -s -X POST $BASE/api/plans/$A/rehearse -H 'Content-Type: application/json' -d '{"revision":<rev>}'
curl -s -X POST $BASE/api/plans/$A/seal    -H 'Content-Type: application/json' -d '{"revision":<rev>}'
```

受阻旁证计划（只看不动，记录唯一约束冲突）的网页入口见
`handover/site-a-open/case-summary.json` 的 `entries.stress_plan_page`。

## 现场 B —— 已归档未封存，只读，无东西可领

- 网页入口：`/plans/$B` ｜ 历史：`/api/plans/$B/attempts`（一条 `blocked`）。
- 一切写操作（裁决/重演/封存）都 409 `closed_plan`；`/download`、`/certificate` 永远
  404 `unpublished`。这里**没有继任动作**；如需重新合流，请用同源镜像新建计划，而不是
  重开这个归档计划。

```sh
curl -s $BASE/api/plans/$B
curl -s $BASE/api/plans/$B/attempts
```

## 现场 C —— 封存后归档，领取成果并离线核对

- 网页入口：`/plans/$C`
- 下载：`/api/plans/$C/download` ｜ 凭证：`/api/plans/$C/certificate`
- 写入口已关闭（`closed_plan`），但归档不影响领取。落盘副本在
  `handover/site-c-sealed/release/`，与在线下载字节一致。

```sh
curl -s $BASE/api/plans/$C/download -o /tmp/result.sqlite
curl -s $BASE/api/plans/$C/certificate -o /tmp/cert.json
# 下载文件摘要必须等于凭证 candidate_digest
sha256sum /tmp/result.sqlite
python3 -c "import json;print(json.load(open('/tmp/cert.json'))['candidate_digest'])"
# 独立打开做完整性/外键检查
python3 -c "import sqlite3;db=sqlite3.connect('/tmp/result.sqlite');\
print(db.execute('PRAGMA integrity_check').fetchone(),\
db.execute('PRAGMA foreign_key_check').fetchall())"
```

## 写入口的版本纪律（适用于任何仍开放的计划）

- 写请求都要带当前 `revision`；过期返回 409 `stale_revision`，刷新计划后重试。
- 同值重复裁决幂等、不推进版本；真实改判才推进版本，并使旧重演证据过期。
- 封存要求**当前版本**有 `passed` 候选；只看到历史“通过”时封存被拒
  （`rehearsal_required`）——这正是现场 A 当前的状态，也是“通过 ≠ 可交付”的现场。
- 封存/归档后不可再编辑；归档幂等，重复归档不产生新写入。
