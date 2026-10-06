from django.test import SimpleTestCase

from pool_service.phone_utils import format_phone, normalize_phone


class PhoneUtilsTests(SimpleTestCase):
    def test_russian_phone_formats_are_normalized_to_plus_seven(self):
        cases = {
            "8 (913) 000-00-01": ("79130000001", "+7 913 000 0001"),
            "+7 913 000 00 01": ("79130000001", "+7 913 000 0001"),
            "9130000001": ("79130000001", "+7 913 000 0001"),
            "79130000001": ("79130000001", "+7 913 000 0001"),
        }
        for raw, (normalized, formatted) in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone(raw), normalized)
                self.assertEqual(format_phone(raw), formatted)

    def test_unknown_short_phone_is_not_rewritten(self):
        self.assertEqual(normalize_phone("601"), "")
        self.assertEqual(format_phone("601"), "601")
