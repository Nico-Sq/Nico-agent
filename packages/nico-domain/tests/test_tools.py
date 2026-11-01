import unittest

from nico_domain import tool_is_read_only


class ToolPolicyTests(unittest.TestCase):
    def test_only_explicit_true_is_read_only(self):
        self.assertTrue(tool_is_read_only(True))
        for value in (False, 1, "true", None):
            with self.subTest(value=value):
                self.assertFalse(tool_is_read_only(value))


if __name__ == "__main__":
    unittest.main()
