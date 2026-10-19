# hegui-shencha —— 合规审查（尽调双模型初筛）

本地双模型工作流：Saluki-27B 全量初筛 + Python 确定性数字复算 + Qwen3.5-9B-Q8 对抗性复核，产出**待人工终审**的红旗登记册。

## 前置条件
- 本地已装 Ollama，并导入两个模型：
  - 初筛：`saluki27b:latest`（qwen35 家族，IQ2_XS）
  - 复核：`qwen3.5:9b-q8_0`（Q8_0）
- 依赖 CLI：`pdftotext`（poppler）、`pandoc`（或用 skill 自带的 python-docx 兜底）、`openpyxl`（DSH runtime Python 自带）
- 输入材料放 `~/Desktop/合规审查/`（设计约定，可在 SKILL.md 顶部改）

## 安装
```bash
npx skills add <owner>/hegui-shencha
# 或在 DSH 内：POST http://127.0.0.1:19387/api/dsh-skills-manager/catalog/install
#   {"catalog":"skillsh","source":"<owner>/hegui-shencha","skillId":"hegui-shencha","root":"dsh"}
```

## 目录结构
```
hegui-shencha/
├── SKILL.md          # 主流程（Phase 0-4 + 硬规则）
├── prompts/          # screen.md 初筛 / review.md 复核
└── scripts/          # call_llm / numcheck / extract_evidence / merge_batches / check_review
```

## 安全边界
- 尽调材料原文**只在本地磁盘与本地 Ollama 之间流转**，不进任何云端模型上下文。
- 所有金额/比例/勾稽关系由 `scripts/numcheck.py` 确定性复算，模型不口算。
- 输出状态列一律「待人工终审」，不替人下结论。
