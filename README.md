# BOM Kitting Planner

从订单和多级 BOM 计算物料需求，结合库存、在途与已批准替代料，生成可复核的齐料计划和采购申请草稿。

适合离散件装配的缺料分析、采购准备和规则验证。Python 3.11+，运行时无第三方依赖，支持命令行和 Python API。

## 功能

- 多级 BOM 展开，保留需求路径，检查循环、版本和单位。
- 分别扣除冻结、待检、已分配和安全库存；按需求日判断在途可用性。
- 检查替代料审批角色、数量上限和库存共享，计算剩余缺料。
- 比较可比报价，保留淘汰理由，处理最小起订量与包装倍数。
- 输出交期风险、采购申请草稿、检验衔接信息和哈希链记录。
- 导出 JSON、CSV 和 Markdown，支持同一输入与配置的确定性复算。

## 快速开始

```bash
git clone https://github.com/CaesarWANG54/bom-kitting-planner.git
cd bom-kitting-planner
python -m venv .venv
```

激活环境：Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1`；macOS / Linux 使用 `source .venv/bin/activate`。也可以直接调用该环境中的 Python。

```bash
python -m pip install .
python -m bom_kitting_planner plan --input examples/assembly/scenario.json --config examples/assembly/config.json --output outputs/demo
```

查看 `outputs/demo/summary.md`。命令也可写为 `bom-kitting-planner plan ...`。再次运行时请选择新的输出目录，已有计划不会被覆盖。

## 示例结果

示例为 **50 套通用控制组件**；主体、数量、价格和审批均为合成数据。以下展示其中三种物料：

| 物料 | 毛需求 | 可用库存 | 已批准替代 | 剩余缺料 |
|---|---:|---:|---:|---:|
| 控制模块 | 51 | 15 | 36 | 0 |
| 电源模块 | 50 | 10 | 0 | 40 |
| 密封件 | 50 | 20 | 0 | 30 |

电源模块另有 20 件晚于需求日到货，未冲减缺料。密封件缺 30 件，受最小起订量和包装倍数影响，采购草稿建议 40 件。

[输入样例](examples/assembly/scenario.json) · [配置样例](examples/assembly/config.json) · [完整结果摘要](examples/assembly/expected-summary.md)

## 输入与输出

`--input` 接受包含订单、物料、BOM、库存、在途、替代规则、供应商、报价和审批的 JSON 对象，也接受拆分为九个 JSON 文件的目录。CSV 是导出格式。[字段与计算说明](docs/INPUT_FORMAT.md)包含完整数据约定。

每次计划输出：

| 文件 | 内容 |
|---|---|
| `summary.md` | 齐料明细与采购草稿摘要 |
| `materials.csv` / `purchase-requisition.csv` | 供表格工具查看的计划与草稿 |
| `bom_explosion.json` / `kitting_analysis.json` | 需求来源与库存净算 |
| `supplier_comparison.json` / `risk_alerts.json` | 报价比较与风险依据 |
| `purchase_requisition_draft.json` / `iqc_handoff.json` | 采购申请与检验衔接草稿 |
| `audit_log.json` / `run_summary.json` | 输入、配置指纹与执行记录 |

只检查输入和计算、或验证已有记录：

```bash
bom-kitting-planner validate --input examples/assembly/scenario.json --config examples/assembly/config.json
bom-kitting-planner audit outputs/demo/audit_log.json
```

Python API：

```python
from bom_kitting_planner import KittingPlanner, load_json, load_scenario

planner = KittingPlanner(load_json("examples/assembly/config.json"))
result = planner.run(load_scenario("examples/assembly/scenario.json"))
shortages = result["kitting_analysis"]["lines"]
```

## 适用范围

当前版本使用 PCS 整数库存、CNY 报价和自然日历，在每条 BOM 边向上取整；对叶级采购物料净算，子组件库存不参与抵扣。每次独立分析一个订单；跨订单库存预留需由调用方管理。

审批字段由调用方提供，本工具检查其完整性、角色与数量限制，不验证人员身份。输出保持为计划和草稿；没有 ERP 写入、采购订单发送或自动审批功能。哈希链用于发现记录修改，不是数字签名或外部存证。

## 开发与测试

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

CI 在 Windows / Linux、Python 3.11 / 3.13 上检查安装、计算、命令行和示例导出。[架构说明](docs/ARCHITECTURE.md)

## 版权

Copyright © 2026 CaesarWANG54。**公开展示，保留代码版权**；使用或二次开发请先取得授权。见 [LICENSE](LICENSE) 与 [第三方说明](THIRD_PARTY_NOTICES.md)。
