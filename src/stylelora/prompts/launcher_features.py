"""Read-only injected adapters. No production Launcher schema is assumed."""
from dataclasses import dataclass
from .types import FeatureCandidate, PromptError
from .validation import require


@dataclass(frozen=True)
class ReadOnlyAdapter:
    version: str
    tool_name: str
    input_schema: dict
    decode_page: object


async def read_launcher_features(selection, *, client, adapter=None):
    require(adapter is not None and selection.get('version')==adapter.version,'launcher:unsupported_schema')
    tools=await client.list_tools()
    tool=next((t for t in tools if t.get('name')==adapter.tool_name),None)
    require(tool is not None and tool.get('inputSchema')==adapter.input_schema,'launcher:unsupported_schema')
    mapping=selection.get('group_mapping',{}); cursor=None; seen=set(); result=[]
    while True:
        page=await client.call_tool(adapter.tool_name,{'cursor':cursor})
        rows,next_cursor=adapter.decode_page(page)
        for row in rows:
            group=tuple(row['group_path']); dimension=mapping.get('/'.join(group))
            require(dimension is not None,'import_mapping_required')
            require(',' not in row['text'],'launcher:split_preview_required')
            result.append(FeatureCandidate(dimension,row['text'],row.get('rating'),'launcher'))
        if next_cursor is None: break
        require(next_cursor not in seen,'launcher:unsupported_schema'); seen.add(next_cursor); cursor=next_cursor
    return tuple(sorted(result,key=lambda c:(c.dimension,c.tag,c.rating or '')))
