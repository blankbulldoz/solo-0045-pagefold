# 演练卡 D1 · 唯一键碰撞：两份合法分支在合流处撞上 UNIQUE

> 自动生成的说明。卡内镜像与 `card.json` 是权威内容；改动请回到 `drills/spec.py` 后重跑
> `python3 -m drills.build`。

## 目的

- 左支新增 site('ridge','脊线')，右支新增 site('mesa','脊线')；两份输入各自合法。
- 两行主键不同，计划不会把它们列为冲突，合流后却同时满足不了 site.name 的 UNIQUE。
- 阻断发生在候选重演，不是导入；不得改输入去凑一个能通过的结果。

## 文件

| 角色 | 文件 | 大小 | SHA-256 | 行数 site/rack/lease |
|---|---|---|---|---|
| `base` | `quartz.sqlite` | 24576 B | `0863ef67e9816818…` | 4 / 3 / 2 |
| `left` | `cerulean.sqlite` | 24576 B | `39c5ccaef30ba362…` | 5 / 4 / 3 |
| `right` | `umber.sqlite` | 24576 B | `fceb94b1bd7b4e3c…` | 5 / 4 / 3 |

三份镜像的模式摘要均为 `3c00d67633f15f5a9d1f635d5b41d45f79d4d8ed13dd37c3814bff1c3c35887a`，各自通过 `integrity_check` 与外键检查。
卡内 `site('drill-d1', 'D1 唯一键沙盘')` 是这张卡的识别行，
用于在共享状态库中区分卡别；它三边相同，不参与合流。

## 操作者步骤（可复跑）

前置：Python 3.11+、SQLite 3.37+；在仓库根目录执行 `python3 -m compileall -q pagefold drills`。
用一份**全新的**状态库启动服务（轮转时不要复用别人的状态）：

```sh
python3 -m pagefold --state /tmp/pagefold-01-unique-collision.sqlite
```

另一终端先核对文件，再走真实 HTTP 入口（把 `DRILLS` 指向本卡目录）：

```sh
cd drills/cards/01-unique-collision && sha256sum -c SHA256SUMS && cd -
DRILLS=drills/cards/01-unique-collision
python3 -m drills.run_card 01-unique-collision --origin http://127.0.0.1:8745
```

`run_card` 会依次：上传三份镜像（按字节摘要复用）、创建计划、读取计划、重演第 0 版；
通过则封存并下载候选再核对摘要，受组则核对下载接口 404。也可按 `RUNBOOK` 末尾的手工 curl
步骤逐条替代。

## 预期计划

- 差异项 **7** 条，全部为自动项，待裁决 **0** 条。
- 重演目标操作 **7** 行。
- 预期结局：**受组**（status=`blocked`），诊断码 `constraint`，详细信息含 site、UNIQUE。

| 表 | 主键 | 处置 |
|---|---|---|
| `lease` | `[["i","3"]]` | 自动·左支 |
| `lease` | `[["i","4"]]` | 自动·右支 |
| `rack` | `[["t","mesa"],["i","1"]]` | 自动·右支 |
| `rack` | `[["t","ridge"],["i","1"]]` | 自动·左支 |
| `rack` | `[["t","south"],["i","1"]]` | 自动·左支 |
| `site` | `[["t","mesa"]]` | 自动·右支 |
| `site` | `[["t","ridge"]]` | 自动·左支 |

## 预期合流产物行数

| 表 | 行数 |
|---|---|
| `lease` | 4 |
| `rack` | 5 |
| `site` | 6 |

## 核验与结论

- 这是本卡的正确答案：两份分支各自合法，合流产物在数据库约束上失败。
- 阻断发生在重演阶段；不要修改输入镜像来绕过，也不要封存受组候选。
- `GET .../download` 与 `.../certificate` 此时都应返回 404 `unpublished`。

也可由导师用独立服务跑完整核验：

```sh
python3 -m drills.verify 01-unique-collision
```

## 手工 curl（不依赖辅助脚本）

```sh
ORIGIN=http://127.0.0.1:8745
for ROLE in base left right; do
  case $ROLE in base) FILE=quartz.sqlite;; left) FILE=cerulean.sqlite;; right) FILE=umber.sqlite;; esac
  curl -sS -X POST "$ORIGIN/api/images" -H 'Content-Type: application/octet-stream' \
       -H "X-Image-Name: D1-$ROLE" --data-binary "@$DRILLS/$FILE"
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
