import unittest
from orchestration.credentials import password_hash
from integration.auth import verify_password


class PasswordTests(unittest.TestCase):
    def test_reject_nine_characters(self):
        with self.assertRaisesRegex(ValueError, '至少10位'):
            password_hash('a' * 9)

    def test_accept_ten_characters_and_verify(self):
        password = 'a' * 10
        hashed = password_hash(password)
        self.assertTrue(verify_password(password, hashed))
        self.assertFalse(verify_password('b' * 10, hashed))
