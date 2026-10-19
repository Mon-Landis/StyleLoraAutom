# Anima 写法参考（预留）

状态：参考资料，不被代码加载，也不随 SKILL.md 注入 LLM（运行时只读 SKILL.md）。当前产品只提交 NovelAI，本文件在产品新增 Anima 目标时参考。与 SKILL.md 冲突时以 SKILL.md 为准。来源：用户自用版 anima-prompt（devcache）。

## 1 文本形态（与 NAI 的差异）

- NAI：tag 与自然语言混写在同一逗号串里。
- Anima：tag 与自然语言分开。第 1 行 tag（逗号分隔，15–40 个），空行，其后一段英文自然语言。
- tag 行与 NL 段之间统一为一个空行（docs/05 §10.5 现行标准；自用版的「空两行」不采用）。
- 自用版多人时，tag 区与 NL 区都按人物用空行分块。composer 的 `natural_language` 允许分段（docs/08 U26），多人可按人物用一个空行分段；tag 区仍是一行，不分块。
- 三种 `text_mode` 在 Anima 下：`tag` 单行 tag；`tag_nl` tag 行 + 空行 + NL 段；`nl` 一段英文 NL。

## 2 Tag 顺序

与 SKILL.md 3.1 相同（人数性别、角色、外貌、姿势表情、场景、时间光线、视角构图、额外效果）。自用版另有两条，composer 因没有角色表而未采用：

- 已知角色用带系列后缀的 Danbooru 角色 tag。
- 服装只在与角色标准服装不同时输出，并加 `alternate_costume`。

## 3 权重（仅供识别，composer 不输出）

- Anima 权重写法 `(tag:1.4)`，自然语言短语也可加权；要求加权时 tag 与对应 NL 短语同时加。
- NAI 的 `1.3::x::`、`{}`、`[]` 与 Anima 不通用。转换规则见 docs/F28，由确定性转换器完成，LLM 不转换。

## 4 画师

Anima 画师触发词写 `@name`：下划线换空格，括号转义（`hyuk_(yeayeo)` → `@hyuk \(yeayeo\)`），加权写 `(@name:w)`，权重不缩放。两个及以上画师时自用版建议用 Anima-Artist-Mixer 插件（人工流程，不并入正文）。转换由转换器完成，content 段仍不得出现任何画师。

## 5 多人特征串漏案例（来自自用版注意事项，已精简）

现象：两个角色同框，其中一人的光环、光翼被画到另一人身上。

- 把高辨识特征写进 tag 行，或给该特征、该角色名加权，都会加重串漏。
- 有效做法：特征移出 tag，只在 NL 里用归属句写；另一人用独有的身份 tag（如兽耳、兽尾）写进自己的块。
- 需要加权时，加给被误填的那个角色的名字（自用版起点约 1.6，超过 3.0 仍不稳就换办法），不加给会漏的特征。composer 不允许权重，所以只采用「移出 tag、NL 归属」这一半，写在 SKILL.md 3.3 第 10 条。

## 6 角色拆分（分区提示词，后续做区域提示词时参考）

输出结构：全局提示词；每个角色分「外貌」「动作」两块（动作块不写人数 tag）；场景块（只含环境）；交互动作块（人数 tag 与接触关系放这里）。不虚构原提示词没有的角色或互动。

## 7 风景为主

自用版用 `(scenery:1.4)` 之类 1.3–1.5 的权重；过高会丢人物，只剩风景。composer 的 `scenery_first` 不加权，靠 `wide shot` 与风景 tag 簇实现（SKILL.md 3.4）。

## 8 与 SKILL.md 冲突、未采用的自用版写法

- 额外效果里的 `depth of field`、`blurry background`、`motion lines`：属 3.5 F 类禁用。`small sound effect bubbles` 不在清单中，按 H 类（`speech bubble` 等文字类）与 J 类兜底规则禁用。
- 质量词、`score_*`、年份：禁用（3.5 B 类、G 类）。
- 角色名权重、`@` 画师：content 段禁用。
