# BOM 齐料计划

合成示例：主体、订单、价格和审批均为虚构数据。

订单：ORDER-001 · 物料需求日：2026-11-24

| 物料 | 毛需求 | 可用库存 | 按期在途 | 已批准替代 | 剩余缺料 |
|---|---:|---:|---:|---:|---:|
| COMPONENT-CABLE | 100 | 100 | 0 | 0 | 0 |
| COMPONENT-CONTROL | 51 | 15 | 0 | 36 | 0 |
| COMPONENT-FASTENER | 204 | 150 | 60 | 0 | 0 |
| COMPONENT-GASKET | 50 | 20 | 0 | 0 | 30 |
| COMPONENT-HOUSING | 54 | 50 | 5 | 0 | 0 |
| COMPONENT-POWER | 50 | 10 | 0 | 0 | 40 |

## 采购申请草稿

| 物料 | 建议数量 | 供应商建议 | 估算金额（CNY） | 复核要求 |
|---|---:|---|---:|---|
| COMPONENT-GASKET | 40 | SUPPLIER-B | 100.00 | 人工复核 |
| COMPONENT-POWER | 40 | SUPPLIER-A | 1000.00 | 采购经理、财务审批 |

建议数量包含最小起订量和包装倍数；草稿尚未形成采购订单。
