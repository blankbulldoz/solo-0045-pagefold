# SQLite 比较归类实测（node-reference.sqlite）

下列结果全部由对交付库的真实查询产生。注意 SQLite 动态类型与亲和规则：

## 1. 整数与精确等值的实数在比较时归为同一值

```
SELECT 3 = 3.0;                                  -> 1
typeof(3)=integer  typeof(3.0)=real
```

type_probe 中 num-int(sample=3, integer) 与 num-real(sample=3.0, real)：

```
  num-int   = num-int   -> 1  (integer vs integer)
  num-int   = num-real  -> 1  (integer vs real)
  num-real  = num-int   -> 1  (real vs integer)
  num-real  = num-real  -> 1  (real vs real)
```

GROUP BY 同样把两者归并（一个比较类，两行）：

```
  值=3      typeof=integer  组内行数=2
```

## 2. 文本与 BLOB 各自独立，即便字节/显示相同也不相等

```
SELECT '3' = x'33';        -> 0   -- text vs blob
SELECT '' = x'';           -> 0   -- 空文本 vs 空 BLOB
SELECT '3' = 3;            -> 0   -- text vs integer
```

type_probe 四样本的等值连通情况（仅自身与自身/数值同类相等）：

```
  num-blob  ~ num-blob  -> 1
  num-int   ~ num-int   -> 1
  num-int   ~ num-real  -> 1
  num-real  ~ num-int   -> 1
  num-real  ~ num-real  -> 1
  num-text  ~ num-text  -> 1
```

## 3. NULL 不参与等值，只能用 IS 定位

```
SELECT NULL = NULL;        -> NULL  (NULL，非真)
SELECT NULL IS NULL;       -> 1
type_probe 中 NULL 行数:
  WHERE sample = NULL  -> 0 行（= 比较找不到 NULL）
  WHERE sample IS NULL -> 1 行（定位 NULL 的正确写法）
```

## 4. node 表 payload 同显示值的比较归类

```
  slot=200 payload=3      typeof=integer  payload=3 -> 1
  slot=201 payload=3.0    typeof=real     payload=3 -> 1
  slot=202 payload='3'    typeof=text     payload=3 -> 0
  slot=203 payload='3'    typeof=blob     payload=3 -> 0
```

## 5. 空 BLOB 与 NULL 在 node 中的定位差异

```
  slot=-9223372036854775808: typeof(payload)=blob
  slot=4: typeof(payload)=null
  slot=100: typeof(payload)=blob
  slot=101: typeof(payload)=null
  slot=300: typeof(payload)=blob
  slot=9223372036854775807: typeof(payload)=null
```

## 6. 整数亲和主键的归一：写 4.0 实际存为 integer 4

```
  写入 4.0 后 slot=4 typeof=integer（再插 4.0 会撞唯一约束）
```
