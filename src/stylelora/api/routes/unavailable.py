"""Explicit capability failures for contracts without a configured adapter.

These are not success placeholders. Unknown routes still remain JSON 404.
"""
from fastapi import APIRouter, Request
from stylelora.runtime.contracts import Failure

router = APIRouter(prefix='/api')

# The transport contract stays discoverable even when an adapter is unavailable.
CONTRACTS = '''
A003 POST /session/shutdown
A015 DELETE /llm/endpoints/{endpoint_id}
A018 DELETE /llm/endpoints/{endpoint_id}/tokens/{token_ref}
A021 POST /llm/endpoints/{endpoint_id}/models/probe
A022 POST /llm/endpoints/{endpoint_id}/models/test-image
A023 GET /presets
A024 POST /presets
A025 GET /presets/{preset_id}
A026 PUT /presets/{preset_id}
A027 DELETE /presets/{preset_id}
A032 DELETE /dictionaries/{dictionary_id}
A034 POST /dictionaries/imports/preview
A035 POST /dictionaries/{dictionary_id}/imports
A036 GET /dictionaries/{dictionary_id}/export
A037 POST /coverage/preview
A038 POST /dictionaries/launcher/preview
A044 PATCH /projects/{project_id}
A046 GET /projects/{project_id}/recovery
A047 POST /projects/{project_id}/recovery/reconcile
A065 POST /projects/{project_id}/images/{image_id}/verify-file
A067 GET /projects/{project_id}/annotations/{image_id}/history
A069 POST /projects/{project_id}/annotations/manual/bulk
A072 POST /rules/imports/preview
A073 POST /rules/imports/commit
A082 GET /projects/{project_id}/jobs/{job_id}/outcomes
A085 POST /projects/{project_id}/ratings/reclassify
A086 GET /prompt-tasks
A087 POST /prompt-tasks
A088 GET /prompt-tasks/{task_id}
A089 GET /prompt-tasks/{task_id}/outcomes
A090 POST /prompt-tasks/{task_id}/control
A091 POST /prompt-tasks/{task_id}/append
A092 POST /prompt-tasks/{task_id}/seeds
A093 DELETE /prompt-tasks/{task_id}
A094 GET /prompt-tasks/{task_id}/coverage
A100 POST /projects/{project_id}/import-schemes/{scheme_id}/set-default
A109 POST /projects/{project_id}/sources/{source_id}/import-root
A110 GET /projects/{project_id}/imports/{source_id}/summary
A117 POST /projects/{project_id}/storage/scans
A118 POST /storage/scans
A119 GET /storage/scans/{scan_id}
A120 POST /projects/{project_id}/storage/cleanup/preview
A121 POST /projects/{project_id}/storage/cleanup
A122 GET /projects/{project_id}/storage/summary
A124 GET /backends/{backend_id}/capabilities
A127 POST /tags/translations/lookup
A128 PUT /translations
A129 POST /tags/imports/preview
A130 POST /tags/imports/commit
A133 POST /projects/{project_id}/duplicates/scans
A134 GET /projects/{project_id}/duplicates/scans/{scan_id}
A135 DELETE /projects/{project_id}/duplicates/scans/{scan_id}
A137 GET /tagger/models
A138 POST /tagger/models/install
A139 GET /tagger/installations/{installation_id}
A140 DELETE /tagger/installations/{installation_id}
'''


def register():
    for line in CONTRACTS.strip().splitlines():
        identity, method, path = line.split()
        def handler(identity):
            async def unavailable(request: Request):
                pid = request.path_params.get('project_id')
                if pid:
                    request.app.state.runtime.require_project(pid)
                code, reason = {
                    'A021': ('llm:probe_unavailable', 'llm_adapter_not_configured'),
                    'A022': ('llm:image_test_unavailable', 'llm_adapter_not_configured'),
                    'A038': ('launcher:unsupported_schema', 'verified_launcher_schema_required'),
                    'A124': ('backend:incompatible_schema', 'verified_backend_schema_required'),
                    'A138': ('tagger:download_unavailable', 'hf_downloader_not_configured'),
                    'A139': ('tagger:installation_unavailable', 'hf_downloader_not_configured'),
                    'A140': ('tagger:installation_unavailable', 'hf_downloader_not_configured'),
                }.get(identity, ('feature:unsupported', 'runtime_adapter_not_implemented'))
                raise Failure(501, code, details={'endpoint': identity, 'reason': reason})
            return unavailable
        router.add_api_route(path, handler(identity), methods=[method], name=identity,
                             responses={501: {'description': 'Adapter not implemented or configured'}})


register()
