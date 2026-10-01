# server.py — CASEFORGE backend
import os, re, json, time, uuid, random, secrets
from typing import Optional, List
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from fastapi import FastAPI, HTTPException, Depends, Header, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import jwt, asyncpg
from passlib.hash import bcrypt

SECRET    = os.getenv("JWT_SECRET", secrets.token_hex(32))
DB_URL    = os.getenv("DATABASE_URL", "")
DATA_PATH = os.getenv("DATA_PATH", "data/game_data.json")

ADMIN_NICK = "admin"
ADMIN_PASS = "AdmiN@1@2@3"

def _clean_dsn(dsn):
    p = urlparse(dsn)
    q = [(k, v) for k, v in parse_qsl(p.query) if k != "channel_binding"]
    return urlunparse(p._replace(query=urlencode(q)))

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
    """Возвращает предмет по id. Понимает номерные скины вида 'it42_n7'."""
    if not item_id:
        return None
    if not isinstance(item_id, str):
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
                nick TEXT PRIMARY KEY,
                until_ts DOUBLE PRECISION,
                reason TEXT,
                by_nick TEXT,
                created DOUBLE PRECISION,
                user_id INT);
            CREATE TABLE IF NOT EXISTS live_drops(
                id BIGSERIAL PRIMARY KEY,
                nick TEXT, item_id TEXT, num INT,
                case_name TEXT, price INT,
                ts DOUBLE PRECISION);
            CREATE INDEX IF NOT EXISTS idx_live_drops_ts ON live_drops(ts DESC);
        """)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    if pool:
        await pool.close()


app = FastAPI(title="CASEFORGE API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


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
            "banned": True,
            "nick": ban["nick"],
            "reason": ban["reason"] or "не указана",
            "by": ban["by_nick"] or "админ",
            "until": ban["until_ts"],
            "left": left,
            "created": ban["created"],
        }, ensure_ascii=False)
    return json.dumps({
        "banned": True,
        "nick": ban["nick"],
        "reason": ban["reason"] or "не указана",
        "by": ban["by_nick"] or "админ",
        "until": 0,
        "left": -1,
        "created": ban["created"],
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
        "qp": {"free": 0, "got": 0, "sold": 0, "big": 0, "gold": 0, "upw": 0, "ct": 0, "wheel": 0},
        "daily_quests": [], "daily_claimed": {}, "quest_date": None,
        "stats": {"opened": 0, "best": 0, "spent": 0, "won": 0, "upW": 0, "upL": 0,
                  "ct": 0, "xp": 0, "free": 0, "earned": 0},
        "created": int(time.time() * 1000),
    }


QUEST_POOL = [
    {"ic": "🎁", "n": "Открыть {} бесплатных кейсов", "s": "free", "t": [3, 12], "k": 90},
    {"ic": "🎒", "n": "Получить {} предметов", "s": "got", "t": [5, 20], "k": 55},
    {"ic": "💰", "n": "Продать {} предметов", "s": "sold", "t": [5, 15], "k": 70},
    {"ic": "🎡", "n": "Крутить колесо {} раз", "s": "wheel", "t": [2, 4], "k": 200},
    {"ic": "⚡", "n": "Выиграть апгрейд", "s": "upw", "t": [1, 1], "k": 1500},
    {"ic": "📜", "n": "Заключить контракт", "s": "ct", "t": [1, 2], "k": 900},
    {"ic": "🔥", "n": "Выбить {} предметов дороже 1000 ₽", "s": "big", "t": [1, 3], "k": 600},
]


def daily_quests(key):
    rnd = random.Random(key)
    pq = QUEST_POOL[:]
    rnd.shuffle(pq)
    out = []
    for i, q in enumerate(pq[:5]):
        t = rnd.randint(*q["t"])
        r = int(t * q["k"] * rnd.uniform(.8, 1.3) // 10 * 10)
        out.append({"id": f"d{i}", "ic": q["ic"], "n": q["n"].format(t),
                    "d": "Обновляется каждые 24 часа", "t": t, "s": q["s"], "r": r})
    return out


async def load_state(uid):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT state FROM saves WHERE user_id=$1", uid)
    st = json.loads(row["state"]) if row and row["state"] else default_state("F2P")
    if st.get("quest_date") != today():
        st["quest_date"] = today()
        st["daily_quests"] = daily_quests(today())
        st["daily_claimed"] = {}
    if not isinstance(st.get("fav"), list):
        st["fav"] = []
    return st


async def persist(uid, st):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO saves(user_id,state,updated) VALUES($1,$2::jsonb,$3) "
            "ON CONFLICT(user_id) DO UPDATE SET state=$2::jsonb, updated=$3",
            uid, json.dumps(st), time.time())


async def sanitize_state(uid, st):
    # Чистим битые предметы: если id не распознаётся — выкидываем
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
    state["daily_claimed"] = old.get("daily_claimed", {})
    state["quest_date"] = old.get("quest_date")
    state = await sanitize_state(user["id"], state)
    await persist(user["id"], state)
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


# ==================== LIVE DROPS ====================
@app.post("/api/live/drop")
async def push_live_drop(r: LiveDropReq, user=Depends(get_user)):
    price = max(0, int(r.price or 0))
    num = r.num if (r.num and 1 <= r.num <= 100) else None
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO live_drops(nick,item_id,num,case_name,price,ts) "
            "VALUES($1,$2,$3,$4,$5,$6)",
            user["nick"], r.item_id, num, r.case[:64], price, time.time())
        await conn.execute(
            "DELETE FROM live_drops WHERE id NOT IN "
            "(SELECT id FROM live_drops ORDER BY ts DESC LIMIT 200)")
    return {"ok": True}


@app.get("/api/live/drops")
async def get_live_drops():
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT nick,item_id,num,case_name,price,ts "
            "FROM live_drops ORDER BY ts DESC LIMIT 30")
    return [{"nick": r["nick"], "item_id": r["item_id"], "num": r["num"],
             "case": r["case_name"] or "", "price": r["price"], "ts": r["ts"]}
            for r in rows]


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
    res = {}
    for pl in players:
        drops = [roll_item(CASES[c]) for c in cases if c in CASES]
        res[str(pl["id"])] = {"nick": pl.get("nick", "?"), "drops": drops,
                              "total": sum((resolve_item(d) or {}).get("price", 0) for d in drops)}
    winner = max(res, key=lambda k: res[k]["total"])
    return {"winner": winner, "claimed": [], "res": res}


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
        for pid, r in (results.get("res") or {}).items():
            for iid in r.get("drops", []):
                it = resolve_item(iid)
                if not it:
                    continue
                st["inv"].insert(0, {"uid": "b" + uuid.uuid4().hex[:8], "id": iid,
                                     "src": "Батл", "ts": now, "st": "in"})
                st["hist"].insert(0, {"id": iid, "ts": now, "src": "Батл", "price": it["price"]})
                st["stats"]["won"] = st["stats"].get("won", 0) + it["price"]
        st["hist"] = st["hist"][:150]
        results.setdefault("claimed", []).append(uid)
        await conn.execute("UPDATE battles SET results=$1::jsonb WHERE id=$2", json.dumps(results), bid)
        await persist(user["id"], st)
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
        if give > 0:
            my_st["balance"] += give
            my_st["stats"]["earned"] = my_st["stats"].get("earned", 0) + give
        my_st["balance"] -= ask
        my_st["stats"]["spent"] = my_st["stats"].get("spent", 0) + ask
        owner_st["balance"] += ask
        owner_st["stats"]["earned"] = owner_st["stats"].get("earned", 0) + ask
        my_st["hist"] = my_st["hist"][:150]
        await persist(owner_id, owner_st)
        await persist(user["id"], my_st)
        await conn.execute("UPDATE battles SET status='done', results=$1::jsonb WHERE id=$2",
                           json.dumps({"accepted_by": user["id"], "ask": ask, "give": give}), r.trade_id)
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


# ==================== PROMO CODES ====================
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
            rows = await conn.fetch(
                "SELECT id,nick,created FROM users ORDER BY id DESC LIMIT 100")
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
        state.setdefault("balance", 0)
        state.setdefault("tokens", 0)
        state.setdefault("inv", [])
        state.setdefault("hist", [])
        state.setdefault("fav", [])
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


@app.post("/api/admin/promo")
async def admin_create_promo(r: dict = Body(...), user=Depends(require_admin)):
    code = (r.get("code") or "").strip().upper()
    if not code:
        raise HTTPException(400, "Пустой код")
    if len(code) < 3:
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
            "tokens": st.get("tokens", 0), "inventory": inv}


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
                o["st"] = "removed"
                found = True
                break
        if not found:
            raise HTTPException(404, "Предмет не найден")
        favs = state.get("fav") or []
        if item_uid in favs:
            favs.remove(item_uid)
            state["fav"] = favs
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
            "SELECT nick,until_ts,reason,by_nick,created,user_id "
            "FROM bans ORDER BY created DESC")
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
                until_ts=EXCLUDED.until_ts,
                reason=EXCLUDED.reason,
                by_nick=EXCLUDED.by_nick,
                created=EXCLUDED.created,
                user_id=EXCLUDED.user_id
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
    return {"ok": True, "ts": time.time()}


if os.path.isdir("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
