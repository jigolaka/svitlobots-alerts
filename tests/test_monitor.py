import base64
import json
import smtplib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from monitor import (
    DEFAULT_CHANNELS, OUTAGE, RESTORED, ChannelState, Event, FileStore,
    GitHubStore, Mailer, decode_state, encode_state, fetch_events, main,
    night_key, parse_events, process_events,
)


CHANNEL = "svitlobot_ShevchenkoveBL"


def message(identifier: int, text: str, timestamp: str = "2026-11-10T05:22:41+00:00") -> str:
    return (
        f'<div class="tgme_widget_message" data-post="{CHANNEL}/{identifier}">'
        f'<div class="tgme_widget_message_text">{text}</div>'
        f'<a class="tgme_widget_message_date"><time datetime="{timestamp}"></time></a>'
        '</div>'
    )


class ParserTests(unittest.TestCase):
    def test_real_format_html_entities_and_publication_delay(self):
        html = message(
            2001, f'<i><b>\U0001f7e2</b></i> <b>07:11 {RESTORED.replace(chr(39), "&#39;")}</b>')
        html += message(2002,
                        f'<i>\U0001f534</i> <b>07:18 {OUTAGE}</b><br/>More text')
        events = parse_events(html, CHANNEL)
        self.assertEqual([event.status for event in events], ["on", "off"])
        self.assertEqual(events[-1].clock, "07:18")
        self.assertEqual(events[-1].published_at.minute, 22)
        self.assertEqual(events[-1].url, f"https://t.me/{CHANNEL}/2002")

    def test_ignores_schedule_and_non_status_messages(self):
        html = message(
            2005, f'Schedule:<br/>\U0001f534 08:00 - 17:00<br/>07:18 {OUTAGE}')
        html += message(2006, '\U0001f7e2 03:00 - 08:00')
        html += message(2007, f'\U0001f7e2 07:18 {OUTAGE}')
        self.assertEqual(parse_events(html, CHANNEL), [])

    def test_curly_apostrophe_no_space_after_emoji_sort_and_deduplicate(self):
        text = '\U0001f7e207:11 ' + RESTORED.replace("'", "\u2019")
        html = message(20, text) + message(19,
                                           f'\U0001f534 06:00 {OUTAGE}') + message(20, text)
        self.assertEqual(
            [event.message_id for event in parse_events(html, CHANNEL)], [19, 20])

    def test_rejects_missing_timezone(self):
        with self.assertRaises(ValueError):
            parse_events(
                message(1, f'\U0001f534 07:18 {OUTAGE}', '2026-11-10T07:22:00'), CHANNEL)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.events = parse_events(
            message(1, f'\U0001f7e2 01:00 {RESTORED}')
            + message(2, f'\U0001f534 02:00 {OUTAGE}')
            + message(3, f'\U0001f7e2 03:00 {RESTORED}'), CHANNEL,
        )
        self.state = {}
        self.notify = Mock()
        self.save = Mock()

    def process(self, events=None, night="2026-11-10", restored=False):
        process_events(CHANNEL, self.events if events is None else events, self.state,
                       night, self.notify, self.save, restored)

    def test_initial_on_status_baselines_without_historical_outage_email(self):
        self.process()
        self.notify.assert_not_called()
        self.assertEqual(self.state[CHANNEL], ChannelState(3, "2026-11-10"))

    def test_initial_off_alerts_once_and_retry_deduplicates(self):
        self.process(self.events[:2])
        self.process(self.events[:2])
        self.notify.assert_called_once_with(CHANNEL, self.events[1])
        self.save.assert_called_once()

    def test_all_unseen_events_processed_including_brief_outage(self):
        self.state[CHANNEL] = ChannelState(1, "2026-11-10")
        self.process()
        self.notify.assert_called_once_with(CHANNEL, self.events[1])
        self.assertEqual(self.state[CHANNEL].last_id, 3)
        self.assertEqual(self.save.call_count, 2)

    def test_optional_restoration_email(self):
        self.state[CHANNEL] = ChannelState(1, "2026-11-10")
        self.process(restored=True)
        self.assertEqual(
            [call.args[1].status for call in self.notify.call_args_list], ["off", "on"])

    def test_delivery_failure_does_not_advance_checkpoint(self):
        self.state[CHANNEL] = ChannelState(1, "2026-11-10")
        self.notify.side_effect = smtplib.SMTPException("delivery failed")
        with self.assertRaises(smtplib.SMTPException):
            self.process()
        self.assertEqual(self.state[CHANNEL].last_id, 1)
        self.save.assert_not_called()

    def test_new_night_baselines_latest_and_alerts_ongoing_outage(self):
        self.state[CHANNEL] = ChannelState(1, "2026-11-09")
        self.process()
        self.notify.assert_not_called()
        self.process(self.events[:2], night="2026-11-11")
        self.notify.assert_called_once_with(CHANNEL, self.events[1])

    def test_stale_page_does_not_roll_checkpoint_back(self):
        self.state[CHANNEL] = ChannelState(5, "2026-11-10")
        with self.assertRaises(ValueError):
            self.process()
        self.save.assert_not_called()

    def test_channels_have_independent_state(self):
        self.process(self.events[:2])
        process_events(
            "another_channel", self.events[:2], self.state, "2026-11-10", self.notify, self.save)
        self.assertEqual(self.notify.call_count, 2)


class StorageAndTimeTests(unittest.TestCase):
    def test_state_roundtrip_and_file_store(self):
        state = {CHANNEL: ChannelState(2, "2026-11-10")}
        self.assertEqual(decode_state(encode_state(state)), state)
        with tempfile.TemporaryDirectory() as directory:
            store = FileStore(Path(directory) / "nested" / "state.json")
            self.assertEqual(store.load(), {})
            store.save(state)
            self.assertEqual(store.load(), state)

    def test_invalid_state_is_not_silently_reset(self):
        for raw in ('{}', '{"version": 1, "channels": {"x": {"last_id": true, "night": "2026-11-10"}}}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                decode_state(raw)

    def test_kyiv_midnight_summer_and_winter(self):
        for utc in ("2026-01-09T22:00:00+00:00", "2026-07-09T21:00:00+00:00"):
            local = datetime.fromisoformat(
                utc).astimezone(ZoneInfo("Europe/Kyiv"))
            self.assertEqual(local.hour, 0)
            self.assertEqual(night_key(local, 0, 6), local.date().isoformat())

    def test_window_boundaries_and_cross_midnight(self):
        now = datetime.fromisoformat("2026-11-10T00:00:00+02:00")
        self.assertEqual(night_key(now, 0, 6), "2026-11-10")
        self.assertIsNone(night_key(now.replace(hour=6), 0, 6))
        self.assertEqual(night_key(now, 22, 6), "2026-11-09")
        self.assertIsNone(night_key(now.replace(hour=12), 22, 6))
        with self.assertRaises(ValueError):
            night_key(now, 6, 6)

    @patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "fake", "GITHUB_SHA": "abc"})
    def test_github_bootstrap_and_update_uses_returned_sha(self):
        session = Mock()
        session.get.return_value.status_code = 404
        session.put.return_value.json.return_value = {
            "content": {"sha": "new-sha"}}
        store = GitHubStore(session)
        self.assertEqual(store.load(), {})
        state = {CHANNEL: ChannelState(2, "2026-11-10")}
        store.save(state)
        session.post.assert_called_once()
        payload = session.put.call_args.kwargs["json"]
        self.assertNotIn("sha", payload)
        self.assertEqual(decode_state(base64.b64decode(
            payload["content"]).decode()), state)
        store.save(state)
        self.assertEqual(
            session.put.call_args.kwargs["json"]["sha"], "new-sha")

    @patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "fake"})
    def test_github_existing_checkpoint_and_api_failure(self):
        session = Mock()
        session.get.return_value.status_code = 200
        state = {CHANNEL: ChannelState(2, "2026-11-10")}
        session.get.return_value.json.return_value = {
            "sha": "old-sha", "content": base64.b64encode(encode_state(state).encode()).decode(),
        }
        store = GitHubStore(session)
        self.assertEqual(store.load(), state)
        session.put.return_value.raise_for_status.side_effect = RuntimeError(
            "API unavailable")
        with self.assertRaises(RuntimeError):
            store.save(state)
        self.assertEqual(
            session.put.call_args.kwargs["json"]["sha"], "old-sha")

    def test_empty_telegram_page_fails_instead_of_reporting_power_on(self):
        session = Mock()
        session.get.return_value.text = "<html>Unavailable</html>"
        with self.assertRaises(ValueError):
            fetch_events(session, CHANNEL)

    @patch("monitor.smtplib.SMTP_SSL")
    def test_smtp_tls_login_and_message_content(self, smtp_class):
        smtp_class.return_value.__enter__.return_value.send_message.return_value = {}
        mailer = Mailer("sender@example.com", "fake",
                        "recipient@example.com", ZoneInfo("Europe/Kyiv"))
        event = parse_events(
            message(2, f'\U0001f534 07:18 {OUTAGE}'), CHANNEL)[0]
        mailer.notify(CHANNEL, event)
        smtp = smtp_class.return_value.__enter__.return_value
        smtp.login.assert_called_once_with("sender@example.com", "fake")
        sent = smtp.send_message.call_args.args[0]
        self.assertIn("POWER OUTAGE", str(sent["Subject"]))
        self.assertIn(event.url, sent.get_content())
        self.assertIn("07:18", sent.get_content())

    @patch("monitor.smtplib.SMTP_SSL")
    def test_simulated_restoration_email_is_marked_and_uses_restoration_format(self, smtp_class):
        smtp_class.return_value.__enter__.return_value.send_message.return_value = {}
        mailer = Mailer("sender@example.com", "fake",
                        "recipient@example.com", ZoneInfo("Europe/Kyiv"))
        event = Event(
            0, "on", "07:18", datetime.fromisoformat(
                "2026-11-10T05:22:41+00:00"),
            "SIMULATION ONLY: This test assumes power has been restored.",
            "No Telegram post; simulated test only.",
        )

        mailer.notify(CHANNEL, event, simulated=True)

        sent = smtp_class.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertEqual(
            str(sent["Subject"]),
            f"[Svitlobot] POWER RESTORED: {CHANNEL} [SIMULATED TEST]",
        )
        body = sent.get_content()
        self.assertIn("Status event: POWER RESTORED", body)
        self.assertIn("SIMULATION ONLY", body)
        self.assertIn("no live power status was checked", body)

    def test_restoration_test_sends_both_channels_without_scraping_or_state(self):
        with (
            patch("sys.argv", ["monitor.py", "--test-restoration"]),
            patch.dict("os.environ", {"CHANNELS": ",".join(DEFAULT_CHANNELS)}),
            patch("monitor.Mailer.from_env") as mailer_factory,
            patch("monitor.make_session") as make_session,
        ):
            self.assertEqual(main(), 0)

        mailer = mailer_factory.return_value
        self.assertEqual(
            [call.args[0] for call in mailer.notify.call_args_list],
            list(DEFAULT_CHANNELS),
        )
        for call in mailer.notify.call_args_list:
            self.assertEqual(call.args[1].status, "on")
            self.assertTrue(call.kwargs["simulated"])
            self.assertIn("SIMULATION ONLY", call.args[1].text)
        make_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
