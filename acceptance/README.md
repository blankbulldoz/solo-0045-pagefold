# Pagefold 离线验收

接收合流成果的一方访问不到 Pagefold 服务时,用手边的封存材料离线核对成果。
`verify.py` 是独立实现(只用 Python 3.11+ 标准库,不导入 pagefold 包、不访问服务),
凭证里的每一项摘要都由它从材料重新推导,核对不依赖任何在线状态。

## 材料与可达层级

结论按手边材料的完整程度分层说明,缺少的材料会在报告中明确标出:

| 手边材料 | 可达层级 | 验收结论 |
| --- | --- | --- |
| 凭证 + 成果 | 层 1 | **字节匹配**:成果尺寸与 SHA-256 对得上凭证 `candidate_size`/`candidate_digest`,镜像健康 |
| + 三份输入 | 层 2 | **来源吻合**:三份输入 SHA-256 分别对得上凭证 `sources`,模式签名对得上 `schema` |
| + 裁决记录 | 层 3 | **实际行重演**:由输入重算计划(`plan_digest`)、按裁决重演(`decision_digest`),候选与凭证逐字节一致 |
| 成果经 SQLite 整理(字节不同) | 层 3 | **内容等价**:字节摘要对不上,但模式签名一致、逐表行内容与重演候选一致 |

层 3 在成果字节与凭证不同时自动改为逻辑比对,报告 `comparison` 一节给出可复核的
逐表结果(行数、逐表内容摘要、行级差异),对应"同一逻辑成果经过 SQLite 整理"的情形。

## 用法

```sh
# 验收包目录(自动识别 certificate.json、deliverable.sqlite、inputs/、adjudication.json)
python3 -I acceptance/verify.py --pack acceptance/pack

# 或按实际材料显式给出;只给凭证与成果时结论为字节匹配
python3 -I acceptance/verify.py --certificate certificate.json --deliverable deliverable.sqlite \
    [--base base.sqlite --left left.sqlite --right right.sqlite] [--adjudication adjudication.json] \
    [--json report.json]
```

退出码:0 验收通过;1 不予验收;2 材料缺失或无法读取。`--json` 写出的报告
(`pagefold-acceptance-report-v1`)逐条记录每项检查的期望值与实际值,可独立复核。

## 篡改如何被重新核对出来

- **左右支调包**:只换输入文件时,层 2 的 `inputs.left/right.digest` 与凭证 `sources` 不符;
  连凭证 `sources` 一起改时,层 2 通过,但层 3 由输入重算的三方计划与凭证 `plan_digest` 不符,
  重演候选也与 `candidate_digest` 不符。
- **改库后仅更新凭证候选摘要**:层 1 字节匹配会通过(摘要被同步改掉了),
  但层 3 由来源与裁决重演的候选摘要与凭证不符,且成果与重演候选的逐行比对给出具体差异行。

## 复现证据

```sh
python3 -m acceptance.build_pack      # 走真实 HTTP 工作流封存成果,生成 acceptance/pack/
python3 -m acceptance.run_acceptance  # 三次验收 + 三组篡改对照,证据写入 acceptance/evidence/
```

`run_acceptance` 对同一逻辑成果跑三次离线验收(仅凭证与成果、完整材料、整理后的
字节不同副本)并跑三组篡改对照,每个场景的完整报告在 `acceptance/evidence/*.report.json`。
重新生成时计划标识会变(每次新建计划),但输入、成果与各摘要摘要是确定性的。
