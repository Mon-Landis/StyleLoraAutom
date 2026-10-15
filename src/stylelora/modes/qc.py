"""Project mode QC projection; does not modify review state."""
from dataclasses import dataclass
from .profiles import ProfileError


@dataclass(frozen=True)
class QcIssue:
    code: str
    severity: str
    image_id: int | None = None


def evaluate(value, profile):
    if not profile.enabled or profile.qc_policy != 'visible-content-v1':
        raise ProfileError('profile:unavailable')
    return tuple(QcIssue(code, 'error' if code == 'trigger:blocklist_conflict' else 'warning')
                 for code in dict.fromkeys(value.warnings))
