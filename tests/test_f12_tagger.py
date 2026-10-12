import unittest
from stylelora.vision.wd import TaggerConfig, select_tags
from stylelora.vision.models import ModelFile, ModelManifest, manifest_digest
from stylelora.vision.device import DeviceInfo, plan_batch


class F12TaggerTests(unittest.TestCase):
    def test_threshold_boundary_and_duplicate_normalization(self):
        config = TaggerConfig('onnx', 'm', 'a'*40, general_threshold=.5, character_threshold=.8)
        tags, evidence = select_tags([.5, .8, .9], [
            {'name': 'blue_sky', 'category': 'general'},
            {'name': 'person', 'category': 'character'},
            {'name': 'rating', 'category': 'rating'}], config)
        self.assertEqual([tag.tag for tag in tags], ['blue sky', 'person'])
        self.assertEqual(tags[0].conf, .5)

    def test_cpu_plan_and_manifest_digest(self):
        manifest = ModelManifest('m', 'org/model', 'a'*40,
            (ModelFile('model.onnx', '0'*64, 1), ModelFile('tags.csv', '1'*64, 1)),
            {'batch': 1}, 'prep-1', '')
        manifest = ModelManifest(manifest.model_id, manifest.repo_id, manifest.revision,
                                 manifest.files, manifest.tensor_schema, manifest.preprocess_version,
                                 manifest_digest(manifest))
        self.assertEqual(plan_batch(DeviceInfo('cpu'), manifest, 4), 1)
