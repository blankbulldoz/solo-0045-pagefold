# 交付物：node 示例可登记 SQLite 镜像与两种读法对照

入口文档：[`docs/对照说明.md`](docs/对照说明.md)（含逐键定位表、比较归类规则与复算命令）。

## 目录

```
deliverables/
├── data/
│   ├── node-reference.sqlite     # 数据库本体（可登记镜像，见下方 git 提示）
│   ├── pagefold-export.json      # 读法 A：Pagefold 带类型整库 JSON
│   ├── pagefold-rows.jsonl       # 读法 A：逐行键/值原子
│   ├── sqlite-view.jsonl         # 读法 B：SQLite 直连逐行值 + typeof + JS 精度告警
│   ├── sqlite-equality.md        # 读法 B：等值/比较归类真实查询结果
│   └── cross-check-report.json   # 两种读法交叉核查结论（8 项全过）
├── docs/
│   └── 对照说明.md
└── scripts/
    ├── build_mirror.py           # 由 pagefold/demo.py 母版构建镜像（确定性输出）
    ├── export_pagefold.py        # 读法 A 导出（复用 pagefold 登记读取链路）
    ├── export_sqlite_view.py     # 读法 B 导出（sqlite3 直连 + typeof）
    ├── cross_check.py            # 交叉核查，失败时退出码非 0
    └── js_safe_integer_demo.js   # 前端 JS Number 精度风险演示（node 运行）
```

## 一键复算

```sh
python3 -I deliverables/scripts/build_mirror.py deliverables/data/node-reference.sqlite
python3 -I deliverables/scripts/export_pagefold.py deliverables/data/node-reference.sqlite deliverables/data
python3 -I deliverables/scripts/export_sqlite_view.py deliverables/data/node-reference.sqlite deliverables/data
python3 -I deliverables/scripts/cross_check.py \
  deliverables/data/node-reference.sqlite \
  deliverables/data/pagefold-rows.jsonl \
  deliverables/data/cross-check-report.json
node deliverables/scripts/js_safe_integer_demo.js
```

## git 跟踪提示

仓库根 `.gitignore` 含 `*.sqlite`，因此 `node-reference.sqlite` 默认不会被普通 `git add`
纳入版本控制。文件本身已在工作区交付且校验稳定（SHA-256
`fa308fcd4cc466c50ff732a2fa9e12efe99d2ff7bce4d127ba388e0f843baa6a`）。若需要随提交分发，
使用 `git add -f deliverables/data/node-reference.sqlite`，或调整忽略规则；也可随时用
`build_mirror.py` 按相同字节确定性重建。
