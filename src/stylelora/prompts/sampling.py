"""Deterministic finite candidate generation and bounded exact backtracking."""
import itertools
import random
from dataclasses import dataclass, field, replace
from .types import *
from .validation import tag_key, normalize_tag, validate_content
from .coverage import quotas, bounds, classify, evaluate, matches
from .routing import classify_rating, fenced


@dataclass(frozen=True)
class Sample:
    entries: tuple[PromptEntry,...]
    candidates: tuple[PromptEntry,...]
    report: CoverageReport
    complete: bool
    ratings: object = field(default_factory=dict)
    dimensions: object = field(default_factory=dict)


def build_feature_snapshot(source=None, *, launcher=False):
    if launcher: raise PromptError('launcher:unsupported_schema')
    if source is None: raise PromptError('schema:invalid')
    return source


def feature_entries(features, plan):
    dims=sorted(features.dimensions, key=lambda d: (d!='count',d))
    values=[sorted(features.dimensions[d],key=lambda c:(c.tag,c.rating or '')) for d in dims]
    for row in itertools.product(*values):
        no_humans=any(c.tag=='no humans' for c in row)
        row=tuple(c for c in row if not (no_humans and plan.compatibility_version=='default-v1' and c.dimension in ('appearance','clothing','pose','expression')))
        ratings=[c.rating for c in row]
        rating=max((r for r in ratings if r),key=RATINGS.index,default=None)
        # Lower bound is kept; verification is tracked separately by routing at feature boundaries.
        yield PromptEntry(tuple(c.tag for c in row),rating=rating),tuple(ratings),{c.dimension:(c.tag,) for c in row}


def sample(request, *, dictionaries=(), features=None, catalog=None, keywords=None, candidate_limit=10000, search_nodes=100000):
    rng=random.Random(request.plan.random_seed); diagnostics={}; pool={}; complete=True; decisions={}; dimensions={}
    def add(entry,source,ratings=None,feature_map=None):
        entry=replace(entry,tags=tuple(dict.fromkeys(normalize_tag(t,catalog) for t in entry.tags)))
        try: validate_content(entry,catalog=catalog)
        except PromptError as e:
            diagnostics[e.code]=diagnostics.get(e.code,0)+1; return
        decision=classify_rating(entry,request.fence,keywords=keywords,ratings=ratings)
        if fenced(decision,request.fence):
            diagnostics['fenced']=diagnostics.get('fenced',0)+1; return
        tags=set(tag_key(entry,catalog))
        if any(matches(p,tags) for p in request.plan.exclude): return
        key=tag_key(entry,catalog)
        if key in pool:
            diagnostics['duplicate']=diagnostics.get('duplicate',0)+1; return
        pool[key]=(entry,source)
        decisions[key]=decision
        if feature_map is not None: dimensions[key]={d:tuple(normalize_tag(t,catalog) for t in ts) for d,ts in feature_map.items()}
        else:
            dims={}
            for tag in entry.tags:
                hit=catalog.lookup(tag) if catalog else None
                dim=hit.category if hit and hit.category in ('count','character','appearance','clothing','pose','expression','scene','lighting','view','effects') else 'content'
                dims.setdefault(dim,[]).append(tag)
            dimensions[key]={d:tuple(ts) for d,ts in dims.items()}
    # Deduplicate each pool before assigning random priorities. More dictionaries never add weight.
    all_entries=sorted((e for d in dictionaries for e in d.entries),key=lambda e:(tag_key(e,catalog),e.natural_language or '',e.aspect or '',e.rating or ''))
    unique={}
    for e in all_entries: unique.setdefault(tag_key(e,catalog),e)
    all_entries=list(unique.values())
    for entry in all_entries[:candidate_limit]: add(entry,'dictionary_pool')
    if len(all_entries)>candidate_limit: complete=False
    if features is not None:
        for i,(entry,ratings,feature_map) in enumerate(feature_entries(features,request.plan)):
            if i>=candidate_limit: complete=False; break
            add(entry,'feature_pool',ratings,feature_map)
    ordered=[]
    # Exponential race gives equal aggregate preference to the two pools, regardless of pool size.
    sizes={s:sum(source==s for _,source in pool.values()) for s in ('dictionary_pool','feature_pool')}
    import math
    for key,(entry,source) in sorted(pool.items()):
        priority=-math.log(max(rng.random(),1e-300))*sizes[source]/request.source_weights.get(source,1)
        ordered.append((priority,key,entry,source))
    ordered.sort(); candidates=tuple(x[2] for x in ordered); sources={tag_key(x[2],catalog):x[3] for x in ordered}
    entries,report=select(request,candidates,complete=complete,search_nodes=search_nodes,diagnostics=diagnostics)
    source_counts={s:sum(sources[tag_key(e,catalog)]==s for e in entries) for s in sizes}
    return Sample(entries,candidates,replace(report,source_counts=source_counts),complete,decisions,dimensions)


def select(request, candidates, *, complete=True, search_nodes=100000, diagnostics=None):
    plan=request.plan; targets=quotas(plan) if request.coverage_enabled else {'random':plan.target_count}
    diagnostics=dict(diagnostics or {}); groups={c:[] for c in targets}; limits=[bounds(c,plan.target_count) for c in plan.constraints]
    seen=set()
    for entry in candidates:
        key=tag_key(entry)
        if key in seen: continue
        seen.add(key)
        cell,error=classify(plan,entry) if request.coverage_enabled else ('random',None)
        if error: diagnostics[error]=diagnostics.get(error,0)+1
        if cell in groups: groups[cell].append(entry)
    if not targets or any(lo>hi for lo,hi in limits):
        return (),evaluate(plan,(),enabled=request.coverage_enabled,diagnostics=diagnostics)
    order=sorted(groups,key=lambda c:(len(groups[c])-targets[c],c))
    flat=[(cell,e) for cell in order for e in groups[cell]]
    memberships=[tuple(normalize_tag(c.tag) in set(e.tags) for c in plan.constraints) for _,e in flat]
    # Explicit stack avoids Python recursion limits for up to 10k target items.
    stack=[(0,(),dict.fromkeys(targets,0),tuple(0 for _ in limits))]; nodes=0; best=(); solution=None
    while stack:
        if nodes>=search_nodes: break
        pos,chosen,used,counts=stack.pop(); nodes+=1
        if len(chosen)>len(best): best=chosen
        if len(chosen)==plan.target_count:
            if all(lo<=counts[i]<=hi for i,(lo,hi) in enumerate(limits)): solution=chosen; break
            continue
        if pos>=len(flat) or len(chosen)+len(flat)-pos<plan.target_count: continue
        remaining={c:0 for c in targets}; potential=[0]*len(limits)
        for j in range(pos,len(flat)):
            remaining[flat[j][0]]+=1
            for k,has in enumerate(memberships[j]): potential[k]+=has
        if any(used[c]+remaining[c]<targets[c] for c in targets): continue
        if any(counts[k]>hi or counts[k]+potential[k]<lo for k,(lo,hi) in enumerate(limits)): continue
        cell,entry=flat[pos]
        stack.append((pos+1,chosen,used,counts))
        next_counts=tuple(a+b for a,b in zip(counts,memberships[pos]))
        if used[cell]<targets[cell] and all(next_counts[k]<=hi for k,(_,hi) in enumerate(limits)):
            nxt=dict(used); nxt[cell]+=1
            stack.append((pos+1,chosen+(pos,),nxt,next_counts))
    chosen=solution if solution is not None else best
    selected_keys={tag_key(flat[i][1]) for i in chosen}
    entries=tuple(e for e in candidates if tag_key(e) in selected_keys)
    exhausted=solution is None and (bool(stack) or not complete)
    return entries,evaluate(plan,entries,enabled=request.coverage_enabled,exhausted=exhausted,diagnostics=diagnostics)


def sample_seeds(plan, features):
    from .seeds import serialize_content
    result=sample(SamplingRequest(plan),features=features)
    if result.report.status!='satisfied': raise PromptError('coverage:'+result.report.status)
    return tuple(SeedRecord(i+1,serialize_content(e),e.aspect,tag_key(e)) for i,e in enumerate(result.entries))


def deduplicate_seeds(records):
    seen=set(); result=[]
    for r in records:
        key=tuple(sorted(set(normalize_tag(t) for t in r.normalized_tags)))
        if key in seen: continue
        seen.add(key); result.append(r)
    return tuple(SeedRecord(i+1,r.prompt,r.aspect,r.normalized_tags) for i,r in enumerate(result))


def plan_coverage(request, **kwargs):
    return sample(request,**kwargs).report
