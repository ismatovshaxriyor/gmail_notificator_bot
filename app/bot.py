import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import Settings
from app.db import (
    EmailDelivery,
    EmailHistory,
    Group,
    deactivate_group,
    deactivate_group_by_chat_id,
    get_last_checked_ts,
    get_sender_filter,
    list_groups,
    set_last_checked_ts,
    set_sender_filter,
    upsert_group,
)
from app.gmail_service import GmailService


LOGGER = logging.getLogger(__name__)

MENU_PREFIX = "menu"
GROUP_PREFIX = "grp"
HISTORY_PREFIX = "hst"


@dataclass
class OrderEmailDetails:
    order_id: str = ""
    price: str = ""
    pickup_location: str = ""
    pickup_date: str = ""
    dropoff_location: str = ""
    dropoff_date: str = ""
    vehicle: str = ""
    carrier: str = ""
    phones: str = ""

    def has_data(self) -> bool:
        return bool(
            self.order_id
            or self.price
            or self.pickup_location
            or self.dropoff_location
            or self.vehicle
            or self.carrier
        )


SECTION_LABELS = ["order id", "price", "bid", "vehicles", "vehicle", "carrier", "view requests"]
LOCATION_RE = re.compile(r"\b[A-Za-z][A-Za-z .'-]*,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")
DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b",
    re.IGNORECASE,
)
PHONE_RE = re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}")


def _clean_lines(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.replace("\r", "\n").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        lines.append(line)
    return lines


def _extract_labeled_value(lines: List[str], label: str) -> str:
    label_lower = label.lower()
    for idx, line in enumerate(lines):
        lower = line.lower()
        if not lower.startswith(label_lower):
            continue

        suffix = line[len(label) :].strip(" :-")
        if suffix:
            return suffix

        for next_idx in range(idx + 1, min(idx + 6, len(lines))):
            nxt = lines[next_idx]
            nxt_lower = nxt.lower()
            if any(nxt_lower.startswith(section) for section in SECTION_LABELS):
                break
            return nxt
    return ""


def _parse_order_email(body_text: str) -> OrderEmailDetails:
    lines = _clean_lines(body_text)
    if not lines:
        return OrderEmailDetails()

    body_joined = "\n".join(lines)
    details = OrderEmailDetails(
        order_id=_extract_labeled_value(lines, "Order ID"),
        price=_extract_labeled_value(lines, "Price") or _extract_labeled_value(lines, "Bid"),
        vehicle=_extract_labeled_value(lines, "Vehicles")
        or _extract_labeled_value(lines, "Vehicle"),
        carrier=_extract_labeled_value(lines, "Carrier"),
    )
    details.order_id = details.order_id.strip(" .")
    details.price = details.price.strip(" .")
    details.vehicle = details.vehicle.strip(" .·")
    details.carrier = details.carrier.strip(" .")

    locations = []
    for line in lines:
        match = LOCATION_RE.search(line)
        if match:
            locations.append(match.group(0))
    if locations:
        details.pickup_location = locations[0]
    if len(locations) > 1:
        details.dropoff_location = locations[1]

    dates = [m.group(0) for m in DATE_RE.finditer(body_joined)]
    if dates:
        details.pickup_date = dates[0]
    if len(dates) > 1:
        details.dropoff_date = dates[1]

    phones = PHONE_RE.findall(body_joined)
    if phones:
        uniq = []
        for phone in phones:
            normalized = re.sub(r"\s+", " ", phone).strip()
            if normalized not in uniq:
                uniq.append(normalized)
        details.phones = ", ".join(uniq[:3])
    return details


def _format_email_message(row: EmailHistory, body_text: str) -> str:
    snippet = row.snippet.strip() if row.snippet else "(Bo'sh)"
    if len(snippet) > 500:
        snippet = snippet[:500] + "..."

    details = _parse_order_email(body_text)
    if details.has_data():
        parts = [
            "NEW SD REQUEST",
            f"{row.subject}",
            f"{row.sender}",
            "",
        ]
        if details.order_id:
            parts.append(f"Order ID: {details.order_id}")
        if details.price:
            parts.append(f"Price: {details.price}")
        if details.pickup_location:
            pickup = f"Pickup: {details.pickup_location}"
            if details.pickup_date:
                pickup += f" ({details.pickup_date})"
            parts.append(pickup)
        if details.dropoff_location:
            dropoff = f"Dropoff: {details.dropoff_location}"
            if details.dropoff_date:
                dropoff += f" ({details.dropoff_date})"
            parts.append(dropoff)
        if details.vehicle:
            parts.append(f"Vehicle: {details.vehicle}")
        if details.carrier:
            parts.append(f"Carrier: {details.carrier}")
        if details.phones:
            parts.append(f"Phone: {details.phones}")

        message = "\n".join(parts).strip()
        if len(message) > 4000:
            return message[:4000] + "..."
        return message

    fallback = (
        "NEW SD REQUEST\n"
        f"{row.subject}\n"
        f"{row.sender}\n"
        "\n"
        f"Snippet: {snippet}"
    )
    if len(fallback) > 4000:
        return fallback[:4000] + "..."
    return fallback


class GmailForwardBot:
    def __init__(self, settings: Settings, gmail: GmailService) -> None:
        self.settings = settings
        self.gmail = gmail

    async def post_init(self, application: Application) -> None:
        if not self.gmail.authenticated:
            try:
                auth_url, _ = self.gmail.generate_auth_url()
                text = (
                    "⚠️ Gmail hisobingiz ulanmagan.\n\n"
                    "Tokenni olish uchun quyidagi havolaga bosing, ruxsat bering, keyin brauzeringiz manzillar satridagi (URL) manzilni to'liq nusxalab yoki undagi code qiymatini ushbu botga yuboring:\n\n"
                    f"{auth_url}"
                )
                for admin_id in self.settings.admin_user_ids:
                    try:
                        await application.bot.send_message(chat_id=admin_id, text=text)
                    except Exception as exc:
                        LOGGER.exception("Admin %s ga xabar yuborishda xato: %s", admin_id, exc)
            except Exception as exc:
                LOGGER.exception("Auth link yaratishda xato: %s", exc)

    def _extract_auth_code(self, text: str) -> Optional[str]:
        text = text.strip()
        if text.startswith("http://") or text.startswith("https://"):
            from urllib.parse import urlparse, parse_qs
            try:
                parsed = urlparse(text)
                qs = parse_qs(parsed.query)
                if "code" in qs:
                    return qs["code"][0]
            except Exception:
                pass
        # Google authorization code is usually a string with no spaces.
        # It can have length 10+ characters.
        if len(text) >= 10 and not any(c.isspace() for c in text):
            return text
        return None

    def _is_admin(self, update: Update) -> bool:
        user = update.effective_user
        return bool(user and user.id in self.settings.admin_user_ids)

    async def _reject_non_admin(self, update: Update) -> None:
        if update.callback_query:
            try:
                await update.callback_query.answer()
            except Exception:
                pass

    async def _ensure_admin(self, update: Update) -> bool:
        if self._is_admin(update):
            return True
        await self._reject_non_admin(update)
        return False

    async def _ensure_group_or_admin(self, update: Update, allowed_in_group: bool = False) -> bool:
        chat = update.effective_chat
        if not chat:
            return False

        if chat.type == "private":
            if self._is_admin(update):
                return True
            await self._reject_non_admin(update)
            return False

        # Group or supergroup chat
        if allowed_in_group:
            # Check if group is registered and active
            grp = Group.get_or_none(Group.chat_id == chat.id, Group.is_active == True)
            if grp:
                return True
            # If not registered, only bot admin can run/interact
            if self._is_admin(update):
                return True
            return False
        else:
            # Not allowed for general group members, must be bot admin
            if self._is_admin(update):
                return True
            return False


    @staticmethod
    async def _edit_or_reply(
        update: Update,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> None:
        query = update.callback_query
        if query:
            try:
                await query.edit_message_text(text=text, reply_markup=reply_markup)
                return
            except BadRequest:
                await query.message.reply_text(text=text, reply_markup=reply_markup)
                return

        if update.message:
            await update.message.reply_text(text=text, reply_markup=reply_markup)

    @staticmethod
    def _main_menu_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Status", callback_data=f"{MENU_PREFIX}:status"),
                    InlineKeyboardButton("Check Now", callback_data=f"{MENU_PREFIX}:check"),
                ],
                [
                    InlineKeyboardButton("Guruhlar", callback_data=f"{MENU_PREFIX}:groups"),
                    InlineKeyboardButton("History", callback_data=f"{MENU_PREFIX}:history"),
                ],
                [
                    InlineKeyboardButton("Yordam", callback_data=f"{MENU_PREFIX}:help"),
                    InlineKeyboardButton("Yangilash", callback_data=f"{MENU_PREFIX}:home"),
                ],
            ]
        )

    @staticmethod
    def _history_markup(limit: int, rows: List[EmailHistory], is_group: bool = False) -> InlineKeyboardMarkup:
        def label(v: int) -> str:
            return f"• {v}" if v == limit else str(v)

        keyboard: list[list[InlineKeyboardButton]] = []
        
        # Email detail view buttons
        email_buttons = []
        for row in rows:
            email_buttons.append(
                InlineKeyboardButton(f"#{row.id}", callback_data=f"{HISTORY_PREFIX}:view:{row.id}:{limit}")
            )
        # Split email buttons into rows of 5
        for i in range(0, len(email_buttons), 5):
            keyboard.append(email_buttons[i:i+5])

        keyboard.append(
            [
                InlineKeyboardButton(label(5), callback_data=f"{HISTORY_PREFIX}:limit:5"),
                InlineKeyboardButton(label(10), callback_data=f"{HISTORY_PREFIX}:limit:10"),
                InlineKeyboardButton(label(20), callback_data=f"{HISTORY_PREFIX}:limit:20"),
            ]
        )
        if not is_group:
            keyboard.append([InlineKeyboardButton("Back", callback_data=f"{MENU_PREFIX}:home")])
        return InlineKeyboardMarkup(keyboard)

    def _render_home_text(self) -> str:
        sender_filter = get_sender_filter(self.settings.sender_filter) or "(hali berilmagan)"
        subject_filter = self.settings.subject_must_contain or "(yo'q)"
        group_count = len(list_groups(active_only=True))
        if self.gmail.authenticated:
            auth_status = "✅ Ulangan"
            auth_instruction = ""
        else:
            auth_status = "⚠️ ULANMAGAN!"
            try:
                auth_url, _ = self.gmail.generate_auth_url()
                auth_instruction = (
                    "\n\n🔑 Gmail-ni ulash uchun quyidagi havolaga bosing, ruxsat bering, keyin brauzeringiz manzillar satridagi (URL) manzilni to'liq nusxalab ushbu botga yuboring:\n\n"
                    f"{auth_url}"
                )
            except Exception as exc:
                LOGGER.exception("Auth link yaratishda xato: %s", exc)
                auth_instruction = "\n\n⚠️ Google Client Secret faylini tekshiring yoki sozlamalarni ko'rib chiqing."

        return (
            "Gmail Forward Bot\n\n"
            f"Gmail holati: {auth_status}\n"
            f"Kuzatilayotgan email: {sender_filter}\n"
            f"Subject filter: {subject_filter}\n"
            f"Ulangan guruhlar: {group_count}\n"
            f"Tekshiruv oralig'i: {self.settings.poll_interval_seconds} soniya"
            f"{auth_instruction}\n\n"
            "Quyidagi menyudan boshqaring."
        )

    def _render_help_text(self) -> str:
        return (
            "Qisqa yo'riqnoma\n\n"
            "1) /setsender sender@gmail.com bilan qaysi email kuzatilishini belgilang.\n"
            "2) Guruhga botni qo'shing va shu guruhda /groups bosing (yoki shaxsiyda /addgroup <chat_id> buyrug'ini yuboring).\n"
            "3) Inline tugma orqali guruhni ro'yxatga qo'shing (guruhda bot admin bo'lishi shart).\n"
            "4) /checknow bilan darhol tekshirib ko'ring.\n\n"
            "Asosiy komandalar:\n"
            "/menu, /status, /groups, /history, /checknow, /addgroup, /delgroup"
        )

    def _render_status_text(self) -> str:
        sender_filter = get_sender_filter(self.settings.sender_filter)
        subject_filter = self.settings.subject_must_contain
        groups = list_groups(active_only=True)
        last_checked = get_last_checked_ts()
        if last_checked:
            last_checked_text = datetime.fromtimestamp(last_checked).astimezone().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        else:
            last_checked_text = "hali tekshirilmagan"

        sender_state = "tayyor" if sender_filter else "yo'q"
        subject_state = "tayyor" if subject_filter else "yo'q"
        groups_state = "tayyor" if groups else "yo'q"
        auth_state = "tayyor" if self.gmail.authenticated else "ULANMAGAN ⚠️"

        return (
            "Joriy holat\n\n"
            f"Gmail ulanishi: {auth_state}\n"
            f"Sender filter: {sender_filter or '(kiritilmagan)'}\n"
            f"Subject filter: {subject_filter or '(kiritilmagan)'}\n"
            f"Guruhlar soni: {len(groups)}\n"
            f"Poll interval: {self.settings.poll_interval_seconds} soniya\n"
            f"Oxirgi tekshiruv: {last_checked_text}\n\n"
            f"Konfiguratsiya: auth={auth_state}, sender={sender_state}, subject={subject_state}, groups={groups_state}"
        )

    def _render_history_text(self, rows: List[EmailHistory], limit: int) -> str:
        lines = [f"Oxirgi yuborilgan xatlar (limit: {limit})", ""]
        has_rows = False
        for row in rows:
            has_rows = True
            delivered_count = (
                EmailDelivery.select()
                .where(
                    (EmailDelivery.email == row)
                    & (EmailDelivery.delivered == True)  # noqa: E712
                )
                .count()
            )
            dt = row.internal_date
            if isinstance(dt, str):
                dt = datetime.fromisoformat(dt)
            lines.append(
                f"{row.id}. [{delivered_count} group] {row.subject[:36]} | {dt.astimezone().strftime('%m-%d %H:%M')}"
            )

        if not has_rows:
            lines.append("Hali yuborilgan email yo'q.")
        return "\n".join(lines)

    def _get_history_rows(self, chat, limit: int) -> List[EmailHistory]:
        if not chat:
            return []
        if chat.type in ("group", "supergroup"):
            grp = Group.get_or_none(Group.chat_id == chat.id, Group.is_active == True)
            if not grp:
                return []
            return list(
                EmailHistory.select()
                .join(EmailDelivery)
                .where(
                    (EmailDelivery.group == grp)
                    & (EmailDelivery.delivered == True)
                )
                .order_by(EmailHistory.id.desc())
                .limit(limit)
            )
        else:
            return list(
                EmailHistory.select()
                .where(EmailHistory.forwarded == True)
                .order_by(EmailHistory.id.desc())
                .limit(limit)
            )

    async def _render_email_detail(self, email_id: int, limit: int, chat: Optional[Update.effective_chat] = None) -> Tuple[str, InlineKeyboardMarkup]:
        row = EmailHistory.get_or_none(EmailHistory.id == email_id)
        if not row:
            return "Email topilmadi.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Ro'yxatga qaytish", callback_data=f"{HISTORY_PREFIX}:list:{limit}")]])

        is_group_chat = chat and chat.type in ("group", "supergroup")
        if is_group_chat:
            grp = Group.get_or_none(Group.chat_id == chat.id, Group.is_active == True)
            if not grp:
                return "Ruxsat etilmagan.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Ro'yxatga qaytish", callback_data=f"{HISTORY_PREFIX}:list:{limit}")]])
            
            # Check if this email was delivered to this group
            delivery = EmailDelivery.get_or_none(EmailDelivery.email == row, EmailDelivery.group == grp, EmailDelivery.delivered == True)
            if not delivery:
                return "Ruxsat etilmagan.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Ro'yxatga qaytish", callback_data=f"{HISTORY_PREFIX}:list:{limit}")]])

        # Get deliveries to display
        if is_group_chat:
            grp = Group.get_or_none(Group.chat_id == chat.id, Group.is_active == True)
            deliveries = EmailDelivery.select().where(EmailDelivery.email == row, EmailDelivery.group == grp)
        else:
            deliveries = EmailDelivery.select().where(EmailDelivery.email == row)

        delivery_lines = []
        for d in deliveries:
            status = "✅ Yetkazildi" if d.delivered else f"❌ Xato: {d.error_text or 'noma\'lum'}"
            delivery_lines.append(f"- {d.group.title}: {status}")
        delivery_status_text = "\n".join(delivery_lines) if delivery_lines else "Yuborilmagan"

        body_text = ""
        try:
            msg = self.gmail.get_message(row.gmail_message_id)
            body_text = msg.body_text or msg.snippet
        except Exception:
            body_text = row.snippet

        formatted_content = _format_email_message(row, body_text)

        text = (
            f"{formatted_content}\n\n"
            f"Delivery status:\n"
            f"{delivery_status_text}"
        )

        keyboard = [
            [InlineKeyboardButton("⬅️ Ro'yxatga qaytish", callback_data=f"{HISTORY_PREFIX}:list:{limit}")]
        ]
        return text, InlineKeyboardMarkup(keyboard)

    def _render_groups_panel(
        self,
        chat_type: Optional[str],
        chat_id: Optional[int],
        chat_title: Optional[str],
    ) -> Tuple[str, InlineKeyboardMarkup]:
        groups = list_groups(active_only=True)
        lines = [f"Ulangan guruhlar: {len(groups)}"]
        if groups:
            lines.append("")
            for idx, group in enumerate(groups, start=1):
                lines.append(f"{idx}. {group.title}")
        else:
            lines.append("Hali guruh qo'shilmagan.")

        keyboard: list[list[InlineKeyboardButton]] = []
        for group in groups:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"➖ O'chirish: {group.title}",
                        callback_data=f"{GROUP_PREFIX}:del:{group.id}",
                    )
                ]
            )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "➕ Guruh qo'shish",
                    callback_data=f"{GROUP_PREFIX}:add_prompt",
                )
            ]
        )

        keyboard.append([InlineKeyboardButton("🔄 Ro'yxatni yangilash", callback_data=f"{GROUP_PREFIX}:refresh")])
        keyboard.append([InlineKeyboardButton("Back", callback_data=f"{MENU_PREFIX}:home")])
        return "\n".join(lines), InlineKeyboardMarkup(keyboard)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
            return
        if not await self._ensure_admin(update):
            return
        text = self._render_home_text()
        await self._edit_or_reply(update, text, self._main_menu_markup())

    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
            return
        if not await self._ensure_admin(update):
            return
        text = self._render_home_text()
        await self._edit_or_reply(update, text, self._main_menu_markup())

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
            return
        if not await self._ensure_admin(update):
            return
        await self._edit_or_reply(update, self._render_help_text(), self._main_menu_markup())

    async def set_sender(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
            return
        if not await self._ensure_admin(update):
            return

        if not context.args:
            await self._edit_or_reply(
                update,
                "Foydalanish:\n/setsender sender@gmail.com",
                self._main_menu_markup(),
            )
            return

        sender = context.args[0].strip().lower()
        if "@" not in sender:
            await self._edit_or_reply(
                update,
                "Email formati noto'g'ri. Misol:\n/setsender sender@gmail.com",
                self._main_menu_markup(),
            )
            return

        set_sender_filter(sender)
        await self._edit_or_reply(
            update,
            f"Sender saqlandi: {sender}",
            self._main_menu_markup(),
        )

    async def groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
            return
        if not await self._ensure_admin(update):
            return

        chat = update.effective_chat
        text, markup = self._render_groups_panel(
            chat_type=chat.type if chat else None,
            chat_id=chat.id if chat else None,
            chat_title=chat.title if chat else None,
        )
        await self._edit_or_reply(update, text, markup)

    async def add_group_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
            return
        if not await self._ensure_admin(update):
            return

        if not context.args:
            await self._edit_or_reply(
                update,
                "Foydalanish:\n/addgroup <chat_id>\n\nMisol: /addgroup -100123456789",
                self._main_menu_markup(),
            )
            return

        try:
            chat_id = int(context.args[0].strip())
        except ValueError:
            await self._edit_or_reply(
                update,
                "Chat ID faqat sonlardan iborat bo'lishi kerak. Misol: -100123456789",
                self._main_menu_markup(),
            )
            return

        try:
            chat = await context.bot.get_chat(chat_id)
        except Exception as exc:
            LOGGER.exception("Chat olishda xato: %s", exc)
            await self._edit_or_reply(
                update,
                "Guruh topilmadi. Bot ushbu guruhga a'zo ekanligini tekshiring.",
                self._main_menu_markup(),
            )
            return

        if chat.type not in ("group", "supergroup"):
            await self._edit_or_reply(
                update,
                "Faqat guruh yoki superguruhlarni qo'shish mumkin.",
                self._main_menu_markup(),
            )
            return

        # Check if bot is admin in the group
        try:
            member = await context.bot.get_chat_member(chat_id=chat.id, user_id=context.bot.id)
            if member.status not in ("administrator", "creator"):
                await self._edit_or_reply(
                    update,
                    "Xatolik: Avval botni o'sha guruhda admin qilishingiz kerak!",
                    self._main_menu_markup(),
                )
                return
        except Exception as exc:
            LOGGER.exception("Bot adminligini tekshirishda xato: %s", exc)
            await self._edit_or_reply(
                update,
                "Xatolik: Bot guruh a'zolarini tekshira olmadi. Bot adminligini tekshiring.",
                self._main_menu_markup(),
            )
            return

        added_by = update.effective_user.id
        title = chat.title or f"group-{chat.id}"
        upsert_group(chat_id=chat.id, title=title, added_by_user_id=added_by)
        await self._edit_or_reply(
            update,
            f"✅ Guruh muvaffaqiyatli saqlandi:\nNomi: {title}\nID: {chat.id}",
            self._main_menu_markup(),
        )

    async def del_group_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
            return
        if not await self._ensure_admin(update):
            return

        if not context.args:
            await self._edit_or_reply(
                update,
                "Foydalanish:\n/delgroup <chat_id>\n\nMisol: /delgroup -100123456789",
                self._main_menu_markup(),
            )
            return

        try:
            chat_id = int(context.args[0].strip())
        except ValueError:
            await self._edit_or_reply(
                update,
                "Chat ID faqat sonlardan iborat bo'lishi kerak. Misol: -100123456789",
                self._main_menu_markup(),
            )
            return

        deleted = deactivate_group_by_chat_id(chat_id)
        if deleted:
            await self._edit_or_reply(
                update,
                f"✅ Guruh o'chirildi (deaktivatsiya qilindi): {chat_id}",
                self._main_menu_markup(),
            )
        else:
            await self._edit_or_reply(
                update,
                f"Guruh ro'yxatda topilmadi: {chat_id}",
                self._main_menu_markup(),
            )

    async def groups_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_admin(update):
            return

        query = update.callback_query
        data = (query.data or "").split(":")
        action = data[1] if len(data) > 1 else ""

        # Clear wait state on other interactions
        if action != "add_prompt":
            context.user_data["waiting_for_group_input"] = False

        if action == "add_prompt":
            context.user_data["waiting_for_group_input"] = True
            await query.answer()
            await query.edit_message_text(
                "Guruh qo'shish uchun guruhning ID raqamini (masalan, `-100123456789`) yoki username'ini (masalan, `@guruh_nomi`) yuboring:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Bekor qilish", callback_data=f"{GROUP_PREFIX}:refresh")]]),
            )
            return

        elif action == "del" and len(data) == 3:
            group_id = int(data[2])
            deleted = deactivate_group(group_id)
            await query.answer("Guruh o'chirildi." if deleted else "Guruh topilmadi.")

        elif action == "refresh":
            await query.answer()
        else:
            await query.answer("Noma'lum amal.", show_alert=True)
            return

        chat = query.message.chat
        text, markup = self._render_groups_panel(
            chat_type=chat.type if chat else None,
            chat_id=chat.id if chat else None,
            chat_title=chat.title if chat else None,
        )
        await self._edit_or_reply(update, text, markup)

    async def handle_group_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_admin(update):
            return

        text = (update.message.text or "").strip()

        # Check if we are waiting for authentication token/code
        if not self.gmail.authenticated:
            code = self._extract_auth_code(text)
            if code:
                try:
                    await update.message.reply_text("⏳ Token tekshirilmoqda, iltimos kuting...")
                    # Exchange code for credentials
                    self.gmail.fetch_token_from_code(code)
                    
                    # Notify admin
                    await update.message.reply_text(
                        "✅ Gmail muvaffaqiyatli ulandi!\n\n"
                        "Bot endi Gmail'dan keladigan xatlarni kuzatishi mumkin.",
                        reply_markup=self._main_menu_markup()
                    )
                    return
                except Exception as exc:
                    LOGGER.exception("Token exchange error: %s", exc)
                    await update.message.reply_text(
                        f"❌ Xatolik: Tokenni olishda yoki ulanishda xato yuz berdi:\n`{exc}`\n\n"
                        "Iltimos, qaytadan havola ustiga bosing va yangi kodni yuboring.",
                        reply_markup=self._main_menu_markup()
                    )
                    return

        if not context.user_data.get("waiting_for_group_input"):
            return

        context.user_data["waiting_for_group_input"] = False
        if not text:
            await update.message.reply_text(
                "Noto'g'ri qiymat yuborildi.",
                reply_markup=self._main_menu_markup(),
            )
            return

        chat_id_or_username = text
        if text.startswith("-") or text.isdigit():
            try:
                chat_id_or_username = int(text)
            except ValueError:
                pass

        try:
            chat = await context.bot.get_chat(chat_id_or_username)
        except Exception as exc:
            LOGGER.exception("Guruh olishda xato: %s", exc)
            await update.message.reply_text(
                f"Guruh topilmadi: {text}\nBot ushbu guruhga qo'shilganini va to'g'ri yozilganini tekshiring.",
                reply_markup=self._main_menu_markup(),
            )
            return

        if chat.type not in ("group", "supergroup"):
            await update.message.reply_text(
                "Faqat guruh yoki superguruhlarni qo'shish mumkin.",
                reply_markup=self._main_menu_markup(),
            )
            return

        # Check if bot is admin in the group
        try:
            member = await context.bot.get_chat_member(chat_id=chat.id, user_id=context.bot.id)
            if member.status not in ("administrator", "creator"):
                await update.message.reply_text(
                    "Xatolik: Avval botni guruhda admin qilishingiz kerak!",
                    reply_markup=self._main_menu_markup(),
                )
                return
        except Exception as exc:
            LOGGER.exception("Bot adminligini tekshirishda xato: %s", exc)
            await update.message.reply_text(
                "Xatolik: Bot guruh a'zolarini tekshira olmadi. Bot adminligini tekshiring.",
                reply_markup=self._main_menu_markup(),
            )
            return

        added_by = update.effective_user.id
        title = chat.title or f"group-{chat.id}"
        upsert_group(chat_id=chat.id, title=title, added_by_user_id=added_by)
        await update.message.reply_text(
            f"✅ Guruh muvaffaqiyatli saqlandi:\nNomi: {title}\nID: {chat.id}",
            reply_markup=self._main_menu_markup(),
        )

    async def menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_admin(update):
            return

        query = update.callback_query
        parts = (query.data or "").split(":")
        action = parts[1] if len(parts) > 1 else "home"

        if action in ("home", "refresh"):
            await query.answer()
            await self._edit_or_reply(update, self._render_home_text(), self._main_menu_markup())
            return

        if action == "status":
            await query.answer()
            await self._edit_or_reply(update, self._render_status_text(), self._main_menu_markup())
            return

        if action == "help":
            await query.answer()
            await self._edit_or_reply(update, self._render_help_text(), self._main_menu_markup())
            return

        if action == "groups":
            await query.answer()
            chat = query.message.chat
            text, markup = self._render_groups_panel(
                chat_type=chat.type if chat else None,
                chat_id=chat.id if chat else None,
                chat_title=chat.title if chat else None,
            )
            await self._edit_or_reply(update, text, markup)
            return

        if action == "history":
            await query.answer()
            rows = (
                EmailHistory.select()
                .where(EmailHistory.forwarded == True)  # noqa: E712
                .order_by(EmailHistory.id.desc())
                .limit(5)
            )
            await self._edit_or_reply(update, self._render_history_text(rows, 5), self._history_markup(5, rows))
            return

        if action == "check":
            if not self.gmail.authenticated:
                await query.answer("Gmail ulanmagan!", show_alert=True)
                await self._edit_or_reply(
                    update,
                    "❌ Xatolik: Gmail hisobi ulanmagan. Avval botni Gmail'ga ulang.",
                    self._main_menu_markup(),
                )
                return
            await query.answer("Gmail tekshirilmoqda...")
            sent_count = await self.poll_once(context)
            text = (
                "Tekshiruv yakunlandi.\n"
                f"Yangi forward qilingan email soni: {sent_count}"
            )
            await self._edit_or_reply(update, text, self._main_menu_markup())
            return

        await query.answer("Noma'lum bo'lim.", show_alert=True)

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
            return
        if not await self._ensure_admin(update):
            return
        await self._edit_or_reply(update, self._render_status_text(), self._main_menu_markup())

    async def history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_group_or_admin(update, allowed_in_group=True):
            return

        is_group = update.effective_chat and update.effective_chat.type in ("group", "supergroup")
        limit = 5
        if context.args:
            try:
                limit = max(1, min(int(context.args[0]), 20))
            except ValueError:
                rows = self._get_history_rows(update.effective_chat, limit=5)
                await self._edit_or_reply(
                    update,
                    "Foydalanish: /history yoki /history 10",
                    self._history_markup(5, rows, is_group=is_group),
                )
                return

        rows = self._get_history_rows(update.effective_chat, limit=limit)
        await self._edit_or_reply(update, self._render_history_text(rows, limit), self._history_markup(limit, rows, is_group=is_group))

    async def history_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_group_or_admin(update, allowed_in_group=True):
            return

        query = update.callback_query
        data = (query.data or "").split(":")
        is_group = update.effective_chat and update.effective_chat.type in ("group", "supergroup")

        if len(data) == 3 and data[1] == "limit":
            try:
                limit = max(1, min(int(data[2]), 20))
            except ValueError:
                await query.answer("Noto'g'ri limit.", show_alert=True)
                return
            await query.answer()
            rows = self._get_history_rows(update.effective_chat, limit)
            await self._edit_or_reply(update, self._render_history_text(rows, limit), self._history_markup(limit, rows, is_group=is_group))
            return

        elif len(data) == 3 and data[1] == "list":
            try:
                limit = max(1, min(int(data[2]), 20))
            except ValueError:
                await query.answer("Noto'g'ri limit.", show_alert=True)
                return
            await query.answer()
            rows = self._get_history_rows(update.effective_chat, limit)
            await self._edit_or_reply(update, self._render_history_text(rows, limit), self._history_markup(limit, rows, is_group=is_group))
            return

        elif len(data) == 4 and data[1] == "view":
            try:
                email_id = int(data[2])
                limit = int(data[3])
            except ValueError:
                await query.answer("Noto'g'ri ID.", show_alert=True)
                return
            await query.answer()
            text, markup = await self._render_email_detail(email_id, limit, chat=update.effective_chat)
            await self._edit_or_reply(update, text, markup)
            return

        await query.answer("Noma'lum amal.", show_alert=True)

    async def check_now(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_group_or_admin(update, allowed_in_group=True):
            return
        if not self.gmail.authenticated:
            markup = None if update.effective_chat and update.effective_chat.type in ("group", "supergroup") else self._main_menu_markup()
            await self._edit_or_reply(
                update,
                "❌ Xatolik: Gmail hisobi ulanmagan. Avval botni Gmail'ga ulang.",
                markup,
            )
            return
        sent_count = await self.poll_once(context)
        markup = None if update.effective_chat and update.effective_chat.type in ("group", "supergroup") else self._main_menu_markup()
        await self._edit_or_reply(
            update,
            f"Tekshirildi. Yangi forward qilingan email soni: {sent_count}",
            markup,
        )

    async def job_poll(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.poll_once(context)

    async def poll_once(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        if not self.gmail.authenticated:
            return 0
        sender_filter = get_sender_filter(self.settings.sender_filter)
        subject_filter = (self.settings.subject_must_contain or "").strip().lower()
        groups = list_groups(active_only=True)
        if not sender_filter or not groups:
            return 0

        after_ts = get_last_checked_ts()
        max_seen = after_ts or 0
        forwarded_email_count = 0
        query_after_ts: Optional[int] = None
        if after_ts:
            query_after_ts = max(after_ts - 2, 0)

        try:
            message_ids = self.gmail.list_message_ids(
                sender_filter=sender_filter,
                after_ts_seconds=query_after_ts,
                extra_query=self.settings.gmail_query_extra,
                max_results=30,
            )
        except Exception as exc:  # pragma: no cover
            LOGGER.exception("Gmail list xatosi: %s", exc)
            return 0

        for message_id in reversed(message_ids):
            if EmailHistory.get_or_none(EmailHistory.gmail_message_id == message_id):
                continue

            try:
                msg = self.gmail.get_message(message_id)
            except Exception as exc:  # pragma: no cover
                LOGGER.exception("Message olishda xato (%s): %s", message_id, exc)
                continue

            ts_seconds = int(msg.internal_date.timestamp())
            max_seen = max(max_seen, ts_seconds)

            if msg.sender_email != sender_filter.lower():
                continue
            if subject_filter and subject_filter not in (msg.subject or "").lower():
                continue

            row = EmailHistory.create(
                gmail_message_id=msg.message_id,
                gmail_thread_id=msg.thread_id,
                sender=msg.sender_raw or msg.sender_email,
                subject=msg.subject,
                snippet=msg.snippet,
                internal_date=msg.internal_date,
            )
            forward_text = _format_email_message(row, msg.body_text or msg.snippet)

            delivery_success = 0
            first_chat_id: Optional[int] = None
            first_message_id: Optional[int] = None

            for group in groups:
                try:
                    sent = await context.bot.send_message(
                        chat_id=group.chat_id,
                        text=forward_text,
                        disable_web_page_preview=True,
                    )
                    EmailDelivery.create(
                        email=row,
                        group=group,
                        delivered=True,
                        telegram_message_id=sent.message_id,
                    )
                    delivery_success += 1
                    if first_chat_id is None:
                        first_chat_id = group.chat_id
                        first_message_id = sent.message_id
                except Exception as exc:  # pragma: no cover
                    LOGGER.exception(
                        "Telegramga yuborishda xato (%s -> %s): %s",
                        message_id,
                        group.chat_id,
                        exc,
                    )
                    EmailDelivery.create(
                        email=row,
                        group=group,
                        delivered=False,
                        error_text=str(exc)[:1000],
                    )

            if delivery_success > 0:
                row.forwarded = True
                row.telegram_chat_id = first_chat_id
                row.telegram_message_id = first_message_id
                row.save()
                forwarded_email_count += 1

        if max_seen:
            set_last_checked_ts(max_seen)
        return forwarded_email_count

    def build_application(self) -> Application:
        app = Application.builder().token(self.settings.bot_token).post_init(self.post_init).build()

        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("menu", self.menu))
        app.add_handler(CommandHandler("help", self.help_cmd))
        app.add_handler(CommandHandler("setsender", self.set_sender))
        app.add_handler(CommandHandler("groups", self.groups))
        app.add_handler(CommandHandler("addgroup", self.add_group_cmd))
        app.add_handler(CommandHandler("delgroup", self.del_group_cmd))
        app.add_handler(CommandHandler("status", self.status))
        app.add_handler(CommandHandler("history", self.history))
        app.add_handler(CommandHandler("checknow", self.check_now))
        app.add_handler(CallbackQueryHandler(self.menu_callback, pattern=rf"^{MENU_PREFIX}:"))
        app.add_handler(CallbackQueryHandler(self.groups_callback, pattern=rf"^{GROUP_PREFIX}:"))
        app.add_handler(CallbackQueryHandler(self.history_callback, pattern=rf"^{HISTORY_PREFIX}:"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_group_input))

        app.job_queue.run_repeating(self.job_poll, interval=self.settings.poll_interval_seconds, first=3)
        return app
