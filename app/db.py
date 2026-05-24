from datetime import datetime
from typing import Optional

from peewee import (
    AutoField,
    BigIntegerField,
    BooleanField,
    CharField,
    DateTimeField,
    ForeignKeyField,
    Model,
    SqliteDatabase,
    TextField,
)


database = SqliteDatabase(None, pragmas={"journal_mode": "wal", "foreign_keys": 1})


class BaseModel(Model):
    class Meta:
        database = database


class BotSetting(BaseModel):
    key = CharField(unique=True)
    value = TextField()
    updated_at = DateTimeField(default=datetime.utcnow)


class EmailHistory(BaseModel):
    id = AutoField()
    gmail_message_id = CharField(unique=True)
    gmail_thread_id = CharField()
    sender = CharField()
    subject = TextField()
    snippet = TextField()
    internal_date = DateTimeField()
    forwarded = BooleanField(default=False)
    telegram_chat_id = BigIntegerField(null=True)
    telegram_message_id = BigIntegerField(null=True)
    created_at = DateTimeField(default=datetime.utcnow)


class Group(BaseModel):
    id = AutoField()
    chat_id = BigIntegerField(unique=True)
    title = CharField()
    is_active = BooleanField(default=True)
    added_by_user_id = BigIntegerField()
    created_at = DateTimeField(default=datetime.utcnow)


class EmailDelivery(BaseModel):
    id = AutoField()
    email = ForeignKeyField(EmailHistory, backref="deliveries", on_delete="CASCADE")
    group = ForeignKeyField(Group, backref="deliveries", on_delete="CASCADE")
    delivered = BooleanField(default=False)
    telegram_message_id = BigIntegerField(null=True)
    error_text = TextField(null=True)
    created_at = DateTimeField(default=datetime.utcnow)


def init_db(db_path: str) -> None:
    database.init(db_path)
    database.connect(reuse_if_open=True)
    database.create_tables([BotSetting, EmailHistory, Group, EmailDelivery])


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    row = BotSetting.get_or_none(BotSetting.key == key)
    return row.value if row else default


def set_setting(key: str, value: str) -> None:
    BotSetting.insert(key=key, value=value).on_conflict(
        conflict_target=[BotSetting.key],
        preserve=[BotSetting.key],
        update={BotSetting.value: value, BotSetting.updated_at: datetime.utcnow()},
    ).execute()


def get_sender_filter(env_value: str) -> str:
    cached = get_setting("sender_filter")
    if cached is not None:
        return cached
    return env_value


def set_sender_filter(sender: str) -> None:
    set_setting("sender_filter", sender)


def get_last_checked_ts() -> Optional[int]:
    raw = get_setting("last_checked_ts")
    if raw is None or not raw.strip():
        return None
    return int(raw)


def set_last_checked_ts(ts_seconds: int) -> None:
    set_setting("last_checked_ts", str(ts_seconds))


def upsert_group(chat_id: int, title: str, added_by_user_id: int) -> Group:
    Group.insert(
        chat_id=chat_id,
        title=title,
        is_active=True,
        added_by_user_id=added_by_user_id,
    ).on_conflict(
        conflict_target=[Group.chat_id],
        update={
            Group.title: title,
            Group.is_active: True,
            Group.added_by_user_id: added_by_user_id,
        },
    ).execute()
    return Group.get(Group.chat_id == chat_id)


def deactivate_group(group_id: int) -> bool:
    deleted = Group.delete().where(Group.id == group_id).execute()
    return deleted > 0


def deactivate_group_by_chat_id(chat_id: int) -> bool:
    deleted = Group.delete().where(Group.chat_id == chat_id).execute()
    return deleted > 0


def list_groups(active_only: bool = True) -> list[Group]:
    query = Group.select().order_by(Group.id.asc())
    if active_only:
        query = query.where(Group.is_active == True)  # noqa: E712
    return list(query)
