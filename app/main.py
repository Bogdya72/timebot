import asyncio
import hashlib
import hmac
import json
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, types
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from aiogram.utils import executor
from aiogram.utils.exceptions import NetworkError
from aiohttp import web
import aiosqlite
from dotenv import load_dotenv

load_dotenv("/Users/bogdanbogdanov/Desktop/TimeBot/.env")

DEFAULT_DB = "/tmp/timebot.sqlite"
DB_PATH = os.environ.get("TIMEBOT_DB", DEFAULT_DB)
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.abspath(DB_PATH)
if not os.path.exists(os.path.dirname(DB_PATH)):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "http://localhost:8080")
DEBUG_ALLOW = os.environ.get("DEBUG_ALLOW", "1") == "1"
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("PORT", os.environ.get("WEB_PORT", "8080")))

SCHEMA_VERSION = "farm-v2"

# --- Game constants ---
PLOT_COUNT = 6
STAMINA_MAX = 5
STAMINA_REGEN_SEC = 300  # 5 minutes
SELL_TAX = 0.10
SEED_TAX = 0.10
SPOIL_RATE_PER_HOUR = 0.08

CROPS = {
    "lettuce": {"name": "Салат", "grow_min": 5, "seed_cost": 6, "base_price": 10, "base_yield": 1, "disease": 0.05},
    "tomato": {"name": "Помидор", "grow_min": 15, "seed_cost": 12, "base_price": 22, "base_yield": 1, "disease": 0.08},
    "potato": {"name": "Картофель", "grow_min": 30, "seed_cost": 18, "base_price": 30, "base_yield": 2, "disease": 0.10},
    "berry": {"name": "Клубника", "grow_min": 60, "seed_cost": 30, "base_price": 55, "base_yield": 1, "disease": 0.14},
    "lavender": {"name": "Лаванда", "grow_min": 120, "seed_cost": 55, "base_price": 90, "base_yield": 1, "disease": 0.18},
}

WEATHER_STATES = [
    {"kind": "Солнечно", "yield_mod": 0.10, "price_mod": 0.98, "weight": 35},
    {"kind": "Облачно", "yield_mod": 0.00, "price_mod": 1.00, "weight": 35},
    {"kind": "Дожди", "yield_mod": -0.05, "price_mod": 1.03, "weight": 15},
    {"kind": "Гроза", "yield_mod": -0.20, "price_mod": 1.08, "weight": 10},
    {"kind": "Засуха", "yield_mod": -0.25, "price_mod": 1.12, "weight": 5},
]


@dataclass
class Player:
    user_id: int
    credits: int
    stamina: int
    last_stamina_at: int
    security_level: int
    notoriety: int
    created_at: int
    last_subsidy_at: Optional[int]


@dataclass
class Plot:
    user_id: int
    plot_id: int
    crop: Optional[str]
    planted_at: Optional[int]
    grow_minutes: Optional[int]
    status: str
    health: int


@dataclass
class Weather:
    kind: str
    yield_mod: float
    price_mod: float
    updated_at: int
    next_change_at: int


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def clamp(value: float, min_v: float, max_v: float) -> float:
    return max(min_v, min(max_v, value))


def crop_name(code: str) -> str:
    return CROPS[code]["name"]


def seed_price(code: str) -> int:
    return int(CROPS[code]["seed_cost"] * (1 + SEED_TAX))


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        async with db.execute("SELECT value FROM meta WHERE key='schema'") as cursor:
            row = await cursor.fetchone()

        if not row or row[0] != SCHEMA_VERSION:
            await db.execute("DROP TABLE IF EXISTS players")
            await db.execute("DROP TABLE IF EXISTS plots")
            await db.execute("DROP TABLE IF EXISTS storage")
            await db.execute("DROP TABLE IF EXISTS seeds")
            await db.execute("DROP TABLE IF EXISTS market")
            await db.execute("DROP TABLE IF EXISTS weather")
            await db.execute("DELETE FROM meta")
            await db.execute("INSERT INTO meta (key, value) VALUES ('schema', ?)", (SCHEMA_VERSION,))

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                credits INTEGER NOT NULL,
                stamina INTEGER NOT NULL,
                last_stamina_at INTEGER NOT NULL,
                security_level INTEGER NOT NULL,
                notoriety INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                last_subsidy_at INTEGER
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS plots (
                user_id INTEGER NOT NULL,
                plot_id INTEGER NOT NULL,
                crop TEXT,
                planted_at INTEGER,
                grow_minutes INTEGER,
                status TEXT NOT NULL,
                health INTEGER NOT NULL,
                PRIMARY KEY (user_id, plot_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS storage (
                user_id INTEGER NOT NULL,
                crop TEXT NOT NULL,
                qty INTEGER NOT NULL,
                last_update INTEGER NOT NULL,
                PRIMARY KEY (user_id, crop)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS seeds (
                user_id INTEGER NOT NULL,
                crop TEXT NOT NULL,
                qty INTEGER NOT NULL,
                PRIMARY KEY (user_id, crop)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS market (
                crop TEXT PRIMARY KEY,
                base_price REAL NOT NULL,
                supply_index REAL NOT NULL,
                last_update INTEGER NOT NULL,
                price REAL NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS weather (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                yield_mod REAL NOT NULL,
                price_mod REAL NOT NULL,
                updated_at INTEGER NOT NULL,
                next_change_at INTEGER NOT NULL
            )
            """
        )
        await db.commit()

        async with db.execute("SELECT COUNT(*) FROM market") as cursor:
            count = (await cursor.fetchone())[0]
        if count == 0:
            now = now_ts()
            for code, cfg in CROPS.items():
                await db.execute(
                    "INSERT INTO market (crop, base_price, supply_index, last_update, price) VALUES (?, ?, ?, ?, ?)",
                    (code, cfg["base_price"], 0.0, now, cfg["base_price"]),
                )
            await db.commit()

        async with db.execute("SELECT COUNT(*) FROM weather") as cursor:
            wcount = (await cursor.fetchone())[0]
        if wcount == 0:
            now = now_ts()
            state = pick_weather()
            await db.execute(
                "INSERT INTO weather (id, kind, yield_mod, price_mod, updated_at, next_change_at) VALUES (1, ?, ?, ?, ?, ?)",
                (state.kind, state.yield_mod, state.price_mod, now, state.next_change_at),
            )
            await db.commit()


async def get_player(db: aiosqlite.Connection, user_id: int) -> Player:
    async with db.execute(
        """
        SELECT user_id, credits, stamina, last_stamina_at, security_level, notoriety, created_at, last_subsidy_at
        FROM players WHERE user_id = ?
        """,
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()

    if row:
        return Player(*row)

    created = now_ts()
    p = Player(
        user_id=user_id,
        credits=120,
        stamina=STAMINA_MAX,
        last_stamina_at=created,
        security_level=0,
        notoriety=0,
        created_at=created,
        last_subsidy_at=None,
    )
    await db.execute(
        "INSERT INTO players VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            p.user_id,
            p.credits,
            p.stamina,
            p.last_stamina_at,
            p.security_level,
            p.notoriety,
            p.created_at,
            p.last_subsidy_at,
        ),
    )

    for i in range(1, PLOT_COUNT + 1):
        await db.execute(
            "INSERT INTO plots VALUES (?, ?, NULL, NULL, NULL, 'empty', 100)",
            (p.user_id, i),
        )
    await db.commit()
    return p


async def update_player(db: aiosqlite.Connection, p: Player) -> None:
    await db.execute(
        """
        UPDATE players
        SET credits=?, stamina=?, last_stamina_at=?, security_level=?, notoriety=?, last_subsidy_at=?
        WHERE user_id=?
        """,
        (
            p.credits,
            p.stamina,
            p.last_stamina_at,
            p.security_level,
            p.notoriety,
            p.last_subsidy_at,
            p.user_id,
        ),
    )
    await db.commit()


async def refresh_stamina(db: aiosqlite.Connection, p: Player) -> Player:
    now = now_ts()
    delta = now - p.last_stamina_at
    if delta >= STAMINA_REGEN_SEC:
        add = delta // STAMINA_REGEN_SEC
        if add > 0:
            p.stamina = min(STAMINA_MAX, p.stamina + add)
            p.last_stamina_at += add * STAMINA_REGEN_SEC
            await update_player(db, p)
    return p


def pick_weather() -> Weather:
    now = now_ts()
    population = []
    for w in WEATHER_STATES:
        population.extend([w] * w["weight"])
    chosen = random.choice(population)
    duration = random.randint(30, 90) * 60
    return Weather(
        kind=chosen["kind"],
        yield_mod=chosen["yield_mod"],
        price_mod=chosen["price_mod"],
        updated_at=now,
        next_change_at=now + duration,
    )


async def get_weather(db: aiosqlite.Connection) -> Weather:
    async with db.execute("SELECT kind, yield_mod, price_mod, updated_at, next_change_at FROM weather WHERE id=1") as cursor:
        row = await cursor.fetchone()
    if not row:
        state = pick_weather()
        await db.execute(
            "INSERT INTO weather (id, kind, yield_mod, price_mod, updated_at, next_change_at) VALUES (1, ?, ?, ?, ?, ?)",
            (state.kind, state.yield_mod, state.price_mod, state.updated_at, state.next_change_at),
        )
        await db.commit()
        return state

    state = Weather(*row)
    now = now_ts()
    if now >= state.next_change_at:
        state = pick_weather()
        await db.execute(
            "UPDATE weather SET kind=?, yield_mod=?, price_mod=?, updated_at=?, next_change_at=? WHERE id=1",
            (state.kind, state.yield_mod, state.price_mod, state.updated_at, state.next_change_at),
        )
        await db.commit()
    return state


async def get_plots(db: aiosqlite.Connection, user_id: int) -> list[Plot]:
    async with db.execute(
        "SELECT user_id, plot_id, crop, planted_at, grow_minutes, status, health FROM plots WHERE user_id=? ORDER BY plot_id",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [Plot(*row) for row in rows]


async def refresh_plots(db: aiosqlite.Connection, user_id: int) -> None:
    now = now_ts()
    async with db.execute(
        "SELECT plot_id, planted_at, grow_minutes, status FROM plots WHERE user_id=? AND status='planted'",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    for plot_id, planted_at, grow_minutes, status in rows:
        if planted_at and grow_minutes and now >= planted_at + grow_minutes * 60:
            await db.execute(
                "UPDATE plots SET status='ready' WHERE user_id=? AND plot_id=?",
                (user_id, plot_id),
            )
    await db.commit()


async def add_storage(db: aiosqlite.Connection, user_id: int, crop: str, qty: int) -> None:
    now = now_ts()
    async with db.execute("SELECT qty, last_update FROM storage WHERE user_id=? AND crop=?", (user_id, crop)) as cursor:
        row = await cursor.fetchone()
    if row:
        cur_qty, last_update = row
        new_qty = cur_qty + qty
        await db.execute("UPDATE storage SET qty=?, last_update=? WHERE user_id=? AND crop=?", (new_qty, now, user_id, crop))
    else:
        await db.execute("INSERT INTO storage VALUES (?, ?, ?, ?)", (user_id, crop, qty, now))
    await db.commit()


async def add_seed(db: aiosqlite.Connection, user_id: int, crop: str, qty: int) -> None:
    async with db.execute("SELECT qty FROM seeds WHERE user_id=? AND crop=?", (user_id, crop)) as cursor:
        row = await cursor.fetchone()
    if row:
        await db.execute("UPDATE seeds SET qty=? WHERE user_id=? AND crop=?", (row[0] + qty, user_id, crop))
    else:
        await db.execute("INSERT INTO seeds VALUES (?, ?, ?)", (user_id, crop, qty))
    await db.commit()


async def get_seed_qty(db: aiosqlite.Connection, user_id: int, crop: str) -> int:
    async with db.execute("SELECT qty FROM seeds WHERE user_id=? AND crop=?", (user_id, crop)) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else 0


async def apply_spoilage(db: aiosqlite.Connection, user_id: int) -> None:
    now = now_ts()
    async with db.execute("SELECT crop, qty, last_update FROM storage WHERE user_id=?", (user_id,)) as cursor:
        rows = await cursor.fetchall()
    for crop, qty, last_update in rows:
        hours = max(0, (now - last_update) / 3600.0)
        if hours <= 0:
            continue
        decay = int(qty * (1 - pow(2.71828, -SPOIL_RATE_PER_HOUR * hours)))
        if decay > 0:
            new_qty = max(0, qty - decay)
            await db.execute("UPDATE storage SET qty=?, last_update=? WHERE user_id=? AND crop=?", (new_qty, now, user_id, crop))
    await db.commit()


async def update_market_prices(db: aiosqlite.Connection, weather: Weather) -> None:
    now = now_ts()
    async with db.execute("SELECT crop, base_price, supply_index, last_update FROM market") as cursor:
        rows = await cursor.fetchall()
    for crop, base_price, supply_index, last_update in rows:
        hours = max(0, (now - last_update) / 3600.0)
        if hours > 0:
            supply_index *= pow(0.85, hours)
        price = base_price * (1 - min(0.6, supply_index)) * weather.price_mod
        price = max(2.0, price)
        await db.execute("UPDATE market SET supply_index=?, last_update=?, price=? WHERE crop=?", (supply_index, now, price, crop))
    await db.commit()


async def market_price(db: aiosqlite.Connection, crop: str) -> float:
    async with db.execute("SELECT price FROM market WHERE crop=?", (crop,)) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else CROPS[crop]["base_price"]


async def market_supply_add(db: aiosqlite.Connection, crop: str, qty: int) -> None:
    async with db.execute("SELECT supply_index FROM market WHERE crop=?", (crop,)) as cursor:
        row = await cursor.fetchone()
    if row:
        supply = row[0] + qty / 50.0
        await db.execute("UPDATE market SET supply_index=? WHERE crop=?", (supply, crop))
        await db.commit()


def verify_init_data(init_data: str) -> Optional[int]:
    if not init_data:
        return None
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    if "hash" not in data:
        return None

    check_hash = data.pop("hash")
    data_check_string = "\n".join([f"{k}={data[k]}" for k in sorted(data.keys())])
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    hmac_hash = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(hmac_hash, check_hash):
        return None
    if "auth_date" in data:
        try:
            auth_date = int(data["auth_date"])
            if now_ts() - auth_date > 86400:
                return None
        except ValueError:
            return None

    user_raw = data.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
        return int(user.get("id"))
    except Exception:
        return None


async def build_state(user_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        p = await get_player(db, user_id)
        p = await refresh_stamina(db, p)
        weather = await get_weather(db)
        await refresh_plots(db, user_id)
        await apply_spoilage(db, user_id)
        await update_market_prices(db, weather)

        plots = await get_plots(db, user_id)
        plot_views = []
        now = now_ts()
        for pl in plots:
            minutes_left = 0
            if pl.status == "planted" and pl.planted_at and pl.grow_minutes:
                ready_at = pl.planted_at + pl.grow_minutes * 60
                minutes_left = max(0, int((ready_at - now) / 60))
            plot_views.append({
                "plot_id": pl.plot_id,
                "status": pl.status,
                "crop": pl.crop,
                "crop_name": crop_name(pl.crop) if pl.crop else None,
                "minutes_left": minutes_left,
            })

        async with db.execute("SELECT crop, qty FROM storage WHERE user_id=?", (user_id,)) as cursor:
            storage_rows = await cursor.fetchall()
        storage = [{"code": c, "name": crop_name(c), "qty": q} for c, q in storage_rows if q > 0]

        async with db.execute("SELECT crop, price FROM market") as cursor:
            market_rows = await cursor.fetchall()
        market = [{"code": c, "name": crop_name(c), "price": p} for c, p in market_rows]

        seeds = [
            {"code": c, "name": crop_name(c), "price": seed_price(c), "grow_min": CROPS[c]["grow_min"]}
            for c in CROPS.keys()
        ]

    return {
        "player": {
            "credits": p.credits,
            "stamina": p.stamina,
            "stamina_max": STAMINA_MAX,
            "security_level": p.security_level,
            "notoriety": p.notoriety,
        },
        "weather": {"kind": weather.kind, "yield_mod": weather.yield_mod, "price_mod": weather.price_mod},
        "plots": plot_views,
        "storage": storage,
        "market": market,
        "seeds": seeds,
    }


async def action_plant(user_id: int, plot_id: int, crop: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        p = await get_player(db, user_id)
        p = await refresh_stamina(db, p)
        if p.stamina <= 0:
            return "Нет стамины."

        async with db.execute("SELECT status FROM plots WHERE user_id=? AND plot_id=?", (p.user_id, plot_id)) as cursor:
            row = await cursor.fetchone()
        if not row or row[0] != "empty":
            return "Участок занят."

        seed_qty = await get_seed_qty(db, p.user_id, crop)
        if seed_qty <= 0:
            return "Нет семян."

        await db.execute("UPDATE seeds SET qty=? WHERE user_id=? AND crop=?", (seed_qty - 1, p.user_id, crop))
        await db.execute(
            "UPDATE plots SET crop=?, planted_at=?, grow_minutes=?, status='planted', health=100 WHERE user_id=? AND plot_id=?",
            (crop, now_ts(), CROPS[crop]["grow_min"], p.user_id, plot_id),
        )
        p.stamina -= 1
        await update_player(db, p)
    return "Посадка выполнена."


async def action_harvest(user_id: int, plot_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        p = await get_player(db, user_id)
        p = await refresh_stamina(db, p)
        if p.stamina <= 0:
            return "Нет стамины."

        async with db.execute(
            "SELECT crop, planted_at, grow_minutes, status, health FROM plots WHERE user_id=? AND plot_id=?",
            (p.user_id, plot_id),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return "Участок не найден."

        crop, planted_at, grow_minutes, status, health = row
        if status != "ready":
            return "Еще не готово."

        now = now_ts()
        ready_at = planted_at + grow_minutes * 60
        overdue_min = max(0, int((now - ready_at) / 60))

        weather = await get_weather(db)
        base_yield = CROPS[crop]["base_yield"]
        mult = 1.0 + weather.yield_mod
        mult *= 0.6 + 0.4 * (health / 100.0)

        disease_chance = CROPS[crop]["disease"] + max(0.0, -weather.yield_mod) * 0.5 + max(0, 50 - health) / 200.0
        rot_chance = min(0.5, overdue_min * 0.02)

        disease = random.random() < disease_chance
        rot = random.random() < rot_chance
        if disease:
            mult *= 0.6
        if rot:
            mult *= 0.5

        qty = max(0, int(round(base_yield * mult)))

        if qty > 0:
            await add_storage(db, p.user_id, crop, qty)
        await db.execute(
            "UPDATE plots SET crop=NULL, planted_at=NULL, grow_minutes=NULL, status='empty', health=100 WHERE user_id=? AND plot_id=?",
            (p.user_id, plot_id),
        )
        p.stamina -= 1
        await update_player(db, p)

    msg = f"Сбор: {crop_name(crop)} x{qty}."
    if disease:
        msg += " Болезнь снизила урожай."
    if rot:
        msg += " Порча снизила урожай."
    return msg


async def action_buy_seed(user_id: int, crop: str, qty: int) -> str:
    cost = seed_price(crop) * qty
    async with aiosqlite.connect(DB_PATH) as db:
        p = await get_player(db, user_id)
        if p.credits < cost:
            return "Не хватает кредитов."
        p.credits -= cost
        await update_player(db, p)
        await add_seed(db, p.user_id, crop, qty)
    return f"Куплено семян: {qty}."


async def action_sell(user_id: int, crop: str, qty: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        await apply_spoilage(db, user_id)
        async with db.execute("SELECT qty FROM storage WHERE user_id=? AND crop=?", (user_id, crop)) as cursor:
            row = await cursor.fetchone()
        have = row[0] if row else 0
        if have < qty:
            return "Недостаточно на складе."

        weather = await get_weather(db)
        await update_market_prices(db, weather)
        price = await market_price(db, crop)
        gross = price * qty
        net = int(gross * (1 - SELL_TAX))

        await db.execute("UPDATE storage SET qty=? WHERE user_id=? AND crop=?", (have - qty, user_id, crop))
        await market_supply_add(db, crop, qty)

        p = await get_player(db, user_id)
        p.credits += net
        await update_player(db, p)
    return f"Продано. +{net} кр"


async def action_security(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        p = await get_player(db, user_id)
        level = p.security_level
        if level >= 5:
            return "Охрана максимальна."
        cost = 80 + level * 60
        if p.credits < cost:
            return "Не хватает кредитов."
        p.credits -= cost
        p.security_level += 1
        await update_player(db, p)
    return "Охрана усилена."


async def action_subsidy(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        p = await get_player(db, user_id)
        now = now_ts()
        if p.last_subsidy_at and now - p.last_subsidy_at < 24 * 3600:
            return "Субсидия доступна раз в сутки."
        p.last_subsidy_at = now
        p.credits += 25
        await update_player(db, p)
    return "Субсидия получена (+25 кр)."


async def action_raid(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        attacker = await get_player(db, user_id)
        attacker = await refresh_stamina(db, attacker)
        if attacker.stamina <= 0:
            return "Нет стамины."

        async with db.execute(
            "SELECT user_id, credits, stamina, last_stamina_at, security_level, notoriety, created_at, last_subsidy_at "
            "FROM players WHERE user_id != ? ORDER BY RANDOM() LIMIT 1",
            (attacker.user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return "Нет целей. Пригласи друзей."

        target = Player(*row)

        chance = 45 + attacker.notoriety * 2 - target.security_level * 6
        chance = clamp(chance, 10, 80)
        success = random.random() < (chance / 100.0)

        attacker.stamina -= 1

        if success:
            async with db.execute(
                "SELECT crop, qty FROM storage WHERE user_id=? AND qty>0 ORDER BY RANDOM() LIMIT 1",
                (target.user_id,),
            ) as cursor:
                srow = await cursor.fetchone()

            if srow:
                crop, qty = srow
                steal = min(qty, random.randint(1, 2))
                await db.execute("UPDATE storage SET qty=? WHERE user_id=? AND crop=?", (qty - steal, target.user_id, crop))
                await add_storage(db, attacker.user_id, crop, steal)
                result = f"Успех. Украдено: {crop_name(crop)} x{steal}."
            else:
                steal = min(target.credits, random.randint(10, 30))
                target.credits -= steal
                attacker.credits += steal
                await update_player(db, target)
                result = f"Успех. Украдено: {steal} кр."

            attacker.notoriety += 1
        else:
            fine = min(attacker.credits, random.randint(8, 18))
            attacker.credits -= fine
            attacker.notoriety = max(0, attacker.notoriety - 1)
            result = f"Провал. Штраф {fine} кр."

        await update_player(db, attacker)
    return result


async def action_sabotage(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        attacker = await get_player(db, user_id)
        attacker = await refresh_stamina(db, attacker)
        if attacker.stamina <= 0:
            return "Нет стамины."

        async with db.execute(
            "SELECT user_id, credits, stamina, last_stamina_at, security_level, notoriety, created_at, last_subsidy_at "
            "FROM players WHERE user_id != ? ORDER BY RANDOM() LIMIT 1",
            (attacker.user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return "Нет целей. Пригласи друзей."

        target = Player(*row)

        chance = 40 + attacker.notoriety * 2 - target.security_level * 7
        chance = clamp(chance, 10, 75)
        success = random.random() < (chance / 100.0)

        attacker.stamina -= 1

        if success:
            async with db.execute(
                "SELECT plot_id, health, grow_minutes FROM plots WHERE user_id=? AND status='planted' ORDER BY RANDOM() LIMIT 1",
                (target.user_id,),
            ) as cursor:
                prow = await cursor.fetchone()
            if prow:
                plot_id, health, grow_minutes = prow
                new_health = max(30, health - random.randint(20, 35))
                new_grow = grow_minutes + random.randint(3, 8)
                await db.execute("UPDATE plots SET health=?, grow_minutes=? WHERE user_id=? AND plot_id=?", (new_health, new_grow, target.user_id, plot_id))
                result = "Успех. Рост цели замедлен."
            else:
                result = "Успех, но у цели нет растущих культур."
            attacker.notoriety += 1
        else:
            fine = min(attacker.credits, random.randint(6, 14))
            attacker.credits -= fine
            attacker.notoriety = max(0, attacker.notoriety - 1)
            result = f"Провал. Штраф {fine} кр."

        await update_player(db, attacker)
    return result


# --- Web handlers ---

async def parse_user(request: web.Request) -> int:
    body = await request.json()
    init_data = body.get("initData", "")
    user_id = verify_init_data(init_data)
    if not user_id and DEBUG_ALLOW:
        debug_user = body.get("debugUser")
        if debug_user:
            return int(debug_user)
    if not user_id:
        raise web.HTTPUnauthorized(text="unauthorized")
    return user_id


async def handle_state(request: web.Request) -> web.Response:
    user_id = await parse_user(request)
    state = await build_state(user_id)
    return web.json_response(state)


async def handle_plant(request: web.Request) -> web.Response:
    body = await request.json()
    user_id = await parse_user(request)
    plot_id = int(body.get("plot_id"))
    crop = body.get("crop")
    msg = await action_plant(user_id, plot_id, crop)
    return web.json_response({"message": msg})


async def handle_harvest(request: web.Request) -> web.Response:
    body = await request.json()
    user_id = await parse_user(request)
    plot_id = int(body.get("plot_id"))
    msg = await action_harvest(user_id, plot_id)
    return web.json_response({"message": msg})


async def handle_buy_seed(request: web.Request) -> web.Response:
    body = await request.json()
    user_id = await parse_user(request)
    crop = body.get("crop")
    qty = int(body.get("qty"))
    msg = await action_buy_seed(user_id, crop, qty)
    return web.json_response({"message": msg})


async def handle_sell(request: web.Request) -> web.Response:
    body = await request.json()
    user_id = await parse_user(request)
    crop = body.get("crop")
    qty = int(body.get("qty"))
    msg = await action_sell(user_id, crop, qty)
    return web.json_response({"message": msg})


async def handle_security(request: web.Request) -> web.Response:
    user_id = await parse_user(request)
    msg = await action_security(user_id)
    return web.json_response({"message": msg})


async def handle_subsidy(request: web.Request) -> web.Response:
    user_id = await parse_user(request)
    msg = await action_subsidy(user_id)
    return web.json_response({"message": msg})


async def handle_raid(request: web.Request) -> web.Response:
    user_id = await parse_user(request)
    msg = await action_raid(user_id)
    return web.json_response({"message": msg})


async def handle_sabotage(request: web.Request) -> web.Response:
    user_id = await parse_user(request)
    msg = await action_sabotage(user_id)
    return web.json_response({"message": msg})


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", lambda request: web.FileResponse("/Users/bogdanbogdanov/Desktop/TimeBot/webapp/index.html"))
    app.router.add_get("/styles.css", lambda request: web.FileResponse("/Users/bogdanbogdanov/Desktop/TimeBot/webapp/styles.css"))
    app.router.add_get("/app.js", lambda request: web.FileResponse("/Users/bogdanbogdanov/Desktop/TimeBot/webapp/app.js"))

    app.router.add_post("/api/state", handle_state)
    app.router.add_post("/api/plant", handle_plant)
    app.router.add_post("/api/harvest", handle_harvest)
    app.router.add_post("/api/buy_seed", handle_buy_seed)
    app.router.add_post("/api/sell", handle_sell)
    app.router.add_post("/api/security", handle_security)
    app.router.add_post("/api/subsidy", handle_subsidy)
    app.router.add_post("/api/raid", handle_raid)
    app.router.add_post("/api/sabotage", handle_sabotage)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    print(f"[web] running on {WEB_HOST}:{WEB_PORT}")
    return runner


# --- Bot ---


def webapp_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Открыть ферму", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True,
    )


async def on_start(message: types.Message):
    await message.answer("Открывай ферму ниже.", reply_markup=webapp_kb())


async def on_text(message: types.Message):
    await message.answer("Открывай ферму ниже.", reply_markup=webapp_kb())


# --- Runner ---

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env is required")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(bot)

    dp.register_message_handler(on_start, commands=["start"])
    dp.register_message_handler(on_text, content_types=types.ContentTypes.TEXT)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    loop.run_until_complete(start_web_server())

    if os.environ.get("NO_RESTART") == "1":
        executor.start_polling(dp, skip_updates=True, loop=loop)
        return

    backoff = 1
    while True:
        try:
            executor.start_polling(dp, skip_updates=True, loop=loop)
            backoff = 1
        except NetworkError:
            time_to_sleep = min(60, backoff)
            print(f"[network] disconnected, retrying in {time_to_sleep}s")
            backoff = min(60, backoff * 2)
            time.sleep(time_to_sleep)
        except Exception as exc:
            time_to_sleep = min(60, backoff)
            print(f"[error] {exc}. retrying in {time_to_sleep}s")
            backoff = min(60, backoff * 2)
            time.sleep(time_to_sleep)


if __name__ == "__main__":
    main()
