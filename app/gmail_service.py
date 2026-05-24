import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
import html as html_lib
from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass
class GmailMessage:
    message_id: str
    thread_id: str
    sender_email: str
    sender_raw: str
    subject: str
    snippet: str
    body_text: str
    internal_date: datetime


class GmailService:
    def __init__(self, credentials_file: str, token_file: str, interactive: bool = False) -> None:
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.flow = None
        self._service = None
        self.authenticated = False

        creds = self._load_credentials(interactive=interactive)
        if creds:
            self._service = build(
                "gmail",
                "v1",
                credentials=creds,
                cache_discovery=False,
            )
            self.authenticated = True

    def _load_credentials(self, interactive: bool = False) -> Optional[Credentials]:
        token_path = Path(self.token_file)
        creds: Optional[Credentials] = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_credentials(creds)
                return creds
            except Exception:
                pass

        if not interactive:
            return None

        credentials_path = Path(self.credentials_file)
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"{self.credentials_file} topilmadi. Google OAuth client faylini shu nomda qo'ying."
            )

        flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
        creds = flow.run_local_server(port=0, open_browser=False)
        self._save_credentials(creds)
        return creds

    def generate_auth_url(self) -> Tuple[str, str]:
        self.flow = InstalledAppFlow.from_client_secrets_file(
            self.credentials_file,
            SCOPES,
            redirect_uri="http://localhost:8080/"
        )
        authorization_url, state = self.flow.authorization_url(prompt="consent", access_type="offline")
        return authorization_url, state

    def fetch_token_from_code(self, code: str) -> Credentials:
        if not self.flow:
            self.flow = InstalledAppFlow.from_client_secrets_file(
                self.credentials_file,
                SCOPES,
                redirect_uri="http://localhost:8080/"
            )
        self.flow.fetch_token(code=code)
        creds = self.flow.credentials
        self._save_credentials(creds)
        self._service = build(
            "gmail",
            "v1",
            credentials=creds,
            cache_discovery=False,
        )
        self.authenticated = True
        return creds

    def _save_credentials(self, creds: Credentials) -> None:
        Path(self.token_file).write_text(creds.to_json(), encoding="utf-8")

    def check_connection(self) -> Dict:
        return self._service.users().getProfile(userId="me").execute()

    def list_message_ids(
        self,
        sender_filter: str,
        after_ts_seconds: Optional[int],
        extra_query: str = "",
        max_results: int = 25,
    ) -> List[str]:
        query_parts = ["in:inbox"]
        if sender_filter:
            query_parts.append(f"from:{sender_filter}")
        if after_ts_seconds:
            query_parts.append(f"after:{after_ts_seconds}")
        if extra_query:
            query_parts.append(extra_query)
        query = " ".join(query_parts).strip()

        response = (
            self._service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        items = response.get("messages", [])
        return [item["id"] for item in items]

    def get_message(self, message_id: str) -> GmailMessage:
        raw = (
            self._service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="full",
            )
            .execute()
        )

        payload = raw.get("payload", {})
        headers = payload.get("headers", [])
        header_map = {h.get("name", "").lower(): h.get("value", "") for h in headers}

        sender_raw = header_map.get("from", "")
        sender_email = parseaddr(sender_raw)[1].lower().strip()

        internal_date_ms = int(raw.get("internalDate", "0"))
        internal_date = datetime.fromtimestamp(
            internal_date_ms / 1000,
            tz=timezone.utc,
        )
        body_text = self._extract_body_text(payload)
        if not body_text:
            body_text = raw.get("snippet", "")

        return GmailMessage(
            message_id=raw["id"],
            thread_id=raw.get("threadId", ""),
            sender_email=sender_email,
            sender_raw=sender_raw,
            subject=header_map.get("subject", "(No subject)"),
            snippet=raw.get("snippet", ""),
            body_text=body_text,
            internal_date=internal_date,
        )

    @staticmethod
    def _iter_parts(payload: dict) -> List[dict]:
        stack = [payload]
        out: List[dict] = []
        while stack:
            node = stack.pop()
            out.append(node)
            for part in node.get("parts", []):
                stack.append(part)
        return out

    @staticmethod
    def _decode_body_data(data: str) -> str:
        if not data:
            return ""
        pad = "=" * (-len(data) % 4)
        decoded = base64.urlsafe_b64decode(data + pad)
        return decoded.decode("utf-8", errors="replace")

    @staticmethod
    def _html_to_text(html_text: str) -> str:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html_text)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(p|div|li|tr|h1|h2|h3|h4|h5|h6)>", "\n", text)
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = html_lib.unescape(text)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\r", "\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _extract_body_text(self, payload: dict) -> str:
        plain_parts: List[str] = []
        html_parts: List[str] = []
        for part in self._iter_parts(payload):
            mime = (part.get("mimeType") or "").lower()
            body = part.get("body", {})
            data = body.get("data")
            if not data:
                continue
            chunk = self._decode_body_data(data)
            if mime == "text/plain":
                plain_parts.append(chunk)
            elif mime == "text/html":
                html_parts.append(chunk)

        if plain_parts:
            text = "\n".join(p.strip() for p in plain_parts if p.strip()).strip()
            if text:
                return text

        if html_parts:
            html_joined = "\n".join(h for h in html_parts if h)
            return self._html_to_text(html_joined)

        body_data = payload.get("body", {}).get("data", "")
        if body_data:
            raw_text = self._decode_body_data(body_data)
            if "<html" in raw_text.lower():
                return self._html_to_text(raw_text)
            return raw_text.strip()
        return ""
