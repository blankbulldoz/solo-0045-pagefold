-- 逐键对比：同一组复合键 (bench, slot) 在两份成果中的绑定数量。
-- 在 deliverables/reporting-pair 目录下执行：
--     sqlite3 :memory: < queries/key_by_key.sql
--
-- 预期输出恰为两行：
--     north | 1 | 0 | 2 | changed
--     south | 2 | 2 | 0 | changed
-- 其余两个键 north/2 与 south/1 不出现在结果里，说明并非所有键都在移动。
-- 如果报表认定的“异常键”不在这个列表里，或列表里出现了别的键，
-- 问题都在报表的读法，而不在合流结果。

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
merged AS (
    SELECT l.bench, l.slot, l.bindings AS left_bindings, r.bindings AS right_bindings
    FROM left_key AS l
    LEFT JOIN right_key AS r
           ON l.bench = r.bench AND l.slot = r.slot
    UNION ALL
    SELECT r.bench, r.slot, NULL, r.bindings
    FROM right_key AS r
    LEFT JOIN left_key AS l
           ON l.bench = r.bench AND l.slot = r.slot
    WHERE l.bench IS NULL
)
SELECT bench,
       slot,
       left_bindings,
       right_bindings,
       CASE
           WHEN left_bindings IS NULL THEN 'right-only'
           WHEN right_bindings IS NULL THEN 'left-only'
           ELSE 'changed'
       END AS state
FROM merged
WHERE left_bindings IS NULL
   OR right_bindings IS NULL
   OR left_bindings <> right_bindings
ORDER BY bench, slot;

DETACH DATABASE left_db;
DETACH DATABASE right_db;
