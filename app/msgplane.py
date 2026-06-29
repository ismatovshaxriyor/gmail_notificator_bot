import asyncio
import json
import logging
import ssl
import urllib.parse
import urllib.request
from typing import Optional

LOGGER = logging.getLogger(__name__)

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _fetch_sync(api_key: str, order_id: str, api_url: str) -> Optional[str]:
    payload = urllib.parse.urlencode(
        {"subaction": "get", "api_key": api_key, "record": order_id}
    ).encode()
    req = urllib.request.Request(api_url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("result") == "success":
                return data.get("user_name")
            LOGGER.warning("MsgPlane API muvaffaqiyatsiz (%s): %s", order_id, data)
    except Exception as exc:
        LOGGER.warning("MsgPlane API xatosi (%s): %s", order_id, exc)
    return None


async def get_agent_name(api_key: str, order_id: str, api_url: str) -> Optional[str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_sync, api_key, order_id, api_url)
