"""Conversational profile-building agent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.profile_agent_prompt import PROFILE_AGENT_SYSTEM_PROMPT
from src.agents.profile_agent_tools import PROFILE_AGENT_TOOLS
from src.db.models import (
    ChatContext,
    ChatMessage,
    ChatRole,
    Profile,
    User,
    UserState,
)
from src.db.session import async_session
from src.services.claude import (
    ClaudeService,
    build_text_block,
    build_tool_result_block,
)

MAX_HISTORY_MESSAGES = 20
MAX_TOOL_ITERATIONS = 10

KICKOFF_MESSAGE = (
    "Окей, давай соберём (или дополним) твой профиль. Я буду задавать "
    "открытые вопросы — отвечай так, как удобно, текстом или голосом.\n\n"
    "Если есть актуальное резюме в PDF — пришли его в любой момент, я разберу.\n\n"
    "Поехали 👇"
)

KICKOFF_AGENT_INSTRUCTION = (
    "Сейчас юзер запустил /edit_profile. Сначала вызови get_current_profile, "
    "чтобы понять, что уже собрано. Потом задай ОДИН следующий уместный вопрос: "
    "если профиль пустой — открывающий вопрос про идеальную работу из шага 1. "
    "Если что-то уже есть — продолжи с того места, на котором логично, не "
    "повторяя вопросы, на которые ответы уже есть. Никаких приветствий — "
    "юзеру только что отправили универсальное приветствие, ты — сразу к делу."
)

FALLBACK_ERROR_MESSAGE = (
    "Что-то пошло не так на моей стороне. Попробуй ещё раз через /edit_profile."
)


@dataclass
class AgentReply:
    """Result of one agent turn."""

    text: str
    finalized: bool
    summary: str | None = None


class ProfileAgent:
    """Conversational agent that fills out a user's career profile."""

    def __init__(self, claude: ClaudeService):
        self.claude = claude

    # ----- Public API -----

    async def start_editing(self, user_id: int) -> tuple[str, AgentReply]:
        """Begin a profile-editing session.

        Returns:
            kickoff_text — universal greeting to send first
            agent_reply  — first contextual question from the agent
        """
        async with async_session() as session:
            db_user = await session.get(User, user_id)
            db_user.state = UserState.editing_profile
            await self._save_message(session, user_id, ChatRole.assistant, KICKOFF_MESSAGE)
            await session.commit()

        first_question = await self.handle_message(
            user_id=user_id,
            user_text=KICKOFF_AGENT_INSTRUCTION,
            persist_user_message=False,
        )
        return KICKOFF_MESSAGE, first_question

    async def handle_message(
        self,
        user_id: int,
        user_text: str,
        extra_content_blocks: list[dict[str, Any]] | None = None,
        persist_user_message: bool = True,
    ) -> AgentReply:
        """Process a user message and return agent reply."""
        async with async_session() as session:
            history = await self._load_history(session, user_id)
            if persist_user_message:
                await self._save_message(session, user_id, ChatRole.user, user_text)
                

            if extra_content_blocks:
                user_content: Any = [*extra_content_blocks, build_text_block(user_text)]
            else:
                user_content = user_text
            messages = [*history, {"role": "user", "content": user_content}]

            final_text, finalized, summary = await self._run_tool_loop(
                session, user_id, messages
            )

            await self._save_message(session, user_id, ChatRole.assistant, final_text)

            if finalized:
                db_user = await session.get(User, user_id)
                db_user.state = UserState.idle

            await session.commit()

        return AgentReply(text=final_text, finalized=finalized, summary=summary)

    # ----- Tool loop -----

    async def _run_tool_loop(
        self,
        session: AsyncSession,
        user_id: int,
        messages: list[dict[str, Any]],
    ) -> tuple[str, bool, str | None]:
        finalized = False
        summary: str | None = None
        last_text: str = ""

        for _ in range(MAX_TOOL_ITERATIONS):
            response = await self.claude.chat(
                messages=messages,
                system=PROFILE_AGENT_SYSTEM_PROMPT,
                tools=PROFILE_AGENT_TOOLS,
            )
            messages.append({"role": "assistant", "content": response.raw_content})

            if response.text:
                last_text = response.text

            if not response.tool_uses:
                final_text = last_text or summary or FALLBACK_ERROR_MESSAGE
                return final_text, finalized, summary

            tool_result_blocks = []
            for tool_use in response.tool_uses:
                result_str, did_finalize, sum_text = await self._execute_tool(
                    session, user_id, tool_use["name"], tool_use["input"]
                )
                tool_result_blocks.append(
                    build_tool_result_block(tool_use["id"], result_str)
                )
                if did_finalize:
                    finalized = True
                    summary = sum_text

            messages.append({"role": "user", "content": tool_result_blocks})

        return last_text or summary or FALLBACK_ERROR_MESSAGE, finalized, summary

    async def _execute_tool(
        self,
        session: AsyncSession,
        user_id: int,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> tuple[str, bool, str | None]:
        """Execute a single tool call. Returns (result_str, finalize_flag, summary)."""
        if tool_name == "get_current_profile":
            profile = await self._get_profile(session, user_id)
            return json.dumps(profile.profile_data or {}, ensure_ascii=False), False, None

        if tool_name == "update_profile_field":
            field = tool_input["field"]
            value = tool_input["value"]
            profile = await self._get_profile(session, user_id)
            data = dict(profile.profile_data or {})
            data[field] = value
            profile.profile_data = data
            await session.flush()
            return f"Поле '{field}' обновлено.", False, None

        if tool_name == "add_cv_source":
            filename = tool_input["filename"]
            summary_extracted = tool_input["summary_extracted"]
            profile = await self._get_profile(session, user_id)
            data = dict(profile.profile_data or {})
            sources = list(data.get("cv_sources") or [])
            sources.append(
                {
                    "filename": filename,
                    "uploaded_at": datetime.now(timezone.utc).isoformat(),
                    "summary_extracted": summary_extracted,
                }
            )
            data["cv_sources"] = sources
            profile.profile_data = data
            await session.flush()
            return f"Резюме '{filename}' добавлено в профиль.", False, None

        if tool_name == "finalize_editing":
            summary_text = tool_input.get("summary", "")
            return "Сессия редактирования завершена.", True, summary_text

        return f"Unknown tool: {tool_name}", False, None

    # ----- Helpers -----

    async def _get_profile(self, session: AsyncSession, user_id: int) -> Profile:
        result = await session.execute(
            select(Profile).where(Profile.user_id == user_id)
        )
        profile = result.scalar_one_or_none()
        if profile is None:
            profile = Profile(user_id=user_id, profile_data={})
            session.add(profile)
            await session.flush()
        return profile

    async def _load_history(
        self,
        session: AsyncSession,
        user_id: int,
    ) -> list[dict[str, Any]]:
        result = await session.execute(
            select(ChatMessage)
            .where(
                ChatMessage.user_id == user_id,
                ChatMessage.context == ChatContext.profile_edit,
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(MAX_HISTORY_MESSAGES)
        )
        messages = list(reversed(result.scalars().all()))
        return [{"role": m.role.value, "content": m.content} for m in messages]

    async def _save_message(
        self,
        session: AsyncSession,
        user_id: int,
        role: ChatRole,
        content: str,
    ) -> None:
        msg = ChatMessage(
            user_id=user_id,
            role=role,
            content=content,
            context=ChatContext.profile_edit,
        )
        session.add(msg)