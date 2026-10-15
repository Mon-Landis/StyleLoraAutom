"""Pure prompt formatter protocol; only the identity formatter is registered."""
from dataclasses import dataclass
from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol
import re
from stylelora.modes.profiles import ProfileSnapshot, resolve_profile, digest
from .types import PromptEntry, PromptError
from .validation import validate_content

VARIABLES = frozenset({'character_name', 'appearance', 'nl_description'})
PLACEHOLDER = re.compile(r'{{\s*([^{}]+?)\s*}}')


@dataclass(frozen=True)
class FormatContext:
    profile: ProfileSnapshot
    values: Mapping[str, str]
    source_revision: str

    def __post_init__(self):
        object.__setattr__(self, 'values', MappingProxyType(dict(self.values)))


@dataclass(frozen=True)
class FormatResult:
    entry: PromptEntry
    formatter_id: str
    formatter_version: str
    input_digest: str
    output_digest: str


class Formatter(Protocol):
    id: str
    version: str

    def format(self, entry: PromptEntry, context: FormatContext) -> FormatResult: ...


def input_digest(entry, formatter, context):
    return digest((entry, context, formatter.id, formatter.version))


class IdentityFormatter:
    id = 'identity'
    version = '1'

    def format(self, entry, context):
        if any('{{' in value or '}}' in value for value in (*entry.tags, entry.natural_language or '')):
            raise PromptError('formatter:unavailable')
        return FormatResult(entry, self.id, self.version, input_digest(entry, self, context), digest(entry))


IDENTITY = IdentityFormatter()


def apply_formatter(entry: PromptEntry, *, formatter: Formatter = IDENTITY, context: FormatContext) -> FormatResult:
    resolve_profile(context.profile)
    try:
        if not isinstance(formatter, IdentityFormatter):
            for name in PLACEHOLDER.findall(' '.join((*entry.tags, entry.natural_language or ''))):
                if name not in VARIABLES:
                    raise PromptError('formatter:unavailable')
                if name not in context.values:
                    raise PromptError('formatter:missing_variable')
            if any(not isinstance(v, str) or '{{' in v or '}}' in v for v in context.values.values()):
                raise PromptError('formatter:unavailable')
        result = formatter.format(entry, context)
        if not isinstance(result, FormatResult) or not isinstance(result.entry, PromptEntry):
            raise PromptError('formatter:failed')
        if (result.formatter_id, result.formatter_version, result.input_digest, result.output_digest) != (
                formatter.id, formatter.version, input_digest(entry, formatter, context), digest(result.entry)):
            raise PromptError('formatter:failed')
        if any('{{' in value or '}}' in value for value in (*result.entry.tags, result.entry.natural_language or '')):
            raise PromptError('formatter:unavailable')
        validate_content(result.entry)
        return result
    except PromptError:
        raise
    except Exception:
        raise PromptError('formatter:failed') from None
