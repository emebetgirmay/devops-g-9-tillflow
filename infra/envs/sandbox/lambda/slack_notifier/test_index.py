"""Unit tests for the Slack notifier. No AWS, no network.

    cd infra/envs/sandbox/lambda/slack_notifier && python3 -m unittest -v
"""

from __future__ import annotations

import json
import os
import unittest

import index

HOOK = "https://hooks.slack.com/services/T000/B000/XXXX"


class FakeSecrets:
    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, secret_string=None):
        self.secret_string = secret_string

    def get_secret_value(self, SecretId):
        if self.secret_string is None:
            raise self.exceptions.ResourceNotFoundException(SecretId)
        return {"SecretString": self.secret_string}


def sns_event(alarm: dict) -> dict:
    return {"Records": [{"Sns": {"Message": json.dumps(alarm)}}]}


CONTRACT = {
    "environment": "sandbox",
    "service": "pos",
    "symptom": "POS fast burn",
    "slo_impact": "POS 99.9% budget burning 14.4x",
    "observed": "5xx 3%",
    "grafana_panel": "https://example.grafana.net/d/tillflow-pos",
    "runbook": "https://github.com/x/y/blob/main/docs/runbook.md#pos-fast-burn",
    "owner": "@Moraaalice",
    "first_safe_action": "Check /ready; do not restart the database",
}


class NotifierTest(unittest.TestCase):
    def setUp(self):
        os.environ["SLACK_SECRET_ID"] = "devops-g9/slack-webhook"
        self.sent = []

    def post(self, url, payload):
        self.sent.append((url, payload))
        return 200

    def test_firing_and_recovered_carry_every_contract_field(self):
        for state, word in (("ALARM", "FIRING"), ("OK", "RECOVERED")):
            self.sent.clear()
            alarm = {
                "AlarmName": "devops-g9-pos-fast-burn",
                "NewStateValue": state,
                "AlarmDescription": json.dumps(CONTRACT),
            }
            out = index.handler(
                sns_event(alarm), None, client=FakeSecrets(json.dumps({"url": HOOK})), post=self.post
            )
            self.assertEqual(out, {"posted": 1})
            url, payload = self.sent[0]
            self.assertEqual(url, HOOK)
            self.assertIn(word, payload["text"])
            self.assertIn("[sandbox] pos", payload["text"])
            titles = {f["title"] for f in payload["attachments"][0]["fields"]}
            self.assertTrue(
                {"SLO impact", "Observed", "Grafana panel", "Runbook", "Owner", "First safe action"}
                <= titles
            )

    def test_secret_without_a_value_drops_quietly(self):
        out = index.handler(sns_event({"NewStateValue": "ALARM"}), None, client=FakeSecrets(None), post=self.post)
        self.assertEqual(out["posted"], 0)
        self.assertEqual(self.sent, [])

    def test_placeholder_or_non_slack_url_is_never_posted_to(self):
        for value in ('{"url": "PLACEHOLDER"}', '{"url": "https://evil.example/hook"}', "not json"):
            out = index.handler(
                sns_event({"NewStateValue": "ALARM"}), None, client=FakeSecrets(value), post=self.post
            )
            self.assertEqual(out["posted"], 0, value)
        self.assertEqual(self.sent, [])

    def test_plain_text_description_falls_back_to_alarm_data(self):
        alarm = {
            "AlarmName": "devops-g9-probe-down",
            "NewStateValue": "ALARM",
            "NewStateReason": "Threshold crossed",
            "AlarmDescription": "probe failing",
        }
        c = index.contract(alarm)
        self.assertEqual(c["symptom"], "probe failing")
        self.assertEqual(c["observed"], "Threshold crossed")
        self.assertEqual(c["environment"], "sandbox")


if __name__ == "__main__":
    unittest.main()
