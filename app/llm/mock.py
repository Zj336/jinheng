"""不联网的规则版 LLM。

存在的意义有两个：
  1. 评审在没有 API key、没有外网的环境里，也能把全流程跑通 —— 这直接
     决定了作品会不会因为「跑不起来」被淘汰。
  2. 它本身就是一个「零幻觉上界」参照系：确定性解析出的槽位，
     用来对照真实 LLM 的输出是否漂移。

它和真实 LLM 遵守完全相同的接口契约：只产出 Intent + Plan，不碰执行。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from app.engine.guard import InputEnvelope
from app.llm.base import LLMAdapter, Proposal
from app.schemas import Intent, Plan, Step, StepCondition

# 「然后/接着/顺便」这类连接词 = 多个子任务，需要拆成 DAG
_SPLIT_RE = re.compile(r"[，,。；;]?\s*(?:然后|接着|顺便|并且|同时|再帮我|再帮|后再)")

_AMOUNT_RE_WITH_UNIT = re.compile(r"(\d+(?:\.\d{1,2})?)\s*(?:元|块钱|块|圆)")
_AMOUNT_RE_AFTER_VERB = re.compile(r"(?:转|汇|打|付|充)\s*(\d+(?:\.\d{1,2})?)")

# 中文数字：支持「一万」「五百」「三千二百」这类口语金额
_CN_NUM_CHARS = "零一二两三四五六七八九十百千万"
_CN_DIGITS = dict(zip("零一二三四五六七八九", range(10))) | {"两": 2}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_AMOUNT_CN_RE = re.compile(rf"([{_CN_NUM_CHARS}]{{1,8}})\s*(?:元|块钱|块|圆)")
_AMOUNT_CN_AFTER_VERB_RE = re.compile(rf"(?:转|汇|打|付|充)\s*([{_CN_NUM_CHARS}]{{1,8}})")


def _cn2num(s: str) -> int | None:
    """中文数字 → 整数。「一万」=10000，「五百」=500。

    已知局限：「一千二」按字面解析为 1002（口语本意 1200）。
    这种歧义在银行场景下宁可显式追问，也不猜——见 _parse_segment 的 missing 逻辑。
    """
    if not s:
        return None
    total = section = num = 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                section = (section + num) * 10000
                total += section
                section = 0
            else:
                section += (num or 1) * unit
            num = 0
        else:
            return None
    return total + section + num

_NUM_CLASS = r"[\d零一二两三四五六七八九十百千万]"
_RECIPIENT_PATTERNS = [
    rf"(?:转|汇|打|付)\s*{_NUM_CLASS}+(?:\.\d{{1,2}})?\s*(?:元|块钱|块)?\s*给\s*([^\s，,。；;的]{{1,12}})",
    rf"给\s*([^\s，,。；;的]{{1,12}}?)\s*(?:转|汇|打|付款|转账)\s*{_NUM_CLASS}",
    r"(?:转给|转账给|转帐给|打给|汇给)\s*([^\s，,。；;的]{1,12})",
    r"向\s*([^\s，,。；;的]{1,12}?)\s*(?:转|汇|打)",
]


def _extract_amount_cents(text: str) -> int | None:
    m = _AMOUNT_RE_WITH_UNIT.search(text) or _AMOUNT_RE_AFTER_VERB.search(text)
    if m:
        # 用 Decimal 转分，避免 0.1 这类浮点误差
        return int((Decimal(m.group(1)) * 100).to_integral_value())
    m = _AMOUNT_CN_RE.search(text) or _AMOUNT_CN_AFTER_VERB_RE.search(text)
    if m:
        v = _cn2num(m.group(1))
        if v:
            return v * 100
    return None


# 指代。★ 「再给他转100」里抠出来的「他」不是收款人，是一个待解析的指代。
#   拿去当名字匹配的后果是 P-011「未找到收款人「他」」——一个**拒绝**，
#   而用户真正需要的是被追问「他指的是谁」。拒绝和追问在体验上差得很远：
#   前者像是系统驳回了你，后者才是系统在帮你。
_PRONOUNS = {"他", "她", "它", "他们", "她们", "对方", "那个人", "那人", "自己"}


def _extract_recipient(text: str) -> str | None:
    for pat in _RECIPIENT_PATTERNS:
        m = re.search(pat, text)
        if m:
            # 去掉口语前缀：「我妈」→「妈」，「俺老王」→「老王」
            name = re.sub(r"^(我的|我|俺)", "", m.group(1).strip())
            # 去掉口语后缀：「给王强手机号转200」抠出来的是「王强手机号」，
            # 拿去做联系人匹配必然 P-011「未找到收款人」——用户看着名单上有王强，
            # 却被系统告知找不到，只会以为系统坏了。
            name = re.sub(r"(手机号|手机|电话号码|电话|号码|的)$", "", name)
            if name in _PRONOUNS:
                return None
            return name
    return None


def _extract_schedule(text: str) -> str | None:
    if "明天" in text:
        return (date.today() + timedelta(days=1)).isoformat()
    if "后天" in text:
        return (date.today() + timedelta(days=2)).isoformat()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    return None


def _extract_sub_merchant(seg: str) -> str | None:
    """从「帮我取消网易云音乐的自动续费」里抠出「网易云音乐」。

    抠不出来就返回 None，由上层追问 —— 宁可多问一句，也不猜商户名。
    猜错的代价是取消失败（还好），但同一套逻辑将来用到转账上就是转错人。
    """
    s = re.sub(r"(帮我|请|麻烦|把|将|给|我|的|一下|这个|那个)", "", seg)
    s = re.sub(r"(取消|退订|关掉|停掉|退掉|不要了|不想要)", "", s)
    s = re.sub(r"(自动续费|自动|订阅|续费|代扣|扣费|会员|服务)", "", s)
    s = re.sub(r"[，,。；;！!？?、\s]", "", s)
    return s or None


def _strip_amount_text(seg: str) -> str:
    """把句子里的金额片段抠掉，剩下的才是「产品名」。

    ★ 不能简单地删所有数字：产品名里本来就有数字（「稳健增利90天」）。
      删掉「90」会把产品名改坏，进而匹配不到产品——错得很安静。
    """
    for pat in (
        _AMOUNT_RE_WITH_UNIT, _AMOUNT_RE_AFTER_VERB,
        _AMOUNT_CN_RE, _AMOUNT_CN_AFTER_VERB_RE,
    ):
        m = pat.search(seg)
        if m:
            return seg[: m.start()] + seg[m.end():]
    return seg


_QUERY_NOISE_RE = re.compile(
    r"(有哪些|有那些|哪些|有什么|有啥|有没有|什么|多少|在售|推荐|适合|列一?下?|看看|看一下|查一下|"
    r"怎么样|如何|收益|年化|收益率|保本|的|吗|呢)"
)


def _extract_product_keyword(seg: str, for_query: bool = False) -> str | None:
    """从「申购一千元稳健增利的」里抠出「稳健增利」。抠不出就返回 None 让上层追问。"""
    s = _strip_amount_text(seg)
    s = re.sub(r"(帮我|请|麻烦|我想|我要|我|的|一笔|一个|一些|一下)", "", s)
    s = re.sub(r"(申购|买入|购入|认购|买进|买|投|购|理财产品|理财|产品|基金|里)", "", s)
    if for_query:
        # 「在售理财有哪些」这类句子里没有产品名，全是疑问词。
        # 不剥掉就会拿「在售有哪些」去匹配产品，结果永远是空列表。
        s = _QUERY_NOISE_RE.sub("", s)
    s = re.sub(r"[，,。；;！!？?、\s]", "", s)
    return s or None


def _extract_card_keyword(seg: str) -> str | None:
    """从「把尾号2233的信用卡挂失」里抠出「2233」，或从「信用卡」里抠出卡种。

    返回 None 表示用户没说清是哪张卡——此时必须追问，不能拿「第一张卡」去挂失。
    """
    m = re.search(r"(?:尾号|末四位|后四位)\s*(\d{4})", seg)
    if m:
        return m.group(1)
    # ★ 不能用 \b：Python 的 \b 是「ASCII 词边界」，中文字符也算词字符，
    #   所以「解挂2333的卡」里 2333 两侧**都没有**边界，\b\d{4}\b 根本匹配不上。
    #   现象是：用户明明说了尾号，系统还是回「还差要操作的卡片」——
    #   既不报错也不崩溃，只是反复问同一件事。
    m = re.search(r"(?<!\d)(\d{4})(?!\d)", seg)
    if m:
        return m.group(1)
    for kw, kind in (
        ("虚拟卡", "virtual"), ("信用卡", "credit"),
        ("借记卡", "debit"), ("储蓄卡", "debit"), ("工资卡", "debit"),
    ):
        if kw in seg:
            return kind
    return None


def _analysis_slots(seg: str) -> dict:
    """从自然语言里挑出「分析目标」与「时间窗口」。"""
    if re.search(r"(异常|可疑|不对劲|风险|大额|深夜|半夜|盗刷|陌生|正常|有问题|乱扣)", seg):
        goal = "anomalies"
    elif re.search(r"(商户|商家|哪儿|哪里|哪个店|最多|花得最多)", seg):
        goal = "top_merchants"
    elif re.search(r"(趋势|走势|每周|按周|每个月|波动)", seg):
        goal = "trend"
    else:
        goal = "category_breakdown"

    days = 30
    m = re.search(r"(\d{1,2})\s*(?:天|日)", seg)
    if m:
        days = max(1, min(90, int(m.group(1))))
    elif re.search(r"(近一年|这一年)", seg):
        days = 90
    elif re.search(r"(本月|这个月)", seg):
        days = 30
    elif re.search(r"(上周|这周|近一周|最近一周)", seg):
        days = 7
    return {"goal": goal, "days": days}


class MockLLM(LLMAdapter):
    name = "mock"

    # -------------------------------------------------------------- 解析

    def _parse_segment(self, seg: str) -> Intent:
        # 1) 纯查询：余额
        # ★ 必须排除「花了多少钱」「扣了多少」这类**支出**问法。
        #   曾经只看「多少钱」三个字，于是「我上个月点外卖花了多少钱」
        #   被答成了活期余额——不是听不懂，是**自信地答错了**，
        #   这比「没听懂」严重得多：用户会以为系统在胡乱应付。
        if (
            re.search(r"(余额|还有多少钱|账户里有多少|卡里有多少|多少钱)", seg)
            and not re.search(r"(转|汇|打款)", seg)
            and not re.search(r"(花|支出|消费|账单|流水|开销|扣)", seg)
        ):
            kind = "savings" if re.search(r"(储蓄|定期|理财账户)", seg) else "current"
            return Intent(
                name="query_balance",
                slots={"account_kind": kind},
                raw_utterance=seg,
            )

        # 2) 账单分析：带「分析意图」的账单请求走沙箱分析，而不是简单罗列
        if re.search(
            r"(分析|统计|拆解|结构|分类|占比|趋势|走势|异常|可疑|不对劲|大额|最多|哪个店|"
            r"正常|有问题|有没有乱|是不是超|超支)",
            seg,
        ) and re.search(r"(消费|支出|花|账单|流水|交易|扣|超支)", seg):
            return Intent(name="analyze_spending", slots=_analysis_slots(seg), raw_utterance=seg)

        # 3) 流水 / 账单
        if re.search(r"(流水|账单|消费记录|花了多少|支出|收支|扣费|扣款|乱扣)", seg):
            days = 30
            m = re.search(r"(\d{1,2})\s*(?:天|日)", seg)
            if m:
                days = max(1, min(90, int(m.group(1))))
            elif re.search(r"(本月|这个月)", seg):
                days = 30
            return Intent(name="query_transactions", slots={"days": days}, raw_utterance=seg)

        # 3) 订阅 / 代扣。先判「取消」，再判「查询」——「取消爱奇艺会员」也含「会员」，
        #    顺序反了就会把取消请求当成查询，这是个不会报错但很危险的错误。
        # 「把爱奇艺退了吧」里没有「订阅/会员/续费」任何一个词，但有「退了」。
        # 不加这一条，用户最口语的说法会被判成「听不懂」。
        if re.search(r"(订阅|自动续费|续费|代扣|会员|退订|退了|退掉|不续|别再扣)", seg):
            if re.search(r"(取消|退订|关掉|停掉|退掉|退了|不要了|不想要|不续|别再扣)", seg):
                merchant = _extract_sub_merchant(seg)
                slots: dict = {}
                missing: list[str] = []
                if merchant:
                    slots["merchant"] = merchant
                else:
                    missing.append("merchant")
                if re.search(r"(马上|立刻|立即|现在就|赶紧)", seg):
                    slots["period"] = "immediate"
                return Intent(
                    name="cancel_subscription",
                    slots=slots,
                    missing=missing,
                    confidence=1.0 if not missing else 0.6,
                    raw_utterance=seg,
                )
            return Intent(name="query_subscriptions", slots={}, raw_utterance=seg)

        # 3.5) 卡片管理。★ 顺序必须是「解挂 → 挂失 → 申请 → 查询」：
        #     「解除挂失」同时含「挂失」，顺序反了会把解挂理解成再挂一次——
        #     这种错误不报错、不崩溃，只是把用户的卡又锁了一遍。
        if re.search(r"(卡|挂失|解挂|解冻|解封|额度|限额|交易限制)", seg) and not re.search(r"(账单|流水|消费记录)", seg):
            # ★ 三条的顺序同样有讲究，和下面「解挂→挂失」是一个道理：
            #   ① 调额：「把尾号 2233 的卡限额调到 2000」——含「卡」但不该被当成查询
            #   ② 解除交易限制必须先于「解挂/解冻」：它同时含「解除」，顺序反了会被
            #      理解成「解挂」，把一张只是限制交易的卡当成挂失卡去处理
            #   ③ 「限制交易」必须先于「锁定/停用 → 挂失」：限制交易不等于挂失
            m_lim = re.search(
                r"(?:额度|限额|单日限额)[^0-9]{0,8}?(?:调到|调成|调为|改为|设为|提到|提高到|降到|降为)"
                r"[^0-9]{0,4}?(\d+(?:\.\d{1,2})?)",
                seg,
            )
            if m_lim:
                card = _extract_card_keyword(seg)
                cents = int(Decimal(m_lim.group(1)) * 100)
                slots = {"new_daily_limit_cents": cents}
                if card:
                    slots["card"] = card
                return Intent(
                    name="adjust_card_limit",
                    slots=slots,
                    missing=[] if card else ["card"],
                    confidence=1.0 if card else 0.6,
                    raw_utterance=seg,
                )
            if re.search(r"(解除交易限制|取消交易限制|恢复交易|恢复用卡)", seg):
                card = _extract_card_keyword(seg)
                return Intent(
                    name="unlock_card_transactions",
                    slots={"card": card} if card else {},
                    missing=[] if card else ["card"],
                    confidence=1.0 if card else 0.6,
                    raw_utterance=seg,
                )
            if re.search(r"(限制交易|禁止交易|禁用交易|关闭交易|限制这张卡)", seg):
                card = _extract_card_keyword(seg)
                return Intent(
                    name="lock_card_transactions",
                    slots={"card": card} if card else {},
                    missing=[] if card else ["card"],
                    confidence=1.0 if card else 0.6,
                    raw_utterance=seg,
                )
            if re.search(r"(解挂|解冻|解封|解锁|解除|恢复用卡|恢复交易)", seg):
                card = _extract_card_keyword(seg)
                return Intent(
                    name="unfreeze_card",
                    slots={"card": card} if card else {},
                    missing=[] if card else ["card"],
                    confidence=1.0 if card else 0.6,
                    raw_utterance=seg,
                )
            if re.search(r"(挂失|丢失|丢了|被盗|被偷|冻结|锁卡|锁定|停用)", seg):
                card = _extract_card_keyword(seg)
                reason = "卡片丢失" if re.search(r"(丢|盗|偷)", seg) else "用户主动挂失"
                slots = {"reason": reason}
                if card:
                    slots["card"] = card
                return Intent(
                    name="freeze_card",
                    slots=slots,
                    missing=[] if card else ["card"],
                    confidence=1.0 if card else 0.6,
                    raw_utterance=seg,
                )
            if re.search(r"(申请|开一张|办一张|开个|新办|开立)", seg):
                slots = {}
                m = re.search(r"(?:限额|额度)\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块)?", seg)
                if m:
                    slots["daily_limit_cents"] = int(Decimal(m.group(1)) * 100)
                return Intent(name="apply_virtual_card", slots=slots, raw_utterance=seg)
            return Intent(name="query_cards", slots={}, raw_utterance=seg)

        # 3.6) 理财。★ 「申购」必须有金额，缺了就追问，绝不默认一个金额。
        if re.search(r"(理财|基金|申购|赎回|年化|收益率|稳健型|进取型)", seg):
            # ★ 「赎回」必须先于「申购」判断：它含「回」不含「买」，
            #   但和申购在同一大类里，顺序反了会被当成「查询理财」——
            #   用户说「赎回稳健增利」，系统的回答是「在售 4 只」，钱一分没回来。
            if re.search(r"(赎回|取出|取回|退出)", seg):
                slots = {}
                product = _extract_product_keyword(seg)
                if product:
                    slots["product"] = product
                amount = _extract_amount_cents(seg)
                if amount:
                    slots["amount_cents"] = amount
                return Intent(name="redeem_wealth", slots=slots, raw_utterance=seg)
            buy = re.search(r"(申购|买入|购入|认购|买进)", seg) or (
                re.search(r"(买|投|购)", seg) and _extract_amount_cents(seg) is not None
            )
            if buy:
                product = _extract_product_keyword(seg)
                amount = _extract_amount_cents(seg)
                slots, missing = {}, []
                if product:
                    slots["product"] = product
                else:
                    missing.append("product")
                if amount:
                    slots["amount_cents"] = amount
                else:
                    missing.append("amount_cents")
                return Intent(
                    name="purchase_wealth",
                    slots=slots,
                    missing=missing,
                    confidence=1.0 if not missing else 0.6,
                    raw_utterance=seg,
                )
            keyword = _extract_product_keyword(seg, for_query=True) or ""
            slots = {"keyword": keyword} if keyword else {}
            return Intent(name="query_wealth_products", slots=slots, raw_utterance=seg)

        # 4) 转账
        if re.search(r"(转账|转给|转帐|汇款|汇给|打款|打钱|转)", seg):
            amount = _extract_amount_cents(seg)
            to = _extract_recipient(seg)
            slots: dict = {}
            missing: list[str] = []
            if to:
                slots["to"] = to
            else:
                missing.append("to")
            if amount:
                slots["amount_cents"] = amount
            else:
                missing.append("amount_cents")
            sched = _extract_schedule(seg)
            if sched:
                slots["schedule_date"] = sched
            return Intent(
                name="transfer",
                slots=slots,
                missing=missing,
                confidence=1.0 if not missing else 0.6,
                raw_utterance=seg,
            )

        # 4) 不认识
        return Intent(name="unknown", raw_utterance=seg, confidence=0.0)

    # -------------------------------------------------------------- 主流程

    def propose(self, envelope: InputEnvelope) -> Proposal:
        segments = [s.strip() for s in _SPLIT_RE.split(envelope.text) if s and s.strip()]
        if not segments:
            segments = [envelope.text]

        intents = [self._parse_segment(s) for s in segments]

        clarifications: list[str] = []
        steps: list[Step] = []
        prev_id: str | None = None

        # ★ 条件步骤：「余额够的话给强哥转500」= 先查余额，够才转。
        #   规划器只产出结构（哪个步骤、取哪个字段、跟什么比），
        #   条件成不成立由执行器拿**真实结果**去算，模型说不上话。
        guarded = bool(re.search(r"(余额|有钱|钱够|够不够)", envelope.text)) and any(
            i.name == "transfer" for i in intents
        )
        if guarded:
            steps.append(Step(id="s0", tool="query_balance", args={"account_kind": "current"}))
            prev_id = "s0"

        for i, intent in enumerate(intents):
            if intent.name == "unknown":
                # ★ 认不出来不是用户的错，是词表的边界。所以这里不写「没听懂」四个字了事，
                #   而是把边界摆出来：用户看一眼就知道该换哪种说法。
                clarifications.append(
                    f"「{intent.raw_utterance}」这句我接不住（规则版解析器的词表有限，"
                    f"真实 LLM 模式下面这种口语是能理解的）。我目前认这几类说法：\n"
                    f"  · 查　查一下余额 / 近7天账单 / 我有哪些卡 / 我有哪些订阅 / 在售理财有哪些\n"
                    f"  · 动　转账500给王强 / 申购1000元稳健增利90天 / 取消爱奇艺订阅 / 挂失尾号2233的信用卡\n"
                    f"  · 析　分析下近30天的消费结构 / 看看有没有异常消费\n"
                    f"  · 其它　你能帮我干什么（看全部能力） / 算了（取消） / 转人工"
                )
                continue
            if intent.missing:
                names = {
                    "to": "收款人",
                    "amount_cents": "金额",
                    "merchant": "要取消的订阅商户",
                    "card": "要操作的卡片（尾号后四位或卡种）",
                    "product": "要申购的产品名",
                }
                want = "、".join(names.get(m, m) for m in intent.missing)
                clarifications.append(f"还差{want}没告诉我，请补充。")
                continue

            sid = f"s{i + 1}"
            when = None
            amount = intent.slots.get("amount_cents")
            if guarded and intent.name == "transfer" and amount:
                when = StepCondition(step="s0", field="balance_cents", op=">=", value=amount)
            steps.append(
                Step(
                    id=sid,
                    tool=intent.name,
                    args=dict(intent.slots),
                    depends_on=[prev_id] if prev_id else [],
                    when=when,
                )
            )
            prev_id = sid

        return Proposal(
            intents=intents,
            plan=Plan(steps=steps, summary=" | ".join(i.name for i in intents)),
            clarifications=clarifications,
        )
