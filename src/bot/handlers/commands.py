"""Bot command handlers."""

import asyncio
import html as html_module

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select
from src.agents.profile_agent import ProfileAgent
from src.services.claude import ClaudeService
from src.services.source_provisioning import provision_default_sources
from src.config import settings

from src.db.models import Profile, User, UserState, UserReaction
from src.db.session import async_session

router = Router()


WELCOME_TEXT = (
    'Привет! Это <a href="https://t.me/puniapple_speaks">Ульяна</a>. '
    'Я создала этого бота, потому что устала искать вакансию мечты на '
    'кладбище рынка труда.\n\n'
    'Что он умеет:\n\n'
    '🎯 <b>Понимает тебя глубже резюме.</b> Через короткий разговор разбирается, '
    'к чему лежит душа — даже если ты сам не уверен, кем хочешь быть дальше. '
    'Биздев, продукт, EdTech, M&A — бот ловит суть, а не просто ключевые слова.\n\n'
    '🔍 <b>Сам обходит источники.</b> Каждые 3 часа проверяет авторские карьерные '
    'тг-каналы и страницы топ-компаний (не использует агрегаторы — там слишком много мусора). '
    'Через AI оценивает каждую вакансию под твой профиль и присылает только релевантные.\n\n'
    '🎙 <b>Общайся как удобно.</b> Можно писать текстом, наговаривать голосовыми, '
    'присылать резюме в PDF — бот всё разберёт и вытащит фактологию сам.\n\n'
    '<b>С чего начать:</b> жми /edit_profile — соберём твой профиль через диалог.\n\n'
    '<b>Все команды:</b>\n'
    '/edit_profile — собрать или обновить профиль\n'
    '/show_profile — показать текущий профиль\n'
    '/done — выйти из режима редактирования\n'
    '/run_now — запустить поиск прямо сейчас\n'
    '/stats — твоя статистика\n'
    '/pause — поставить на паузу\n'
    '/resume — возобновить\n\n'
    'С багами и любым фидбэком — ко мне в личку <a href="https://t.me/puniapple">@puniapple</a>'
)


async def get_or_create_user(telegram_id: int, telegram_username: str | None) -> User:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        is_new = False
        if user is None:
            user = User(telegram_id=telegram_id, telegram_username=telegram_username)
            session.add(user)
            await session.flush()
            profile = Profile(user_id=user.id, profile_data={})
            session.add(profile)
            is_new = True

        # Provision default sources (idempotent — safe to call for existing users)
        await provision_default_sources(session, user.id)

        await session.commit()
        await session.refresh(user)
        return user


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    # Гейт: проверяем подписку на канал
    if is_required_channel_configured():
        subscribed = await is_subscribed(message.bot, message.from_user.id)
        if not subscribed:
            await _send_subscription_gate(message)
            return

    # Обычный flow
    await message.answer(WELCOME_TEXT, parse_mode="HTML", disable_web_page_preview=True)
    asyncio.create_task(
        get_or_create_user(
            telegram_id=message.from_user.id,
            telegram_username=message.from_user.username,
        )
    )

@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(WELCOME_TEXT, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("show_profile"))
async def cmd_show_profile(message: Message) -> None:
    """Показать текущее состояние профиля, даже если он не закрыт."""
    import json
    def _escape_html(text: str) -> str:
        """Экранирует HTML спецсимволы."""
        if not text:
            return ""
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )).scalar_one_or_none()
        if user is None:
            await message.answer("Сначала /start.")
            return

        profile = (await session.execute(
            select(Profile).where(Profile.user_id == user.id)
        )).scalar_one_or_none()

    if profile is None or not profile.profile_data:
        await message.answer(
            "Профиль пустой. Жми /edit_profile, чтобы начать собирать."
        )
        return

    pd = profile.profile_data
    ready_emoji = "🚀" if user.profile_ready_for_search else "⏸"
    status = "поиск активен" if user.profile_ready_for_search else "поиск ещё не запущен"

    parts = [f"{ready_emoji} Статус: {status}\n"]

    # Дружелюбное отображение основных полей, если они заполнены
    fields_map = [
        ("expertise", "Экспертиза"),
        ("current_role_summary", "Текущая роль"),
        ("ideal_work_description", "Идеальная работа"),
        ("interests_and_resonance", "Резонирует"),
        ("target_roles", "Целевые роли"),
        ("anti_roles", "Anti-roles"),
        ("industries_interested", "Интересные индустрии"),
        ("industries_avoid", "Избегаемые индустрии"),
        ("seniority", "Уровень"),
        ("languages", "Языки"),
        ("must_haves", "Must haves"),
        ("deal_breakers", "Deal-breakers"),
    ]

    for key, label in fields_map:
        value = pd.get(key)
        if not value:
            continue
        if isinstance(value, list):
            display = ", ".join(str(v).strip() for v in value if str(v).strip())
        else:
            # Строка — отображаем как есть, без лишней манипуляции
            display = str(value).strip().lstrip(",").strip()
        if len(display) > 300:
            display = display[:300] + "…"
        parts.append(f"<b>{label}</b>: {_escape_html(display)}")
        parts.append("")

    # Локация и формат отдельно
    lp = pd.get("location_preferences")
    if isinstance(lp, dict):
        loc_bits = []
        if lp.get("cities"):
            loc_bits.append(", ".join(lp["cities"]))
        if lp.get("countries"):
            loc_bits.append(", ".join(lp["countries"]))
        if lp.get("remote_ok"):
            loc_bits.append("remote ok")
        if loc_bits:
            parts.append(f"<b>Локация</b>: {_escape_html(' | '.join(loc_bits))}")
            parts.append("")

    fmt = pd.get("format")
    if fmt:
        parts.append(f"<b>Формат</b>: {_escape_html(', '.join(fmt) if isinstance(fmt, list) else fmt)}")
        parts.append("")

    comp = pd.get("compensation")
    if isinstance(comp, dict):
        comp_bits = []
        if comp.get("min_monthly"):
            comp_bits.append(f"мин: {comp['min_monthly']:,}")
        if comp.get("comfortable_monthly"):
            comp_bits.append(f"комфортно: {comp['comfortable_monthly']:,}")
        if comp.get("currency"):
            comp_bits.append(comp["currency"])
        if comp_bits:
            parts.append(f"<b>Деньги</b>: {_escape_html(' '.join(comp_bits))}")
            parts.append("")
    # CV
    cv_sources = pd.get("cv_sources") or []
    if cv_sources:
        parts.append(f"<b>Резюме</b>: загружено ({len(cv_sources)} файл(а/ов))")

    parts.append("")
    parts.append("Хочешь дополнить — /edit_profile")
    if not user.profile_ready_for_search:
        parts.append("Запустить поиск — /run_now")

    text = "\n".join(parts)
    if len(text) > 3900:
        text = text[:3900] + "…"

    await message.answer(text, parse_mode="HTML")

@router.message(Command("edit_profile"))
async def cmd_edit_profile(message: Message) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            await message.answer("Сначала напиши /start.")
            return

    claude = ClaudeService()
    agent = ProfileAgent(claude=claude)

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    kickoff_text, first_reply = await agent.start_editing(user_id=user.id)
    await message.answer(kickoff_text)
    await message.answer(first_reply.text)

@router.message(Command("done"))
async def cmd_done(message: Message) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            await message.answer("Сначала напиши /start.")
            return
        if user.state == UserState.editing_profile:
            user.state = UserState.idle
            await session.commit()
            await message.answer(
                "Окей, вышла из режима редактирования. /show_profile — посмотреть, "
                "что собрали. /edit_profile — продолжить позже."
            )
        else:
            await message.answer("Ты сейчас не в режиме редактирования — нечего завершать.")

@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one_or_none()
        if user:
            user.state = UserState.paused
            user.is_active = False
            await session.commit()
    await message.answer("Поставила на паузу. /resume — возобновить.")


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one_or_none()
        if user:
            user.state = UserState.idle
            user.is_active = True
            await session.commit()
    await message.answer("Возобновила. Жди вакансий ✨")


def _escape_md(text: str) -> str:
    """Escape MarkdownV2 special characters in user content."""
    chars = r"_*[]()~`>#+-=|{}.!\\"
    result = []
    for ch in str(text):
        if ch in chars:
            result.append("\\" + ch)
        else:
            result.append(ch)
    return "".join(result)


def format_profile(data: dict) -> str:
    """Render profile_data as a human-readable Russian message (MarkdownV2)."""
    if not data:
        return "Профиль пуст\\."

    labels = {
        "ideal_work_description": "🎯 Идеальная работа",
        "expertise": "💼 Экспертиза",
        "current_role_summary": "📍 Сейчас",
        "interests_and_resonance": "✨ К чему лежит душа",
        "target_roles": "🎯 Целевые роли",
        "anti_roles": "🚫 Не хочу",
        "industries_interested": "🏭 Интересные индустрии",
        "industries_avoid": "❌ Индустрии — нет",
        "location_preferences": "📍 Локация",
        "format": "🕐 Формат",
        "compensation": "💰 Компенсация",
        "languages": "🗣 Языки",
        "seniority": "📊 Уровень",
        "must_haves": "✅ Обязательно должно быть",
        "deal_breakers": "⛔ Точно нет",
        "free_form_notes": "📝 Заметки",
    }

    parts = ["*Твой профиль*", ""]
    for key, label in labels.items():
        value = data.get(key)
        if not value:
            continue
        if isinstance(value, list):
            rendered = ", ".join(str(v) for v in value)
        elif isinstance(value, dict):
            rendered = ", ".join(f"{k}: {v}" for k, v in value.items() if v)
        else:
            rendered = str(value)
        parts.append(f"*{_escape_md(label)}*")
        parts.append(_escape_md(rendered))
        parts.append("")

    cv_sources = data.get("cv_sources") or []
    if cv_sources:
        parts.append(f"*📄 Загруженные резюме \\({len(cv_sources)}\\)*")
        for cv in cv_sources:
            parts.append("• " + _escape_md(cv.get("filename", "без названия")))

    return "\n".join(parts)

@router.message(Command("run_now"))
async def cmd_run_now(message: Message) -> None:
    """Manually trigger a job search cycle for this user only.

    Useful for testing and on-demand fetch.
    """
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()
    if user is None:
        await message.answer("Сначала напиши /start.")
        return

    @router.message(Command("run_now"))
    async def cmd_run_now(message: Message) -> None:
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == message.from_user.id)
            )
            user = result.scalar_one_or_none()
        if user is None:
            await message.answer("Сначала напиши /start.")
            return

    # Проверка подписки
    if is_required_channel_configured():
        subscribed = await is_subscribed(message.bot, message.from_user.id)
        if not subscribed:
            await _send_subscription_gate(message)
            return

    await message.answer("🔄 Запускаю поиск... минут пять займёт.")
    # ... остальной код без изменений
    

    from src.workers.job_search import _process_user
    try:
        result = await _process_user(message.bot, user)
        await message.answer(
            f"Готово!\n"
            f"Источников проверено: всё активное\n"
            f"Свежих вакансий найдено: {result['fetched']}\n"
            f"Прогнано через matcher: {result['matched']}\n"
            f"Отправлено тебе: {result['delivered']}"
        )
    except Exception as e:
        await message.answer(f"Ошибка во время поиска: {e}")


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Personal stats: how many vacancies delivered, reactions breakdown."""
    from src.db.models import VacancyMatch

    async with async_session() as session:
        user_result = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            await message.answer("Сначала напиши /start.")
            return

        match_result = await session.execute(
            select(VacancyMatch).where(VacancyMatch.user_id == user.id)
        )
        matches = list(match_result.scalars())

    if not matches:
        await message.answer("Пока ни одной вакансии не присылала.")
        return

    total = len(matches)
    liked = sum(1 for m in matches if m.user_reaction == UserReaction.liked)
    disliked = sum(1 for m in matches if m.user_reaction == UserReaction.disliked)
    applied = sum(1 for m in matches if m.user_reaction == UserReaction.applied)
    no_reaction = total - liked - disliked - applied
    avg_score = sum(m.match_score for m in matches) / total

    await message.answer(
        f"📊 Твоя статистика\n\n"
        f"Всего получено: {total}\n"
        f"Средний score: {avg_score:.1f}\n\n"
        f"👍 интересно: {liked}\n"
        f"📨 откликнулась: {applied}\n"
        f"👎 не моё: {disliked}\n"
        f"⏳ без реакции: {no_reaction}"
    )

# ════════════════════════════════════════════════════════════════════
# Admin commands (limited to ADMIN_TELEGRAM_IDS)
# ════════════════════════════════════════════════════════════════════

def _is_admin(telegram_id: int) -> bool:
    """Проверка по env-переменной ADMIN_TELEGRAM_IDS."""
    if not settings.admin_telegram_ids:
        return False
    ids = {
        int(x.strip()) for x in settings.admin_telegram_ids.split(",") if x.strip()
    }
    return telegram_id in ids


@router.message(Command("admin_users"))
async def cmd_admin_users(message: Message) -> None:
    """Обзор всех юзеров со статистикой и краткой выжимкой профиля."""
    if not _is_admin(message.from_user.id):
        return  # молча игнорируем — не палим существование команды

    from src.db.models import VacancyMatch

    async with async_session() as session:
        users = (await session.execute(select(User))).scalars().all()

        if not users:
            await message.answer("Юзеров пока нет.")
            return

        chunks: list[str] = []
        header = f"📊 Всего юзеров: {len(users)}"
        current = header + "\n\n"

        for user in users:
            profile = (await session.execute(
                select(Profile).where(Profile.user_id == user.id)
            )).scalar_one_or_none()

            matches = (await session.execute(
                select(VacancyMatch).where(VacancyMatch.user_id == user.id)
            )).scalars().all()

            total = len(matches)
            liked = sum(1 for m in matches if m.user_reaction == UserReaction.liked)
            disliked = sum(1 for m in matches if m.user_reaction == UserReaction.disliked)
            applied = sum(1 for m in matches if m.user_reaction == UserReaction.applied)
            avg_score = (sum(m.match_score for m in matches) / total) if total else 0

            profile_summary = "пусто"
            if profile and profile.profile_data:
                pd = profile.profile_data
                bits = []
                if pd.get("seniority"):
                    bits.append(f"уровень: {pd['seniority']}")
                if pd.get("expertise"):
                    exp = pd["expertise"]
                    exp_str = ", ".join(exp[:3]) if isinstance(exp, list) else str(exp)
                    bits.append(f"экспертиза: {exp_str[:80]}")
                if pd.get("target_roles"):
                    tr = pd["target_roles"]
                    roles = ", ".join(tr[:2]) if isinstance(tr, list) else str(tr)
                    bits.append(f"роли: {roles[:80]}")
                if pd.get("industries_interested"):
                    ind = pd["industries_interested"]
                    industries = ", ".join(ind[:3]) if isinstance(ind, list) else str(ind)
                    bits.append(f"индустрии: {industries[:80]}")
                if pd.get("ideal_work_description"):
                    ideal = pd["ideal_work_description"]
                    bits.append(f"идеал: {ideal[:100]}")
                if bits:
                    profile_summary = " | ".join(bits)

            username = f"@{user.telegram_username}" if user.telegram_username else "—"
            state_value = user.state.value if user.state else "idle"
            state_emoji = {
                "idle": "✅", "paused": "⏸", "editing_profile": "✏️"
            }.get(state_value, "❓")

            ready_emoji = "🚀" if user.profile_ready_for_search else "⏸"

            block = (
                f"{state_emoji} {username} (id: {user.telegram_id})\n"
                f"   Профиль: {profile_summary}\n"
                f"   Доставлено: {total}, средн. score: {avg_score:.1f}\n"
                f"   👍 {liked}  👎 {disliked}  📨 {applied}\n\n"
            )

            # Telegram limit ~4096 — режем сообщение
            if len(current) + len(block) > 3800:
                chunks.append(current)
                current = block
            else:
                current += block

        if current.strip():
            chunks.append(current)

        for chunk in chunks:
            await message.answer(chunk)


@router.message(Command("admin_profile"))
async def cmd_admin_profile(message: Message) -> None:
    """Полный профиль конкретного юзера. Использование: /admin_profile <telegram_id>"""
    if not _is_admin(message.from_user.id):
        return

    import json

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /admin_profile <telegram_id>")
        return

    try:
        target_id = int(parts[1].strip())
    except ValueError:
        await message.answer("telegram_id должен быть числом.")
        return

    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == target_id)
        )).scalar_one_or_none()
        if user is None:
            await message.answer(f"Юзер с id {target_id} не найден.")
            return

        profile = (await session.execute(
            select(Profile).where(Profile.user_id == user.id)
        )).scalar_one_or_none()

    if profile is None or not profile.profile_data:
        await message.answer(f"У юзера {target_id} пустой профиль.")
        return

    username = f"@{user.telegram_username}" if user.telegram_username else "—"
    header = f"👤 {html_module.escape(username)} (id: {target_id})\n\n"

    # Pretty JSON, обрезаем под лимит сообщения

    pretty = json.dumps(profile.profile_data, ensure_ascii=False, indent=2)
    escaped = html_module.escape(pretty)

    # Один кусок целиком, если влезает в лимит
    body = header + f"<pre>{escaped}</pre>"

    if len(body) > 3900:
        # Разбиваем JSON на чанки и каждый оборачиваем в <pre>
        chunk_size = 3500  # с запасом на теги <pre></pre> и header
        is_first = True
        for i in range(0, len(escaped), chunk_size):
            chunk = escaped[i : i + chunk_size]
            prefix = header if is_first else ""
            await message.answer(f"{prefix}<pre>{chunk}</pre>", parse_mode="HTML")
            is_first = False
    else:
        await message.answer(body, parse_mode="HTML")


from src.services.subscription import (
    get_channel_display,
    get_channel_url,
    is_required_channel_configured,
    is_subscribed,
)


async def _send_subscription_gate(message: Message) -> None:
    """Показывает экран 'подпишись на канал и нажми проверить'."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Перейти в канал", url=get_channel_url())],
        [InlineKeyboardButton(text="✅ Я подписался — проверить", callback_data="sub:check")],
    ])
    text = (
        f"Привет! Чтобы пользоваться ботом, подпишись на мой канал "
        f"{get_channel_display()}.\n\n"
        f"Там я рассказываю про карьерные стратегии, BD-практики и делюсь "
        f"находками из мира работы. После подписки нажми кнопку — продолжим."
    )
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "sub:check")
async def handle_subscription_check(callback: CallbackQuery) -> None:
    """Юзер нажал 'я подписался, проверь' — повторно проверяем."""
    subscribed = await is_subscribed(callback.bot, callback.from_user.id)
    if subscribed:
        await callback.answer("✅ Подписка подтверждена!", show_alert=False)
        # Удаляем сообщение с гейтом
        try:
            await callback.message.delete()
        except Exception:
            pass
        # Запускаем стандартный flow приветствия
        await callback.message.answer(WELCOME_TEXT, parse_mode="HTML", disable_web_page_preview=True)
        # И провижининг в фоне, как в обычном /start
        asyncio.create_task(
            get_or_create_user(
                telegram_id=callback.from_user.id,
                telegram_username=callback.from_user.username,
            )
        )
    else:
        await callback.answer(
            "Пока не вижу подписки. Подпишись и нажми ещё раз.",
            show_alert=True,
        )

