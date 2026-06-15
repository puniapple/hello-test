import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from src.config import settings


async def main():
    engine = create_async_engine(
        settings.database_url,
        connect_args={"ssl": "require"},
    )
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT version()"))
        print("✓ Подключение работает:", result.scalar())
    await engine.dispose()


asyncio.run(main())
