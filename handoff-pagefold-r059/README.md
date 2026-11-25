# Pagefold r059 离线合流交接包

这是用仓库自带示例 **实际启动 HTTP 服务、上传三库、创建计划、保存右支裁决、重演、封存、下载并归档** 后整理出的可复制交接目录。接收方不需要启动 Pagefold 服务；只需 Python 3.11+ 标准库即可核对哈希、凭证、SQLite 约束和业务数据。

- 仓库提交：`baec633d240a688561f84830ec52cff0f74cd4d2`
- 分支：`task/r059-run-h33xpfbf`
- Plan ID：`d89aaf4a32e24ce28e6c2d9a086c91e0`
- 裁决版本：`revision=1`，选择 `right`
- 冲突项 ID：`3d9fecf2f8e0e841605f0984d234e878b149ba4a0758ce68606e4c9543b0c3bf`
- 封存候选 SHA-256：`c9d6dd3673ae91571c1cd52197a3b18bcb082b74a159de777c32f4cfcc6a3d24`
- 凭证文件 SHA-256：`6ae71e67c81dc40b81cbcf4640c8a72b9f4d6b66e43d2b91811ea3df3b93af94`

## 接收方三步核对

```sh
# 1. 进入复制后的目录
cd handoff-pagefold-r059

# 2. 推荐：完整离线核对（清单、凭证、来源角色、模式、SQLite、业务行、混件）
python3 -I tools/verify_offline.py

# 也可用标准 sha256sum 核对常规校验和
sha256sum -c SHA256SUMS

# 3. 直接从封存结果查具体复合键和 BLOB
python3 -I tools/query_row.py north 1
python3 -I tools/query_row.py north 1 --json
```

完整核对成功时最后一行是：

```text
结论: PASS — 交接包完整且凭证/业务证据一致
```

## 同事最需要知道的合并事实

1. **左支增加了一行**：`node` 表复合主键 `('south', 2)`，内容为 `note='左支新增'`、`payload=NULL`、`weight=4.0`。
2. **唯一冲突在 `node('north',1)`**：左支改了 `note='左支覆写'`；右支改了 `payload=x'ff00'` 和 `weight=2.25`。
3. **本次选择右支整行**：封存结果中该键是 `note='祖本节点'`、`payload=x'ff00'`、`weight=2.25`。因为 Pagefold 按整行裁决，不做单元格自动拼接，所以左支对该键的 `note` 覆写不保留。
4. **左支新增行没有被右支覆盖掉**：它是单边新增，自动采纳。封存库 `node` 有 3 行，而右支原库只有 2 行。
5. 右支的 `bench('south')` 更名为 `右支更名` 也自动保留。

详见 [`evidence/BUSINESS-EVIDENCE.md`](evidence/BUSINESS-EVIDENCE.md)。

## 成果、凭证与来源角色

| 角色 | 示例风味 | 文件 | 镜像 ID / 字节 SHA-256 |
|---|---|---|---|
| `base` 共同祖本 | `quartz` | [`sources/base-quartz.sqlite`](sources/base-quartz.sqlite) | `90438d5a76cd0744920d78acb009df1d19aa23baad0a7ed6775927b53e7a1a25` |
| `left` 左支 | `cerulean` | [`sources/left-cerulean.sqlite`](sources/left-cerulean.sqlite) | `ab1bceb83ee5599db0a06e4429977951f26378db082cbffa052f8215a14d6f75` |
| `right` 右支 | `umber` | [`sources/right-umber.sqlite`](sources/right-umber.sqlite) | `d6a547d0982d6150b30a8a78706c3db162c3d339c41ee918a718e46dce39a009` |

主要交付物：

- 封存 SQLite 成果：[`result/merged-r059-right-choice.sqlite`](result/merged-r059-right-choice.sqlite)
- Pagefold 凭证：[`certificate/pagefold-certificate.json`](certificate/pagefold-certificate.json)
- 来源角色映射：[`sources/SOURCE-ROLES.json`](sources/SOURCE-ROLES.json)
- 整包清单：[`handoff-manifest.json`](handoff-manifest.json)
- 标准校验和：[`SHA256SUMS`](SHA256SUMS)

凭证把三个来源角色、模式摘要、计划摘要、裁决摘要、版本和候选库摘要绑定在一起。它是摘要绑定凭证；Pagefold 当前没有声称第三方数字签名。若要防御有人同时重写整包和凭证，请通过另一可信渠道核对上面的 Plan ID、凭证 SHA-256 或候选 SHA-256。

## 怎么判别“同模式另一份文件”混进来了

同模式不等于同一份镜像。包内附带仓库生成的 `stress-left` 作为“同模式另一份文件”样本：

- 它和本次三库有相同模式摘要 `2850b20c…fb6ac`；
- 但字节 SHA-256 是 `6b7331fe…1662`，不是左支 `ab1bceb8…6f75`；
- 它还多了业务行 `bench('stress-left')`。

校验方式：

```sh
# 用包内样本实际演示“额外混入”和“顶替左支文件名”两种情形
python3 -I tools/verify_offline.py --mixup-self-test

# 检查接收方自己的任意来源目录：按 SHA-256 识别 base/left/right，
# 外来 SQLite 会分类为 foreign_same_schema 或 foreign_other_schema
python3 -I tools/verify_offline.py --scan-dir /path/to/source-mirror
```

实测输出已保存在 [`negative-tests/mixup-selftest.txt`](negative-tests/mixup-selftest.txt)；“额外文件落入整包”和“外来库顶替左支文件名”两种拒绝记录见 [`negative-tests/package-mixup-detection.txt`](negative-tests/package-mixup-detection.txt)。完整校验还会发现交接目录中的清单外文件；即使外来文件恰好也是同一套表结构，只要字节摘要不在凭证中，就不能被识别为受信任来源。

## 不用服务也能看的业务证据

- [`evidence/BUSINESS-EVIDENCE.md`](evidence/BUSINESS-EVIDENCE.md)：左支新增、右支选择、逐行合并矩阵。
- [`evidence/composite-key-blob-lookup.txt`](evidence/composite-key-blob-lookup.txt)：`('north',1)` 与 BLOB `ff00` 的查询回执。
- [`evidence/composite-key-blob-lookup.json`](evidence/composite-key-blob-lookup.json)：同一回执的 JSON。
- [`evidence/sealed-database.md`](evidence/sealed-database.md) / [`evidence/sealed-database.json`](evidence/sealed-database.json)：封存库全表导出。
- [`evidence/source-databases.md`](evidence/source-databases.md) / [`evidence/source-databases.json`](evidence/source-databases.json)：三份来源库全表导出。
- [`evidence/csv/`](evidence/csv/)：封存库各表 CSV，BLOB 以 `hex:` 明确标注。
- [`evidence/reconciliation.json`](evidence/reconciliation.json)：关键业务断言。

## 操作审计材料

[`audit/`](audit/) 保存真实 HTTP 工作流的请求/响应、计划、裁决、重演、封存、归档和服务端状态库备份：

- `audit/http-transcript.json`
- `audit/initial-plan-response.json`
- `audit/decision-response.json`
- `audit/stale-revision-response.json`
- `audit/idempotent-decision-response.json`
- `audit/attempt-revision-1.json`
- `audit/attempts.json`
- `audit/seal-response.json`
- `audit/archive-response.json`
- `audit/final-plan.json`
- `audit/pagefold-state-backup.sqlite`

## 目录约定

```text
handoff-pagefold-r059/
├── README.md
├── handoff-manifest.json      # 整包文件 SHA-256/SIZE 清单
├── SHA256SUMS                 # 可用 sha256sum -c 的常规校验和
├── certificate/               # Pagefold 封存凭证
├── result/                    # 封存后的独立 SQLite 成果
├── sources/                   # base/left/right 原始镜像与角色映射
├── evidence/                  # 不用服务也能读的业务证据/导出
├── audit/                     # HTTP 实录、API JSON、状态库备份
├── tools/                     # 离线校验、按键查询、清单生成脚本
└── negative-tests/            # 同模式外来库样本与混件判别实测
```
