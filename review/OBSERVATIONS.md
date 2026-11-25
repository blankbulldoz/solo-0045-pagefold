# Pagefold 评审 · 真实操作观察记录

- 日期：2026-11-25 05:04:55 UTC
- 环境：Python 3.11.2 ／ SQLite 3.40.1 ／ 工作区提交 `baec633`
- 方式：临时状态库 ＋ 随机端口的真实 HTTP 服务；四组样本各自完成 上传 → 建计划 → 裁决 → 重演 → 封存 → 下载校验 全流程。
- 同批原始记录见 `observations.json`；样本与差异表见 `MANIFEST.md`。

## 总览

| 组 | 输入行 | 输入字节 | 计划条目 | 自动 | 冲突 | 重演变更 | 条目/千行 | 候选字节 | 计划耗时 | 重演耗时 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| a-small-conflict（小冲突） | 55 | 49,152 | 5 | 4 | 1 | 5 | 90.91 | 16,384 | 6.3 ms | 6.1 ms |
| b-dense-auto（密集自动更新） | 12,400 | 405,504 | 1,500 | 1,500 | 0 | 1,500 | 120.97 | 155,648 | 99.1 ms | 68.4 ms |
| c-bulk-identical（大量相同行） | 48,000 | 1,249,280 | 7 | 6 | 1 | 7 | 0.15 | 417,792 | 247.0 ms | 148.5 ms |
| d-noop-decisions（空转裁决（有效成果刻意少于计划项）） | 144 | 49,152 | 10 | 4 | 6 | 6 | 69.44 | 16,384 | 5.9 ms | 6.4 ms |

## a-small-conflict — 小冲突

少量行分歧：核对逐行裁决、版本拦截、封存与归档流程。

观察：
- 未裁决直接重演被 409（unresolved）拦截；过期版本裁决被 409（stale_revision）拒绝；同值重复裁决不推进版本。
- 重演变更 5 行 ＝ 计划条目 5：本组无空转项；封存后归档，凭证与候选摘要保持不变。
- 候选 16,384 字节：摘要与凭证一致，integrity_check 通过、无外键违规、行数与内容抽样全部符合预期。

校验（26 项，全部通过）：
- ✓ base 镜像登记，摘要与清单一致
- ✓ left 镜像登记，摘要与清单一致
- ✓ right 镜像登记，摘要与清单一致
- ✓ 计划条目 5 ＝ 独立差异表 5
- ✓ 自动采纳 4 ＝ 独立差异表 4
- ✓ 待裁决 1 ＝ 独立差异表 1
- ✓ 存在未决项时重演被拦截（409 unresolved）
- ✓ 过期版本裁决被拒绝（409 stale_revision）
- ✓ 同值重复裁决不推进版本（幂等）
- ✓ 重演通过（status=passed）
- ✓ 重演变更 5 ＝ 预期 5
- ✓ 同版本重演复用首次证据
- ✓ 重复封存结果稳定
- ✓ 凭证绑定三个来源
- ✓ 下载摘要与封存凭证一致
- ✓ 候选 integrity_check 通过
- ✓ 候选无外键违规
- ✓ 候选 entry 行数 9 ＝ 预期 9
- ✓ 候选 tag 行数 10 ＝ 预期 10
- ✓ 候选内容 SELECT qty FROM entry WHERE id=1 → 999
- ✓ 候选内容 SELECT note FROM entry WHERE id=1 → '祖本记录-1'
- ✓ 候选内容 SELECT note FROM entry WHERE id=3 → '右支改写-3'
- ✓ 候选内容 SELECT COUNT(*) FROM tag WHERE entry_id=2 AND label='t2' → 0
- ✓ 候选内容 SELECT note FROM entry WHERE id=9 → '左支新增-9'
- ✓ 归档后状态为 archived
- ✓ 归档后凭证保持可提取且不变

## b-dense-auto — 密集自动更新

单侧高密度更新：计划条目多，但全部自动采纳、零冲突。

观察：
- 1,500 条计划全部自动采纳、零冲突：左支 1,400 项（1,000 改 ＋ 200 增及其标签），右支 100 项。
- 同版本重复重演复用首次证据：首次 68.4 ms，复用 1.3 ms。
- 计划响应 640,868 字节 / 1,500 条，条目数随变更行数线性增长。
- 候选 155,648 字节：摘要与凭证一致，integrity_check 通过、无外键违规、行数与内容抽样全部符合预期。

校验（20 项，全部通过）：
- ✓ base 镜像登记，摘要与清单一致
- ✓ left 镜像登记，摘要与清单一致
- ✓ right 镜像登记，摘要与清单一致
- ✓ 计划条目 1500 ＝ 独立差异表 1500
- ✓ 自动采纳 1500 ＝ 独立差异表 1500
- ✓ 待裁决 0 ＝ 独立差异表 0
- ✓ 重演通过（status=passed）
- ✓ 重演变更 1500 ＝ 预期 1500
- ✓ 同版本重演复用首次证据
- ✓ 重复封存结果稳定
- ✓ 凭证绑定三个来源
- ✓ 下载摘要与封存凭证一致
- ✓ 候选 integrity_check 通过
- ✓ 候选无外键违规
- ✓ 候选 entry 行数 2200 ＝ 预期 2200
- ✓ 候选 tag 行数 2200 ＝ 预期 2200
- ✓ 候选内容 SELECT COUNT(*) FROM entry WHERE note='左支密集更新' → 1000
- ✓ 候选内容 SELECT qty FROM entry WHERE id=2 → 9
- ✓ 候选内容 SELECT weight FROM entry WHERE id=1 → 0.75
- ✓ 候选内容 SELECT COUNT(*) FROM entry WHERE id BETWEEN 2001 AND 2200 → 200

## c-bulk-identical — 大量相同行

三份镜像 99.96% 行相同：验证大量相同键不会被误计为大量变更。

观察：
- 三份输入合计 48,000 行 / 1,249,280 字节，计划仅 7 条：15,995 行三方相同被跳过，未计入变更。
- 计划响应仅 3,027 字节，与输入体积无比例关系；计划创建 247.0 ms、重演 148.5 ms（全量扫描与校验的成本随输入行数走，不随条目数走）。
- 候选 417,792 字节：摘要与凭证一致，integrity_check 通过、无外键违规、行数与内容抽样全部符合预期。

校验（24 项，全部通过）：
- ✓ base 镜像登记，摘要与清单一致
- ✓ left 镜像登记，摘要与清单一致
- ✓ right 镜像登记，摘要与清单一致
- ✓ 计划条目 7 ＝ 独立差异表 7
- ✓ 自动采纳 6 ＝ 独立差异表 6
- ✓ 待裁决 1 ＝ 独立差异表 1
- ✓ 存在未决项时重演被拦截（409 unresolved）
- ✓ 过期版本裁决被拒绝（409 stale_revision）
- ✓ 同值重复裁决不推进版本（幂等）
- ✓ 重演通过（status=passed）
- ✓ 重演变更 7 ＝ 预期 7
- ✓ 同版本重演复用首次证据
- ✓ 重复封存结果稳定
- ✓ 凭证绑定三个来源
- ✓ 下载摘要与封存凭证一致
- ✓ 候选 integrity_check 通过
- ✓ 候选无外键违规
- ✓ 候选 entry 行数 8000 ＝ 预期 8000
- ✓ 候选 tag 行数 8000 ＝ 预期 8000
- ✓ 候选内容 SELECT note FROM entry WHERE id=200 → '左支冲突-200'
- ✓ 候选内容 SELECT note FROM entry WHERE id=100 → '左支微调-100'
- ✓ 候选内容 SELECT qty FROM entry WHERE id=300 → 5
- ✓ 候选内容 SELECT COUNT(*) FROM entry WHERE id=7000 → 0
- ✓ 候选内容 SELECT COUNT(*) FROM entry WHERE id=8001 → 1

## d-noop-decisions — 空转裁决（有效成果刻意少于计划项）

6 项冲突中 4 项刻意裁决为祖本：计划 10 条，实际变更只有 6 行。

观察：
- 10 条计划（6 冲突 ＋ 4 自动），6 项裁决中 4 项选祖本：重演变更 6 行 ＜ 计划条目 10，祖本裁决不产生实际变更。
- 下载候选中 entry 1–4 保持祖本值，entry 5 为左支值、entry 6 为右支值；凭证 decision_digest 绑定全部 6 项裁决。
- 候选 16,384 字节：摘要与凭证一致，integrity_check 通过、无外键违规、行数与内容抽样全部符合预期。

校验（27 项，全部通过）：
- ✓ base 镜像登记，摘要与清单一致
- ✓ left 镜像登记，摘要与清单一致
- ✓ right 镜像登记，摘要与清单一致
- ✓ 计划条目 10 ＝ 独立差异表 10
- ✓ 自动采纳 4 ＝ 独立差异表 4
- ✓ 待裁决 6 ＝ 独立差异表 6
- ✓ 存在未决项时重演被拦截（409 unresolved）
- ✓ 过期版本裁决被拒绝（409 stale_revision）
- ✓ 同值重复裁决不推进版本（幂等）
- ✓ 重演通过（status=passed）
- ✓ 重演变更 6 ＝ 预期 6
- ✓ 同版本重演复用首次证据
- ✓ 重复封存结果稳定
- ✓ 凭证绑定三个来源
- ✓ 下载摘要与封存凭证一致
- ✓ 候选 integrity_check 通过
- ✓ 候选无外键违规
- ✓ 候选 entry 行数 24 ＝ 预期 24
- ✓ 候选 tag 行数 24 ＝ 预期 24
- ✓ 候选内容 SELECT note FROM entry WHERE id=1 → '祖本-1'
- ✓ 候选内容 SELECT qty FROM entry WHERE id=1 → 1
- ✓ 候选内容 SELECT note FROM entry WHERE id=4 → '祖本-4'
- ✓ 候选内容 SELECT note FROM entry WHERE id=5 → '左支-5'
- ✓ 候选内容 SELECT qty FROM entry WHERE id=6 → 1006
- ✓ 候选内容 SELECT note FROM entry WHERE id=6 → '祖本-6'
- ✓ 候选内容 SELECT COUNT(*) FROM entry WHERE id=24 → 0
- ✓ 候选内容 SELECT COUNT(*) FROM entry WHERE id=100 → 1
