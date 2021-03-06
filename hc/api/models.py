# coding: utf-8

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta as td

from croniter import croniter
from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.urls import reverse
from django.utils import timezone
from hc.api import transports
from hc.lib import emails
import requests

STATUSES = (
    ("up", "Up"),
    ("down", "Down"),
    ("new", "New"),
    ("paused", "Paused")
)
DEFAULT_TIMEOUT = td(days=1)
DEFAULT_GRACE = td(hours=1)
CHECK_KINDS = (("simple", "Simple"),
               ("cron", "Cron"))

CHANNEL_KINDS = (("email", "Email"),
                 ("webhook", "Webhook"),
                 ("hipchat", "HipChat"),
                 ("slack", "Slack"),
                 ("pd", "PagerDuty"),
                 ("pagertree", "PagerTree"),
                 ("po", "Pushover"),
                 ("pushbullet", "Pushbullet"),
                 ("opsgenie", "OpsGenie"),
                 ("victorops", "VictorOps"),
                 ("discord", "Discord"),
                 ("telegram", "Telegram"),
                 ("sms", "SMS"),
                 ("zendesk", "Zendesk"),
                 ("trello", "Trello"))

PO_PRIORITIES = {
    -2: "lowest",
    -1: "low",
    0: "normal",
    1: "high",
    2: "emergency"
}


def isostring(dt):
    """Convert the datetime to ISO 8601 format with no microseconds. """
    return dt.replace(microsecond=0).isoformat()


class Check(models.Model):
    name = models.CharField(max_length=100, blank=True)
    tags = models.CharField(max_length=500, blank=True)
    code = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    desc = models.TextField(blank=True)
    user = models.ForeignKey(User, models.CASCADE, blank=True, null=True)
    created = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(max_length=10, default="simple",
                            choices=CHECK_KINDS)
    timeout = models.DurationField(default=DEFAULT_TIMEOUT)
    grace = models.DurationField(default=DEFAULT_GRACE)
    schedule = models.CharField(max_length=100, default="* * * * *")
    tz = models.CharField(max_length=36, default="UTC")
    n_pings = models.IntegerField(default=0)
    last_ping = models.DateTimeField(null=True, blank=True)
    last_ping_was_fail = models.NullBooleanField(default=False)
    has_confirmation_link = models.BooleanField(default=False)
    alert_after = models.DateTimeField(null=True, blank=True, editable=False)
    status = models.CharField(max_length=6, choices=STATUSES, default="new")

    def name_then_code(self):
        if self.name:
            return self.name

        return str(self.code)

    def url(self):
        return settings.PING_ENDPOINT + str(self.code)

    def details_url(self):
        return settings.SITE_ROOT + reverse("hc-details", args=[self.code])

    def email(self):
        return "%s@%s" % (self.code, settings.PING_EMAIL_DOMAIN)

    def send_alert(self):
        if self.status not in ("up", "down"):
            raise NotImplementedError("Unexpected status: %s" % self.status)

        errors = []
        for channel in self.channel_set.all():
            error = channel.notify(self)
            if error not in ("", "no-op"):
                errors.append((channel, error))

        return errors

    def get_grace_start(self):
        """ Return the datetime when grace period starts. """

        # The common case, grace starts after timeout
        if self.kind == "simple":
            return self.last_ping + self.timeout

        # The complex case, next ping is expected based on cron schedule
        with timezone.override(self.tz):
            last_naive = timezone.make_naive(self.last_ping)
            it = croniter(self.schedule, last_naive)
            next_naive = it.get_next(datetime)
            return timezone.make_aware(next_naive, is_dst=True)

    def get_status(self, now=None):
        """ Return "up" if the check is up or in grace, otherwise "down". """

        if self.status in ("new", "paused"):
            return self.status

        if self.last_ping_was_fail:
            return "down"

        if now is None:
            now = timezone.now()

        grace_start = self.get_grace_start()
        grace_end = grace_start + self.grace
        if now >= grace_end:
            return "down"

        if now >= grace_start:
            return "grace"

        return "up"

    def get_alert_after(self):
        """ Return the datetime when check potentially goes down. """

        # For "fail" pings, sendalerts should the check right
        # after receiving the ping, without waiting for the grace time:
        if self.last_ping_was_fail:
            return self.last_ping

        return self.get_grace_start() + self.grace

    def assign_all_channels(self):
        if self.user:
            channels = Channel.objects.filter(user=self.user)
            self.channel_set.add(*channels)

    def tags_list(self):
        return [t.strip() for t in self.tags.split(" ") if t.strip()]

    def matches_tag_set(self, tag_set):
        return tag_set.issubset(self.tags_list())

    def to_dict(self):
        update_rel_url = reverse("hc-api-update", args=[self.code])
        pause_rel_url = reverse("hc-api-pause", args=[self.code])
        channel_codes = [str(ch.code) for ch in self.channel_set.all()]

        result = {
            "name": self.name,
            "ping_url": self.url(),
            "update_url": settings.SITE_ROOT + update_rel_url,
            "pause_url": settings.SITE_ROOT + pause_rel_url,
            "tags": self.tags,
            "grace": int(self.grace.total_seconds()),
            "n_pings": self.n_pings,
            "status": self.get_status(),
            "channels": ",".join(sorted(channel_codes))
        }

        if self.kind == "simple":
            result["timeout"] = int(self.timeout.total_seconds())
        elif self.kind == "cron":
            result["schedule"] = self.schedule
            result["tz"] = self.tz

        if self.last_ping:
            result["last_ping"] = isostring(self.last_ping)
            result["next_ping"] = isostring(self.get_grace_start())
        else:
            result["last_ping"] = None
            result["next_ping"] = None

        return result

    def ping(self, remote_addr, scheme, method, ua, body, is_fail=False):
        self.n_pings = models.F("n_pings") + 1
        self.last_ping = timezone.now()
        self.last_ping_was_fail = is_fail
        self.has_confirmation_link = "confirm" in str(body).lower()
        self.alert_after = self.get_alert_after()
        if self.status in ("new", "paused"):
            self.status = "up"

        self.save()
        self.refresh_from_db()

        ping = Ping(owner=self)
        ping.n = self.n_pings
        ping.fail = is_fail
        ping.remote_addr = remote_addr
        ping.scheme = scheme
        ping.method = method
        # If User-Agent is longer than 200 characters, truncate it:
        ping.ua = ua[:200]
        ping.body = body[:10000]
        ping.save()


class Ping(models.Model):
    id = models.BigAutoField(primary_key=True)
    n = models.IntegerField(null=True)
    owner = models.ForeignKey(Check, models.CASCADE)
    created = models.DateTimeField(auto_now_add=True)
    fail = models.NullBooleanField(default=False)
    scheme = models.CharField(max_length=10, default="http")
    remote_addr = models.GenericIPAddressField(blank=True, null=True)
    method = models.CharField(max_length=10, blank=True)
    ua = models.CharField(max_length=200, blank=True)
    body = models.CharField(max_length=10000, blank=True, null=True)


class Channel(models.Model):
    code = models.UUIDField(default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, models.CASCADE)
    created = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(max_length=20, choices=CHANNEL_KINDS)
    value = models.TextField(blank=True)
    email_verified = models.BooleanField(default=False)
    checks = models.ManyToManyField(Check)

    def __str__(self):
        if self.kind == "email":
            return "Email to %s" % self.value
        elif self.kind == "sms":
            if self.sms_label:
                return "SMS to %s" % self.sms_label
            return "SMS to %s" % self.sms_number
        elif self.kind == "slack":
            return "Slack %s" % self.slack_channel
        elif self.kind == "telegram":
            return "Telegram %s" % self.telegram_name

        return self.get_kind_display()

    def assign_all_checks(self):
        checks = Check.objects.filter(user=self.user)
        self.checks.add(*checks)

    def make_token(self):
        seed = "%s%s" % (self.code, settings.SECRET_KEY)
        seed = seed.encode()
        return hashlib.sha1(seed).hexdigest()

    def send_verify_link(self):
        args = [self.code, self.make_token()]
        verify_link = reverse("hc-verify-email", args=args)
        verify_link = settings.SITE_ROOT + verify_link
        emails.verify_email(self.value, {"verify_link": verify_link})

    def get_unsub_link(self):
        args = [self.code, self.make_token()]
        verify_link = reverse("hc-unsubscribe-alerts", args=args)
        return settings.SITE_ROOT + verify_link

    @property
    def transport(self):
        if self.kind == "email":
            return transports.Email(self)
        elif self.kind == "webhook":
            return transports.Webhook(self)
        elif self.kind == "slack":
            return transports.Slack(self)
        elif self.kind == "hipchat":
            return transports.HipChat(self)
        elif self.kind == "pd":
            return transports.PagerDuty(self)
        elif self.kind == "pagertree":
            return transports.PagerTree(self)
        elif self.kind == "victorops":
            return transports.VictorOps(self)
        elif self.kind == "pushbullet":
            return transports.Pushbullet(self)
        elif self.kind == "po":
            return transports.Pushover(self)
        elif self.kind == "opsgenie":
            return transports.OpsGenie(self)
        elif self.kind == "discord":
            return transports.Discord(self)
        elif self.kind == "telegram":
            return transports.Telegram(self)
        elif self.kind == "sms":
            return transports.Sms(self)
        elif self.kind == "zendesk":
            return transports.Zendesk(self)
        elif self.kind == "trello":
            return transports.Trello(self)
        else:
            raise NotImplementedError("Unknown channel kind: %s" % self.kind)

    def notify(self, check):
        if self.transport.is_noop(check):
            return "no-op"

        n = Notification(owner=check, channel=self)
        n.check_status = check.status
        n.error = "Sending"
        n.save()

        if self.kind == "email":
            error = self.transport.notify(check, n.bounce_url()) or ""
        else:
            error = self.transport.notify(check) or ""

        n.error = error
        n.save()

        return error

    @property
    def po_value(self):
        assert self.kind == "po"
        user_key, prio = self.value.split("|")
        prio = int(prio)
        return user_key, prio, PO_PRIORITIES[prio]

    @property
    def url_down(self):
        assert self.kind == "webhook"
        if not self.value.startswith("{"):
            parts = self.value.split("\n")
            return parts[0]

        doc = json.loads(self.value)
        return doc.get("url_down")

    @property
    def url_up(self):
        assert self.kind == "webhook"
        if not self.value.startswith("{"):
            parts = self.value.split("\n")
            return parts[1] if len(parts) > 1 else ""

        doc = json.loads(self.value)
        return doc.get("url_up")

    @property
    def post_data(self):
        assert self.kind == "webhook"
        if not self.value.startswith("{"):
            parts = self.value.split("\n")
            return parts[2] if len(parts) > 2 else ""

        doc = json.loads(self.value)
        return doc.get("post_data")

    @property
    def headers(self):
        assert self.kind == "webhook"
        if not self.value.startswith("{"):
            return {}

        doc = json.loads(self.value)
        return doc.get("headers", {})

    @property
    def slack_team(self):
        assert self.kind == "slack"
        if not self.value.startswith("{"):
            return None

        doc = json.loads(self.value)
        return doc["team_name"]

    @property
    def slack_channel(self):
        assert self.kind == "slack"
        if not self.value.startswith("{"):
            return None

        doc = json.loads(self.value)
        return doc["incoming_webhook"]["channel"]

    @property
    def slack_webhook_url(self):
        assert self.kind == "slack"
        if not self.value.startswith("{"):
            return self.value

        doc = json.loads(self.value)
        return doc["incoming_webhook"]["url"]

    @property
    def discord_webhook_url(self):
        assert self.kind == "discord"
        doc = json.loads(self.value)
        return doc["webhook"]["url"]

    @property
    def discord_webhook_id(self):
        assert self.kind == "discord"
        doc = json.loads(self.value)
        return doc["webhook"]["id"]

    @property
    def telegram_id(self):
        assert self.kind == "telegram"
        doc = json.loads(self.value)
        return doc.get("id")

    @property
    def telegram_type(self):
        assert self.kind == "telegram"
        doc = json.loads(self.value)
        return doc.get("type")

    @property
    def telegram_name(self):
        assert self.kind == "telegram"
        doc = json.loads(self.value)
        return doc.get("name")

    def refresh_hipchat_access_token(self):
        assert self.kind == "hipchat"
        if not self.value.startswith("{"):
            return  # Don't have OAuth credentials

        doc = json.loads(self.value)
        if time.time() < doc.get("expires_at", 0):
            return  # Current access token is still valid

        url = "https://api.hipchat.com/v2/oauth/token"
        auth = (doc["oauthId"], doc["oauthSecret"])
        r = requests.post(url, auth=auth, data={
            "grant_type": "client_credentials",
            "scope": "send_notification"
        })

        doc.update(r.json())
        doc["expires_at"] = int(time.time()) + doc["expires_in"] - 300
        self.value = json.dumps(doc)
        self.save()

    @property
    def hipchat_webhook_url(self):
        assert self.kind == "hipchat"
        if not self.value.startswith("{"):
            return self.value

        doc = json.loads(self.value)
        tmpl = "https://api.hipchat.com/v2/room/%s/notification?auth_token=%s"
        return tmpl % (doc["roomId"], doc.get("access_token"))

    @property
    def pd_service_key(self):
        assert self.kind == "pd"
        if not self.value.startswith("{"):
            return self.value

        doc = json.loads(self.value)
        return doc["service_key"]

    @property
    def pd_account(self):
        assert self.kind == "pd"
        if self.value.startswith("{"):
            doc = json.loads(self.value)
            return doc["account"]

    @property
    def zendesk_token(self):
        assert self.kind == "zendesk"
        doc = json.loads(self.value)
        return doc["access_token"]

    @property
    def zendesk_subdomain(self):
        assert self.kind == "zendesk"
        doc = json.loads(self.value)
        return doc["subdomain"]

    def latest_notification(self):
        return Notification.objects.filter(channel=self).latest()

    @property
    def sms_number(self):
        assert self.kind == "sms"
        if self.value.startswith("{"):
            doc = json.loads(self.value)
            return doc["value"]
        return self.value

    @property
    def sms_label(self):
        assert self.kind == "sms"
        if self.value.startswith("{"):
            doc = json.loads(self.value)
            return doc["label"]

    @property
    def trello_token(self):
        assert self.kind == "trello"
        if self.value.startswith("{"):
            doc = json.loads(self.value)
            return doc["token"]

    @property
    def trello_board_list(self):
        assert self.kind == "trello"
        if self.value.startswith("{"):
            doc = json.loads(self.value)
            return doc["board_name"], doc["list_name"]

    @property
    def trello_list_id(self):
        assert self.kind == "trello"
        if self.value.startswith("{"):
            doc = json.loads(self.value)
            return doc["list_id"]


class Notification(models.Model):
    class Meta:
        get_latest_by = "created"

    code = models.UUIDField(default=uuid.uuid4, null=True, editable=False)
    owner = models.ForeignKey(Check, models.CASCADE)
    check_status = models.CharField(max_length=6)
    channel = models.ForeignKey(Channel, models.CASCADE)
    created = models.DateTimeField(auto_now_add=True)
    error = models.CharField(max_length=200, blank=True)

    def bounce_url(self):
        return settings.SITE_ROOT + reverse("hc-api-bounce", args=[self.code])
