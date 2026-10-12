from collections import Counter
from dataclasses import replace, asdict
from threading import RLock
import copy
import time
from uuid import uuid4
from stylelora.project_db import read_snapshot, submit_write, commands
from stylelora.project_db.repositories import images, operations, annotations
from stylelora.project_db.types import Scope, ImageFilter, PageRequest, RulePlan, RuleImageChange, RuleApplyResult
from stylelora.project_db.errors import ValidationError, PreviewStale, BatchTooLarge, CommitUnknown
from stylelora.project_db.validation import digest, dumps, scope_json
from stylelora.annotations.projection import read_rows
from stylelora.annotations.merge import merge_annotations
from .types import parse_rule, RulePreview
from .engine import evaluate_rule


class PreviewExpired(PreviewStale):
    pass


class RuleService:
    def __init__(self, *, catalog=None, clock=time.monotonic):
        self.catalog, self.clock = catalog, clock
        self._previews, self._lock = {}, RLock()

    def preview_rule(self, project, rule, scope):
        rule = parse_rule(rule,catalog=self.catalog)
        scope_json(scope)
        if scope.selected_ids == ():
            raise ValidationError('rule:empty_selection')
        changes, refs, examples, reasons = [], [], [], Counter()
        matched = 0
        with read_snapshot(project) as db:
            if scope.selected_ids is not None:
                identities = tuple(scope.selected_ids)
                if len(identities)>5000:
                    raise BatchTooLarge('rule:batch_too_large')
            else:
                query = scope.filter
                if query.review_statuses is None:
                    query = replace(query,review_statuses=('keep','unreviewed'))
                identities, page = [], PageRequest(limit=1000)
                while True:
                    result = images.list_images(db,query,page)
                    identities.extend(r.image_id for r in result.items)
                    if len(identities)>5000:
                        raise BatchTooLarge('rule:batch_too_large')
                    if result.next_after_id is None:
                        break
                    page = PageRequest(result.next_after_id,1000,result.upper_id)
                identities = tuple(identities)
            for identity in identities:
                rows, read_set = read_rows(db,identity,nl=False)
                refs.extend(read_set)
                view = replace(merge_annotations(rows,editing=True),image_id=identity)
                original = next((r for r in rows if r.source == 'original'),None)
                if original is None:
                    reasons['rule:wd_fallback'] += 1
                    original = next((r for r in rows if r.source == 'wd'),None)
                delta = evaluate_rule(rule,view,original.content if original else None)
                if delta.reason:
                    reasons[delta.reason] += 1
                else:
                    matched += 1
                previous = next((r for r in rows if r.source == 'rule'),None)
                evidence_changed = (delta.reason is None and previous is not None and
                    previous.params_json.get('merge_meta',{}).get('tag_origins') !=
                    {t.tag:[asdict(o) for o in view.origins.get(t.tag,())] for t in delta.after})
                if delta.after != delta.before or evidence_changed:
                    prior = next(r.annotation_id for r in read_set if r.source == 'rule')
                    changes.append(RuleImageChange(identity,delta.after,prior,{}))
                if len(examples)<20:
                    examples.append(delta)
        frozen_scope = Scope(selected_ids=identities) if identities else scope
        plan = RulePlan(rule.to_json(),frozen_scope,tuple(changes),tuple(refs),'')
        if len(dumps(plan).encode('utf-8'))>16*1024*1024:
            raise BatchTooLarge('rule:batch_too_large')
        plan = replace(plan,preview_digest=commands.rule_digest(plan))
        signature = digest({'plan':plan,'catalog':self.catalog.revision if self.catalog else None})
        preview = RulePreview(uuid4().hex,signature,plan,matched,len(changes),tuple(examples),dict(reasons))
        with self._lock:
            self._previews[preview.preview_id] = [project,copy.deepcopy(preview),self.clock()+900,None,False]
        return preview

    def apply_preview(self, project, preview_id, signature):
        with self._lock:
            entry = self._previews.get(preview_id)
            if entry is None or entry[0] is not project or self.clock()>entry[2]:
                raise PreviewExpired('rule:preview_expired')
            preview = entry[1]
            if signature != preview.digest:
                raise PreviewStale('rule:preview_digest')
            if entry[3] is not None:
                return entry[3]
            if entry[4]:
                raise CommitUnknown('rule:outcome_unknown')
            try:
                entry[3] = submit_write(project,lambda db:commands.apply_rule(db,preview.plan)).result()
            except CommitUnknown:
                entry[4] = True
                # Read evidence only; never replay a possibly committed operation.
                with read_snapshot(project) as db:
                    latest = operations.latest_applied(db)
                    if latest and latest.rule_json == preview.plan.rule:
                        expected = {c.image_id:c for c in preview.plan.changes}
                        actual = {c['image_id']:c for c in latest.changes_json}
                        proven = bool(expected) and expected.keys() == actual.keys()
                        for identity,change in expected.items():
                            current = annotations.get_current(db,identity,'rule','tags')
                            record = actual.get(identity,{})
                            proven = proven and (current is not None and current.content == change.tags
                                and record.get('new_annotation_id') == current.annotation_id
                                and record.get('previous_annotation_id') == change.previous_rule_id
                                and current.annotation_id not in {r.annotation_id for r in preview.plan.read_set})
                        for ref in preview.plan.read_set:
                            if ref.source == 'rule' and ref.image_id in expected:
                                continue
                            current = annotations.get_current(db,ref.image_id,ref.source,ref.type)
                            proven = proven and (current.annotation_id if current else None) == ref.annotation_id
                        if proven:
                            entry[3] = RuleApplyResult(latest.op_id,latest.affected_count)
                            return entry[3]
                raise
            except PreviewStale:
                entry[2] = float('-inf')
                raise
            return entry[3]


_default = RuleService()
preview_rule = _default.preview_rule
apply_preview = _default.apply_preview


def undo_latest_rule(project, op_id):
    return submit_write(project,lambda db:commands.undo_rule(db,op_id)).result()
