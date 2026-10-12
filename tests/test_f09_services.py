from dataclasses import replace
from unittest.mock import patch
from support import ProjectCase
from f09_support import catalog
from stylelora.project_db import commands
from stylelora.project_db.repositories import annotations, operations
from stylelora.project_db.types import Tag, Scope, ImageFilter, OriginalInput, PageRequest
from stylelora.project_db.errors import CurrentConflict, PreviewStale, UndoConflict, BatchTooLarge, ValidationError, CommitUnknown
from stylelora.annotations.projection import get_annotation_view, read_rows
from stylelora.annotations.editing import ManualEdit, edit_manual, bulk_edit_manual
from stylelora.annotations.merge import merge_annotations
from stylelora.rules.service import RuleService, PreviewExpired, undo_latest_rule
from stylelora.rules.types import RuleSpec, RuleGroup


def write_rule(*tags):
    return RuleSpec(write=RuleGroup('any',tags))


class EditingRuleTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.service = RuleService(catalog=catalog())

    def preview(self,image,rule):
        return self.service.preview_rule(self.project,rule,Scope(selected_ids=(image.image_id,)))

    def apply(self,preview):
        return self.service.apply_preview(self.project,preview.preview_id,preview.digest)

    def manual(self,image,tags,*,nl=None,write_nl=False):
        view = get_annotation_view(self.project,image.image_id)
        return edit_manual(self.project,ManualEdit(image.image_id,tuple(Tag(t) for t in tags),nl,view.refs,write_nl))

    def test_T06_projection_overlap_and_manual_keeps_original(self):
        image = self.image(('a',))
        job = self.job((image.image_id,))
        self.write(lambda db:commands.commit_job_outcome(db,job.job_id,self.outcome(job,image,tag='A')))
        view = get_annotation_view(self.project,image.image_id)
        self.assertEqual(view.wd_overlap,{'a':True})
        self.assertEqual(view.layers['wd'].content[0].conf,.8)
        self.manual(image,('b',))
        original = self.read(lambda db:annotations.get_current(db,image.image_id,'original','tags'))
        self.assertEqual(original.content,(Tag('a'),))
        empty = self.image(payload=b'other')
        job = self.job((empty.image_id,))
        self.write(lambda db:commands.commit_job_outcome(db,job.job_id,self.outcome(job,empty,tag='c')))
        view = get_annotation_view(self.project,empty.image_id)
        self.assertTrue(view.missing_original)
        self.assertEqual(view.wd_overlap,{'c':True})

    def test_T07_manual_noop_stale_and_nl_atomic(self):
        image = self.image(('a',))
        old = get_annotation_view(self.project,image.image_id)
        first = self.manual(image,('b',),nl='manual NL',write_nl=True)
        self.assertTrue(first.changed)
        self.assertFalse(self.manual(image,('b',),nl='manual NL',write_nl=True).changed)
        with self.assertRaises(CurrentConflict):
            edit_manual(self.project,ManualEdit(image.image_id,(Tag('b'),),'manual NL',old.refs,True))
        self.assertEqual(get_annotation_view(self.project,image.image_id).natural_language,'manual NL')
        job = self.job((image.image_id,),executor='llm_vision',targets=['nl'])
        self.write(lambda db:commands.commit_job_outcome(db,job.job_id,self.outcome(job,image,tag='llm NL')))
        self.manual(image,('b',),nl=None,write_nl=True)
        self.assertEqual(get_annotation_view(self.project,image.image_id).natural_language,'llm NL')

    def test_T07_bulk_all_or_nothing_and_existing_add_evidence(self):
        a,b = self.image(('a',)),self.image(('a',),payload=b'b')
        expected = {i.image_id:get_annotation_view(self.project,i.image_id).refs for i in (a,b)}
        self.manual(b,('c',))
        with self.assertRaises(CurrentConflict):
            bulk_edit_manual(self.project,(a.image_id,b.image_id),add=(Tag('d'),),remove=(),expected=expected)
        self.assertIsNone(self.read(lambda db:annotations.get_current(db,a.image_id,'manual','tags')))
        expected = {i.image_id:get_annotation_view(self.project,i.image_id).refs for i in (a,b)}
        result = bulk_edit_manual(self.project,(a.image_id,b.image_id),add=(Tag('d'),),remove=(Tag('a'),),expected=expected)
        self.assertEqual(result.changed_count,2)
        current = self.read(lambda db:annotations.get_current(db,a.image_id,'manual','tags'))
        expected = {a.image_id:get_annotation_view(self.project,a.image_id).refs}
        repeat = bulk_edit_manual(self.project,(a.image_id,),add=(Tag('d'),),remove=(),expected=expected)
        self.assertEqual(repeat.changed_count,0)
        self.assertEqual(current,self.read(lambda db:annotations.get_current(db,a.image_id,'manual','tags')))
        with self.assertRaises(ValidationError):
            bulk_edit_manual(self.project,(a.image_id,),add=(Tag('A'),),remove=(Tag('a'),),expected=expected)

    def test_T07_manual_nl_failure_rolls_back_tags(self):
        image = self.image(('a',))
        original = annotations.append_version
        def fail(db,value,**kwargs):
            if value.type == 'nl':
                raise ValidationError('injected')
            return original(db,value,**kwargs)
        with patch.object(annotations,'append_version',fail),self.assertRaises(ValidationError):
            self.manual(image,('b',),nl='x',write_nl=True)
        self.assertIsNone(self.read(lambda db:annotations.get_current(db,image.image_id,'manual','tags')))

    def test_T09_sequential_undo_and_later_wd_conflict(self):
        image = self.image(('a',))
        a = self.apply(self.preview(image,write_rule('b')))
        b = self.apply(self.preview(image,write_rule('c')))
        with self.assertRaises(UndoConflict):
            undo_latest_rule(self.project,a.op_id)
        undo_latest_rule(self.project,b.op_id)
        undo_latest_rule(self.project,a.op_id)
        self.assertFalse(undo_latest_rule(self.project,a.op_id).changed)
        a = self.apply(self.preview(image,write_rule('b')))
        job = self.job((image.image_id,))
        self.write(lambda db:commands.commit_job_outcome(db,job.job_id,self.outcome(job,image)))
        with self.assertRaises(UndoConflict):
            undo_latest_rule(self.project,a.op_id)

    def test_T10_condition_empty_original_does_not_fallback(self):
        source,_ = self.source()
        item = self.items(source)[0]
        originals = (OriginalInput('tags',()),)
        artifact = self.artifact(item,b'empty original',originals)
        image = self.write(lambda db:commands.complete_item(db,artifact,originals)).value
        job = self.job((image.image_id,))
        self.write(lambda db:commands.commit_job_outcome(db,job.job_id,self.outcome(job,image,tag='a')))
        rule = RuleSpec(write=RuleGroup('any',('b',)),condition=RuleGroup('any',('a',)))
        self.assertEqual(self.preview(image,rule).matched_count,0)
        missing = self.image(payload=b'no original')
        job = self.job((missing.image_id,))
        self.write(lambda db:commands.commit_job_outcome(db,job.job_id,self.outcome(job,missing,tag='a')))
        self.assertEqual(self.preview(missing,rule).matched_count,1)

    def test_T11_digest_tamper_replay_expiry_and_absence_stale(self):
        image = self.image(('a',))
        preview = self.preview(image,write_rule('b'))
        self.assertEqual(len(preview.plan.read_set),5)
        with self.assertRaises(PreviewStale):
            self.service.apply_preview(self.project,preview.preview_id,'tampered')
        result = self.apply(preview)
        self.assertEqual(self.apply(preview),result)
        self.assertEqual(self.count('operation_log'),1)
        stale = self.preview(image,write_rule('c'))
        job = self.job((image.image_id,))
        self.write(lambda db:commands.commit_job_outcome(db,job.job_id,self.outcome(job,image)))
        with self.assertRaises(PreviewStale):
            self.apply(stale)
        service = RuleService(clock=lambda:0)
        preview = service.preview_rule(self.project,write_rule('d'),Scope(selected_ids=(image.image_id,)))
        service.clock = lambda:901
        with self.assertRaises(PreviewExpired):
            service.apply_preview(self.project,preview.preview_id,preview.digest)

    def test_T11_nonmatching_candidates_frozen_noop_no_log(self):
        a,b = self.image(('a',)),self.image(('c',),payload=b'b')
        rule = RuleSpec(write=RuleGroup('any',('b',)),condition=RuleGroup('any',('a',)))
        preview = self.service.preview_rule(self.project,rule,Scope(selected_ids=(a.image_id,b.image_id)))
        self.assertEqual(preview.changed_count,1)
        self.manual(b,('c','d'))
        with self.assertRaises(PreviewStale):
            self.apply(preview)
        self.assertEqual(self.count('operation_log'),0)
        noop = self.preview(a,write_rule('a'))
        self.assertEqual(self.apply(noop).affected_count,0)
        self.assertEqual(self.count('operation_log'),0)

    def test_T26_limits_empty_selection_and_default_filter(self):
        image = self.image(('a',))
        with self.assertRaises(BatchTooLarge):
            self.service.preview_rule(self.project,write_rule('b'),Scope(selected_ids=tuple(range(1,5002))))
        with self.assertRaises(BatchTooLarge):
            self.preview(image,write_rule('z'*(16*1024*1024)))
        with self.assertRaises(ValidationError):
            self.service.preview_rule(self.project,write_rule('b'),Scope(selected_ids=()))
        preview = self.service.preview_rule(self.project,write_rule('b'),Scope(filter=ImageFilter(source_ids=())))
        self.assertEqual(self.apply(preview).affected_count,0)
        self.assertEqual(self.count('operation_log'),0)

    def test_T29_layer_removals_not_copied_to_other_operation(self):
        image = self.image(('a',))
        rule = RuleSpec(delete=RuleGroup('any',('a',)),write=RuleGroup('any',('b',)))
        self.apply(self.preview(image,rule))
        self.manual(image,('b','c'))
        manual = self.read(lambda db:annotations.get_current(db,image.image_id,'manual','tags'))
        self.assertEqual(manual.params_json['merge_meta']['removals'],[])
        from stylelora.caption.types import CaptionConfig
        rows,_ = self.read(lambda db:read_rows(db,image.image_id))
        view = merge_annotations(rows,replace(CaptionConfig(),tag_layers=('manual','original')))
        self.assertEqual({t.tag for t in view.tags},{'a','b','c'})

    def test_T29_rule_metadata_only_change(self):
        image = self.image()
        job = self.job((image.image_id,))
        self.write(lambda db:commands.commit_job_outcome(db,job.job_id,self.outcome(job,image,tag='a')))
        self.apply(self.preview(image,write_rule('b')))
        before = self.read(lambda db:annotations.get_current(db,image.image_id,'rule','tags'))
        job = self.job((image.image_id,),mode='all')
        self.write(lambda db:commands.commit_job_outcome(db,job.job_id,self.outcome(job,image,tag='a')))
        preview = self.preview(image,write_rule('b'))
        self.assertEqual(preview.changed_count,1)
        self.apply(preview)
        after = self.read(lambda db:annotations.get_current(db,image.image_id,'rule','tags'))
        self.assertEqual(before.content,after.content)
        self.assertNotEqual(before.params_json,after.params_json)

    def test_T26_unknown_commit_queries_evidence_never_replays(self):
        image = self.image(('a',))
        preview = self.preview(image,write_rule('b'))
        from stylelora.project_db import submit_write
        class UnknownFuture:
            def result(self):
                raise CommitUnknown('unknown')
        calls = []
        def committed(project,work):
            calls.append(1)
            submit_write(project,work).result()
            return UnknownFuture()
        with patch('stylelora.rules.service.submit_write',committed):
            result = self.apply(preview)
            self.assertEqual(self.apply(preview),result)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.count('operation_log'),1)
        preview = self.preview(image,write_rule('c'))
        with patch('stylelora.rules.service.submit_write',return_value=UnknownFuture()) as mocked:
            with self.assertRaises(CommitUnknown):
                self.apply(preview)
            with self.assertRaises(CommitUnknown):
                self.apply(preview)
            self.assertEqual(mocked.call_count,1)
