# 核对步骤（按接收方实际操作顺序）

以下步骤用真实 HTTP 入口核对。先启动服务（默认仅监听本机）：

```sh
python3 -m pagefold --state ./pagefold-state.sqlite
# Pagefold 已启动，访问 http://127.0.0.1:8745
```

约定 `BASE=http://127.0.0.1:8745`，上传均为 `application/octet-stream`，
标签经 `X-Image-Name` 传递。下文在 `supplier-kit/` 目录内执行。

## 第 1 步：上传单份镜像——场景 1、2 在此被拒

```sh
# 场景 1 坏样件：生成列 → 期望 HTTP 422，code=table_shape
curl -s -o - -w "\nHTTP %{http_code}\n" -X POST "$BASE/api/images" \
  -H "Content-Type: application/octet-stream" -H "X-Image-Name: gen-bad" \
  --data-binary @01-generated-column/bad/part-ledger-generated.sqlite

# 场景 1 修正副本 → 期望 HTTP 201，catalog 中 part_ledger 2 行
curl -s -o - -w "\nHTTP %{http_code}\n" -X POST "$BASE/api/images" \
  -H "Content-Type: application/octet-stream" -H "X-Image-Name: gen-fixed" \
  --data-binary @01-generated-column/fixed/part-ledger-plain.sqlite

# 场景 2 坏样件：WAL 运行文件 → 期望 HTTP 422，code=image_journal
curl -s -o - -w "\nHTTP %{http_code}\n" -X POST "$BASE/api/images" \
  -H "Content-Type: application/octet-stream" -H "X-Image-Name: wal-bad" \
  --data-binary @02-wal-runtime-file/bad/sensor-reading-wal.sqlite

# 场景 2 修正副本：独立镜像 → 期望 HTTP 201
curl -s -o - -w "\nHTTP %{http_code}\n" -X POST "$BASE/api/images" \
  -H "Content-Type: application/octet-stream" -H "X-Image-Name: wal-fixed" \
  --data-binary @02-wal-runtime-file/fixed/sensor-reading-standalone.sqlite
```

核对点：两个坏样件都在**上传阶段**被拒且只报各自那一个错误码；两个修正副本
均返回 201 和镜像目录。

## 第 2 步：三份镜像建合流计划——场景 3 在此被拒

```sh
# 2a. 三份坏样件逐一上传 → 均期望 HTTP 201（单份都合规），记下返回的 id
for role in base left right; do
  curl -s -X POST "$BASE/api/images" \
    -H "Content-Type: application/octet-stream" -H "X-Image-Name: idx-bad-$role" \
    --data-binary @03-same-index-name/bad/inventory-$role.sqlite
  echo
done

# 2b. 用三个 id 建计划 → 期望 HTTP 409，code=schema_mismatch
curl -s -o - -w "\nHTTP %{http_code}\n" -X POST "$BASE/api/plans" \
  -H "Content-Type: application/json" \
  -d '{"base":"<bad-base-id>","left":"<bad-left-id>","right":"<bad-right-id>"}'
```

核对点：同名索引 `stock_lookup` 在左支定义不同，单份上传查不出来，只有三方
目录指纹比对时才暴露为 `schema_mismatch`。

```sh
# 2c. 上传三份修正副本 → 均 201（内容一致时服务按字节复用同一 id）
for role in base left right; do
  curl -s -X POST "$BASE/api/images" \
    -H "Content-Type: application/octet-stream" -H "X-Image-Name: idx-fixed-$role" \
    --data-binary @03-same-index-name/fixed/inventory-$role.sqlite
  echo
done

# 2d. 用修正后的三个 id 建计划 → 期望 HTTP 201，unresolved=0、items 为空
curl -s -X POST "$BASE/api/plans" -H "Content-Type: application/json" \
  -d '{"base":"<fixed-id>","left":"<fixed-id>","right":"<fixed-id>"}'
```

## 第 3 步：重演、封存、下载——证明端到端接收成功

```sh
# 3a. 重演修正后的计划（无差异项，changed=0）→ 期望 status=passed
curl -s -X POST "$BASE/api/plans/<plan-id>/rehearse" \
  -H "Content-Type: application/json" -d '{"revision":0}'

# 3b. 封存 → 200；取凭证与下载文件，核对摘要一致
curl -s -X POST "$BASE/api/plans/<plan-id>/seal" \
  -H "Content-Type: application/json" -d '{"revision":0}'
curl -s "$BASE/api/plans/<plan-id>/certificate"
curl -s -o candidate.sqlite "$BASE/api/plans/<plan-id>/download"
sha256sum candidate.sqlite   # 应等于凭证里的 candidate_digest
```

## 一键自动核对

```sh
python3 -I tools/verify_kit.py ./supplier-kit
# 三行 PASS，退出码 0；逐步 HTTP 记录见 verify-result.json
```

## 不依赖服务的本地复查

```sh
# 场景 1：hidden=2 即生成列
sqlite3 01-generated-column/bad/part-ledger-generated.sqlite "PRAGMA table_xinfo(part_ledger);"
# 场景 2：文件头 0202（WAL）对照 0101（独立）
od -An -tx1 -j18 -N2 02-wal-runtime-file/bad/sensor-reading-wal.sqlite
od -An -tx1 -j18 -N2 02-wal-runtime-file/fixed/sensor-reading-standalone.sqlite
# 场景 3：同名索引的 DDL 差异
sqlite3 03-same-index-name/bad/inventory-left.sqlite \
  "SELECT sql FROM sqlite_schema WHERE name='stock_lookup';"
```
