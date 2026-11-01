import unittest

from nico_domain import (
    can_accept_chat_message,
    can_create_claim_from_material,
    is_current_chat_run,
    is_pending_memory_candidate,
    required_text,
)


class DomainRuleTests(unittest.TestCase):
    def test_required_text_trims_and_rejects_blank_values(self):
        self.assertEqual(required_text("  经历  "), "经历")
        with self.assertRaisesRegex(ValueError, "内容不能为空"):
            required_text("  ")

    def test_only_pending_memory_candidates_can_be_changed(self):
        self.assertTrue(is_pending_memory_candidate("pending"))
        self.assertFalse(is_pending_memory_candidate("confirmed"))

    def test_chat_message_and_run_versions_follow_session_state(self):
        self.assertTrue(can_accept_chat_message("active"))
        self.assertTrue(can_accept_chat_message("running"))
        self.assertFalse(can_accept_chat_message("paused"))
        self.assertTrue(is_current_chat_run("running", 4, 4))
        self.assertFalse(is_current_chat_run("active", 4, 4))
        self.assertFalse(is_current_chat_run("running", 4, 3))

    def test_claim_sources_must_be_active_resume_or_project_materials(self):
        self.assertTrue(can_create_claim_from_material("resume", None))
        self.assertTrue(can_create_claim_from_material("project", None))
        self.assertFalse(can_create_claim_from_material("job_description", None))
        self.assertFalse(can_create_claim_from_material("resume", "revoked"))


if __name__ == "__main__":
    unittest.main()
