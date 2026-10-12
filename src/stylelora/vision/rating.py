"""Pure rating chain. The optional detector is reserved and never invoked."""
from dataclasses import dataclass
from stylelora.tagging.normalization import normalize_tag

RANK = {'general': 0, 'sensitive': 1, 'questionable': 2, 'explicit': 3}


@dataclass(frozen=True)
class RatingDecision:
    rating: str | None
    verified: bool
    route: str
    reason: str
    source: str | None = None


def decision_for(rating, verified, source):
    return RatingDecision(rating, verified, 'nsfw' if RANK[rating] >= 2 else 'normal',
                          source if source != 'manual' else 'default', source)


def r0_applies(decision):
    return decision.route == 'normal' and not decision.verified


class RatingRouter:
    def __init__(self, *, keywords=(), model=None, fallback=False, detector=None):
        self.keywords = tuple((normalize_tag(key), rating) for key, rating in keywords)
        if any(rating not in RANK for _, rating in self.keywords):
            raise ValueError('rating_invalid')
        self.model, self.fallback = model, fallback

    def keyword(self, tags):
        matches = [(rating, tag) for tag in tags for key, rating in self.keywords
                   if normalize_tag(tag.tag) == key]
        return max(matches, key=lambda value: RANK[value[0]]) if matches else None

    def local(self, context):
        cached = context.rating
        if cached is not None and cached.verified:
            return decision_for(cached.rating, cached.verified, cached.source)
        found = self.keyword(context.evidence.original_tags + context.evidence.wd_tags)
        if found:
            return decision_for(found[0], True, 'keyword')
        return None

    async def decide(self, context, *, llm=None):
        local = self.local(context)
        if local:
            return local
        if self.model is not None and llm is not None:
            value = await self.model(context, llm)
            if value.rating in RANK:
                return value
        return self.default()

    def default(self):
        if self.fallback:
            return decision_for('questionable', False, 'fallback')
        return RatingDecision(None, False, 'normal', 'default', None)
