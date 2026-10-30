"""sqlite3 数据访问层。

约定：
  * 金额字段一律以「分」为单位的整数（*_cents），禁止 float。
  * 时间统一 ISO8601 字符串，便于 substr(ts,1,10) 取日期做当日累计。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterator

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL,
    kyc_level   INTEGER NOT NULL DEFAULT 1,
    -- ★ 角色只能由后台写入。任何「我是管理员」的自然语言声明都到不了这一列。
    role        TEXT NOT NULL DEFAULT 'customer',
    created_at  TEXT NOT NULL,
    -- 登录口令。★ 存 PBKDF2 哈希而不是明文/裸哈希：
    -- 6 位纯数字的裸 sha256 与明文没有实质区别。
    pin_salt    TEXT,
    pin_hash    TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    kind          TEXT NOT NULL,          -- current / savings / virtual_card
    currency      TEXT NOT NULL DEFAULT 'CNY',
    balance_cents INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS contacts (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    phone      TEXT,
    account_no TEXT,
    bank       TEXT,
    aliases    TEXT,                      -- 逗号分隔，支持"妈""老妈"这类口语备注
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id            TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    ts            TEXT NOT NULL,
    direction     TEXT NOT NULL,          -- in / out
    amount_cents  INTEGER NOT NULL,
    counterparty  TEXT,
    category      TEXT,
    memo          TEXT
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    merchant         TEXT NOT NULL,
    amount_cents     INTEGER NOT NULL,
    period           TEXT NOT NULL,       -- monthly / yearly
    next_charge_date TEXT,
    status           TEXT NOT NULL DEFAULT 'active'
);

-- 银行卡。kind: debit 借记卡 / credit 信用卡 / virtual 虚拟卡
-- status: active 正常 / frozen 已挂失 / locked 交易锁定
CREATE TABLE IF NOT EXISTS cards (
    id                 TEXT PRIMARY KEY,
    user_id            TEXT NOT NULL,
    kind               TEXT NOT NULL,
    brand              TEXT NOT NULL,
    last4              TEXT NOT NULL,
    credit_limit_cents INTEGER NOT NULL DEFAULT 0,
    daily_limit_cents  INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'active',
    frozen_reason      TEXT,
    created_at         TEXT NOT NULL
);

-- 理财产品。annual_bp = 年化收益率，万分比整数（350 = 3.50%），全系统零浮点。
CREATE TABLE IF NOT EXISTS wealth_products (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    kind             TEXT NOT NULL,        -- 稳健 / 平衡 / 进取
    annual_bp        INTEGER NOT NULL,
    min_amount_cents INTEGER NOT NULL,
    lock_days        INTEGER NOT NULL DEFAULT 0,
    risk_level       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'on_sale'
);

-- 理财订单。★ 刻意不写 transfer_ledger：
--    理财申购不是转账，不能占用转账的当日额度，否则用户买完理财就转不出钱了。
CREATE TABLE IF NOT EXISTS wealth_orders (
    id             TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    product_id     TEXT NOT NULL,
    product_name   TEXT NOT NULL,
    amount_cents   INTEGER NOT NULL,
    ts             TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'executed',
    expected_annual_cents INTEGER NOT NULL DEFAULT 0,
    trace_id       TEXT
);

-- 转账台账：权限分级里「当日累计」的唯一事实来源
CREATE TABLE IF NOT EXISTS transfer_ledger (
    id             TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    ts             TEXT NOT NULL,
    amount_cents   INTEGER NOT NULL,
    contact_id     TEXT,
    counterparty   TEXT,
    status         TEXT NOT NULL,         -- executed / scheduled / failed
    decision_level TEXT,
    trace_id       TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id   TEXT NOT NULL,
    ts         TEXT NOT NULL,
    user_id    TEXT,
    session_id TEXT,
    stage      TEXT NOT NULL,             -- input/intent/plan/validate/decide/execute/render
    payload    TEXT NOT NULL,             -- JSON
    -- ★ 哈希链：每一行都咬着上一行。改任何一行，后面全部对不上。
    --   「可审计」和「不可篡改」是两件事：一张普通的表，有库权限就能改，
    --   改完还看不出来。加了这两列，「可审计」才有证据。
    prev_hash  TEXT,
    row_hash   TEXT
);

-- 模拟短信网关。★ 动态码**不出现在聊天回复里**：
--   聊天通道正是攻击者能改写的那条通道，把验证码从同一条通道发出去，
--   等于把钥匙和锁一起交给对方。这个表就是「手机」那一侧。
CREATE TABLE IF NOT EXISTS sms_messages (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    ts         TEXT NOT NULL,
    text       TEXT NOT NULL,
    code       TEXT,
    kind       TEXT NOT NULL DEFAULT 'otp'
);

CREATE TABLE IF NOT EXISTS confirmations (
    id         TEXT PRIMARY KEY,
    trace_id   TEXT,
    session_id TEXT,
    user_id    TEXT,
    step_id    TEXT,
    kind       TEXT NOT NULL,             -- confirm / mfa
    payload    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    expires_at TEXT
);

-- 人工接管工单。自动流程处理不了的时候，交给人的那条「交接棒」必须留下痕迹：
-- 谁、什么时候、因为什么转人工、带过去多少条审计记录。
CREATE TABLE IF NOT EXISTS handoff_tickets (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    session_id TEXT,
    trace_id   TEXT,
    reason     TEXT NOT NULL,
    audit_rows INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_user_day ON transfer_ledger(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_cards_user     ON cards(user_id);
CREATE INDEX IF NOT EXISTS idx_wealth_user    ON wealth_orders(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_trace     ON audit_log(trace_id);
CREATE INDEX IF NOT EXISTS idx_audit_session   ON audit_log(session_id);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """每次操作开一个连接。数据量小，简单优于连接池。"""
    os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """给老库补列。

    ★ `CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列：
      已经跑过的 data/bank.db 不会因为这里改了 SCHEMA 就自动长出 pin_salt。
      不补的话，升级后第一次登录就是「no such column」——现场崩，且看起来像鉴权写错了。
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    for col in ("pin_salt", "pin_hash"):
        if col not in have:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
    # 角色：只能由后台/种子数据写入，永远不来自用户输入。
    if "role" not in have:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'customer'")

    have_a = {r["name"] for r in conn.execute("PRAGMA table_info(audit_log)").fetchall()}
    added = False
    for col in ("prev_hash", "row_hash"):
        if col not in have_a:
            conn.execute(f"ALTER TABLE audit_log ADD COLUMN {col} TEXT")
            added = True
    if added:
        # 老库里已有的审计行没有指纹。不补的话，新行会从这个洞开始链接，
        # 而校验器一上来就报「链断了」——看起来像被篡改过，实际只是没迁移。
        backfill_audit_chain(conn)


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def reset_db() -> None:
    """清库重建。demo 脚本和测试用。"""
    if os.path.exists(settings.db_path):
        for suffix in ("", "-wal", "-shm"):
            p = settings.db_path + suffix
            if os.path.exists(p):
                os.remove(p)
    init_db()


# ------------------------------------------------------------------ 查询

def get_user(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def set_user_pin(conn: sqlite3.Connection, user_id: str, pin: str) -> None:
    from app.auth import hash_pin, new_salt   # 延迟导入：db 层不依赖鉴权实现

    salt = new_salt()
    conn.execute(
        "UPDATE users SET pin_salt=?, pin_hash=? WHERE id=?",
        (salt, hash_pin(pin, salt), user_id),
    )


def check_user_pin(conn: sqlite3.Connection, user_id: str, pin: str) -> bool:
    from app.auth import check_pin

    row = get_user(conn, user_id)
    if row is None:
        return False
    # 列可能不存在于极老的库上，用 keys() 兜一下，但取值一律走 fail-closed
    data = dict(row)
    return check_pin(pin, data.get("pin_salt"), data.get("pin_hash"))


def get_account(conn: sqlite3.Connection, user_id: str, kind: str = "current") -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM accounts WHERE user_id=? AND kind=? AND status='active'", (user_id, kind)
    ).fetchone()


def find_contacts(conn: sqlite3.Connection, user_id: str, keyword: str) -> list[sqlite3.Row]:
    """按姓名 / 备注 / 手机号模糊匹配联系人。只在本人的联系人里找——
    这条约束是「越权」防线的一部分，永远不允许跨用户检索。"""
    kw = f"%{keyword}%"
    return conn.execute(
        """SELECT * FROM contacts
           WHERE user_id=? AND (name LIKE ? OR aliases LIKE ? OR phone LIKE ?)""",
        (user_id, kw, kw, kw),
    ).fetchall()


def get_contact(conn: sqlite3.Connection, user_id: str, contact_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM contacts WHERE id=? AND user_id=?", (contact_id, user_id)
    ).fetchone()


def daily_transferred_cents(conn: sqlite3.Connection, user_id: str, day: str | None = None) -> int:
    """当日已执行转账累计（分）。★ 权限分级的核心状态源。

    必须是「日累计」而不是「单笔」——否则用户拆单即可绕过黄色阈值。
    """
    day = day or date.today().isoformat()
    row = conn.execute(
        """SELECT COALESCE(SUM(amount_cents), 0) AS s FROM transfer_ledger
           WHERE user_id=? AND status='executed' AND substr(ts,1,10)=?""",
        (user_id, day),
    ).fetchone()
    return int(row["s"])


def monthly_transferred_cents(conn: sqlite3.Connection, user_id: str, month: str | None = None) -> int:
    """当月已执行转账累计（分）。日限额防「一天之内拆单」，月限额防「天天拆单」。"""
    month = month or date.today().isoformat()[:7]
    row = conn.execute(
        """SELECT COALESCE(SUM(amount_cents), 0) AS s FROM transfer_ledger
           WHERE user_id=? AND status='executed' AND substr(ts,1,7)=?""",
        (user_id, month),
    ).fetchone()
    return int(row["s"])


def transferred_to_contact_today_cents(
    conn: sqlite3.Connection, user_id: str, contact_id: str, day: str | None = None
) -> int:
    """今天已经给**这个收款人**转了多少钱（分）。

    ★ 单日总额度拦不住「分散到多个人」的洗钱结构；收款人级限额是另一道闸。
    """
    day = day or date.today().isoformat()
    row = conn.execute(
        """SELECT COALESCE(SUM(amount_cents), 0) AS s FROM transfer_ledger
           WHERE user_id=? AND contact_id=? AND status='executed' AND substr(ts,1,10)=?""",
        (user_id, contact_id, day),
    ).fetchone()
    return int(row["s"])


def has_paid_contact(conn: sqlite3.Connection, user_id: str, contact_id: str) -> bool:
    """是否曾经给这个联系人转过账（用于「首次收款人」升级判定）。"""
    row = conn.execute(
        "SELECT 1 FROM transfer_ledger WHERE user_id=? AND contact_id=? AND status='executed' LIMIT 1",
        (user_id, contact_id),
    ).fetchone()
    return row is not None


def recent_transactions(conn: sqlite3.Connection, user_id: str, days: int = 30) -> list[sqlite3.Row]:
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT * FROM transactions WHERE user_id=? AND ts>=? ORDER BY ts DESC",
        (user_id, since),
    ).fetchall()


def active_subscriptions(conn: sqlite3.Connection, user_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM subscriptions WHERE user_id=? AND status='active' ORDER BY amount_cents DESC",
        (user_id,),
    ).fetchall()


def find_subscriptions(
    conn: sqlite3.Connection, user_id: str, keyword: str
) -> list[sqlite3.Row]:
    """按商户名模糊匹配当前用户的有效订阅。

    ★ 永远带 user_id 条件——与联系人查询同一条规矩，越权不是靠上层记得检查，
      是靠这层根本查不出别人的数据。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    rows = active_subscriptions(conn, user_id)
    exact = [r for r in rows if r["merchant"] == kw]
    if exact:
        return exact
    return [r for r in rows if kw in r["merchant"] or r["merchant"] in kw]


def get_subscription(
    conn: sqlite3.Connection, user_id: str, subscription_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM subscriptions WHERE id=? AND user_id=?",
        (subscription_id, user_id),
    ).fetchone()


def set_subscription_status(
    conn: sqlite3.Connection, user_id: str, subscription_id: str, status: str
) -> bool:
    cur = conn.execute(
        "UPDATE subscriptions SET status=? WHERE id=? AND user_id=?",
        (status, subscription_id, user_id),
    )
    return cur.rowcount > 0


# ------------------------------------------------------------------ 卡

CARD_KIND_CN = {"debit": "借记卡", "credit": "信用卡", "virtual": "虚拟卡"}


def list_cards(conn: sqlite3.Connection, user_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cards WHERE user_id=? ORDER BY created_at", (user_id,)
    ).fetchall()


def find_cards(conn: sqlite3.Connection, user_id: str, keyword: str) -> list[sqlite3.Row]:
    """按卡号后四位 / 卡种中文名解析「哪张卡」。

    ★ 与联系人查询同一条规矩：永远带 user_id 条件。
      越权不是靠上层记得检查，是靠这层根本查不出别人的卡。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    rows = list_cards(conn, user_id)
    if kw.startswith("card_"):
        return [r for r in rows if r["id"] == kw]
    if kw.isdigit() and len(kw) == 4:
        return [r for r in rows if r["last4"] == kw]
    exact = [r for r in rows if CARD_KIND_CN.get(r["kind"], "") == kw]
    if exact:
        return exact
    return [
        r for r in rows
        if kw in (r["last4"] or "")
        or kw in (r["brand"] or "")
        or kw in CARD_KIND_CN.get(r["kind"], "")
    ]


def get_card(conn: sqlite3.Connection, user_id: str, card_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cards WHERE id=? AND user_id=?", (card_id, user_id)
    ).fetchone()


def insert_card(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    kind: str,
    brand: str,
    daily_limit_cents: int,
    credit_limit_cents: int = 0,
) -> sqlite3.Row:
    """发一张新卡。后四位由数据库里现有卡号派生，保证同一用户不撞号。"""
    while True:
        last4 = f"{uuid.uuid4().int % 10000:04d}"
        clash = conn.execute(
            "SELECT 1 FROM cards WHERE user_id=? AND last4=?", (user_id, last4)
        ).fetchone()
        if clash is None:
            break
    cid = new_id("card_")
    conn.execute(
        "INSERT INTO cards(id,user_id,kind,brand,last4,credit_limit_cents,daily_limit_cents,"
        "status,frozen_reason,created_at) VALUES (?,?,?,?,?,?,?,'active',NULL,?)",
        (cid, user_id, kind, brand, last4, credit_limit_cents, daily_limit_cents, now_iso()),
    )
    return get_card(conn, user_id, cid)


def set_card_status(
    conn: sqlite3.Connection, user_id: str, card_id: str, status: str, reason: str | None = None
) -> bool:
    cur = conn.execute(
        "UPDATE cards SET status=?, frozen_reason=? WHERE id=? AND user_id=?",
        (status, reason, card_id, user_id),
    )
    return cur.rowcount > 0


# ------------------------------------------------------------------ 理财

def list_wealth_products(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM wealth_products WHERE status='on_sale' ORDER BY annual_bp DESC"
    ).fetchall()


def find_wealth_products(conn: sqlite3.Connection, keyword: str) -> list[sqlite3.Row]:
    """按产品名 / 类型（稳健、平衡、进取）模糊匹配在售产品。"""
    kw = (keyword or "").strip()
    rows = list_wealth_products(conn)
    if not kw:
        return rows
    if kw.startswith("wp_"):
        return [r for r in rows if r["id"] == kw]
    exact = [r for r in rows if r["name"] == kw]
    if exact:
        return exact
    return [r for r in rows if kw in r["name"] or kw == r["kind"] or kw in r["kind"]]


def get_wealth_product(conn: sqlite3.Connection, product_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM wealth_products WHERE id=? AND status='on_sale'", (product_id,)
    ).fetchone()


def insert_wealth_order(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    product: sqlite3.Row,
    amount_cents: int,
    trace_id: str = "",
) -> str:
    oid = new_id("wo_")
    # 预计年化收益：整数分运算再四舍五入，不引入 float
    expected = (amount_cents * int(product["annual_bp"]) + 5_000) // 10_000
    conn.execute(
        "INSERT INTO wealth_orders(id,user_id,product_id,product_name,amount_cents,ts,"
        "status,expected_annual_cents,trace_id) VALUES (?,?,?,?,?,?,'executed',?,?)",
        (oid, user_id, product["id"], product["name"], amount_cents, now_iso(),
         expected, trace_id),
    )
    return oid


def list_wealth_orders(conn: sqlite3.Connection, user_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM wealth_orders WHERE user_id=? ORDER BY ts DESC", (user_id,)
    ).fetchall()


# ------------------------------------------------------------------ 审计

AUDIT_GENESIS = "GENESIS"


def _audit_digest(prev_hash: str, row: tuple) -> str:
    """一行的指纹 = H(上一行指纹 ‖ 本行全部字段)。

    ★ 用 \\x1f 分隔而不是直接拼接：不分隔的话 "ab"+"c" 和 "a"+"bc" 指纹相同，
      构造两行不同的记录得到同一个哈希，链就白加了。
    """
    h = hashlib.sha256()
    h.update((prev_hash or AUDIT_GENESIS).encode("utf-8"))
    for part in row:
        h.update(b"\x1f")
        h.update(("" if part is None else str(part)).encode("utf-8"))
    return h.hexdigest()


def audit(
    conn: sqlite3.Connection,
    *,
    trace_id: str,
    stage: str,
    payload: dict[str, Any] | Any,
    user_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """写入审计日志。所有关键阶段都必须调用它——这是「可审计」的物理落点。

    ★ 每行都咬着上一行（哈希链）。`prev_hash` 用 INSERT…SELECT 在**写入的那一刻**
      取表的真实尾部，而不是先 SELECT 再 INSERT——后者的读快照可能是旧的，
      高并发下会分叉出一条虚假的平行链。
    """
    if not isinstance(payload, (dict, list)):
        payload = {"value": str(payload)}
    ts = now_iso()
    # sort_keys：同一份 payload 的两种字典顺序不能算出两个指纹
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    conn.execute(
        """INSERT INTO audit_log(trace_id, ts, user_id, session_id, stage, payload, prev_hash)
           SELECT ?, ?, ?, ?, ?, ?,
                  COALESCE((SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1), ?)""",
        (trace_id, ts, user_id, session_id, stage, text, AUDIT_GENESIS),
    )
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    prev = conn.execute(
        "SELECT prev_hash FROM audit_log WHERE id=?", (row_id,)
    ).fetchone()["prev_hash"]
    digest = _audit_digest(prev, (trace_id, ts, user_id, session_id, stage, text))
    conn.execute("UPDATE audit_log SET row_hash=? WHERE id=?", (digest, row_id))


def verify_audit_chain(
    conn: sqlite3.Connection, session_id: str | None = None, limit: int = 5000
) -> dict[str, Any]:
    """逐行重算哈希链。返回 {ok, checked, first_bad_id, reason}。

    ★ 这是「可审计」和「不可篡改」的分界线：改任意一行的 payload、改一行的时间、
      删一行、插一行，后面的 prev_hash 全部对不上，一个接口就能证明。
    """
    sql = "SELECT * FROM audit_log"
    args: tuple = ()
    if session_id:
        sql += " WHERE session_id=?"
        args = (session_id,)
    sql += " ORDER BY id DESC LIMIT ?"
    rows = conn.execute(sql, (*args, limit)).fetchall()
    rows = list(reversed(rows))

    prev = AUDIT_GENESIS
    checked = 0
    for r in rows:
        if r["prev_hash"] != prev:
            return {
                "ok": False, "checked": checked, "first_bad_id": r["id"],
                "reason": f"第 {r['id']} 行的 prev_hash 与上一行的 row_hash 不一致（{r['prev_hash']} ≠ {prev}）",
            }
        want = _audit_digest(
            r["prev_hash"], (r["trace_id"], r["ts"], r["user_id"], r["session_id"], r["stage"], r["payload"])
        )
        if want != r["row_hash"]:
            return {
                "ok": False, "checked": checked, "first_bad_id": r["id"],
                "reason": f"第 {r['id']} 行的内容与它自己的指纹不符——这一行被改过",
            }
        prev = r["row_hash"]
        checked += 1
    return {"ok": True, "checked": checked, "first_bad_id": None,
            "reason": f"连续校验 {checked} 行，链完整", "head": prev}


def backfill_audit_chain(conn: sqlite3.Connection) -> int:
    """给迁移前的老库补一条从内容现算的链。

    诚实说明：这些行是**迁移时按当时内容**生成的指纹，因此它们保证的是
    「迁移之后没被改过」，而不是「迁移之前没被改过」。生产环境应当从第一天就开链。
    """
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    prev = AUDIT_GENESIS
    n = 0
    for r in rows:
        digest = _audit_digest(
            prev, (r["trace_id"], r["ts"], r["user_id"], r["session_id"], r["stage"], r["payload"])
        )
        conn.execute("UPDATE audit_log SET prev_hash=?, row_hash=? WHERE id=?", (prev, digest, r["id"]))
        prev = digest
        n += 1
    return n


def audit_trail(conn: sqlite3.Connection, trace_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE trace_id=? ORDER BY id", (trace_id,)
    ).fetchall()
    return [
        {
            "id": r["id"],
            "ts": r["ts"],
            "stage": r["stage"],
            "payload": json.loads(r["payload"]),
            # 前端把这两个值显示出来，「可审计」才看得见摸得着
            "prev_hash": r["prev_hash"],
            "row_hash": r["row_hash"],
        }
        for r in rows
    ]


def audit_trail_by_session(
    conn: sqlite3.Connection, session_id: str, limit: int = 200
) -> list[dict[str, Any]]:
    """整段会话的审计链，按时间正序。

    ★ trace_id 是「一轮对话」的粒度：一次多轮操作会散成十几条 trace。
      人工接管工单、事故复现、安全自评要的都是「整段会话」。
      limit 只截最近 N 条，避免一次会话跑几百轮后把响应撑爆。
    """
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, max(1, min(limit, 2000))),
    ).fetchall()
    return [
        {
            "ts": r["ts"],
            "stage": r["stage"],
            "trace_id": r["trace_id"],
            "user_id": r["user_id"],
            "payload": json.loads(r["payload"]),
            "prev_hash": r["prev_hash"],
            "row_hash": r["row_hash"],
        }
        for r in reversed(rows)
    ]
