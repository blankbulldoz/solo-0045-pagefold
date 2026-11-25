# Pagefold 供应方合规/拒绝样件包

本包用于向供应方说明 Pagefold 接收独立 SQLite 镜像时的三类常见边界。每份坏样件
**只暴露一个问题**，并在相邻的 `fixed/` 目录提供可修正到接收成功的副本；
`bad/` 下的原始样件生成后不再改写，可随时复查（原件哈希见各 `ATTRIBUTES.md`
与 `manifest.json`）。

## 三个差别

| 场景 | 唯一问题 | 暴露环节 | 预期拒绝码 |
| --- | --- | --- | --- |
| `01-generated-column/` | 普通表与**生成列**的差别（生成列不可写入） | 上传镜像 | 422 `table_shape` |
| `02-wal-runtime-file/` | **运行中的 WAL 文件**与独立镜像的差别（文件头 `02 02` vs `01 01`） | 上传镜像 | 422 `image_journal` |
| `03-same-index-name/` | **索引同名不代表目录相同**（目录指纹比对完整 DDL） | 创建合流计划 | 409 `schema_mismatch` |

场景 3 的三份坏样件单独都能上传成功（201），只有在建计划时才被拒绝——
这刻意区分了“单份镜像合规”和“三方模式精确匹配”两个不同环节。

## 目录结构

```
supplier-kit/
├── README.md                 # 本说明
├── CHECKS.md                 # 按接收方操作顺序的短核对步骤
├── manifest.json             # 机器可读属性：路径、大小、SHA-256、目录指纹、预期结果
├── verify-result.json        # 最近一次真实 HTTP 实测记录
├── 01-generated-column/
│   ├── ATTRIBUTES.md         # 相邻属性说明
│   ├── bad/                  # 原始坏样件（勿改，供复查）
│   └── fixed/                # 修正副本（接收成功）
├── 02-wal-runtime-file/
│   ├── ATTRIBUTES.md
│   ├── bad/
│   └── fixed/
└── 03-same-index-name/
    ├── ATTRIBUTES.md
    ├── bad/                  # base / left / right 三份
    └── fixed/                # 修正后可建计划、重演、封存的三份
```

## 重新生成与自动实测（可选）

样件由仓库内脚本生成，脚本与数据分目录存放，运行时不依赖样件目录：

```sh
python3 -I tools/build_kit.py ./supplier-kit       # 重建整包（幂等，按字节去重）
python3 -I tools/verify_kit.py ./supplier-kit      # 起临时服务走真实 HTTP 全流程
```

`verify_kit.py` 会在临时状态库上启动真实 Pagefold 服务，依次完成上传、建计划、
重演、封存与下载校验，并把每一步 HTTP 状态与响应写入 `verify-result.json`，
三个场景全部通过时退出码为 0。

环境要求：Python 3.11+、带 `deserialize/serialize` 的 SQLite 3.37+（本包在
SQLite 3.40.1 下实测）。
