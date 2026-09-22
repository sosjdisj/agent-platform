"""Seed 脚本：按 docs/case-chain-sales-drop-data-design.md 生成评估案例链数据。

用法：python -m app.seed
- 幂等：每次执行先清空业务表（refunds / orders / sales_records / customers / products / customer_tickets）
  再全量重建，随机部分使用固定种子，重复执行结果完全一致。
- 案例链：客户 A（CUST-0001）核心产品 P1 近 3 个月订单逐月下降；客户 B（CUST-0002）为对照组。
- 其余客户 / 产品 / 订单随机生成但数值合理（金额 = 数量 × 单价）。
- 工单按文档 3.5 落库（文本仅描述现象，不含归因语句）；知识文档 Markdown 落盘到
  KNOWLEDGE_DOCS_DIR（默认 data/knowledge_docs），供后续 RAG / 向量化使用。
"""
import asyncio
import calendar
import random
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import SessionLocal, engine
from app.models.customer import Customer
from app.models.customer_ticket import CustomerTicket
from app.models.order import Order
from app.models.product import Product
from app.models.refund import Refund
from app.models.sales_record import SalesRecord

# 固定随机种子：保证幂等（重复执行生成完全相同的数据）
RANDOM_SEED = 20260919

# 评估时点 2026-09-19 的数据窗口：6 个完整自然月（2026-03 .. 2026-08）
YEAR = 2026
MONTHS = range(3, 9)

# ---------- 4.1 文档固定案例数据 ----------

CASE_CUSTOMERS = [
    {"code": "CUST-0001", "name": "华信智造", "industry": "制造业", "region": "华东", "contact_name": "陈敏"},
    {"code": "CUST-0002", "name": "苏南精工", "industry": "制造业", "region": "华东", "contact_name": "赵磊"},
]

CASE_PRODUCTS = [
    {"sku": "SKU-XS100", "name": "工业传感器 XS-100", "price": "1200.00"},
    {"sku": "SKU-DC20", "name": "数据线缆 DC-20", "price": "150.00"},
]

# 案例订单：(产品索引, 日, 数量)，按月依次排列——客户 A 的 P1 逐月下降 / P2 平稳，客户 B 全程平稳
# 产品索引对应 CASE_PRODUCTS / CASE_PRODUCTS 顺序（0=P1 核心产品，1=P2 次要产品），实际 id 运行时映射
CASE_ORDERS = {
    0: [(0, 5, 500), (0, 8, 520), (0, 9, 480), (0, 10, 300), (0, 8, 180), (0, 6, 120),
        (1, 15, 40), (1, 15, 40), (1, 15, 40), (1, 15, 40), (1, 15, 40), (1, 15, 40)],
    1: [(0, 7, 470), (0, 10, 500), (0, 12, 480), (0, 15, 510), (0, 10, 490), (0, 8, 520),
        (1, 18, 60), (1, 18, 60), (1, 18, 60), (1, 18, 60), (1, 18, 60), (1, 18, 60)],
}

# 客户 A 核心产品 P1 的预期月度销售额（元），seed 后自校验，与文档 3.4 一致
EXPECTED_A_P1_MONTHLY = [600000, 624000, 576000, 360000, 216000, 144000]

# 案例工单（文档 3.5，customer_index 对应 CASE_CUSTOMERS 顺序：0=A，1=B）
# 文本仅描述现象（缺货数量 / 延迟天数 / 业务影响 / 客户表态），不含任何归因语句——
# 答案需结合订单时序交叉推理得出，避免数据自曝
CASE_TICKETS = [
    {
        "customer_index": 0,
        "ticket_no": "T-2606-001",
        "title": "XS-100 部分到货延迟",
        "content": (
            "6 月订单原定 15 日交付的 100 台 XS-100，目前仅到货 40 台，"
            "剩余部分已延迟一周以上，影响我方产线排期，请尽快协调。"
        ),
        "priority": "high",
        "created_at": datetime(2026, 6, 18, tzinfo=timezone.utc),
        "resolved_at": datetime(2026, 6, 25, tzinfo=timezone.utc),
    },
    {
        "customer_index": 0,
        "ticket_no": "T-2607-003",
        "title": "连续第二个月缺货",
        "content": (
            "7 月订单 180 台中缺货 60 台，这是连续第二个月出现缺货。"
            "我方下游订单无法按时交付，已被迫调整生产计划。"
        ),
        "priority": "high",
        "created_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
        "resolved_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
    },
    {
        "customer_index": 0,
        "ticket_no": "T-2608-002",
        "title": "质疑后续供货稳定性",
        "content": (
            "8 月交付虽已完成，但近三个月供货多次出现缺货与延迟。"
            "若 9 月仍无法保障 XS-100 的稳定供应，我方将考虑调整采购渠道。"
        ),
        "priority": "urgent",
        "created_at": datetime(2026, 8, 25, tzinfo=timezone.utc),
        "resolved_at": None,  # 未解决：暗示流失风险
    },
    {
        "customer_index": 1,
        "ticket_no": "T-2604-001",
        "title": "发票抬头信息更正",
        "content": "我司已完成名称变更，请将后续发票抬头更新为新名称，信息见附件。",
        "priority": "low",
        "created_at": datetime(2026, 4, 20, tzinfo=timezone.utc),
        "resolved_at": datetime(2026, 4, 22, tzinfo=timezone.utc),
    },
    {
        "customer_index": 1,
        "ticket_no": "T-2607-001",
        "title": "咨询 XS-100 新版本功能",
        "content": "想了解 XS-100 新版本与现采购版本的功能差异，以及是否支持现场升级。",
        "priority": "medium",
        "created_at": datetime(2026, 7, 12, tzinfo=timezone.utc),
        "resolved_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
    },
]

# 知识文档（文件名, Markdown 内容）：通用企业制度 / 产品资料，不含任何案例客户的事实信息，
# 供 KnowledgeAgent 后续做 RAG 检索（Embedding 属 Prompt 9，本期只落盘）
KNOWLEDGE_DOCS: list[tuple[str, str]] = [
    (
        "退款政策.md",
        """# 退款政策

> 适用范围：本公司全部产品销售订单（合同另有约定的从其约定）。

## 1. 退款条件

1. 产品存在质量问题，经售后部门确认无法通过维修或更换解决的，支持全额退款；
2. 交付延误超过合同约定交期 30 天，且客户书面拒绝延期收货的，支持全额退款；
3. 非质量原因的退款（客户单方面取消），按以下标准收取手续费：
   - 未发货：免收手续费；
   - 已发货未签收：收取订单金额的 5%；
   - 已签收 15 日内且包装完好：收取订单金额的 10%；
   - 已签收超过 15 日：原则上不再受理退款。

## 2. 退款流程

1. 客户通过客户经理或客服渠道提交退款申请，注明订单号与退款原因；
2. 客服 2 个工作日内完成受理与初审；
3. 金额 5 万元以下由客服主管审批，5 万元（含）以上需销售总监会签；
4. 审批通过后 10 个工作日内按原支付路径退回。

## 3. 注意事项

- 定制类产品已投入生产的部分不予退款；
- 退款不免除已生效合同中的违约责任条款。
""",
    ),
    (
        "销售制度.md",
        """# 销售管理制度

## 1. 订单管理

1. 销售订单一律录入系统，未经审批不得私下承诺交期或价格；
2. 标准品订单由销售主管审批；单笔金额超过 50 万元的订单需销售总监审批；
3. 订单变更（数量、交期、收货信息）须由客户经理发起书面申请，留档备查。

## 2. 定价与折扣

1. 销售价格以产品目录价为基础，折扣权限：客户经理 95 折、销售主管 9 折、销售总监 85 折；
2. 低于 85 折的报价须提交定价委员会审议；
3. 同一客户同一产品在同一季度内执行统一价格，不得随意变动。

## 3. 客户分级与回款

1. 客户按年度采购额分为战略（≥500 万）、重点（≥100 万）、普通（<100 万）三级；
2. 战略客户由客户经理每月至少上门回访一次，重点客户每季度至少一次；
3. 应收账款逾期 30 天须升级至销售主管，逾期 60 天冻结新订单。
""",
    ),
    (
        "库存管理制度.md",
        """# 库存管理制度

## 1. 安全库存

1. 每个产品按近 3 个月平均销量的 1.5 倍设定安全库存线，每月复核一次；
2. 库存低于安全库存线时，仓储系统自动触发补货预警，通知采购与计划部门。

## 2. 补货流程

1. 采购部门收到补货预警后 2 个工作日内下达采购单，标准品采购周期不超过 15 天；
2. 因供应商原因无法按期补货的，须在 1 个工作日内上报销售部门，由销售部门
   主动联系受影响客户说明情况并协商替代方案。

## 3. 缺货处理

1. 出现缺货时，优先保障已确认订单，按订单确认时间顺序安排发货；
2. 部分到货时，仓储部门须在到货当日同步客户经理，由客户经理告知客户
   实际到货数量与预计补齐时间；
3. 连续两个月出现同一产品缺货的，须上报运营总监并启动供应商评审。

## 4. 出入库管理

1. 出入库均须凭单据操作，做到账、卡、物一致；
2. 每月末进行库存盘点，盘盈盘亏须查明原因并于 3 个工作日内处理完毕。
""",
    ),
    (
        "客户服务规范.md",
        """# 客户服务规范

## 1. 响应时限

| 渠道 | 首次响应 | 问题闭环 |
|------|----------|----------|
| 电话 | 即时 | - |
| 工单 | 4 小时内 | 按优先级：urgent 24 小时 / high 48 小时 / medium 5 个工作日 / low 10 个工作日 |
| 邮件 | 1 个工作日内 | 同工单标准 |

## 2. 投诉处理与升级

1. 投诉工单按影响程度分为 urgent / high / medium / low 四级，分级标准：
   - urgent：客户明确表示将终止合作或转向其他供应商；
   - high：已影响客户生产、交付或造成经济损失；
   - medium：影响体验但未造成实际损失；
   - low：一般咨询与信息更正类；
2. urgent 级投诉须升级至客户服务总监，并由客户经理同步跟进；
3. 处理完成后须回访确认客户满意度，回访记录随工单归档。

## 3. 服务红线

- 不得承诺制度范围外的补偿；
- 不得向客户透露未经核实的问题原因，回复内容以已确认的事实为准。
""",
    ),
    (
        "产品说明-工业传感器XS-100.md",
        """# 产品说明：工业传感器 XS-100

## 1. 产品概述

XS-100 是面向工业自动化场景的高精度通用传感器，广泛应用于产线监测、
设备状态采集与质量检测等环节。

## 2. 主要参数

| 参数 | 取值 |
|------|------|
| 量程 | 0 ~ 5000 单位 |
| 精度 | ±0.05% FS |
| 采样频率 | 最高 10 kHz |
| 工作温度 | -20 ℃ ~ 85 ℃ |
| 供电 | DC 24V |
| 通讯接口 | RS-485 / Modbus RTU |
| 防护等级 | IP67 |

## 3. 型号与配套

- 目录价：1200 元/台；
- 标准包装：单台独立防静电包装，50 台/箱；
- 配套线缆：DC-20 数据线缆（另购）；
- 质保期：自交付之日起 18 个月（详见售后服务制度）。

## 4. 安装与维护

1. 安装须由具备资质的技术人员操作，避免强电磁干扰环境；
2. 建议每 6 个月进行一次现场校准；
3. 固件支持现场升级，升级前请备份当前配置。
""",
    ),
    (
        "售后服务制度.md",
        """# 售后服务制度

## 1. 保修政策

1. 标准产品自交付之日起提供 18 个月保修，人为损坏与不可抗力除外；
2. 保修期内非人为故障提供免费维修或更换，更换件重新计算剩余保修期。

## 2. 维修与更换流程

1. 客户提交维修申请，注明产品型号、序列号与故障现象；
2. 售后部门 1 个工作日内响应，远程无法解决的 3 个工作日内安排现场处理；
3. 维修完成后由客户签收确认，处理记录随工单归档。

## 3. 技术支持

1. 提供工作日 9:00-18:00 电话与邮件技术支持；
2. 产品说明书、安装手册与固件升级包通过客户经理统一发放；
3. 重大版本升级前提前 10 个工作日通知在册客户，并提供升级影响评估。
""",
    ),
]

RANDOM_CUSTOMER_COUNT = 20  # 随机客户数（案例 2 客户之外），总计 22 >= 20
RANDOM_PRODUCT_COUNT = 10  # 随机产品数（案例 2 产品之外），总计 12 >= 10

_CUSTOMER_PREFIXES = ["华创", "联恒", "锐特", "博远", "天工", "凯盛", "启明", "恒力", "中晟", "威泰",
                      "朗科", "海润", "正达", "云帆", "德普", "兴华", "瑞丰", "领航", "金隅", "宏远",
                      "新络", "蓝湾", "卓立", "越海"]
_CUSTOMER_SUFFIXES = ["智造", "精工", "电子", "科技", "装备", "仪器", "自动化", "材料", "光电", "仪表"]
_PRODUCT_CATEGORIES = ["温度传感器", "压力变送器", "PLC 模块", "工业网关", "伺服驱动器", "编码器",
                       "连接器", "继电器", "电源模块", "控制仪表", "变频器", "数据采集卡"]


def _month_end(year: int, month: int) -> datetime:
    """该月最后一天（UTC），作为月度 sales_records 的 sale_date。"""
    last_day = calendar.monthrange(year, month)[1]
    return datetime(year, month, last_day, tzinfo=timezone.utc)


def _random_customers(start_code: int, count: int) -> list[dict]:
    """生成随机客户主数据（制造业），名称不重复，code 从 start_code 起递增。"""
    rng = random.Random(RANDOM_SEED)
    names: set[str] = set()
    while len(names) < count:
        names.add(rng.choice(_CUSTOMER_PREFIXES) + rng.choice(_CUSTOMER_SUFFIXES))
    return [
        {
            "code": f"CUST-{start_code + i:04d}",
            "name": name,
            "industry": "制造业",
            "region": rng.choice(["华东", "华北", "华南", "西南"]),
            "contact_name": rng.choice(["王强", "李娜", "张伟", "刘洋", "周涛", "吴静", "郑凯", "孙悦"]),
        }
        for i, name in enumerate(sorted(names))
    ]


def _random_products(count: int) -> list[dict]:
    """生成随机产品主数据，价格 100~3000 元，品类不重复。"""
    rng = random.Random(RANDOM_SEED + 1)
    categories = rng.sample(_PRODUCT_CATEGORIES, count)
    return [
        {
            "sku": f"SKU-R{i:03d}",
            "name": f"{category} R-{i:03d}",
            "price": Decimal(rng.randrange(100, 3000)).quantize(Decimal("0.01")),
        }
        for i, category in enumerate(categories, start=1)
    ]


def _build_orders(
    case_customer_ids: list[int],
    random_customer_ids: list[int],
    case_product_ids: list[int],
    product_prices: dict[int, Decimal],
) -> list[dict]:
    """收集全部订单（案例固定 + 随机），按日期排序后统一生成唯一 order_no。

    案例订单通过运行时 id 映射关联（不依赖自增序列状态，保证幂等）。
    随机规则：每个随机客户对每个产品每月约 55% 概率下单 1 单；
    高价产品（>=800 元）单量 200~600，低价产品 30~80。
    """
    rng = random.Random(RANDOM_SEED + 2)
    rows: list[tuple[int, int, int, int, int]] = []  # (customer_id, product_id, month, day, qty)

    for customer_index, specs in CASE_ORDERS.items():
        customer_id = case_customer_ids[customer_index]
        for offset, (product_index, day, qty) in enumerate(specs):
            month = MONTHS.start + offset % len(MONTHS)  # 每产品 6 条，逐月对应 2026-03..08
            rows.append((customer_id, case_product_ids[product_index], month, day, qty))

    for customer_id in random_customer_ids:
        for product_id, price in product_prices.items():
            if product_id in case_product_ids:  # 案例产品的随机订单不生成，避免干扰对照数据
                continue
            for month in MONTHS:
                if rng.random() >= 0.55:
                    continue
                qty = rng.randrange(200, 600) if price >= 800 else rng.randrange(30, 80)
                rows.append((customer_id, product_id, month, rng.randrange(1, 29), qty))

    rows.sort(key=lambda r: (r[2], r[3]))  # 按月、日排序后逐月编号，保证 order_no 唯一且可读
    orders: list[dict] = []
    monthly_seq: dict[int, int] = defaultdict(int)
    for customer_id, product_id, month, day, qty in rows:
        monthly_seq[month] += 1
        orders.append(
            {
                "customer_id": customer_id,
                "product_id": product_id,
                "order_date": datetime(YEAR, month, day, tzinfo=timezone.utc),
                "quantity": qty,
                "amount": product_prices[product_id] * qty,
                "order_no": f"ORD-{YEAR % 100}{month:02d}-{monthly_seq[month]:03d}",
            }
        )
    return orders


def _build_sales_records(orders: list[dict]) -> list[dict]:
    """由订单按（客户, 产品, 月）汇总生成月度 sales_records——单一事实源，两表数值严格一致。"""
    monthly: dict[tuple[int, int, int], list[int]] = defaultdict(lambda: [0, 0])  # key -> [qty, amount 分]
    for order in orders:
        key = (order["customer_id"], order["product_id"], order["order_date"].month)
        monthly[key][0] += order["quantity"]
        monthly[key][1] += int(order["amount"] * 100)  # 分为单位累加，避免浮点误差

    return [
        {
            "customer_id": customer_id,
            "product_id": product_id,
            "sale_date": _month_end(YEAR, month),
            "quantity": qty,
            "amount": Decimal(amount_cents).scaleb(-2),
        }
        for (customer_id, product_id, month), (qty, amount_cents) in sorted(monthly.items())
    ]


def _build_tickets(customer_ids: list[int]) -> list[dict]:
    """按 CASE_TICKETS 构建工单行，customer_index 通过运行时 id 映射关联（保证幂等）。"""
    return [
        {key: value for key, value in spec.items() if key != "customer_index"}
        | {"customer_id": customer_ids[spec["customer_index"]]}
        for spec in CASE_TICKETS
    ]


def write_knowledge_docs() -> int:
    """将知识文档 Markdown 落盘到 KNOWLEDGE_DOCS_DIR（覆盖写入，天然幂等）。返回文件数。"""
    target = get_settings().knowledge_docs_path
    target.mkdir(parents=True, exist_ok=True)
    for filename, content in KNOWLEDGE_DOCS:
        (target / filename).write_text(content, encoding="utf-8")
    return len(KNOWLEDGE_DOCS)


async def reset(session: AsyncSession) -> None:
    """清空六张业务表（先引用后主数据），保证幂等。不触碰 users / RBAC 等其他表。"""
    await session.execute(delete(Refund))
    await session.execute(delete(CustomerTicket))
    await session.execute(delete(SalesRecord))
    await session.execute(delete(Order))
    await session.execute(delete(Customer))
    await session.execute(delete(Product))
    await session.commit()


async def seed(session: AsyncSession) -> dict[str, int]:
    """写入全部案例数据，返回各类数据行数。"""
    customers = CASE_CUSTOMERS + _random_customers(3, RANDOM_CUSTOMER_COUNT)
    products = CASE_PRODUCTS + _random_products(RANDOM_PRODUCT_COUNT)

    customer_rows = [Customer(**c) for c in customers]
    product_rows = [Product(**{**p, "price": Decimal(str(p["price"]))}) for p in products]
    session.add_all(customer_rows + product_rows)
    await session.flush()  # 取得自增 id，供订单引用

    product_prices = {p.id: p.price for p in product_rows}
    orders = _build_orders(
        case_customer_ids=[c.id for c in customer_rows[:2]],
        random_customer_ids=[c.id for c in customer_rows[2:]],
        case_product_ids=[p.id for p in product_rows[:2]],
        product_prices=product_prices,
    )
    sales_records = _build_sales_records(orders)
    tickets = _build_tickets([c.id for c in customer_rows])

    session.add_all([Order(**o) for o in orders])
    session.add_all([SalesRecord(**s) for s in sales_records])
    session.add_all([CustomerTicket(**t) for t in tickets])
    await session.commit()

    return {
        "customers": len(customer_rows),
        "products": len(product_rows),
        "orders": len(orders),
        "sales_records": len(sales_records),
        "customer_tickets": len(tickets),
    }


async def verify(session: AsyncSession) -> None:
    """数据量统计 SQL 校验 + 客户 A 核心产品下降趋势校验，不达标即抛异常。"""
    counts = {
        "customers": await session.scalar(select(func.count()).select_from(Customer)),
        "products": await session.scalar(select(func.count()).select_from(Product)),
        "orders": await session.scalar(select(func.count()).select_from(Order)),
        "sales_records": await session.scalar(select(func.count()).select_from(SalesRecord)),
    }
    print(f"行数统计: {counts}")
    assert counts["customers"] >= 20, f"客户数不足 20：{counts['customers']}"
    assert counts["products"] >= 10, f"产品数不足 10：{counts['products']}"
    assert counts["orders"] >= 50, f"订单数不足 50：{counts['orders']}"
    assert counts["sales_records"] >= 3 * 20, "sales_records 月份窗口不足 3 个月"

    # 客户 A（CUST-0001）核心产品 P1 的月度销售额趋势（P1 按 sku 动态定位，不依赖自增 id）
    p1_id = await session.scalar(select(Product.id).where(Product.sku == "SKU-XS100"))
    stmt = (
        select(SalesRecord.sale_date, SalesRecord.amount)
        .join(Customer, Customer.id == SalesRecord.customer_id)
        .where(Customer.code == "CUST-0001", SalesRecord.product_id == p1_id)
        .order_by(SalesRecord.sale_date)
    )
    monthly = [int(amount) for _, amount in (await session.execute(stmt)).all()]
    print(f"客户 A P1 月度销售额: {monthly}")
    assert monthly == EXPECTED_A_P1_MONTHLY, f"客户 A 趋势与设计不符：{monthly}"
    assert monthly[3] > monthly[4] > monthly[5], "近 3 个月未呈逐月下降趋势"

    # 对照组客户 B（CUST-0002）同期 P1 应平稳（波动幅度 < 15%）
    stmt = (
        select(SalesRecord.amount)
        .join(Customer, Customer.id == SalesRecord.customer_id)
        .where(Customer.code == "CUST-0002", SalesRecord.product_id == p1_id)
        .order_by(SalesRecord.sale_date)
    )
    b_monthly = [int(amount) for (amount,) in (await session.execute(stmt)).all()]
    print(f"客户 B P1 月度销售额: {b_monthly}")
    assert (max(b_monthly) - min(b_monthly)) / min(b_monthly) < 0.15, f"对照组波动过大：{b_monthly}"

    # 工单：与 4.1 文档 3.5 一致——A 3 条交付类投诉（其中 1 条未解决），B 2 条日常工单
    a_id = await session.scalar(select(Customer.id).where(Customer.code == "CUST-0001"))
    b_id = await session.scalar(select(Customer.id).where(Customer.code == "CUST-0002"))
    stmt = select(CustomerTicket.customer_id, func.count()).group_by(CustomerTicket.customer_id)
    ticket_counts = dict((await session.execute(stmt)).all())
    assert ticket_counts.get(a_id) == 3 and ticket_counts.get(b_id) == 2, (
        f"工单分布与设计不符：{ticket_counts}"
    )
    unresolved = await session.scalar(
        select(func.count()).select_from(CustomerTicket)
        .where(CustomerTicket.customer_id == a_id, CustomerTicket.resolved_at.is_(None))
    )
    assert unresolved == 1, f"客户 A 未解决工单数应为 1（T-2608-002）：{unresolved}"

    # 知识文档：全部落盘且非空
    docs_dir = get_settings().knowledge_docs_path
    missing = [name for name, _ in KNOWLEDGE_DOCS if not (docs_dir / name).is_file()
               or (docs_dir / name).stat().st_size == 0]
    assert not missing, f"知识文档缺失或为空：{missing}"

    print("校验通过：数据量达标，客户 A 呈设计下降趋势，对照组平稳，工单与知识文档就绪。")


async def main() -> None:
    async with SessionLocal() as session:
        await reset(session)
        counts = await seed(session)
        counts["knowledge_docs"] = write_knowledge_docs()
        print(f"Seed 完成: {counts}")
        await verify(session)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
