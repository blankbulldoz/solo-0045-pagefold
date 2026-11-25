-- 反例：只按 bench 一列连接 binding 的错误写法（用于复现报表误报）。
-- 在 deliverables/reporting-pair 目录下执行：
--     sqlite3 :memory: < queries/wrong_join_counterexample.sql
--
-- 这条查询故意漏掉复合键的第二列 slot，聚合时把同一 bench 下的绑定
-- 扇出到该 bench 的每个节点。两份成果会显得“未绑定节点数不同”：
--     left  | unbound=0 | multi=2
--     right | unbound=2 | multi=2
-- 而正确口径（见 grouped_history.sql）两库都是 unbound=2、multi=1。
-- 若报表对两库给出不同的未绑定/多重绑定汇总，就应先检查连接条件
-- 是否只用了复合键的一部分，而不是怀疑合流结果。

ATTACH DATABASE 'outcomes/decision-left.sqlite'  AS left_db;
ATTACH DATABASE 'outcomes/decision-right.sqlite' AS right_db;

WITH left_key AS (
    SELECT n.bench, n.slot, COUNT(b.id) AS bindings
    FROM left_db.node AS n
    LEFT JOIN left_db.binding AS b
           ON b.bench = n.bench               -- 缺陷：漏掉 AND b.slot = n.slot
    GROUP BY n.bench, n.slot
),
right_key AS (
    SELECT n.bench, n.slot, COUNT(b.id) AS bindings
    FROM right_db.node AS n
    LEFT JOIN right_db.binding AS b
           ON b.bench = n.bench               -- 缺陷：漏掉 AND b.slot = n.slot
    GROUP BY n.bench, n.slot
),
merged AS (
    SELECT 'left' AS outcome, * FROM left_key
    UNION ALL
    SELECT 'right', * FROM right_key
)
SELECT outcome,
       SUM(bindings = 0) AS unbound,
       SUM(bindings > 1) AS multi
FROM merged
GROUP BY outcome
ORDER BY outcome;

DETACH DATABASE left_db;
DETACH DATABASE right_db;
