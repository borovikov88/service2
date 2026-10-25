from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.utils import timezone

from pool_service import avito_promotion_observations as observations
from pool_service.avito_feed_models import AvitoServicesFeed, AvitoServicesFeedVersion
from pool_service.communication_models import (
    AvitoCredential, ChannelConnection, CommunicationAccess, CommunicationChannel,
)
from pool_service.models import Organization, OrganizationAccess
from pool_service.tests.test_avito_services_feed import export_bytes
from pool_service.avito_services_feed import parse_export


class PromotionObservationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Promotion journal test")
        self.owner = User.objects.create_user("promotion-owner")
        OrganizationAccess.objects.create(organization=self.organization, user=self.owner, role="owner")
        self.channel = CommunicationChannel.objects.create(
            organization=self.organization, kind="avito", name="Services")
        self.connection = ChannelConnection.objects.create(
            pk=4, channel=self.channel, external_id="446312402", name="Services")
        self.credential = AvitoCredential.objects.create(connection=self.connection,
            client_id_encrypted="synthetic-client", client_secret_encrypted="synthetic-secret")
        self.goods = ChannelConnection.objects.create(
            pk=1, channel=self.channel, external_id="other-account", settings={"sentinel": "unchanged"})
        data = parse_export(export_bytes(("111",)))
        self.feed = AvitoServicesFeed.objects.create(connection=self.connection,
            account_id="446312402", organization_id_at_import=self.organization.pk,
            draft=deepcopy(data), published=deepcopy(data), enabled=True,
            revision=18, published_revision=18)
        self.publication = AvitoServicesFeedVersion.objects.create(feed=self.feed,
            action="publish", revision=18, published_revision=18, snapshot=data, file_digest="f" * 64)
        self.checked = timezone.now()
        self.data = {"item_id": "111", "autoload_item_id": data[0]["rows"][0]["Id"],
            "status": "active", "vas_available": True,
            "vas": [{"id": "xl", "finish_time": "2026-11-01T00:00:00+07:00",
                     "schedule": ["2026-10-30T00:00:00+07:00"]}]}
        self.binding = observations.capture_binding(self.connection)

    def observe(self, data=None, *, actor=None, checked=None, binding=None, connection=None, identifier="111"):
        return observations.observe(connection or self.connection, actor or self.owner,
            self.data if data is None else data, self.checked if checked is None else checked,
            self.binding if binding is None else binding, expected_item_id=identifier)

    def event(self):
        return self.feed.versions.filter(action="verify").latest("pk")

    def test_baseline_is_an_observation_not_a_purchase_or_start_date(self):
        before = (deepcopy(self.feed.draft), deepcopy(self.feed.published),
                  self.feed.revision, self.feed.published_revision, self.feed.enabled)
        with patch("pool_service.avito_workspace._get") as get:
            result = self.observe()
        get.assert_not_called()
        self.assertEqual(result["state"], "recorded")
        event = self.event()
        self.assertEqual(event.source_version_id, self.publication.pk)
        self.assertEqual(len(event.changes), 1)
        change = event.changes[0]
        self.assertEqual(change["kind"], "provider_promotion_observation")
        self.assertEqual(change["field"], "AvitoPromotionState")
        self.assertTrue(change["baseline"])
        self.assertIsNone(change["before"])
        self.assertFalse(change["purchase_confirmed"])
        self.assertIsNone(change["promotion_started_at"])
        self.assertEqual(change["promotion_started_at_source"], "unknown")
        self.assertEqual(change["time_source"], "service2_observed_at")
        self.assertEqual(change["after"]["services"][0]["finish_time"], "2026-10-31T17:00:00+00:00")
        self.assertEqual(event.observations, [])
        self.feed.refresh_from_db()
        self.assertEqual((self.feed.draft, self.feed.published, self.feed.revision,
                          self.feed.published_revision, self.feed.enabled), before)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {"sentinel": "unchanged"})

    def test_identical_state_is_deduplicated_and_changed_state_has_before_after(self):
        first = self.observe()
        repeated = self.observe(checked=self.checked + timedelta(seconds=1))
        self.assertEqual(repeated["state"], "unchanged")
        self.assertEqual(first["version_id"], repeated["version_id"])
        self.assertEqual(self.feed.versions.filter(action="verify").count(), 1)
        empty = {**self.data, "vas": []}
        changed = self.observe(empty, checked=self.checked + timedelta(seconds=2))
        self.assertEqual(changed["state"], "recorded")
        change = self.event().changes[0]
        self.assertFalse(change["baseline"])
        self.assertTrue(change["before"]["services"])
        self.assertEqual(change["after"], {"services": []})
        self.assertFalse(change["purchase_confirmed"])

    def test_unknown_dates_are_not_invented_and_missing_vas_is_not_empty(self):
        unavailable = dict(self.data)
        unavailable.pop("vas_available")
        unavailable["vas"] = []
        self.assertIsNone(self.observe(unavailable))
        self.assertEqual(self.feed.versions.filter(action="verify").count(), 0)
        data = {**self.data, "vas": [{"id": "xl", "finish_time": "unknown",
                                   "schedule": ["not a date", "2026-11-01"]}]}
        self.observe(data)
        service = self.event().changes[0]["after"]["services"][0]
        self.assertIsNone(service["finish_time"])
        self.assertFalse(service["finish_time_known"])
        self.assertEqual(service["schedule"], [])
        self.assertFalse(service["schedule_complete"])
        self.assertIsNone(self.event().changes[0]["promotion_started_at"])

    def test_order_and_duplicate_rows_do_not_imply_another_purchase(self):
        extra = {"id": "highlight", "finish_time": "", "schedule": []}
        self.observe({**self.data, "vas": self.data["vas"] + [extra]})
        result = self.observe({**self.data, "vas": [extra] + self.data["vas"] + [extra]},
                              checked=self.checked + timedelta(seconds=1))
        self.assertEqual(result["state"], "unchanged")
        self.assertEqual(self.feed.versions.filter(action="verify").count(), 1)

    def test_stale_response_cannot_replace_a_later_observation(self):
        self.observe(checked=self.checked + timedelta(seconds=2))
        with self.assertRaisesMessage(observations.PromotionObservationError, "promotion_observation_stale"):
            self.observe({**self.data, "vas": []}, checked=self.checked)
        self.assertEqual(self.feed.versions.filter(action="verify").count(), 1)

    def test_changed_credential_or_channel_binding_blocks_the_observation(self):
        self.credential.client_secret_encrypted = "rotated-synthetic-secret"
        self.credential.save(update_fields=["client_secret_encrypted"])
        with self.assertRaisesMessage(observations.PromotionObservationError, "promotion_observation_binding_changed"):
            self.observe()
        current = observations.capture_binding(self.connection)
        channel = CommunicationChannel.objects.create(
            organization=self.organization, kind="avito", name="Changed services")
        ChannelConnection.objects.filter(pk=4).update(channel=channel)
        with self.assertRaisesMessage(observations.PromotionObservationError, "promotion_observation_binding_changed"):
            self.observe(binding=current)
        self.assertEqual(self.feed.versions.filter(action="verify").count(), 0)

    def test_foreign_account_organization_row_or_requested_id_cannot_create_history(self):
        for kind in ("requested", "foreign_row", "internal_id", "provider_status", "organization"):
            data, identifier = deepcopy(self.data), "111"
            if kind == "requested":
                data["item_id"] = "222"
            elif kind == "foreign_row":
                data["item_id"] = identifier = "222"
            elif kind == "internal_id":
                data["autoload_item_id"] = "foreign"
            elif kind == "provider_status":
                data["status"] = "another_user"
            else:
                self.feed.organization_id_at_import = self.organization.pk + 1000
                self.feed.save(update_fields=["organization_id_at_import"])
            with self.subTest(kind=kind), self.assertRaises(observations.PromotionObservationError):
                self.observe(data, identifier=identifier)
            self.feed.organization_id_at_import = self.organization.pk
            self.feed.save(update_fields=["organization_id_at_import"])
        with self.assertRaises(observations.PromotionObservationError):
            self.observe(connection=self.goods)
        ChannelConnection.objects.filter(pk=4).update(external_id="other-account")
        with self.assertRaises(observations.PromotionObservationError):
            self.observe()
        self.assertEqual(self.feed.versions.filter(action="verify").count(), 0)

    def test_inactive_foreign_or_read_only_actor_cannot_create_history(self):
        foreign = User.objects.create_user("foreign-promotion-user")
        for actor in (foreign, self.owner):
            if actor == self.owner:
                User.objects.filter(pk=actor.pk).update(is_active=False)
            with self.subTest(actor=actor.pk), self.assertRaises(PermissionDenied):
                self.observe(actor=actor)
        User.objects.filter(pk=self.owner.pk).update(is_active=True)
        CommunicationAccess.objects.filter(user=self.owner, organization=self.organization).update(can_manage_channels=False)
        with self.assertRaises(PermissionDenied):
            self.observe()
        self.assertEqual(self.feed.versions.filter(action="verify").count(), 0)

    def test_other_typed_manual_evidence_does_not_become_a_promotion_baseline(self):
        AvitoServicesFeedVersion.objects.create(feed=self.feed, action="verify", revision=18,
            published_revision=18, snapshot=self.feed.draft,
            changes=[{"kind": "manual_content_evidence", "avito_id": "111", "field": "photo:10",
                      "after": {"checked": True}}])
        self.assertTrue(self.observe()["baseline"])
        self.assertIsNone(self.event().changes[0]["before"])

    def test_invalid_data_and_time_fail_closed_without_provider_io(self):
        for data in ({**self.data, "status": {}}, {**self.data, "vas": [{"id": "SECRET URL", "schedule": []}]}):
            with self.subTest(data=data), self.assertRaises(observations.PromotionObservationError):
                self.observe(data)
        for checked in (self.checked.replace(tzinfo=None), self.checked - timedelta(minutes=11),
                        self.checked + timedelta(minutes=1)):
            with self.subTest(checked=checked), self.assertRaises(observations.PromotionObservationError):
                self.observe(checked=checked)
        self.assertEqual(self.feed.versions.filter(action="verify").count(), 0)


    def test_existing_manual_refresh_appends_observation_without_extra_provider_reads(self):
        from django.urls import reverse
        self.connection.settings = {'avito_workspace': {'account_id': '446312402',
            'sections': {'items': {'data': {'rows': [{'id': '111'}]}}}}}
        self.connection.save(update_fields=['settings'])
        self.client.force_login(self.owner)
        with patch('pool_service.avito_management.access_token', return_value='opaque'), \
                patch('pool_service.avito_workspace._get', return_value={'id': 446312402}), \
                patch('pool_service.avito_workspace.fetch_item_detail', return_value=self.data) as fetch:
            response = self.client.post(reverse('avito_refresh_data', args=[4]),
                {'section': 'item_detail', 'item_id': '111'})
        self.assertEqual(response.status_code, 302)
        fetch.assert_called_once_with('opaque', '446312402', '111')
        self.assertEqual(self.feed.versions.filter(action='verify').count(), 1)
        self.connection.refresh_from_db()
        section = self.connection.settings['avito_workspace']['sections']['item_detail']
        self.assertEqual(section['promotion_journal_state'], 'recorded')
        self.assertEqual(section['status'], 'ok')
