# server.py — CASEFORGE backend — v3.0 "ПЕРЕЗАГРУЗКА"
import os, re, json, time, uuid, random, secrets
from datetime import datetime
from math import comb
from typing import Optional, List
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from fastapi import FastAPI, HTTPException, Depends, Header, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import jwt, asyncpg
from passlib.hash import bcrypt

VERSION = "3.0"
CODENAME = "ПЕРЕЗАГРУЗКА"

SECRET    = os.getenv("JWT_SECRET", secrets.token_hex(32))
DB_URL    = os.getenv("DATABASE_URL", "")
DATA_PATH = os.getenv("DATA_PATH", "data/game_data.json")

ADMIN_NICK = "admin"
ADMIN_PASS = "AdmiN@1@2@3"

RATING_EXCLUDED_NICKS = {"admin", "maga"}
QUESTS_VERSION = 3
WEEKEND_MULT   = 1.5
TRADE_FEE      = 0.05

# ==================== MINES ====================
MINES_GRID      = 25       # 5×5
MINES_VALID     = [1, 3, 5, 10, 24]
MINES_MIN_BET   = 10
MINES_MAX_BET   = 1_000_000
MINES_EDGE      = 0.97     # 3% в пользу сервера


def _clean_dsn(dsn):
    p = urlparse(dsn)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "channel_binding"]
    return urlunparse(p._replace(query=urlencode(q)))


def is_weekend():
    return datetime.utcnow().weekday() >= 5


def current_rating_period():
    now = datetime.utcnow()
    if now.day >= 5:
        return f"{now.year:04d}-{now.month:02d}"
    if now.month == 1:
        return f"{now.year-1:04d}-12"
    return f"{now.year:04d}-{now.month-1:02d}"


def mines_multiplier(mines: int, opened: int) -> float:
    """Множитель при заданном числе мин и открытых клеток."""
    if opened <= 0:
        return 1.0
    safe_total = MINES_GRID - mines       # сколько безопасных
    if opened > safe_total:
        return 0.0
    # P(открыть k безопасных подряд) = C(safe, k) / C(25, k)
    p = comb(safe_total, opened) / comb(MINES_GRID, opened)
    return round((1.0 / p) * MINES_EDGE, 6)


if os.path.exists(DATA_PATH):
    with open(DATA_PATH, encoding="utf-8") as f:
        DATA = json.load(f)
else:
    DATA = {"items": [], "cases": []}
ITEMS = {i["id"]: i for i in DATA.get("items", [])}
CASES = {c["id"]: c for c in DATA.get("cases", [])}
for _i in ITEMS.values():
    if "name" not in _i:
        _i["name"] = f"{_i.get('wt', '???')} | {_i.get('sk', '???')}"


def resolve_item(item_id):
    if not item_id or not isinstance(item_id, str):
        return None
    base = ITEMS.get(item_id)
    if base:
        return base
    m = re.match(r'^(it\d+)_n(\d+)$', str(item_id))
    if not m:
        return None
    b = ITEMS.get(m.group(1))
    if not b:
        return None
    num = int(m.group(2))
    if not (1 <= num <= 100):
        return None
    t = (101 - num) / 100.0
    mult = 1 + t * t * 40
    return {**b, "price": round(b["price"] * mult), "num": num, "baseId": b["id"]}


pool: Optional[asyncpg.Pool] = None


async def init_db():
    global pool
    pool = await asyncpg.create_pool(_clean_dsn(DB_URL), min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users(
                id SERIAL PRIMARY KEY, nick TEXT UNIQUE, pass TEXT, created DOUBLE PRECISION);
            CREATE TABLE IF NOT EXISTS saves(
                user_id INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                state JSONB, updated DOUBLE PRECISION);
            CREATE TABLE IF NOT EXISTS friends(
                user_id INT REFERENCES users(id) ON DELETE CASCADE,
                friend_id INT REFERENCES users(id) ON DELETE CASCADE,
                PRIMARY KEY(user_id, friend_id));
            CREATE TABLE IF NOT EXISTS presence(
                user_id INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                last_seen DOUBLE PRECISION);
            CREATE TABLE IF NOT EXISTS battles(
                id TEXT PRIMARY KEY, creator INT, mode TEXT, target TEXT,
                cases JSONB, status TEXT, players JSONB, results JSONB,
                members TEXT, created DOUBLE PRECISION);
            CREATE TABLE IF NOT EXISTS promos(
                code TEXT PRIMARY KEY, type TEXT NOT NULL, payload JSONB,
                uses INT DEFAULT 0, max_uses INT DEFAULT -1, created DOUBLE PRECISION);
            CREATE TABLE IF NOT EXISTS promo_uses(
                user_id INT REFERENCES users(id) ON DELETE CASCADE,
                code TEXT, used_at DOUBLE PRECISION,
                PRIMARY KEY(user_id, code));
            CREATE TABLE IF NOT EXISTS bans(
                nick TEXT PRIMARY KEY, until_ts DOUBLE PRECISION, reason TEXT,
                by_nick TEXT, created DOUBLE PRECISION, user_id INT);
            CREATE TABLE IF NOT EXISTS live_drops(
                id BIGSERIAL PRIMARY KEY, nick TEXT, item_id TEXT, num INT,
                case_name TEXT, price INT, ts DOUBLE PRECISION);
            CREATE INDEX IF NOT EXISTS idx_live_drops_ts ON live_drops(ts DESC);
            CREATE TABLE IF NOT EXISTS rating(
                user_id INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                period TEXT, battle_profit DOUBLE PRECISION DEFAULT 0,
                cases_spent DOUBLE PRECISION DEFAULT 0,
                cases_opened INT DEFAULT 0,
                updated DOUBLE PRECISION);
            CREATE TABLE IF NOT EXISTS mines_games(
                id TEXT PRIMARY KEY,
                user_id INT REFERENCES users(id) ON DELETE CASCADE,
                bet BIGINT NOT NULL,
                mines INT NOT NULL,
                mine_positions JSONB NOT NULL,
                revealed JSONB NOT NULL DEFAULT '[]'::jsonb,
                multiplier DOUBLE PRECISION DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                payout BIGINT DEFAULT 0,
                created DOUBLE PRECISION,
                finished DOUBLE PRECISION);
            CREATE INDEX IF NOT EXISTS idx_mines_user_status ON mines_games(user_id, status);
        """)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    if pool:
        await pool.close()


app = FastAPI(title=f"CASEFORGE {VERSION} — {CODENAME}", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=500)


def _j(x):
    if x is None:
        return None
    if isinstance(x, (dict, list)):
        return x
    if isinstance(x, str):
        try:
            return json.loads(x)
        except Exception:
            return None
    return None


def make_token(uid, admin=False):
    return jwt.encode({"uid": uid, "adm": bool(admin), "exp": time.time() + 30 * 86400},
                      SECRET, algorithm="HS256")


def _ban_message(ban):
    if ban["until_ts"] and ban["until_ts"] > 0:
        left = max(0, int(ban["until_ts"] - time.time()))
        return json.dumps({
            "banned": True, "nick": ban["nick"],
            "reason": ban["reason"] or "не указана",
            "by": ban["by_nick"] or "админ",
            "until": ban["until_ts"], "left": left, "created": ban["created"],
        }, ensure_ascii=False)
    return json.dumps({
        "banned": True, "nick": ban["nick"],
        "reason": ban["reason"] or "не указана",
        "by": ban["by_nick"] or "админ",
        "until": 0, "left": -1, "created": ban["created"],
    }, ensure_ascii=False)


async def get_active_ban(nick):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM bans WHERE LOWER(nick)=LOWER($1)", nick)
    if not row:
        return None
    if row["until_ts"] and row["until_ts"] > 0 and row["until_ts"] < time.time():
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM bans WHERE LOWER(nick)=LOWER($1)", nick)
        return None
    return dict(row)


async def get_admin_id():
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM users WHERE LOWER(nick)=LOWER($1)", ADMIN_NICK)
    return row["id"] if row else None


async def credit_admin_fee(amount, reason):
    if amount <= 0:
        return 0
    admin_id = await get_admin_id()
    if not admin_id:
        return 0
    st = await load_state(admin_id)
    st["balance"] = st.get("balance", 0) + amount
    st["stats"]["earned"] = st["stats"].get("earned", 0) + amount
    st.setdefault("fee_log", [])
    st["fee_log"].insert(0, {"ts": int(time.time() * 1000), "amount": amount, "reason": reason})
    st["fee_log"] = st["fee_log"][:200]
    await persist(admin_id, st)
    return amount


async def get_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Требуется вход")
    try:
        uid = jwt.decode(authorization.split(" ")[1], SECRET, algorithms=["HS256"])["uid"]
    except Exception:
        raise HTTPException(401, "Неверный токен")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id=$1", uid)
    if not row:
        raise HTTPException(401, "Пользователь не найден")
    ban = await get_active_ban(row["nick"])
    if ban:
        raise HTTPException(403, _ban_message(ban))
    return dict(row)


def is_admin_user(user):
    return user["nick"].lower() == ADMIN_NICK


async def require_admin(user=Depends(get_user)):
    if not is_admin_user(user):
        raise HTTPException(403, "Требуются права администратора")
    return user


def today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def default_state(nick):
    return {
        "balance": 3000, "inv": [], "hist": [], "fav": [], "fast": False, "sound": True,
        "name": nick, "tokens": 0, "welcome": False,
        "cd": {}, "wheel": 0, "daily": {"streak": 0, "last": 0},
        "promo": [], "claimed": {},
        "qp": {
            "free": 0, "got": 0, "sold": 0, "big": 0, "gold": 0,
            "upw": 0, "ct": 0, "wheel": 0,
            "promo_used": 0, "battle_play": 0, "battle_win": 0,
            "trade_done": 0, "daily_get": 0, "case_paid": 0,
            "upgrade_all": 0, "num_skin": 0, "covert_drop": 0,
            "legendary_drop": 0, "ct_streak": 0,
            "mines_play": 0, "mines_win": 0,
        },
        "daily_quests": [], "daily_claimed": {},
        "quest_date": None, "quests_v": QUESTS_VERSION,
        "fee_log": [],
        "stats": {"opened": 0, "best": 0, "spent": 0, "won": 0, "upW": 0, "upL": 0,
                  "ct": 0, "xp": 0, "free": 0, "earned": 0,
                  "mines_spent": 0, "mines_earned": 0},
        "created": int(time.time() * 1000),
    }


QUEST_POOL = [
    # ==== Лёгкие (k=5000) → 15к–25к ====
    {"ic": "🎁", "n": "Открыть {} бесплатных кейсов", "s": "free", "t": [3, 5], "k": 5000},
    {"ic": "🎒", "n": "Получить {} предметов", "s": "got", "t": [3, 5], "k": 5000},
    {"ic": "💰", "n": "Продать {} предметов", "s": "sold", "t": [3, 5], "k": 5000},
    {"ic": "🎡", "n": "Крутить колесо {} раз", "s": "wheel", "t": [1, 2], "k": 5000},
    {"ic": "🎟️", "n": "Использовать {} промокодов", "s": "promo_used", "t": [1, 1], "k": 5000},
    {"ic": "⚔️", "n": "Сыграть {} батлов", "s": "battle_play", "t": [2, 4], "k": 5000},
    {"ic": "🔁", "n": "Совершить {} обменов", "s": "trade_done", "t": [1, 2], "k": 5000},
    {"ic": "📅", "n": "Забрать ежедневный бонус", "s": "daily_get", "t": [1, 1], "k": 5000},
    {"ic": "💎", "n": "Открыть {} платных кейсов", "s": "case_paid", "t": [1, 3], "k": 5000},
    {"ic": "⚡", "n": "Сделать {} апгрейдов", "s": "upgrade_all", "t": [1, 3], "k": 5000},
    {"ic": "💣", "n": "Сыграть {} раундов в Mines", "s": "mines_play", "t": [1, 3], "k": 5000},

    # ==== Средние (k=8000) → 80к–120к ====
    {"ic": "🎁", "n": "Открыть {} бесплатных кейсов", "s": "free", "t": [10, 15], "k": 8000},
    {"ic": "🎒", "n": "Получить {} предметов", "s": "got", "t": [10, 15], "k": 8000},
    {"ic": "💰", "n": "Продать {} предметов", "s": "sold", "t": [10, 15], "k": 8000},
    {"ic": "🎡", "n": "Крутить колесо {} раз", "s": "wheel", "t": [10, 15], "k": 8000},
    {"ic": "⚔️", "n": "Сыграть {} батлов", "s": "battle_play", "t": [10, 15], "k": 8000},
    {"ic": "💎", "n": "Открыть {} платных кейсов", "s": "case_paid", "t": [10, 15], "k": 8000},
    {"ic": "⚡", "n": "Сделать {} апгрейдов", "s": "upgrade_all", "t": [10, 15], "k": 8000},
    {"ic": "📜", "n": "Заключить {} контрактов", "s": "ct", "t": [10, 15], "k": 8000},
    {"ic": "🔁", "n": "Совершить {} обменов", "s": "trade_done", "t": [8, 12], "k": 8000},
    {"ic": "🎟️", "n": "Использовать {} промокодов", "s": "promo_used", "t": [3, 5], "k": 8000},
    {"ic": "💣", "n": "Сыграть {} раундов в Mines", "s": "mines_play", "t": [10, 15], "k": 8000},

    # ==== Сложные (k=12000) → 240к–360к ====
    {"ic": "🎁", "n": "Открыть {} бесплатных кейсов", "s": "free", "t": [20, 30], "k": 12000},
    {"ic": "🎒", "n": "Получить {} предметов", "s": "got", "t": [20, 30], "k": 12000},
    {"ic": "💰", "n": "Продать {} предметов", "s": "sold", "t": [20, 30], "k": 12000},
    {"ic": "🎡", "n": "Крутить колесо {} раз", "s": "wheel", "t": [20, 30], "k": 12000},
    {"ic": "⚔️", "n": "Сыграть {} батлов", "s": "battle_play", "t": [20, 30], "k": 12000},
    {"ic": "💎", "n": "Открыть {} платных кейсов", "s": "case_paid", "t": [20, 30], "k": 12000},
    {"ic": "⚡", "n": "Сделать {} апгрейдов", "s": "upgrade_all", "t": [20, 30], "k": 12000},
    {"ic": "📜", "n": "Заключить {} контрактов", "s": "ct", "t": [20, 30], "k": 12000},
    {"ic": "🔥", "n": "Выбить {} предметов дороже 1000 ₽", "s": "big", "t": [20, 30], "k": 12000},
    {"ic": "🎯", "n": "Сыграть {} батлов подряд", "s": "battle_play", "t": [15, 25], "k": 12000},
    {"ic": "💣", "n": "Сыграть {} раундов в Mines", "s": "mines_play", "t": [20, 30], "k": 12000},

    # ==== Эпические (k=40000 / 4000) → 40к–400к ====
    {"ic": "⚡", "n": "Выиграть {} апгрейдов", "s": "upw", "t": [1, 3], "k": 40000},
    {"ic": "🏆", "n": "Выиграть {} батлов", "s": "battle_win", "t": [1, 3], "k": 40000},
    {"ic": "🔪", "n": "Выбить {} ★ редких предметов", "s": "gold", "t": [1, 2], "k": 40000},
    {"ic": "🎯", "n": "Выбить {} номерных скинов", "s": "num_skin", "t": [1, 3], "k": 40000},
    {"ic": "🔥", "n": "Выбить {} «Тайных» предметов", "s": "covert_drop", "t": [1, 2], "k": 40000},
    {"ic": "💎", "n": "Выбить {} «Легендарных» предметов", "s": "legendary_drop", "t": [1, 1], "k": 40000},
    {"ic": "📜", "n": "Победить в {} контрактах подряд", "s": "ct_streak", "t": [2, 3], "k": 40000},
    {"ic": "💣", "n": "Выиграть {} раз в Mines", "s": "mines_win", "t": [1, 3], "k": 40000},
    {"ic": "🎁", "n": "Открыть {} кейсов за день", "s": "case_paid", "t": [50, 100], "k": 4000},
    {"ic": "🎒", "n": "Собрать {} предметов за день", "s": "got", "t": [50, 100], "k": 4000},
    {"ic": "💰", "n": "Продать {} предметов за день", "s": "sold", "t": [50, 100], "k": 4000},
]


def _week_key(day_key):
    dt = datetime.strptime(day_key, "%Y-%m-%d")
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}", iso[2] - 1


def daily_quests(day_key):
    week_str, day_idx = _week_key(day_key)
    rnd = random.Random(week_str)
    shuffled = QUEST_POOL[:]
    rnd.shuffle(shuffled)
    weekly = shuffled[:35]
    day_quests = weekly[day_idx * 5: day_idx * 5 + 5]
    reward_rnd = random.Random(week_str + "-rw")
    out = []
    for i, q in enumerate(day_quests):
        t = reward_rnd.randint(*q["t"])
        raw = t * q["k"] * reward_rnd.uniform(0.9, 1.15)
        r = max(1000, int(raw // 1000 * 1000))
        out.append({
            "id": f"d{i}", "ic": q["ic"], "n": q["n"].format(t),
            "d": "Обновляется каждые 24 часа", "t": t, "s": q["s"], "r": r,
        })
    return out


async def load_state(uid):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT state FROM saves WHERE user_id=$1", uid)
    st = json.loads(row["state"]) if row and row["state"] else default_state("F2P")

    need_regen = (
        st.get("quest_date") != today()
        or int(st.get("quests_v") or 0) != QUESTS_VERSION
    )
    if need_regen:
        st["quest_date"] = today()
        st["quests_v"] = QUESTS_VERSION
        st["daily_quests"] = daily_quests(today())
        st["daily_claimed"] = {}

    if not isinstance(st.get("fav"), list):
        st["fav"] = []
    if not isinstance(st.get("qp"), dict):
        st["qp"] = default_state("F2P")["qp"]
    return st


async def persist(uid, st):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO saves(user_id,state,updated) VALUES($1,$2::jsonb,$3) "
            "ON CONFLICT(user_id) DO UPDATE SET state=$2::jsonb, updated=$3",
            uid, json.dumps(st), time.time())


async def sanitize_state(uid, st):
    cleaned_inv = []
    for o in st.get("inv", []):
        if o.get("st") in ("in", "trade_pending") and not resolve_item(o.get("id")):
            continue
        cleaned_inv.append(o)
    st["inv"] = cleaned_inv

    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT status, cases FROM battles WHERE mode='trade' AND creator=$1", uid)
    pending, done, cancelled = set(), set(), set()
    for r in rows:
        info = _j(r["cases"]) or {}
        uids = info.get("offer", [])
        if r["status"] == "waiting":
            pending.update(uids)
        elif r["status"] == "done":
            done.update(uids)
        elif r["status"] == "cancelled":
            cancelled.update(uids)
    claw = 0
    for o in st.get("inv", []):
        u = o.get("uid")
        if u in done:
            if o.get("st") == "sold":
                it = resolve_item(o.get("id"))
                claw += it["price"] if it else 0
                o["st"] = "traded"
            elif o.get("st") in ("in", "trade_pending"):
                o["st"] = "traded"
        elif u in pending:
            if o.get("st") == "in":
                o["st"] = "trade_pending"
        elif u in cancelled:
            if o.get("st") == "trade_pending":
                o["st"] = "in"
    if claw:
        st["balance"] = max(0, st.get("balance", 0) - claw)
    if not isinstance(st.get("fav"), list):
        st["fav"] = []
    return st


# ============== RATING HELPERS ==============
async def ensure_rating(user_id):
    period = current_rating_period()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT period FROM rating WHERE user_id=$1", user_id)
        if not row:
            await conn.execute(
                "INSERT INTO rating(user_id,period,battle_profit,cases_spent,cases_opened,updated) "
                "VALUES($1,$2,0,0,0,$3) ON CONFLICT (user_id) DO NOTHING",
                user_id, period, time.time())
        elif row["period"] != period:
            await conn.execute(
                "UPDATE rating SET period=$1,battle_profit=0,cases_spent=0,cases_opened=0,updated=$2 "
                "WHERE user_id=$3", period, time.time(), user_id)


async def add_rating(user_id, *, battle_profit=0.0, cases_spent=0.0, cases_opened=0):
    await ensure_rating(user_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE rating SET "
            "battle_profit = battle_profit + $1, "
            "cases_spent = cases_spent + $2, "
            "cases_opened = cases_opened + $3, "
            "updated = $4 WHERE user_id=$5",
            battle_profit, cases_spent, cases_opened, time.time(), user_id)


# ============== MINES HELPERS ==============
def mines_view(row: dict) -> dict:
    revealed = _j(row.get("revealed")) or []
    mines = int(row.get("mines") or 3)
    opened = len(revealed)
    mult = float(row.get("multiplier") or mines_multiplier(mines, opened) or 1.0)
    safe_total = MINES_GRID - mines
    next_mult = mines_multiplier(mines, opened + 1) if opened < safe_total else 0
    status = row.get("status") or "active"

    out = {
        "id": row.get("id"),
        "bet": int(row.get("bet") or 0),
        "mines": mines,
        "revealed": revealed,
        "opened": opened,
        "safe_total": safe_total,
        "multiplier": round(mult, 4),
        "next_multiplier": round(next_mult, 4),
        "payout": int(row.get("payout") or 0),
        "potential": int(round(int(row.get("bet") or 0) * mult)),
        "status": status,
    }
    if status == "lost":
        out["mine_positions"] = _j(row.get("mine_positions")) or []
    return out


class AuthReq(BaseModel):
    nick: str
    password: str
class NickReq(BaseModel):
    nick: str
class BattleReq(BaseModel):
    cases: List[str]
    mode: str = "bot"
    friend: Optional[str] = None
class TradeCreateReq(BaseModel):
    target_nick: str
    offer_items: List[str]
    ask_balance: int = 0
    give_balance: int = 0
class TradeActionReq(BaseModel):
    trade_id: str
class LiveDropReq(BaseModel):
    item_id: str
    num: Optional[int] = None
    case: str = ""
    price: int = 0
class BanReq(BaseModel):
    nick: str
    duration_ms: int = 0
    reason: str = ""
    by: Optional[str] = None
class MinesStartReq(BaseModel):
    bet: int
    mines: int
class MinesRevealReq(BaseModel):
    cell: int


# ==================== AUTH ====================
@app.post("/api/register")
async def register(a: AuthReq):
    if len(a.nick) < 3:
        raise HTTPException(400, "Ник от 3 символов")
    if len(a.password) < 4:
        raise HTTPException(400, "Пароль от 4 символов")
    if a.nick.lower() == ADMIN_NICK and a.password != ADMIN_PASS:
        raise HTTPException(403, "Ник admin зарезервирован")
    ban = await get_active_ban(a.nick)
    if ban:
        raise HTTPException(403, _ban_message(ban))
    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users(nick,pass,created) VALUES($1,$2,$3) RETURNING id",
                a.nick, bcrypt.hash(a.password), time.time())
            await conn.execute(
                "INSERT INTO saves(user_id,state,updated) VALUES($1,$2::jsonb,$3)",
                uid, json.dumps(default_state(a.nick)), time.time())
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "Ник занят")
    adm = a.nick.lower() == ADMIN_NICK
    return {"token": make_token(uid, adm), "nick": a.nick, "admin": adm}


@app.post("/api/login")
async def login(a: AuthReq):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE LOWER(nick)=LOWER($1)", a.nick.strip())
    if not row or not bcrypt.verify(a.password, row["pass"]):
        raise HTTPException(403, "Неверный ник или пароль")
    ban = await get_active_ban(row["nick"])
    if ban:
        raise HTTPException(403, _ban_message(ban))
    adm = row["nick"].lower() == ADMIN_NICK
    return {"token": make_token(row["id"], adm), "nick": row["nick"], "admin": adm}


# ==================== STATE ====================
@app.get("/api/state")
async def get_state(user=Depends(get_user)):
    st = await load_state(user["id"])
    st = await sanitize_state(user["id"], st)
    await persist(user["id"], st)
    return st


@app.post("/api/state")
async def put_state(state: dict = Body(...), user=Depends(get_user)):
    old = await load_state(user["id"])

    state["daily_quests"] = old.get("daily_quests", [])
    state["quest_date"] = old.get("quest_date")
    state["quests_v"] = old.get("quests_v", QUESTS_VERSION)

    srv_claimed = old.get("daily_claimed", {}) or {}
    cli_claimed = state.get("daily_claimed", {}) or {}
    merged = dict(srv_claimed)
    merged.update(cli_claimed)
    state["daily_claimed"] = merged

    state["fee_log"] = old.get("fee_log", [])

    state = await sanitize_state(user["id"], state)
    await persist(user["id"], state)

    try:
        os_ = float(old.get("stats", {}).get("spent", 0) or 0)
        oo_ = int(old.get("stats", {}).get("opened", 0) or 0)
        ns_ = float(state.get("stats", {}).get("spent", 0) or 0)
        no_ = int(state.get("stats", {}).get("opened", 0) or 0)
        ds = max(0.0, ns_ - os_)
        do = max(0, no_ - oo_)
        if ds > 0 or do > 0:
            await add_rating(user["id"], cases_spent=ds, cases_opened=do)
    except Exception:
        pass
    return {"ok": True, "ts": time.time()}


@app.post("/api/quests/{qid}/claim")
async def claim_quest(qid: str, user=Depends(get_user)):
    st = await load_state(user["id"])
    if qid in st.get("daily_claimed", {}):
        raise HTTPException(400, "Уже получено")
    q = next((x for x in st.get("daily_quests", []) if x["id"] == qid), None)
    if not q:
        raise HTTPException(404, "Нет такого задания")
    if st["qp"].get(q["s"], 0) < q["t"]:
        raise HTTPException(400, "Ещё не выполнено")
    st.setdefault("daily_claimed", {})[qid] = 1
    st["balance"] += q["r"]
    st["stats"]["earned"] = st["stats"].get("earned", 0) + q["r"]
    await persist(user["id"], st)
    return {"ok": True, "balance": st["balance"], "reward": q["r"]}


@app.post("/api/ping")
async def ping(user=Depends(get_user)):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO presence(user_id,last_seen) VALUES($1,$2) "
            "ON CONFLICT(user_id) DO UPDATE SET last_seen=$2",
            user["id"], time.time())
        row = await conn.fetchrow("SELECT updated FROM saves WHERE user_id=$1", user["id"])
    return {"ok": True, "state_ts": row["updated"] if row else 0}


# ==================== EVENT ====================
@app.get("/api/event")
async def event_status():
    wk = is_weekend()
    return {
        "version": VERSION,
        "codename": CODENAME,
        "weekend": wk,
        "mult": WEEKEND_MULT if wk else 1.0,
        "rating_period": current_rating_period(),
        "trade_fee": TRADE_FEE,
        "trade_fee_to": ADMIN_NICK,
        "mines": {
            "grid": MINES_GRID,
            "valid_mines": MINES_VALID,
            "min_bet": MINES_MIN_BET,
            "max_bet": MINES_MAX_BET,
        },
    }


# ==================== MINES ====================
@app.get("/api/mines/current")
async def mines_current(user=Depends(get_user)):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM mines_games WHERE user_id=$1 AND status='active' "
            "ORDER BY created DESC LIMIT 1", user["id"])
    if not row:
        return {"active": False}
    return {"active": True, "game": mines_view(dict(row))}


@app.post("/api/mines/start")
async def mines_start(r: MinesStartReq, user=Depends(get_user)):
    if r.mines not in MINES_VALID:
        raise HTTPException(400, f"Число мин должно быть одно из: {MINES_VALID}")
    if r.bet < MINES_MIN_BET:
        raise HTTPException(400, f"Минимальная ставка {MINES_MIN_BET} ₽")
    if r.bet > MINES_MAX_BET:
        raise HTTPException(400, f"Максимальная ставка {MINES_MAX_BET} ₽")

    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM mines_games WHERE user_id=$1 AND status='active' LIMIT 1",
            user["id"])
    if exists:
        raise HTTPException(400, "У тебя уже есть активная игра. Забери или доиграй её.")

    st = await load_state(user["id"])
    if st["balance"] < r.bet:
        raise HTTPException(400, "Не хватает ₽ на ставку")
    st["balance"] -= r.bet
    st["stats"]["mines_spent"] = st["stats"].get("mines_spent", 0) + r.bet
    st["stats"]["spent"] = st["stats"].get("spent", 0) + r.bet
    st["qp"]["mines_play"] = st["qp"].get("mines_play", 0) + 1
    await persist(user["id"], st)

    # Генерируем позиции мин
    positions = random.sample(range(MINES_GRID), r.mines)
    gid = "m" + uuid.uuid4().hex[:12]

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO mines_games(id,user_id,bet,mines,mine_positions,revealed,multiplier,status,created) "
            "VALUES($1,$2,$3,$4,$5::jsonb,'[]'::jsonb,1,'active',$6)",
            gid, user["id"], r.bet, r.mines, json.dumps(positions), time.time())
        row = await conn.fetchrow("SELECT * FROM mines_games WHERE id=$1", gid)

    return {"ok": True, "balance": st["balance"], "game": mines_view(dict(row))}


@app.post("/api/mines/reveal")
async def mines_reveal(r: MinesRevealReq, user=Depends(get_user)):
    if not (0 <= r.cell < MINES_GRID):
        raise HTTPException(400, "Клетка вне поля")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM mines_games WHERE user_id=$1 AND status='active' "
            "ORDER BY created DESC LIMIT 1", user["id"])
        if not row:
            raise HTTPException(400, "Нет активной игры")

        revealed = _j(row["revealed"]) or []
        if r.cell in revealed:
            raise HTTPException(400, "Клетка уже открыта")

        positions = _j(row["mine_positions"]) or []
        bet = int(row["bet"])
        mines = int(row["mines"])

        if r.cell in positions:
            # Проигрыш
            await conn.execute(
                "UPDATE mines_games SET status='lost', finished=$1, revealed=$2::jsonb WHERE id=$3",
                time.time(), json.dumps(revealed), row["id"])
            row = await conn.fetchrow("SELECT * FROM mines_games WHERE id=$1", row["id"])
            st = await load_state(user["id"])
            return {"ok": True, "hit_mine": True, "game": mines_view(dict(row)), "balance": st["balance"]}

        revealed.append(r.cell)
        opened = len(revealed)
        safe_total = MINES_GRID - mines
        mult = mines_multiplier(mines, opened)

        if opened >= safe_total:
            # Все безопасные открыты — авто-забор
            payout = int(round(bet * mult))
            await conn.execute(
                "UPDATE mines_games SET status='won', finished=$1, revealed=$2::jsonb, "
                "multiplier=$3, payout=$4 WHERE id=$5",
                time.time(), json.dumps(revealed), mult, payout, row["id"])
            st = await load_state(user["id"])
            st["balance"] += payout
            st["stats"]["mines_earned"] = st["stats"].get("mines_earned", 0) + payout
            st["stats"]["earned"] = st["stats"].get("earned", 0) + payout
            st["qp"]["mines_win"] = st["qp"].get("mines_win", 0) + 1
            await persist(user["id"], st)
            row = await conn.fetchrow("SELECT * FROM mines_games WHERE id=$1", row["id"])
            return {"ok": True, "hit_mine": False, "auto_cashout": True,
                    "game": mines_view(dict(row)), "balance": st["balance"]}

        await conn.execute(
            "UPDATE mines_games SET revealed=$1::jsonb, multiplier=$2 WHERE id=$3",
            json.dumps(revealed), mult, row["id"])
        row = await conn.fetchrow("SELECT * FROM mines_games WHERE id=$1", row["id"])

    st = await load_state(user["id"])
    return {"ok": True, "hit_mine": False, "game": mines_view(dict(row)), "balance": st["balance"]}


@app.post("/api/mines/cashout")
async def mines_cashout(user=Depends(get_user)):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM mines_games WHERE user_id=$1 AND status='active' "
            "ORDER BY created DESC LIMIT 1", user["id"])
        if not row:
            raise HTTPException(400, "Нет активной игры")
        revealed = _j(row["revealed"]) or []
        if not revealed:
            raise HTTPException(400, "Сначала открой хотя бы одну клетку")
        bet = int(row["bet"])
        mult = float(row["multiplier"] or 1)
        payout = int(round(bet * mult))
        await conn.execute(
            "UPDATE mines_games SET status='won', finished=$1, payout=$2 WHERE id=$3",
            time.time(), payout, row["id"])

    st = await load_state(user["id"])
    st["balance"] += payout
    st["stats"]["mines_earned"] = st["stats"].get("mines_earned", 0) + payout
    st["stats"]["earned"] = st["stats"].get("earned", 0) + payout
    st["qp"]["mines_win"] = st["qp"].get("mines_win", 0) + 1
    await persist(user["id"], st)

    return {"ok": True, "payout": payout, "multiplier": round(mult, 4), "balance": st["balance"]}


@app.post("/api/mines/abandon")
async def mines_abandon(user=Depends(get_user)):
    """Сбросить активную игру (для отладки). Ставка не возвращается."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE mines_games SET status='lost', finished=$1 "
            "WHERE user_id=$2 AND status='active'",
            time.time(), user["id"])
    return {"ok": True}


# ==================== RATING ====================
@app.get("/api/rating/leaderboard")
async def rating_leaderboard(limit: int = 50, period: str = ""):
    p = (period or "").strip() or current_rating_period()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.id, u.nick, r.battle_profit, r.cases_spent, r.cases_opened
            FROM rating r JOIN users u ON u.id = r.user_id
            WHERE r.period = $1
              AND LOWER(u.nick) <> ALL($2::text[])
            ORDER BY (COALESCE(r.battle_profit,0) + COALESCE(r.cases_spent,0)) DESC
            LIMIT $3
        """, p, list(RATING_EXCLUDED_NICKS), max(1, min(int(limit), 200)))
    return {
        "period": p,
        "entries": [
            {"id": r["id"], "nick": r["nick"],
             "battle_profit": int(r["battle_profit"] or 0),
             "cases_spent": int(r["cases_spent"] or 0),
             "cases_opened": int(r["cases_opened"] or 0),
             "score": int((r["battle_profit"] or 0) + (r["cases_spent"] or 0))}
            for r in rows
        ]
    }


@app.get("/api/rating/periods")
async def rating_periods(limit: int = 12):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT period FROM rating
            WHERE period IS NOT NULL
            ORDER BY period DESC LIMIT $1
        """, max(1, min(int(limit), 36)))
    periods = [r["period"] for r in rows]
    cur = current_rating_period()
    if cur not in periods:
        periods.insert(0, cur)
    return {"current": cur, "periods": periods}


@app.get("/api/rating/me")
async def rating_me(user=Depends(get_user)):
    await ensure_rating(user["id"])
    period = current_rating_period()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT battle_profit, cases_spent, cases_opened FROM rating
            WHERE user_id=$1 AND period=$2
        """, user["id"], period)
        rank = await conn.fetchval("""
            SELECT COUNT(*)+1 FROM rating r JOIN users u ON u.id = r.user_id
            WHERE r.period=$1
              AND LOWER(u.nick) <> ALL($3::text[])
              AND (COALESCE(r.battle_profit,0)+COALESCE(r.cases_spent,0)) >
                (SELECT COALESCE(battle_profit,0)+COALESCE(cases_spent,0)
                 FROM rating WHERE user_id=$2 AND period=$1)
        """, period, user["id"], list(RATING_EXCLUDED_NICKS))
    bp = int((row["battle_profit"] if row else 0) or 0)
    cs = int((row["cases_spent"] if row else 0) or 0)
    co = int((row["cases_opened"] if row else 0) or 0)
    return {"period": period, "battle_profit": bp, "cases_spent": cs,
            "cases_opened": co, "score": bp + cs, "rank": int(rank or 0)}


# ==================== LIVE DROPS ====================
@app.post("/api/live/drop")
async def push_live_drop(r: LiveDropReq, user=Depends(get_user)):
    price = max(0, int(r.price or 0))
    num = r.num if (r.num and 1 <= r.num <= 100) else None
    now = time.time()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO live_drops(nick,item_id,num,case_name,price,ts) "
            "VALUES($1,$2,$3,$4,$5,$6)",
            user["nick"], r.item_id, num, r.case[:64], price, now)
        await conn.execute("DELETE FROM live_drops WHERE ts < $1", now - 3 * 3600)
    return {"ok": True}


@app.get("/api/live/drops")
async def get_live_drops(since: float = 0):
    cutoff = since if since > 0 else (time.time() - 2 * 3600)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT nick,item_id,num,case_name,price,ts "
            "FROM live_drops WHERE ts >= $1 ORDER BY ts DESC LIMIT 500", cutoff)
    return [{"nick": r["nick"], "item_id": r["item_id"], "num": r["num"],
             "case": r["case_name"] or "", "price": r["price"], "ts": r["ts"]} for r in rows]


# ==================== FRIENDS ====================
@app.post("/api/friends")
async def add_friend(r: NickReq, user=Depends(get_user)):
    nick = r.nick.strip()
    if not nick:
        raise HTTPException(400, "Пустой ник")
    async with pool.acquire() as conn:
        f = await conn.fetchrow("SELECT id FROM users WHERE LOWER(nick)=LOWER($1)", nick)
        if not f:
            raise HTTPException(404, "Игрок не найден")
        if f["id"] == user["id"]:
            raise HTTPException(400, "Нельзя добавить себя")
        await conn.execute("INSERT INTO friends(user_id,friend_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
                           user["id"], f["id"])
    return {"ok": True}


@app.get("/api/friends")
async def list_friends(user=Depends(get_user)):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.id,u.nick,p.last_seen FROM friends f
            JOIN users u ON u.id=f.friend_id
            LEFT JOIN presence p ON p.user_id=u.id
            WHERE f.user_id=$1 ORDER BY u.nick
        """, user["id"])
    now = time.time()
    return [{"id": r["id"], "nick": r["nick"],
             "online": bool(r["last_seen"] and now - r["last_seen"] < 40)} for r in rows]


# ==================== BATTLES ====================
BOT_NAMES = ["BattleBot_3000", "Железный", "Skynet", "КиберВолк", "GLaDOS", "R2D2", "МегаБот", "X-500"]


def roll_item(case):
    r, acc, chosen = random.random(), 0, list(case["w"].keys())[-1]
    for k, ch in case["w"].items():
        acc += ch
        if r <= acc:
            chosen = k
            break
    pool_items = [i for i in case["items"] if ITEMS.get(i, {}).get("rar") == chosen]
    return random.choice(pool_items or case["items"])


def simulate(cases, players):
    mult = WEEKEND_MULT if is_weekend() else 1.0
    res = {}
    for pl in players:
        drops = [roll_item(CASES[c]) for c in cases if c in CASES]
        total = sum((resolve_item(d) or {}).get("price", 0) for d in drops)
        res[str(pl["id"])] = {"nick": pl.get("nick", "?"), "drops": drops,
                              "total": int(round(total * mult))}
    winner = max(res, key=lambda k: res[k]["total"])
    return {"winner": winner, "claimed": [], "res": res, "weekend_mult": mult}


def battle_view(b):
    cases = _j(b["cases"])
    players = _j(b["players"]) or []
    results = _j(b["results"])
    if b["mode"] == "trade":
        case_list, entry = [], 0
    else:
        ids = cases if isinstance(cases, list) else []
        case_list = [{"id": c, "name": CASES[c]["name"], "price": CASES[c]["price"]}
                     for c in ids if c in CASES]
        entry = sum(CASES[c]["price"] for c in ids if c in CASES)
    return {"id": b["id"], "mode": b["mode"], "status": b["status"], "creator": b["creator"],
            "target": b["target"], "cases": case_list, "entry": entry,
            "players": players, "results": results, "created": b["created"]}


@app.post("/api/battles")
async def create_battle(r: BattleReq, user=Depends(get_user)):
    if not r.cases or len(r.cases) > 10:
        raise HTTPException(400, "От 1 до 10 кейсов")
    for cid in r.cases:
        if cid not in CASES:
            raise HTTPException(400, "Неизвестный кейс")
    entry = sum(CASES[c]["price"] for c in r.cases)
    st = await load_state(user["id"])
    if st["balance"] < entry:
        raise HTTPException(400, "Не хватает ₽ на вход")
    st["balance"] -= entry
    st["stats"]["spent"] = st["stats"].get("spent", 0) + entry
    await persist(user["id"], st)
    try:
        await add_rating(user["id"], battle_profit=-entry)
    except Exception:
        pass
    players = [{"id": user["id"], "nick": user["nick"], "ready": True, "paid": True}]
    status, results = "waiting", None
    if r.mode == "bot":
        players.append({"id": "bot", "nick": random.choice(BOT_NAMES),
                        "bot": True, "ready": True, "paid": True})
        results = simulate(r.cases, players)
        status = "done"
    bid = uuid.uuid4().hex[:10]
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO battles(id,creator,mode,target,cases,status,players,results,members,created) "
            "VALUES($1,$2,$3,$4,$5::jsonb,$6,$7::jsonb,$8::jsonb,$9,$10)",
            bid, user["id"], r.mode, r.friend, json.dumps(r.cases), status,
            json.dumps(players), json.dumps(results), str(user["id"]), time.time())
    return {"id": bid, "status": status, "results": results}


@app.get("/api/battles")
async def list_battles(user=Depends(get_user)):
    async with pool.acquire() as conn:
        avail = await conn.fetch("""
            SELECT * FROM battles WHERE status='waiting' AND mode IN ('bot','friend','public')
            AND creator!=$1 AND (mode='public' OR (mode='friend' AND LOWER(target)=LOWER($2)))
        """, user["id"], user["nick"])
        mine = await conn.fetch("""
            SELECT * FROM battles WHERE mode!='trade'
            AND (creator=$1 OR ','||members||',' LIKE $2)
        """, user["id"], f"%,{user['id']},%")
    seen, out = set(), []
    for b in list(avail) + list(mine):
        if b["id"] in seen:
            continue
        seen.add(b["id"])
        out.append(battle_view(dict(b)))
    out.sort(key=lambda x: x["created"], reverse=True)
    return out


@app.post("/api/battles/{bid}/join")
async def join_battle(bid: str, user=Depends(get_user)):
    async with pool.acquire() as conn:
        b = await conn.fetchrow("SELECT * FROM battles WHERE id=$1", bid)
        if not b or b["status"] != "waiting" or b["mode"] == "trade":
            raise HTTPException(400, "Батл недоступен")
        players = _j(b["players"]) or []
        if any(str(p.get("id")) == str(user["id"]) for p in players):
            return {"ok": True, "status": b["status"], "results": _j(b["results"])}
        if len(players) >= 2:
            raise HTTPException(400, "Батл занят")
        cases = _j(b["cases"]) or []
        entry = sum(CASES[c]["price"] for c in cases if c in CASES)
        st = await load_state(user["id"])
        if st["balance"] < entry:
            raise HTTPException(400, f"Не хватает ₽ на вход ({entry})")
        st["balance"] -= entry
        st["stats"]["spent"] = st["stats"].get("spent", 0) + entry
        await persist(user["id"], st)
        try:
            await add_rating(user["id"], battle_profit=-entry)
        except Exception:
            pass
        players.append({"id": user["id"], "nick": user["nick"], "ready": True, "paid": True})
        status, results = "waiting", None
        if all(p.get("ready") for p in players):
            results = simulate(cases, players)
            status = "done"
        members = ",".join(str(p["id"]) for p in players)
        await conn.execute(
            "UPDATE battles SET players=$1::jsonb, status=$2, results=$3::jsonb, members=$4 WHERE id=$5",
            json.dumps(players), status, json.dumps(results), members, bid)
    return {"ok": True, "status": status, "results": results}


@app.post("/api/battles/{bid}/claim")
async def claim_battle(bid: str, user=Depends(get_user)):
    async with pool.acquire() as conn:
        b = await conn.fetchrow("SELECT * FROM battles WHERE id=$1", bid)
        if not b or b["status"] != "done":
            raise HTTPException(400, "Батл не завершён")
        results = _j(b["results"]) or {}
        uid = str(user["id"])
        if str(results.get("winner")) != uid:
            raise HTTPException(400, "Вы проиграли этот батл")
        if uid in results.get("claimed", []):
            raise HTTPException(400, "Уже забрано")
        st = await load_state(user["id"])
        now = int(time.time() * 1000)
        total_value = 0
        for pid, r in (results.get("res") or {}).items():
            for iid in r.get("drops", []):
                it = resolve_item(iid)
                if not it:
                    continue
                total_value += it["price"]
                st["inv"].insert(0, {"uid": "b" + uuid.uuid4().hex[:8], "id": iid,
                                     "src": "Батл", "ts": now, "st": "in"})
                st["hist"].insert(0, {"id": iid, "ts": now, "src": "Батл", "price": it["price"]})
                st["stats"]["won"] = st["stats"].get("won", 0) + it["price"]
        st["hist"] = st["hist"][:150]
        results.setdefault("claimed", []).append(uid)
        await conn.execute("UPDATE battles SET results=$1::jsonb WHERE id=$2", json.dumps(results), bid)
        await persist(user["id"], st)
    try:
        mult = WEEKEND_MULT if is_weekend() else 1.0
        await add_rating(user["id"], battle_profit=total_value * mult)
    except Exception:
        pass
    return {"ok": True, "balance": st["balance"]}


# ==================== TRADES ====================
def trade_view(b):
    d = battle_view(b)
    d["trade_info"] = _j(b["cases"]) or {}
    return d


@app.post("/api/trades")
async def create_trade(r: TradeCreateReq, user=Depends(get_user)):
    if not r.offer_items and r.give_balance <= 0:
        raise HTTPException(400, "Обмен пустой — добавь предметы или деньги")
    if r.ask_balance < 0 or r.give_balance < 0:
        raise HTTPException(400, "Сумма не может быть отрицательной")
    nick = r.target_nick.strip()
    async with pool.acquire() as conn:
        t = await conn.fetchrow("SELECT id,nick FROM users WHERE LOWER(nick)=LOWER($1)", nick)
        if not t:
            raise HTTPException(404, "Игрок не найден")
        if t["id"] == user["id"]:
            raise HTTPException(400, "Нельзя обменяться с собой")
        st = await load_state(user["id"])
        if r.give_balance > 0 and st.get("balance", 0) < r.give_balance:
            raise HTTPException(400, f"Не хватает ₽ для передачи ({r.give_balance})")
        if r.give_balance > 0:
            st["balance"] = st.get("balance", 0) - r.give_balance
        fav = set(st.get("fav") or [])
        details = []
        for u in r.offer_items:
            o = next((x for x in st["inv"] if x["uid"] == u and x["st"] == "in"), None)
            if not o:
                raise HTTPException(400, "Предмет недоступен")
            if u in fav:
                raise HTTPException(400, "Избранное нельзя обменять")
            it = resolve_item(o["id"])
            if not it:
                raise HTTPException(400, "Предмет не найден")
            details.append({"uid": u, "id": o["id"], "name": it["name"], "price": it["price"]})
        for u in r.offer_items:
            o = next((x for x in st["inv"] if x["uid"] == u), None)
            if o:
                o["st"] = "trade_pending"
        await persist(user["id"], st)
        tid = "t" + uuid.uuid4().hex[:10]
        payload = {"offer": r.offer_items, "offer_details": details,
                   "ask_balance": r.ask_balance, "give_balance": r.give_balance,
                   "owner": user["id"], "owner_nick": user["nick"]}
        await conn.execute(
            "INSERT INTO battles(id,creator,mode,target,cases,status,players,results,members,created) "
            "VALUES($1,$2,'trade',$3,$4::jsonb,'waiting',$5::jsonb,$6::jsonb,$7,$8)",
            tid, user["id"], t["nick"], json.dumps(payload),
            json.dumps([{"id": user["id"], "nick": user["nick"]},
                        {"id": t["id"], "nick": t["nick"]}]),
            json.dumps(None), f"{user['id']},{t['id']}", time.time())
    return {"id": tid}


@app.get("/api/trades")
async def list_trades(user=Depends(get_user)):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM battles WHERE mode='trade'
            AND (creator=$1 OR ','||members||',' LIKE $2) ORDER BY created DESC LIMIT 30
        """, user["id"], f"%,{user['id']},%")
    return [trade_view(dict(b)) for b in rows]


@app.post("/api/trades/accept")
async def accept_trade(r: TradeActionReq, user=Depends(get_user)):
    async with pool.acquire() as conn:
        b = await conn.fetchrow("SELECT * FROM battles WHERE id=$1 AND mode='trade'", r.trade_id)
        if not b or b["status"] != "waiting":
            raise HTTPException(400, "Обмен недоступен")
        if b["creator"] == user["id"]:
            raise HTTPException(400, "Это твой собственный обмен")
        info = _j(b["cases"]) or {}
        ask = int(info.get("ask_balance", 0))
        give = int(info.get("give_balance", 0))
        owner_id = int(info.get("owner", b["creator"]))
        owner = await conn.fetchrow("SELECT * FROM users WHERE id=$1", owner_id)
        if not owner:
            raise HTTPException(400, "Создатель обмена не найден")
        owner_st = await load_state(owner_id)
        my_st = await load_state(user["id"])
        if my_st["balance"] < ask:
            raise HTTPException(400, f"Не хватает ₽ (нужно {ask})")
        for u in info.get("offer", []):
            o = next((x for x in owner_st["inv"] if x["uid"] == u and x["st"] in ("in", "trade_pending")), None)
            if not o:
                raise HTTPException(400, "Предметов обмена уже нет у отправителя")
        now = int(time.time() * 1000)
        for u in info.get("offer", []):
            o = next((x for x in owner_st["inv"] if x["uid"] == u), None)
            if not o:
                continue
            it = resolve_item(o["id"])
            o["st"] = "traded"
            my_st["inv"].insert(0, {"uid": "tr" + uuid.uuid4().hex[:8], "id": o["id"],
                                    "src": f"Обмен от {owner['nick']}", "ts": now, "st": "in"})
            my_st["hist"].insert(0, {"id": o["id"], "ts": now, "src": "Обмен",
                                     "price": it["price"] if it else 0})

        ask_received = int(ask * (1 - TRADE_FEE))
        give_received = int(give * (1 - TRADE_FEE))
        ask_fee  = ask  - ask_received
        give_fee = give - give_received
        total_fee = ask_fee + give_fee

        if give > 0:
            my_st["balance"] += give_received
            my_st["stats"]["earned"] = my_st["stats"].get("earned", 0) + give_received
        my_st["balance"] -= ask
        my_st["stats"]["spent"] = my_st["stats"].get("spent", 0) + ask

        if ask > 0:
            owner_st["balance"] += ask_received
            owner_st["stats"]["earned"] = owner_st["stats"].get("earned", 0) + ask_received
        owner_st["balance"] -= give
        owner_st["stats"]["spent"] = owner_st["stats"].get("spent", 0) + give

        my_st["hist"] = my_st["hist"][:150]
        await persist(owner_id, owner_st)
        await persist(user["id"], my_st)

        if total_fee > 0:
            try:
                await credit_admin_fee(total_fee, f"Обмен #{r.trade_id}")
            except Exception:
                pass

        await conn.execute("UPDATE battles SET status='done', results=$1::jsonb WHERE id=$2",
                           json.dumps({
                               "accepted_by": user["id"],
                               "ask": ask, "give": give,
                               "ask_received": ask_received, "give_received": give_received,
                               "ask_fee": ask_fee, "give_fee": give_fee,
                               "total_fee": total_fee,
                               "fee_to": ADMIN_NICK,
                               "fee_pct": TRADE_FEE,
                           }), r.trade_id)
    return {"ok": True}


@app.post("/api/trades/cancel")
async def cancel_trade(r: TradeActionReq, user=Depends(get_user)):
    async with pool.acquire() as conn:
        b = await conn.fetchrow("SELECT * FROM battles WHERE id=$1 AND mode='trade'", r.trade_id)
        if not b:
            raise HTTPException(404, "Обмен не найден")
        if b["creator"] != user["id"]:
            raise HTTPException(403, "Отменить может только создатель")
        if b["status"] != "waiting":
            raise HTTPException(400, "Обмен уже закрыт")
        await conn.execute("UPDATE battles SET status='cancelled' WHERE id=$1", r.trade_id)
    info = _j(b["cases"]) or {}
    st = await load_state(user["id"])
    give = int(info.get("give_balance", 0))
    if give > 0:
        st["balance"] = st.get("balance", 0) + give
    for u in info.get("offer", []):
        o = next((x for x in st["inv"] if x["uid"] == u), None)
        if o and o["st"] == "trade_pending":
            o["st"] = "in"
    await persist(user["id"], st)
    return {"ok": True}


# ==================== PROMO ====================
@app.post("/api/promo/redeem")
async def redeem_promo(r: dict = Body(...), user=Depends(get_user)):
    code = (r.get("code") or "").strip().upper()
    if not code:
        raise HTTPException(400, "Пустой код")
    async with pool.acquire() as conn:
        promo = await conn.fetchrow("SELECT * FROM promos WHERE code=$1", code)
        if not promo:
            raise HTTPException(404, "Промокод не найден")
        used = await conn.fetchval("SELECT 1 FROM promo_uses WHERE user_id=$1 AND code=$2",
                                   user["id"], code)
        if used:
            raise HTTPException(400, "Вы уже использовали этот промокод")
        if promo["max_uses"] > 0 and promo["uses"] >= promo["max_uses"]:
            raise HTTPException(400, "Промокод исчерпан")
        await conn.execute("INSERT INTO promo_uses(user_id,code,used_at) VALUES($1,$2,$3)",
                           user["id"], code, time.time())
        await conn.execute("UPDATE promos SET uses=uses+1 WHERE code=$1", code)
    st = await load_state(user["id"])
    payload = _j(promo["payload"]) or {}
    ptype = promo["type"]
    now = int(time.time() * 1000)
    out = {"ok": True, "type": ptype, "balance": 0, "tokens": 0, "items": []}
    if ptype == "balance":
        payload = {"balance": payload.get("amount", 0)}
    elif ptype == "tokens":
        payload = {"tokens": payload.get("amount", 0)}
    elif ptype == "items":
        payload = {"cases": [{"id": "__explicit__", "n": 0, "items": payload.get("items", [])}]}
    bal = int(payload.get("balance", 0) or 0)
    if bal > 0:
        st["balance"] += bal
        st["stats"]["earned"] = st["stats"].get("earned", 0) + bal
        out["balance"] = bal
    tok = int(payload.get("tokens", 0) or 0)
    if tok > 0:
        st["tokens"] = st.get("tokens", 0) + tok
        out["tokens"] = tok
    cases = payload.get("cases") or []
    unresolved_cases = []
    for c in cases:
        cid = c.get("id")
        n = int(c.get("n", 0) or 0)
        if not cid or n <= 0:
            continue
        if cid == "__explicit__":
            for iid in (c.get("items") or []):
                it = resolve_item(iid)
                if not it:
                    continue
                st["inv"].insert(0, {"uid": "pr" + uuid.uuid4().hex[:8], "id": iid,
                                     "src": f"Промокод {code}", "ts": now, "st": "in"})
                st["hist"].insert(0, {"id": iid, "ts": now, "src": "Промокод", "price": it["price"]})
                out["items"].append(iid)
            continue
        case = CASES.get(cid)
        if not case:
            unresolved_cases.append({"id": cid, "n": n})
            continue
        for _ in range(n):
            iid = roll_item(case)
            it = resolve_item(iid)
            if not it:
                continue
            st["inv"].insert(0, {"uid": "pr" + uuid.uuid4().hex[:8], "id": iid,
                                 "src": f"Промокод {code}", "ts": now, "st": "in"})
            st["hist"].insert(0, {"id": iid, "ts": now, "src": "Промокод", "price": it["price"]})
            out["items"].append(iid)
    out["unresolved_cases"] = unresolved_cases
    st["hist"] = st["hist"][:500]
    await persist(user["id"], st)
    return out


# ==================== ADMIN ====================
@app.get("/api/admin/users")
async def admin_users(q: str = "", user=Depends(require_admin)):
    async with pool.acquire() as conn:
        if q:
            rows = await conn.fetch(
                "SELECT id,nick,created FROM users WHERE LOWER(nick) LIKE LOWER($1) "
                "ORDER BY id DESC LIMIT 100", f"%{q}%")
        else:
            rows = await conn.fetch("SELECT id,nick,created FROM users ORDER BY id DESC LIMIT 100")
    return [{"id": r["id"], "nick": r["nick"], "created": r["created"]} for r in rows]


@app.post("/api/admin/give")
async def admin_give(r: dict = Body(...), user=Depends(require_admin)):
    nick = (r.get("nick") or "").strip()
    if not nick:
        raise HTTPException(400, "Укажи ник")
    balance = int(r.get("balance", 0) or 0)
    tokens = int(r.get("tokens", 0) or 0)
    item_ids = r.get("items") or []
    async with pool.acquire() as conn:
        target = await conn.fetchrow("SELECT id,nick FROM users WHERE LOWER(nick)=LOWER($1)", nick)
        if not target:
            raise HTTPException(404, "Игрок не найден")
        row = await conn.fetchrow("SELECT state FROM saves WHERE user_id=$1", target["id"])
        state = json.loads(row["state"]) if row and row["state"] else default_state(target["nick"])
        state.setdefault("balance", 0); state.setdefault("tokens", 0)
        state.setdefault("inv", []); state.setdefault("hist", []); state.setdefault("fav", [])
        state["balance"] += balance
        state["tokens"] += tokens
        now = int(time.time() * 1000)
        for iid in item_ids:
            it = resolve_item(iid)
            if not it:
                continue
            state["inv"].insert(0, {"uid": "adm" + uuid.uuid4().hex[:8], "id": iid,
                                    "src": "От админа", "ts": now, "st": "in"})
            state["hist"].insert(0, {"id": iid, "ts": now, "src": "От админа", "price": it["price"]})
        await conn.execute("UPDATE saves SET state=$1::jsonb, updated=$2 WHERE user_id=$3",
                           json.dumps(state), time.time(), target["id"])
    return {"ok": True, "nick": target["nick"], "balance": balance,
            "tokens": tokens, "items": len(item_ids)}


@app.post("/api/admin/rating-award")
async def admin_rating_award(r: dict = Body(...), admin=Depends(require_admin)):
    place = int(r.get("place", 0))
    balance = int(r.get("balance", 0) or 0)
    tokens = int(r.get("tokens", 0) or 0)
    items = r.get("items") or []
    period = (r.get("period") or "").strip() or current_rating_period()
    if place not in (1, 2, 3):
        raise HTTPException(400, "Место должно быть 1, 2 или 3")
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.id, u.nick FROM rating r JOIN users u ON u.id = r.user_id
            WHERE r.period = $1
              AND LOWER(u.nick) <> ALL($2::text[])
            ORDER BY (COALESCE(r.battle_profit,0) + COALESCE(r.cases_spent,0)) DESC
            LIMIT 3
        """, period, list(RATING_EXCLUDED_NICKS))
        if len(rows) < place:
            raise HTTPException(400, f"Нет игрока на {place} месте в периоде {period}")
        target = rows[place - 1]
        row = await conn.fetchrow("SELECT state FROM saves WHERE user_id=$1", target["id"])
        state = json.loads(row["state"]) if row and row["state"] else default_state(target["nick"])
        state.setdefault("balance", 0); state.setdefault("tokens", 0)
        state.setdefault("inv", []); state.setdefault("hist", []); state.setdefault("fav", [])
        state["balance"] += balance
        state["tokens"] += tokens
        now = int(time.time() * 1000)
        for iid in items:
            it = resolve_item(iid)
            if not it:
                continue
            state["inv"].insert(0, {"uid": "aw" + uuid.uuid4().hex[:8], "id": iid,
                                    "src": f"Топ-{place} рейтинга ({period})", "ts": now, "st": "in"})
            state["hist"].insert(0, {"id": iid, "ts": now, "src": "Рейтинг-приз", "price": it["price"]})
        await conn.execute("UPDATE saves SET state=$1::jsonb, updated=$2 WHERE user_id=$3",
                           json.dumps(state), time.time(), target["id"])
    return {"ok": True, "place": place, "nick": target["nick"], "period": period}


@app.get("/api/admin/fees")
async def admin_fees(user=Depends(require_admin)):
    admin_id = await get_admin_id()
    if not admin_id:
        return {"total": 0, "last30": 0, "log": []}
    st = await load_state(admin_id)
    log = st.get("fee_log", []) or []
    cutoff = (time.time() - 30 * 86400) * 1000
    total = sum(int(e.get("amount", 0)) for e in log)
    last30 = sum(int(e.get("amount", 0)) for e in log if e.get("ts", 0) >= cutoff)
    return {"total": total, "last30": last30, "log": log[:100]}


@app.post("/api/admin/promo")
async def admin_create_promo(r: dict = Body(...), user=Depends(require_admin)):
    code = (r.get("code") or "").strip().upper()
    if not code or len(code) < 3:
        raise HTTPException(400, "Код не менее 3 символов")
    payload = r.get("payload") or {}
    ptype = r.get("type", "multi")
    max_uses = int(r.get("max_uses", -1) or -1)
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO promos(code,type,payload,uses,max_uses,created) "
                "VALUES($1,$2,$3::jsonb,0,$4,$5)",
                code, ptype, json.dumps(payload), max_uses, time.time())
        except asyncpg.UniqueViolationError:
            raise HTTPException(409, "Такой код уже существует")
    return {"ok": True, "code": code}


@app.get("/api/admin/promos")
async def admin_list_promos(user=Depends(require_admin)):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT code,type,payload,uses,max_uses,created FROM promos ORDER BY created DESC")
    return [{"code": r["code"], "type": r["type"], "payload": _j(r["payload"]),
             "uses": r["uses"], "max_uses": r["max_uses"], "created": r["created"]} for r in rows]


@app.delete("/api/admin/promo/{code}")
async def admin_delete_promo(code: str, user=Depends(require_admin)):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM promos WHERE code=$1", code.upper())
    return {"ok": True}


@app.get("/api/admin/user-state/{nick}")
async def admin_user_state(nick: str, user=Depends(require_admin)):
    async with pool.acquire() as conn:
        target = await conn.fetchrow("SELECT id,nick FROM users WHERE LOWER(nick)=LOWER($1)", nick)
        if not target:
            raise HTTPException(404, "Игрок не найден")
        row = await conn.fetchrow("SELECT state FROM saves WHERE user_id=$1", target["id"])
    st = json.loads(row["state"]) if row and row["state"] else default_state(target["nick"])
    inv = [o for o in st.get("inv", []) if o.get("st") == "in"]
    return {"nick": target["nick"], "balance": st.get("balance", 0),
            "tokens": st.get("tokens", 0), "inventory": inv,
            "fee_log": st.get("fee_log", [])}


@app.post("/api/admin/set-balance")
async def admin_set_balance(r: dict = Body(...), user=Depends(require_admin)):
    nick = (r.get("nick") or "").strip()
    if not nick:
        raise HTTPException(400, "Укажи ник")
    new_balance = max(0, int(r.get("balance", 0) or 0))
    async with pool.acquire() as conn:
        target = await conn.fetchrow("SELECT id,nick FROM users WHERE LOWER(nick)=LOWER($1)", nick)
        if not target:
            raise HTTPException(404, "Игрок не найден")
        row = await conn.fetchrow("SELECT state FROM saves WHERE user_id=$1", target["id"])
        state = json.loads(row["state"]) if row and row["state"] else default_state(target["nick"])
        state["balance"] = new_balance
        await conn.execute("UPDATE saves SET state=$1::jsonb, updated=$2 WHERE user_id=$3",
                           json.dumps(state), time.time(), target["id"])
    return {"ok": True, "nick": target["nick"], "balance": new_balance}


@app.post("/api/admin/remove-item")
async def admin_remove_item(r: dict = Body(...), user=Depends(require_admin)):
    nick = (r.get("nick") or "").strip()
    item_uid = (r.get("uid") or "").strip()
    if not nick or not item_uid:
        raise HTTPException(400, "Укажи ник и uid")
    async with pool.acquire() as conn:
        target = await conn.fetchrow("SELECT id,nick FROM users WHERE LOWER(nick)=LOWER($1)", nick)
        if not target:
            raise HTTPException(404, "Игрок не найден")
        row = await conn.fetchrow("SELECT state FROM saves WHERE user_id=$1", target["id"])
        state = json.loads(row["state"]) if row and row["state"] else default_state(target["nick"])
        found = False
        for o in state.get("inv", []):
            if o.get("uid") == item_uid:
                o["st"] = "removed"; found = True; break
        if not found:
            raise HTTPException(404, "Предмет не найден")
        favs = state.get("fav") or []
        if item_uid in favs:
            favs.remove(item_uid); state["fav"] = favs
        await conn.execute("UPDATE saves SET state=$1::jsonb, updated=$2 WHERE user_id=$3",
                           json.dumps(state), time.time(), target["id"])
    return {"ok": True}


@app.post("/api/admin/clear-inventory")
async def admin_clear_inventory(r: dict = Body(...), user=Depends(require_admin)):
    nick = (r.get("nick") or "").strip()
    if not nick:
        raise HTTPException(400, "Укажи ник")
    async with pool.acquire() as conn:
        target = await conn.fetchrow("SELECT id,nick FROM users WHERE LOWER(nick)=LOWER($1)", nick)
        if not target:
            raise HTTPException(404, "Игрок не найден")
        row = await conn.fetchrow("SELECT state FROM saves WHERE user_id=$1", target["id"])
        state = json.loads(row["state"]) if row and row["state"] else default_state(target["nick"])
        for o in state.get("inv", []):
            if o.get("st") == "in":
                o["st"] = "removed"
        state["fav"] = []
        await conn.execute("UPDATE saves SET state=$1::jsonb, updated=$2 WHERE user_id=$3",
                           json.dumps(state), time.time(), target["id"])
    return {"ok": True}


# ==================== BANS ====================
@app.get("/api/admin/bans")
async def admin_list_bans(user=Depends(require_admin)):
    now = time.time()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM bans WHERE until_ts IS NOT NULL AND until_ts>0 AND until_ts<$1", now)
        rows = await conn.fetch(
            "SELECT nick,until_ts,reason,by_nick,created,user_id FROM bans ORDER BY created DESC")
    return [{"nick": r["nick"], "until": r["until_ts"] or 0, "reason": r["reason"] or "",
             "by": r["by_nick"] or "админ", "created": r["created"],
             "left": max(0, int((r["until_ts"] or 0) - now)) if r["until_ts"] else -1}
            for r in rows]


@app.post("/api/admin/ban")
async def admin_ban_user(r: BanReq, admin=Depends(require_admin)):
    nick = (r.nick or "").strip()
    if not nick:
        raise HTTPException(400, "Укажи ник")
    if nick.lower() == ADMIN_NICK:
        raise HTTPException(400, "Нельзя забанить главного админа")
    duration = max(0, int(r.duration_ms or 0))
    until_ts = (time.time() + duration / 1000.0) if duration > 0 else 0
    by_nick = (r.by or admin["nick"]).strip()[:32]
    reason = (r.reason or "").strip()[:400]
    async with pool.acquire() as conn:
        target = await conn.fetchrow("SELECT id FROM users WHERE LOWER(nick)=LOWER($1)", nick)
        target_id = target["id"] if target else None
        await conn.execute("""
            INSERT INTO bans(nick,until_ts,reason,by_nick,created,user_id)
            VALUES($1,$2,$3,$4,$5,$6)
            ON CONFLICT(nick) DO UPDATE SET
                until_ts=EXCLUDED.until_ts, reason=EXCLUDED.reason,
                by_nick=EXCLUDED.by_nick, created=EXCLUDED.created, user_id=EXCLUDED.user_id
        """, nick, until_ts, reason, by_nick, time.time(), target_id)
    return {"ok": True, "nick": nick, "until": until_ts,
            "duration": duration, "reason": reason, "by": by_nick}


@app.delete("/api/admin/ban/{nick}")
async def admin_unban_user(nick: str, admin=Depends(require_admin)):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM bans WHERE LOWER(nick)=LOWER($1)", nick)
    return {"ok": True}


@app.get("/api/ban/status")
async def ban_status(nick: str = ""):
    nick = (nick or "").strip()
    if not nick:
        raise HTTPException(400, "Укажи ник")
    ban = await get_active_ban(nick)
    if not ban:
        return {"banned": False}
    if ban["until_ts"] and ban["until_ts"] > 0:
        return {"banned": True, "nick": ban["nick"], "reason": ban["reason"] or "не указана",
                "by": ban["by_nick"] or "админ", "until": ban["until_ts"],
                "left": max(0, int(ban["until_ts"] - time.time())), "created": ban["created"]}
    return {"banned": True, "nick": ban["nick"], "reason": ban["reason"] or "не указана",
            "by": ban["by_nick"] or "админ", "until": 0, "left": -1, "created": ban["created"]}


# ==================== HEALTH ====================
@app.get("/api/health")
async def health():
    return {"ok": True, "ts": time.time(), "version": VERSION, "codename": CODENAME}


if os.path.isdir("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")