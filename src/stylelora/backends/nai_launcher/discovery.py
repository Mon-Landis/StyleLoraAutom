from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
import json
from ..types import BackendError


@dataclass(frozen=True)
class DiscoveryInfo:
    endpoint: str
    port: int
    token: str = field(repr=False)
    pid: int
    protocol_versions: tuple[str, ...]
    app_version: str


def discover(appdata: Path, *, explicit_token=None, environ, retries=2):
    path = appdata / 'nai-launcher' / 'mcp-server.json'
    missing = False
    for _ in range(retries + 1):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            endpoint, port = data['endpoint'], data['port']
            url = urlsplit(endpoint)
            token = explicit_token if explicit_token is not None else environ.get('NAI_LAUNCHER_MCP_TOKEN', data.get('token'))
            versions = data['protocol_versions']
            if (url.scheme != 'http' or url.hostname != '127.0.0.1' or url.port != port
                or url.username is not None or url.password is not None or url.query or url.fragment
                or type(port) is not int or not 1 <= port <= 65535 or type(data['pid']) is not int or data['pid'] < 1
                or not isinstance(token, str) or not token or '\r' in token or '\n' in token
                or not isinstance(versions, list) or not versions or not all(isinstance(v, str) and v for v in versions)
                or not isinstance(data['app_version'], str) or not data['app_version']):
                raise ValueError()
            return DiscoveryInfo(endpoint, port, token, data['pid'], tuple(versions), data['app_version'])
        except FileNotFoundError:
            missing = True
        except (OSError, ValueError, KeyError, TypeError):
            missing = False
    raise BackendError('discovery:not_running' if missing else 'discovery:invalid_file', 'discovery') from None
