# Pagefold 接入：数据库兼容与升级手册

本手册写给把自己的 SQLite 镜像送进 Pagefold 做三方合流的团队。目标是回答两个问题：

1. 为什么我的文件**登记**不上（`POST /api/images` 被拒）；
2. 就算三份都登记成功，为什么**创建计划**或**重演**仍然失败，以及哪一类失败该改文件、哪一类必须改流程。

所有结论都来自对 `pagefold/` 源码的实查和在 Python 3.11 / SQLite 3.40.1 上复跑的结果。凡是本文写“会／不会”的地方，都附了可复跑的检查例。**不要**把本手册当成 SQLite 通用文档的替代：下面多处行为恰恰是一般文档容易讲错或不讲的边界。

---

## 0. 先跑检查，再读结论

仓库自带一个检查器，复用 Pagefold 自己的判定函数（`describe` / `check_database` / `read_image` / `validate_header`），因此它给出的结论与网页入口一致。

```sh
# 1) 单个镜像：走完 file → schema → integrity → rows 四个阶段，报首个拒绝点
python3 -m examples.compat_check path/to/image.sqlite

# 2) 三份镜像：比对能否创建计划（模式记录逐对象比对）
python3 -m examples.compat_check --compare base.sqlite left.sqlite right.sqlite

# 3) 不想看别人的库：先验证检查器自身判定是否成立
python3 -m examples.compat_check --selftest
```

退出码：`0` 通过；`1` 至少一份登记被拒；`2` 可各自登记但模式记录不一致（`schema_mismatch`）；`3` 文件读不了。`--json` 给机器读，`--quiet` 只出结论。

`--selftest` 会现场构造最小库并逐条断言下述规则（35 条），所以手册与代码不会各说各话。它当前输出 `自检：35/35 通过`。

---

## 1. 登记发生在哪四个阶段

`pagefold/images.py:register` 的顺序是固定的，**首个**不满足的阶段决定你看到的错误码：

| 顺序 | 阶段 | 代码位置 | 检查内容 | 典型代码 |
|---|---|---|---|---|
| 1 | file | `sqlite_image.py:validate_header` | 大小、magic、页大小/长度、文件头 18–19 字节 | `image_size` `image_header` `image_pages` `image_journal` |
| 2 | schema | `catalog.py:describe` | 只许普通表/索引、1–32 表、显式主键、主键 BINARY、无生成列、无 AUTOINCREMENT | `schema_objects` `table_count` `table_shape` `key_collation` `sequence` |
| 3 | integrity | `constraints.py:check_database` | `integrity_check` 与外键一致性 | `integrity` `foreign_key` |
| 4 | rows | `rows.py:read_image` | 行数上限、NULL 主键、重复主键、可编码值 | `row_limit` `null_key` `duplicate_key` `unsupported_value` `image_invalid` |

> **定位口诀**：错误里带 `file` 阶段的多半是“文件还没成为合格快照”；`schema` 阶段是“结构形状不合规”；`integrity` 是“库内部已坏或外键已断”；`rows` 是“结构没问题，但数据本身不能进登记表”。

网页 `POST /api/images` 把这四类都折成 HTTP `422`（`image_size` 为 `413`），JSON 里只给 `code` 和 `detail`，不给阶段名。检查器额外打印阶段名和“此前已通过”的阶段，便于区分。

---

## 2. 文件阶段：先做成“独立、已检查点的回滚日志文件”

`validate_header` 只看 100 字节头和文件长度，规则比一般文档严格：

- 必须是完整 SQLite 文件，`data[:16] == b"SQLite format 3\x00"`，长度 ≥ 100 且是页大小整数倍（`image_header` / `image_pages`）。
- 页大小取头部 16–17 字节，`0` 视为 65536；必须为 2 的幂且 512–65536（`image_pages`）。
- **头部 18–19 字节必须是 `0x01 0x01`**，否则报 `image_journal`（“请先完成检查点并保存为独立的 SQLite 文件”）。WAL 模式写出的文件这里是 `0x02 0x02`，**即使已经 checkpoint 也一样被拒**——很多团队以为“checkpoint 过就行”，这里会踩坑。
- 上限 4 MiB（`MAX_BYTES`），判定是严格大于，恰好等于通过。

这不是“SQLite 文件损坏”问题，而是**导出手法**问题。实测：

| 导出方式 | 头部 18–19 | 结果 |
|---|---|---|
| 回滚日志（默认 `DELETE`）库直接拷贝 | `0101` | 接受 |
| WAL 库 checkpoint 后、连接仍开 | `0202` | `image_journal` |
| WAL 库 checkpoint 后、连接已关 | `0202` | `image_journal` |
| 对 WAL 库 `VACUUM INTO 新文件` | `0101` | 接受 |
| 打开 WAL 库后 `conn.serialize()` | `0202` | `image_journal` |
| 对 `DELETE` 库 `deserialize`+`serialize` 往返 | `0101` | 接受 |

**建议导出配方**（都会得到 `0101`；本例已验证 Python 版，CLI 版是同一 `VACUUM` 操作）：

```sh
# A. 命令行（需本机安装 sqlite3）
sqlite3 input.sqlite "VACUUM INTO '/abs/out.sqlite'"

# B. Python：VACUUM INTO 生成回滚日志格式的新文件
python3 - <<'PY'
import sqlite3
src = sqlite3.connect("input.sqlite")
src.execute("VACUUM INTO '/abs/out.sqlite'")
src.close()
PY
```

复跑最小例（把任意库塞进去看头部两字节）：

```sh
python3 - <<'PY'
d = open("out.sqlite","rb").read()
print(d[18:20].hex())        # 期望 0101
PY
```

`serialize()` 不是安全导出：它保留源库的日志模式字节。

---

## 3. 模式阶段：登记要什么形状

### 3.1 只接受普通表和索引

`describe` 扫描 `main.sqlite_schema`（跳过 `sqlite_*`），任何对象不是 `table` 或 `index`，或表不是 `table_list` 里的 `main` 普通表，就 `schema_objects`。这覆盖：视图、触发器、虚拟表（FTS 等）。`sqlite_sequence` 的存在单独报 `sequence`（即 AUTOINCREMENT）。

```sh
# 复跑：视图 / 触发器 / FTS / AUTOINCREMENT 分别被拒
python3 -m examples.compat_check --selftest   # 覆盖视图、AUTOINCREMENT
```

实测：视图、触发器、FTS5 虚表 → `schema_objects`；`INTEGER PRIMARY KEY AUTOINCREMENT` → `sequence`（注意：**建表即产生** `sqlite_sequence`，即使一行没插也不行）。

### 3.2 每张表必须是显式主键，且所有列可写入

`catalog.py:relation` 用 `PRAGMA table_xinfo`：

- 没有任何 `pk` 列 → `table_shape`；
- 任何 `hidden` 列（生成列，`VIRTUAL` 或 `STORED`）→ `table_shape`；
- `UNIQUE` 不是主键：`CREATE TABLE t(a TEXT UNIQUE, ...)` 没有主键位，报 `table_shape`；
- `CREATE TABLE ... AS SELECT` 无主键，报 `table_shape`。

`WITHOUT ROWID`、`STRICT` 都接受（实测），普通索引、外键都接受。

### 3.3 主键必须是 BINARY 比较

`relation` 找 `index_list` 里 `origin == "pk"` 的索引，再查 `index_xinfo`，**任何**主键分量的 collation 不是 `BINARY` 就 `key_collation`。

实测被拒的写法：

```sql
CREATE TABLE t(a TEXT PRIMARY KEY COLLATE NOCASE, ...);
CREATE TABLE t(a TEXT COLLATE NOCASE PRIMARY KEY, ...);
CREATE TABLE t(a TEXT, v TEXT, PRIMARY KEY(a COLLATE NOCASE));
CREATE TABLE t(a TEXT, b TEXT COLLATE NOCASE, v TEXT, PRIMARY KEY(a,b));  -- 一个分量就够
CREATE TABLE t(a TEXT COLLATE RTRIM PRIMARY KEY, ...);
```

`COLLATE BINARY` 显式写可以；但**写出来会改变模式签名**（见第 4 节），所以除非三份都这么写，否则不要为了“保险”加它。

非主键列上的 `NOCASE` 不触发 `key_collation`——但可能在第 6 节变成重演失败，注意区分。

### 3.4 键列顺序取“声明顺序”

`key` 来自 `sorted((field[5], field[1]) for field in fields if field[5])`，即按 `pk` 序号（`PRAGMA table_xinfo` 第 6 列，从 1 开始）排序，而不是列在表里的位置。

- `PRIMARY KEY(b,a)` 且列顺序是 `a,b,v` → `key == ["b","a"]`。
- 这点很重要：GUI 工具常把复合主键显示成列顺序，别据此判断 Pagefold 的键序。

### 3.5 行数、大小、表数

- 1–32 张表（`table_count`）。
- **每份镜像总计** 20,000 行（`read_image` 递减 `remaining`），不是每表 20,000 行。实测 32 表 × 626 行 = 20,032 → `row_limit`；32 × 625 = 20,000 → 通过。
- 文件 ≤ 4 MiB。

---

## 4. 关键：模式签名是 `sqlite_schema` 文本，不是语义

`describe` 的签名是 `fingerprint(schemas)`，其中 `schemas` 是：

```sql
SELECT type,name,tbl_name,sql FROM main.sqlite_schema
WHERE substr(name,1,7) <> 'sqlite_' ORDER BY type,name
```

`fingerprint` 是对该结果做 `json.dumps(sort_keys=True,separators=(",",":"))` 再 SHA-256。也就是说：**签名等价于 `sqlite_schema.sql` 原文**，所有表/索引按 `(type,name)` 排序后逐字比较。

这解释了一类最常见的“登记成功、不能合流”：

> 三个团队各自 `CREATE TABLE`，哪怕逻辑上完全一样，只要 DDL 原文有一处不同，签名就不同，`POST /api/plans` 返回 `409 schema_mismatch`。

### 4.1 什么会改变签名（实测）

| 差异 | 改变签名？ |
|---|---|
| 空白：`t( a TEXT ...)` vs `t(a TEXT ...)` | **是** |
| 列类型名大小写：`text` vs `TEXT` | **是** |
| 列顺序不同 | **是** |
| 独立索引增删 | **是** |
| 只改索引名 | **是** |
| 唯一索引 vs 普通索引 | **是** |
| 部分索引 / 表达式索引 / `DESC` / `COLLATE` | **是** |
| `INTEGER` vs `INT`、`TEXT` vs `VARCHAR` | **是** |
| 显式 `COLLATE BINARY` | **是** |
| `WITHOUT ROWID` / `STRICT` 有无 | **是** |
| 表名大小写 `t` vs `T` | **是** |
| 表/索引名被引号包起来：`"t"` | **是** |
| 内联注释 `/* ... */`、`--` | **是** |
| `ALTER TABLE RENAME TO/COLUMN` 之后 | **是**（SQLite 会重写 DDL 文本） |
| `CREATE TABLE ... AS SELECT` 再改名 | **是** |

### 4.2 什么不会改变签名（实测）

SQLite 在写入 `sqlite_schema` 前只会规范化**语句头部**，其余文本原样保留：

- 开头的 `CREATE TABLE` / `CrEaTe TaBlE`（以及索引的 `CREATE INDEX` / `CREATE UNIQUE INDEX`）被规范成大写固定形式；但**其余关键字不动**——例如 `primary key` 写成小写就会留在原文里，改变签名；
- `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` 被去掉；
- 语句末尾的分号、首尾空白被去掉；
- 表/索引的**创建顺序**（签名按 `(type,name)` 排序，与创建先后无关）；
- `PRAGMA user_version`、`application_id`（不在 `sqlite_schema` 里）；
- `VACUUM INTO`、`deserialize`+`serialize` 往返（DDL 文本原样保留）；
- `conn.iterdump()` 往返（同样原样保留，实测）。

所以“大小写无所谓”只在**开头那几个词**成立。列名、类型名、`PRIMARY KEY` 等括号内文本的大小写和空白都会改变签名。

```sh
# 复跑：打印每份镜像的签名，以及对 DDL 的敏感度
python3 -m examples.compat_check --compare a.sqlite b.sqlite c.sqlite
```

### 4.3 结论：不要“各写各的 DDL”，要“同一份模式记录”

第 4.1 与 4.2 的边界，一般文档不会讲清；实践中只有一条稳妥规则：

> **三份镜像的 `sqlite_schema` 必须来自同一份 DDL 文本。**

推荐做法：

1. 把建表/索引 DDL 纳入版本库，三支都用**同一份**脚本建库；
2. 若分支库是演化来的，用 `ALTER TABLE` 改完后，**用原始 DDL 重建一次**再导出（重建后 DDL 文本才回到基线）；
3. 导出统一走 `VACUUM INTO`（第 2 节），它保留 DDL 文本、只改日志格式；
4. 交付前跑 `--compare`，看到 `presence`/`sql` 差异就停在原地，别去点“创建计划”。

> **注意**：`ALTER TABLE ... RENAME` 会把 DDL 改写成带引号的形式（例如 `CREATE TABLE "t"(...)`），即使语义不变也会破坏签名。这是“改文件”的场景，不是“改流程”的场景。

---

## 5. 行阶段：值与行身份的边界

### 5.1 行身份只看主键，且键有类型归一

`rows.py` 的 `key_atom` 对键值是：

- `float` 且 `is_integer()` 且在 `[-2^63, 2^63)` → **归一成整数** `["i", str(int(v))]`；
- 其余走 `atom`：`NULL→["n"]`、`int→["i",...]`、有限 `float→["r", hex]`、`str→["t",...]`、`bytes→["b", base64]`。

所以：

| 键值 | 身份 atom |
|---|---|
| `1` | `["i","1"]` |
| `1.0` | `["i","1"]`（与 `1` **同一行**） |
| `-0.0` | `["i","0"]`（与 `0` **同一行**） |
| `2^63-1`（整数） | `["i","9223372036854775807"]` |
| `float(2^63)` | `["r","0x1.0000000000000p+63"]`（**不**归一，超出上界） |
| `"1"` | `["t","1"]`（与整数 `1` **不同行**） |
| `b"1"` | `["b","MQ=="]`（三者都不同） |

**但非键单元格不归一**：`atom(1) != atom(1.0)`，`1.0` 保留为 `["r",...]`。这与键的处理不对称，是设计上的边界：

- 键：整数与精确等值实数同一身份；
- 值：整数与实数分别保留表示。

```sh
# 复跑键归一与存储类保留
python3 -m examples.compat_check --selftest   # 覆盖 i/r/t/b、-0.0、2**63
```

### 5.2 应用把“两个相等数字”存成不同类型时的真实后果

这是接入方最容易误判的地方。分两种：

**(a) 主键列。** 如果应用在左支绑定整数 `1`、右支绑定实数 `1.0`，Pagefold 视为**同一行**，于是按普通三方规则归类：

| 情景 | 归类 |
|---|---|
| 祖本 `[1,…]`、左支 `[1.0,…]`、右支 `[1,…]`（右支未动） | **自动选左支**（左边变了、右边等于祖本） |
| 祖本 `[1,old]`、左支 `[1.0,L]`、右支 `[1,R]` | **冲突项**，需裁决 |
| 祖本无此行、左支 `[1,L]`、右支 `[1.0,R]` | **冲突项**（两边都新增且不等），需裁决 |

关键是：只要两边的**整行 atom** 不完全相等（类型不同也算不等），且都不等于祖本，就是一个必须裁决的冲突项；只有类型差异、内容相同的情形会因“一边等于祖本”被自动采纳。实测第二、三行分别是 `automatic=None`，第一行是 `automatic="left"`，裁决后候选里 `typeof(k)` 随所选行变化（选左为 `real`，选右为 `integer`）。

**(b) 非键列。** 不做键归一，`1` 和 `1.0` 是不同的单元格值，同样会成为冲突项；裁决后按所选行原样写入。

**(c) 文本 vs 数值主键。** 在无亲和或 BLOB 亲和的主键列里，`1`、`"1"`、`b"1"` 是**三行不同的数据**：

```sh
python3 - <<'PY'
import sqlite3
db = sqlite3.connect(":memory:")
db.execute("CREATE TABLE t(k, v TEXT, PRIMARY KEY(k))")
for value in (1, "1", b"1"):
    db.execute("INSERT INTO t VALUES(?, 'x')", (value,))
db.commit()
print(db.execute("SELECT typeof(k), k FROM t ORDER BY k").fetchall())
PY
```

这会直接造出“看起来重复、其实三行”的数据，进 Pagefold 后就是三个键、三个计划项。

**(d) 列亲和会掩盖一部分问题。** 存储类由插入时的列亲和决定：

| 列声明 | 存 `1` | 存 `1.0` | 存 `"1"` |
|---|---|---|---|
| （无亲和） | integer 1 | real 1.0 | text '1' |
| `TEXT` | text '1' | text '1.0' | text '1' |
| `NUMERIC` / `INTEGER` | integer 1 | integer 1 | integer 1 |
| `REAL` | real 1.0 | real 1.0 | real 1.0 |
| `BLOB` | integer 1 | real 1.0 | text '1' |

因此：

- 有 `TEXT` 亲和的主键列，绑定整数 `1` 也会存成 `'1'`，三个团队“都存 1”但有的绑字符串，结果一致；
- 有 `NUMERIC`/`INTEGER` 亲和的主键列，`1.0` 存成整数 `1`；
- 只有**无亲和或 `BLOB`** 的主键列，才会让 `integer 1` 与 `real 1.0` 这类不同存储类**并存于同一表的不同键**（例如键 `1` 存成整数、键 `2.0` 存成实数，实测并存）；但**同一个数值** `1` 与 `1.0` 不会在同一张表里并存（SQLite 唯一索引判它们相等，见 (e)）。

**(e) SQLite 唯一索引把 `1` 和 `1.0` 判为相等。** 在无亲和主键列上，`INSERT 1` 之后再 `INSERT 1.0` 会 `UNIQUE constraint failed`。所以同一张表内部不会同时出现两种类型；**跨镜像**才会出现（左支应用绑 `1`，右支绑 `1.0`），这正是 Pagefold 需要按同一身份处理的原因。

> **给应用的约束**：主键列不要混用 Python `int` / `float` / `str` 绑定同一个逻辑值。若业务上“1”和 1 是同一实体，统一绑定一种类型；若是不同实体，用无亲和或 `BLOB` 亲和主键并接受“三行”的事实。Pagefold 会把混用表现为“行身份相同但内容冲突”，不会替你猜哪个对。

### 5.3 NULL 主键与非有限实数

- rowid 表上非整数主键列允许 NULL，`read_table` 在 rows 阶段报 `null_key`。`STRICT` 表或 `WITHOUT ROWID` 表在插入时就会因 NOT NULL 拒绝，轮不到 Pagefold。
- `PRAGMA table_xinfo` 里 `INTEGER PRIMARY KEY`（rowid 别名）不能为 NULL（插入 NULL 会自动分配 rowid），所以不会触发 `null_key`。
- 实数 `inf` 会存进库但 `atom` 只接受有限实数（`math.isfinite`），登记报 `unsupported_value`。`NaN` 在 SQLite 里存成 NULL，登记通过，但那已经是 NULL 语义。
- **rowid 别名主键只保留整数**。`CREATE TABLE t(k INTEGER PRIMARY KEY, ...)`（`PRAGMA index_list` 无 `origin='pk'` 项）在绑定 `1.5`、`"x"`、`b"\x01"` 时直接 `datatype mismatch`，绑定 `1.0` 或 `"1"` 会被强制成整数 `1`。同一个声明换成 `WITHOUT ROWID`、复合主键或 `INTEGER PRIMARY KEY DESC`（这些都会建立独立主键索引），就会按亲和规则保留 `real`/`text`/`blob`。这决定了你的“同值不同类型”问题会不会在入库前就被压平。

```sh
# 复跑：同一数值在不同主键写法下的存储类
python3 - <<'PY'
import sqlite3
for ddl in ("CREATE TABLE t(k INTEGER PRIMARY KEY, v TEXT)",
            "CREATE TABLE t(k INTEGER PRIMARY KEY DESC, v TEXT)",
            "CREATE TABLE t(k INTEGER, v TEXT, PRIMARY KEY(k))"):
    db = sqlite3.connect(":memory:")
    db.execute(ddl)
    try:
        db.execute("INSERT INTO t VALUES(1.5, 'y')")
        print(ddl, "->", db.execute("SELECT typeof(k) FROM t").fetchone()[0])
    except sqlite3.IntegrityError as exc:
        print(ddl, "->", type(exc).__name__, exc)
    db.close()
PY
```

---

## 6. 登记通过、却在重演阶段失败：这才是“不能三方合流”的另一半

即使三份签名一致、四阶段全过，`POST /api/plans/{id}/rehearse` 仍可能 `blocked`。`replay.py:rebuild` 在祖本副本上：

1. 计算 `expected`（目标行集合）；
2. `BEGIN`，对每个变更项：若祖本有该行，先 `DELETE ... WHERE <主键> COLLATE BINARY=?`；然后 `INSERT OR ABORT` 目标行；
3. `check_database`（`integrity_check` + `foreign_key_check`）；
4. 重新 `read_image` 与 `expected` 逐行比较，不等则 `row_conversion`。

按报错代码定位，分清“改文件/改数据”还是“改裁决/改流程”：

| 重演错误码 | 含义 | 通常处理 |
|---|---|---|
| `constraint` | `INSERT` 触发 UNIQUE / CHECK / NOT NULL | 改数据或改裁决；这是合法的三方合流冲突，不是文件问题 |
| `foreign_key` | 合并后外键断裂 | 改裁决（保留被引用的行）；若输入本身断裂，回登记阶段 |
| `integrity` | 写入后库损坏（罕见） | 查输入与触发器/索引表达式 |
| `row_conversion` | 写入后读回与目标不一致 | 亲和/类型边界，见 6.3 |
| `sqlite_replay` | 其他 `sqlite3.Error` | 看 detail |

### 6.1 两个各自合法的分支，合并后唯一键冲突

这是文档和演练素材里已演示的情况（`examples.make_images` 的 `stress-left` / `stress-right` 在 `bench` 的唯一约束上冲突）。外键版同样成立，用检查器自带的判定函数可完整复跑：

```sh
python3 - <<'PY'
import sys; sys.path.insert(0, ".")
import sqlite3
from examples.compat_check import inspect
from pagefold.catalog import describe
from pagefold.errors import Problem
from pagefold.merge import calculate
from pagefold.replay import amendments, rebuild
from pagefold.rows import read_image
from pagefold.sqlite_image import mounted

SCHEMA = ("CREATE TABLE p(a TEXT PRIMARY KEY);"
          "CREATE TABLE c(b TEXT PRIMARY KEY, a TEXT NOT NULL REFERENCES p(a));")

def build(statements):
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript(SCHEMA)
    for statement in statements:
        db.execute(statement)
    return db.serialize()

base  = build(["INSERT INTO p VALUES('p1')"])                # 只有父行
left  = build(["INSERT INTO p VALUES('p1')",                 # 加子行，引用 p1
               "INSERT INTO c VALUES('c1','p1')"])
right = build([])                                            # 删父行，空库也合法

for label, data in (("base", base), ("left", left), ("right", right)):
    print(label, "登记接受：", inspect(data)["accepted"])     # 三份都是 True

with mounted(base) as db: catalog = describe(db); snap = [read_image(db, catalog)]
for data in (left, right):
    with mounted(data) as db: snap.append(read_image(db, catalog))
items = calculate(*snap)
decisions = {item["id"]: "left" for item in items if item["automatic"] is None}
try:
    rebuild(base, catalog, amendments(items, decisions))
    print("重演通过")
except Problem as exc:
    print("重演被拒：", exc.code, "—", exc.detail)             # foreign_key
PY
```

输出会是三份“登记接受：True”，随后“重演被拒：foreign_key”。这就是“各自的库都合法，合起来却不合法”的典型。

用检查器可以复现“各自接受、合并不了”：

```sh
python3 -m examples.compat_check --compare base.sqlite left.sqlite right.sqlite
# 若只是模式一致但数据冲突，compare 会说“可以创建三方计划”，
# 重演冲突要在网页里 rehearse 才看得到。
```

> **要点**：`--compare` 只回答“模式记录是否一致”，不回答“重演是否通过”。后者依赖数据与裁决，请在网页里重演确认。

### 6.2 独立索引/非键列的 NOCASE 在重演时爆炸

登记只检查**主键** collation，不检查独立索引或非键列的 collation。但它们仍参与唯一约束：

```sql
CREATE TABLE t(k TEXT PRIMARY KEY, v TEXT COLLATE NOCASE UNIQUE);
CREATE INDEX ix ON t(v COLLATE NOCASE);           -- 非唯一，不拦
CREATE UNIQUE INDEX ix ON t(v COLLATE NOCASE);    -- 拦
CREATE UNIQUE INDEX ix ON t(lower(v));            -- 表达式索引，拦
```

实测：base 空、left 插 `('k1','Alpha')`、right 插 `('k2','alpha')`，两项各自合法，合并时 `UNIQUE constraint failed`（`constraint`）。

这属于**改文件**：若业务不要求大小写不敏感唯一，去掉 `NOCASE`/表达式索引后再导出；若要求，则接受“合法输入仍可能合并不了”，用裁决避开（例如让两支不要同时插入 NOCASE 相等的值）。

### 6.3 `row_conversion`：写入后读回不一致

`rebuild` 第 4 步会重读数据库并与目标逐行比较。因为 `atom` 保留存储类、而 `INSERT` 会经过列亲和，理论上可能出现“想写 `real 1.0`，读回 `integer 1`”。

**实测结论**：在 Pagefold 自己的读路径下，这个安全网不会因常见的亲和转换而误报。原因是 `read_image` 读到的目标行**已经是存储后的类型**（源镜像里存的是什么，计划项里就是什么），再插入同一列亲和会得到相同存储类。检查器内置了穷举：6 种亲和（无、`TEXT`、`NUMERIC`、`INTEGER`、`REAL`、`BLOB`）× 7 种值（整数、整值实数、小数实数、数值文本、文本、二进制、NULL）的键位与值位组合，每格真正走一遍写入与回读，未触发 `row_conversion`。

所以看到 `row_conversion` 应先怀疑：

- 触发器（但触发器在登记阶段就被 `schema_objects` 拒了，不会到这）；
- 自定义 collation 或函数（Pagefold 挂载时 `trusted_schema=OFF`）；
- 你手工改过状态库或候选文件。

复跑这段穷举（作为回归基线）：

```sh
python3 -m examples.compat_check --affinity
# 亲和穷举：196/196 组合重演通过，实际写入 328 行，跳过 98 组
# 未出现 row_conversion；登记—重演的类型往返在常见亲和下自洽。
```

“跳过”的格子全部来自**作为 rowid 别名**的单列整数主键（`PRAGMA index_list` 里没有 `origin='pk'` 的索引）：这种主键只保留整数，绑定小数实数、文本或二进制会在**建库**时 `datatype mismatch`，压根造不出合法镜像，所以不是 Pagefold 拒绝。反过来，只要存在独立的主键索引（复合键、`WITHOUT ROWID`、`INTEGER PRIMARY KEY DESC` 这类非别名写法），存储类就按第 5.2(d) 表的亲和规则保留。注意这与“声明类型是 `INTEGER` 的普通列”不同——只有 rowid 别名才有“只能存整数”的限制。

### 6.4 删除/修改按显式主键定位，隐式 rowid 会变

`apply_rows` 用 `WHERE <key> COLLATE BINARY=?` 定位，删除后重插。对 rowid 表（非 `INTEGER PRIMARY KEY`）：

> 被修改的行的**隐式 rowid 可能改变**。实测 `k='a'` 的 rowid 从 1 变 3。

如果你的下游用隐式 rowid 做关联，合流后这些关联会断。这不是 Pagefold 的 bug，是“以显式主键标识行”的直接后果。**建议**：下游不要依赖隐式 rowid；需要稳定标识就用显式 `INTEGER PRIMARY KEY` 或业务键。

（`INTEGER PRIMARY KEY` 的行，主键即 rowid，删除重插后仍保持原值，实测 `(5,5,'y')`。）

---

## 7. 出错时：改文件，还是保留原始模式记录？

这是本手册最想让你带走的分诊表。

| 现象 | 阶段/代码 | 该做什么 |
|---|---|---|
| WAL 格式被拒 | file / `image_journal` | **改导出方式**：`VACUUM INTO` |
| 页大小/长度坏 | file / `image_pages` | 文件损坏或截断，**重新导出** |
| 有视图/触发器/虚表 | schema / `schema_objects` | **改文件**：这些对象须移出镜像；若业务需要，另库存放 |
| AUTOINCREMENT | schema / `sequence` | **改文件**：去掉 AUTOINCREMENT，自管主键 |
| 复合主键缺 BINARY | schema / `key_collation` | **改文件**：去掉主键上的 NOCASE/RTRIM；但改完会变签名，见下 |
| 无显式主键 / 有生成列 | schema / `table_shape` | **改文件**：补显式主键、移走生成列 |
| 外键已断 | integrity / `foreign_key` | **改数据**（补父行或删子行）后重新导出 |
| NULL 主键 | rows / `null_key` | **改数据** |
| 非有限实数 | rows / `unsupported_value` | **改数据**：`inf` 换表示 |
| 三份 DDL 文本不同 | plan / `schema_mismatch` | **保留原始模式记录**：以基线 DDL 重建分支库，而不是让各方各改 DDL 去“对齐” |
| 重演唯一键/外键冲突 | rehearse / `constraint` `foreign_key` | **改裁决**或**改数据**；不是模式问题 |
| NOCASE 唯一索引冲突 | rehearse / `constraint` | **改文件**（去索引）或**改裁决** |
| 隐式 rowid 变化影响下游 | — | **改下游**：别依赖隐式 rowid |

### 7.1 为什么“保留原始模式记录”不能靠事后修补

签名是 DDL 原文。很多团队想“把三份 DDL 改成一样”——这是可行的，但**必须在建库时就用同一份 DDL**，或重建。事后用 `ALTER TABLE` 调整、用工具导出再导入，都会改写 `sqlite_schema.sql`（引号、空白、`IF NOT EXISTS` 的差异），反而制造新签名。实测：

```sh
python3 -m examples.compat_check --compare a.sqlite a.sqlite b.sqlite
# 会列出差异对象及其三份 DDL 原文
```

因此把“原始模式记录”当作**输入资产**保管：

1. 一份受版本控制的 DDL 脚本（表、索引、约束），三支共用；
2. 每次导出前用它重建空库再灌数据，或用 `VACUUM INTO` 复制结构；
3. 把 `--compare` 纳入交接检查。

### 7.2 什么时候“改文件”是错的

- 你为了对齐签名而手工 `ALTER TABLE RENAME` / 加 `COLLATE BINARY` / 调空白：**签名变了，问题没解决**。
- 你为了绕过 `constraint` 而删唯一索引：**削弱了原本的业务约束**，下游会收到不合规数据。
- 你把一个已有 AUTOINCREMENT 的主键改成普通 `INTEGER PRIMARY KEY`：主键分配语义变了，需领域确认。

正确顺序是：先按第 0 节定位阶段 → 判断属于“形状/格式”（改文件）还是“数据/裁决”（改裁决或改数据）还是“模式记录不一致”（统一的 DDL 与导出流程）→ 再动手。

---

## 8. 最短可复跑检查例

以下前两个直接调用本仓库检查器（在仓库根目录执行）；后两个只用 Python 标准库，可复制到任何环境。

### 8.1 我的文件能登记吗

```sh
python3 -m examples.compat_check path/to/image.sqlite
```

### 8.2 三份能不能合流（模式层面）

```sh
python3 -m examples.compat_check --compare base.sqlite left.sqlite right.sqlite
```

### 8.3 只用标准库，打印结构事实

```sh
python3 - <<'PY'
import sqlite3
db = sqlite3.connect("image.sqlite")
for (name,) in db.execute("SELECT name FROM sqlite_schema "
                          "WHERE type='table' AND substr(name,1,7)<>'sqlite_' ORDER BY name"):
    info = db.execute("SELECT * FROM pragma_table_xinfo(?)", (name,)).fetchall()
    key = [c[1] for c in sorted((c for c in info if c[5]), key=lambda c: c[5])]
    hidden = [c[1] for c in info if c[6]]
    print(f"{name}: key={key} hidden={hidden}")
    for idx in db.execute("SELECT * FROM pragma_index_list(?)", (name,)).fetchall():
        parts = db.execute("SELECT * FROM pragma_index_xinfo(?)", (idx[1],)).fetchall()
        cols = [(p[2], p[4]) for p in parts if p[5]]
        print(f"   {idx[3]:3} {idx[1]} unique={idx[2]} partial={idx[4]} {cols}")
db.close()
PY
```

对示例镜像 `quartz.sqlite`，输出应为：

```
bench: key=['code'] hidden=[]
   u   sqlite_autoindex_bench_2 unique=1 partial=0 [('label', 'BINARY')]
   pk  sqlite_autoindex_bench_1 unique=1 partial=0 [('code', 'BINARY')]
binding: key=['id'] hidden=[]
node: key=['bench', 'slot'] hidden=[]
   c   node_weight unique=0 partial=0 [('weight', 'BINARY')]
   pk  sqlite_autoindex_node_1 unique=1 partial=0 [('bench', 'BINARY'), ('slot', 'BINARY')]
```

这份输出里：`key` 是声明顺序（不是列位置）；`origin` 为 `pk` 的行就是主键索引，第二列若出现非 `BINARY` 的 collation 就是 `key_collation` 的根源；`origin` 为 `u`/`c` 的是独立索引；`hidden` 非空就是生成列；`partial=1` 或列名为 `None`（表达式索引）要留意第 6.2 节。

### 8.4 复算签名（与 Pagefold 完全一致）

```sh
python3 - <<'PY'
import hashlib, json, sqlite3
db = sqlite3.connect("image.sqlite")
rows = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema "
                  "WHERE substr(name,1,7)<>'sqlite_' ORDER BY type,name").fetchall()
rows = [list(r) for r in rows]
db.close()
print(hashlib.sha256(json.dumps(rows, ensure_ascii=True,
      separators=(",",":"), sort_keys=True).encode("ascii")).hexdigest())
PY
```

对示例镜像 `quartz.sqlite`，此脚本与 `pagefold.catalog.describe` 输出同一个签名（实测 `2850b20c…`）。

---

## 9. 升级/迁移检查单

把镜像接入或升级 Pagefold 时，按序打勾：

- [ ] 三份镜像来自**同一份 DDL 脚本**，已提交版本库；
- [ ] 导出用 `VACUUM INTO`，头部 18–19 字节为 `0101`；
- [ ] 每份 ≤ 4 MiB、≤ 32 表、**合计** ≤ 20,000 行；
- [ ] 每表显式主键，所有主键分量 BINARY，无生成列、无 AUTOINCREMENT、无视图/触发器/虚表；
- [ ] 无 NULL 主键、无非有限实数；
- [ ] 外键自洽（`PRAGMA foreign_key_check` 为空）；
- [ ] 主键列的应用绑定类型在支间一致（不混 `int` / `float` / `str`）；
- [ ] 确认 rowid 别名主键（`INTEGER PRIMARY KEY`）不会把你要保留的 `real`/`text` 压成整数；
- [ ] 下游不依赖隐式 rowid；
- [ ] 已跑 `python3 -m examples.compat_check --compare …` 且退出码为 `0`；
- [ ] 已在网页里对**当前版本**重演通过，再封存。

> 最后一条不能省：模式一致、登记通过，都只保证“能进计划”，不保证“能重演”。重演失败是正常的领域冲突，靠裁决解决，不靠改文件掩盖。

---

## 附：本文引用的代码位置

- `pagefold/sqlite_image.py` — `validate_header`、`mounted`、`MAX_BYTES`
- `pagefold/catalog.py` — `describe`（签名来源）、`relation`（主键/collation/外键）
- `pagefold/constraints.py` — `check_database`
- `pagefold/rows.py` — `read_table`、`key_atom`、`read_image`
- `pagefold/codec.py` — `atom`、`fingerprint`、`canonical`
- `pagefold/merge.py` — `calculate`、`selected`
- `pagefold/replay.py` — `amendments`、`apply_rows`、`rebuild`
- `pagefold/images.py` — `register`（四阶段顺序）
- `examples/compat_check.py` — 本手册的检查器与自检
