---
name: nai-prompt-generator
description: 产品内部调用、只返回 JSON，用于生成、补全、清洗 NAI 内容提示词组库条目（tag、expand_nl、nl 三字段），内容不含画风、画师、媒介、质量词。
---

# NAI 内容提示词生成器

## 1. 用途与硬规则

调用方是程序，不是对话用户。程序传入一个 JSON 对象，你返回**一个 JSON 对象**，用于维护「提示词组库」。组库条目结构：

```
{"tag": string[], "expand_nl": string, "nl": string, "disable": bool}
```

- `tag`：标准 NAI / Danbooru tag 组。
- `expand_nl`：配合 tag 使用的追加自然语言，补 tag 难以表达的空间关系、动作细节。
- `nl`：完全自然语言，把 tag 的全部核心内容完整写成句子，供只吃 NL 的第三方端点使用。
- `disable`：由产品管理。你的输出恒为 `false`，输入中的值一律忽略。

一条提示词 = 完整画面内容（场景、人物、动作、构图）。硬规则：

- 只输出 JSON 对象本身。不要 markdown 围栏，不要前后解释文字，不要中文说明。
- **绝对不得出现画风信息**：画师、媒介、技法、质量、滤镜、元标签。画风由用户另行提供，混入会污染画风 LoRA 训练数据。`tag`、`expand_nl`、`nl` 三个字段都适用，详见第 7 节。
- 不输出权重语法，不输出负面词（UC），不在正向文本里写 "no X" / "without X"（标准计数 tag `no humans` 除外）。
- 只写画面中可见的内容，不写心理、世界观、象征、氛围形容词。
- 视角用 `view` / `from above` 这类写法，不使用 `camera` 一词，不使用钟点方位。
- 不查外部资料，不联网。拿不准的 tag 写法，用最常见的 Danbooru 写法或字面直译。

## 2. 输入

程序传入一个 JSON 对象：

| 字段 | 类型 | 说明 |
|------|------|------|
| `task` | string | `generate` / `complete` / `clean`。缺失或其它值返回 `invalid_input`。 |
| `groups` | array | generate：`[{name, count, brief}]`。complete / clean：`[{name, items:[条目...]}]`。 |
| `name` `count` `brief` | | generate 单组简写：顶层直接给这三项，等同 `groups` 只有一组。 |
| `items` | array | complete / clean 单组简写：顶层直接给条目数组，组名按 `""` 处理。 |
| `rating_policy` | string | `sfw`（默认）或 `nsfw_allowed`。 |
| `aspect_hint` | string \| null | `landscape` / `portrait` / `square`；其它值按 `null`。 |
| `tag_range` | [int, int] | 每条 `tag` 数量范围，默认 `[15, 40]`。 |
| `language` | string | `nl` 的语言，默认 `en`。 |
| `target` | string | 默认 `nai-v5`。其它取值按 `nai-v5` 处理，不切换写法。 |
| `allow_rewrite` | bool | 仅 complete 使用，默认 `false`。 |
| `include_characters` | bool | 是否输出可选扩展字段 `characters`，默认 `false`，见第 5 节。 |
| `subject_mix` | object \| null | 预留扩展：generate 的人数分布要求，默认 `null`（按第 9 节默认分布）。形如 `{"solo": 3, "multi": 1, "no_humans": 0}`，键为条数；给出时各值之和应等于 `count`，按它输出，不再套用默认分布。产品当前不发送此字段。 |

`brief` 是该组的主题与特征描述（可含场景、人物特征、构图倾向）。`count` 是要生成的条目数，应为正整数，上限 [待确认]，超过 50 时建议程序拆分。

输入条目（complete / clean）字段均可缺失：`tag`（string[]）、`expand_nl`（string）、`nl`（string）、`disable`（忽略）。`tag` 为 `[]`、`expand_nl` 或 `nl` 为 `""`，均视为缺失。

以下情况返回 `invalid_input`：JSON 无法理解、`task` 非法、generate 缺 `brief` 或 `count` 非正整数、complete / clean 没有任何条目。

## 3. 输出契约

返回单个 JSON 对象，顶层字段如下：

```json
{
  "status": "ok",
  "groups": [
    {
      "name": "组名，照抄输入",
      "prompts": [
        {
          "index": 0,
          "tag": ["1girl", "solo", "long hair"],
          "expand_nl": "She sits on a bench.",
          "nl": "A girl with long hair sits alone on a bench.",
          "aspect": "portrait",
          "disable": false,
          "changes": ["removed forbidden tag: masterpiece"]
        }
      ]
    }
  ],
  "issues": [
    {"group": "组名", "index": 0, "code": "forbidden_dropped", "detail": "说明"}
  ]
}
```

- `status` 取值：`ok`（全部完成）、`partial`（部分条目未能完成，见 `issues`）、`refused_nsfw`、`invalid_input`。
- `reason`：仅非 `ok` 时输出，一句话说明原因。`ok` 时省略。
- `prompts[].aspect` 取值只能是 `landscape`、`portrait`、`square`，判定见第 8 节。
- `prompts[].disable` 恒为 `false`。
- `index`：仅 complete / clean 输出，值为该条目在**输入的同一组 `items` 中的 0 起始位置**。generate 不输出 `index`。
- `changes`：仅 clean 输出，string 数组，每条一句英文短语说明一处改动；未改动的条目输出 `[]`。
- `characters`：可选扩展，仅 `include_characters: true` 时输出，见第 5 节。
- `issues` 无问题时输出 `[]`。generate 模式的 `index` 指该组 `prompts` 内的位置。

### 3.1 顺序与完整性

- complete / clean：每组 `prompts` 的数量与顺序必须与输入 `items` 一致。某条无法处理时，仍在原位置输出，字段保留输入原值（缺失字段补 `[]` 或 `""`），并写入 `issues`，整体 `status` 为 `partial`。
- generate：每组 `prompts` 数量等于 `count`。实在生成不足时返回已有条目，`status` 为 `partial`，issue 用 `count_short`。
- `groups` 的顺序、`name` 与输入一致。输入没有 `name` 时输出 `""`。

### 3.2 issue code

| code | 含义 |
|------|------|
| `forbidden_dropped` | 输入中的 tag 或文本命中禁用词，已丢弃或改写掉 |
| `nsfw_item` | 单条明确 nsfw 内容（sfw 策略下），该条未处理 |
| `empty_item` | 条目缺全部信息，无法补全 |
| `conflict_tag_nl` | tag 与已有 nl 或 expand_nl 矛盾，complete 模式下未改写，仅报告 |
| `tag_out_of_range` | 无法使 tag 数量落入 `tag_range`（例如内容过少） |
| `count_short` | generate 返回条数少于 `count` |
| `kept_existing` | complete 模式下保留了已有字段，未按 `tag_range` 等规则调整 |

## 4. 三种任务模式

### 4.1 generate

按每组 `brief` 生成 `count` 条新条目。

- `brief` 里的主题、人物特征、场景、构图倾向都要体现；`brief` 没说的部分自行补全，同组内按第 9 节拉开差异。
- 每条三个字段都必须填写：`tag` 非空，`expand_nl` 与 `nl` 非空字符串。
- `tag` 数量落在 `tag_range` 内。
- `nl` 必须覆盖 `tag` 的全部核心内容（人数、外貌、服饰、动作、场景、视角、光线）；`expand_nl` 是追加信息，不重复 `tag` 已表达清楚的内容。
- 多组输入时各组独立生成，组间不要求差异，组内要求差异。

### 4.2 complete

补全缺失字段，已有字段保留原意。

| 输入缺失 | 处理 |
|----------|------|
| 只缺 `tag` | 由 `expand_nl` 与 `nl` 推出 tag，只写文本里有依据的内容 |
| 只缺 `nl` | 由 `tag` 与 `expand_nl` 合并写成完整自然语言 |
| 只缺 `expand_nl` | 写出 tag 难以表达的空间关系与动作细节；`tag` 已足够清楚时，写一句最少的补充，不重复 `nl` |
| 缺 `tag` 与 `nl` | 以 `expand_nl` 为依据推 tag，再写 `nl` |
| 缺 `nl` 与 `expand_nl` | 以 `tag` 为依据写 `nl`，再写 `expand_nl`；`tag` 没有的细节不得凭空加入过多，只补常见关系性信息 |
| 三项都缺 | 返回该条原位置，issue `empty_item`，`status` 为 `partial` |

规则：

- **不得改写已存在字段**，包括措辞、顺序、tag 大小写。`allow_rewrite: true` 时才允许调整，且只为满足禁用词、tag 写法规范或矛盾修正，并在 `changes` 中说明。
- 已存在字段命中禁用词时：`allow_rewrite` 为 `false` 则原样保留，issue 用 `forbidden_dropped`，detail 写「kept as-is, use clean task」；新补出的字段仍须不含禁用词。
- 已有 `nl` 可能由程序预先填入（规则：只有 `expand_nl` 时程序令 `nl` 等于 `expand_nl`）。此时 `nl` 与 `expand_nl` 相同且 `tag` 缺失，只推 `tag`，不改写 `nl`。
- 补出的 `tag` 数量尽量落入 `tag_range`；已有文本信息不足时少于下限可以接受，报 `tag_out_of_range`。
- 补出的新字段不得与已有字段矛盾；已有字段彼此矛盾时不改写，报 `conflict_tag_nl`。
- complete 不输出 `changes`。

### 4.3 clean

清洗已有条目，返回清洗后的条目，并为每条输出 `changes`。

清洗范围：

1. 去除 tag、`expand_nl`、`nl` 中的画风、画师、媒介、质量、滤镜、画幅类词（第 7 节）。`nl` 中含这类词的句子，改写为只描述画面内容的句子；整句都是画风描述的直接删除。
2. 规范 tag 写法：下划线改空格，去权重括号与数值，去首尾空格，大小写统一为小写（消歧括号内容同样小写），去重，保持原有先后顺序。
3. 修正 tag 与文本矛盾：以多数一致的一侧为准。例如 tag 为 `1girl` 而 `nl` 写了两人，且 `expand_nl` 也只提一人，则改 `nl`。无法判断多数时，保留 `tag`，改文本，并在 `changes` 说明。
4. 缺失字段不在 clean 中补全；除非清洗后字段变空且可由另外两项直接推出，此时补出并写入 `changes`。
5. 保持原意：不加入新的人物、动作、场景。

`changes` 的格式：英文短语，描述改动类型与对象，例如 `removed style tag: oil painting`、`normalized tag: long_hair -> long hair`、`rewrote nl: removed "in the style of" clause`。没有改动输出 `[]`。

## 5. 多人物表达与 characters 扩展

产品条目没有 Base / Character 分栏字段。默认输出仍然只有 `tag`、`expand_nl`、`nl`，人物归属按下面方式承载。

### 5.1 tag 内

- 先写人数与性别（`2girls`、`1girl`、`1boy`、`no humans`），单人场景加 `solo`，多人场景不加 `solo`。
- 一个人物的外貌、服饰、表情 tag 连续写完，再写下一人物，不穿插。
- 人物区分借助 NAI 的 tag 本身，不自造归属前缀。能用 `girl on left` 之类的可见位置 tag 时才用，拿不准不写，把归属放进 `expand_nl`。
- 不使用 `source#` / `target#` / `mutual#` 等动作标记。[待确认] 是否需要由用户另行开启。

### 5.2 expand_nl 内（多人物时）

- 按人物分句：一个人物一句或一组句子，开头明确位置或身份，例如 `The girl on the left ...`、`The taller woman ...`。
- 每句写该人物自己的属性、手、视线与动作，再用单独一句写人物之间的接触（谁的哪只手握住什么）。
- 属性绑定到人物：服饰颜色、发色、手持物都跟在人物主语后面，避免「红色和蓝色」这类悬空并列。
- 位置用文字（left / right / foreground / behind）写明，不依赖顺序。

### 5.3 nl 内

沿用同一套人物分句，整体先用一句交代人数与场景，再逐人物展开，最后写共用的光线与视角。

### 5.4 可选扩展 characters

`include_characters: true` 时，每条输出额外增加：

```
"characters": [{"label": "left girl", "prompt": "blonde hair, blue jacket, extending right hand"}]
```

- `label`：该人物的简短可见称呼；`prompt`：只含该人物的 tag 或短句，逗号分隔。
- 仅当条目有 2 个及以上人物时输出；单人或无人时输出 `[]`。
- `characters` 不替代 `tag` / `expand_nl` / `nl`，三者仍须完整。
- 默认不输出该字段。产品入库是否保存此字段 [待确认]。

## 6. 内容写法

### 6.1 Natural-Hybrid 分工

tag 负责锚定，句子负责关系。判据：tag 说「有什么」，`expand_nl` 说「谁在哪、手怎么放、朝向哪、和谁接触」。

- 人数、发色瞳色、主要服饰、场景名、取景、视角用 tag 锚定。
- 握持、坐姿、视线、透视、遮挡、前后景、多人互动这类 tag 容易写歪的内容，放进 `expand_nl`。
- `expand_nl` 不重复 tag 已说清的事实。已有 `red hair` 就不写 "her hair is red"。
- `nl` 则相反：它必须独立成立，把 tag 的全部核心内容写全，不依赖 tag 阅读。
- tag 与两段文本不能互相矛盾，同一属性在三处取值一致。

### 6.2 tag 写法

- 空格分词（`long hair`），全小写，不带下划线，不带权重括号或数值。
- 消歧括号原样保留（`ganyu (genshin impact)`、`bow (weapon)`）。角色 tag 与版权角色只在输入已带时保留，不自行添加。
- tag 顺序：人数与性别、角色（仅输入带时）、外貌与服饰、姿势动作与表情、场景、时间与光线、取景视角、画面内实体效果（`wind`、`steam`、`falling leaves`）。
- 取景只用一个短语（`full body`、`cowboy shot`、`upper body`、`close-up`），视角可另加一个（`from above`、`from side`、`dutch angle`）。
- 人物可见且脸部入画时，必须有一个表情 tag；脸不入画则不写。
- `no humans` 场景不写人物外貌、姿势、表情，也不在文本里提到人。
- 背景要写具体实体（`bookshelf`、`wooden table`、`distant mountains`），不用 `detailed background`、`indoors` 这类空泛词单独撑场景。

### 6.3 细节规则（主要写入 expand_nl 与 nl）

- 手：说明哪只手、掌心朝向、手指弯曲方式、接触的物体与部位。握持状但无物体时写 `fingers curved around an empty space`，不凭空添加物体。
- 坐姿：说明臀部支撑面、躯干倾斜、大腿方向、膝盖弯曲、脚的位置。翘腿明确写一条腿搭在另一条腿上，不写成斜躺。
- 视线：写视线落点（对着观众、看向左侧的某物、低头看手中的物体），与头部朝向分开说。
- 透视：近处部位放大、肢体前缩、遮挡脸部等关系要具体，例如 `her foot extends toward the viewer`。仅在视角真有明显透视时写。
- 服饰层次：由内到外、由上到下说明领口、袖型、腰线、下摆、鞋袜、饰品；材质只写可见质感（针织纹理、布料褶皱、皮革反光），不写渲染质感。
- 光源分区：主光来自哪一侧，落在头发、脸、衣物的哪里；受光面与阴影面分别写。光源与发光物体对应，不堆叠矛盾的光线 tag。
- 表情细节：眉形、眼睑、嘴角、唇是否张开、头部倾角，不只靠 `smile`。
- 前中后景：用实体与位置表达，不使用虚化类词（见第 7 节）。

### 6.4 多人属性绑定

- 每个人物的发色、服饰、手持物、动作只绑定在该人物的主语后面。
- 两人以上的 `expand_nl` 先写位置，再逐人物，再写互动。互动句必须写清谁对谁、哪只手、碰到什么。
- tag 内一个人物写完再写下一个，不交错。

## 7. 禁用词（画风隔离）

规则同 `skills/prompt-composer/SKILL.md` 3.5 节：类别 A–J、匹配规则、三条例外（物体属性、消歧括号、画面范围 frame）与兜底规则全部适用，不在此重复全表。拿不准按禁用处理。

适用范围：`tag`、`expand_nl`、`nl`、`characters` 全部字段，以及输入中已有的内容。nl 里的同义改写同样禁用。

最关键的 20 条示例：

- 画师与引用：`artist:xxx`、`by xxx`、`in the style of xxx`、`signature`、`watermark`
- 质量与评级：`masterpiece`、`best quality`、`very aesthetic`、`absurdres`、`rating:general`
- 媒介：`oil painting`、`watercolor`、`digital art`、`illustration`、`3d render`
- 技法：`lineart`、`flat color`、`cel shading`、`soft shading`、`painterly`
- 光学与滤镜：`blurry background`、`bokeh`、`depth of field`、`cinematic lighting`、`lens flare`
- 流派：`anime style`、`realistic`、`pixel art`

nl 与 expand_nl 中的典型违规写法：`in the style of`、`anime style`、`painted in watercolor`、`clean sharp linework`、`high-quality render`、`soft dreamy atmosphere`、`cinematic composition`。

额外约束：

- 画幅类词不得出现在任何字段：`landscape orientation`、`vertical composition`、`16:9` 等（3.5 H 类）。图幅只通过 `aspect` 字段表达。取景 tag（`wide shot`、`full body`、`upper body`、`close-up`）允许。
- 文字类标记不得出现：`speech bubble`、`text`、`logo` 等。
- 输入的 `brief` 或条目中含禁用词：generate 时直接忽略该词，并在 `issues` 记 `forbidden_dropped`；不得换同义词保留其效果。
- 全部内容都是禁用词、清洗后无剩余可用信息：该条记 `empty_item`。

## 8. aspect 判定

每条输出必须有 `aspect`。规则同 `skills/prompt-composer/SKILL.md` 3.6 节：

1. `aspect_hint` 是合法三值之一：照抄，不再判断，不因与构图不一致而改 tag。
2. 否则按下表自行判断，命中即停：

| 顺序 | 条件 | aspect |
|---|---|---|
| 1 | 两个及以上角色左右并排或横向排开 | `landscape` |
| 2 | `wide shot`、`very wide shot`，或以地平线、天际线、城市远景、原野等横向延展物为主；`no humans` 风景同理 | `landscape` |
| 3 | 躺姿或横向主体：`lying`、`on back`、`on side`、`on stomach`、`reclining` | `landscape` |
| 4 | 脸部或头肩特写：`close-up`、`portrait`、`face`；左右对称构图；`no humans` 且主体是单个居中物件 | `square` |
| 5 | 单人全身、站姿、坐姿、`upper body`、`cowboy shot`，或竖长主体 | `portrait` |
| 6 | 其余或拿不准 | `portrait` |

说明：

- prompt-composer 3.6 的第 1 行（`scenery_first`）在本 skill 没有对应输入，不适用。
- `aspect` 与 tag 不冲突即可，不得为配合 `aspect` 增删 tag。
- 同一组 generate 条目的 `aspect` 不要求一致，应随构图自然分布；`aspect_hint` 给定时整批都照抄。
- complete / clean 模式：`aspect` 也按上述规则对清洗后的 tag 判断；输入条目自带 `aspect` 字段时 [待确认] 是否优先保留，当前行为：忽略输入值，重新判断。

## 9. 多样性（generate 同组多条）

同一组内的条目要在以下维度拉开差异，不要只换发色或服饰：

- 人数（默认分布，`brief` 或 `subject_mix` 另有要求时以它们为准）：
  - 以单角色为主（`1girl` / `1boy` + `solo`），约占同组 70%–80%。
  - 少量多角色（2–3 人），约占 20%–30%；`count` ≤ 5 时最多 1 条。
  - 尽量不出纯景色（`no humans`）。只有 `brief` 明确要求风景、物件或无人画面时才输出。
- 取景：`full body`、`cowboy shot`、`upper body`、`close-up`、`wide shot` 分布使用。
- 视角：正面、侧面、`from above`、`from below`、`from behind` 轮换。
- 场景与时间：不同地点、不同光线（白天、黄昏、夜晚、室内灯光）。
- 动作：站、坐、走、持物、互动各异。

自检清单（输出前逐项过一遍）：

- 人数分布符合 `subject_mix`；未给出时符合上面的默认分布（单角色为主、少量多角色、无纯景色）。
- 同组任意两条的「人数 + 取景 + 视角」组合不完全相同。
- 同组内相同场景最多出现 2 次，且动作不同。
- 条目数等于 `count`，每条 `tag` 数量在 `tag_range` 内。
- 每条 `nl` 覆盖 `tag` 的核心内容，`expand_nl` 没有重复 `tag`。
- 三个字段中没有画风、质量、画幅、权重、负面表述。
- 多人条目：属性只绑定在对应人物后，tag 内人物不交错。
- `brief` 要求的特征（主题、人物特征）每条都体现，而不是只体现在第一条。

## 10. 评级与拒绝

- `rating_policy` 为 `sfw`（或缺省）时，输入 `brief` 或条目含明确性行为或裸露要求，返回 `status: "refused_nsfw"`，`groups: []`，`issues: []`，`reason` 写一句英文。不清洗、不改写、不返回部分结果。
- generate 的多组输入里只要有一组含明确 nsfw 要求，整体仍返回 `refused_nsfw`，不对其它组部分生成。
- complete / clean 中单条命中 nsfw：整体返回 `refused_nsfw`。[待确认] 是否允许改为逐条 `nsfw_item` 并返回 `partial`，当前行为是整体拒绝。
- `nsfw_allowed` 时按输入如实写，仍然遵守第 7 节，且不添加输入没有的性内容。
- 泳装、校服、日常亲密互动等不构成明确 nsfw，不拒绝。

拒绝输出示例：

```json
{"status":"refused_nsfw","groups":[],"issues":[],"reason":"brief requests explicit sexual content under sfw policy"}
```

invalid_input 示例：

```json
{"status":"invalid_input","groups":[],"issues":[],"reason":"generate task requires a positive integer count"}
```

## 11. 示例

示例中的 JSON 均为单行压缩，仅为节省篇幅；实际输出同样只能是单个 JSON 对象，无围栏。

### 示例 1：generate 多组

输入：

```json
{"task":"generate","rating_policy":"sfw","tag_range":[8,14],"language":"en","groups":[{"name":"cafe","count":2,"brief":"two girls at a cafe"},{"name":"night street","count":1,"brief":"a lone boy walking home in the rain"}]}
```

输出：

```json
{"status":"ok","groups":[
{"name":"cafe","prompts":[
{"tag":["2girls","blonde hair","blue jacket","brown hair","gray sweater","sitting","holding cup","wooden table","cafe","window","sunlight","smile","cowboy shot"],"expand_nl":"The girl on the left has blonde hair and wears a blue jacket; she supports the base of a white cup with her right hand. The girl on the right has brown hair and wears a gray sweater; she grasps the cup handle with her left hand. Their hands meet around the same cup above the table, and both look at it.","nl":"Two girls sit side by side at a wooden table in a cafe, shown from the waist up at eye level. The girl on the left has blonde hair and a blue jacket. The girl on the right has brown hair and a gray sweater. They hold one white cup together above the table, the left girl supporting its base and the right girl holding its handle, and both smile. Sunlight comes in through the window on the left.","aspect":"landscape","disable":false},
{"tag":["2girls","black hair","green sweater","sitting","red hair","white apron","standing","holding tray","cafe counter","ceiling lamp","looking at another","smile","from side"],"expand_nl":"In the foreground, the girl with black hair and a green sweater sits at a small table, turned slightly to the right and looking up at the other girl. Behind her, the girl with red hair and a white apron stands leaning forward and holds a round tray with both hands. A cafe counter fills the background on the left.","nl":"Two girls in a cafe, seen from the side. In the foreground a girl with black hair and a green sweater sits at a small table and looks up at the girl behind her. The girl with red hair and a white apron stands behind her, leaning forward slightly and holding a round tray with both hands, and both of them smile. A cafe counter and a ceiling lamp are visible in the background, giving the room a warm glow.","aspect":"portrait","disable":false}]},
{"name":"night street","prompts":[
{"tag":["1boy","solo","short hair","black jacket","holding umbrella","hand in pocket","walking","rain","night","street lamp","wet road","from behind","full body"],"expand_nl":"He walks along a wet road with an open umbrella held above his head in his right hand and his left hand in his jacket pocket. A street lamp on the right lights the rain falling around him.","nl":"A boy with short hair and a black jacket walks away from the viewer along a wet road at night, seen from behind at full length. He holds an open umbrella above his head in his right hand and keeps his left hand in his jacket pocket. Rain falls around him, and a street lamp on the right lights the road.","aspect":"portrait","disable":false}]}
],"issues":[]}
```

### 示例 2：complete，缺 tag

两条条目都只有 `expand_nl` 与 `nl`。只推 `tag`，两段文本原样保留，输出带 `index`（输入 `items` 内的 0 起始位置），不输出 `changes`。

输入：

```json
{"task":"complete","rating_policy":"sfw","tag_range":[8,16],"groups":[{"name":"park","items":[{"expand_nl":"She sits at the left end of a bench with her right leg crossed over her left, holding an open book in both hands.","nl":"A girl with short brown hair and a white shirt sits on a park bench in daylight, reading an open book with her legs crossed. Trees stand behind the bench."},{"expand_nl":"The boy stands in the foreground, points up with his right hand and smiles. Behind him, the girl looks at the sky with her hands behind her back.","nl":"A boy and a girl stand on a rooftop at dusk. The boy, in front, points upward with his right hand and smiles, and the girl behind him looks at the sky with her hands behind her back."}]}]}
```

输出：

```json
{"status":"ok","groups":[{"name":"park","prompts":[{"index":0,"tag":["1girl","solo","short hair","brown hair","white shirt","sitting","crossed legs","holding book","looking down","closed mouth","bench","park","tree","day","full body"],"expand_nl":"She sits at the left end of a bench with her right leg crossed over her left, holding an open book in both hands.","nl":"A girl with short brown hair and a white shirt sits on a park bench in daylight, reading an open book with her legs crossed. Trees stand behind the bench.","aspect":"portrait","disable":false},{"index":1,"tag":["1boy","1girl","standing","pointing up","hands behind back","looking up","smile","rooftop","dusk","sky","cowboy shot"],"expand_nl":"The boy stands in the foreground, points up with his right hand and smiles. Behind him, the girl looks at the sky with her hands behind her back.","nl":"A boy and a girl stand on a rooftop at dusk. The boy, in front, points upward with his right hand and smiles, and the girl behind him looks at the sky with her hands behind her back.","aspect":"portrait","disable":false}]}],"issues":[]}
```

### 示例 3：complete，只有 tag

条目只有 `tag`，缺 `nl` 与 `expand_nl`。先由 `tag` 写 `nl`，再写一句最少的 `expand_nl`，只补常见关系性信息，不加入 `tag` 没有的人物或物体。`tag` 原样保留，包括顺序。

输入：

```json
{"task":"complete","rating_policy":"sfw","tag_range":[8,16],"name":"snow","items":[{"tag":["1girl","solo","silver hair","long hair","red scarf","black coat","standing","looking at viewer","smile","snow","falling snow","upper body"]}]}
```

输出：

```json
{"status":"ok","groups":[{"name":"snow","prompts":[{"index":0,"tag":["1girl","solo","silver hair","long hair","red scarf","black coat","standing","looking at viewer","smile","snow","falling snow","upper body"],"expand_nl":"She faces the viewer directly, with snow landing on her scarf and the shoulders of her coat.","nl":"A girl with long silver hair, a red scarf and a black coat stands in falling snow and smiles at the viewer, shown from the waist up.","aspect":"portrait","disable":false}]}],"issues":[]}
```

### 示例 4：clean

输入的 `tag` 含质量词、媒介词、画师标签、下划线、权重括号和重复项；`expand_nl` 带一句线条描述；`nl` 带画风引用，且写成两人，而 `tag`（`1girl`）与 `expand_nl`（`She`）都是一人，按多数一致以 `tag` 为准，改 `nl`。`tag` 清洗后保持原有先后顺序。输入条目用顶层 `items` 简写，输出组名为 `""`。

输入：

```json
{"task":"clean","rating_policy":"sfw","tag_range":[8,16],"items":[{"tag":["masterpiece","best quality","1girl","solo","Long_Hair","(red_ribbon:1.2)","watercolor","artist:kuroda","sitting","tree","hands on own knees","smile","full body","1girl"],"expand_nl":"She sits under a tree with her hands on her knees. Clean sharp linework.","nl":"A masterpiece anime style watercolor picture of two girls with long hair and red ribbons sitting under a tree and smiling, in the style of kuroda."}]}
```

输出：

```json
{"status":"ok","groups":[{"name":"","prompts":[{"index":0,"tag":["1girl","solo","long hair","red ribbon","sitting","tree","hands on own knees","smile","full body"],"expand_nl":"She sits under a tree with her hands on her knees.","nl":"A girl with long hair and a red ribbon sits under a tree and smiles.","aspect":"portrait","disable":false,"changes":["removed forbidden tag: masterpiece","removed forbidden tag: best quality","removed style tag: watercolor","removed artist tag: artist:kuroda","normalized tag: Long_Hair -> long hair","normalized tag: (red_ribbon:1.2) -> red ribbon","removed duplicate tag: 1girl","rewrote expand_nl: removed sentence \"Clean sharp linework.\"","rewrote nl: removed quality, style and artist wording; changed two girls to one girl to match tag"]}]}],"issues":[{"group":"","index":0,"code":"forbidden_dropped","detail":"dropped quality, medium, artist and linework wording from tag, expand_nl and nl"}]}
```

### 示例 5：invalid_input（complete 无条目）

第 10 节已覆盖 `refused_nsfw`（sfw 下 brief 含明确性内容）与 `invalid_input`（generate 的 `count` 非法）。这里补第 10 节没有的触发条件：complete 的 `items` 为空数组。仍输出 `groups: []`、`issues: []` 与 `reason`。

输入：

```json
{"task":"complete","rating_policy":"sfw","items":[]}
```

输出：

```json
{"status":"invalid_input","groups":[],"issues":[],"reason":"complete task requires at least one item"}
```

## 12. 反例

以下输出均不合格，不得模仿。

### 反例 1：tag 带权重括号与画风词，nl 含画风引用

输入为 generate，`brief` 为 `a girl reading under a tree`。错误输出：

```json
{"status":"ok","groups":[{"name":"","prompts":[{"tag":["masterpiece","1girl","solo","(long hair:1.2)","watercolor","reading","tree"],"expand_nl":"She holds a book. Soft dreamy atmosphere.","nl":"A watercolor picture in the style of a famous painter, showing a girl reading under a tree.","aspect":"portrait","disable":false}]}],"issues":[]}
```

原因：

- `masterpiece`、`watercolor` 属于质量词与媒介词，不得进 `tag`（第 7 节）。
- `(long hair:1.2)` 是权重语法，应写 `long hair`。
- `expand_nl` 的 `Soft dreamy atmosphere.` 与 `nl` 的 `watercolor`、`in the style of` 都是画风或氛围描述，禁用。
- 生成时命中禁用词应忽略并记 `forbidden_dropped`，而不是写进字段。
- 另一类常见错误：在 JSON 外加 markdown 围栏或解释文字。输出必须只有单个 JSON 对象。

### 反例 2：complete 改写了已有字段

输入条目 `{"tag":["1girl","Solo","red hair","sitting"],"nl":"A girl with red hair sits alone."}`，`allow_rewrite` 缺省（`false`）。错误输出：

```json
{"status":"ok","groups":[{"name":"","prompts":[{"index":0,"tag":["1girl","solo","red hair","sitting","looking at viewer"],"expand_nl":"She sits with her knees together.","nl":"A young woman with vivid red hair is sitting by herself.","aspect":"portrait","disable":false}]}],"issues":[]}
```

原因：

- 已有 `tag` 的 `Solo` 被改成小写，且追加了输入没有的 `looking at viewer`；`allow_rewrite` 为 `false` 时已有字段须原样保留，包括大小写与内容。
- 已有 `nl` 被改写成另一句话。complete 只补缺失字段，此处只应新增 `expand_nl`。
- 正确做法：`tag` 与 `nl` 照抄输入（含 `Solo`），仅补 `expand_nl`；`Solo` 的大小写问题留给 clean 任务处理。complete 下是否需要为此记 issue [待确认]。

## 13. 自检清单

- 输出只有一个 JSON 对象，无围栏、无解释文字，JSON 合法，无注释。
- `status`、`reason`、`issues` 与实际结果一致；非 `ok` 时有 `reason`。
- complete / clean：`prompts` 数量与顺序同输入，`index` 为输入位置。
- complete 未改写已有字段；clean 每条都有 `changes`，complete 与 generate 没有。
- 三个字段（及 `characters`）无画风、质量、画幅、权重、负面表述。
- `aspect` 只取三个合法值；`disable` 恒为 `false`。
