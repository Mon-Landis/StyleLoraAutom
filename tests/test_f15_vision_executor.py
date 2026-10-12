import unittest
from stylelora.vision.rating import RatingRouter
from stylelora.executors.types import ImageContext, ImageInput, Evidence, CancelToken
from pathlib import Path


class F15VisionTests(unittest.IsolatedAsyncioTestCase):
    def context(self, tags=(), rating=None):
        return ImageContext(ImageInput(1, Path('x.png'), 'a'*64, 1, 1, 1),
            Evidence(tuple(tags), '', (), (), 'd'*64), rating, {}, CancelToken())

    async def test_exact_keyword_highest_route(self):
        router = RatingRouter(keywords=(('nudity', 'questionable'), ('explicit tag', 'explicit')))
        decision = await router.decide(self.context(tags=[type('T', (), {'tag': 'nudity'})()]))
        self.assertEqual((decision.rating, decision.route, decision.verified), ('questionable', 'nsfw', True))

    async def test_normal_unverified_does_not_invent_rating(self):
        decision = await RatingRouter().decide(self.context())
        self.assertEqual((decision.rating, decision.route, decision.verified), (None, 'normal', False))

    def test_T24_extensions_are_disabled(self):
        from stylelora.executors.extensions import capabilities
        self.assertTrue(all(not value['enabled'] and value['status'] == 'unsupported'
                            for value in capabilities().values()))

    async def test_T15_reliable_model_cache_and_detector_reserved(self):
        from stylelora.project_db.types import RatingValue
        async def forbidden(*args):
            self.fail('must not call model or detector')
        router = RatingRouter(model=forbidden, detector=forbidden)
        result = await router.decide(self.context(rating=RatingValue('sensitive', 'model', True, '2026-01-01T00:00:00Z')), llm=object())
        self.assertEqual(result.route, 'normal')
        self.assertTrue(result.verified)
        self.assertEqual((await RatingRouter(detector=forbidden).decide(self.context())).rating, None)
