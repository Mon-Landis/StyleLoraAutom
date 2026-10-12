from dataclasses import dataclass
from ..types import Immutable, digest

READ_TOOLS = frozenset({'get_generation_settings', 'get_generation_status', 'get_recent_images'})
WRITE_TOOLS = frozenset({'update_generation_settings', 'prepare_generation', 'submit_generation',
                         'cancel_generation_preparation', 'save_generated_image'})
CORE_TOOLS = READ_TOOLS | WRITE_TOOLS
PROTOCOLS = ('2025-06-18', '2025-03-26')


@dataclass(frozen=True)
class ServerInfo:
    name: str
    version: str


@dataclass(frozen=True)
class ToolSchema(Immutable):
    name: str
    input_schema: dict
    output_schema: dict | None = None


@dataclass(frozen=True)
class CompatibilityReport:
    accepted: bool
    schema_hash: str
    reason: str
    full_schema_hash: str = ''


def schema_hash(tools):
    return digest(sorted(tools, key=lambda t: t.name))


# Deliberately empty: repository contains no audited real Launcher schema fixture.
# Test callers inject a manifest built from their explicitly synthetic fixture.
TRUSTED_MANIFEST = frozenset()


def verify_compatibility(server, tools, *, app_version=None, protocol_version=None, manifest=TRUSTED_MANIFEST):
    core = tuple(t for t in tools if t.name in CORE_TOOLS)
    key = schema_hash(core)
    accepted = (len({t.name for t in tools}) == len(tools) and {t.name for t in core} == CORE_TOOLS
                and app_version == server.version and protocol_version in PROTOCOLS
                and (server.version, protocol_version, key) in manifest)
    return CompatibilityReport(accepted, key, '' if accepted else 'backend:incompatible_schema', schema_hash(tools))
