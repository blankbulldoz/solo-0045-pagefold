# 类型化键与受控列身份

此重构把“序列化后的键”从内部表示降级为边界格式。改造前，镜像查行、三方比较
和重演三处各自处理同一种 JSON 文本（`[["t","north"],["i","1"]]`）：`rows.py`
生成它，`merge.py` 用 `json.loads` 再解析回来，`images.table_page` 再解析一次，
`replay.py` 用 `canonical(item["key"])` 拼回去。改造后，应用内部流动的是
`Value` / `Key` / `Row` 值对象和 `Catalog` / `Table` / `Column` 结构，序列化只在
应用边界发生。

## 分层

| 层 | 模块 | 内部类型 | 落盘/出网形态 |
| --- | --- | --- | --- |
| SQLite 读出 | `rows.scan`、`rows.scan_image` | `{Key: Row}` | — |
| 结构目录 | `catalog.describe`、`catalog.relation` | `Catalog` / `Table` / `Column` / `ForeignKey` | `Catalog.record()` |
| 三方比较 | `merge.calculate`、`merge.selected` | `PlanItem`（持有 `Key`/`Row`） | `PlanItem.record()` |
| 计划与裁决 | `plans.plan_content`、`decisions.resolve` | `PlanItem` + 决策映射 | `items` / `decisions` 文本列 |
| 页面 | `images.table_page`、`plan_views`、`image_views` | 消费 `record()` 结果 | HTML |
| 重演 | `replay.amendments`、`replay.rebuild` | 直接消费 `PlanItem` | 候选字节 |

`pagefold/keys.py` 只依赖 `codec`，不 import SQLite；`pagefold/rows.py` 是唯一
把 `sqlite3` 游标元组转成 `Key`/`Row` 的地方；`pagefold/catalog.py` 是唯一读
`PRAGMA` 并决定列名与主键位置的地方。

## 应用边界上的转换

1. **SQLite → 应用**：`rows.scan` 用 `Table.key_positions()` 取主键列位置，
   `Key.of` 把游标值分类成带类型标签的 `Value`；`Row.of` 对整行做同样的事。
   `Value.key_of` 在此处把精确等值的整数型实数折叠成整数身份（`1` 与 `1.0`
   同键），但 `Row` 保留单元格自身的类型标签（`i` 仍是 `i`，`r` 仍是 `r`）。
2. **应用 → SQLite**：只在 `replay.apply_rows` 的 `execute` 调用点，通过
   `Value.sqlite()` / `unpack` 还原成可绑定值；列名与主键谓词来自 `Table`，
   不再由各调用方自行 `ident()` 拼接。
3. **应用 → 持久化**：`plans.create_plan` 写 `[item.record() for item in items]`；
   `images.register` 写 `catalog.record()`；`decisions.resolve` 写决策映射。
   三者的 JSON 形状与改造前完全一致。
4. **持久化 → 应用**：`plans.plan_content` 用 `PlanItem.from_record` 还原，
   `images.image_catalog` 用 `Catalog.from_record` 还原。这是读侧唯一的重建点。
5. **应用 → HTTP/HTML**：`plans.read_plan` 用 `item.record()` 组织响应；
   `images.table_page` 用 `key.atoms()` / `row.atoms()`；视图层只消费这些记录，
   不再接触 `Key`/`Row`。

## 必须保持的既有约束

- **冲突标识**：`PlanItem.id == key.item_id(table) == fingerprint([table, key.atoms()])`。
  `Key.token` 与改造前的 `canonical(key)` 相同，因此已保存计划里的 `id`、计划
  摘要和凭证摘要逐字节稳定。实测旧状态库的计划项标识、`items` 文本、冲突标识
  均未被改写（见“验证”）。
- **offset 排序**：行窗口仍按 `Key.token` 的字符串序排序（`sorted(keys, key=lambda k: k.token)`），
  与改造前 `sorted(rows)` 的结果一致——包括 `ord` 表里 `1,10,11,...,2,20` 的
  字典序，而不是数值序。
- **类型标签**：`Value.atom()` 输出与 `codec.atom` 完全相同的 `["i"/"r"/"t"/"b"/"n", ...]`；
  空 BLOB 为 `["b",""]`，与空文本 `["t",""]` 区分；`0.0`/`-0.0` 的十六进制不同，
  `Value` 以 payload 文本比较，故二者不相等。
- **数值同键**：`Key.of([1])` 与 `Key.of([1.0])` 相等且同哈希，去重与比较都合流为一行。
- **复合外键**：`ForeignKey` 保留 `id`/`seq` 顺序，`Table.record()` 输出原始八列
  列表；`child` 表的 `(code, slot)` 复合引用在 `foreign_key_list` 中的两行次序不变。
- **空 BLOB 主键**：`Key.has_null` 只把 `n` 视为非法；`b""` 是合法主键值，
  在 `mix` 表的 `("empty", b"")` 行上保持为独立身份。

## 对照结果

`docs/typed-keys-snapshot.json` 由 `examples/typed_keys.py` 生成，只走公开 HTTP
入口，覆盖：

- 四张表的目录（列序、主键顺序、复合外键）；三份镜像的 `image_id`；
- 每张表所有 offset 窗口的键/值与排序；
- 9 个计划项（其中 1 个冲突：`mix` 主键 `("blobkey", b"\x00\xff")`）；`plan_digest`；
- 一次裁决（`right`）、重演（`changed=9`）与封存凭证；
- 封存 SQLite 的模式、全部行、`integrity_check` 与 `foreign_key_check`。

用同一脚本对改造前（冻结副本）和改造后各跑一次，两份 JSON 完全相同：

```
diff <(json.tool before.json) <(json.tool after.json)   # 无差异
plan_digest      = e845ef81c426eb060b4ad5260f97d8cb08a244859c9e90131a75542c7f076970
candidate_digest = c84da851c554f718d37efe655d177ca1ddddfcc981063ed05d35bffb0dae7c97
```

代表性行（`mix` 表左支）：

```
key    = [["t","num"], ["r","0x1.0000000000000p+0"]]   # 键作为 real 存储，但身份 = 整数 1
values = [["t","num"], ["r","0x1.0000000000000p+0"], ["t","left-num"], ["b","AQ=="], ["r","0x1.8000000000000p+0"]]
```

同一行在改造前以字符串键 `'[["t","num"],["r","0x1.0000000000000p+0"]]'` 存在于 `dict`，
在改造后以 `Key(values=(Value('t','num'), Value('r','0x1.0000000000000p+0')), token=...)`
存在；`token` 仍用于排序与去重，`atoms()` 仍输出同一 JSON。

## 验证

```sh
python3 -m compileall -q pagefold examples
python3 -m examples.walkthrough all
python3 -m examples.typed_keys --output /tmp/after.json
```

另外验证了跨版本状态库兼容：改造前写入的计划由改造后读取、裁决、重演、封存成功，
且 `items` 文本与计划项标识不变；反向亦然。重演受唯一约束阻挡时的诊断文本也逐字相同
（`bench [["t","stress-right"]]：UNIQUE constraint failed: bench.label`）。

## 有意不做的事

- `codec.atom`/`unpack` 仍是唯一的字节级编码实现，`keys.Value` 在其上做类型化包装，
  不复制 tag 到 payload 的映射规则。
- 计划、裁决、凭证的持久格式不变，因此没有迁移步骤；`Catalog.from_record` 容忍
  `rows` 字段缺失，旧的只含结构字段的目录记录仍可读。
- `PlanItem.automatic`、`selected()` 的语义未变；“自动项优先于显式裁决”的既有行为保留。

## 附：差分抽样

除固定快照外，另用确定性随机种子生成 12 份覆盖多种列声明类型、复合主键、
`WITHOUT ROWID`、空 BLOB / 空文本 / 全 NULL 候选主键的镜像，对改造前后分别调用
`catalog.describe` + `scan_image`/`read_image`，把目录记录和每张表按键排序的
`(key, row)` 对统一序列化后比较：结果逐字节相同。其中 7 份因含 NULL 主键被
同一诊断拒绝（`表 tX 含有 NULL 主键。`），其余 5 份的目录与行也完全一致。
