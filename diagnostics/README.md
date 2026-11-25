# stress-left / stress-right 合流受阻诊断

本目录是对“值班人员用示例模块的 `stress-left`、`stress-right` 建计划，界面显示无待决，
重演却被挡、下载也拿不到”这一操作的可公开复核诊断材料。材料只新增诊断脚本与证据，
不修改 `pagefold` 包的任何代码。

- `stress_triage.py`：通过真实 HTTP 入口复现三个现场，内置断言，可重复运行。
- `evidence.json`：一次完整运行固化的证据（内容派生字段跨运行逐字节稳定，
  仅计划/重演记录的服务端 UUID 每次不同）。

## 一、可交付状态到底在哪里被改变

结论先行：**没有任何裁决或版本操作改变可交付状态。改变发生在重演落库阶段
（`pagefold/replay.py`），并经 `pagefold/attempts.py` 固化为“无候选的失败重演记录”，
随后由封存/下载入口按“不存在候选”拒绝。**

调用链与证据：

1. `pagefold/merge.py:9-22` 的三方合流只按**显式主键行身份**比对。两个 stress 镜像
   各自新增的 `bench` 行主键是 `stress-left`、`stress-right`（互不相同），且都是
   单边变化，因此两条都被**自动采纳**（`automatic=left/right`），计划
   `unresolved=0`、`revision=0`、`state=open`。界面“无待决”与接口事实一致。
2. 合流层不看二级唯一约束，也不看外键。两条自动项的 `label` 都是 `共享键值`，
   而 `bench.label` 是整表 `UNIQUE`。
3. `pagefold/replay.py:31-36` 向祖本副本插入第二行（主键 `stress-right`）时，
   SQLite 抛出 `UNIQUE constraint failed: bench.label`，被包装成
   `Problem("constraint", ...)`；事务随后回滚，**没有候选字节产出**。
4. `pagefold/attempts.py:35-42` 捕获该问题，写入一条 `candidate IS NULL` 的
   `attempts` 记录，结果为 `status=blocked`。同版本再次重演会原样返回同一条记录
   （证据中的 `attempt_is_reused=true`），证明状态不是偶发、也不会自愈。
5. 可交付闸门因此关闭：
   - `pagefold/releases.py:25-27` 封存要求当前版本存在非空候选，否则返回
     `rehearsal_required`（409）；
   - `pagefold/releases.py:10-15` 与 `pagefold/api.py:33-35` 在没有 `releases`
     记录时返回 `unpublished`（404），所以下载也拿不到。

即：**“无待决”只代表主键层面没有需要人工选择的冲突；可交付性由重演时的约束校验
单独决定，两者是不同环节。** 持久化状态改变的确切位置是 `replay.py` 的插入失败 →
`attempts.py` 写入空候选记录，而不是任何决策端点。

## 二、三个现场的构造与输入/最终行推导

三个现场的计划都满足 `unresolved=0`、全部为自动项、`revision=0`，区别只在约束形态
与输入数据。

### 现场 A —— 原样例（整表 UNIQUE，相撞）

输入（`pagefold/demo.py` 原样生成）：

| 镜像 | bench 新增行 |
| --- | --- |
| quartz（祖本） | 无 |
| stress-left（左支） | `('stress-left','共享键值')` |
| stress-right（右支） | `('stress-right','共享键值')` |

主键不同 → 两条自动采纳；`label` 相同且 `label TEXT NOT NULL UNIQUE` 覆盖全表 →
最终行无法同时存在。重演首个错误：
`constraint: bench [["t","stress-right"]]：UNIQUE constraint failed: bench.label`。

相撞的主键与值（证据 `scenes.A_full_unique.items`）：

- 主键 `stress-left`，条目指纹 `91a3c8f6…cbd48`，自动取左支；
- 主键 `stress-right`，条目指纹 `d8476d7d…54312`，自动取右支；
- 两行的相撞值都是 `label='共享键值'`。

### 现场 B —— 部分唯一索引谓词对照

模式只改 `bench`（其余表与索引保持原样，三份镜像的 `sqlite_schema` 仍精确匹配）：

```sql
CREATE TABLE bench(
  code TEXT PRIMARY KEY, label TEXT NOT NULL, shared INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE UNIQUE INDEX bench_shared_label ON bench(label) WHERE shared=1;
```

- **B1（两行都在谓词内）**：左支 `('stress-left','共享键值',1)`、
  右支 `('stress-right','共享键值',1)`。两行都满足 `shared=1`，部分索引同时收录，
  仍报同一个 `UNIQUE constraint failed: bench.label`。这说明现场 A 的冲突不是
  “整表 UNIQUE”特有，部分唯一索引在谓词命中时行为一致。
- **B2（其中一行落到谓词之外）**：仅把右支的 `shared` 改成 `0`，主键与 `label`
  完全不动。该行不再被索引收录，两行同值不再相撞，重演 `passed`，封存后可下载。
  最终库中的实际行（证据 `scenes.B2_partial_outside.persisted_bench_rows`）：
  `('stress-left','共享键值',1)` 与 `('stress-right','共享键值',0)` 同时存在，
  `foreign_key_check=[]`，下载件 SHA-256 与凭证 `candidate_digest` 一致。

B1→B2 的唯一输入差异是谓词列 `shared`，它直接决定持久化成败——这正面回答了
“改哪个输入会使持久化结果发生变化”。

### 现场 C —— 把首个错误换成真正的外键失败

模式与现场 A 完全相同，只改输入。祖本有
`binding(1,'north',1) → node('north',1)`：

- 左支：删除 `node('north',1)`，并在同一镜像内连带删除 `binding id=1`
  （单份镜像始终外键有效，登记不会被拒）；
- 右支：保留父行，新增 `binding(2,'north',1)`。

合流按主键自动采纳两侧，产生三个自动项（证据 `scenes.C_foreign_key.items`）：
删除 `binding 1`（取左）、新增 `binding 2`（取右）、删除 `node('north',1)`（取左）。
最终态里 `binding 2` 引用的父行已被采纳的删除操作移除 → 孤儿子行。插入阶段没有任何
唯一冲突，`pagefold/replay.py:54` 调用 `check_database`，由
`pagefold/constraints.py:7-11` 的 `PRAGMA foreign_key_check` 报出**真正的外键失败**：
`foreign_key: 外键检查失败：binding → node（约束 0）`。

## 三、三个现场的共同之处

1. **计划层都“无待决”**：差异行的主键互不重合，全部是单边变化或可自动判定的变化，
   `unresolved=0`、无人工裁决、`revision=0`。
2. **约束冲突都不发生在主键上，而发生在主键之外的关系上**：A/B1/B2 是二级唯一约束
   （整表 UNIQUE 或部分唯一索引），C 是跨表外键。合流算法（`merge.py`）只识别主键
   行身份，天然看不到这两类约束，所以冲突只会在重演写库后暴露。
3. **每份源镜像单独都合法**：登记时 `images.register` 已跑过完整性与外键检查；
   证据 `source_health` 对 12 份镜像逐一记录 `integrity_check=ok`、
   `foreign_key_check=[]`。问题只在“把三方自动采纳结果放进同一个库”时出现。
4. **失败都不产生候选**：三条 blocked 记录的 `candidate` 均为空，因此封存被
   `rehearsal_required` 拒绝、下载被 `unpublished` 拒绝。

## 四、哪项证据排除三个怀疑方向

- **排除“版本过期（stale revision）”**：所有计划始终 `revision=0`，重演携带的就是
  当前版本，服务端从未返回 `stale_revision`；B2 还主动用错误版本探测，确认该路径
  独立返回 409 `stale_revision`，与受阻现场无关。同版本重复重演返回同一条记录
  （`attempt_is_reused=true`），说明阻断是确定性内容问题，不是并发或版本错位。
- **排除“源库破损”**：全部输入镜像 SHA-256 寻址、登记成功，且证据中每份镜像的
  `PRAGMA integrity_check='ok'`、`PRAGMA foreign_key_check=[]`；模式记录三份精确
  匹配（同现场内 `schema` 数组逐字相同）。破损文件在登记入口就会被 422 拒绝，
  根本建不成计划。
- **排除“依赖处理顺序”**：`stress_triage.py` 的 `reverse_order_check` 直接调用重演
  引擎，把 `catalog.tables` 的迭代顺序整体反转后重放同一组操作。证据
  `order_independence` 显示现场 A 正序/逆序的首个错误都是
  `constraint … bench.label`，现场 C 正序/逆序都是
  `foreign_key … binding → node`，`same_first_error=true`。根因是最终行集合本身
  违反约束，与表的处理先后无关。

## 五、如何复核与判断“改哪个输入能改变持久化结果”

运行（脚本以隔离模式启动，仅监听本机临时端口，使用临时状态库，无第三方依赖）：

```sh
python3 -m compileall -q pagefold examples diagnostics
python3 -I diagnostics/stress_triage.py --out /tmp/evidence.json
```

脚本内置断言，任一结论不成立即非零退出；通过后打印四个现场的状态与首个错误。
值班人员可据此：

1. 用 `evidence.json` 中的条目指纹与主键/值，核对原样例相撞的是
   `stress-left` 与 `stress-right` 两个不同主键、同一 `label='共享键值'`；
2. 对照 B1/B2，确认把右支谓词列 `shared` 由 `1` 改为 `0`（主键和业务值不变）
   即可让持久化由 `blocked` 变 `passed` 并可下载；
3. 对照 A/C，确认在不改模式的前提下，改变输入行的增删组合会把首个错误从
   唯一约束失败切换为真正的外键失败；
4. 用 `source_health` 与 `order_independence` 独立复核源库健康与顺序无关性。

判断准则：**只要修改后的输入使三方自动采纳的最终行集合满足全部唯一（含部分索引
谓词）、检查与外键约束，重演就会产出非空候选，封存与下载随即开放；反之任一行仍
违反约束，`attempts` 就只记录空候选，可交付状态保持关闭。**
