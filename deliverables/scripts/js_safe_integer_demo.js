#!/usr/bin/env node
/*
 * 前端读法风险演示：同样的整数，经 JS Number 与经 Pagefold 标签原子的差别。
 *
 * 数据取自交付镜像 node-reference.sqlite 的真实复合键（node 表 bench='east'）。
 * 浏览器/Node 的 number 是 IEEE-754 双精度，超过 Number.MAX_SAFE_INTEGER(2^53-1)
 * 的整数无法精确保留；而 Pagefold JSON 把整数放在 ["i","<十进制字符串>"] 里，
 * 用字符串承载，不经过 Number，因此不丢精度。
 *
 * 运行：node deliverables/scripts/js_safe_integer_demo.js
 */

'use strict';

// 与 node 表中真实复合键 slot 完全一致的十进制文本（从 SQLite 原样取得）。
const exactSlotStrings = [
  '9007199254740993',   // 2^53 + 1
  '9223372036854775807', // 2^63 - 1，SQLite 64 位有符号整数上限
  '-9223372036854775808', // 2^63 的负值
];

// Pagefold 复合键原子（与 data/pagefold-rows.jsonl 中 node 表的 key 逐字一致）。
const pagefoldKeys = exactSlotStrings.map((s) => [['t', 'east'], ['i', s]]);

console.log('Number.MAX_SAFE_INTEGER =', Number.MAX_SAFE_INTEGER.toLocaleString('en-US'));
console.log('');
console.log('--- 读法 A：裸 JSON 数字 → JS Number（前端直接 JSON.parse 的默认行为）---');
for (const text of exactSlotStrings) {
  const asNumber = Number(text);
  const roundTrips = String(asNumber) === text;
  console.log(
    `原值 ${text}\n  Number() -> ${asNumber}\n  回填文本 ${String(asNumber)}  精度保留: ${roundTrips}`,
  );
}

console.log('');
console.log('--- 读法 B：Pagefold 标签原子 ["i","<字符串>"]，不经过 Number ---');
for (const compositeKey of pagefoldKeys) {
  const integerAtom = compositeKey[1]; // 复合键第二列 slot 的原子
  const [tag, digits] = integerAtom;
  const preserved = exactSlotStrings.includes(digits);
  console.log(`复合键 ${JSON.stringify(compositeKey)}  slot 标签=${tag}  精确保留: ${preserved}`);
}

console.log('');
console.log('--- 复合键定位后果：用被舍入的 Number 当键会查到错误的行 ---');
const wanted = '9007199254740993';
const damagedLookup = Number(wanted); // 9007199254740992
console.log(`想定位 slot=${wanted}`);
console.log(`裸 Number 查表实际使用键 slot=${damagedLookup}（与库中真实键不同，定位失败或串键）`);
console.log(`Pagefold 原子查表使用键 slot=${wanted}（字符串精确匹配，命中正确行）`);

// 退出码：若运行环境的 Number 居然能精确表示这些值（不应发生），返回非零以便 CI 捕获。
const allUnsafe = exactSlotStrings.every((s) => String(Number(s)) !== s);
process.exitCode = allUnsafe ? 0 : 1;
