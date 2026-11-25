-- 分组统计：把每个复合键的绑定数折成直方图，再对照两份成果。
-- 在 deliverables/reporting-pair 目录下执行：
--     sqlite3 :memory: < queries/grouped_history.sql
--
-- 预期两库都输出同样的三行（0 个绑定 / 1 个绑定 / 2 个绑定 各有几个键）：
--     left  | 0 | 2
--     left  | 1 | 1
--     left  | 2 | 1
--     right | 0 | 2
--     right | 1 | 1
--     right | 2 | 1
--
-- “2 个绑定”的键在各库落在不同的 (bench, slot) 上，但每个桶的计数完全相同。
-- 因此仅凭分组直方图区分不出这两份成果；报表若把它当作正常/异常的判据，
-- 会得到同样的结论而漏报。必须配合逐键查询才能定位到复合键。

ATTACH DATABASE 'outcomes/decision-left.sqlite'  AS left_db;
ATTACH DATABASE 'outcomes/decision-right.sqlite' AS right_db;

WITH left_key AS (
    SELECT n.bench, n.slot, COUNT(b.id) AS bindings
    FROM left_db.node AS n
    LEFT JOIN left_db.binding AS b
           ON b.bench = n.bench AND b.slot = n.slot
    GROUP BY n.bench, n.slot
),
right_key AS (
    SELECT n.bench, n.slot, COUNT(b.id) AS bindings
    FROM right_db.node AS n
    LEFT JOIN right_db.binding AS b
           ON b.bench = n.bench AND b.slot = n.slot
    GROUP BY n.bench, n.slot
),
histogram AS (
    SELECT 'left' AS outcome, bindings, COUNT(*) AS keys FROM left_key GROUP BY bindings
    UNION ALL
    SELECT 'right', bindings, COUNT(*) FROM right_key GROUP BY bindings
)
SELECT outcome, bindings, keys
FROM histogram
ORDER BY outcome, bindings;

DETACH DATABASE left_db;
DETACH DATABASE right_db;
