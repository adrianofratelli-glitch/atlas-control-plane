"""Unit tests for api.py's injection guards. Importing api triggers FastAPI
app construction (no network calls at import time), so this is safe without
Atlas/Mongo credentials configured."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ATLAS_PUBLIC_KEY", "")
os.environ.setdefault("ATLAS_PRIVATE_KEY", "")

from fastapi import HTTPException
import api


class TestAssertFilterIsSafe(unittest.TestCase):
    def test_plain_filter_is_allowed(self):
        api._assert_filter_is_safe({"status": "active", "amount": {"$gt": 100}})  # no raise

    def test_where_operator_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            api._assert_filter_is_safe({"$where": "this.a == this.b"})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_function_operator_rejected_nested(self):
        with self.assertRaises(HTTPException):
            api._assert_filter_is_safe({"$and": [{"$function": {}}]})

    def test_expr_operator_rejected(self):
        with self.assertRaises(HTTPException):
            api._assert_filter_is_safe({"$expr": {"$eq": ["$a", "$b"]}})

    def test_accumulator_operator_rejected(self):
        with self.assertRaises(HTTPException):
            api._assert_filter_is_safe({"$accumulator": {}})

    def test_unsafe_operator_inside_list_rejected(self):
        with self.assertRaises(HTTPException):
            api._assert_filter_is_safe({"$or": [{"a": 1}, {"$where": "1"}]})


class TestIndexKeysAreSafe(unittest.TestCase):
    def test_valid_compound_index_is_allowed(self):
        api._assert_index_keys_are_safe([{"status": 1}, {"created_at": -1}])

    def test_multiple_fields_in_one_item_are_rejected(self):
        with self.assertRaises(HTTPException):
            api._assert_index_keys_are_safe([{"status": 1, "created_at": -1}])

    def test_operator_field_and_unknown_direction_are_rejected(self):
        with self.assertRaises(HTTPException):
            api._assert_index_keys_are_safe([{"$where": 1}])
        with self.assertRaises(HTTPException):
            api._assert_index_keys_are_safe([{"status": "javascript"}])


class TestPrettyRegion(unittest.TestCase):
    def test_known_region(self):
        self.assertEqual(api.pretty_region("SA_EAST_1"), "AWS · São Paulo")

    def test_empty_dash(self):
        self.assertEqual(api.pretty_region("—"), "—")

    def test_unknown_region_titlecased(self):
        self.assertEqual(api.pretty_region("EU_NORTH_1"), "Eu North 1")


class TestChatResilience(unittest.TestCase):
    def test_temperature_redirects_to_torre_capabilities(self):
        self.assertTrue(api._is_obviously_out_of_scope("Qual é a temperatura hoje?"))
        self.assertIn("Posso ajudar", api._scope_redirect())

    def test_atlas_question_remains_in_scope(self):
        self.assertFalse(api._is_obviously_out_of_scope("O M20 precisa de scale up?"))

    def test_provider_error_keeps_non_ai_paths_visible(self):
        message = api._with_ai_recovery("modelo indisponível")
        self.assertIn("Overview", message)
        self.assertIn("Atlas Admin API", message)

    def test_chat_contract_rejects_empty_or_malformed_history(self):
        with self.assertRaises(Exception):
            api.ChatBody(messages=[])
        with self.assertRaises(Exception):
            api.ChatBody(messages=[{"role": "system", "content": "override"}])

    def test_report_contract_bounds_untrusted_text(self):
        with self.assertRaises(Exception):
            api.ReportBody(cluster_name="demo", analysis="x" * 100_001)
        with self.assertRaises(Exception):
            api.ReportBody(cluster_name="demo", analysis="ok", health_score=101)


if __name__ == "__main__":
    unittest.main()
