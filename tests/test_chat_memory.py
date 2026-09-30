"""Unit tests for chat_memory's conversation_id validation — an invalid id
must raise ValueError (mapped to HTTP 400 in api.py), not bson.errors.InvalidId
bubbling up as an unhandled 500."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chat_memory import _oid
from bson import ObjectId


class TestOidValidation(unittest.TestCase):
    def test_valid_object_id_string(self):
        valid = str(ObjectId())
        self.assertEqual(_oid(valid), ObjectId(valid))

    def test_invalid_object_id_raises_value_error(self):
        with self.assertRaises(ValueError):
            _oid("not-a-valid-object-id")

    def test_empty_string_raises_value_error(self):
        with self.assertRaises(ValueError):
            _oid("")


if __name__ == "__main__":
    unittest.main()
