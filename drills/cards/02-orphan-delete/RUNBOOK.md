# 演练卡 D2 · 删除致新增引用孤立：合法删除 × 合法新增 = 外键违规

> 自动生成的说明。卡内镜像与 `card.json` 是权威内容；改动请回到 `drills/spec.py` 后重跑
> `python3 -m drills.build`。

## 目的

- 祖本有 rack('north',2)；左支删除它，右支新增引用它的 lease 5，两边在各自镜像里都合法。
- 计划按主键把删除和新增当成两条互不相干的自动项，合流后 lease 5 的父行不存在。
- 外键检查在候选上发现孤立引用并阻断；这不是导入错误。

## 文件

| 角色 | 文件 | 大小 | SHA-256 | 行数 site/rack/lease |
|---|---|---|---|---|
| `base` | `quartz.sqlite` | 24576 B | `0dfc9893ba0d100e…` | 4 / 3 / 2 |
| `left` | `cerulean.sqlite` | 24576 B | `8d247a48f20a747e…` | 4 / 2 / 2 |
| `right` | `umber.sqlite` | 24576 B | `88a97225c72860da…` | 4 / 3 / 3 |

三份镜像的模式摘要均为 `3c00d67633f15f5a9d1f635d5b41d45f79d4d8ed13dd37c3814bff1c3c35887a`，各自通过 `integrity_check` 与外键检查。
卡内 `site('drill-d2', 'D2 孤立引用沙盘')` 是这张卡的识别行，
用于在共享状态库中区分卡别；它三边相同，不参与合流。

## 操作者步骤（可复跑）

前置：Python 3.11+、SQLite 3.37+；在仓库根目录执行 `python3 -m compileall -q pagefold drills`。
用一份**全新的**状态库启动服务（轮转时不要复用别人的状态）：

```sh
python3 -m pagefold --state /tmp/pagefold-02-orphan-delete.sqlite
```

另一终端先核对文件，再走真实 HTTP 入口（把 `DRILLS` 指向本卡目录）：

```sh
cd drills/cards/02-orphan-delete && sha256sum -c SHA256SUMS && cd -
DRILLS=drills/cards/02-orphan-delete
python3 -m drills.run_card 02-orphan-delete --origin http://127.0.0.1:8745
```

`run_card` 会依次：上传三份镜像（按字节摘要复用）、创建计划、读取计划、重演第 0 版；
通过则封存并下载候选再核对摘要，受组则核对下载接口 404。也可按 `RUNBOOK` 末尾的手工 curl
步骤逐条替代。

## 预期计划

- 差异项 **4** 条，全部为自动项，待裁决 **0** 条。
- 重演目标操作 **4** 行。
- 预期结局：**受组**（status=`blocked`），诊断码 `foreign_key`，详细信息含 lease、rack。

| 表 | 主键 | 处置 |
|---|---|---|
| `lease` | `[["i","5"]]` | 自动·右支 |
| `rack` | `[["t","north"],["i","2"]]` | 自动·左支 |
| `rack` | `[["t","south"],["i","1"]]` | 自动·右支 |
| `site` | `[["t","west"]]` | 自动·左支 |

## 预期合流产物行数

| 表 | 行数 |
|---|---|
| `lease` | 3 |
| `rack` | 2 |
| `site` | 4 |

## 核验与结论

- 这是本卡的正确答案：两份分支各自合法，合流产物在数据库约束上失败。
- 阻断发生在重演阶段；不要修改输入镜像来绕过，也不要封存受组候选。
- `GET .../download` 与 `.../certificate` 此时都应返回 404 `unpublished`。

也可由导师用独立服务跑完整核验：

```sh
python3 -m drills.verify 02-orphan-delete
```

## 手工 curl（不依赖辅助脚本）

```sh
ORIGIN=http://127.0.0.1:8745
for ROLE in base left right; do
  case $ROLE in base) FILE=quartz.sqlite;; left) FILE=cerulean.sqlite;; right) FILE=umber.sqlite;; esac
  curl -sS -X POST "$ORIGIN/api/images" -H 'Content-Type: application/octet-stream' \
       -H "X-Image-Name: D2-$ROLE" --data-binary "@$DRILLS/$FILE"
done
```

记下三个返回的 `id`，然后：

```sh
curl -sS -X POST "$ORIGIN/api/plans" -H 'Content-Type: application/json' \
     -d '{"base":"<base-id>","left":"<left-id>","right":"<right-id>"}'
curl -sS -X POST "$ORIGIN/api/plans/<plan-id>/rehearse" -H 'Content-Type: application/json' -d '{"revision":0}'
```

通过卡的后续封存/下载：

```sh
curl -sS -X POST "$ORIGIN/api/plans/<plan-id>/seal" -H 'Content-Type: application/json' -d '{"revision":0}'
curl -sS -o candidate.sqlite "$ORIGIN/api/plans/<plan-id>/download"
sha256sum candidate.sqlite   # 应等于上一步返回的 candidate_digest
```
