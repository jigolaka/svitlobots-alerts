from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


RESTORED = "\u0421\u0432\u0456\u0442\u043b\u043e \u0437'\u044f\u0432\u0438\u043b\u043e\u0441\u044f"
OUTAGE = "\u0421\u0432\u0456\u0442\u043b\u043e \u0437\u043d\u0438\u043a\u043b\u043e"
STATUS_PATTERN = re.compile(
    rf"^(?P<icon>[\U0001f7e2\U0001f534])\s*"
    rf"(?P<clock>(?:[01]\d|2[0-3]):[0-5]\d)\s+"
    rf"(?P<status>{re.escape(RESTORED)}|{re.escape(OUTAGE)})(?:\s|$)"
)


@dataclass(frozen=True)
class Event:
    message_id: int
    status: str
    clock: str
    published_at: datetime
    text: str
    url: str


def parse_events(html: str, channel: str) -> list[Event]:
    events: dict[int, Event] = {}
    soup = BeautifulSoup(html, "html.parser")
    for message in soup.select(".tgme_widget_message[data-post]"):
        post = str(message.get("data-post", ""))
        owner, separator, identifier = post.rpartition("/")
        if not separator or owner.casefold() != channel.casefold() or not identifier.isdigit():
            continue
        content = message.select_one(".tgme_widget_message_text")
        if content is None:
            continue
        text = content.get_text(" ", strip=True).replace(
            "\u2019", "'").replace("\u02bc", "'")
        match = STATUS_PATTERN.match(text)
        if match is None:
            continue
        status = "off" if match["status"] == OUTAGE else "on"
        expected_icon = "\U0001f534" if status == "off" else "\U0001f7e2"
        if match["icon"] != expected_icon:
            continue
        timestamp = message.select_one(
            ".tgme_widget_message_date time[datetime]")
        if timestamp is None:
            raise ValueError("Status message has no publication timestamp")
        published_at = datetime.fromisoformat(
            str(timestamp["datetime"]).replace("Z", "+00:00"))
        if published_at.tzinfo is None:
            raise ValueError("Status timestamp must include a timezone")
        message_id = int(identifier)
        events[message_id] = Event(
            message_id, status, match["clock"], published_at, text,
            f"https://t.me/{channel}/{message_id}",
        )
    return sorted(events.values(), key=lambda event: event.message_id)


DEFAULT_CHANNELS = ("svitlobot_ShevchenkoveBL", "svitlobot_juvileinyi")
LOGGER = logging.getLogger("svitlobot")


def configured_channels() -> tuple[str, ...]:
    channels = tuple(channel.strip() for channel in os.getenv(
        "CHANNELS", ",".join(DEFAULT_CHANNELS)).split(","))
    if not channels or any(not re.fullmatch(r"[A-Za-z0-9_]+", channel) for channel in channels):
        raise ValueError(
            "CHANNELS must contain comma-separated Telegram usernames")
    return channels


@dataclass(frozen=True)
class ChannelState:
    last_id: int
    night: str


def decode_state(raw: str) -> dict[str, ChannelState]:
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("channels"), dict):
        raise ValueError("Invalid state schema")
    result = {}
    for channel, value in data["channels"].items():
        if not isinstance(value, dict):
            raise ValueError("Invalid channel state")
        last_id, night = value.get("last_id"), value.get("night")
        if type(last_id) is not int or last_id < 1 or not isinstance(night, str):
            raise ValueError("Invalid channel checkpoint")
        datetime.strptime(night, "%Y-%m-%d")
        result[channel] = ChannelState(last_id, night)
    return result


def encode_state(state: dict[str, ChannelState]) -> str:
    return json.dumps({"version": 1, "channels": {key: asdict(value) for key, value in state.items()}}, indent=2) + "\n"


class FileStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, ChannelState]:
        if not self.path.exists():
            return {}
        return decode_state(self.path.read_text(encoding="utf-8"))

    def save(self, state: dict[str, ChannelState]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(encode_state(state), encoding="utf-8")
        temporary.replace(self.path)


class GitHubStore:
    def __init__(self, session: requests.Session):
        self.session = session
        repository = required_env("GITHUB_REPOSITORY")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Invalid GITHUB_REPOSITORY")
        self.base = f"https://api.github.com/repos/{repository}"
        self.headers = {
            "Authorization": f"Bearer {required_env('GITHUB_TOKEN')}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.branch = os.getenv("STATE_BRANCH", "svitlobot-state")
        self.sha: str | None = None

    def load(self) -> dict[str, ChannelState]:
        response = self.session.get(
            f"{self.base}/contents/state.json", params={"ref": self.branch},
            headers=self.headers, timeout=30,
        )
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        data = response.json()
        state = decode_state(base64.b64decode(data["content"]).decode("utf-8"))
        self.sha = data["sha"]
        return state

    def save(self, state: dict[str, ChannelState]) -> None:
        if self.sha is None:
            response = self.session.get(
                f"{self.base}/git/ref/heads/{quote(self.branch, safe='')}",
                headers=self.headers, timeout=30,
            )
            if response.status_code == 404:
                created = self.session.post(
                    f"{self.base}/git/refs", headers=self.headers, timeout=30,
                    json={"ref": f"refs/heads/{self.branch}",
                          "sha": required_env("GITHUB_SHA")},
                )
                created.raise_for_status()
            else:
                response.raise_for_status()
        payload = {
            "message": "Update power monitor checkpoint",
            "branch": self.branch,
            "content": base64.b64encode(encode_state(state).encode("utf-8")).decode("ascii"),
        }
        if self.sha is not None:
            payload["sha"] = self.sha
        response = self.session.put(
            f"{self.base}/contents/state.json", headers=self.headers, json=payload, timeout=30,
        )
        response.raise_for_status()
        self.sha = response.json()["content"]["sha"]


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Set {name}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def night_key(now: datetime, start: int, end: int) -> str | None:
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        raise ValueError("Night hours must be distinct integers from 0 to 23")
    active = start <= now.hour < end if start < end else now.hour >= start or now.hour < end
    if not active:
        return None
    day = now.date()
    if start > end and now.hour < end:
        day -= timedelta(days=1)
    return day.isoformat()


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = "svitlobot-alerts/1.0 (public channel power monitor)"
    retry = Retry(
        total=2, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}), respect_retry_after_header=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_events(session: requests.Session, channel: str) -> list[Event]:
    response = session.get(f"https://t.me/s/{channel}", timeout=(10, 30))
    response.raise_for_status()
    events = parse_events(response.text, channel)
    if not events:
        raise ValueError(
            "No status messages found; channel may be unavailable or markup changed")
    return events


@dataclass(frozen=True)
class Mailer:
    username: str
    password: str
    recipient: str
    timezone: ZoneInfo

    @classmethod
    def from_env(cls, timezone: ZoneInfo) -> Mailer:
        return cls(required_env("GMAIL_USERNAME"), required_env("GMAIL_APP_PASSWORD"), required_env("ALERT_TO"), timezone)

    def deliver(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.username
        message["To"] = self.recipient
        message["Subject"] = subject
        message.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(self.username, self.password)
            refused = smtp.send_message(message)
            if refused:
                raise RuntimeError("SMTP refused a recipient")

    def notify(self, channel: str, event: Event, *, simulated: bool = False) -> None:
        status = "POWER OUTAGE" if event.status == "off" else "POWER RESTORED"
        published = event.published_at.astimezone(
            self.timezone).isoformat(timespec="seconds")
        subject = f"[Svitlobot] {status}: {channel}"
        if simulated:
            subject += " [SIMULATED TEST]"
        disclaimer = (
            "This is a simulated restoration test email; no live power status was checked."
            if simulated else
            "This reports a Telegram status post, not a direct power measurement. "
            "A later post may already report a change."
        )
        self.deliver(
            subject,
            f"Channel: {channel}\nStatus event: {status}\n"
            f"Reported local event time: {event.clock} (channel's clock)\n"
            f"Post published: {published}\n\n{event.text}\n\n{event.url}\n\n"
            f"{disclaimer}",
        )


def process_events(
    channel: str, events: list[Event], state: dict[str, ChannelState], night: str,
    notify: Callable[[str, Event], None], save: Callable[[dict[str, ChannelState]], None],
    notify_restored: bool = False,
) -> None:
    previous = state.get(channel)
    if previous is None or previous.night != night:
        pending = events[-1:]
    else:
        pending = [event for event in events if event.message_id >
                   previous.last_id]
        if events[-1].message_id < previous.last_id:
            raise ValueError("Channel page is older than its checkpoint")
        if events[0].message_id > previous.last_id:
            LOGGER.warning(
                "%s: checkpoint no longer visible; some events may be missing", channel)
    for event in pending:
        baseline = previous is None or previous.night != night
        if event.status == "off" or (notify_restored and not baseline):
            notify(channel, event)
            LOGGER.info("%s: sent %s alert for post %d",
                        channel, event.status, event.message_id)
        state[channel] = ChannelState(event.message_id, night)
        save(state)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check public Svitlobot channels once; no Telegram token required.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true",
                       help="Fetch latest statuses without email or state changes; ignores night hours")
    modes.add_argument("--test-email", action="store_true",
                       help="Send a test email without scraping or state changes")
    modes.add_argument("--test-restoration", action="store_true",
                       help="Send simulated restoration emails without scraping or state changes")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        timezone = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "Europe/Kyiv"))
        if args.test_email:
            Mailer.from_env(timezone).deliver(
                "[Svitlobot] Test email", "Gmail delivery is configured successfully.")
            LOGGER.info("Test email accepted by Gmail SMTP")
            return 0
        if args.test_restoration:
            mailer = Mailer.from_env(timezone)
            now = datetime.now(timezone)
            channels = configured_channels()
            for channel in channels:
                event = Event(
                    0, "on", now.strftime("%H:%M"), now,
                    "SIMULATION ONLY: This test assumes power has been restored.",
                    "No Telegram post; simulated test only.",
                )
                mailer.notify(channel, event, simulated=True)
            LOGGER.info(
                "Sent %d simulated restoration test emails via Gmail SMTP", len(channels))
            return 0
        night = night_key(
            datetime.now(timezone), int(os.getenv("NIGHT_START_HOUR", "0")),
            int(os.getenv("NIGHT_END_HOUR", "6")),
        )
        if night is None and not args.dry_run:
            LOGGER.info(
                "Outside the configured night window; no scraping or email")
            return 0
        channels = configured_channels()
        notify_restored = env_bool("NOTIFY_RESTORED")
        mailer = None if args.dry_run else Mailer.from_env(timezone)
        with make_session() as session:
            backend = os.getenv("STATE_BACKEND", "file")
            if backend not in {"file", "github"}:
                raise ValueError("STATE_BACKEND must be file or github")
            store = None if args.dry_run else (
                GitHubStore(session) if backend == "github" else FileStore(
                    Path(os.getenv("STATE_FILE", ".state/state.json")))
            )
            state = {} if store is None else store.load()
            failures = 0
            for channel in channels:
                try:
                    events = fetch_events(session, channel)
                    latest = events[-1]
                    LOGGER.info("%s: latest=%s reported=%s post=%s",
                                channel, latest.status, latest.clock, latest.url)
                    if store is not None and mailer is not None and night is not None:
                        process_events(channel, events, state, night,
                                       mailer.notify, store.save, notify_restored)
                except Exception as error:
                    failures += 1
                    LOGGER.error("%s: check failed (%s); see README troubleshooting", channel, type(
                        error).__name__)
            return 1 if failures else 0
    except Exception as error:
        LOGGER.error("Monitor failed (%s); check configuration, credentials, and state. See README.", type(
            error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
