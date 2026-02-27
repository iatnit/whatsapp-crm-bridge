"""One-time fix: reassign phone 919711111387 from Paramjit-PP to Ranjit-CH."""

import asyncio
import aiosqlite


async def main():
    async with aiosqlite.connect("data/whatsapp.db") as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT phone, display_name, customer_id, customer_name FROM conversations WHERE phone = ?",
            ("919711111387",),
        )
        row = await cur.fetchone()
        if row:
            print(f"Before: {dict(row)}")
        else:
            print("Phone 919711111387 not found")
            return

        await db.execute(
            "UPDATE conversations SET customer_id = ?, customer_name = ?, match_status = ? WHERE phone = ?",
            ("100671", "Ranjit-CH", "matched", "919711111387"),
        )
        await db.commit()
        print("Done: 919711111387 → Ranjit-CH (100671)")


if __name__ == "__main__":
    asyncio.run(main())
