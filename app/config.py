from dataclasses import dataclass
import os
from typing import Optional, Set

from dotenv import load_dotenv


load_dotenv()


def _parse_admin_ids(raw: str) -> Set[int]:
    result: Set[int] = set()
    for chunk in raw.split(","):
        value = chunk.strip()
        if not value:
            continue
        result.add(int(value))
    return result


def _parse_optional_int(raw: str) -> Optional[int]:
    value = raw.strip()
    if not value:
        return None
    return int(value)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    db_path: str
    gmail_credentials_file: str
    gmail_token_file: str
    admin_user_ids: Set[int]
    sender_filter: str
    subject_must_contain: str
    target_chat_id: Optional[int]
    poll_interval_seconds: int
    gmail_query_extra: str


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("BOT_TOKEN topilmadi. .env faylga BOT_TOKEN kiriting.")

    poll_interval_raw = os.getenv("POLL_INTERVAL_SECONDS", "15").strip()
    poll_interval_seconds = int(poll_interval_raw) if poll_interval_raw else 15
    if poll_interval_seconds < 5:
        raise ValueError("POLL_INTERVAL_SECONDS kamida 5 bo'lishi kerak.")

    admin_user_ids = _parse_admin_ids(os.getenv("ADMIN_USER_IDS", ""))
    if not admin_user_ids:
        raise ValueError("ADMIN_USER_IDS bo'sh. .env faylga kamida bitta admin ID kiriting.")

    return Settings(
        bot_token=bot_token,
        db_path=os.getenv("DB_PATH", "bot.db").strip() or "bot.db",
        gmail_credentials_file=os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json").strip()
        or "credentials.json",
        gmail_token_file=os.getenv("GMAIL_TOKEN_FILE", "token.json").strip() or "token.json",
        admin_user_ids=admin_user_ids,
        sender_filter=os.getenv("SENDER_FILTER", "").strip(),
        subject_must_contain=os.getenv("SUBJECT_MUST_CONTAIN", "New request from").strip(),
        target_chat_id=_parse_optional_int(os.getenv("TARGET_CHAT_ID", "")),
        poll_interval_seconds=poll_interval_seconds,
        gmail_query_extra=os.getenv("GMAIL_QUERY_EXTRA", "").strip(),
    )
