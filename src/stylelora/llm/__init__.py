"""Product-wide LLM service. Share one LlmService instance across callers.

ConfigStore/SecretStore are internal product stores, not API response DTOs.
Use SecretStore.public_endpoint for external configuration views.
"""
from .adapters import (HttpResponse, JsonAdapter, ProtocolAdapter, TransportFailure,
                       UrllibTransport, adapter_for, register_adapter)
from .config import ConfigStore, load_endpoints, save_endpoint
from .images import encode_for_vision, encode_image, next_timeout_tier
from .json_output import repair_json_text, validate_json_reply
from .secrets import SecretStore, mask_secret, put_secret, resolve_secret
from .service import LlmService, capability_fixture
from .types import (CancelToken, DEFAULT_TIERS, EncodedImage, EndpointConfig, ImageInput,
                    ImageTier, JsonObject, JsonReply, LlmFailure, LlmReply, LlmRequest,
                    ModelConfig, ProtocolName, RepairBudget, Role, RoleBinding)

__all__ = [
    'LlmService', 'LlmRequest', 'LlmReply', 'LlmFailure', 'RoleBinding', 'Role',
    'ProtocolName', 'JsonObject', 'JsonReply', 'RepairBudget', 'CancelToken',
    'ConfigStore', 'EndpointConfig', 'ModelConfig', 'load_endpoints', 'save_endpoint',
    'SecretStore', 'mask_secret', 'put_secret', 'resolve_secret', 'ImageTier',
    'ImageInput', 'EncodedImage', 'DEFAULT_TIERS', 'encode_image', 'encode_for_vision',
    'next_timeout_tier', 'validate_json_reply', 'repair_json_text', 'ProtocolAdapter',
    'JsonAdapter', 'adapter_for', 'register_adapter', 'UrllibTransport', 'HttpResponse',
    'TransportFailure', 'capability_fixture',
]
