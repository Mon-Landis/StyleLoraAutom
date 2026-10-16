# F28 NAI → Anima 提示词转换协议

状态：模块设计草案；只适用于提示词组导入的「来源格式 nai」。
约束：纯算法、不调用模型、不查询画师目录、不改变现有架构决策。
本文的推荐策略用于使测试可执行；Q 项获确认前不等于已批准产品默认值。

## 1 范围与接口

公开入口：`convert_nai_to_anima(text: str, *, policy: ConversionPolicy) -> ConversionResult`。
输入是完整 NAI 提示词串；输出供 Anima 使用，不写数据库、不改原始输入。
`ConversionResult(text: str, tokens: tuple[ConvertedToken, ...], warnings: tuple[ConversionWarning, ...])`。
`ConvertedToken(value: str, raw_weight: Decimal, weight: Decimal, span: SourceSpan, rendered: str)`。
`value` 是解码转义、完成 artist/下划线规范化但尚未输出转义的正文；tokens 仅含最终保留项。
`SourceSpan(start: int, end: int)` 使用原串 Unicode 码点索引、左闭右开，不是 UTF-8 字节。
token span 覆盖正文源区间；warning span 指向触发符号；全局压缩告警覆盖整个输入。
`ConversionWarning(code: WarningCode, span: SourceSpan, detail: str)`；detail 仅辅助显示，不参与逻辑。
`ConversionPolicy(bracket_mode: Literal['multiplicative','additive'], ordinary_underscores: Literal['preserve','space'], min_weight: Decimal, max_weight: Decimal, decimal_places: int, max_depth: int, max_chars: int, max_numeric_chars: int)`。
推荐测试实例 P：`('multiplicative', 'preserve', Decimal('0.1'), Decimal('3.2'), 2, 128, 1000000, 64)`。
调用方必须显式传 policy；不要用隐藏默认值提前决定 Q01 或其它 Q 项。
校验要求：`0 < min_weight < 1 < max_weight <= 3.2`；decimal_places 固定为 2。
min_weight、max_weight 必须是有限且可精确表示为两位小数的 Decimal；各资源上限为正整数。
非法类型抛 `TypeError`；非法 policy 抛 `ValueError`；文本语法错误走 warnings，不抛异常。
入口幂等性：同输入、同策略得到完全相同结果；不保证 `convert(convert(x).text)` 相等。
原因：输出是 Anima 方言，重复作为 nai 导入会再次转义普通括号；导入层须记录来源和转换版本。
空串返回 `('', (), ())`；纯分隔符同样返回空结果，不制造空 tag。
输出保持原顺序及重复 tag；不得在转换期间去重、排序、转移到负向提示词或推断角色归属。
最小入口测试：空串、重复 tag、错误 policy，以及连续两次调用的结果对象值相等。

## 2 词法与嵌套栈

词法模块公开接口：`scan_nai(text: str, *, policy: ConversionPolicy) -> LexResult`。
`LexResult(items: tuple[LexToken, ...], warnings: tuple[ConversionWarning, ...])`。
`LexToken(kind: LexKind, raw: str, span: SourceSpan, value: str | Decimal | None)`。
| LexKind | 形式 | 行为 |
| --- | --- | --- |
| TEXT | 其它连续正文 | 原样累积；圆括号属于正文 |
| ESCAPED | 反斜线加下一码点 | 解码为字面字符，不触发语法 |
| OPEN_UP / CLOSE_UP | `{` / `}` | 增权栈开闭 |
| OPEN_DOWN / CLOSE_DOWN | `[` / `]` | 降权栈开闭 |
| NUM_OPEN | 合法数值紧接 `::` | 开启一个数值区段 |
| DCOLON | `::` | 关闭数值区段，或保留为未知语法 |
| INVALID_NUM | 非法数值候选紧接 `::` | 保留文字并告警 |
| SEP | `,`、`，`、CRLF、CR、LF | 结束当前 tag；CRLF 视为一个符号 |
| PIPE | 未转义 U+007C | 保留字面字符并告警，不展开交替 |
| EOF | 串尾 | 收束正文和未闭合状态 |
优先级：ESCAPED → SEP → 数值候选/双冒号 → 方括号/花括号 → PIPE → TEXT。
前导 BOM 仅在索引 0 丢弃；其余 BOM 作为正文；水平空白在输出阶段折叠。
数值文法：`number = ['+'|'-'], (digits, ['.', digits] | '.', digits)`；不接受指数、NaN、Infinity。
数值候选仅从 tag/区段起点开始，允许前导水平空白；也可紧随开括号或关闭数值段。
候选以数字、正负号、点起始，或完整匹配大小写不敏感的 NaN/Infinity；读取到紧邻的 `::`。
候选不得跨空白、转义、括号、逗号、换行或 U+007C；未紧接双冒号时退回 TEXT，不报非法数值。
`1e3::`、`--1::`、`NaN::` 因而是 INVALID_NUM；普通 `foo::` 则是 TEXT 加 DCOLON。
数值活动时，任何双冒号优先作为结束符；其前数字属于正文，不允许嵌套数值段。
scan_nai 自身维护数值段开闭布尔状态以选择词法类型；parse_nai 重放这些类型并管理系数及括号栈。
解析接口：`parse_nai(items: tuple[LexToken, ...], *, policy: ConversionPolicy) -> ParseResult`。
`ParseResult(items: tuple[WeightedTag, ...], warnings: tuple[ConversionWarning, ...])`。
`WeightedTag(value: str, raw_weight: Decimal, span: SourceSpan)`；保留非正权重，交压缩模块处理。
状态由正文缓冲、括号栈、单一活动数值段、当前 up/down 计数组成。
括号或数值权重发生变化前，先将非空正文缓冲按旧状态发出；因此 `a{b}c` 产生三个 tag。
SEP 只结束正文，不关闭括号和数值段；故 `2::a,b::` 对两个 tag 都应用 2。
开括号压入 `(kind, span, emitted_count)`；正确闭括号弹栈；数值段记录起点和 emitted_count。
空白缓冲不发出 token；关闭或 EOF 时 emitted_count 未增加的合法块产生 W_EMPTY_BLOCK。
最小词法测试：转义优先、CRLF 一个 SEP、数值区段跨逗号、混合嵌套、源索引回溯。

## 3 权重计算与合成

乘法口径（NAI 实际语义）：`B = 1.05 ** (up - down)`；降权是除以 1.05，不是乘 0.95。
用户“每层 ±5%”的加法口径：`B = 1 + 0.05 * (up - down)`；不在单层途中截断。
三层花括号分别得到 1.157625 和 1.15；三层方括号分别约 0.8638376 和 0.85。
混合 `{[x]}` 两种口径都得到 1；三增一减分别为 1.1025 和 1.10。
活动数值段的系数记为 N，无数值段则 N=1；合成始终为 `raw_weight = B * N`。
例如 `{2::x::}` 的原始权重为 2.1；`2::{x}::` 也为 2.1。
每个正文片段按当时栈状态计算；`{a,[b],c}` 分别为 1.05、1、1.05。
加法口径在 down-up >=20 时可能得到非正值，和数值非正权重走同一丢弃规则。
采用 Decimal 局部上下文 precision=128、ROUND_HALF_UP；不依赖进程全局 Decimal 设置。
合成中不做两位小数舍入；只在压缩完成后量化，避免嵌套累计误差。
数值候选长度含符号和点，超过 max_numeric_chars 不进入 Decimal，按非法数值恢复。
建议 Q01 选择乘法，兼容实际 NAI；加法作为显式导入策略，不能依据内容猜测。

## 4 输出规范化

目标权重语法确定为 `(tag:x)`，用户原写 `(x:tag)` 不采用；仍列 Q02 请求确认。
正文的 `(`、`)` 一律分别输出为 `\(`、`\)`；权重包装括号由序列化器最后生成。
转义先解码一次，再序列化一次；已转义 `\(` 不应变为双重转义。
字面反斜线输出为 `\\`；字面 `{ } [ ]` 同样加反斜线，防止目标解析器将方括号当作降权。
精确小写前缀 `artist:` 在正文 trim 后识别；删除前缀，去前导空白，并添加 `@`。
artist 正文所有 `_` 替换为空格，再折叠空白；`artist:xxx_(yyy)` 输出 `@xxx \(yyy\)`。
空 artist 正文丢弃并 W_EMPTY_ARTIST；非空 artist 即使权重非正，也先做正文规范化。
普通 tag 下划线默认保留（P）；space 策略才替换为空格，详见 Q03。
水平空白连续串压为一个空格，去首尾空白；换行已作为 SEP；空正文删除。
所有输出 tag 以 ASCII `, ` 连接，无首尾逗号；重复 tag 保留。
最终权重 ROUND_HALF_UP 到 0.01，删尾零及小数点；量化后等于 1 时省略权重括号。
例如 1.157625 → `(x:1.16)`，2 → `(x:2)`，1.004 → `x`；不输出科学计数法。
字面 `|`、`::` 不承诺目标语义等价，保留并告警；不能将其解释为已支持的多角色协议。

## 5 权重压缩算法

推荐按一次入口调用的全部 tag 统一压缩；换行和数值段不重置压缩范围。
非正值丢弃并 W_NONPOSITIVE_WEIGHT；避免把负向意图反转成微弱正向意图。
低于 1 的值保留，低于下限 L=0.1 时抬到 L，并逐项 W_WEIGHT_FLOORED。
高于 1 的值按到 1 的距离统一缩放，保持增权顺序和增量比例；不逐 tag 截断。
上限 U=3.2；若最大值 M<=U 不改变增权值；否则全局只发一次 W_RANGE_COMPRESSED。
例：权重 1、2、5 经压缩成为 1、1.55、3.2；逐项截断会使多个大权重失去强弱差异。
代价：加入一个极大权重会改变其它增权 tag，必须在导入预览显示全局告警；详见 Q04。
伪代码（在正文规范化、空 artist 删除之后执行）：
```text
kept = []
for tag in tags:
    if tag.raw_weight <= 0: warn(W_NONPOSITIVE_WEIGHT, tag.span)
    else: kept.append(tag)
M = max([1] + [t.raw_weight for t in kept])
s = 1 if M <= U else (U - 1) / (M - 1)
if M > U: warn(W_RANGE_COMPRESSED, whole_input_span)
for t in kept:
    w = t.raw_weight
    if w < L: w = L; warn(W_WEIGHT_FLOORED, t.span)
    elif w > 1: w = 1 + (w - 1) * s
    emit ScaledTag(t.value, t.raw_weight, clamp(quantize(w, '0.01', HALF_UP), L, U), t.span)
return emitted_items
```
算法对已经压缩的权重再次运行数值上幂等；不意味着整条 nai→anima 文本转换幂等。
舍入可能合并接近的权重，保证非严格单调；不保证舍入后增量比例仍精确相同。
资源复杂度：扫描/解析 O(n)，压缩 O(t)，栈 O(max_depth)，token/结果 O(n)。

## 6 错误恢复与受限告警

WarningCode 是以下封闭枚举；新增码必须更新协议和测试，不能用任意字符串代替。
| code | 触发与恢复 |
| --- | --- |
| W_UNCLOSED_BRACKET | EOF 剩余开括号逐个自动关闭，已应用权重保留 |
| W_UNMATCHED_CLOSE | 栈空或闭括号不匹配栈顶：丢弃此闭括号，不跨层搜索 |
| W_UNCLOSED_NUMERIC | 数值段延伸到 EOF，保留该区段权重 |
| W_INVALID_NUMBER | 非法数值候选连同 `::` 作为字面正文；不改变状态 |
| W_UNKNOWN_DCOLON | 非数值段内裸 `::` 作为正文保留，不猜测角色边界 |
| W_UNSUPPORTED_ALTERNATION | 未转义 `|` 保留，不选择或展开任一分支 |
| W_EMPTY_BLOCK | 空括号块或空数值段删除；每个空块各一次 |
| W_NONPOSITIVE_WEIGHT | 每个规范化后非空 tag 的原始权重 <=0，丢弃 |
| W_WEIGHT_FLOORED / W_RANGE_COMPRESSED | 按第 5 节处理 |
| W_EMPTY_ARTIST | 删除正文为空的 artist tag |
| W_DANGLING_ESCAPE | 串尾孤立反斜线按字面反斜线保留 |
| W_DEPTH_LIMIT | 第 max_depth+1 个开括号：终止转换，text 为空、tokens 为空 |
| W_INPUT_LIMIT | 输入超过 max_chars：不扫描，text 为空、tokens 为空 |
资源上限触发时只返回相应一个告警，撤销此前中间结果；导入层须阻止将其作为成功空提示词入库。
深度只计 `{` 和 `[`；`{{{` 本身深度为 3，EOF 返回三个 W_UNCLOSED_BRACKET。
不匹配闭括号不改变当前权重；例如 `{x]` 输出 `(x:1.05)`，含两个恢复告警。
INVALID_NUM 只消费候选及紧接的 `::`；之后裸 `::` 独立告警，不猜测其配对关系。
合法的未闭合数值段不是非法数值：`2::a,b` 正常作用至串尾并给一次未闭合告警。
告警按 `(span.start, span.end, code)` 升序稳定排序；同码不同位置不合并。
EOF 恢复告警定位对应开符号；空块告警也定位开符号，因此一个空的未闭合块可有两类告警。
转义后的逗号/换行作为正文会使目标重新分段：序列化须对字面逗号输出 `\,`。
字面 CR/LF 统一成空格；字面中文逗号保留；此类跨方言效果列 Q07 确认。

## 7 与共享 tokenizer 的关系及模块规格

只读依据：`src/stylelora/tagging/tokenizer.py` 和 `src/stylelora/tagging/parser.py`。
现有 `tokenize_prompt(text, *, catalog)` 用 `text.split(',')`；VERSION 为 `comma-v1`。
它依赖 catalog.lookup/normalize_tag，权重正则只提取局部数字；不维护括号或数值区段状态。
`syntax_of` 可粗分 escaped/separator/weight，但不足以识别本文词法优先级及嵌套语义。
`parse_original_prompt` 要求 catalog.available，并按目录识别及去重；与本协议保序、不依赖目录不同。
因此建议新增 nai 方言 parser，不将源串先交 comma-v1 切分，也不通过替换正则实现嵌套。
复用点：保留现有通用导入/校验职责；若导入链需目录分析，可对转换结果另行调用共享 tokenizer。
共享 tokenizer 的结果只能作辅助诊断，不能反向覆盖本转换器的权重、span 或去重结果。
不直接复用 normalize_tag：本次未读取实现，无法保证它遵守 artist/普通下划线策略。
`ConvertedToken` 为独立传输类型；不假设与现有 `.types.Token` 构造参数或语义相容。
下列是逻辑模块及建议接口，物理文件拆分留给既定架构；不在本文替换现有公共入口。
| 模块 | 职责 / 公开类型接口 | 依赖 | 错误 / 幂等性 | 最小测试 |
| --- | --- | --- | --- | --- |
| 词法 | scan_nai，见第 2 节 | policy、公共数据类型 | 词法告警；同输入确定性 | 转义、非法数值、CRLF、输入上限 |
| NAI 解析 | parse_nai，见第 2 节 | LexToken、Decimal | 恢复及深度告警；同输入确定性 | 跨 tag 数值段、混合栈、错配 |
| 正文规范化 | `normalize_nai_tags(tags: tuple[WeightedTag,...], *, policy: ConversionPolicy) -> ParseResult` | 公共类型 | 空 artist 告警；规范化值再次处理不变 | artist、两种下划线策略、空 artist |
| 权重压缩 | `compress_weights(tags: tuple[WeightedTag,...], *, policy: ConversionPolicy) -> CompressionResult` | Decimal | 非正/下限/全局告警；数值幂等 | 0、负值、最大值、统一缩放 |
| 序列化 | `render_anima(tags: tuple[ScaledTag,...]) -> tuple[ConvertedToken,...]` | 转义规则 | 合法类型无语法错误；同输入确定性 | 普通括号、反斜线、权重 1 |
| 编排入口 | convert_nai_to_anima，见第 1 节 | 上述五模块 | 参数异常/告警汇总；不保证文本往返幂等 | 空串、排序、资源失败、结果一致性 |
`ScaledTag(value: str, raw_weight: Decimal, weight: Decimal, span: SourceSpan)`。
`CompressionResult(items: tuple[ScaledTag,...], warnings: tuple[ConversionWarning,...])`。
正文规范化须接收解码后的正文；不要将 render_anima 的结果再次送入规范化模块。
编排顺序：验证参数 → 扫描 → 解析 → 正文规范化 → 压缩 → 序列化 → 拼接与告警排序。
各阶段只返回不可变数据；无日志、文件、网络、目录查询或全局状态副作用。

## 8 测试向量

下表使用 P，除非备注覆盖策略；反引号中反斜线是实际字符，非 Python 字符串字面量。
`∅` 表示空串；warnings 列是码的多重集，`×N` 表示次数；实际顺序按第 6 节断言。
测试还需逐项断言 tokens 的 value、raw_weight、weight、span；以下表格直接驱动文本/告警断言。
| ID | 输入 | 期望输出 | warnings / 策略备注 |
| --- | --- | --- | --- |
| T01 | ∅ | ∅ | 无 |
| T02 | `cat` | `cat` | 无 |
| T03 | `{cat}` | `(cat:1.05)` | 无 |
| T04 | `[cat]` | `(cat:0.95)` | 无 |
| T05 | `{{{cat}}}` | `(cat:1.16)` | 无 |
| T06 | `{{{cat}}}` | `(cat:1.15)` | 无；bracket_mode=additive |
| T07 | `[[[cat]]]` | `(cat:0.86)` | 无 |
| T08 | `[[[cat]]]` | `(cat:0.85)` | 无；bracket_mode=additive |
| T09 | `{[cat]}` | `cat` | 无 |
| T10 | `{a,[b],c}` | `(a:1.05), b, (c:1.05)` | 无 |
| T11 | `1.5::cat::` | `(cat:1.5)` | 无 |
| T12 | `2::a,b::` | `(a:2), (b:2)` | 无 |
| T13 | `2::a,b` | `(a:2), (b:2)` | W_UNCLOSED_NUMERIC |
| T14 | `{2::cat::}` | `(cat:2.1)` | 无 |
| T15 | `2::{cat}::` | `(cat:2.1)` | 无 |
| T16 | `name (series)` | `name \(series\)` | 无 |
| T17 | `name \(series\)` | `name \(series\)` | 无 |
| T18 | `artist:xxx_(yyy)` | `@xxx \(yyy\)` | 无 |
| T19 | `{artist:xxx_(yyy)}` | `(@xxx \(yyy\):1.05)` | 无 |
| T20 | `blue_hair` | `blue_hair` | 无 |
| T21 | `blue_hair` | `blue hair` | 无；ordinary_underscores=space |
| T22 | `-1::cat::,dog` | `dog` | W_NONPOSITIVE_WEIGHT |
| T23 | `0::cat::` | ∅ | W_NONPOSITIVE_WEIGHT |
| T24 | `0.01::cat::` | `(cat:0.1)` | W_WEIGHT_FLOORED |
| T25 | `0.5::cat::` | `(cat:0.5)` | 无 |
| T26 | `a,2::b::,5::c::` | `a, (b:1.55), (c:3.2)` | W_RANGE_COMPRESSED |
| T27 | `3.2::a::` | `(a:3.2)` | 无 |
| T28 | `1.004::a::` | `a` | 无 |
| T29 | `1.005::a::` | `(a:1.01)` | 无 |
| T30 | `{cat` | `(cat:1.05)` | W_UNCLOSED_BRACKET |
| T31 | `cat}` | `cat` | W_UNMATCHED_CLOSE |
| T32 | `{cat]` | `(cat:1.05)` | W_UNCLOSED_BRACKET、W_UNMATCHED_CLOSE |
| T33 | `\{cat\}` | `\{cat\}` | 无 |
| T34 | `{}` | ∅ | W_EMPTY_BLOCK |
| T35 | `2::::` | ∅ | W_EMPTY_BLOCK |
| T36 | `1e3::cat::` | `1e3::cat::` | W_INVALID_NUMBER、W_UNKNOWN_DCOLON |
| T37 | `NaN::cat::` | `NaN::cat::` | W_INVALID_NUMBER、W_UNKNOWN_DCOLON |
| T38 | `a` + U+007C + `b` | `a` + U+007C + `b` | W_UNSUPPORTED_ALTERNATION |
| T39 | `a::b` | `a::b` | W_UNKNOWN_DCOLON |
| T40 | ` a， b` + LF + `c,,` | `a, b, c` | 无 |
| T41 | `a{b}c` | `a, (b:1.05), c` | 无 |
| T42 | `{{{cat}}}` | ∅ | W_DEPTH_LIMIT；max_depth=2 |
| T43 | `artist:` | ∅ | W_EMPTY_ARTIST |
| T44 | `cat,cat` | `cat, cat` | 无 |
| T45 | `a\,b` | `a\,b` | 无；目标兼容性见 Q07 |
| T46 | `abc` | ∅ | W_INPUT_LIMIT；max_chars=2 |
| T47 | `{{{cat` | `(cat:1.16)` | W_UNCLOSED_BRACKET×3 |
| T48 | `a` + 一个反斜线 | `a` + 两个反斜线 | W_DANGLING_ESCAPE |
| T49 | `2::a 3::b` | `(a 3:2), b` | 无；活动段的双冒号总是关闭符 |
| T50 | `[` 重复20次 + `x` + `]` 重复20次 | ∅ | W_NONPOSITIVE_WEIGHT；additive |
补充边界断言：默认深度128可处理，129失败；64字符数值可解析，65字符候选给 W_INVALID_NUMBER。
span 例：`{cat}` 的 tag span=(1,4)，`2::a,b::` 为 (3,4)、(5,6)。
端到端回归须验证原始输入未改写、无 catalog 时可转换；本设计阶段不新增或运行代码测试。

## 9 待确认与建议变更

- Q01：默认每层乘法还是加法？推荐乘法，三层增权 1.157625，对照加法 1.15；保留显式加法策略。
- Q02：目标是否确认采用 `(tag:x)`？推荐采用，与 Anima/ComfyUI 惯用格式一致。
- Q03：普通 tag 是否将下划线换空格？推荐先保留；artist 固定替换，避免无依据改写其它标识。
- Q04：是否接受非正值丢弃、下限0.1、全串统一增量压缩到3.2？推荐接受并展示告警。
- Q05：`|` 和无数值开启的 `::` 是否有项目特定交替/多角色语义？推荐暂保留并告警，不自动拆角色。
- Q06：同一逗号项内权重切换是否拆成多个 tag？推荐拆分，如 T41；否则单 tag 无法精确保留分段权重。
- Q07：实际 Anima 接入端是否支持反斜线转义方括号、花括号、逗号和反斜线？推荐接入测试确认后固化 T33/T45/T48。
- Q08：是否接受深度128、输入100万码点、数值64字符的上限及超限阻止导入？推荐接受，避免部分结果静默入库。
- C60：增加纯函数转换入口及公共策略/结果/受限告警类型，供来源 nai 的导入步骤调用。
- C61：新增 NAI 方言词法/解析职责，保留 comma-v1 行为，明确 catalog 分析为独立后续步骤。
- C62：增加正文规范化、Decimal 权重合成和统一范围压缩职责；不新增模型或网络依赖。
- C63：导入预览展示转换文本及告警；资源上限告警阻止入库，保存原串、来源与转换版本以避免重复转换。
- C64：按 T01–T50 建立 unittest 参数表，加上 span、告警排序、策略验证和资源边界断言。
- C65：Q 项确认后更新默认策略和对应测试期望；确认前所有推荐均保持草案性质。
