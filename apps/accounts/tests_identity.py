"""HMAC of the registration number and encryption of the card name."""
import hashlib
import hmac

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from apps.accounts import identity

KEY = "test-hmac-key-0123456789-0123456789-abc"


@override_settings(REG_NO_HMAC_KEY=KEY)
class RegNoTests(SimpleTestCase):
    def test_spaces_and_dashes_are_stripped(self):
        for raw in ["12345678", " 1234 5678 ", "1234-5678", "12-34 56\t78\n"]:
            with self.subTest(raw=raw):
                self.assertEqual(identity.normalize_reg_no(raw), "12345678")

    def test_invalid_numbers_are_rejected(self):
        bad = [
            "", "1234567", "123456789", "1234567a", "abcdefgh", "1234_5678",
            "١٢٣٤٥٦٧٨",  # Arabic-Indic digits: \d without re.ASCII would accept these
            "１２３４５６７８",  # full-width digits
            "12345678\n9",
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(identity.InvalidRegNo):
                identity.normalize_reg_no(raw)

    def test_non_strings_are_rejected_not_coerced(self):
        for raw in [12345678, None, b"12345678"]:
            with self.subTest(raw=raw), self.assertRaises(identity.InvalidRegNo):
                identity.normalize_reg_no(raw)

    def test_leading_zeros_are_preserved(self):
        self.assertEqual(identity.normalize_reg_no("00123456"), "00123456")
        self.assertNotEqual(identity.hash_reg_no("00123456"), identity.hash_reg_no("00123457"))

    @override_settings(REG_NO_PATTERN=r"^\d{5}$")
    def test_pattern_comes_from_settings(self):
        self.assertEqual(identity.normalize_reg_no("12-345"), "12345")
        with self.assertRaises(identity.InvalidRegNo):
            identity.normalize_reg_no("12345678")

    def test_hmac_is_hmac_sha256_of_the_normalized_number(self):
        digest, last4 = identity.hash_reg_no("1234-5678")
        self.assertEqual(digest, hmac.new(KEY.encode(), b"12345678", hashlib.sha256).hexdigest())
        self.assertEqual(len(digest), 64)
        self.assertEqual(last4, "5678")

    def test_formatting_variants_hash_identically(self):
        self.assertEqual(identity.hash_reg_no("1234 5678"), identity.hash_reg_no("12345678"))

    def test_digest_is_keyed_and_reveals_no_digits(self):
        digest, _ = identity.hash_reg_no("12345678")
        self.assertNotEqual(digest, hashlib.sha256(b"12345678").hexdigest())
        with override_settings(REG_NO_HMAC_KEY="another-key-0123456789-0123456789-xyz"):
            self.assertNotEqual(identity.hash_reg_no("12345678")[0], digest)
        self.assertNotIn("12345678", digest)

    @override_settings(REG_NO_HMAC_KEY="")
    def test_missing_key_fails_loudly_instead_of_hashing_with_an_empty_key(self):
        with self.assertRaises(ImproperlyConfigured):
            identity.hash_reg_no("12345678")


class CardNameTests(SimpleTestCase):
    def setUp(self):
        self.old, self.new = Fernet.generate_key().decode(), Fernet.generate_key().decode()

    def test_round_trip_and_ciphertext_hides_the_name(self):
        with override_settings(CARD_NAME_KEYS=[self.old]):
            token = identity.encrypt_card_name("Asha  Verma ")
            self.assertNotIn("Asha", token)
            self.assertEqual(identity.decrypt_card_name(token), "Asha Verma")

    def test_rotation_new_key_first_still_reads_old_ciphertext(self):
        with override_settings(CARD_NAME_KEYS=[self.old]):
            token = identity.encrypt_card_name("Asha Verma")
        with override_settings(CARD_NAME_KEYS=[self.new, self.old]):
            self.assertEqual(identity.decrypt_card_name(token), "Asha Verma")
            fresh = identity.encrypt_card_name("Ravi Kumar")
        # New writes use the first key, so the old key alone can no longer read them...
        with override_settings(CARD_NAME_KEYS=[self.old]), self.assertRaises(
            identity.CardNameUnreadable
        ):
            identity.decrypt_card_name(fresh)
        # ...and once the old key is dropped, old ciphertext is unreadable (that is the point
        # of re-encrypting before dropping a key).
        with override_settings(CARD_NAME_KEYS=[self.new]), self.assertRaises(
            identity.CardNameUnreadable
        ):
            identity.decrypt_card_name(token)

    def test_blank_and_overlong_names_are_rejected(self):
        with override_settings(CARD_NAME_KEYS=[self.old]):
            for raw in ["", "   ", None, "x" * (identity.MAX_CARD_NAME_LENGTH + 1)]:
                with self.subTest(raw=raw), self.assertRaises(identity.InvalidCardName):
                    identity.encrypt_card_name(raw)

    @override_settings(CARD_NAME_KEYS=[])
    def test_missing_keys_fail_loudly(self):
        with self.assertRaises(ImproperlyConfigured):
            identity.encrypt_card_name("Asha Verma")
