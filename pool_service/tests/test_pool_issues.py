import shutil
import tempfile
from datetime import timedelta
from io import BytesIO

from PIL import Image
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Client, CrmItem, CrmItemPhoto, Organization, OrganizationAccess, Pool


@override_settings(ALLOWED_HOSTS=["testserver"], VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY="")
class PoolIssueTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="rovik-issues-tests-")
        self.override = override_settings(MEDIA_ROOT=self.media_root)
        self.override.enable()

        self.organization = Organization.objects.create(
            name="Issue Org",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.admin = User.objects.create_user(username="issue-admin", password="pass")
        OrganizationAccess.objects.create(user=self.admin, organization=self.organization, role="admin")
        self.client_record = Client.objects.create(
            organization=self.organization,
            client_type="private",
            first_name="Test",
            last_name="Client",
            name="Test Client",
            phone="+79000000000",
        )
        self.pool = Pool.objects.create(
            client=self.client_record,
            address="Test pool",
            organization=self.organization,
        )

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def image_upload(self):
        buffer = BytesIO()
        Image.new("RGB", (40, 40), "white").save(buffer, format="JPEG")
        return SimpleUploadedFile("issue.jpg", buffer.getvalue(), content_type="image/jpeg")

    def test_pool_issue_create_saves_photo_and_row_links_to_card(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("pool_issue_create", kwargs={"pool_uuid": self.pool.uuid}),
            {
                "title": "Pump leak",
                "urgency": CrmItem.URGENCY_REQUIRED,
                "description": "Seal replacement needed",
                "photos": self.image_upload(),
            },
        )

        self.assertRedirects(response, reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}))
        issue = CrmItem.objects.get(title="Pump leak")
        self.assertEqual(CrmItemPhoto.objects.filter(item=issue).count(), 1)

        detail_url = reverse(
            "crm_view",
            kwargs={"direction": CrmItem.DIRECTION_SERVICE, "item_id": issue.id},
        )
        detail_response = self.client.get(reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, f'data-row-href="{detail_url}"', html=False)
        self.assertContains(detail_response, "issue-photo-thumb")
        self.assertContains(detail_response, "crm_issues/")
