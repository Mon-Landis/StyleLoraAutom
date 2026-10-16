# F27 后端合同

- 基址 `/api`，沿用 Bearer 认证和统一 `{error:{code,message,details,...}}` 错误体。
- 所有写入发 `Idempotency-Key`（非空字符串≤256 字符）；同键异载荷 409。
- 创建组、新建导入确认发 `If-None-Match: *`；修改现有组/条目/图片发所属组 `If-Match: "revision"`。
- 缺 ETag 前置条件 428；组 CAS 冲突 412；成功返回 `revision`、`version` 和强 ETag。
- `Ref={scope:"global"|"project",project_token:null|string,group_id:UUID}`。
- project_token 取当前 session.project.project_id；关闭/重开项目后旧 token 失效（409 project:closed）。
- 创建 scope 默认已开项目时 project，否则 global；其它组请求 query 必带选中 scope/project_token。
- 后端不按 nsfw_enabled 隐藏、阻止抽取或补全；组/条目响应带 nsfw，由前端控制可见性。

## 组与条目

- `GET /prompt-groups?scope=&nsfw=true|false&q=&limit=50&cursor=` → `{items:[GroupSummary],next_cursor,issues}`。
- GroupSummary 示例：`{dictionary_id,name,kind:"prompts",category:"full_content",schema_version:3,scope,project_token,nsfw,revision:1,version:"1",entry_count:0}`。
- 项目组优先，再按 dictionary_id；同 ID 不同 scope 保留两组。损坏组隔离，issues 给出告警。
- `POST /prompt-groups` body `{name:"场景",nsfw:false,scope:"global"}` → 201 GroupSummary。
- 可指定 dictionary_id；kind/category 只能 prompts/full_content；名称去空白后 1–128 字符。
- 以下 `G=/prompt-groups/{group_id}`，`E=G/entries/{entry_id}`，query 携带 Ref 的 scope/project_token。
- `GET G` → GroupSummary，不内嵌全量 entries；`PATCH G` body `{name?,nsfw?}` → GroupSummary。
- `DELETE G` → `{deleted:group_id,revision,version}`，允许非空删除，幂等墓碑保存收据。
- `GET G/entries?disable=true|false&limit=50&cursor=` → `{items:[Entry],next_cursor,revision,version,nsfw}`。
- `POST G/entries` body EntryDraft → 201 Entry；服务端分配 entry_id。
- `GET E` → Entry；`PATCH E` body EntryDraft 的任意字段 → Entry；`DELETE E` → 删除收据。
- EntryDraft：`{tag:["cat"],expand_nl:"",nl:"",disable:false,aspect:null}`。
- 三内容字段至少一项非空；aspect 为 landscape/portrait/square/null；字段未知或类型错误返回逐字段 issues。
- Entry 在 EntryDraft 上增加 `{entry_id,preview:null|PreviewRef,completion:{state,missing,task_id,error_code},nsfw}`。
- state 为 pending/queued/running/complete/failed/paused/canceled；只有 expand_nl 时自动令 nl=expand_nl。
- 游标为不透明 keyset，limit 1–200；条件/项目改变或条目组 revision 改变返回 409 pagination:stale。
- 非法游标返回 422 pagination:invalid；从第一页重新读取。

## 导入、导出与图片

- `POST /prompt-imports/preview`：JSON body `{data:{group_metadata?:{format_version:1,group_id?,name?,nsfw?,scope?,category?},prompts:[EntryDraft]},options?:{...}}`。
- 文本文件/zip 用 multipart：`file` 为上传字节，`options` 为 JSON 字符串；UTF-8/BOM 支持，JSON 限 10MiB。
- options：`{format:"json"|"zip"|"folder",level:"group"|"entries",source_format:"anima"|"nai",metadata?:{group_id?,name?,nsfw?,scope?,category?},ref?:Ref,entry_ids?:UUID[]}`。
- folder body `{grant_id,options:{format:"folder",...}}`；grant 来自现有 import_root 文件选择器，拒绝任意路径。
- JSON 组根只接受数组或 `{group_metadata?,prompts}`，不兼容 prompt/promprt 等别名。
- JSON 条目根只接受 EntryDraft 或数组；level=entries 必须指定 ref。JSON 不接受 entry_id/preview。
- 组包根为 group.json + previews/，条目有 entry_id，preview_manifest 按 entry_id 声明 file/sha256。
- zip 限 100MiB，解压 200MiB，10000 条，拒绝越界/链接/ADS/大小写冲突/未声明文件/压缩比>100。
- 预览响应 `{preview_id,digest,ref,base_revision,entries,metadata,valid_count,skipped_count,issues,warnings,id_mapping,blocked}`。
- 修改 metadata、来源格式或目标必须重新预览；project 未打开时 blocked=true，不可确认。
- `POST /prompt-imports/{preview_id}/confirm` body `{digest,ref?}` → `{ref,inserted,duplicates,conflicts,revision,version}`。
- 预览保留 15 分钟；确认沿用冻结 ID、资源、revision 和项目 token；重复确认幂等。
- nai 延迟调用 F28；模块未就绪返回 422 import:unsupported，转换警告显示在 warnings。
- `GET G/export?format=json|zip` → 附件字节；JSON 包含禁用和空字段，不含图片/内部状态。
- `GET G/export?format=folder` → `{handle,download_url,format:"folder",transport:"zip",revision}`；下载 zip 解包保存，不泄漏本地路径。
- `GET E/export?format=json` → EntryDraft，用于单条复制/导出。
- `PUT E/preview` multipart `image` → PreviewRef + revision/version/nsfw；组 ETag CAS。
- PreviewRef：`{asset_revision,sha256,width,height,mime:"image/webp"}`。
- 静态 PNG/JPEG/WebP：输入≤10MiB、40MP；去元数据，长边≤2048，缩略图≤320，不放大小图。
- `GET E/preview?size=full|thumb&asset_revision=` → WebP；ETag 为所取图片摘要，If-None-Match 命中 304。
- 图片响应 `Cache-Control: private, no-cache`、`X-NSFW`；缺图 404 image:not_found，前端保留文本并显示占位图。

## 补全与设置

- `POST /tasks` body `{type:"prompt_completion",ref:Ref,entry_ids:[UUID],base_revision:3}` → 202 统一 TaskView。
- 省略 entry_ids 表示该 revision 全部待补全（跳过禁用）；单 ID 可显式补全禁用条目；完整条目不调用模型。
- TaskView 的 kind 保持 `prompt`，resources 含 `{kind:"prompt_completion",id}`；task_id 是统一中心 ID。
- `GET /tasks/{task_id}` → TaskView + `{type:"prompt_completion",reports:[{entry_id,error_code,warnings,...}]}`。
- `POST /tasks/{task_id}/cancel` 沿用 body `{request_key:string}`；退出/关项目暂停，不自动重放未知模型调用。
- 模型只填空字段；人工编辑后 completion:stale；未补齐 completion:incomplete；缺 skill/LLM 时 completion:unavailable。
- 重试创建新任务和新 Idempotency-Key；已有成功字段保留，旧任务不会自动恢复调用。
- `GET /settings` → 产品设置 + revision/version/revealed_this_run；默认 nsfw_enabled=false、nsfw_display_mode="click_reveal"。
- `PATCH /settings` body `{nsfw_enabled?:bool,nsfw_display_mode?:"click_reveal"|"hover"|"show"}`，设置 ETag CAS（冲突 412 settings:revision_conflict）。
- image_display 兼容映射同一值；两个键同时提交不同值返回 422；磁盘只保存 nsfw_display_mode。
- `POST /safety/reveal` → `{revealed_this_run:true}`，不检查 NSFW 开关；当前进程共享，退出重置。
- NSFW 隐藏/模糊、hover/键盘、清晰图缓存清理与长按删除均由前端实现。
