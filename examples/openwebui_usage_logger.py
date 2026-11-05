"""
title: Open WebUI Usage Logger
author: Sushant
version: 0.2
description: Log provider calls and include stateless API completions in Analytics.
"""

from datetime import datetime, timezone
import logging
from typing import Optional
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, Field


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=0, description="Priority level for the filter.")

    def __init__(self):
        self.valves = self.Valves()
        self.logger = logging.getLogger("openwebui_usage")
        self.logger.setLevel(logging.INFO)

    async def request(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        __model__: Optional[dict] = None,
        __request__=None,
    ) -> dict:
        user = __user__ or {}
        metadata = __metadata__ or {}
        model = body.get("model", "unknown")
        base_model = model
        if __model__:
            info = __model__.get("info") or {}
            base_model = info.get("base_model_id") or __model__.get("base_model_id") or model

        chat_id = metadata.get("chat_id")
        session_id = metadata.get("session_id")
        source = "webui" if chat_id else "api"

        method = getattr(__request__, "method", None)
        url = getattr(__request__, "url", None)
        path = getattr(url, "path", None)
        client = getattr(__request__, "client", None)
        client_ip = getattr(client, "host", None)

        self.logger.info(
            "LLM_USAGE user_id=%s email=%s name=%s role=%s source=%s "
            "model=%s base_model=%s method=%s path=%s ip=%s "
            "chat_id=%s session_id=%s",
            user.get("id", "unknown"),
            user.get("email", "unknown"),
            user.get("name", "unknown"),
            user.get("role", "unknown"),
            source,
            model,
            base_model,
            method or "-",
            path or "-",
            client_ip or "-",
            chat_id or "-",
            session_id or "-",
        )
        return body

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
        __request__=None,
    ) -> dict:
        """Persist a usage-only row so built-in Analytics counts stateless API replies."""
        metadata = __metadata__ or {}
        user_id = (__user__ or {}).get("id")
        path = getattr(getattr(__request__, "url", None), "path", None)

        # Saved chats are already dual-written by Open WebUI. Internal model
        # calls are not a user API completion. Never copy prompt or reply text.
        if (
            metadata.get("chat_id")
            or not user_id
            or path not in {"/api/chat/completions", "/api/v1/chat/completions", "/v1/chat/completions"}
            or getattr(getattr(__request__, "state", None), "internal", False)
        ):
            return body

        assistant = next(
            (m for m in reversed(body.get("messages") or []) if m.get("role") == "assistant"),
            None,
        )
        if not assistant:
            return body

        usage = assistant.get("usage") or (assistant.get("info") or {}).get("usage")
        model_id = body.get("model")
        if not model_id:
            return body

        try:
            from open_webui.models.chats import ChatForm, Chats
            from open_webui.models.chat_messages import ChatMessages

            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            chat_id = str(uuid5(NAMESPACE_URL, f"openwebui-api-usage:{user_id}:{day}"))
            chat = await Chats.get_chat_by_id(chat_id)
            if chat is None:
                try:
                    chat = await Chats.insert_new_chat(
                        chat_id,
                        user_id,
                        ChatForm(chat={"title": f"API usage {day}", "history": {"messages": {}}}),
                        internal_meta={"api_usage": True},
                    )
                except Exception:
                    # Another worker may have created the daily chat first.
                    chat = await Chats.get_chat_by_id(chat_id)
                if chat and not chat.archived:
                    await Chats.toggle_chat_archive_by_id(chat_id)

            if not chat:
                raise RuntimeError("could not create API usage chat")

            await ChatMessages.upsert_message(
                message_id=f"api-{uuid4().hex}",
                chat_id=chat_id,
                user_id=user_id,
                data={
                    "role": "assistant",
                    "content": "",
                    "model": model_id,
                    "usage": usage,
                    "meta": {"source": "api"},
                    "done": True,
                },
            )
            self.logger.info("API_ANALYTICS_SAVED user_id=%s model=%s", user_id, model_id)
        except Exception:
            self.logger.exception("API_ANALYTICS_SAVE_FAILED user_id=%s model=%s", user_id, model_id)

        return body
