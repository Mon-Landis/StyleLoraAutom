"""Integer coverage quotas, classification and final acceptance."""
import itertools
import json
import math
import random
import secrets
from .types import *
from .validation import normalize_tag, require


def default_plan(*, target_count=100, random_seed=None):
    random_seed=secrets.randbits(64) if random_seed is None else random_seed
    return CoveragePlan(('scene','lighting'), {'scene':('forest','beach','city street'),'lighting':('sunlight','night')},
        (ConstraintSpec('count','no humans',minimum_ratio=.15),ConstraintSpec('view','wide shot',minimum_ratio=.1)),
        target_count,random_seed,compatibility_version='default-v1')


def matches(predicate, tags):
    return {normalize_tag(t) for t in predicate.all_tags} <= tags and not {normalize_tag(t) for t in predicate.none_tags} & tags


def cell_id(plan, values):
    return json.dumps(dict(zip(plan.primary_dimensions,values)),sort_keys=True,separators=(',',':'),ensure_ascii=False)


def quotas(plan):
    cells=[]
    for values in itertools.product(*(tuple(normalize_tag(t) for t in plan.cell_values[d]) for d in plan.primary_dimensions)):
        # Only predicates decidable on primary tags remove cells. Other predicates filter full candidates.
        primary=set(values)
        primary_domain={normalize_tag(t) for values in plan.cell_values.values() for t in values}
        if any(set(map(normalize_tag,p.all_tags+p.none_tags)) <= primary_domain and matches(p,primary) for p in plan.exclude):
            continue
        cells.append(cell_id(plan,values))
    cells.sort()
    require(not set(plan.cell_weights)-set(cells))
    if not cells or plan.target_count<len(cells):
        return {}
    rng=random.Random(plan.random_seed); tie=cells[:]; rng.shuffle(tie); rank={c:i for i,c in enumerate(tie)}
    weights={c:plan.cell_weights.get(c,1) for c in cells}; total=sum(weights.values()); rest=plan.target_count-len(cells)
    result={c:1+rest*weights[c]//total for c in cells}
    order=sorted(cells,key=lambda c:(-(rest*weights[c]%total),rank[c]))
    for c in order[:plan.target_count-sum(result.values())]: result[c]+=1
    return result


def bounds(constraint, count):
    # Decimal input avoids binary .14*100 unexpectedly rounding up to 15.
    from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
    low=int((Decimal(str(constraint.minimum_ratio or 0))*count).to_integral_value(rounding=ROUND_CEILING))
    high=int((Decimal(str(1 if constraint.maximum_ratio is None else constraint.maximum_ratio))*count).to_integral_value(rounding=ROUND_FLOOR))
    return max(low,constraint.minimum_count or 0), min(high,count if constraint.maximum_count is None else constraint.maximum_count)


def classify(plan, entry):
    tags={normalize_tag(t) for t in entry.tags}; values=[]
    for dim in plan.primary_dimensions:
        hits=tags & {normalize_tag(t) for t in plan.cell_values[dim]}
        if len(hits)!=1: return None,'ambiguous' if hits else 'unclassified'
        values.append(next(iter(hits)))
    return cell_id(plan,values),None


def evaluate(plan, entries, *, enabled=True, exhausted=False, diagnostics=None, source_counts=None):
    targets=quotas(plan) if enabled else {'random':plan.target_count}
    cells=dict.fromkeys(targets,0); counts={str(i):0 for i in range(len(plan.constraints))}; deficits={}
    for entry in entries:
        cell,error=classify(plan,entry) if enabled else ('random',None)
        if cell in cells: cells[cell]+=1
        tags={normalize_tag(t) for t in entry.tags}
        for i,c in enumerate(plan.constraints): counts[str(i)]+=normalize_tag(c.tag) in tags
    if enabled and not targets: deficits['cells']=1
    for c,target in targets.items():
        if cells[c]!=target: deficits[c]=abs(target-cells[c])
    for i,c in enumerate(plan.constraints):
        low,high=bounds(c,plan.target_count); actual=counts[str(i)]
        if actual<low or actual>high: deficits['constraint:'+str(i)]=max(low-actual,actual-high)
    if len(entries)!=plan.target_count: deficits['count']=abs(plan.target_count-len(entries))
    status='satisfied' if not deficits else ('search_exhausted' if exhausted else 'unsatisfiable')
    return CoverageReport(plan.target_count,len(entries),cells,counts,deficits,status,targets,diagnostics or {},source_counts or {})


def plan_from_json(data):
    from .validation import strict_loads, construct
    obj=strict_loads(data) if isinstance(data,(str,bytes)) else dict(data)
    obj['constraints']=tuple(construct(ConstraintSpec,c) for c in obj.get('constraints',()))
    obj['exclude']=tuple(construct(Predicate,p) for p in obj.get('exclude',()))
    return construct(CoveragePlan,obj)


def export_plan(plan):
    return json.dumps(plain(plan),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def save_plan(plan_id,plan,*,root=None):
    from .validation import identifier
    from .dictionaries import _root, _write, LOCK
    with LOCK:
        _write(_root(root).parent/'coverage'/(identifier(plan_id)+'.json'),plain(plan))


def load_plan(plan_id,*,root=None):
    from .validation import identifier
    from .dictionaries import _root
    return plan_from_json((_root(root).parent/'coverage'/(identifier(plan_id)+'.json')).read_bytes())
