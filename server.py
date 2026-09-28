# server.py — CASEFORGE backend
import os, json, time, uuid, sqlite3, random, secrets
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Depends, Header, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import jwt
from passlib.hash import bcrypt

SECRET    = os.getenv("JWT_SECRET", secrets.token_hex(32))
DB_PATH   = os.getenv("DB_PATH",   "data/game.db")
DATA_PATH = os.getenv("DATA_PATH", "data/game_data.json")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

app = FastAPI(title="CASEFORGE API")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ---------- игровые данные (кейсы/скины) ----------
with open(DATA_PATH, encoding="utf-8") as f:
    DATA = json.load(f)
ITEMS = {i["id"]: i for i in DATA["items"]}
CASES = {c["id"]: c for c in DATA["cases"]}

# ---------- БД ----------
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, nick TEXT UNIQUE,
            pass TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS saves(
            user_id INTEGER PRIMARY KEY, state TEXT, updated REAL);
        CREATE TABLE IF NOT EXISTS friends(
            user_id INT, friend_id INT, PRIMARY KEY(user_id, friend_id));
        CREATE TABLE IF NOT EXISTS presence(
            user_id INT PRIMARY KEY, last_seen REAL);
        CREATE TABLE IF NOT EXISTS battles(
            id TEXT PRIMARY KEY, creator INT, mode TEXT, target TEXT,
            cases TEXT, status TEXT, players TEXT, results TEXT,
            members TEXT, created REAL);
        """)
init_db()

# ---------- auth ----------
def make_token(uid):
    return jwt.encode({"uid": uid, "exp": time.time() + 30*86400},
                      SECRET, algorithm="HS256")

def me(authorization: Optional[str] = Header(None)):
    try:
        uid = jwt.decode(authorization.split(" ")[1], SECRET,
                         algorithms=["HS256"])["uid"]
    except Exception:
        raise HTTPException(401, "Требуется вход")
    row = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        raise HTTPException(401, "Пользователь не найден")
    return row

class Auth(BaseModel):
    nick: str
    password: str

def default_state(nick):
    return {"balance":3000,"inv":[],"hist":[],"fast":False,"sound":True,
        "name":nick,"tokens":0,"welcome":True,"cd":{},"wheel":0,
        "daily":{"streak":0,"last":0},"promo":[],"claimed":{},
        "qp":{"free":0,"got":0,"sold":0,"big":0,"gold":0,"upw":0,"ct":0,"wheel":0},
        "daily_quests":[],"daily_claimed":{},"quest_date":None,
        "stats":{"opened":0,"best":0,"spent":0,"won":0,"upW":0,"upL":0,
                 "ct":0,"xp":0,"free":0,"earned":0},
        "created":int(time.time()*1000)}

@app.post("/api/register")
def register(a: Auth):
    if len(a.nick) < 3:  raise HTTPException(400, "Ник минимум 3 символа")
    if len(a.password) < 4: raise HTTPException(400, "Пароль минимум 4 символа")
    try:
        with db() as c:
            cur = c.execute("INSERT INTO users(nick,pass,created) VALUES(?,?,?)",
                            (a.nick, bcrypt.hash(a.password), time.time()))
            uid = cur.lastrowid
            c.execute("INSERT INTO saves VALUES(?,?,?)",
                      (uid, json.dumps(default_state(a.nick)), time.time()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Ник уже занят")
    return {"token": make_token(uid), "nick": a.nick}

@app.post("/api/login")
def login(a: Auth):
    row = db().execute("SELECT * FROM users WHERE nick=?", (a.nick,)).fetchone()
    if not row or not bcrypt.verify(a.password, row["pass"]):
        raise HTTPException(403, "Неверный ник или пароль")
    return {"token": make_token(row["id"]), "nick": row["nick"]}

# ---------- сохранение ----------
def persist(uid, st):
    with db() as c:
        c.execute("INSERT INTO saves(user_id,state,updated) VALUES(?,?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, "
                  "updated=excluded.updated",
                  (uid, json.dumps(st), time.time()))

def load_state(uid):
    row = db().execute("SELECT state FROM saves WHERE user_id=?", (uid,)).fetchone()
    st = json.loads(row["state"]) if row else default_state("F2P")
    # ===== сброс ежедневных заданий каждые 24 часа =====
    if st.get("quest_date") != time.strftime("%Y-%m-%d", time.gmtime()):
        st["quest_date"] = time.strftime("%Y-%m-%d", time.gmtime())
        st["daily_quests"] = daily_quests(st["quest_date"])
        st["daily_claimed"] = {}
    return st

@app.get("/api/state")
def get_state(user=Depends(me)):
    st = load_state(user["id"])
    persist(user["id"], st)
    return st

@app.post("/api/state")
def put_state(state: dict = Body(...), user=Depends(me)):
    old = load_state(user["id"])
    # серверные поля нельзя перезаписать с клиента (анти-чит)
    state["daily_quests"]  = old["daily_quests"]
    state["daily_claimed"] = old["daily_claimed"]
    state["quest_date"]    = old["quest_date"]
    persist(user["id"], state)
    return {"ok": True}

# ---------- ежедневные задания (рестарт раз в 24ч) ----------
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
    rnd = random.Random(date_key)   # детерминированно: все игроки получают
    pool = QUEST_POOL[:]            # одинаковые задания в один день
    rnd.shuffle(pool)
    out = []
    for i, q in enumerate(pool[:5]):
        t = rnd.randint(*q["t"])
        r = int(t * q["k"] * rnd.uniform(.8, 1.3) // 10 * 10)
        out.append({"id":f"d{i}","ic":q["ic"],"n":q["n"].format(t),
                    "d":"Обновляется каждые 24 часа","t":t,"s":q["s"],"r":r})
    return out

@app.post("/api/quests/{qid}/claim")
def claim_quest(qid: str, user=Depends(me)):
    st = load_state(user["id"])
    if qid in st.get("daily_claimed", {}):
        raise HTTPException(400, "Уже получено")
    q = next((x for x in st["daily_quests"] if x["id"] == qid), None)
    if not q: raise HTTPException(404, "Нет такого задания")
    if st["qp"].get(q["s"], 0) < q["t"]:
        raise HTTPException(400, "Задание ещё не выполнено")
    st["daily_claimed"][qid] = 1
    st["balance"] += q["r"]
    st["stats"]["earned"] += q["r"]
    persist(user["id"], st)
    return {"ok": True, "balance": st["balance"], "reward": q["r"]}

# ---------- онлайн-статус и друзья ----------
@app.post("/api/ping")
def ping(user=Depends(me)):
    with db() as c:
        c.execute("INSERT INTO presence(user_id,last_seen) VALUES(?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen",
                  (user["id"], time.time()))
    return {"ok": True}

class NickReq(BaseModel):
    nick: str

@app.post("/api/friends")
def add_friend(r: NickReq, user=Depends(me)):
    f = db().execute("SELECT id FROM users WHERE nick=?", (r.nick,)).fetchone()
    if not f: raise HTTPException(404, "Игрок не найден")
    with db() as c:
        c.execute("INSERT OR IGNORE INTO friends VALUES(?,?)", (user["id"], f["id"]))
    return {"ok": True}

@app.get("/api/friends")
def friends(user=Depends(me)):
    rows = db().execute("""
        SELECT u.id, u.nick, p.last_seen FROM friends f
        JOIN users u ON u.id = f.friend_id
        LEFT JOIN presence p ON p.user_id = u.id
        WHERE f.user_id = ?""", (user["id"],)).fetchall()
    now = time.time()
    return [{"id": r["id"], "nick": r["nick"],
             "online": bool(r["last_seen"] and now - r["last_seen"] < 40)}
            for r in rows]

# ---------- батлы ----------
BOT_NAMES = ["BattleBot_3000","Железный","Skynet","КиберВолк","GLaDOS","R2D2"]

def roll_item(case):
    r, acc, chosen = random.random(), 0, list(case["w"].keys())[-1]
    for k, ch in case["w"].items():
        acc += ch
        if r <= acc:
            chosen = k
            break
    pool = [i for i in case["items"] if ITEMS[i]["rar"] == chosen]
    return random.choice(pool or case["items"])

class BattleReq(BaseModel):
    cases: List[str]
    mode: str = "bot"          # bot | friend | public
    friend: Optional[str] = None

@app.post("/api/battles")
def create_battle(r: BattleReq, user=Depends(me)):
    if not r.cases or len(r.cases) > 5:
        raise HTTPException(400, "От 1 до 5 кейсов")
    for cid in r.cases:
        if cid not in CASES:
            raise HTTPException(400, "Неизвестный кейс")
    players = [{"id":user["id"],"nick":user["nick"],"ready":False,"paid":False}]
    if r.mode == "bot":
        players.append({"id":"bot","nick":random.choice(BOT_NAMES),
                        "bot":True,"ready":True,"paid":True})
    bid = uuid.uuid4().hex[:10]
    with db() as c:
        c.execute("INSERT INTO battles VALUES(?,?,?,?,?,?,?,?,?,?)",
            (bid, user["id"], r.mode, r.friend, json.dumps(r.cases),
             "waiting", json.dumps(players), "null",
             str(user["id"]), time.time()))
    return {"id": bid}

def battle_view(b):
    cases = json.loads(b["cases"])
    return {"id": b["id"], "mode": b["mode"], "status": b["status"],
            "creator": b["creator"], "target": b["target"],
            "cases": [{"id": c, "name": CASES[c]["name"], "price": CASES[c]["price"]}
                      for c in cases],
            "entry": sum(CASES[c]["price"] for c in cases),
            "players": json.loads(b["players"]),
            "results": json.loads(b["results"]) if b["results"] != "null" else None}

@app.get("/api/battles")
def list_battles(user=Depends(me)):
    c = db()
    avail = c.execute("""SELECT * FROM battles WHERE status='waiting'
        AND creator != ? AND (mode='public' OR (mode='friend' AND target=?))""",
        (user["id"], user["nick"])).fetchall()
    mine = c.execute("""SELECT * FROM battles WHERE creator=?
        OR ',' || members || ',' LIKE ?""",
        (user["id"], f"%,%{user['id']},%")).fetchall()
    seen, out = set(), []
    for b in list(avail) + list(mine):
        if b["id"] in seen: continue
        seen.add(b["id"])
        out.append(battle_view(b))
    return out

@app.post("/api/battles/{bid}/join")
def join_battle(bid: str, user=Depends(me)):
    b = db().execute("SELECT * FROM battles WHERE id=?", (bid,)).fetchone()
    if not b or b["status"] != "waiting":
        raise HTTPException(400, "Батл недоступен")
    players = json.loads(b["players"])
    if any(p["id"] == user["id"] for p in players):
        return {"ok": True}
    if len(players) >= 2:
        raise HTTPException(400, "Батл уже занят")
    players.append({"id":user["id"],"nick":user["nick"],"ready":False,"paid":False})
    with db() as c:
        c.execute("UPDATE battles SET players=?, members=? WHERE id=?",
                  (json.dumps(players),
                   ",".join(str(p["id"]) for p in players), bid))
    return {"ok": True}

@app.post("/api/battles/{bid}/ready")
def ready_battle(bid: str, user=Depends(me)):
    b = db().execute("SELECT * FROM battles WHERE id=?", (bid,)).fetchone()
    if not b or b["status"] != "waiting":
        raise HTTPException(400, "Батл недоступен")
    players = json.loads(b["players"])
    p = next((x for x in players if x["id"] == user["id"]), None)
    if not p: raise HTTPException(400, "Вы не в этом батле")
    entry = sum(CASES[c]["price"] for c in json.loads(b["cases"]))
    st = load_state(user["id"])
    if not p["paid"]:
        if st["balance"] < entry:
            raise HTTPException(400, "Не хватает ₽ на вход")
        st["balance"] -= entry
        st["stats"]["spent"] += entry
        persist(user["id"], st)
        p["paid"] = True
    p["ready"] = True
    status, results = "waiting", "null"
    if all(x["ready"] for x in players):
        # симуляция
        res = {}
        for pl in players:
            drops = [roll_item(CASES[cid]) for cid in json.loads(b["cases"])]
            res[str(pl["id"])] = {"nick": pl["nick"], "drops": drops,
                "total": sum(ITEMS[d]["price"] for d in drops)}
        winner = max(res, key=lambda k: res[k]["total"])
        results = json.dumps({"winner": winner, "claimed": [], "res": res})
        status = "done"
    with db() as c:
        c.execute("UPDATE battles SET players=?, status=?, results=? WHERE id=?",
                  (json.dumps(players), status, results, bid))
    return {"status": status,
            "results": json.loads(results) if results != "null" else None}

@app.post("/api/battles/{bid}/claim")
def claim_battle(bid: str, user=Depends(me)):
    b = db().execute("SELECT * FROM battles WHERE id=?", (bid,)).fetchone()
    if not b or b["status"] != "done":
        raise HTTPException(400, "Батл ещё не завершён")
    results = json.loads(b["results"])
    uid = str(user["id"])
    if results["winner"] != uid:
        raise HTTPException(400, "Вы проиграли этот батл")
    if uid in results["claimed"]:
        raise HTTPException(400, "Уже забрано")
    st = load_state(user["id"])
    now = int(time.time()*1000)
    for pid, r in results["res"].items():          # победитель забирает ВСЁ
        for iid in r["drops"]:
            it = ITEMS[iid]
            st["inv"].insert(0, {"uid":"b"+uuid.uuid4().hex[:8],
                                 "id":iid,"src":"Батл","ts":now,"st":"in"})
            st["hist"].insert(0, {"id":iid,"ts":now,"src":"Батл","price":it["price"]})
            st["stats"]["won"] += it["price"]
    st["hist"] = st["hist"][:150]
    results["claimed"].append(uid)
    with db() as c:
        c.execute("UPDATE battles SET results=? WHERE id=?", (json.dumps(results), bid))
    persist(user["id"], st)
    return {"ok": True, "balance": st["balance"]}

# ---------- статика (фронт) — ОБЯЗАТЕЛЬНО после всех /api роутов ----------
app.mount("/", StaticFiles(directory="static", html=True), name="static")
