import unittest

from stylelora.runtime.prompt_completion import (NSFW_DECLARATION, SFW_DECLARATION,
                                                 content_safety)


class ContentSafetyTests(unittest.TestCase):
    def test_declaration_follows_group_nsfw_flag(self):
        self.assertEqual(content_safety(False), SFW_DECLARATION)
        self.assertEqual(content_safety(True), NSFW_DECLARATION)
        self.assertIn('SFW ONLY', SFW_DECLARATION)
        self.assertIn('excludes any NSFW', SFW_DECLARATION)
        self.assertIn('NSFW REQUESTED', NSFW_DECLARATION)


if __name__ == '__main__':
    unittest.main()
