---
name: hegui-shencha
description: 合规审查（尽调初筛）。本地双模型工作流：Saluki-27B 全量初筛 + Python 确定性复算 + Qwen3.5-9B-Q8 对抗性复核，产出待人工终审的红旗登记册。处理本地尽调材料（财报、审计报告、合同、工商档案）的初筛、红旗比对、数字核对时使用。触发语：「跑合规审查」「跑 hegui-shencha」。输入目录固定为桌面「合规审查」文件夹。
---

# 合规审查（尽调双模型初筛工作流）

> 技能内部名 `hegui-shencha`（DSH 要求 frontmatter 的 name 必须是 ASCII kebab-case，
> 中文名会通不过校验而无法加载），对用户的中文名称是**合规审查**。
> 用户说「跑合规审查」时，就是指本技能。

敏感尽调材料的第一读者流水线：本地 Saluki-27B 全量初筛，Python 确定性复算所有数字，Qwen3.5-9B-Q8 对抗性复核，最终产出**待人工终审**的红旗登记册。

模型定位：Saluki 是"筛出可疑处"的分诊员，不是终审会计；9B 是"证据够不够"的裁判员，不是计算器。本流程消除低级错误，不消除判断错误——红旗类判断永远回到原文和人。

## 模型名（以本机实际为准）

运行前先 `ollama list` 核对：

| 角色 | 本机模型名 | 参数量化 | 家族 |
|---|---|---|---|
| 初筛 | `saluki27b:latest` | 26.9B / IQ2_XS | qwen35（本体 Qwen3.8-27B） |
| 复核 | `qwen3.5:9b-q8_0` | 9.0B / Q8_0 | qwen35（Qwen3.5-9B） |

**两个模型同属 qwen35 家族。** 这意味着复核纠的是随机错误，漏的是系统错误（共同的家族盲区）。这不是缺陷而是本流程的已知边界——所以登记册状态列锁死"待人工终审"，高风险科目（收入确认、关联交易、体外主体）必须人眼回原文抽查。

名字对不上时**改本文件或脚本里的默认值**，不要静默继续；任一模型缺失则停止并提示先完成导入，不得用云端模型替代本地环节。

## 硬规则（违反即失败）

1. **材料原文不进会话上下文**：所有材料原文只能经 shell 在磁盘文件与本地 Ollama 请求之间流转。你（指挥模型）只允许读写：脚本输出、结构化 JSON、你自己写的摘要。任何情况下不要用 read_file/cat 把材料原文读进对话。
2. **数字必须复算**：凡是金额、比例、同比、勾稽关系，一律由 `scripts/numcheck.py` 确定性计算，禁止任何模型口算后采信。**9B 复核员尤其不准碰算术**——实测它把"应收增速比收入增速高多少个百分点"算成 4.5（正确 55）。
3. **复核必须对抗**：9B 复核时禁止把初筛结论当前提，必须先独立看证据再下判断；结论必须附证据原文摘录，无摘录一律降级为"无法判断"。
4. **终审留人**：输出红旗登记册的状态列一律为"待人工终审"，禁止输出"已确认无异常"之类替你下结论的话。
5. **材料只进桌面「合规审查」文件夹**：绝不把尽调材料拖进 DSH 聊天框——拖入即等于原文进入云端模型上下文，整套"材料只在本地流转"的设计直接作废。

## 目录约定

| 用途 | 路径 |
|---|---|
| 输入（你手动放材料） | `~/Desktop/合规审查/` |
| 工作目录（中间产物） | `~/Desktop/合规审查/dd-work/` |
| 输出（唯一成品） | `~/Desktop/合规审查/dd-output/红旗登记册.md` |

**固定用绝对路径 `~/Desktop/合规审查/`，不用相对路径**——这样无论 dsh 开在哪个目录都能跑，不会因为启动位置不同而找不到材料。

**输入根目录下的 `Icon`\r 与 `.DS_Store` 不是尽调材料，任何阶段都不要读写、归档或删除。**
`Icon`\r 是该文件夹的自定义图标：0 字节数据文件 + `com.apple.ResourceFork` 扩展属性承载实际图像，
配合文件夹自身的 `com.apple.FinderInfo` 第 10 字节 `04` 标记生效。清场只动 `dd-work/`、`dd-output/`、
`dd-archive/` 三个目录，入库只对 pdf/docx/xlsx 调转换工具，两者都天然跳过 `Icon`\r。
若它被误删，Finder 会同时清除自定义图标标记，桌面图标会变回默认文件夹样式。

## Phase 0：材料入库

**⚠️ 每轮开始前必须先清场（这条是 2026-10-18 那次 12 小时全灭的直接原因之一）：**

上一次运行的中间产物（`dd-work/text/` 旧转换、`dd-work/batches/` 旧分批、`dd-output/` 旧登记册）
以及任何**其他任务**落在这里的文件，都会被 Phase 1 无差别拼进 prompt——污染材料、放大 token、
更糟的是把非尽调内容（曾混入诉讼策略文稿）当成审查对象。所以 Phase 0 第 0 步是清场：

```bash
# 用 bash 工具的 workdir 参数把 cwd 设到 $DD，再删相对路径
# （本机 CC Safety Net 的 rm.recursive-force-outside-cwd 规则会拦 rm -rf <绝对路径>）
DD=~/Desktop/合规审查
ts=$(date +%Y%m%d-%H%M)
mkdir -p "$DD/dd-archive/$ts"
# 先复制后删（复制失败可及时发现，不会先丢东西）；一边不存在时 cp 会报错，
# 所以先逐个源目录分别复制并单独判断，任一边有内容才算归档成功
for d in dd-work dd-output; do
  [ -d "$DD/$d" ] && cp -a "$DD/$d/." "$DD/dd-archive/$ts/"
done
n=$(find "$DD/dd-archive/$ts" -type f | wc -l)
[ "$n" -gt 0 ] || echo "归档为空，检查 $DD/dd-archive/$ts"
rm -rf ./dd-work ./dd-output
mkdir -p ./dd-work/text
```

**执行方式（本机 2026-10-19 实测）**：上面的 `rm -rf ./dd-work ./dd-output` 必须配合 bash 工具的
`workdir` 参数（`workdir=$HOME/Desktop/合规审查`）执行。裸写 `rm -rf "$DD/dd-work"`
会被 CC Safety Net 拦下（`rm.recursive-force-outside-cwd`），即使 `$DD` 就是目标目录也不放行。
归档建议用 `cp -a` 而非 `mv`——`mv` 跨卷/遇占用时会部分失败且不易察觉，`cp -a` + 数量校验更稳。

只删 `dd-work/`、`dd-output/`、`dd-archive/` 三个目录，**输入根目录的原始文件永远不动**
（登记册里的证据定位要能回到原文）。没有旧产物时跳过即可。

然后做入库：

1. 确认输入目录 `~/Desktop/合规审查/`。把里面的 pdf / docx / xlsx 转成文本，存入 `~/Desktop/合规审查/dd-work/text/`：
   - PDF：`pdftotext -layout 输入.pdf ~/Desktop/合规审查/dd-work/text/文件名.txt`
   - DOCX：`pandoc 输入.docx -t plain -o ~/Desktop/合规审查/dd-work/text/文件名.txt`（未装 pandoc 时用下方 Python 兜底）
   - XLSX：openpyxl 逐 sheet 输出（本机 DSH runtime Python 自带 openpyxl）。财报/Datapack 类多 sheet
     工作簿务必转全，**numcheck 的复算数据源全部来自这里**——漏转一份报表，Phase 2 就少一块复算。
2. 转换失败的文件记入工作日志，不要假装读过。
3. 原文件留在 `~/Desktop/合规审查/` 不要移动——登记册里的证据定位要能回到原文。
4. 入库后核对一次 `ls dd-work/text/` 数量与输入目录的文件数一致，且**没有上一轮残留**。

> 本机备注：`pdftotext` 已装；`pandoc` 未安装时 DOCX 兜底方案是
> `$HOME/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/bin/python3 -c "import docx,sys; print('\n'.join(p.text for p in docx.Document(sys.argv[1]).paragraphs))" 输入.docx > ~/Desktop/合规审查/dd-work/text/文件名.txt`
> （该 Python 自带 python-docx）。表格内容需另行抽取时，可改用 python-docx 输出表格。

## Phase 1：Saluki-27B 初筛（**必须分批，禁止单次全量**）

> 本机铁律（2026-10-18 用 12 小时零产出换来的）：M5/24GB 上禁止把整个 text/ 目录一次性喂给
> Saluki-27B。94K tokens 的 prompt 加巨型 JSON 输出在 6-7 tok/s 下必然超过任何合理超时。
> 正确做法：按文件体量分批，每批一个目录（符号链接指向 text/ 里的文件即可），跑完合并。

```bash
DD=~/Desktop/合规审查
# 1) 建分批目录：三大协议各一批、小文件合并批、大表格类按主题拆批
mkdir -p $DD/dd-work/batches/{b1,b2,b3,b4,b5}
ln -sf "$T/<大文件1>" $DD/dd-work/batches/b1/
# ... b2/b3 同理；b4 放小文件（决议/名册/财报）；b5 放其余

# 2) 逐批调用（参数为实测可用值：num_ctx 49152，输出上限 20480-24576，超时 2400-3600s）
python3 ~/.dsh/skills/hegui-shencha/scripts/call_llm.py saluki27b:latest \
    ~/.dsh/skills/hegui-shencha/prompts/screen.md \
    $DD/dd-work/batches/b1 $DD/dd-work/batches/screen_b1.json \
    --no-think --num-ctx 49152 --num-predict 20480 --timeout 2400
# ... b2..b5 同理（分批可串行跑，总耗时约 1.5-2 小时）

# 3) 合并（编号重排、缺键即报错）
python3 ~/.dsh/skills/hegui-shencha/scripts/merge_batches.py \
    $DD/dd-work/batches/screen_b*.json $DD/dd-work/screen_result.json
```

解析输出，检查结构完整：必须含 `flags`（可疑条目）和 `numbers`（数字抽取表）两个键。结构不完整时原样重试一次，仍失败则记录"初筛失败"并停止，不要自己编造初筛结果。

分批参考（按本机实测 token 量）：单份协议 80-93KB → 独批；决议/名册/财报小文件 → 合并批；
多 sheet 的 Datapack 类 → 按主题拆 2-3 批。宁可多拆一批，不要拼大。

> `--no-think`：Saluki 开思考模式时有"长篇推理半天不进正题"的毛病（文档实测），初筛是抽取型任务，关掉可大幅提速。

## Phase 2：数字确定性复算

```bash
DD=~/Desktop/合规审查
python3 ~/.dsh/skills/hegui-shencha/scripts/numcheck.py \
    $DD/dd-work/screen_result.json $DD/dd-work/numcheck_report.md
```

复算内容：同比变动、应收/收入增速背离、经营现金流与净利润背离、资产负债表勾稽。输出 PASS/FAIL 逐项报告。FAIL 项全部并入红旗候选，由 Phase 4 汇总。

模型漏抽基期时，脚本会从原文引用兜底补抽（报告中标记 `⚠补抽`），并在"抽取层面的数据问题"里留痕。**补抽值未经模型独立标注期间，优先级低于正式抽取值，需人工核对。**

## Phase 3：Qwen3.5-9B 对抗性复核

1. 对 Phase 1 的每条 flag，用 `scripts/extract_evidence.py` 从原文中抽取该 flag 证据坐标前后各 300 字的段落，拼成复核输入文件 `$DD/dd-work/review_input.md`（只含初筛结论 + 证据段落，此文件同样不进你的上下文）。

   ```bash
   DD=~/Desktop/合规审查
   python3 ~/.dsh/skills/hegui-shencha/scripts/extract_evidence.py \
       $DD/dd-work/screen_result.json $DD/dd-work/text/ $DD/dd-work/review_input.md \
       --window 300 --index $DD/dd-work/evidence_index.json
   ```

2. 调用复核（**9B 输出超约 1,200 tokens 连接会被切断，必须分批**）：

   ```bash
   DD=~/Desktop/合规审查
   python3 ~/.dsh/skills/hegui-shencha/scripts/call_llm.py qwen3.5:9b-q8_0 \
       ~/.dsh/skills/hegui-shencha/prompts/review.md \
       $DD/dd-work/review_input.md $DD/dd-work/review_result.json --no-think
   ```

   本机实测（2026-10-18）：27 条一次复核必断流。按每条一个 `## 疑点 N：` 标题切分
   （Python `re.split(r"(?m)^(?=## 疑点 \d+：)")`），**每批 3-4 条**（单条也行，40-110s/条），
   参数 `--num-ctx 32768 --num-predict 8192-16384 --timeout 1800-2400 --stall-timeout 300-600`。
   断流时不要原参数重试，继续往小拆。各批结果合并成一个 `reviews` 数组（按编号排序）即可。

3. review prompt 会强制 9B 先找反例再表态，且必须附证据原文摘录。解析输出后**必须跑校验**：

   ```bash
   DD=~/Desktop/合规审查
   python3 ~/.dsh/skills/hegui-shencha/scripts/check_review.py \
       $DD/dd-work/review_result.json \
       --index $DD/dd-work/evidence_index.json \
       --evidence-dir $DD/dd-work/text/ \
       --out $DD/dd-work/review_check.md
   ```

   降级规则（命中任一即把该条降级为"无法判断"）：
   - **R1 缺证据摘录**：摘录为空或过短——防 9B 凭空断言
   - **R2 摘录非原文**：摘录内容对不上证据段落里的连续子串——防 9B 自行造句
   - **R3 理由含算术痕迹**：出现"高出 X 个百分点""增长 X%"等式子——它算了就已越界，结论不可信

   关注度调整：
   - 复核意见为"反驳"或"无法判断"的 flag：关注度提升一级
   - 复核结论被降级的项：在复核列标注"已降级"，按其原疑点的复算结果定关注度

> `--no-think` 必加：该模型 Modelfile 默认 `presence_penalty=1.5`，与长 prompt 组合会触发 Ollama `token repeat limit reached` 500 报错。脚本已默认覆盖为 `presence_penalty=0`；实测关思考后从 679 秒降到约 99 秒（快 7 倍）且不再报错。

## Phase 4：汇总红旗登记册

生成 `~/Desktop/合规审查/dd-output/红旗登记册.md`，每条一行：

| 编号 | 科目 | 疑点 | 证据定位 | 复算结果 | 初筛 | 复核 | 关注度 | 状态 |
|---|---|---|---|---|---|---|---|---|

- 复算 FAIL 项：关注度=高
- 复算 WARN 项：关注度=中
- 复核反驳/无法判断项：关注度提升一级
- 复核结论被降级（缺摘录/含算术）的项：在复核列标注"已降级"
- 状态列一律写"待人工终审"

可用 `dd-work/gen_register.py`（本机 2026-10-18 新增）从 screen_result.json + 两份报告直接生成；
没有它时手写表格也行，但**不得把材料原文抄进登记册以外的任何位置**。

## 结束语规范

收尾只报三件事：登记册路径、复算 FAIL 数量、初筛或复核环节的技术性失败项。不得输出对材料整体质量的判断。

## 模型缺失处理

Saluki 或 9B 任一未在 Ollama 中时，停止并提示先完成模型导入，不得用云端模型替代本地环节。
