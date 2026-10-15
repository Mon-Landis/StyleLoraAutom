"""Local lower-bound rating, optional text classifier, and independent fence."""
from uuid import uuid4
from stylelora.llm.types import LlmRequest
from .types import *
from .validation import normalize_tag, require


def highest(values):
    return max((v for v in values if v in RATINGS),key=RATINGS.index,default=None)


def classify_rating(entry, policy, *, keywords=None, ratings=None):
    values=tuple(ratings) if ratings is not None else (entry.rating,)
    rating=highest(values); verified=bool(values) and all(v in RATINGS for v in values)
    reason='explicit' if rating else 'default'
    hits=[(keywords or {}).get(normalize_tag(t)) for t in entry.tags]
    keyword=highest(hits)
    if keyword is not None:
        rating=highest((rating,keyword)); reason='keyword'
        # A keyword is a verified lower bound, not proof that every other feature is safe.
    route='nsfw' if rating in ('questionable','explicit') else 'normal'
    if rating is None and policy.nsfwfallback:
        route='nsfw'; reason='nsfwfallback'
    return RatingDecision(rating,verified,reason,route)


def fenced(decision, policy):
    return policy.enabled and (decision.reason=='nsfwfallback' or decision.rating is not None and RATINGS.index(decision.rating)>RATINGS.index(policy.threshold))


def refused(decision):
    return RatingDecision(highest((decision.rating,'questionable')),True,'refused_nsfw','nsfw')


async def resolve_rating(entry, policy, llm, *, keywords=None, ratings=None, binding=None, cancel=None):
    decision=classify_rating(entry,policy,keywords=keywords,ratings=ratings)
    if decision.rating is not None or not policy.use_rating_model:
        return decision
    binding=binding or await llm.resolve_role('rating')
    if binding is None: return decision
    schema={'type':'object','properties':{'rating':{'enum':list(RATINGS)+['uncertain']}},'required':['rating'],'additionalProperties':False}
    reply=await llm.complete_json(LlmRequest(str(uuid4()),binding,
        'Classify content rating as general, sensitive, questionable, explicit or uncertain. Return only JSON.',
        {'tags':list(entry.tags),'natural_language':entry.natural_language},schema,cancel=cancel),repair_limit=1)
    rating=reply.value.get('rating'); require(rating in (*RATINGS,'uncertain'),'llm:schema_contract')
    if rating=='uncertain': return decision
    return RatingDecision(rating,True,'rating_model','nsfw' if rating in ('questionable','explicit') else 'normal')
