import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from webauthn.helpers import bytes_to_base64url

from pool_service.models import Organization, OrganizationAccess, Profile, WebAuthnCredential
from pool_service.security import (
    SESSION_FRESH_PASSWORD_LOGIN_AT_KEY,
    SESSION_LAST_ACTIVITY_KEY,
    SESSION_LOCKED_KEY,
    set_security_pin,
    timestamp_now,
)
from pool_service.services.finance import can_configure_automatic_lock
from pool_service.webauthn_utils import SESSION_WEBAUTHN_AUTHENTICATION_CHALLENGE
from pool_service.webauthn_utils import credential_id_hash


@override_settings(SECURITY_IDLE_TIMEOUT_SECONDS=300)
class SessionSecurityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="security-user",
            password="strong-password",
            first_name="Иван",
            last_name="Петров",
        )
        self.profile, _ = Profile.objects.get_or_create(user=self.user)

    def grant_automatic_lock_control(self, role="accountant"):
        organization = Organization.objects.create(
            name=f"Security organization {role}",
            city="Барнаул",
            trial_started_at=timezone.now(),
        )
        OrganizationAccess.objects.create(
            user=self.user,
            organization=organization,
            role=role,
        )

    def test_pin_can_be_set_from_profile(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("security_pin_set"),
            {
                "current_password": "strong-password",
                "pin": "1234",
                "pin_confirm": "1234",
            },
        )

        self.assertRedirects(response, reverse("profile"))
        self.profile.refresh_from_db()
        self.assertTrue(check_password("1234", self.profile.security_pin_hash))
        self.assertFalse(self.client.session.get(SESSION_LOCKED_KEY))

    def test_idle_user_with_pin_is_redirected_to_unlock(self):
        set_security_pin(self.profile, "1234")
        self.client.force_login(self.user)
        session = self.client.session
        session[SESSION_LAST_ACTIVITY_KEY] = timestamp_now() - 301
        session[SESSION_LOCKED_KEY] = False
        session.save()

        response = self.client.get(reverse("profile"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("security_unlock"), response["Location"])
        self.assertIn("next=/profile/", response["Location"])

    def test_eligible_user_can_change_automatic_lock_preference(self):
        self.grant_automatic_lock_control()
        self.client.force_login(self.user)

        response = self.client.get(reverse("profile"))

        self.assertContains(response, "Отключить автоматическую блокировку")
        response = self.client.post(
            reverse("profile"),
            {
                "automatic_lock_settings": "1",
                "automatic_lock_disabled": "1",
            },
        )
        self.assertRedirects(response, reverse("profile"))
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.automatic_lock_disabled)

        response = self.client.post(reverse("profile"), {"automatic_lock_settings": "1"})
        self.assertRedirects(response, reverse("profile"))
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.automatic_lock_disabled)

    def test_only_management_finance_roles_can_configure_automatic_lock(self):
        for role in ("owner", "admin", "accountant"):
            self.grant_automatic_lock_control(role=role)
        self.assertTrue(can_configure_automatic_lock(self.user))

        manager = User.objects.create_user(username="security-manager", password="pass")
        organization = Organization.objects.create(name="Security manager organization")
        OrganizationAccess.objects.create(user=manager, organization=organization, role="manager")
        self.assertFalse(can_configure_automatic_lock(manager))

        superuser = User.objects.create_superuser(username="security-superuser", password="pass")
        self.assertTrue(can_configure_automatic_lock(superuser))

    def test_ineligible_user_cannot_view_or_change_automatic_lock_preference(self):
        self.grant_automatic_lock_control(role="manager")
        self.client.force_login(self.user)

        response = self.client.get(reverse("profile"))

        self.assertNotContains(response, "Отключить автоматическую блокировку")
        response = self.client.post(
            reverse("profile"),
            {
                "automatic_lock_settings": "1",
                "automatic_lock_disabled": "1",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.automatic_lock_disabled)

    def test_automatic_lock_preference_skips_idle_middleware_and_client_timer(self):
        self.grant_automatic_lock_control()
        self.profile.automatic_lock_disabled = True
        self.profile.save(update_fields=["automatic_lock_disabled"])
        set_security_pin(self.profile, "1234")
        self.client.force_login(self.user)
        session = self.client.session
        session[SESSION_LAST_ACTIVITY_KEY] = timestamp_now() - 301
        session[SESSION_LOCKED_KEY] = False
        session.save()

        response = self.client.get(reverse("profile"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.session.get(SESSION_LOCKED_KEY))
        self.assertNotContains(response, "const timeoutMs =")

        another_session = Client()
        another_session.force_login(self.user)
        session = another_session.session
        session[SESSION_LAST_ACTIVITY_KEY] = timestamp_now() - 301
        session[SESSION_LOCKED_KEY] = False
        session.save()

        response = another_session.get(reverse("profile"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(another_session.session.get(SESSION_LOCKED_KEY))

    def test_eligible_user_still_auto_locks_when_preference_is_disabled(self):
        self.grant_automatic_lock_control(role="accountant")
        self.profile.automatic_lock_disabled = False
        self.profile.save(update_fields=["automatic_lock_disabled"])
        set_security_pin(self.profile, "1234")
        self.client.force_login(self.user)
        session = self.client.session
        session[SESSION_LAST_ACTIVITY_KEY] = timestamp_now() - 301
        session[SESSION_LOCKED_KEY] = False
        session.save()

        response = self.client.get(reverse("profile"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("security_unlock"), response["Location"])
        self.assertTrue(self.client.session.get(SESSION_LOCKED_KEY))

    def test_automatic_lock_endpoint_skips_lock_for_eligible_disabled_user(self):
        self.grant_automatic_lock_control()
        self.profile.automatic_lock_disabled = True
        self.profile.save(update_fields=["automatic_lock_disabled"])
        set_security_pin(self.profile, "1234")
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("security_lock"),
            {"next": reverse("profile"), "automatic": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["locked"])
        self.assertTrue(response.json()["automatic_lock_disabled"])
        self.assertFalse(self.client.session.get(SESSION_LOCKED_KEY))

    def test_automatic_lock_endpoint_locks_session_by_default(self):
        set_security_pin(self.profile, "1234")
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("security_lock"),
            {"next": reverse("profile"), "automatic": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["locked"])
        self.assertTrue(self.client.session.get(SESSION_LOCKED_KEY))

    def test_correct_pin_unlocks_session(self):
        set_security_pin(self.profile, "1234")
        self.client.force_login(self.user)
        session = self.client.session
        session[SESSION_LOCKED_KEY] = True
        session["security_next"] = reverse("profile")
        session.save()

        response = self.client.post(
            reverse("security_unlock"),
            {"pin": "1234", "next": reverse("profile")},
        )

        self.assertRedirects(response, reverse("profile"))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.security_pin_failed_attempts, 0)
        self.assertFalse(self.client.session.get(SESSION_LOCKED_KEY))

    def test_ajax_correct_pin_unlocks_session(self):
        set_security_pin(self.profile, "1234")
        self.client.force_login(self.user)
        session = self.client.session
        session[SESSION_LOCKED_KEY] = True
        session["security_next"] = reverse("profile")
        session.save()

        response = self.client.post(
            reverse("security_unlock"),
            {"pin": "1234", "next": reverse("profile")},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redirect_url"], reverse("profile"))
        self.assertFalse(self.client.session.get(SESSION_LOCKED_KEY))

    def test_ajax_wrong_pin_returns_error_without_redirect(self):
        set_security_pin(self.profile, "1234")
        self.client.force_login(self.user)
        session = self.client.session
        session[SESSION_LOCKED_KEY] = True
        session.save()

        response = self.client.post(
            reverse("security_unlock"),
            {"pin": "9999"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertIn("Неверный PIN", response.json()["error"])
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.security_pin_failed_attempts, 1)

    def test_quick_pin_setup_requires_fresh_password_login(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("security_pin_quick_setup"),
            {"pin": "1234", "pin_confirm": "1234"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 403)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.security_pin_hash, "")

    def test_quick_pin_setup_sets_pin_after_fresh_password_login(self):
        self.client.force_login(self.user)
        session = self.client.session
        session[SESSION_FRESH_PASSWORD_LOGIN_AT_KEY] = timestamp_now()
        session.save()

        response = self.client.post(
            reverse("security_pin_quick_setup"),
            {"pin": "1234", "pin_confirm": "1234"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.profile.refresh_from_db()
        self.assertTrue(check_password("1234", self.profile.security_pin_hash))

    def test_too_many_wrong_pins_disable_pin_and_logout(self):
        set_security_pin(self.profile, "1234")
        self.client.force_login(self.user)
        session = self.client.session
        session[SESSION_LOCKED_KEY] = True
        session.save()

        for _ in range(5):
            response = self.client.post(reverse("security_unlock"), {"pin": "9999"})

        self.assertRedirects(response, reverse("login"))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.security_pin_hash, "")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_user_without_pin_is_not_locked_by_idle_timeout(self):
        self.client.force_login(self.user)
        session = self.client.session
        session[SESSION_LAST_ACTIVITY_KEY] = timestamp_now() - 301
        session[SESSION_LOCKED_KEY] = False
        session.save()

        response = self.client.get(reverse("profile"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.session.get(SESSION_LOCKED_KEY))

    def test_manual_lock_endpoint_locks_session(self):
        self.grant_automatic_lock_control()
        self.profile.automatic_lock_disabled = True
        self.profile.save(update_fields=["automatic_lock_disabled"])
        set_security_pin(self.profile, "1234")
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("security_lock"),
            {"next": reverse("profile")},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["locked"])
        self.assertTrue(self.client.session.get(SESSION_LOCKED_KEY))

    def test_unlock_page_only_starts_passkey_after_explicit_click(self):
        WebAuthnCredential.objects.create(
            user=self.user,
            credential_id=bytes_to_base64url(b"unlock-credential-id"),
            credential_id_hash=credential_id_hash(bytes_to_base64url(b"unlock-credential-id")),
            public_key=b"public-key",
        )
        self.client.force_login(self.user)
        session = self.client.session
        session[SESSION_LOCKED_KEY] = True
        session.save()

        response = self.client.get(reverse("security_unlock"))

        self.assertContains(response, 'button.addEventListener("click", runPasskeyUnlock);')
        self.assertNotContains(response, "runPasskeyUnlock(true)")
        self.assertNotContains(response, "window.setTimeout(() => runPasskeyUnlock")

    def test_locked_session_cannot_change_pin_without_unlock(self):
        set_security_pin(self.profile, "1234")
        self.client.force_login(self.user)
        session = self.client.session
        session[SESSION_LOCKED_KEY] = True
        session.save()

        response = self.client.post(
            reverse("security_pin_set"),
            {
                "current_password": "strong-password",
                "pin": "5678",
                "pin_confirm": "5678",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("security_unlock"), response["Location"])
        self.profile.refresh_from_db()
        self.assertTrue(check_password("1234", self.profile.security_pin_hash))

    def test_webauthn_registration_options_require_password(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("webauthn_registration_options"),
            json.dumps({"current_password": "wrong"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)

    def test_webauthn_registration_options_create_challenge(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("webauthn_registration_options"),
            json.dumps({"current_password": "strong-password", "name": "iPhone"}),
            content_type="application/json",
            HTTP_HOST="localhost",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertIn("challenge", data["publicKey"])
        self.assertIn("webauthn_registration_challenge", self.client.session)
        self.assertEqual(self.client.session["webauthn_pending_name"], "iPhone")

    def test_webauthn_registration_options_allow_fresh_login_without_password(self):
        self.client.force_login(self.user)
        session = self.client.session
        session[SESSION_FRESH_PASSWORD_LOGIN_AT_KEY] = timestamp_now()
        session.save()

        response = self.client.post(
            reverse("webauthn_registration_options"),
            json.dumps({"name": "iPhone"}),
            content_type="application/json",
            HTTP_HOST="localhost",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_webauthn_registration_options_reject_stale_login_without_password(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("webauthn_registration_options"),
            json.dumps({"name": "iPhone"}),
            content_type="application/json",
            HTTP_HOST="localhost",
        )

        self.assertEqual(response.status_code, 403)

    def test_user_with_passkey_and_no_pin_is_locked_by_idle_timeout(self):
        WebAuthnCredential.objects.create(
            user=self.user,
            credential_id=bytes_to_base64url(b"credential-id"),
            credential_id_hash=credential_id_hash(bytes_to_base64url(b"credential-id")),
            public_key=b"public-key",
        )
        self.client.force_login(self.user)
        session = self.client.session
        session[SESSION_LAST_ACTIVITY_KEY] = timestamp_now() - 301
        session[SESSION_LOCKED_KEY] = False
        session.save()

        response = self.client.get(reverse("profile"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("security_unlock"), response["Location"])

    def test_webauthn_authentication_verify_unlocks_session(self):
        credential = WebAuthnCredential.objects.create(
            user=self.user,
            credential_id=bytes_to_base64url(b"credential-id"),
            credential_id_hash=credential_id_hash(bytes_to_base64url(b"credential-id")),
            public_key=b"public-key",
            sign_count=1,
        )
        self.client.force_login(self.user)
        session = self.client.session
        session[SESSION_LOCKED_KEY] = True
        session[SESSION_WEBAUTHN_AUTHENTICATION_CHALLENGE] = bytes_to_base64url(b"challenge")
        session.save()

        with patch(
            "pool_service.security_views.verify_authentication_response",
            return_value=SimpleNamespace(new_sign_count=2),
        ):
            response = self.client.post(
                reverse("webauthn_authentication_verify"),
                json.dumps({"id": credential.credential_id, "next": reverse("profile")}),
                content_type="application/json",
                HTTP_HOST="localhost",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertFalse(self.client.session.get(SESSION_LOCKED_KEY))
        credential.refresh_from_db()
        self.assertEqual(credential.sign_count, 2)
