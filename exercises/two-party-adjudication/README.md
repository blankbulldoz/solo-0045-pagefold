# 两人复合键冲突裁决演习

本目录是一次围绕 Pagefold 三方合流的模拟裁决演习材料，起点是仓库示例中既有的复合主键冲突
`node(bench, slot) = ('north', 1)`（表 `node` 为 WITHOUT ROWID，主键是两列复合键）。

## 角色与来源

| 角色 | 手上的镜像 | 镜像 SHA-256（前 12 位） | 立场 |
| --- | --- | --- | --- |
| 共同祖本 | `quartz.sqlite` | `90438d5a…1a25` | 裁决可选项之一 |
| 第一组（左支方） | `cerulean.sqlite` | `ab1bceb8…6f75` | 认为左支对 `('north',1)` 的覆写正确 |
| 第二组（右支方） | `umber.sqlite` | `d6a547d0…a009` | 认为右支对同一行的改动正确；**工作页版本落后一个修订版** |

三方镜像由 `python3 -m examples.make_images ./demo-images` 生成，字节按 SHA-256 寻址，
所以上表指纹在任何机器上都一致。每次新建计划的 `plan_id` 是随机 UUID，不复刻。

## 计划中的三个差异条目

| # | 表 / 键 | 性质 | 系统行为 |
| --- | --- | --- | --- |
| 1 | `bench ('south')` | 仅右支把标签 `砂层` 改为 `右支更名` | **自动采纳右支**，没有裁决表单 |
| 2 | `node ('south', 2)` | 祖本缺席，仅左支新增 `左支新增 / NULL / 4.0` | **自动采纳左支**，没有裁决表单 |
| 3 | `node ('north', 1)` | 三方各不相同（见下） | **唯一人工裁决冲突** |

第 1、2 项是“本来就自动采纳的分支改动”，在计划页带 `自动` 标记；第 3 项带 `冲突` 标记。
参与者据此区分**自动行**和**自己选择的行**。

冲突行 `('north',1)` 的三方取值（列：bench, slot, note, payload, weight）：

| 来源 | note | payload | weight |
| --- | --- | --- | --- |
| 祖本 quartz | `祖本节点` | `0014`（BLOB） | 1.5 |
| 左支 cerulean | `左支覆写` | `0014`（BLOB） | 1.5 |
| 右支 umber | `祖本节点` | `ff00`（BLOB） | 2.25 |

## 版本时间线（裁决只允许携带当前 revision）

| revision | 事件 | 结果 |
| --- | --- | --- |
| 0 | 计划建立，1 项待裁决 | 未决时重演被 `409 unresolved` 拦截 |
| 0 → 1 | 第一组先提交冲突选择 `right` | **接受**，revision 推进到 1 |
| 0（过期） | 第二组用落后的工作页提交 `left` | **拒绝** `409 stale_revision`，版本不变 |
| 1 → 2 | 第二组刷新看到 revision=1、当前选择 right，重新提交 `left` | **接受**，覆盖第一组选择，revision 推进到 2 |
| 2 | 同值重复提交 `left` | 幂等，版本保持 2 |
| 2 | 重演 | `passed`，`changed=3`（两条自动行 + 一条人工行） |
| 2 | 封存、下载 | 凭证绑定四个摘要；封存后再写被 `409 closed_plan` 拒绝 |

最终生效的冲突选择是第二组的 `left`：第一组的提交曾被接受并进入过 revision 1，
但在 revision 2 被覆盖；第二组的过期提交从未落库。

## 目录内容

- `team-a-cerulean.md` —— 发给第一组的短材料（只含该组应有的信息与动作）。
- `team-b-umber.md` —— 发给第二组的短材料（含落后工作页设定与刷新重试动作）。
- `facilitator-result-sheet.md` —— 主持人结果清单：固定指纹、逐步预期响应、最终行集与复盘问题。
- `drill_run.py` —— 可实跑的操作版序，自建临时服务走真实 HTTP 入口，17 项断言并落盘证据。
- `evidence/` —— 运行 `drill_run.py` 生成的证据 JSON（运行产物，已在 `.gitignore` 忽略）。

## 一次实跑

```sh
# 方式一：脚本自建临时状态库和服务，跑完即销毁
python3 exercises/two-party-adjudication/drill_run.py

# 方式二：对着已启动的服务执行
python3 -m pagefold --state ./pagefold-state.sqlite --port 8745
python3 exercises/two-party-adjudication/drill_run.py --origin http://127.0.0.1:8745
```

退出码 0 表示 17 项核对全部通过；完整请求/响应时间线写入
`evidence/drill-evidence.json`。手工 curl 版步骤见主持人清单。
