from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.models import AnonymousUser
from django.test import Client, RequestFactory

from sentry.auth.helper import OK_LINK_IDENTITY, AuthIdentityHandler, RedisBackedState
from sentry.auth.provider import Provider
from sentry.models import (
    AuditLogEntry,
    AuditLogEntryEvent,
    AuthIdentity,
    AuthProvider,
    InviteStatus,
    OrganizationMember,
)
from sentry.testutils import TestCase
from sentry.utils.compat import mock


class AuthIdentityHandlerTest(TestCase):
    def setUp(self):
        self.provider = "dummy"
        self.request = RequestFactory().post("/auth/sso/")
        self.request.user = AnonymousUser()
        self.request.session = Client().session

        self.auth_provider = AuthProvider.objects.create(
            organization=self.organization, provider=self.provider
        )
        self.identity = {
            "id": "1234",
            "email": "test@example.com",
            "name": "Morty",
            "data": {"foo": "bar"},
        }

        self.handler = AuthIdentityHandler(
            self.auth_provider, Provider(self.provider), self.organization, self.request
        )

        self.state = RedisBackedState(self.request)

    def set_up_user(self):
        """Set up a persistent user and associate it to the request.

        If not called, default to having the request come from an
        anonymous user.
        """

        user = self.create_user()
        self.request.user = user
        return user

    def set_up_user_identity(self):
        """Set up a persistent user who already has an auth identity."""
        user = self.set_up_user()
        auth_identity = AuthIdentity.objects.create(
            user=user, auth_provider=self.auth_provider, ident="test_ident"
        )
        return user, auth_identity


class HandleNewUserTest(AuthIdentityHandlerTest):
    @mock.patch("sentry.analytics.record")
    def test_simple(self, mock_record):

        auth_identity = self.handler.handle_new_user(self.identity)
        user = auth_identity.user

        assert user.email == self.identity["email"]
        assert OrganizationMember.objects.filter(organization=self.organization, user=user).exists()

        signup_record = [r for r in mock_record.call_args_list if r[0][0] == "user.signup"]
        assert signup_record == [
            mock.call(
                "user.signup",
                user_id=user.id,
                source="sso",
                provider=self.provider,
                referrer="in-app",
            )
        ]

    def test_associated_existing_member_invite_by_email(self):
        member = OrganizationMember.objects.create(
            organization=self.organization, email=self.identity["email"]
        )

        auth_identity = self.handler.handle_new_user(self.identity)

        assigned_member = OrganizationMember.objects.get(
            organization=self.organization, user=auth_identity.user
        )

        assert assigned_member.id == member.id

    def test_associated_existing_member_invite_request(self):
        member = self.create_member(
            organization=self.organization,
            email=self.identity["email"],
            invite_status=InviteStatus.REQUESTED_TO_BE_INVITED.value,
        )

        auth_identity = self.handler.handle_new_user(self.identity)

        assert OrganizationMember.objects.filter(
            organization=self.organization,
            user=auth_identity.user,
            invite_status=InviteStatus.APPROVED.value,
        ).exists()

        assert not OrganizationMember.objects.filter(id=member.id).exists()

    def test_associate_pending_invite(self):
        # The org member invite should have a non matching email, but the
        # member id and token will match from the cookie, allowing association
        member = OrganizationMember.objects.create(
            organization=self.organization, email="different.email@example.com", token="abc"
        )

        self.request.COOKIES["pending-invite"] = urlencode(
            {"memberId": member.id, "token": member.token, "url": ""}
        )

        auth_identity = self.handler.handle_new_user(self.identity)

        assigned_member = OrganizationMember.objects.get(
            organization=self.organization, user=auth_identity.user
        )

        assert assigned_member.id == member.id


class HandleExistingIdentityTest(AuthIdentityHandlerTest):
    @mock.patch("sentry.auth.helper.auth")
    def test_simple(self, mock_auth):
        mock_auth.get_login_redirect.return_value = "test_login_url"
        user, auth_identity = self.set_up_user_identity()

        redirect = self.handler.handle_existing_identity(self.state, auth_identity, self.identity)

        assert redirect.url == mock_auth.get_login_redirect.return_value
        assert mock_auth.get_login_redirect.called_with(self.request)

        persisted_identity = AuthIdentity.objects.get(ident=auth_identity.ident)
        assert persisted_identity.data == self.identity["data"]

        persisted_om = OrganizationMember.objects.get(user=user, organization=self.organization)
        assert getattr(persisted_om.flags, "sso:linked")
        assert not getattr(persisted_om.flags, "sso:invalid")

        login_request, login_user = mock_auth.login.call_args.args
        assert login_request == self.request
        assert login_user == user


class HandleAttachIdentityTest(AuthIdentityHandlerTest):
    @mock.patch("sentry.auth.helper.messages")
    def test_new_identity(self, mock_messages):
        self.set_up_user()

        auth_identity = self.handler.handle_attach_identity(self.identity)
        assert auth_identity.ident == self.identity["id"]
        assert auth_identity.data == self.identity["data"]

        assert AuditLogEntry.objects.filter(
            organization=self.organization,
            target_object=auth_identity.id,
            event=AuditLogEntryEvent.SSO_IDENTITY_LINK,
            data=auth_identity.get_audit_log_data(),
        ).exists()

        assert mock_messages.add_message.called_with(
            self.request, messages.SUCCESS, OK_LINK_IDENTITY
        )

    @mock.patch("sentry.auth.helper.messages")
    def test_existing_identity(self, mock_messages):
        user, existing_identity = self.set_up_user_identity()

        returned_identity = self.handler.handle_attach_identity(self.identity)
        assert returned_identity == existing_identity
        assert not mock_messages.add_message.called

    def test_wipe_other_identity(self):
        request_user, existing_identity = self.set_up_user_identity()
        other_profile = self.create_user()

        # The user logs in with credentials from this other identity
        AuthIdentity.objects.create(
            user=other_profile, auth_provider=self.auth_provider, ident=self.identity["id"]
        )
        OrganizationMember.objects.create(user=other_profile, organization=self.organization)

        returned_identity = self.handler.handle_attach_identity(self.identity)
        assert returned_identity.ident == self.identity["id"]
        assert returned_identity.data == self.identity["data"]

        assert not AuthIdentity.objects.filter(id=existing_identity.id).exists()

        persisted_om = OrganizationMember.objects.get(
            user=other_profile, organization=self.organization
        )
        assert not getattr(persisted_om.flags, "sso:linked")
        assert getattr(persisted_om.flags, "sso:invalid")


class HandleUnknownIdentityTest(AuthIdentityHandlerTest):
    def _test_simple(self, mock_render, expected_template):
        redirect = self.handler.handle_unknown_identity(self.state, self.identity)

        assert redirect is mock_render.return_value
        template, context, request = mock_render.call_args.args
        status = mock_render.call_args.kwargs["status"]

        assert template == expected_template
        assert request is self.request
        assert status == 200

        assert context["organization"] is self.organization
        assert context["identity"] == self.identity
        assert context["provider"] == self.auth_provider.get_provider().name
        assert context["identity_display_name"] == self.identity["name"]
        assert context["identity_identifier"] == self.identity["email"]
        return context

    @mock.patch("sentry.auth.helper.render_to_response")
    def test_unauthenticated(self, mock_render):
        context = self._test_simple(mock_render, "sentry/auth-confirm-identity.html")
        assert context["existing_user"] is None
        assert "login_form" in context

    @mock.patch("sentry.auth.helper.render_to_response")
    def test_authenticated(self, mock_render):
        self.set_up_user()
        context = self._test_simple(mock_render, "sentry/auth-confirm-link.html")
        assert context["existing_user"] is self.request.user
        assert "login_form" not in context
