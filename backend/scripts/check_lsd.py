"""cp_01KSTWJK2CWVZ97DP789D7Q2KM 의 last_sent_data 실제 타입/값 확인."""

import asyncio

from sqlmodel import select

from backend.db.orm import get_write_sessionmaker
from backend.domain.samba.collector.model import SambaCollectedProduct

PID = "cp_01KSTWJK2CWVZ97DP789D7Q2KM"


async def main():
    Session = get_write_sessionmaker()
    async with Session() as session:
        stmt = select(SambaCollectedProduct).where(SambaCollectedProduct.id == PID)
        p = (await session.execute(stmt)).scalars().first()
        if not p:
            print("없음")
            return
        lsd = p.last_sent_data
        print(f"type(last_sent_data) = {type(lsd)}")
        print(f"repr(앞 600자) = {repr(lsd)[:600]}")
        try:
            d = dict(lsd or {})
            print(f"dict() OK keys={list(d.keys())[:10]}")
        except Exception as e:
            print(f"dict() 실패: {type(e).__name__}: {e}")
        # registered_accounts / market_product_nos 도 같이
        print(f"registered_accounts type={type(p.registered_accounts)} val={repr(p.registered_accounts)[:200]}")
        print(f"market_product_nos type={type(p.market_product_nos)} val={repr(p.market_product_nos)[:200]}")
        print(f"status={p.status}")


if __name__ == "__main__":
    asyncio.run(main())
