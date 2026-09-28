# server.py — CASEFORGE backend (Neon Postgres version)
import os, json, time, uuid, random, secrets
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Header, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import jwt
import asyncpg
from passlib.hash import bcrypt

# --- Настройки ---
SECRET     = os.getenv("JWT_SECRET", secrets.token_hex(32))
DB_URL     = os.getenv("DATABASE_URL") # Сюда Render подставит твою строку
DATA_PATH  = os.getenv("DATA_PATH", "data/game_data.json")

# --- Данные игры ---
if not os.path.exists(DATA_PATH):
    # Заглушка, если файла нет (чтобы сервер не падал при первом запуске)
    DATA = {"items": [], "cases": []}
else:
    with open(DATA_PATH, encoding="utf-8") as f:
        DATA = json.load(f)

ITEMS = {i["id"]: i for i in DATA.get("items", [])}
CASES = {c["id"]: c for c in DATA.get("cases", [])}

# --- База данных (Pool) ---
pool: asyncpg.Pool = None

async def init_db():
    global pool
    # Подключаемся к Neon
    pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        # Создаем таблицы, если их нет
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users(
                id SERIAL PRIMARY KEY, 
                nick TEXT UNIQUE, 
                pass TEXT, 
                created DOUBLE PRECISION
            );
            CREATE TABLE IF NOT EXISTS saves(
                user_id INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, 
                state JSONB, 
                updated DOUBLE PRECISION
            );
            CREATE TABLE IF NOT EXISTS friends(
                user_id INT REFERENCES users(id) ON DELETE CASCADE, 
                friend_id INT REFERENCES users(id) ON DELETE CASCADE, 
                PRIMARY KEY(user_id, friend_id)
            );
            CREATE TABLE IF NOT EXISTS presence(
                user_id INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, 
                last_seen DOUBLE PRECISION
            );
            CREATE TABLE IF NOT EXISTS battles(
                id TEXT PRIMARY KEY, 
                creator INT, 
                mode TEXT, 
                target TEXT,
                cases JSONB, 
                status TEXT, 
                players JSONB, 
                results JSONB,
                members TEXT, 
                created DOUBLE PRECISION
            );
        """)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    if pool: await pool.close()

app = FastAPI(title="CASEFORGE API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- Auth Helpers ---
def make_token(uid):
    return jwt.encode({"uid": uid, "exp": time.time() + 30*86400}, SECRET, algorithm="HS256")

async def get_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Требуется вход")
    try:
        payload = jwt.decode(authorization.split(" ")[1], SECRET, algorithms=["HS256"])
        uid = payload["uid"]
    except Exception:
        raise HTTPException(401, "Неверный токен")
    
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id=$1", uid)
    if not row: raise HTTPException(401, "Пользователь не найден")
    return dict(row)

# --- Models ---
class AuthReq(BaseModel):
    nick: str
    password: str

class NickReq(BaseModel):
    nick: str

class BattleReq(BaseModel):
    cases: List[str]
    mode: str = "bot"
    friend: Optional[str] = None

# --- Default State ---
def default_state(nick):
    return {
        "balance": 3000, "inv": [], "hist": [], "fast": False, "sound": True,
        "name": nick, "tokens": 0, "welcome": False, # False чтобы показать приветствие
        "cd": {}, "wheel": 0, "daily": {"streak": 0, "last": 0}, "promo": [], "claimed": {},
        "qp": {"free":0,"got":0,"sold":0,"big":0,"gold":0,"upw":0,"ct":0,"wheel":0},
        "daily_quests": [], "daily_claimed": {}, "quest_date": None,
        "stats": {"opened":0,"best":0,"spent":0,"won":0,"upW":0,"upL":0,"ct":0,"xp":0,"free":0,"earned":0},
        "created": int(time.time()*1000)
    }

# --- Logic: Quests ---
QUEST_POOL = [
    {"ic":"🎁","n":"Открыть {} бесплатных кейсов","s":"free","t":[3,12],"k":90},
    {"ic":"🎒","n":"Получить {} предметов","s":"got","t":[5,20],"k":55},
    {"ic":"💰","n":"Продать {} предметов","s":"sold","t":[5,15],"k":70},
    {"ic":"🎡","n":"Крутить колесо {} раз","s":"wheel","t":[2,4],"k":200},
    {"ic":"⚡","n":"Выиграть апгрейд","s":"upw","t":[1,1],"k":1500},
    {"ic":"📜","n":"Заключить контракт","s":"ct","t":[1,2],"k":900},
    {"ic":"🔥","n":"Выбить {} предметов дороже 1000 ₽","s":"big","t":[1,3],"k":600},
]

def daily_quests(date_key):
    rnd = random.Random(date_key)
    pool_q = QUEST_POOL[:]
    rnd.shuffle(pool_q)
    out = []
    for i, q in enumerate(pool_q[:5]):
        t = rnd.randint(*q["t"])
        r = int(t * q["k"] * rnd.uniform(.8, 1.3) // 10 * 10)
        out.append({"id":f"d{i}","ic":q["ic"],"n":q["n"].format(t),
                    "d":"Обновляется каждые 24 часа","t":t,"s":q["s"],"r":r})
    return out

async def load_state(uid):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT state FROM saves WHERE user_id=$1", uid)
    st = json.loads(row["state"]) if row else default_state("F2P")
    
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if st.get("quest_date") != today:
        st["quest_date"] = today
        st["daily_quests"] = daily_quests(today)
        st["daily_claimed"] = {}
    return st

async def persist(uid, st):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO saves(user_id, state, updated) VALUES($1, $2::jsonb, $3) "
            "ON CONFLICT(user_id) DO UPDATE SET state=$2::jsonb, updated=$3",
            uid, json.dumps(st), time.time()
        )

# --- Endpoints: Auth & State ---
@app.post("/api/register")
async def register(a: AuthReq):
    if len(a.nick) < 3: raise HTTPException(400, "Ник от 3 символов")
    if len(a.password) < 4: raise HTTPException(400, "Пароль от 4 символов")
    try:
        async with pool.acquire() as conn:
            uid = await conn.fetchval(
                "INSERT INTO users(nick, pass, created) VALUES($1, $2, $3) RETURNING id",
                a.nick, bcrypt.hash(a.password), time.time()
            )
            await conn.execute(
                "INSERT INTO saves(user_id, state, updated) VALUES($1, $2::jsonb, $3)",
                uid, json.dumps(default_state(a.nick)), time.time()
            )
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

@app.get("/api/state")
async def get_state(user=Depends(get_user)):
    st = await load_state(user["id"])
    await persist(user["id"], st) # Сохраняем обновленные квесты
    return st

@app.post("/api/state")
async def put_state(state: dict = Body(...), user=Depends(get_user)):
    # Защита серверных полей
    old = await load_state(user["id"])
    state["daily_quests"] = old.get("daily_quests", [])
    state["daily_claimed"] = old.get("daily_claimed", {})
    state["quest_date"] = old.get("quest_date")
    await persist(user["id"], state)
    return {"ok": True}

@app.post("/api/quests/{qid}/claim")
async def claim_quest(qid: str, user=Depends(get_user)):
    st = await load_state(user["id"])
    if qid in st.get("daily_claimed", {}): raise HTTPException(400, "Уже получено")
    q = next((x for x in st["daily_quests"] if x["id"] == qid), None)
    if not q: raise HTTPException(404)
    if st["qp"].get(q["s"], 0) < q["t"]: raise HTTPException(400, "Ещё не выполнено")
    
    st.setdefault("daily_claimed", {})[qid] = 1
    st["balance"] += q["r"]
    st["stats"]["earned"] += q["r"]
    await persist(user["id"], st)
    return {"ok": True, "balance": st["balance"], "reward": q["r"]}

# --- Endpoints: Friends & Presence ---
@app.post("/api/ping")
async def ping(user=Depends(get_user)):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO presence(user_id, last_seen) VALUES($1, $2) "
            "ON CONFLICT(user_id) DO UPDATE SET last_seen=$2",
            user["id"], time.time()
        )
    return {"ok": True}

@app.post("/api/friends")
async def add_friend(r: NickReq, user=Depends(get_user)):
    async with pool.acquire() as conn:
        f = await conn.fetchrow("SELECT id FROM users WHERE nick=$1", r.nick)
        if not f: raise HTTPException(404, "Игрок не найден")
        await conn.execute(
            "INSERT INTO friends(user_id, friend_id) VALUES($1, $2) ON CONFLICT DO NOTHING",
            user["id"], f["id"]
        )
    return {"ok": True}

@app.get("/api/friends")
async def list_friends(user=Depends(get_user)):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.id, u.nick, p.last_seen FROM friends f
            JOIN users u ON u.id = f.friend_id
            LEFT JOIN presence p ON p.user_id = u.id
            WHERE f.user_id = $1
        """, user["id"])
    now = time.time()
    return [{"id": r["id"], "nick": r["nick"], "online": bool(r["last_seen"] and now - r["last_seen"] < 40)} for r in rows]

# --- Endpoints: Battles ---
BOT_NAMES = ["BattleBot_3000","Железный","Skynet","КиберВолк","GLaDOS","R2D2"]

def roll_item(case):
    r, acc, chosen = random.random(), 0, list(case["w"].keys())[-1]
    for k, ch in case["w"].items():
        acc += ch
        if r <= acc: chosen = k; break
    pool_items = [i for i in case["items"] if ITEMS[i]["rar"] == chosen]
    return random.choice(pool_items or case["items"])

@app.post("/api/battles")
async def create_battle(r: BattleReq, user=Depends(get_user)):
    if not r.cases or len(r.cases) > 5: raise HTTPException(400, "От 1 до 5 кейсов")
    for cid in r.cases:
        if cid not in CASES: raise HTTPException(400, "Неизвестный кейс")
    
    players = [{"id":user["id"],"nick":user["nick"],"ready":False,"paid":False}]
    if r.mode == "bot":
        players.append({"id":"bot","nick":random.choice(BOT_NAMES),"bot":True,"ready":True,"paid":True})
    
    bid = uuid.uuid4().hex[:10]
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO battles VALUES($1,$2,$3,$4,$5::jsonb,$6,$7::jsonb,$8::jsonb,$9,$10)",
            bid, user["id"], r.mode, r.friend, json.dumps(r.cases),
            "waiting", json.dumps(players), "null", str(user["id"]), time.time()
        )
    return {"id": bid}

def battle_view(b):
    cases = json.loads(b["cases"]) if isinstance(b["cases"], str) else b["cases"]
    players = json.loads(b["players"]) if isinstance(b["players"], str) else b["players"]
    results = json.loads(b["results"]) if b["results"] and b["results"] != "null" else None
    
    # Если results это строка "null", то None
    if isinstance(b["results"], str) and b["results"] == "null": results = None
    
    return {
        "id": b["id"], "mode": b["mode"], "status": b["status"],
        "creator": b["creator"], "target": b["target"],
        "cases": [{"id": c, "name": CASES[c]["name"], "price": CASES[c]["price"]} for c in cases if c in CASES],
        "entry": sum(CASES[c]["price"] for c in cases if c in CASES),
        "players": players,
        "results": results
    }

@app.get("/api/battles")
async def list_battles(user=Depends(get_user)):
    async with pool.acquire() as conn:
        # Доступные (публичные или для друга)
        avail = await conn.fetch("""
            SELECT * FROM battles WHERE status='waiting'
            AND creator != $1 AND (mode='public' OR (mode='friend' AND target=$2))
        """, user["id"], user["nick"])
        
        # Мои (где я создатель или участник)
        mine = await conn.fetch("""
            SELECT * FROM battles WHERE creator=$1 OR members LIKE $2
        """, user["id"], f"%,%{user['id']},%")
    
    seen, out = set(), []
    for b in list(avail) + list(mine):
        if b["id"] in seen: continue
        seen.add(b["id"])
        out.append(battle_view(dict(b)))
    return out

@app.post("/api/battles/{bid}/join")
async def join_battle(bid: str, user=Depends(get_user)):
    async with pool.acquire() as conn:
        b = await conn.fetchrow("SELECT * FROM battles WHERE id=$1", bid)
        if not b or b["status"] != "waiting": raise HTTPException(400, "Батл недоступен")
        
        players = json.loads(b["players"])
        if any(p["id"] == user["id"] for p in players): return {"ok": True}
        if len(players) >= 2: raise HTTPException(400, "Батл занят")
        
        players.append({"id":user["id"],"nick":user["nick"],"ready":False,"paid":False})
        members = ",".join(str(p["id"]) for p in players)
        
        await conn.execute("UPDATE battles SET players=$1::jsonb, members=$2 WHERE id=$3",
                           json.dumps(players), members, bid)
    return {"ok": True}

@app.post("/api/battles/{bid}/ready")
async def ready_battle(bid: str, user=Depends(get_user)):
    async with pool.acquire() as conn:
        b = await conn.fetchrow("SELECT * FROM battles WHERE id=$1", bid)
        if not b or b["status"] != "waiting": raise HTTPException(400, "Батл недоступен")
        
        players = json.loads(b["players"])
        p = next((x for x in players if x["id"] == user["id"]), None)
        if not p: raise HTTPException(400, "Вы не в батле")
        
        cases = json.loads(b["cases"])
        entry = sum(CASES[c]["price"] for c in cases)
        
        st = await load_state(user["id"])
        if not p["paid"]:
            if st["balance"] < entry: raise HTTPException(400, "Не хватает ₽")
            st["balance"] -= entry
            st["stats"]["spent"] += entry
            await persist(user["id"], st)
            p["paid"] = True
        
        p["ready"] = True
        status, results_str = "waiting", "null"
        
        if all(x["ready"] for x in players):
            res = {}
            for pl in players:
                drops = [roll_item(CASES[cid]) for cid in cases]
                res[str(pl["id"])] = {"nick": pl["nick"], "drops": drops, "total": sum(ITEMS[d]["price"] for d in drops)}
            winner = max(res, key=lambda k: res[k]["total"])
            results_str = json.dumps({"winner": winner, "claimed": [], "res": res})
            status = "done"
            
        await conn.execute("UPDATE battles SET players=$1::jsonb, status=$2, results=$3::jsonb WHERE id=$4",
                           json.dumps(players), status, results_str, bid)
    
    return {"status": status, "results": json.loads(results_str) if results_str != "null" else None}

@app.post("/api/battles/{bid}/claim")
async def claim_battle(bid: str, user=Depends(get_user)):
    async with pool.acquire() as conn:
        b = await conn.fetchrow("SELECT * FROM battles WHERE id=$1", bid)
        if not b or b["status"] != "done": raise HTTPException(400, "Батл не завершён")
        
        results = json.loads(b["results"])
        uid = str(user["id"])
        
        if results["winner"] != uid: raise HTTPException(400, "Вы проиграли")
        if uid in results["claimed"]: raise HTTPException(400, "Уже забрано")
        
        st = await load_state(user["id"])
        now = int(time.time()*1000)
        
        # Победитель забирает всё
        for pid, r in results["res"].items():
            for iid in r["drops"]:
                it = ITEMS[iid]
                st["inv"].insert(0, {"uid":"b"+uuid.uuid4().hex[:8], "id":iid, "src":"Батл", "ts":now, "st":"in"})
                st["hist"].insert(0, {"id":iid, "ts":now, "src":"Батл", "price":it["price"]})
                st["stats"]["won"] += it["price"]
        
        st["hist"] = st["hist"][:150]
        results["claimed"].append(uid)
        
        await conn.execute("UPDATE battles SET results=$1::jsonb WHERE id=$2", json.dumps(results), bid)
        await persist(user["id"], st)
        
    return {"ok": True, "balance": st["balance"]}

# --- Static Files (Frontend) ---
# Должен быть в самом конце
if os.path.isdir("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
