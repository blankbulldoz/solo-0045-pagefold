# Pagefold 三方合流裁决练习（两轮）

面向新同事的合流裁决训练。同一批三份 SQLite 镜像，支撑两种都能交付的裁决策略，
外加一个会被系统拒绝的组合。练习分两轮：第一轮只看祖本与左右支的行值做纸面判断，
第二轮进 Pagefold 盲测自己的判断。讲师在课上只发输入镜像，结局由系统现场重演决定。

## 练习想让练习者遇到的三种情形

| 情形 | 位置 | 三方表现 | 系统如何归类 |
| --- | --- | --- | --- |
| 双方相同插入 | `ledger` 主键 `L-101` | 祖本缺席；左右两支插入了**完全同值**的行 | 自动采纳（双边同值），无需人裁决 |
| 删改分叉 | `ledger` 主键 `L-102` | 左支**删除**；右支**改写**为新标题与新备注 | 唯一人工冲突，必须在 base/left/right 中选一个 |
| 来自不同分支的引用行为 | `citation` 的 `C-210` 与 `C-300` | 左支新增 `C-210`、右支新增 `C-300`，均引用 `L-100` | 各自单边变化，分别自动采左、自动采右 |

## 两种可交付结果与一个被拒组合

三者都基于同一批镜像、同一套自动项，差别只在人工冲突 `L-102` 的选择：

| 结局 | 对 L-102 的选择 | 重演 | 可否封存下载 |
| --- | --- | --- | --- |
| 策略 A「尊重删除」 | `left`（左支行缺席 = 删除） | 通过 | 可封存、可下载 |
| 策略 B「保留改写」 | `right`（右支改写后的行） | 通过 | 可封存、可下载 |
| 被拒组合「回退祖本」 | `base`（保留祖本旧行） | **阻断**：与自动采纳的 L-101 在 `ledger.title` 上 UNIQUE 冲突 | 不可封存、不可下载 |

两种可交付结果在**保留现场备注 `site_note` 与自动标记 `auto_flag`** 上存在可查询差异：
策略 A 中 L-102 整行缺席，其备注/标记随之消失；策略 B 中 L-102 保留右支的
`右支改写备注 / 右支标记`。核验 SQL 见讲师核验表。

## 目录与交付物

```
training/
├── README.md                         # 本文件
├── images/                           # 交付镜像：同一批三份输入（课堂只发这个目录）
│   ├── base-ledger.sqlite            # 祖本
│   ├── left-branch.sqlite            # 左支
│   └── right-branch.sqlite           # 右支
├── materials/                        # 练习者材料（课堂发放）
│   ├── 入场资料.md                    # 有哪些表、字段、角色与约束
│   ├── 练习者手册.md                  # 两轮流程与作答区（不含结局）
│   └── 行值对照底稿.md                # 由镜像直接导出的三方行值（第一轮纸面用）
├── instructor/                       # 讲师材料（课后/揭盅时才发）
│   ├── 讲师答案与核验表.md
│   └── evidence/                     # 实跑证据：计划、裁决、重演、凭证、候选镜像
└── scripts/                          # 可复跑的脚本
    ├── build_exercise_images.py      # 生成三份镜像
    ├── export_worksheet.py           # 从镜像导出行值底稿
    └── run_exercise.py               # 走真实 HTTP 入口跑三种结局并产出 evidence
```

## 复跑方式

需要 Python 3.11+ 与带 `serialize/deserialize` 的 SQLite 3.37+，无第三方依赖。
脚本以隔离模式运行，路径均通过参数传入。

```sh
# 1. 重新生成镜像（目录已存在同名文件时会拒绝覆盖）
python3 -I training/scripts/build_exercise_images.py training/images

# 2. 重新导出行值底稿
python3 -I training/scripts/export_worksheet.py \
  --images training/images --out training/materials/行值对照底稿.md

# 3. 起真实 HTTP 服务，把两种可交付结果与被拒组合各跑一遍
python3 -I training/scripts/run_exercise.py \
  --images training/images --out training/instructor/evidence
```

讲师也可以用正式服务手动演示：

```sh
python3 -m pagefold --state ./pagefold-state.sqlite
# 浏览器打开 http://127.0.0.1:8745 ，依次上传三份镜像并创建计划
```

## 课堂发放建议

- 第一轮只发 `materials/` 与 `images/`，练习者不看 Pagefold 的自动结论，先纸面作答。
- 第二轮练习者自己上传 `images/` 里的三份镜像，在 Pagefold 盲测并对照第一轮判断。
- 结局与 `instructor/` 在揭盅环节才公布；讲师可只发输入，凭系统现场重演得到相同结果。
