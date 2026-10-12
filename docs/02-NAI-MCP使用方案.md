# 02 NAI Launcher MCP 使用方案

> 基于 Launcher v4.3.0 实测（98 个工具，仅 tools 能力，无 resources/prompts）+ 仓库 docs/mcp_server.md。
> 实测只跑过 1 次 0 Anlas 生图。付费路径、队列、protection mode 未实测。

## 1. 连接方式（生产环境）

端口 20624 不固定，不要写死。

| 方式 | 做法 | 适用 |
|---|---|---|
| A. 发现文件（推荐） | 读 `%APPDATA%/nai-launcher/mcp-server.json` 取 `endpoint` + `token` + `pid`；文件不存在就是 Launcher 没开 | 我们自己的 Python 客户端 |
| B. 官方 stdio 代理 | `nai_launcher_mcp.exe proxy`，自动解析端点，session 404 时自动重新握手 | 直接挂给 opencode / 其他 MCP 客户端 |
| C. 状态检查 | `nai_launcher_mcp.exe status`，退出码 0 正常 / 2 未运行 | 启动前健康检查 |

协议要点：Streamable HTTP，只用 POST（DELETE 结束会话），每次请求带 `Authorization: Bearer`，`initialize` 后带 `Mcp-Session-Id`。会话闲置 30 分钟回收，最多 16 个并发会话，请求体上限 8MiB。只监听 127.0.0.1。

客户端必须处理：

- 会话 404 → 重新握手，**但不能重放未完成的写操作**（可能重复扣费）
- 工具调用超时 ≥ 600s（审批弹窗最长等 5 分钟后自动拒绝）
- token 走环境变量或发现文件，不进配置文件、不进日志

## 2. 权限模式

| 模式 | 行为 | 我们怎么用 |
|---|---|---|
| 安全 | 只放行只读工具 | 开发调试、只做查询时 |
| 敏感操作前询问（默认） | 每个写操作都要在 Launcher 里点确认 | 不适合批量 |
| 完全访问 | 0 Anlas 直接执行；付费、永久删除仍要人确认 | **批量生产用这个** |

结论（2026-10-12 修订）：NAI 已无免费图，`estimated_anlas == 0` 实际消耗的是自回体力条额度。原「必须等于 0 才提交」的拦截规则已废止，改为**费用提醒**：每张 prepare 都检查 `estimated_anlas`，从 0 变为非 0 时暂停该数据源并提醒用户，确认后继续，不做死拦截（见 docs/05 §7.3、docs/04 F04）。注意非 0 时 Launcher 本身仍会弹付费确认，即使在完全访问模式下。

## 3. 核心生图链路

```
update_generation_settings   # 每个 run 开始时设一次：model / sampler / steps / scale / uc_preset(=None) / quality_toggle(=false)
  ↓
prepare_generation           # 每张一次，operation=generate, count=1, characters=[]
  ↓ 检查 estimated_anlas：从 0 变非 0 时暂停数据源并提醒用户（费用提醒，不拦截）
submit_generation(preparation_id)
  ↓ 返回 image + resource_ref + saved_path
读 saved_path 的 PNG tEXt → 回写 manifest.png_meta_prompt
```

实测得到的行为（这几条直接影响 manifest 设计）：

| 行为 | 实测结果 | 对策 |
|---|---|---|
| 页面提示词 / 固定词是否混入 | **不混入**，传入的 prompt 完整覆盖 | 原方案担心的「画风 tag 被冲掉」在 MCP 路径下不成立，仍保留 PNG 回读校验 |
| 页面角色是否混入 | 不传 `characters` 会继承页面角色 | **必须显式传 `characters: []`** |
| `quality_toggle=true` | 自动追加 `very aesthetic, masterpiece, no text` | **决议：固定关闭**，不做成可配项，避免污染溯源 |
| 下划线 | `simple_background` 被转成 `simple background` | 对比 sent 和 meta 前先统一归一化 |
| 自定义负面词 | effort=medium 时被忽略，用的是 uc_preset 列表 | 要自定义负面词就把 effort 设为 high（effort 由前端配；费用归 Launcher 管，不再作为设计约束） |
| model / sampler / steps | `prepare_generation` 不支持按请求传 | 只能通过 `update_generation_settings` 改页面设置，**会改掉用户的 UI 状态**，跑完要恢复 |
| 输出位置 | 画廊 `D:/ComfyUI/NAIGEN/<日期>/` **始终另存一份**（传了 save_path 也双写） | 传 `save_path` 写进项目 `images/.pending/<item_id>/<attempts>/`。实测：任意本地目录可写、父目录自动创建；目标已存在时 prepare 即返回 `save_path_exists`，从不覆盖。画廊副本归 Launcher 管，不得移动/删除 |

PNG 元数据里有：`prompt`、`seed`、`steps`、`scale`、`sampler`、`v4_prompt`（base_caption + char_captions）、`v4_negative_prompt`、`uc`、`model_hash`、`signed_hash`。足够完整记录溯源。

## 4. 工具使用分级（98 个工具里我们用哪些）

| 级别 | 工具 | 用途 |
|---|---|---|
| **核心** | `prepare_generation`, `submit_generation`, `cancel_generation_preparation`, `get_generation_settings`, `update_generation_settings`, `get_generation_status` | 生图主链路 |
| **核心** | `save_generated_image`, `get_recent_images` | 补救：save_path 失败时按 resource_ref 导出 |
| 辅助 | `search_tags`（search / suggest） | 提示词生成时做 tag 合法性校验、别名归一 |
| 辅助 | `interrogate_image` | 反推 tag，可作为本地 tagger 的备选/交叉验证 `[待确认：用的什么模型、速度]` |
| 辅助 | `list/get_tag_library_*`, `list_fixed_tags` | 复用用户已有词库作为采样池 |
| 辅助 | `browse/search_online_gallery`, `get_online_gallery_detail` | 从 Danbooru 等拉真实 tag 组合当提示词参考 |
| 预留（角色 LoRA） | `precise_reference_*`, `vibe_*`, `add/update_character`, img2img / inpaint 系列 | 角色参考增图、三视图修补 |
| 不用 | 队列系列、`navigate_application`、剪贴板、Krita、收藏 | 队列由我们自己调度；其余是 UI 操作 |

为什么不用 Launcher 的队列：队列状态在 Launcher 内存里，我们没法和 manifest 做事务一致；断点续跑、重试策略要以 manifest 为准。

## 5. 稳定性与风险

| 风险 | 处理 |
|---|---|
| Launcher 未启动 / 重启换端口 | 每批开始前重读发现文件；连接失败整批暂停，不标 failed |
| 写操作串行执行 | 并发提交没有收益，客户端单线程顺序提交即可 |
| pending 调用被重试导致重复扣费 | 服务端指令明确要求「不要重试 pending 调用」。客户端超时后先 `get_generation_status` / `get_recent_images` 对账，再决定是否重提 |
| 修改页面设置影响用户 | run 开始时快照 `get_generation_settings`，结束时恢复 |
| Launcher 版本升级改 schema | 启动时检查 `serverInfo.version` + 工具 schema 哈希，变化时报警 |
| MCP 协议后续改无状态（2026-07-28 修订） | 工具用显式 handle（preparation_id / resource_ref），只影响传输层，适配层隔离即可 |
| protection mode 开启 | 返回的图片字节会剥离元数据；我们读 `saved_path` 原文件，不受影响。检查 `metadata_stripped` 字段 |

## 6. 维护清单

- 依赖：Launcher ≥ 4.3.0，MCP 开启 + 完全访问模式
- 每次升级 Launcher 后重跑 `probe_capabilities`，对比工具列表和 schema
- 审计日志 `agent/mcp-audit-v1.jsonl`（Launcher 数据目录下）可用于对账扣费
- 未实测项：付费确认路径、`interrogate_image` 质量、并发会话下的表现
- 已实测（2026-10-12）：**没有账号/额度查询工具**（98 个工具与 `get_application_context` 均无相关字段），能拿到的只有 `prepare_generation` 的 `estimated_anlas`（单次预估，非余额），F21 走本地「剩余预估」（对未完成条目汇总单张预估，不累计已发出的，见 docs/08 U2）；`save_path` 行为见第 3 节
- 实测中 prepare 回显的 steps 为 28（页面设置原为 14），说明用户页面设置可能已变；适配器每次开跑前必须重读 `get_generation_settings`，不能依赖旧快照
