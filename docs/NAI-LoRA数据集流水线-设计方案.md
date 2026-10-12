# NAI → Anima 画风 LoRA 数据集流水线 设计方案

> v0.1 草案。训练环节不在本方案范围，产出终点是「图片 + 同名 txt（正面提示词）」。
> 标注 `[待确认]` 的条目需要到开发环境探测 Aaalice NAI Launcher 的实际接口后才能定稿。

## 1. 目标与范围

用 NAI 批量产出带特定画风的图像，清洗后转成 Anima 可用的训练集。

**交付物**

- `dataset/<版本号>/` 下成对的 `xxx.png` 与 `xxx.txt`，txt 内是单行正面提示词
- 一份完整的 manifest，记录每张图从提示词到最终 caption 的全链路信息
- 一套可重跑、可断点续跑的流水线

**不在范围内**：LoRA 训练与训练参数调优。但 manifest 和导出格式要为后续训练和评估留好接口。

## 2. 总体架构

分三层，职责严格分开。

```
┌─ 控制层：OpenCode agents ───────────────────────────┐
│  orchestrator（主）                                  │
│  prompt-designer / vision-reviewer / caption-writer  │
│  只做判断和编排，不做批量搬运                          │
└────────────────┬────────────────────────────────────┘
                 │ MCP
┌────────────────▼────────────────────────────────────┐
│  执行层：MCP servers（确定性、可单独重跑）             │
│  nai-bridge │ vision-tools │ dataset-manifest       │
└────────────────┬────────────────────────────────────┘
                 │ 读写
┌────────────────▼────────────────────────────────────┐
│  数据层：manifest.db（SQLite）+ 图片文件              │
│  唯一数据源，所有阶段只认它                            │
└─────────────────────────────────────────────────────┘
```

## 3. 三条核心设计原则

**3.1 manifest 是唯一数据源**

一张图一条记录，每个阶段在记录上写自己的状态字段。阶段之间不传数据体，只传 id 列表或筛选条件。这样做换来三件事：断点续跑、重新清洗不用重新生图、任何一张图的来历都可追溯。

选 SQLite 而不是 JSONL，因为后续大量操作是条件查询（「取出所有 qc 通过但还没打 caption 的记录」），且 MCP server 可能并发写。

**3.2 MCP 工具只返回摘要**

这是本方案最关键的工程约束。几百上千张图的逐条数据绝不能进 agent 上下文。

- 批处理类工具返回 `{processed, ok, failed, 失败id样本}`
- 查询类工具强制分页，默认上限 50 条
- 需要看明细时，agent 显式调用 `query_records` 带 limit

**3.3 LLM 只在判断点介入**

确定性工作（生图调度、哈希去重、反推打标、格式转换、导出）全部由 MCP 工具完成，不经过 LLM。LLM 只负责四件事：设计提示词、裁决边界样本、裁决 tag 冲突、解读统计结果。

每个阶段都要幂等：入口按状态过滤只处理 `pending`，写入统一用 upsert，重复执行无副作用。

## 4. manifest 数据模型

主表 `records`，一行一张图。辅表 `runs`（批次元信息）、`prompt_plans`（覆盖矩阵定义）。

| 字段组 | 字段 | 说明 |
|---|---|---|
| 标识 | `id`, `run_id`, `created_at` | |
| 提示词 | `content_prompt` | 内容段，不含画风 tag |
| | `style_prompt` | 画风段，整批固定 |
| | `quality_prompt`, `negative_prompt` | |
| | `full_prompt_sent` | 实际提交给 NAI 的完整串 |
| | `matrix_cell` | 对应覆盖矩阵的哪一格 |
| 生成 | `job_id`, `seed`, `sampler`, `steps`, `cfg`, `width`, `height`, `nai_model` | |
| | `image_path`, `gen_status` | `pending`/`sent`/`done`/`failed` |
| | `png_meta_prompt` | **从输出 PNG 里读回的实际提示词** |
| 清洗 | `phash`, `dup_of`, `aesthetic_score`, `tagger_tags` | |
| | `qc_status` | `pending`/`pass`/`reject`/`needs_review` |
| | `qc_reason`, `qc_by` | `auto` / `llm` / `human` |
| caption | `final_tags`, `nl_caption`, `trigger_word`, `caption_text` | |
| 导出 | `export_status`, `dataset_version`, `bucket` | |

`png_meta_prompt` 这个字段值得单独说。NAI 输出的 PNG 会把实际使用的提示词写进元数据，把它和 `full_prompt_sent` 对比，就能确认画风 tag 有没有被 Launcher 冲掉。这是应对接口行为不明的最可靠手段。

## 5. MCP server 划分

只做三个 server，不拆更细。拆太碎会导致 agent 的工具列表膨胀、调度变难。

### 5.1 nai-bridge

把 Aaalice NAI Launcher 包成适配器层，底层实现可替换。

| 工具 | 职责 |
|---|---|
| `probe_capabilities` | 探测可用模型、参数范围、接口形态 |
| `submit_batch` | 从 manifest 取 `gen_status=pending` 的记录，提交任务，回写 `job_id` |
| `poll_batch` | 轮询任务状态，返回计数摘要 |
| `ingest_outputs` | 收图、解析 PNG 元数据、按 job_id 关联回记录 |
| `retry_failed` | 失败记录重新入队 |

**画风 tag 被冲掉的对策**，按优先级：

1. **不用 Launcher 的随机提示词功能**。提示词在我们这一侧完整组装好（画风段 + 内容段 + 质量段），作为一个完整串提交。随机采样逻辑我们自己实现，Launcher 只当执行器。
2. 如果接口把提示词拆成多个字段且互相覆盖，就定位覆盖发生的时机，把画风段放在提交前的最后一步注入。
3. 如果 Launcher 实在不可控，退化为只当转发层，或者自己写一个最小 NAI 客户端。调度、重试、限速本来就在我们这层，Launcher 的这部分能力并非不可替代。

无论走哪条路，`ingest_outputs` 都要做 PNG 元数据校验，画风段缺失的记录直接标 `reject`。

**关于自然语言描述**：NAI 本身是 tag 驱动的，生成阶段不需要自然语言，所以 Launcher 缺这个能力不构成阻碍。自然语言描述是在第 7 节的 caption 阶段由视觉模型生成，服务于 Anima 一侧。

**Launcher 的 tag 词库**可以当作我们采样器的候选池复用，不浪费你已有的积累。`[待确认]` 词库文件的格式和位置。

### 5.2 vision-tools

纯本地确定性计算，不联网、不调 LLM。

| 工具 | 职责 |
|---|---|
| `compute_hashes` | 感知哈希，回写 `phash` |
| `find_duplicates` | 按汉明距离分组，回写 `dup_of` |
| `score_aesthetic` | 美学评分 |
| `run_tagger` | 反推 tag，回写 `tagger_tags` |
| `cluster_embeddings` | CLIP 向量聚类，用于检查多样性 |

具体用哪个 tagger 和评分模型到开发时再定，MCP 层只暴露统一接口。`[待确认]` 本地显存和已有模型。

### 5.3 dataset-manifest

状态管理与导出。

| 工具 | 职责 |
|---|---|
| `init_run` | 建批次，写入画风基线和目标张数 |
| `add_prompts` | 批量写入提示词记录，自动语义去重 |
| `query_records` | 条件查询，强制分页 |
| `update_records` | 批量更新字段 |
| `assemble_captions` | 按第 7 节规则组装 `caption_text` |
| `export_dataset` | 导出图片 + 同名 txt，写 `dataset_version` |
| `stats` | 返回各阶段计数、矩阵覆盖度、分桶分布 |
| `snapshot` | 给当前筛选条件打版本标签 |

## 6. 流水线阶段

| 阶段 | 内容 | 执行者 |
|---|---|---|
| S0 初始化 | 定画风基线、目标张数、覆盖矩阵，`init_run` 落库 | 人 + orchestrator |
| S1 提示词 | 按矩阵采样产出内容段，`add_prompts` 去重入库 | prompt-designer |
| S2 生成 | `submit_batch` → `poll_batch` → `ingest_outputs` 循环 | nai-bridge |
| S3 自动清洗 | 哈希 → 查重 → 评分 → 反推 tag，明显不合格直接 `reject`，边界样本标 `needs_review` | vision-tools |
| S4 LLM 复核 | 分批审查画面缺陷、裁决 tag 冲突，回写 `qc_by=llm`；已通过样本随机抽样人工复核 | vision-reviewer |
| S5 caption | 按第 7 节规则组装，复杂描述补一句自然语言 | caption-writer |
| S6 导出 | 导出图片 + 同名 txt，打版本快照，出统计报告 | dataset-manifest |

S1 的硬性要求：内容段不含任何画风类 tag；按比例混入无人物场景、物件、多人图；控制版权角色占比。

S6 的统计报告要检查矩阵覆盖度和分桶分布是否均衡，不均衡就回到 S1 定向补数据。

## 7. caption 组装规则

直接决定 LoRA 质量，是整个方案里最需要讲究的部分。

**组成顺序**

```
触发词, 质量/元 tag, 主体与角色, 动作表情, 服装配饰,
构图视角, 场景背景, 光照色调, 一句自然语言概括
```

**硬性规则**

- 画风 tag 必须全部按 blocklist 剔除（画师名、风格词），风格信息完全交给触发词承载。blocklist 从本批次使用的 `style_prompt` 派生。这一步不做，LoRA 效果会明显打折。
- tag 来源优先级：原始提示词 ∩ 反推结果（高置信）最可信；只在原始提示词里出现、图上没体现的，删掉；只有反推结果里出现的，保留。
- 冲突项交给 LLM 裁决。
- 自然语言句子只出现在 caption 里，对应 Anima 支持 tag 与自然语言混合输入的特性，属于可选项。
- caption dropout、tag shuffle 等增强放在训练脚本层面做，导出的 txt 保持完整有序，数据集层面不做随机化。

## 8. OpenCode agent 分工

| agent | 模型档位 | 职责 |
|---|---|---|
| orchestrator | 强 | 读 `stats` 做决策、推进阶段、关键节点找人确认 |
| prompt-designer | 中 | 按矩阵产出提示词批次 |
| vision-reviewer | 便宜的视觉模型，多实例并行 | 审图、裁决 tag 冲突 |
| caption-writer | 中 | 批量生成自然语言概括句 |

**必须由人拍板、不能让 orchestrator 自行决定的节点**

- 美学评分与哈希距离的阈值
- 覆盖矩阵的维度定义与配比
- 数据集版本定稿
- 任何删除已生成图片的操作

**并行策略**：S4 的 LLM 调用量最大，切成几十张一批交给多个 vision-reviewer 实例并行，返回结构化结果而非叙述文字。

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| Launcher 冲掉画风 tag | PNG 元数据回读校验 + 5.1 的三级退化方案 |
| agent 上下文溢出 | 工具只返回摘要，明细按需分页查询 |
| 账号与额度风险 | 并发保守、失败退避、限速；分批小量先验证 |
| NAI 服务条款 | 开工前确认条款里关于「生成结果用于训练模型」的表述 |
| NAI 自身画风混入 | 蒸馏固有问题，只能靠 S3/S4 剔除明显瑕疵缓解 |
| 角色特征污染画风 | 控制版权角色占比，混入无人物样本 |
| 盲目堆量 | S2 多产出、S3 狠筛；几十到两三百张精选优于上千张杂图 |

## 10. 目录结构

```
nai-lora-pipeline/
├── mcp/
│   ├── nai-bridge/        # Launcher 适配器
│   ├── vision-tools/      # 本地视觉计算
│   └── dataset-manifest/  # 状态与导出
├── .opencode/agents/      # 四个 agent 定义
├── config/
│   ├── style-presets/     # 画风基线 + blocklist
│   └── matrix/            # 覆盖矩阵定义
├── data/
│   ├── manifest.db
│   └── raw/               # NAI 原始输出
└── dataset/<版本号>/       # 最终产物：图片 + 同名 txt
```

## 11. 实施顺序

1. **dataset-manifest 的数据模型**。地基，先定下来，其他一切依赖它。
2. **nai-bridge 的接口探测**。先搞清楚 Launcher 到底能怎么调，这决定第 5.1 节走哪条路。
3. **小批量贯通**。20 张图跑完 S1→S6，验证链路可行，重点验 PNG 元数据回读。
4. **补齐 vision-tools**，接入 agent 复核。
5. **放量生产**，回流评估结果定向补数据。

## 12. 到开发环境需要确认的问题

1. Launcher 的接口形态：HTTP API、命令行，还是配置文件驱动？
2. 提示词字段如何拆分，覆盖具体发生在哪一步？
3. tag 随机词库的文件格式和位置？
4. 输出目录结构，以及 PNG 元数据的实际内容？
5. 本地显存情况，已有哪些 tagger / 评分模型？
6. 目标画风的 tag 串，以及打算用什么触发词？
7. 覆盖矩阵的维度设计 —— 这个值得单独展开讨论。


