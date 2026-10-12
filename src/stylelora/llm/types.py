"""Shared DTOs for prompt production and vision callers."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock
from types import MappingProxyType
from typing import Any, Literal, Mapping

JsonObject = Mapping[str, Any]
Role = Literal['prompt', 'prompt_nsfw', 'vision', 'vision_nsfw', 'rating', 'clean', 'clean_nsfw']
ProtocolName = Literal['openai_chat', 'openai_responses', 'anthropic_messages']
ROLES = ('prompt', 'prompt_nsfw', 'vision', 'vision_nsfw', 'rating', 'clean', 'clean_nsfw')


class LlmFailure(Exception):
    """Only fixed, safe diagnostics cross the service boundary."""
    def __init__(self, code: str, retryable: bool = False, outcome: str = 'rejected',
                 safe_message: str = 'LLM operation failed'):
        self.code, self.retryable, self.outcome = code, retryable, outcome
        self.safe_message = safe_message
        super().__init__(safe_message)


class CancelToken:
    def __init__(self):
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def canceled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class ModelConfig:
    name: str
    supports_images: bool = False
    capability_source: Literal['manual', 'models', 'tested'] = 'manual'
    tested_at: str | None = None
    # Explicit frozen wire mapping; aliases are never inferred from responses.
    wire_name: str | None = None
    response_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class EndpointConfig:
    endpoint_id: str
    base_url: str = field(repr=False)
    protocol: ProtocolName
    token_refs: tuple[str, ...] = field(repr=False)
    models: tuple[ModelConfig, ...]
    revision: int = 0
    supports_json_schema: bool = False
    changed: bool = field(default=True, compare=False)


@dataclass(frozen=True)
class RoleBinding:
    binding_id: str
    role: Role
    model: str
    protocol: ProtocolName
    revision: int


@dataclass(frozen=True)
class ImageTier:
    max_pixels: int = 2_000_000
    quality: int = 92


DEFAULT_TIERS = (ImageTier(), ImageTier(1_600_000, 88), ImageTier(1_000_000, 84))


@dataclass(frozen=True)
class ImageInput:
    path: Path | None = field(default=None, repr=False)
    data: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class EncodedImage:
    data: bytes = field(repr=False)
    width: int
    height: int
    original_sha256: str
    tier: ImageTier = ImageTier()
    mime_type: str = 'image/jpeg'
    encoder_version: str = ''


@dataclass
class RepairBudget:
    limit: int = 1
    used: int = 0
    _lock: Any = field(default_factory=Lock, repr=False, compare=False)

    def claim(self) -> bool:
        with self._lock:
            if self.used >= min(self.limit, 1):
                return False
            self.used += 1
            return True


@dataclass(frozen=True)
class LlmRequest:
    request_id: str
    binding: RoleBinding
    system_text: str = field(repr=False)
    payload: JsonObject = field(repr=False)
    response_schema: JsonObject = field(repr=False)
    timeout_s: int = 300
    transport_retry_limit: int = 2
    images: tuple[EncodedImage, ...] = ()
    cancel: CancelToken | None = field(default=None, repr=False)
    timeout_policy: Literal['fail_unknown', 'vision_downgrade'] = 'fail_unknown'
    tiers: tuple[ImageTier, ...] = ()
    image_input: ImageInput | None = None
    sampling: JsonObject = field(default_factory=lambda: MappingProxyType({}), repr=False)
    repair_budget: RepairBudget | None = None


@dataclass(frozen=True)
class LlmReply:
    text: str = field(repr=False)
    actual_model: str
    request_id: str
    usage: JsonObject | None = None
    actual_tier: ImageTier | None = None
    attempts: int = 1


@dataclass(frozen=True)
class JsonReply:
    value: JsonObject = field(repr=False)
    reply: LlmReply
    repair_used: bool
