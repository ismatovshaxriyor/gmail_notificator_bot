import logging
from typing import Optional

import aiohttp

LOGGER = logging.getLogger(__name__)


async def get_agent_name(api_key: str, order_id: str, api_url: str) -> Optional[str]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                api_url,
                data={"subaction": "get", "api_key": api_key, "record": order_id},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
                if data.get("result") == "success":
                    return data.get("user_name")
                LOGGER.warning("MsgPlane API muvaffaqiyatsiz (%s): %s", order_id, data)
    except Exception as exc:
        LOGGER.warning("MsgPlane API xatosi (%s): %s", order_id, exc)
    return None
