import json
from unittest.mock import patch
from django.test import SimpleTestCase
from pool_service import avito_profile_source as source
from pool_service.communication_avito import AvitoError
from scripts.avito_readonly_receipt import configured_feed_source

class ProfileSourceTests(SimpleTestCase):
    def test_current_config_uses_feed_url_and_keeps_private_data_in_process(self):
        value, fingerprint = source.profile_data({"autoload_enabled": True,
            "feeds_data": [{"feed_name": "PRIVATE_NAME", "feed_url": "https://shop.aqualine22.ru/avito/avitofeed.xml"}],
            "report_email": "PRIVATE_EMAIL"})
        self.assertEqual("shop_aggregate_file", value["shop_feed_source"])
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertNotIn("https://", json.dumps(value))
        self.assertEqual(value, configured_feed_source(value))
        self.assertEqual(64, len(fingerprint))
    def test_signed_unknown_mixed_and_disabled_sources_are_not_approved(self):
        for feeds in [
            [{"feed_url": "https://shop.aqualine22.ru/avito/avitofeed.xml?token=PRIVATE"}],
            [{"feed_url": "https://other.invalid/PRIVATE"}],
            [{"feed_url": "https://shop.aqualine22.ru/avito/avitofeed.xml"},
             {"feed_url": "https://other.invalid/feed"}],
        ]:
            value, _ = source.profile_data({"autoload_enabled": True, "feeds_data": feeds})
            self.assertEqual("other_or_unknown", value["shop_feed_source"])
            self.assertNotIn("PRIVATE", json.dumps(value))
        value, _ = source.profile_data({"autoload_enabled": False, "feeds_data": None})
        self.assertFalse(value["autoload_enabled"])
        self.assertEqual("other_or_unknown", value["shop_feed_source"])
    def test_fixed_get_only_and_malformed_data_fails_closed(self):
        with patch.object(source.avito_workspace, "enforce_rate"), patch.object(source.avito_workspace, "_get",
                return_value={"autoload_enabled": True, "feeds_data": []}) as read:
            source.fetch_profile_source("synthetic-token", "123")
        read.assert_called_once_with("synthetic-token", "/autoload/v2/profile")
        for raw in ({}, {"autoload_enabled": 1, "feeds_data": []},
                    {"autoload_enabled": True, "feeds_data": "PRIVATE"},
                    {"autoload_enabled": True, "feeds_data": [{"feed_url": 123}]}):
            with self.assertRaises(AvitoError):
                source.profile_data(raw)
        with self.assertRaises(ValueError):
            configured_feed_source({"complete": True, "autoload_enabled": "PRIVATE",
                                    "shop_feed_source": "shop_aggregate_file", "shop_feed_diagnostics": []})
