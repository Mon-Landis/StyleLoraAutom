from dataclasses import replace
from unittest.mock import patch

from support import *
from stylelora.project_db.errors import *
from stylelora.project_db.merge_metadata import effective_evidence
from stylelora.project_db.repositories import operations, schemes


class RuleTests(ProjectCase):
    def test_U01_rule_log_failure_rolls_back(self):
        image = self.image(('a',))
        plan = self.rule(image,('b',))
        from stylelora.project_db.connection import WriteSession
        original = WriteSession.execute
        def fail(db,sql,parameters=()):
            if sql.startswith('INSERT INTO operation_log'):
                raise RuntimeError('injected')
            return original(db,sql,parameters)
        with patch.object(WriteSession,'execute',fail), self.assertRaises(RuntimeError):
            self.write(lambda db: cmd.apply_rule(db,plan))
        self.assertEqual(self.count('operation_log'),0)
        self.assertIsNone(self.read(lambda db: annotations.get_current(db,image.image_id,'rule','tags')))

    def test_U02_same_result_no_log_and_no_dynamic_rule(self):
        image = self.image(('a',))
        plan = self.rule(image,('b',))
        self.write(lambda db: cmd.apply_rule(db,plan))
        again = self.rule(image,('b',))
        result = self.write(lambda db: cmd.apply_rule(db,again))
        self.assertEqual(result,RuleApplyResult(None,0))
        self.assertEqual(self.count('operation_log'),1)
        prior = self.read(lambda db: annotations.get_current(db,image.image_id,'rule','tags'))
        job = self.job((image.image_id,))
        self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,self.outcome(job,image,tag='new')))
        self.assertEqual(self.read(lambda db: annotations.get_current(db,image.image_id,'rule','tags')),prior)

    def test_U03_undo_null_previous_and_replay(self):
        image = self.image(('a',))
        plan = self.rule(image,('b',))
        result = self.write(lambda db: cmd.apply_rule(db,plan))
        self.assertTrue(self.write(lambda db: cmd.undo_rule(db,result.op_id)).changed)
        self.assertFalse(self.write(lambda db: cmd.undo_rule(db,result.op_id)).changed)
        self.assertIsNone(self.read(lambda db: annotations.get_current(db,image.image_id,'rule','tags')))
        self.assertEqual(self.count('annotations'),2)

    def test_U04_latest_and_later_annotation_conflicts(self):
        image = self.image(('a',))
        first = self.rule(image,('b',))
        a = self.write(lambda db: cmd.apply_rule(db,first))
        second = self.rule(image,('c',))
        b = self.write(lambda db: cmd.apply_rule(db,second))
        with self.assertRaises(UndoConflict):
            self.write(lambda db: cmd.undo_rule(db,a.op_id))
        self.write(lambda db: cmd.undo_rule(db,b.op_id))
        self.write(lambda db: cmd.undo_rule(db,a.op_id))
        third = self.rule(image,('d',))
        c = self.write(lambda db: cmd.apply_rule(db,third))
        refs = self.refs(image.image_id)
        self.write(lambda db: cmd.edit_manual_tags(db,image.image_id,tags=(Tag('e'),),read_set=refs))
        with self.assertRaises(UndoConflict):
            self.write(lambda db: cmd.undo_rule(db,c.op_id))
        self.assertEqual(self.read(lambda db: operations.get_operation(db,c.op_id)).status,'applied')

    def test_U05_preview_absence_is_evidence(self):
        image = self.image(('a',))
        plan = self.rule(image,('b',))
        job = self.job((image.image_id,))
        self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,self.outcome(job,image)))
        with self.assertRaises(PreviewStale):
            self.write(lambda db: cmd.apply_rule(db,plan))
        self.assertEqual(self.count('operation_log'),0)

    def test_U06_removal_boundary_and_new_wd(self):
        image = self.image(('a',))
        plan = self.rule(image,('b',))
        first = self.write(lambda db: cmd.apply_rule(db,plan))
        refs = self.refs(image.image_id)
        evidence,_ = self.read(lambda db: effective_evidence(db,image.image_id,refs))
        self.assertEqual(set(evidence),{'b'})
        job = self.job((image.image_id,))
        self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,self.outcome(job,image,tag='a')))
        refs = self.refs(image.image_id)
        evidence,_ = self.read(lambda db: effective_evidence(db,image.image_id,refs))
        self.assertEqual(set(evidence),{'a','b'})
        plan = self.rule(image,('b',))
        second = self.write(lambda db: cmd.apply_rule(db,plan))
        refs = self.refs(image.image_id)
        evidence,_ = self.read(lambda db: effective_evidence(db,image.image_id,refs))
        self.assertEqual(set(evidence),{'b'})
        self.write(lambda db: cmd.undo_rule(db,second.op_id))
        refs = self.refs(image.image_id)
        evidence,_ = self.read(lambda db: effective_evidence(db,image.image_id,refs))
        self.assertEqual(set(evidence),{'a','b'})

    def test_U07_caption_configuration_persistence(self):
        # Caption execution, WD thresholds, NL selection and F09 sorter are outside
        # F01. This test covers only the persistence side of acceptance U07.
        value = self.read(lambda db: schemes.get_export_scheme(db,1))
        self.assertEqual(set(value.caption_config_json['tag_layers']),{'manual','rule','llm','wd','original'})
        self.assertEqual(value.caption_config_json['nl_priority'],['manual','llm','original'])
        config = {**value.caption_config_json,'tag_sort':'prompt_pyramid','wd_min_confidence':0.4}
        modified = ExportSchemeInput(value.name,caption_config_json=config,filter_json=value.filter_json,output_config_json=value.output_config_json)
        updated = self.write(lambda db: schemes.update_export_scheme(db,1,modified,expected_updated_at=value.updated_at)).value
        self.assertEqual(updated.caption_config_json,config)

    def test_U08_copy_preserves_origin_and_cutoff(self):
        image = self.image(('A_tag','c'))
        plan = self.rule(image,('c',))
        self.write(lambda db: cmd.apply_rule(db,plan))
        prior = self.read(lambda db: annotations.get_current(db,image.image_id,'rule','tags'))
        plan = self.rule(image,('c','d'))
        self.write(lambda db: cmd.apply_rule(db,plan))
        current = self.read(lambda db: annotations.get_current(db,image.image_id,'rule','tags'))
        self.assertEqual(prior.params_json['merge_meta']['removals'],current.params_json['merge_meta']['removals'])
        self.assertEqual(prior.params_json['merge_meta']['tag_origins']['c'],current.params_json['merge_meta']['tag_origins']['c'])
        refs = self.refs(image.image_id)
        result = self.write(lambda db: cmd.edit_manual_tags(db,image.image_id,tags=(Tag('a tag'),Tag('c'),Tag('d')),read_set=refs)).value
        self.assertEqual(result.params_json['merge_meta']['tag_origins']['a tag'][0]['annotation_id'],result.annotation_id)

    def test_U09_copied_wd_evidence_and_semantic_change(self):
        image = self.image()
        job = self.job((image.image_id,))
        self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,self.outcome(job,image,tag='A_tag')))
        wd = self.read(lambda db: annotations.get_current(db,image.image_id,'wd','tags'))
        plan = self.rule(image,('a tag',))
        self.write(lambda db: cmd.apply_rule(db,plan))
        first = self.read(lambda db: annotations.get_current(db,image.image_id,'rule','tags'))
        origin = first.params_json['merge_meta']['tag_origins']['a tag'][0]
        self.assertEqual(origin,{'layer':'wd','annotation_id':wd.annotation_id,'conf':0.8})
        job = self.job((image.image_id,),mode='all')
        self.write(lambda db: cmd.commit_job_outcome(db,job.job_id,self.outcome(job,image,tag='A_tag')))
        plan = self.rule(image,('a tag',))
        self.assertEqual(self.write(lambda db: cmd.apply_rule(db,plan)).affected_count,1)
        second = self.read(lambda db: annotations.get_current(db,image.image_id,'rule','tags'))
        self.assertEqual(first.content,second.content)
        self.assertNotEqual(first.params_json,second.params_json)
