# NovelAI V5 写法参考（预留）

状态：参考资料，不被代码加载，也不随 SKILL.md 注入 LLM（运行时只读 SKILL.md）。与 SKILL.md 冲突时以 SKILL.md 为准。来源：用户自用版 nai-prompt（devcache）。其中公开规则沿用自用版 2026-10-15 的核查记录，本次未重新联网核验 [待确认]。

## 1 Base / Character 分栏

NAI V5 客户端有「共用 Base Prompt + 每个角色一个 Character Prompt」的分栏。产品条目目前没有分栏字段，SKILL.md 第 5 节用 `characters` 扩展承载。分栏时的分工：

- Base：人数标签（单人加 `solo`）、核心事件、镜头构图、前中后景、物件环境、光线、人物间关系。
- Character：以 `girl` / `boy` / `other` 开头，只写该角色的外貌、服饰、位置、动作、表情、视线。人数不重复，不加 `solo`。
- 角色栏顺序通常对应画面从上到下、从左到右，不保证前后深度；位置要用文字写明。
- 分栏能减少属性串漏，不保证消除。
- 互动标记 `source#hug`、`target#hug`、`mutual#hug` 可辅助互动，不能代替自然语言。SKILL.md 5.1 不使用，是否开启 [待确认]。
- 最多约 22 个角色（自用版转述）。

## 2 Base 内常用顺序

1. 人数标签（`1girl, solo`、`2boys`、`1girl, 1boy`）
2. 核心动作或视觉事件
3. 镜头与构图
4. 空间、物件、关系（前中后、左右、遮挡、接触）
5. 全局材质与颜色
6. 光线、天气
7. `Text:`（如有）放最后

自用版还有数据集标签、风格媒介、质量复杂度几项，产品内容段禁用，不采用。

## 3 权重语法（仅供识别，SKILL.md 不输出）

- `{x}` 每层约 ×1.05，`[x]` 每层约 ÷1.05。
- `1.3::x::` 数值强调，`::` 关闭区段；`-1::x::` 为负数强调。
- 不使用 Anima 的 `(x:1.3)`。
- 转换见 docs/F28，由转换器完成。

## 4 质量标签与画风

自用版默认不主动加画师、画风、泛化质量词、年份、复杂度标签；自动 Quality Tags 开启时也不重复手写。产品内容段在此基础上更严：全部禁用（SKILL.md 第 7 节）。

## 5 画面文字与容量（本产品不适用，仅记录）

- 画面文字写成 `Text: exact text`，放在 Base 末尾。产品内容段禁止文字类标记，不使用。
- 自用版记录的容量：Curated 约 703、Full 约 1471 个有效 prompt token；文字额度 Curated 374、Full 750 字符（单位与 token 口径不一致）。均来自自用版转述，未核验 [待确认]。

## 6 公开文档（自用版列出）

- https://docs.novelai.net/en/image/models/
- https://docs.novelai.net/en/image/multiplecharacters/
- https://docs.novelai.net/en/image/strengthening-weakening/
- https://docs.novelai.net/en/image/textrendering/
- https://docs.novelai.net/en/image/qualitytags/
