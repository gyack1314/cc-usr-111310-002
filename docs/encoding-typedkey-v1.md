# 有类型复合键编码规范 typedkey-v1（冻结版）

本文件是仓库索引工具的**正式编码版本承诺**。数据资料中的 `candidate-v1`
只是资料标签，不是编码承诺；索引文件头与 manifest 均写入本版本号
`typedkey-v1`，打开时版本不符即拒绝。

## 1. Schema 冻结内容

列序、列 id、类型、`direction`（asc/desc）、`nulls`（first/last）在索引
构建时冻结；decimal 另冻结 `precision` 与 `scale`。schema 摘要为各列摘要
数组的规范 JSON（UTF-8、sort_keys、紧凑分隔符）的 SHA-256，写入段头与
index manifest。方向与 null 位置是两个独立维度：desc 只反转非 null 值的
排序，绝不联动反转 null 位置。

支持类型：`int64`、定标 `decimal`、UTC 微秒 `timestamp`、UTF8 `string`、
`bytes`、`boolean`。输入元组必须与 schema 等列，缺字段直接拒绝；显式
`null` 单独编码。

## 2. 单列自然序编码（asc 视角）

每列编码是自定界字节串：

```
非null:  tag(1B) + escape(payload) + 0x00
null:    null-tag(1B)
```

| 类型 | tag | payload（escape 前） |
| --- | --- | --- |
| null（nulls=first） | `0x02` | 无（单字节即整列） |
| boolean | `0x04` | false=`02`，true=`03` |
| int64 | `0x06` | 有符号保序变长整数（§3） |
| timestamp | `0x08` | 同 int64，语义为 UTC 微秒 |
| decimal | `0x0A` | unscaled 整数的同型变长编码 |
| string | `0x0C` | 原 UTF-8 字节，不做 Unicode 归一 |
| bytes | `0x0E` | 原始字节，可含任意 0x00 |
| null（nulls=last） | `0x20` | 无（单字节即整列） |

所有 tag 为偶值且严格按类型排序：null-first 最前，随后
boolean<int64<timestamp<decimal<string<bytes，null-last 最后。因此不同
类型的误配可在解码时被 tag 检查拒绝，null 与任何非 null 值不会混淆。

**转义**：payload 中 `0x01 → 01 01`、`0x00 → 01 00`，再追加终结符
`0x00`。解码遇到裸 `0x00` 即列尾；`0x01` 后必须紧跟 `00/01`，否则报
非法转义；转义前缀后无字节报截断。该转义保证终结符不出现在载荷内部，
且不改变 payload 的字节序（每个原始字节映射为同序的一或两字节序列）。

**string/bytes 保序性**：对两个字节串 A、B，escape(A)+00 与
escape(B)+00 的无符号比较等于 A、B 的字节比较；A 是 B 前缀时
escape(A)+00 在 escape(B) 的下一个原始字节处先遇到终结符，仍严格在前。

## 3. 有符号保序变长整数

取整数最小大端字节表示，用一个标记字节编码长度与符号：

- 非负 n，大端长度 L（1..9）：`marker = 0x16 + L`（`17..1F`）+ 大端字节；
- 负 n，`-n` 大端长度 L（1..9）：`marker = 0x0B - L`（`0A..02`）+ 大端字节按位取反。

负数组标记 `02..0A` 全部小于非负组 `17..1F`；组内标记随绝对值位数反向/
正向变化，载荷再按大端（负数取反）保序。int64 全范围（含
-2^63，L=9 marker=02）与 decimal unscaled（precision ≤ 18）均可表示。
`0B..16` 为保留标记，出现即判损坏/版本不符。

## 4. decimal 定标

输入必须是十进制词法字符串，禁止经 float 转换。按 schema 的 scale 用
`ROUND_UNNECESSARY` 定标：任何需要舍入的输入直接拒绝；定标后 unscaled
位数超过 precision 拒绝。负零归一为零；`1.2` 与 `1.20` 在 scale=2 下
unscaled 均为 120，编码字节完全相同（等值异写同键）。

## 5. 降序列与 null 独立性

desc 列把该列**完整自然编码逐字节按位取反**（tag、payload、终结符都
取反）。对任意两个合法单列编码 X、Y：X < Y ⇔ invert(X) > invert(Y)，
包括变长前缀情形（较短编码以自然 `00` 结尾，取反后为 `FF`，而较长编码
对应位置不可能是 `00`，因为转义后载荷内不存在裸 `00`）。

null 不使用“取反自然 null”这种隐式规则（那会让 desc 顺手反转 null
位置）。asc 空间用固定 null-tag；desc 空间的 null 使用**独立的单字节
tag**，不参与取反：

| direction | nulls=first | nulls=last |
| --- | --- | --- |
| asc | `02` | `20` |
| desc | `00`（独立tag，不取反） | `FC`（独立tag，不取反） |

desc 非 null 值的首字节集合为 `{F1,F3,F5,F7,F9,FB}`（各类型 tag 取反），
`00` 恒在其前、`FC` 恒在其后，且二者都不会出现在 desc 转义流或终结符
（`FE/FF`）中。因此 desc/nulls=first 时 null 仍在最前，desc/nulls=last
时 null 仍在最后，方向不改变 null 位置。

## 6. 元组、自定界与可逆性

元组编码 = 各列落盘编码按冻结列序直接拼接。无额外长度前缀也无歧义：
每列解码器依据“裸终结符”（asc 空间 `00`、desc 空间 `FF`，且跳过转义
对）确定列尾；null 列单字节即定界。

- 无歧义/可逆：`decode_tuple` 逐列还原全部原始值（string 还原原 UTF-8、
  bytes 还原含零字节原串、decimal 还原 unscaled+scale），并拒绝任何
  截断、非法转义、tag 不符与尾随未解释字节。
- 保序：两个合法元组的无符号字节比较 ⇔ 逐列 schema 比较（每列含方向与
  null 策略）；共享前导列的元组，其编码共享真实字节前缀，前缀在后续列
  分叉处继续保持该序。

## 7. row_id 与总序

相等业务键允许重复。索引条目键 = 业务元组编码 + row_id 编码；row_id 为
非空 UTF-8 字符串，按同样的 escape + `00` 自定界规则附加。条目键按
无符号字节排序，因此同业务键内按 row_id 升序形成稳定总序，原行位置
（source_position）独立保留。row_id 后缀只扩展业务键，不改变任何业务键
范围的边界：业务前缀 P 的区间恒包含其全部 row_id。

## 8. 范围编译（半开字节区间）

查询形状：若干前导列精确相等（可显式 null）+ 紧随一列的 range（开闭
上下界，语义按自然类型序，不随 desc 改含义）或 string/bytes 的
starts_with；其后列无约束。每个查询编译为至多一个半开区间
`[lower, upper)`，None 端表示无界；语义为空输出空区间列表，不使用最大
哨兵真值。

- 前导相等列：逐列落盘编码拼接为固定前缀（null 用其固定 null-tag）。
- asc range：下界闭取完整列编码、开取其后继；上界开取完整列编码、闭取
  其后继。后继 = 末个非 `FF` 字节加一、截去尾部；全 `FF` 时该端无界。
- desc range：在 desc 落盘空间直接由完整合法列编码取反推导
  （自然下界对应 desc 上界、自然上界对应 desc 下界，开闭经后继转换），
  不对残缺前缀取反。
- starts_with：命中键在对应空间共享 `tag + escape(prefix)`（desc 时整体
  取反）这一真实字节前缀，区间为该前缀到其后继；空前缀即覆盖该列全部
  非 null 值。null 不属于 starts_with 也不属于 range。
- 自然空间先判语义为空（lo>hi，或 lo=hi 且任一端开），返回空区间。

## 9. 段文件与失效绑定

段文件 = 文件头（magic、schema SHA-256、编码版本、快照、区段定位、
CRC32）+ 数据区（帧化条目按总序分块，每块 CRC32）+ 稀疏块目录（每块
首键/行数/偏移，CRC32）+ 文件尾（magic、数据区 SHA-256）。目录支持
二分 seek 到首个可能相交块；重复业务键跨块时扫描不提前终止。任何
magic/CRC/摘要/长度不一致或截断、尾随字节均抛损坏错误，绝不以空结果
返回。

index manifest 记录编码版本、schema 摘要、数据快照 id、源记录 SHA-256
与资源上限。打开时核对编码版本、schema 摘要与快照；源数据或 schema
变更导致摘要变化即判定旧索引失效。
