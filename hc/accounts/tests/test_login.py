from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from hc.accounts.models import Profile
from django.conf import settings


class LoginTestCase(TestCase):

    def test_it_sends_link(self):
        alice = User(username="alice", email="alice@example.org")
        alice.save()

        form = {"identity": "alice@example.org"}

        r = self.client.post("/accounts/login/", form)
        assert r.status_code == 302

        # Alice should be the only existing user
        self.assertEqual(User.objects.count(), 1)

        # And email should have been sent
        self.assertEqual(len(mail.outbox), 1)
        subject = "Log in to %s" % settings.SITE_NAME
        self.assertEqual(mail.outbox[0].subject, subject)

    def test_it_pops_bad_link_from_session(self):
        self.client.session["bad_link"] = True
        self.client.get("/accounts/login/")
        assert "bad_link" not in self.client.session

    def test_it_ignores_case(self):
        alice = User(username="alice", email="alice@example.org")
        alice.save()

        form = {"identity": "ALICE@EXAMPLE.ORG"}

        r = self.client.post("/accounts/login/", form)
        assert r.status_code == 302

        # There should be exactly one user:
        self.assertEqual(User.objects.count(), 1)

        profile = Profile.objects.for_user(alice)
        self.assertIn("login", profile.token)

    def test_it_handles_password(self):
        alice = User(username="alice", email="alice@example.org")
        alice.set_password("password")
        alice.save()

        form = {
            "action": "login",
            "email": "alice@example.org",
            "password": "password"
        }

        r = self.client.post("/accounts/login/", form)
        self.assertEqual(r.status_code, 302)

    def test_it_handles_wrong_password(self):
        alice = User(username="alice", email="alice@example.org")
        alice.set_password("password")
        alice.save()

        form = {
            "action": "login",
            "email": "alice@example.org",
            "password": "wrong password"
        }

        r = self.client.post("/accounts/login/", form)
        self.assertContains(r, "Incorrect email or password")
