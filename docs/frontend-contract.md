# 前后端合同核对

核对范围：`frontend/src/api/{client,session,sse,endpoints,more,types}.ts`、调用页面、`api/routes/*.py`、`runtime/*.py`；后端合同依据 `F11-F14-API与进程设计.md`。下表记录修复后的实际调用。`P=/api/projects/{pid}`；GET 无请求体。所有 API 除 bootstrap 外带 Bearer，POST 自动带 Idempotency-Key；标记 CAS 的调用带 If-Match。

共同响应：Page 为 `{items,next_cursor,has_more,limit,upper_id?}`；Operation 为 `{operation_id,kind,status,completed,total,result,error,version}`，202 后轮询 `/api/operations/{id}` 至终态。ProjectView 为 `{project_id,metadata:{name,trigger_word},version}`。表中仅列前端消费字段，服务端可有其他字段。

| 方法 | 路径（查询参数） | 请求体 / 前置条件 | 前端期望响应字段 |
|---|---|---|---|
| POST | /api/session/bootstrap | `{code}`，匿名，浏览器 Origin | token, instance_id |
| GET | /api/session | — | 仅确认成功；后端含 instance_id,epoch,api_version,project,capabilities,draining |
| GET | /api/events?project_id&topics（重复） | Accept: text/event-stream；重连 Last-Event-ID | SSE id,event,data；data.project_id,payload；stream.ready / stream.resync_required |
| GET | P/images?review_status&source_id&rating&limit&cursor&around_id | 三种筛选可重复；cursor 与 around_id 不同时提供 | Page<ImageItem> + total,start_index,anchor_index；ImageItem: image_id,filename,source_id,review_status,rating,width,height,file_missing,has_alpha,annotation_coverage.status,version,original_url,thumbnail_url |
| GET | P/images/stats?review_status&source_id&rating | 同上筛选 | total,groups（宽松 Record） |
| GET | P/images/{id}/thumbnail | fetchBlob 使用 thumbnail_url，Bearer | 图片 Blob，ETag；If-None-Match 可得 304 |
| GET | P/images/{id}/content | fetchBlob 使用 original_url，Bearer | 图片 Blob，ETag；If-None-Match 可得 304 |
| POST | P/images/review | `{ids,target,expected_versions}` | items[].image: ImageItem（后端另含 old_review_status） |
| GET | P/annotations/{id} | — | image_id,tags,natural_language,layers,refs,missing_original,version |
| PUT | P/annotations/{id}/manual | `{tags,natural_language?,write_nl,read_set}` + CAS | ManualResult: annotation_ids,changed；适配器再 GET 最新 AnnotationView |
| POST | P/rules/preview | `{rule:{schema_version:1,delete,write,condition},scope:{selected_ids}}`；group 为 `{mode,tags}` 或 null | preview_id,digest,affected_count（另有 version,expires_at,issues） |
| POST | P/rules/apply | `{preview_id,digest}` + CAS digest | op_id,affected_count（调用方不消费返回值） |
| GET | P/operation-log?limit=20&sort=-id | — | Page<{op_id,status,version,affected_count,created_at}> |
| POST | P/operation-log/{op_id}/undo | `{}` + CAS version | OperationRow: op_id,status,version,affected_count,created_at（调用方不消费） |
| GET | /api/projects | — | Page<{known_id,display_name}> |
| GET | /api/projects/current | — | ProjectView 或 null |
| GET | /api/operations/{id} | — | Operation |
| POST | /api/filesystem/grants | `{purpose:'project_create'或'project_open'}` | Operation → result 为 grant_id 等或 null；picker 不可用 501 |
| POST | /api/projects/open | `{grant_id}` 或 `{known_id}` | Operation → ProjectView |
| POST | /api/projects | `{grant_id,name,trigger_word?}` | Operation → ProjectView |
| POST | P/close | `{}` + CAS | Operation（页面重新读取 current） |
| POST | P/jobs/batches | `{batch_id,executors,mode,scope}`；selected 为 selected_ids，其他为 filter:{} | JobRow[]；创建动作只确认成功 |
| GET | P/jobs?limit=50 | — | Page<JobRow>；列表刻意省略 progress_json |
| GET | P/jobs/{job_id} | — | job_id,executor,status,progress_json,version；列表适配器取详情以供进度展示 |
| POST | P/jobs/{id}/control | `{action}` + CAS | JobRow（当前调用方不消费） |
| GET | P/sources?limit=200 | — | Page<{source_id,type,name,status,target_count,version}> |
| POST | P/sources | `{type,name,target_count?}` | source_id,type,name,status,target_count,version |
| GET | /api/prompt-tasks?limit=50 | — | Page<{task_id,status,version}>；后端没有 name/id，页面以 task_id 展示 |
| POST | /api/prompt-tasks | SamplingRequest：plan,prompt_dictionary_ids,coverage_enabled,optimize | TaskView: task_id,status,version；不是 Operation |
| GET | P/export-schemes?limit=100 | — | Page<{scheme_id,name,version}> |
| POST | P/exports/preview | `{scheme_id}` | Operation → result:{preview_id,digest,selected,issues:[{severity,code}],trigger_override_digest?} |
| POST | P/exports | `{scheme_id,preview_id,digest}` | ExportView: export_id,project_id,status,selected,exported,skipped,failed,report_url,relative_dir,version |
| GET | P/exports/{export_id} | — | ExportView；planned/running 继续轮询，finished 为成功 |
| GET | /api/config | — | Record；设置页按键值展示 |

`client.ts` 的 request/get/post/put/patch、more.ts 重导出的 put/patch 是通用传输函数，不固定增加端点。响应适配器另读取 ETag、Location、status；SSE 的 topics 匹配事件前缀 `job`，不是 `jobs`。

本轮发现与修复：manual 写入误当 AnnotationView；规则误用 `{op,find,replace,layer}`；操作日志误用 operation_id/id/kind 且未倒序；导出预览误当同步结果、启动漏 scheme_id、把 ExportView 当 Operation 轮询；任务进度误取列表省略字段；SSE 订阅错用 jobs；提示词创建误发 name。均调整前端，未增加后端业务范围。提示词创建保留最小单条字典采样，明确输入已有字典 ID，不调用 LLM。

F11 §4 的 CAS 冲突统一 409，不使用 412。A078 文档描述撤销返回 OperationRow，而现有实现返回 WriteResult 包装，本轮将路由投影为 OperationRow。合同测试断言此响应。

`tests/test_frontend_contract.py` 使用临时目录、离线 LocalCatalog、Pillow PNG，真实走导入预览/执行、项目创建/关闭/打开、图片分页/定位/统计/缓存、审图预览/执行/撤销、manual CAS、规则与导出。测试按 TS interface 提取必需字段检查响应，SSE 仅把传输限制为一个真实帧并验证清理。真实服务冒烟另验证端口与 Vite 代理。
