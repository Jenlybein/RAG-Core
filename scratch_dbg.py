import asyncio
from tests.test_console_api import _world
from tests.fake_db import RecordingSession, async_mirror
from app.repositories import account_repository

db = RecordingSession(rows=_world())
mirror = async_mirror(db)
print("same rows:", mirror._rows is db._rows)

async def main():
    got = await account_repository.get_by_username_async(mirror, "alice")
    print("got:", got)
    from sqlalchemy import select
    from app.models.account import Account
    st = select(Account).where(Account.username == "alice")
    print("orm model:", __import__("tests.fake_db", fromlist=["x"])._orm_model_of(st))
    print("raw:", mirror.execute and db.execute(st).all())

asyncio.run(main())
