# server.py — CASEFORGE backend (Neon Postgres + asyncpg)
import os, json, time, uuid, random, secrets
from typing import Optional, List
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from fastapi import FastAPI, HTTPException, Depends, Header, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import jwt
import asyncpg
from passlib.hash import bcrypt

# ---------- Настройки ----------
SECRET    = os.getenv("JWT_SECRET", secrets.token_hex(32))
DB_URL    = os.getenv("DATABASE_URL", "")
DATA_PATH = os.getenv("DATA_PATH", "data/game_data.json")

def _clean_dsn(dsn: str) -> str:
    """Убирает параметры DSN, которые asyncpg не понимает (channel_binding и т.п.)."""
    p = urlparse(dsn)
    q = [(k, v) for k, v in parse_qsl(p.query) if k not in ("channel_binding",)]
    return urlunparse(p._replace(query=urlencode(q)))

# ---------- Игровые данные (кейсы/предметы) ----------
if os.path.exists(DATA_PATH):
    with open(DATA_PATH, encoding="utf-8") as f:
        DATA = json.load(f)
else:
    DATA = {"items": [], "cases": []}
ITEMS = {i["id"]: i for i in DATA.get("items", [])}
CASES = {c["id"]: c for c in DATA.get("cases", [])}

# ---------- База ----------
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
        """)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    if pool:
        await pool.close()

app = FastAPI(title="CASEFORGE API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ---------- Вспомогательные ----------
def _j(x):
    """JSONB из asyncpg приходит строкой — безопасно парсим."""
    if x is None: return None
    if isinstance(x, (dict, list)): return x
    if isinstance(x, str):
        try: return json.loads(x)
        except Exception: return None
    return None

def make_token(uid: int) -> str:
    return jwt.encode({"uid": uid, "exp": time.time() + 30*86400}, SECRET, algorithm="HS256")

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
    return dict(row)

def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())

def default_state(nick: str) -> dict:
    return {
        "balance": 3000, "inv": [], "hist": [], "fast": False, "sound": True,
        "name": nick, "tokens": 0, "welcome": False,
        "cd": {}, "wheel": 0, "daily": {"streak": 0, "last": 0}, "promo": [], "claimed": {},
        "qp": {"free":0,"got":0,"sold":0,"big":0,"gold":0,"upw":0,"ct":0,"wheel":0},
        "daily_quests": [], "daily_claimed": {}, "quest_date": None,
        "stats": {"opened":0,"best":0,"spent":0,"won":0,"upW":0,"upL":0,"ct":0,"xp":0,"free":0,"earned":0},
        "created": int(time.time()*1000),
    }

# ---------- Ежедневные задания (рестарт каждые 24 ч) ----------
QUEST_POOL = [
    {"ic":"🎁","n":"Открыть {} бесплатных кейсов","s":"free","t":[3,12],"k":90},
    {"ic":"🎒","n":"Получить {} предметов","s":"got","t":[5,20],"k":55},
    {"ic":"💰","n":"Продать {} предметов","s":"sold","t":[5,15],"k":70},
    {"ic":"🎡","n":"Крутить колесо {} раз","s":"wheel","t":[2,4],"k":200},
    {"ic":"⚡","n":"Выиграть апгрейд","s":"upw","t":[1,1],"k":1500},
    {"ic":"📜","n":"Заключить контракт","s":"ct","t":[1,2],"k":900},
    {"ic":"🔥","n":"Выбить {} предметов дороже 1000 ₽","s":"big","t":[1,3],"k":600},
]

def daily_quests(date_key: str) -> list:
    rnd = random.Random(date_key)          # одинаковые для всех в один день
    pool_q = QUEST_POOL[:]
    rnd.shuffle(pool_q)
    out = []
    for i, q in enumerate(pool_q[:5]):
        t = rnd.randint(*q["t"])
        r = int(t * q["k"] * rnd.uniform(.8, 1.3) // 10 * 10)
        out.append({"id": f"d{i}", "ic": q["ic"], "n": q["n"].format(t),
                    "d": "Обновляется каждые 24 часа", "t": t, "s": q["s"], "r": r})
    return out

async def load_state(uid: int) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT state FROM saves WHERE user_id=$1", uid)
    st = json.loads(row["state"]) if row and row["state"] else default_state("F2P")
    if st.get("quest_date") != today():
        st["quest_date"] = today()
        st["daily_quests"] = daily_quests(today())
        st["daily_claimed"] = {}
    return st

async def persist(uid: int, st: dict):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO saves(user_id, state, updated) VALUES($1, $2::jsonb, $3) "
            "ON CONFLICT(user_id) DO UPDATE SET state=$2::jsonb, updated=$3",
            uid, json.dumps(st), time.time())

# ---------- Модели запросов ----------
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

class TradeActionReq(BaseModel):
    trade_id: str

class UpgBattleReq(BaseModel):
    src_item_uid: str
    target_item_id: str

# ---------- Auth ----------
@app.post("/api/register")
async def register(a: AuthReq):
    if len(a.nick) < 3:  raise HTTPException(400, "Ник от 3 символов")
    if len(a.password) < 4: raise HTTPException(400, "Пароль от 4 символов")
    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users(nick, pass, created) VALUES($1, $2, $3) RETURNING id",
                a.nick, bcrypt.hash(a.password), time.time())
            await conn.execute(
                "INSERT INTO saves(user_id, state, updated) VALUES($1, $2::jsonb, $3)",
                uid, json.dumps(default_state(a.nick)), time.time())
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "Ник занят")
    return {"token": make_token(uid), "nick": a.nick}

@app.post("/api/login")
async def login(a: AuthReq):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE nick=$1", a.nick)
    if not row or not bcrypt.verify(a.password, row["pass"]):
        raise HTTPException(403, "Неверный ник или пароль")
    return {"token": make_token(row["id"]), "nick": row["nick"]}

# ---------- Сейв ----------
@app.get("/api/state")
async def get_state(user=Depends(get_user)):
    st = await load_state(user["id"])
    await persist(user["id"], st)   # сохраняем обновлённые ежедневные задания
    return st

@app.post("/api/state")
async def put_state(state: dict = Body(...), user=Depends(get_user)):
    old = await load_state(user["id"])
    # серверные поля клиентом не перезаписываются
    state["daily_quests"]  = old.get("daily_quests", [])
    state["daily_claimed"] = old.get("daily_claimed", {})
    state["quest_date"]    = old.get("quest_date")
    await persist(user["id"], state)
    return {"ok": True}

@app.post("/api/quests/{qid}/claim")
async def claim_quest(qid: str, user=Depends(get_user)):
    st = await load_state(user["id"])
    if qid in st.get("daily_claimed", {}): raise HTTPException(400, "Уже получено")
    q = next((x for x in st.get("daily_quests", []) if x["id"] == qid), None)
    if not q: raise HTTPException(404, "Нет такого задания")
    if st["qp"].get(q["s"], 0) < q["t"]: raise HTTPException(400, "Ещё не выполнено")
    st.setdefault("daily_claimed", {})[qid] = 1
    st["balance"] += q["r"]
    st["stats"]["earned"] = st["stats"].get("earned", 0) + q["r"]
    await persist(user["id"], st)
    return {"ok": True, "balance": st["balance"], "reward": q["r"]}

# ---------- Онлайн и друзья ----------
@app.post("/api/ping")
async def ping(user=Depends(get_user)):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO presence(user_id, last_seen) VALUES($1, $2) "
            "ON CONFLICT(user_id) DO UPDATE SET last_seen=$2",
            user["id"], time.time())
    return {"ok": True}

@app.post("/api/friends")
async def add_friend(r: NickReq, user=Depends(get_user)):
    async with pool.acquire() as conn:
        f = await conn.fetchrow("SELECT id FROM users WHERE nick=$1", r.nick)
        if not f: raise HTTPException(404, "Игрок не найден")
        if f["id"] == user["id"]: raise HTTPException(400, "Нельзя добавить себя")
        await conn.execute(
            "INSERT INTO friends(user_id, friend_id) VALUES($1, $2) ON CONFLICT DO NOTHING",
            user["id"], f["id"])
    return {"ok": True}

@app.get("/api/friends")
async def list_friends(user=Depends(get_user)):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.id, u.nick, p.last_seen FROM friends f
            JOIN users u ON u.id = f.friend_id
            LEFT JOIN presence p ON p.user_id = u.id
            WHERE f.user_id = $1 ORDER BY u.nick
        """, user["id"])
    now = time.time()
    return [{"id": r["id"], "nick": r["nick"],
             "online": bool(r["last_seen"] and now - r["last_seen"] < 40)} for r in rows]

# ---------- Батлы ----------
BOT_NAMES = ["BattleBot_3000","Железный","Skynet","КиберВолк","GLaDOS","R2D2","МегаБот","X-500"]

def roll_item(case: dict):
    r, acc, chosen = random.random(), 0, list(case["w"].keys())[-1]
    for k, ch in case["w"].items():
        acc += ch
        if r <= acc:
            chosen = k
            break
    pool_items = [i for i in case["items"] if ITEMS.get(i, {}).get("rar") == chosen]
    return random.choice(pool_items or case["items"])

def battle_view(b):
    cases = _j(b["cases"])
    players = _j(b["players"]) or []
    results = _j(b["results"])
    if b["mode"] in ("trade", "upgbattle"):
        case_list, entry = [], 0
    else:
        ids = cases if isinstance(cases, list) else []
        case_list = [{"id": c, "name": CASES[c]["name"], "price": CASES[c]["price"]}
                     for c in ids if c in CASES]
        entry = sum(CASES[c]["price"] for c in ids if c in CASES)
    return {"id": b["id"], "mode": b["mode"], "status": b["status"],
            "creator": b["creator"], "target": b["target"],
            "cases": case_list, "entry": entry, "players": players,
            "results": results, "created": b["created"]}

@app.post("/api/battles")
async def create_battle(r: BattleReq, user=Depends(get_user)):
    if not r.cases or len(r.cases) > 10: raise HTTPException(400, "От 1 до 10 кейсов")
    for cid in r.cases:
        if cid not in CASES: raise HTTPException(400, "Неизвестный кейс")
    players = [{"id": user["id"], "nick": user["nick"], "ready": False, "paid": False}]
    if r.mode == "bot":
        players.append({"id": "bot", "nick": random.choice(BOT_NAMES),
                        "bot": True, "ready": True, "paid": True})
    bid = uuid.uuid4().hex[:10]
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO battles(id, creator, mode, target, cases, status, players, results, members, created) "
            "VALUES($1,$2,$3,$4,$5::jsonb,$6,$7::jsonb,$8::jsonb,$9,$10)",
            bid, user["id"], r.mode, r.friend, json.dumps(r.cases), "waiting",
            json.dumps(players), json.dumps(None), str(user["id"]), time.time())
    return {"id": bid}

@app.get("/api/battles")
async def list_battles(user=Depends(get_user)):
    async with pool.acquire() as conn:
        avail = await conn.fetch("""
            SELECT * FROM battles WHERE status='waiting' AND mode IN ('bot','friend','public')
            AND creator != $1 AND (mode='public' OR (mode='friend' AND target=$2))
        """, user["id"], user["nick"])
        mine = await conn.fetch("""
            SELECT * FROM battles WHERE mode NOT IN ('trade','upgbattle')
            AND (creator=$1 OR ','||members||',' LIKE $2)
        """, user["id"], f"%,{user['id']},%")
    seen, out = set(), []
    for b in list(avail) + list(mine):
        if b["id"] in seen: continue
        seen.add(b["id"])
        out.append(battle_view(dict(b)))
    out.sort(key=lambda x: x["created"], reverse=True)
    return out

@app.post("/api/battles/{bid}/join")
async def join_battle(bid: str, user=Depends(get_user)):
    async with pool.acquire() as conn:
        b = await conn.fetchrow("SELECT * FROM battles WHERE id=$1", bid)
        if not b or b["status"] != "waiting": raise HTTPException(400, "Батл недоступен")
        players = _j(b["players"]) or []
        if any(str(p.get("id")) == str(user["id"]) for p in players):
            return {"ok": True}
        if len(players) >= 2: raise HTTPException(400, "Батл занят")
        players.append({"id": user["id"], "nick": user["nick"], "ready": False, "paid": False})
        members = ",".join(str(p["id"]) for p in players)
        await conn.execute("UPDATE battles SET players=$1::jsonb, members=$2 WHERE id=$3",
                           json.dumps(players), members, bid)
    return {"ok": True}

@app.post("/api/battles/{bid}/ready")
async def ready_battle(bid: str, user=Depends(get_user)):
    async with pool.acquire() as conn:
        b = await conn.fetchrow("SELECT * FROM battles WHERE id=$1", bid)
        if not b or b["status"] != "waiting": raise HTTPException(400, "Батл недоступен")
        players = _j(b["players"]) or []
        p = next((x for x in players if str(x.get("id")) == str(user["id"])), None)
        if not p: raise HTTPException(400, "Вы не в батле")
        cases = _j(b["cases"]) or []
        entry = sum(CASES[c]["price"] for c in cases if c in CASES)
        st = await load_state(user["id"])
        if not p.get("paid"):
            if st["balance"] < entry: raise HTTPException(400, "Не хватает ₽ на вход")
            st["balance"] -= entry
            st["stats"]["spent"] = st["stats"].get("spent", 0) + entry
            await persist(user["id"], st)
            p["paid"] = True
        p["ready"] = True
        status, results = "waiting", None
        if all(x.get("ready") for x in players):
            res = {}
            for pl in players:
                drops = [roll_item(CASES[c]) for c in cases if c in CASES]
                res[str(pl["id"])] = {
                    "nick": pl.get("nick", "?"), "drops": drops,
                    "total": sum(ITEMS[d]["price"] for d in drops if d in ITEMS)}
            winner = max(res, key=lambda k: res[k]["total"])
            results = {"winner": winner, "claimed": [], "res": res}
            status = "done"
        await conn.execute(
            "UPDATE battles SET players=$1::jsonb, status=$2, results=$3::jsonb WHERE id=$4",
            json.dumps(players), status, json.dumps(results), bid)
    return {"status": status, "results": results}

@app.post("/api/battles/{bid}/claim")
async def claim_battle(bid: str, user=Depends(get_user)):
    async with pool.acquire() as conn:
        b = await conn.fetchrow("SELECT * FROM battles WHERE id=$1", bid)
        if not b or b["status"] != "done": raise HTTPException(400, "Батл не завершён")
        results = _j(b["results"]) or {}
        uid = str(user["id"])
        if str(results.get("winner")) != uid: raise HTTPException(400, "Вы проиграли этот батл")
        if uid in results.get("claimed", []): raise HTTPException(400, "Уже забрано")
        st = await load_state(user["id"])
        now = int(time.time()*1000)
        for pid, r in (results.get("res") or {}).items():
            for iid in r.get("drops", []):
                it = ITEMS.get(iid)
                if not it: continue
                st["inv"].insert(0, {"uid": "b"+uuid.uuid4().hex[:8], "id": iid,
                                     "src": "Батл", "ts": now, "st": "in"})
                st["hist"].insert(0, {"id": iid, "ts": now, "src": "Батл", "price": it["price"]})
                st["stats"]["won"] = st["stats"].get("won", 0) + it["price"]
        st["hist"] = st["hist"][:150]
        results.setdefault("claimed", []).append(uid)
        await conn.execute("UPDATE battles SET results=$1::jsonb WHERE id=$2",
                           json.dumps(results), bid)
        await persist(user["id"], st)
    return {"ok": True, "balance": st["balance"]}

# ---------- Обмены между игроками ----------
def trade_view(b):
    d = battle_view(b)
    d["trade_info"] = _j(b["cases"]) or {}
    return d

@app.post("/api/trades")
async def create_trade(r: TradeCreateReq, user=Depends(get_user)):
    if not r.offer_items: raise HTTPException(400, "Выбери хотя бы 1 предмет")
    if r.ask_balance < 0: raise HTTPException(400, "Сумма не может быть отрицательной")
    async with pool.acquire() as conn:
        t = await conn.fetchrow("SELECT id FROM users WHERE nick=$1", r.target_nick)
        if not t: raise HTTPException(404, "Игрок не найден")
        if t["id"] == user["id"]: raise HTTPException(400, "Нельзя обменяться с собой")
        st = await load_state(user["id"])
        details = []
        for uid_item in r.offer_items:
            o = next((x for x in st["inv"] if x["uid"] == uid_item and x["st"] == "in"), None)
            if not o: raise HTTPException(400, "Предмет недоступен")
            it = ITEMS.get(o["id"])
            if not it: raise HTTPException(400, "Предмет не найден")
            details.append({"uid": uid_item, "id": it["id"], "name": it["name"], "price": it["price"]})
        tid = "t" + uuid.uuid4().hex[:10]
        payload = {"offer": r.offer_items, "offer_details": details,
                   "ask_balance": r.ask_balance, "owner": user["id"], "owner_nick": user["nick"]}
        await conn.execute(
            "INSERT INTO battles(id, creator, mode, target, cases, status, players, results, members, created) "
            "VALUES($1,$2,'trade',$3,$4::jsonb,'waiting',$5::jsonb,$6::jsonb,$7,$8)",
            tid, user["id"], r.target_nick, json.dumps(payload),
            json.dumps([{"id": user["id"], "nick": user["nick"]},
                        {"id": t["id"], "nick": r.target_nick}]),
            json.dumps(None), f"{user['id']},{t['id']}", time.time())
    return {"id": tid}

@app.get("/api/trades")
async def list_trades(user=Depends(get_user)):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM battles WHERE mode='trade'
            AND (creator=$1 OR ','||members||',' LIKE $2)
            ORDER BY created DESC LIMIT 30
        """, user["id"], f"%,{user['id']},%")
    return [trade_view(dict(b)) for b in rows]

@app.post("/api/trades/accept")
async def accept_trade(r: TradeActionReq, user=Depends(get_user)):
    async with pool.acquire() as conn:
        b = await conn.fetchrow("SELECT * FROM battles WHERE id=$1 AND mode='trade'", r.trade_id)
        if not b or b["status"] != "waiting": raise HTTPException(400, "Обмен недоступен")
        if b["creator"] == user["id"]: raise HTTPException(400, "Это твой собственный обмен")
        info = _j(b["cases"]) or {}
        ask = int(info.get("ask_balance", 0))
        owner_id = int(info.get("owner", b["creator"]))
        owner = await conn.fetchrow("SELECT * FROM users WHERE id=$1", owner_id)
        if not owner: raise HTTPException(400, "Создатель обмена не найден")
        owner_st = await load_state(owner_id)
        my_st = await load_state(user["id"])
        if my_st["balance"] < ask: raise HTTPException(400, f"Не хватает ₽ (нужно {ask})")
        # проверяем предметы создателя
        for uid_item in info.get("offer", []):
            o = next((x for x in owner_st["inv"] if x["uid"] == uid_item and x["st"] == "in"), None)
            if not o: raise HTTPException(400, "Предметов обмена уже нет у отправителя")
        now = int(time.time()*1000)
        # передаём предметы
        for uid_item in info.get("offer", []):
            o = next((x for x in owner_st["inv"] if x["uid"] == uid_item), None)
            if not o: continue
            it = ITEMS.get(o["id"])
            o["st"] = "traded"
            my_st["inv"].insert(0, {"uid": "tr"+uuid.uuid4().hex[:8], "id": o["id"],
                                    "src": f"Обмен от {owner['nick']}", "ts": now, "st": "in"})
            my_st["hist"].insert(0, {"id": o["id"], "ts": now, "src": "Обмен",
                                     "price": it["price"] if it else 0})
        # передаём деньги
        my_st["balance"] -= ask
        my_st["stats"]["spent"] = my_st["stats"].get("spent", 0) + ask
        owner_st["balance"] += ask
        owner_st["stats"]["earned"] = owner_st["stats"].get("earned", 0) + ask
        my_st["hist"] = my_st["hist"][:150]
        await persist(owner_id, owner_st)
        await persist(user["id"], my_st)
        await conn.execute(
            "UPDATE battles SET status='done', results=$1::jsonb WHERE id=$2",
            json.dumps({"accepted_by": user["id"], "ask": ask}), r.trade_id)
    return {"ok": True}

@app.post("/api/trades/cancel")
async def cancel_trade(r: TradeActionReq, user=Depends(get_user)):
    async with pool.acquire() as conn:
        b = await conn.fetchrow("SELECT * FROM battles WHERE id=$1 AND mode='trade'", r.trade_id)
        if not b: raise HTTPException(404, "Обмен не найден")
        if b["creator"] != user["id"]: raise HTTPException(403, "Отменить может только создатель")
        if b["status"] != "waiting": raise HTTPException(400, "Обмен уже закрыт")
        await conn.execute("UPDATE battles SET status='cancelled' WHERE id=$1", r.trade_id)
    return {"ok": True}

# ---------- PvP-апгрейд ----------
@app.post("/api/upg-battles")
async def create_upg_battle(r: UpgBattleReq, user=Depends(get_user)):
    tgt = ITEMS.get(r.target_item_id)
    if not tgt: raise HTTPException(400, "Неизвестная цель")
    st = await load_state(user["id"])
    src_o = next((x for x in st["inv"] if x["uid"] == r.src_item_uid and x["st"] == "in"), None)
    if not src_o: raise HTTPException(400, "Предмет недоступен")
    src = ITEMS.get(src_o["id"])
    if not src: raise HTTPException(400, "Предмет не найден")
    if tgt["price"] <= src["price"]: raise HTTPException(400, "Цель должна быть дороже")
    chance = max(0.005, min(0.97, (src["price"] / tgt["price"]) * 0.94))
    win = random.random() < chance
    now = int(time.time()*1000)
    src_o["st"] = "upgraded" if win else "lost"
    if win:
        st["inv"].insert(0, {"uid": "u"+uuid.uuid4().hex[:8], "id": tgt["id"],
                             "src": "Апгрейд-батл", "ts": now, "st": "in"})
        st["hist"].insert(0, {"id": tgt["id"], "ts": now, "src": "Апгрейд", "price": tgt["price"]})
        st["stats"]["upW"] = st["stats"].get("upW", 0) + 1
        st["stats"]["won"] = st["stats"].get("won", 0) + tgt["price"]
        if tgt["price"] > st["stats"].get("best", 0): st["stats"]["best"] = tgt["price"]
    else:
        st["stats"]["upL"] = st["stats"].get("upL", 0) + 1
    st["hist"] = st["hist"][:150]
    await persist(user["id"], st)
    bid = "u" + uuid.uuid4().hex[:10]
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO battles(id, creator, mode, target, cases, status, players, results, members, created) "
            "VALUES($1,$2,'upgbattle',$3,$4::jsonb,$5,$6::jsonb,$7::jsonb,$8,$9)",
            bid, user["id"], r.target_item_id,
            json.dumps({"src": src["id"], "src_uid": r.src_item_uid, "tgt": tgt["id"],
                        "chance": chance, "win": win, "owner": user["id"]}),
            "won" if win else "lost",
            json.dumps([{"id": user["id"], "nick": user["nick"]}]),
            json.dumps(None), str(user["id"]), time.time())
    return {"id": bid, "chance": chance, "win": win}

# ---------- Служебное ----------
@app.get("/api/health")
async def health():
    return {"ok": True, "ts": time.time()}

# Статика (фронт) — ОБЯЗАТЕЛЬНО после всех /api роутов
if os.path.isdir("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
