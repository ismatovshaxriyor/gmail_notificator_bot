import argparse
import asyncio
import logging

from app.bot import GmailForwardBot
from app.config import load_settings
from app.db import init_db, upsert_group
from app.gmail_service import GmailService


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gmail -> Telegram forward bot")
    parser.add_argument(
        "--auth-only",
        action="store_true",
        help="Faqat Gmail OAuth autentifikatsiyasini bajaraydi va chiqadi.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings()
    init_db(settings.db_path)

    gmail = GmailService(
        credentials_file=settings.gmail_credentials_file,
        token_file=settings.gmail_token_file,
        interactive=args.auth_only,
    )

    if gmail.authenticated:
        try:
            profile = gmail.check_connection()
            logging.info("Gmail ulandi: %s", profile.get("emailAddress"))
        except Exception as exc:
            logging.warning("Gmail ulanish tekshiruvi muvaffaqiyatsiz (bot ishini davom ettiradi): %s", exc)
    else:
        logging.warning("Gmail hozircha ulanmagan (token.json yo'q). Bot ishga tushmoqda, lekin Gmail so'rovlari ishlamaydi.")

    if args.auth_only:
        if gmail.authenticated:
            logging.info("Auth muvaffaqiyatli tugadi.")
            try:
                from telegram import Bot
                async def notify_admins():
                    bot = Bot(token=settings.bot_token)
                    for admin_id in settings.admin_user_ids:
                        try:
                            await bot.send_message(
                                chat_id=admin_id,
                                text="✅ Gmail hisobi terminal orqali muvaffaqiyatli ulandi!\n\nEndi botni oddiy rejimda ishga tushirishingiz mumkin:\n`python main.py`"
                            )
                        except Exception as e:
                            logging.error("Admin %s ga xabar yuborishda xato: %s", admin_id, e)
                
                asyncio.run(notify_admins())
            except Exception as e:
                logging.error("Adminlarga Telegram xabari yuborishda xato: %s", e)
        else:
            logging.error("Auth muvaffaqiyatsiz tugadi.")
        return

    if settings.target_chat_id is not None:
        upsert_group(
            chat_id=settings.target_chat_id,
            title=f"env-target-{settings.target_chat_id}",
            added_by_user_id=0,
        )
        logging.info("TARGET_CHAT_ID orqali guruh bootstrap qilindi: %s", settings.target_chat_id)

    runner = GmailForwardBot(settings=settings, gmail=gmail)
    app = runner.build_application()
    # Python 3.14 da default event loop avtomatik yaratilmaydi.
    # python-telegram-bot run_polling ichida get_event_loop ishlatgani uchun
    # loopni qo'lda ulab olamiz.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
