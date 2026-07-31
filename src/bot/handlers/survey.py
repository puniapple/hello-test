from aiogram import Router, F
from aiogram.types import Message
from sqlalchemy import select, update
from src.db.models import User, SurveyResponse
from src.db.session import async_session

SURVEY_KEY = "hard_job_search_072026"

router = Router()


async def _is_awaiting_survey(message: Message) -> bool:
    if not message.from_user:
        return False
    async with async_session() as session:
        awaiting = (await session.execute(
            select(User.survey_awaiting).where(User.telegram_id == message.from_user.id)
        )).scalar_one_or_none()
    return awaiting == SURVEY_KEY


@router.message(F.text & ~F.text.startswith("/"), _is_awaiting_survey)
async def catch_survey_answer(message: Message):
    async with async_session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )).scalar_one_or_none()
        if not user:
            return

        session.add(SurveyResponse(
            user_id=user.id,
            telegram_id=message.from_user.id,
            question_key=SURVEY_KEY,
            answer_text=message.text,
        ))
        await session.execute(
            update(User).where(User.id == user.id).values(survey_awaiting=None)
        )
        await session.commit()

    await message.answer("Спасибо, записала🙏🏼")