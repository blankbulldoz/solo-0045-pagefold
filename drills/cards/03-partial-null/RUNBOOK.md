# 演练卡 D3 · 对照卡：复合外键部分为空不产生孤立，本来就应通过

> 自动生成的说明。卡内镜像与 `card.json` 是权威内容；改动请回到 `drills/spec.py` 后重跑
> `python3 -m drills.build`。

## 目的

- lease(site,slot) 参照 rack(site,slot)：任一列为 NULL 时 SQLite 按 MATCH SIMPLE 跳过检查。
- 三行部分为空（7、8、9）都合法保留，候选外键检查为空——它们不是孤立。
- 本卡不裁决、不修数据，rev 0 直接重演通过；若它被阻断，问题在环境而非数据。

## 文件

| 角色 | 文件 | 大小 | SHA-256 | 行数 site/rack/lease |
|---|---|---|---|---|
| `base` | `quartz.sqlite` | 24576 B | `9362a4c46e82685f…` | 4 / 3 / 2 |
| `left` | `cerulean.sqlite` | 24576 B | `88f3fcc818e09f92…` | 4 / 3 / 4 |
| `right` | `umber.sqlite` | 24576 B | `920fb759aa869815…` | 4 / 3 / 4 |

三份镜像的模式摘要均为 `3c00d67633f15f5a9d1f635d5b41d45f79d4d8ed13dd37c3814bff1c3c35887a`，各自通过 `integrity_check` 与外键检查。
卡内 `site('drill-d3', 'D3 部分为空沙盘')` 是这张卡的识别行，
用于在共享状态库中区分卡别；它三边相同，不参与合流。

## 操作者步骤（可复跑）

前置：Python 3.11+、SQLite 3.37+；在仓库根目录执行 `python3 -m compileall -q pagefold drills`。
用一份**全新的**状态库启动服务（轮转时不要复用别人的状态）：

```sh
python3 -m pagefold --state /tmp/pagefold-03-partial-null.sqlite
```

另一终端先核对文件，再走真实 HTTP 入口（把 `DRILLS` 指向本卡目录）：

```sh
cd drills/cards/03-partial-null && sha256sum -c SHA256SUMS && cd -
DRILLS=drills/cards/03-partial-null
python3 -m drills.run_card 03-partial-null --origin http://127.0.0.1:8745
```

`run_card` 会依次：上传三份镜像（按字节摘要复用）、创建计划、读取计划、重演第 0 版；
通过则封存并下载候选再核对摘要，受组则核对下载接口 404。也可按 `RUNBOOK` 末尾的手工 curl
步骤逐条替代。

## 预期计划

- 差异项 **5** 条，全部为自动项，待裁决 **0** 条。
- 重演目标操作 **5** 行。
- 预期结局：**通过**（status=`passed`），可封存并下载候选，行摘要 `2bf2de7050b10d84…`。

| 表 | 主键 | 处置 |
|---|---|---|
| `lease` | `[["i","7"]]` | 自动·左支 |
| `lease` | `[["i","8"]]` | 自动·左支 |
| `lease` | `[["i","9"]]` | 自动·右支 |
| `rack` | `[["t","north"],["i","1"]]` | 自动·左支 |
| `site` | `[["t","west"]]` | 自动·右支 |

## 预期合流产物行数

| 表 | 行数 |
|---|---|
| `lease` | 5 |
| `rack` | 3 |
| `site` | 4 |

## 核验与结论

- 这是本卡本来就应通过的结果；若它被阻断，先检查环境（Python/SQLite 版本、状态库是否复用错误）。
- 封存后下载的 SQLite 应能独立打开，`PRAGMA integrity_check` 为 `ok`，`foreign_key_check` 为空。

也可由导师用独立服务跑完整核验：

```sh
python3 -m drills.verify 03-partial-null
```

## 手工 curl（不依赖辅助脚本）

```sh
ORIGIN=http://127.0.0.1:8745
for ROLE in base left right; do
  case $ROLE in base) FILE=quartz.sqlite;; left) FILE=cerulean.sqlite;; right) FILE=umber.sqlite;; esac
  curl -sS -X POST "$ORIGIN/api/images" -H 'Content-Type: application/octet-stream' \
       -H "X-Image-Name: D3-$ROLE" --data-binary "@$DRILLS/$FILE"
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
