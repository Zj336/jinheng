"""演示数据。

刻意种了三种「坑」进去，演示时直接打：
  1. 联系人「妈」——首次转账，触发新收款人升级
  2. 订阅里有一个假装是「会员」实则是自动续费的商户
  3. 近 30 天流水里埋了 3 笔异常（深夜、异地、整数大额）
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.bank.db import connect, init_db, new_id, now_iso


def _ts(days_ago: int = 0, hour: int = 10, minute: int = 0) -> str:
    d = datetime.now() - timedelta(days=days_ago)
    return d.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat(timespec="seconds")


DEMO_PINS = {"u_1001": "123456", "u_2001": "654321"}


def ensure_pins(quiet: bool = True) -> None:
    """给「升级前就存在」的老库补上演示口令。

    ★ 必须只补 `pin_hash` 为空的账号，绝不能覆盖已有口令——
      否则每次重启都把用户改过的口令重置成 123456，那是后门，不是兼容。
    """
    from app.bank.db import set_user_pin

    with connect() as conn:
        rows = conn.execute("SELECT id, pin_hash FROM users").fetchall()
        for row in rows:
            if row["pin_hash"]:
                continue
            pin = DEMO_PINS.get(row["id"])
            if pin:
                set_user_pin(conn, row["id"], pin)
                if not quiet:
                    print(f"[seed] 已为 {row['id']} 补演示口令")


def seed(reset: bool = True, quiet: bool = False) -> None:
    if reset:
        from app.bank.db import reset_db

        reset_db()
    else:
        init_db()

    uid = "u_1001"
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users(id,name,phone,kyc_level,created_at) VALUES (?,?,?,?,?)",
            (uid, "李明", "13800138000", 2, now_iso()),
        )
        # 第二个用户：用于验证「跨用户联系人隔离」，防止越权转账
        conn.execute(
            "INSERT OR REPLACE INTO users(id,name,phone,kyc_level,created_at) VALUES (?,?,?,?,?)",
            ("u_2001", "张伟", "13500135000", 2, now_iso()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO contacts(id,user_id,name,phone,account_no,bank,aliases,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("c_stranger", "u_2001", "赵四", "13400134000", "6212340000998877", "农业银行", "四哥", now_iso()),
        )
        # 演示登录口令。★ 两个用户口令不同，是为了能直白地演示
        # 「知道 u_2001 这个 id 也拿不到他的数据」——想拿令牌先要过他的口令。
        from app.bank.db import set_user_pin

        for _uid, _pin in DEMO_PINS.items():
            set_user_pin(conn, _uid, _pin)

        # ---- 账户 ----
        conn.executemany(
            "INSERT OR REPLACE INTO accounts(id,user_id,kind,currency,balance_cents,status)"
            " VALUES (?,?,?,?,?,?)",
            [
                ("acc_current", uid, "current", "CNY", 2_468_050, "active"),   # 24,680.50
                ("acc_savings", uid, "savings", "CNY", 12_000_000, "active"),  # 120,000.00
            ],
        )

        # ---- 联系人 ----
        conn.executemany(
            "INSERT OR REPLACE INTO contacts(id,user_id,name,phone,account_no,bank,aliases,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [
                ("c_mother", uid, "张秀兰", "13900139000", "6222020200112233", "工商银行", "妈,老妈,母亲", now_iso()),
                ("c_wang", uid, "王强", "13700137000", "6217000000114455", "建设银行", "强哥,老王", now_iso()),
                ("c_landlord", uid, "陈房东", "13600136000", "6214830000116677", "招商银行", "房东,房租", now_iso()),
            ],
        )

        # ---- 近 30 天流水 ----
        tx = [
            # (days_ago, hour, direction, cents, counterparty, category, memo)
            (1, 8, "out", 1800, "瑞幸咖啡", "餐饮", "美式"),                         # 正常
            (1, 12, "out", 3500, "美团外卖", "餐饮", "午餐"),
            (2, 19, "out", 45000, "盒马鲜生", "日用", "囤货"),
            (3, 9, "in", 1_800_000, "南京工程学院", "收入", "工资"),
            (5, 21, "out", 9900, "爱奇艺", "娱乐", "会员"),                          # 订阅
            (7, 2, "out", 500_000, "未知商户", "其他", "消费"),                      # ⚠️ 深夜大额
            (9, 14, "out", 2500, "滴滴出行", "交通", "打车"),
            (12, 3, "out", 300_000, "未知商户", "其他", "消费"),                     # ⚠️ 深夜大额
            (15, 23, "out", 199_000, "某境外商户", "其他", "购物"),                  # ⚠️ 深夜异地
            (18, 10, "out", 6800, "肯德基", "餐饮", "早餐"),
            (22, 16, "out", 12800, "京东", "日用", "生活用品"),
            (26, 11, "out", 9900, "网易云音乐", "娱乐", "会员"),
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO transactions(id,account_id,user_id,ts,direction,amount_cents,"
            "counterparty,category,memo) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (new_id("t_"), "acc_current", uid, _ts(d, h), di, cents, cp, cat, memo)
                for (d, h, di, cents, cp, cat, memo) in tx
            ],
        )

        # ---- 订阅 ----
        conn.executemany(
            "INSERT OR REPLACE INTO subscriptions(id,user_id,merchant,amount_cents,period,"
            "next_charge_date,status) VALUES (?,?,?,?,?,?,?)",
            [
                ("s_iqiyi", uid, "爱奇艺", 9900, "monthly", _ts(-8).split("T")[0], "active"),
                ("s_netease", uid, "网易云音乐", 800, "monthly", _ts(-13).split("T")[0], "active"),
                ("s_icloud", uid, "iCloud 存储", 600, "monthly", _ts(-5).split("T")[0], "active"),
                ("s_gym", uid, "超级猩猩健身", 29900, "monthly", _ts(-21).split("T")[0], "active"),
            ],
        )

        # ---- 银行卡（u_1001 两张：借记 + 信用；u_2001 一张，用于越权测试）----
        conn.executemany(
            "INSERT OR REPLACE INTO cards(id,user_id,kind,brand,last4,credit_limit_cents,"
            "daily_limit_cents,status,frozen_reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("card_debit", uid, "debit", "招商银行", "6688", 0, 500_000, "active", None, _ts(400)),
                ("card_credit", uid, "credit", "银联", "2233", 5_000_000, 300_000, "active", None, _ts(300)),
                ("card_other", "u_2001", "debit", "农业银行", "8899", 0, 500_000, "active", None, _ts(200)),
            ],
        )

        # ---- 理财产品 ----
        # annual_bp 是万分比整数：285 = 2.85%。全系统零浮点。
        conn.executemany(
            "INSERT OR REPLACE INTO wealth_products(id,name,kind,annual_bp,min_amount_cents,"
            "lock_days,risk_level,status) VALUES (?,?,?,?,?,?,?,?)",
            [
                ("wp_cash",    "天天盈货币",   "稳健", 175, 100,       0,   "低", "on_sale"),
                ("wp_steady",  "稳健增利90天", "稳健", 285, 10_000,    90,  "低", "on_sale"),
                ("wp_balance", "平衡优选180天", "平衡", 350, 100_000,  180, "中", "on_sale"),
                ("wp_growth",  "进取成长一年", "进取", 520, 500_000,  365, "高", "on_sale"),
            ],
        )

    if not quiet:
        print(
            f"[seed] 已生成演示数据 -> 用户 {uid} 李明（口令 123456）/ 联系人 3 人 / "
            f"流水 {len(tx)} 条 / 订阅 4 项 / 卡 2 张 / 理财 4 只\n"
            f"[seed] 越权对照账号 u_2001 张伟（口令 654321）—— 不知道口令就换不到他的令牌"
        )


if __name__ == "__main__":
    seed()
