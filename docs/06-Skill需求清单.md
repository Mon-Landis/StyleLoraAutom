# 06 Skill 需求清单

> 运行时 Skill = 产品调用 LLM 时注入的提示词模板，放在产品层 `skills/<name>/SKILL.md`，随产品分发。
> 开发 Skill = 给 opencode 开发 agent 用的知识，放在 `.opencode/skills/`，由我从现有文档整理，不需要你准备。

## 1. 通用约定（所有运行时 Skill）

每个 Skill 需要包含：

| 部分 | 内容 |
|---|---|
| 目的 | 一句话说明这个 Skill 在哪一步、解决什么 |
| 输入占位符 | 产品会替换的 `{{变量}}`，列出名字和含义 |
| 规则 | 必须做 / 禁止做 |
| 输出契约 | 严格 JSON schema，产品侧会校验，不合格重试 |
| 正反例 | 至少 2 个正例 + 1 个反例（反例写明错在哪） |
| 模式开关 | 受 `{{dataset_mode_policy}}`（画风/角色）和 `{{rating_policy}}`（sfw/nsfw）影响的部分要单独标出 |

产品侧固定追加的内容（Skill 里不用写，写了也会被覆盖）：JSON 输出契约的最终版、受保护触发词、当前模式规则。这一点沿用 TagCleaner 的做法。

## 2. 运行时 Skill

| # | 名称 | 来源 | 用在哪 | 优先级 |
|---|---|---|---|---|
| R1 | `prompt-composer` | 新写 | 提示词生产：特征组合 → 可直接提交 NAI 的内容段提示词（可选自然语言润色） | MVP |
| R2 | `vision-captioner` | 迁移 TagCleaner 阶段一 | 视觉反推：输出辅助 tags + 一段自然语言描述 | MVP |
| R3 | `dataset-cleaner` | 迁移 TagCleaner 阶段三 | 数据清洗：WD14 为主证据，合并视觉候选，输出最终 tags + 自然语言 | MVP |
| R4 | `prompt-pyramid` | 迁移 TagCleaner 现有 | 最终 tags 归一与排序 | MVP |
| R5 | `rating-classifier` | 新写 | 分级模型：判定内容 rating | MVP |
| R6 | `character-tag-auditor` | 迁移 TagCleaner 现有 | A-pose 角色审计 | 预留（角色线） |

### R1 prompt-composer（初版已完成：`skills/prompt-composer/`）
- 基于用户的 anima-prompt skill 改写（doc-writer 执行）：保留 Tag Order、表情补齐、可见性/出画推理、view 措辞、多人物绑定规则、scenery-priority（改为 `composition_mode=scenery_first`）、poses 查表（已剔除 NSFW 条目）；删除角色查表、NSFW、权重、负面、修改请求、人工输出格式
- 输入 JSON：`features`（维度 → tag 数组）、`rating_policy`、`nl_enabled`、`composition_mode`（normal / scenery_first）、`tag_range`
- 输出 JSON：`{ status: ok|refused_nsfw|invalid_input, tags, natural_language, out_of_frame, added, dropped[{tag,reason}], reason? }`
- 目标是 NAI：空格分词、默认无权重、严禁画风/画师/媒介/质量词
- 9 个 JSON 示例块已用解析器校验通过
- 待复核：doc-writer 自行做的 12 项判断（冲突优先级、禁用词补充、scenery 簇位置等），见第 5 节

### R0 通用规则：未验证 rating 的请求标注（R1 / R2 / R3 / R5）

决议：rating 无法由显式值或关键词确定、也没有配置分级模型时，按普通线路处理，但在请求里加 `rating_verified: false`。所有接收图片或标签的 Skill 都要包含这条规则：

> 若 `rating_verified=false` 且内容含明确的性或裸露内容，立即返回 `{"status": "refused_nsfw", "reason": "..."}`，不做任何处理或净化改写。

产品收到 `refused_nsfw` 后把该图 rating 升为 `questionable`，改走 nsfw 线路；nsfw 角色为空时跳过 LLM 步骤并保留原值。

### R2 vision-captioner
- 输入：图片、`{{tags}}`（已有原始/WD 标签，可空）、`{{dataset_mode_policy}}`
- 规则：画风模式下不描述画风/笔触/媒介；描述只写可见内容
- 输出：`{ "vision_tags": [...], "vision_description": "..." }`

### R3 dataset-cleaner
- 输入：`{{tags}}`、`{{vision_tags}}`、`{{merged_tag_candidates}}`、`{{vision_description}}`、`{{protected_dataset_tags}}`、`{{original_natural_language}}`（新增：来自原始提示词的自然语言部分）
- 规则：保留原始 tag 拼写与顺序；LLM 候选只在有视觉描述支持时加入；画风模式强制删画风类；触发词保持首位
- 输出：`{ "final_tags": [...], "natural_language": "...", "removed": [...], "added": [...] }`

### R4 prompt-pyramid
- 现有内容直接迁移；需要确认是否与 docs/03 的 caption 顺序一致（触发词 → 主体 → 动作表情 → 服装 → 构图 → 场景 → 光照）

### R5 rating-classifier
- 输入：`{{tags}}`、`{{natural_language}}`，可选图片（视觉分级时）
- 规则：输出 Danbooru 四级之一；拿不准时返回 `uncertain`，不要猜
- 输出：`{ "rating": "general|sensitive|questionable|explicit|uncertain", "reason": "..." }`
- 只在关键词匹配没得出结论时调用

## 3. 需要你准备的数据（不是 Skill，但同样要准备）

| # | 数据 | 用途 | 格式建议 |
|---|---|---|---|
| D1 | NSFW 关键词词库 | rating 关键词匹配，命中即升级 | 每行一个 tag，附目标等级：`tag<TAB>questionable` |
| D2 | 画风/画师/媒介词表 | 清洗阶段的保险 blocklist | 每行一个 tag；画师类可以用 Danbooru artist 分类自动生成，这里只放补充项 |
| D3 | 特征字典样例 | 提示词生产测试 | 每个维度一个文件，或一个 YAML：维度 → 词条（带 rating） |
| D4 | 标签翻译表（可选） | 界面翻译 | 迁移 TagCleaner 的 `translations.json` 即可 |

## 4. 开发 Skill（我来整理）

| 名称 | 内容 |
|---|---|
| `project-conventions` | 目录结构、数据模型、幂等与日志约定、密钥规则 |
| `nai-launcher-mcp` | docs/02 的精简版：调用链路、必传参数、不可重试规则 |
| `danbooru-nai-prompt` | Danbooru tag 规范 + NAI 提示词语法，R1/R3 也会引用 |
