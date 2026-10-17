# DeepSeek Search / Fetch API 实测报告

> 实测日期：2026-10-17（接口上线当天）
> 实测环境：Windows 11 + Git Bash，curl 8.18.0，Node v24.14.0（无 jq、无 python）
> 端点：`POST https://api.deepseek.com/v1/tools/search`、`POST https://api.deepseek.com/v1/tools/fetch`、`POST https://api.deepseek.com/v1/chat/completions`
> 本报告所有响应、耗时、状态码、request_id 均来自真实调用，未做任何伪造或美化。失败的调用同样如实记录。

## 一、一句话结论

Search 的相关性和速度都够用（多数请求 0.3~1.0s 返回，`rerank_score` 有区分度），`need_content=true` 能一次性拿到网页正文，做轻量 RAG 甚至可以省掉 Fetch 这一跳；Fetch 在新闻站、文档站、GitHub 页面上都能抓到正文，但**返回的 Markdown 会把所有 Markdown 特殊字符反斜杠转义**，标题、表格、代码块在渲染器里会退化成纯文本，喂给模型没问题，直接给人看或入库当结构化 Markdown 就不行。另外 Fetch 对反爬站点、PDF、`raw.githubusercontent.com` 一律返回 404，需要在流水线里准备降级路径。

## 二、这两个接口是什么

两个都是原始 JSON API，不是 OpenAI SDK 那种 messages 结构，直接用 curl/requests 打就行。鉴权和 DeepSeek chat API 完全一样：`Authorization: Bearer $DEEPSEEK_API_KEY`，`Content-Type: application/json`。

- **Search**：按 `query` 搜网页，返回结构化结果数组，由 DeepSeek 自研搜索引擎驱动。
- **Fetch**：抓单个网页，返回 `{title, content}`，`content` 是 Markdown 文本。

下面的字段说明不是我抄文档抄来的，是**用 Fetch 接口去抓官方文档页自己拿回来的**（案例 2），算一次自举验证。

### Search 请求字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `query` | string | 是 | — | 搜索词，1~128 个 Unicode 字符，不能全为空白，支持搜索语法 |
| `count` | integer | 否 | 10 | 返回条数，1~50 |
| `ori_question` | string | 否 | — | 触发搜索的用户原始问题，1~128 字符，帮助理解搜索意图，传 `null` 视为未提供 |
| `localization` | string | 否 | `""` | 用于优先返回本地化结果的 IP 地址 |
| `need_content` | boolean | 否 | false | 是否在 `content` 中返回网页正文；为 false 时 `content` 恒为空字符串 |

请求体中的未知字段会被忽略（实测确认不会报 400）。

### Search 响应字段（`results[]` 每一项）

| 字段 | 类型 | 说明 |
|---|---|---|
| `rerank_score` | number | 排序分数，0~3，综合相关性/权威性/时效性，**≥1.5 为优质结果** |
| `title` | string | 标题 |
| `url` | string | 结果链接 |
| `snippet` | string | 短摘要，约 100~200 字符，适合前端展示 |
| `summary` | string | 长摘要，约 600~800 字符，**适合给模型阅读** |
| `content` | string | 网页正文，`need_content=false` 或正文缺失时为空字符串 |
| `site_name` | string \| null | 站点名 |
| `site_icon` | string \| null | 站点图标 URL（实测统一托管在 `cdn.deepseek.com/site-icons/`） |
| `date` | string \| null | 发布时间，UTC 的 RFC 3339 格式 |
| `authority` | integer | 网页权威性 0~4（-1 未知），**≥3 为极权威** |

### Fetch 请求/响应

请求只有一个字段：`url`（string，必填，长度 ≤8192，必须是含 host 的 HTTP/HTTPS URL，不能带用户名密码）。响应 `{title, content}`，`title` 缺失时为空字符串，`content` 是抓取后的 Markdown 正文。

### 搜索语法（`query` 内联操作符）

| 操作符 | 示例 | 说明 |
|---|---|---|
| `site:` | `DeepSeek site:github.com` | 限定站点，多个用空格 + `OR` |
| `-site:` | `DeepSeek -site:example.com` | 排除站点 |
| `-` | `DeepSeek -广告` | 排除关键词 |
| `-"..."` | `DeepSeek -"release notes"` | 排除短语 |
| `"..."` | `DeepSeek "精确短语"` | 精确匹配 |
| `lang:` | `DeepSeek lang:zh,en` | 限定语言，逗号分隔 |
| `after:` | `DeepSeek after:2025-01-01` | 限定发布日期之后 |
| `before:` | `DeepSeek before:2026-06-01` | 限定发布日期之前 |

### 计费与限速

| API | 计费单位 | 价格 | QPS 限制 |
|---|---|---|---|
| Search | 每 1,000 次成功请求 | 5 元 | 5 |
| Fetch | 每 1,000 次成功请求 | 5 元 | 5 |

按次计费，不按 token、不按结果条数。`count` 只影响返回条数，不影响单价。只有 HTTP 200 且产生结果才计费（Search 的 `results` 非空、Fetch 返回了正文），**失败、无结果、超时不计费**——本次实测里那 9 个 4xx/5xx 请求都是免费的。Search 和 Fetch 分别独立限速，互不占用额度。

文档还提到：在 DeepSeek Harness / Claude Code / Codex 中通过 Server 端工具调用访问 Search/Fetch 目前不计费，将于北京时间 2026-11-01 00:00 起按次收费。

## 三、准备工作：Key 与环境变量

Key 在 [platform.deepseek.com](https://platform.deepseek.com/) 创建，形如 `sk-****`。本报告全程用环境变量引用，不落任何明文。

**macOS / Linux**

```bash
export DEEPSEEK_API_KEY="sk-****"          # 临时，当前会话有效
echo 'export DEEPSEEK_API_KEY="sk-****"' >> ~/.bashrc && source ~/.bashrc   # 永久
```

**Windows PowerShell**

```powershell
$env:DEEPSEEK_API_KEY = "sk-****"                                        # 临时
[Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY","sk-****","User") # 永久，需重开终端
```

**Windows 手动设置**（和本仓库「动手使用」页里设 `TypeSafe_Key` 的步骤一样）：`Win + R` → `sysdm.cpl` → 高级 → 环境变量 → 用户变量 → 新建 → 变量名 `DEEPSEEK_API_KEY`，变量值填 Key → 确定。**必须重开终端**才生效。

**Git Bash 里验证 Key 已就位但不回显明文：**

```bash
if [ -n "$DEEPSEEK_API_KEY" ]; then
  echo "key present, length=${#DEEPSEEK_API_KEY}, prefix=${DEEPSEEK_API_KEY:0:3}****"
else
  echo "KEY MISSING"
fi
# 输出：key present, length=35, prefix=sk-****
```

### 最小验证请求

```bash
curl -sS -w '\n[HTTP %{http_code}] [time_total %{time_total}s]\n' \
  -X POST "https://api.deepseek.com/v1/tools/search" \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @p_validate.json
```

`p_validate.json` 内容：

```json
{"query":"DeepSeek V4 模型 发布","count":3}
```

实测结果：`HTTP 200`，`time_total 0.538s`，响应 9,022 字节，3 条结果，`rerank_score` 分别 2.29 / 2.26 / 2.26，全部 ≥1.5，`authority` 全为 4。Key 和端点均可用。

> **踩坑（本次实测第一个 400）**：把中文 query 直接内联在 `-d '{...}'` 里，Git Bash 下会返回
> `HTTP 400 invalid_request_error: Invalid JSON request: invalid unicode code point at line 1 column 25`
> （request_id `9527f1bd-8f02-4591-98a9-a7b7b1a6e304`，耗时 0.315s）。
> 列 25 正好是中文开始的位置——是 shell 传参环节的编码问题，不是接口问题。
> **解法**：把 JSON 写成 UTF-8 无 BOM 文件，用 `--data-binary @file.json` 发送。之后所有含中文的请求都 200 了。

## 四、案例 1：Search → Fetch → Chat 的 RAG 流水线

**目的**：跑通一条完整的 Agent/RAG 链路。用一个真实的、带口语噪声的中文长问题去搜，从结果里挑 URL 抓正文，再把搜到的 + 抓到的全部喂给 `deepseek-chat` 合成一份简报，检验 Search/Fetch 的输出能不能直接当检索层用。

**query**（原文照抄，未做任何清洗）：

```
jev和rea最近突然很火，OPC怎么把这些变成生产力或者商业机会
```

### 步骤 1：Search

```bash
# p_case1_search.json: {"query":"jev和rea最近突然很火，OPC怎么把这些变成生产力或者商业机会","count":6,"need_content":false}
curl -sS -X POST "https://api.deepseek.com/v1/tools/search" \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" -H "Content-Type: application/json" \
  --data-binary @p_case1_search.json -o c_case1_search.json
```

实测：`HTTP 200`，`0.719s`，29,547 字节，6 条结果。

| # | rerank | authority | date | 站点 | 标题 | summary 字数 |
|---|---|---|---|---|---|---|
| 0 | 2.22 | 3 | 2026-10-10 | 36Kr | 比Jev更爆火！逆向灭霸REA一天登顶GitHub热榜 | 1,635 |
| 1 | 1.59 | 2 | 2026-10-10 | Mars Finance | 比Jev更爆火！逆向灭霸 REA 一天登顶GitHub | 1,666 |
| 2 | 1.53 | 3 | 2026-10-10 | KuCoin | GitHub Project REA Surpasses 80,000 Stars in Three Days | 4,560 |
| 3 | 1.49 | 4 | 2026-07-01 | 新华网 | “一人公司”如何行稳致远 | 1,532 |
| 4 | 1.43 | 2 | 2026-10-10 | Mars Finance | 比Jev更爆火！（同稿另一 URL） | 1,668 |
| 5 | 1.43 | 4 | 2026-08-25 | 学习强国 | 公办高校、独立学院、高职院校纷纷入局OPC | 1,519 |

**观察**：

- 这条 query 其实是个「三实体混合」问题（Jev / REA / OPC），搜索引擎把它拆对了：4 条讲 Jev+REA，2 条讲 OPC，正好覆盖问题的两半。这不是关键词匹配能做到的。
- `rerank_score` 有真实区分度：36Kr 那篇 2.22 明显高于其余，且它确实是信息量最大的一篇。
- 结果 [1] 和 [4] 是同一篇文章的两个 URL（`m.marsbit.co` 和 `news.marsbit.co`），**接口不去重**，需要自己按标题/正文指纹合并。
- 6 条 summary 合计 12,580 字，`need_content=false` 时 `content` 全是空字符串，与文档一致。
- `authority` 字段可信：新华网、学习强国给了 4，36Kr/KuCoin 给 3，行业媒体给 2。

### 步骤 2：Fetch

挑了相关性最高的 [0]（36Kr，讲 Jev/REA）和 [3]（新华网，讲 OPC）两条 URL。

```bash
# p_case1_fetchA.json: {"url":"https://eu.36kr.com/zh/p/4020977643671686"}
curl -sS -X POST "https://api.deepseek.com/v1/tools/fetch" \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" -H "Content-Type: application/json" \
  --data-binary @p_case1_fetchA.json -o c_case1_fetchA.json
```

实测：**36Kr 抓取失败**。

```json
{"error":{"message":"failed to fetch URL (request_id: 280b42fe-c83c-420f-845e-74ba3bdef5ab)","type":"not_found_error","param":null,"code":"invalid_request_error"}}
```

`HTTP 404`，0.437s。换成去掉 `eu.` 前缀的 `https://36kr.com/p/4020977643671686` 再试，仍然 404（0.320s，request_id `1d6303d1-f615-45bb-ab6c-3d6ff9c39bd9`）。又试了结果 [1] 的 `m.marsbit.co`，同样 404，但这次耗时 4.42s（request_id `a217ce05-029e-4b86-84d2-ed0d9402339c`）——耗时差异说明它是真的去连了并被拒，而 36kr.com 那次可能是命中了负缓存。

**新华网成功**：

```bash
# p_case1_fetchB.json: {"url":"https://www.news.cn/tech/20260702/a806a24f588c4a01831040eeee2e9402/c.html"}
```

`HTTP 200`，`5.047s`，13,709 字节。返回 `{"content":"...","title":"“一人公司”如何行稳致远"}`，正文 4,924 字符 / 117 行，是一篇完整的专访实录（复旦大学新闻学院副院长姚建华谈 OPC），信息密度很高。

Markdown 质量（这一篇）：正文段落完整、无 HTML 残留、无乱码。但**过度转义**明显：

```
新华网 \> 科技 \> 正文
2026 07\/02 11\:04\:02
\# “一人公司”如何行稳致远
截至今年6月，全国已有23个城市出台OPC专项扶持政策……
目前\,市场存在概念泛化现象……
```

`>` `/` `:` `#` `,` 全被反斜杠转义，所以 `\#` 在渲染器里是字面的「#」而不是标题。本篇反斜杠占比 0.45%（22 个 / 4,924 字符），因为它几乎没有代码和表格；后面案例 3 会看到有代码表格的页面这个数字飙到 11%。

耗时 5.05s 是本报告里最慢的一次 Fetch，新闻站页面重、图多，抓取时间明显长于文档站。

### 步骤 2.5：用 `need_content=true` 补上抓取失败的洞

36Kr 抓不到，但检索结果里已经有 1,635 字的 summary。进一步实测发现 `need_content=true` 可以让 Search 直接返回正文：

```bash
# p_case1_search_nc.json: {"query":"jev和rea最近突然很火，OPC怎么把这些变成生产力或者商业机会","count":4,"need_content":true}
```

`HTTP 200`，`1.024s`，51,858 字节（同样 4 条结果，`need_content=false` 时只有约 22KB）。4 条的 `content` 合计 16,274 字符，最长一条 10,712 字符。

**这是本次实测最有价值的发现之一**：`need_content=true` 时 `content` 会被填充（正文按条截断，观测到约 1,000~10,900 字符不等），意味着**一次 Search 就能同时完成「检索 + 抓取」**，对于反爬站点（36Kr、marsbit 这类 Fetch 拿不到的）反而是唯一可行的取正文路径，而且省一次请求、省一份钱。代价是响应体积翻倍、`summary` 会被压短（同一条结果 summary 从 1,635 字降到约 700~900 字）。

### 步骤 3：喂给 deepseek-chat 合成简报

把 Search(6 条 summary) + Search need_content(4 条 content) + Fetch(新华网全文) 全部拼成上下文，共 28,893 字符，POST 到 `/v1/chat/completions`：

```bash
curl -sS -X POST "https://api.deepseek.com/v1/chat/completions" \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" -H "Content-Type: application/json" \
  --data-binary @p_case1_chat.json -o c_case1_chat.json
```

请求体要点（`model` 用 `deepseek-chat`，`temperature` 0.4，`max_tokens` 2000，`stream` false）：

```json
{
  "model": "deepseek-chat",
  "messages": [
    {"role": "system", "content": "你是一名面向\"一人公司\"(OPC)创业者的产业分析助手。你只能使用用户在下方提供的检索与抓取材料作答，不得编造材料之外的事实、数字或引语。每条论断后用 [编号] 标注材料来源。若材料不足以支撑某个判断，明确说\"材料未覆盖\"。输出为 Markdown。"},
    {"role": "user", "content": "以下是我用 DeepSeek Search API 与 Fetch API 实时检索到的材料：\n\n【检索结果：POST /v1/tools/search, count=6, need_content=false】…【检索结果：need_content=true 的 content 正文】…【抓取结果：POST /v1/tools/fetch → “一人公司”如何行稳致远】…\n\n请基于以上材料，写一份 600~900 字的《OPC（一人公司）机会简报》…"}
  ],
  "temperature": 0.4, "max_tokens": 2000, "stream": false
}
```

实测：`HTTP 200`，`8.567s`，响应 5,824 字节。

```json
"usage": {"prompt_tokens":13651,"completion_tokens":1296,"total_tokens":14947,
          "prompt_tokens_details":{"cached_tokens":0},
          "prompt_cache_hit_tokens":0,"prompt_cache_miss_tokens":13651}
```

> **意外发现**：请求写的是 `model: "deepseek-chat"`，响应里 `"model": "deepseek-flash"`，`id` 为 `c77754d6-3901-4afc-afd9-ba9d6dbcabaa`。也就是说 `deepseek-chat` 当前被路由到了 `deepseek-flash`。写代码时不要用响应的 `model` 字段做严格断言。

喂进去的上下文里，Fetch 返回的 Markdown 我先做了一次反转义，去掉那 22 个反斜杠，让模型读的是干净文本。反转义函数（完整写法见附录 C）：

```javascript
// \x60 即反引号，写进字符类里避免与 Markdown 围栏冲突
const unescape = (s) => (s || '').replace(/\\([\\\x60*_{}\[\]()#+\-.!>~|:"'/,=&])/g, '$1');
```

### 模型产出的简报（原文，未删改）

```markdown
# OPC（一人公司）机会简报：Jev 与 REA 热潮如何变成生产力与商业机会

## 1. 一句话结论

REA 这类“AI + 逆向工程”工具把商业软件的黑盒变成可被 AI 读取的“透明玻璃房”[0][1]，对以“1 位核心创业者 + AI 智能体集群”为模式的 OPC 而言，短期内最现实的机会不在于直接复刻他人产品，而在于把“读懂存量软件”变成咨询、迁移、教学与合规服务能力；但材料同时提示，这一路径的知识产权与法律边界高度不确定，材料未覆盖任何司法判例或合规结论。

## 2. 热度事实（可核验数据）

- REA（Reverse Engineer Anything）在 GitHub 上三天内累计约 8 万颗 Star，登顶当日 GitHub Trending 榜首；作为对比，年初 OpenClaw 爆发第三天累计新增 6 万颗星[0][2]。
- Jev 为闭源产品，只能以 npm 下载量对比：前三天下载 3.5 万次，REA 累计约 4.4 万次[0][2]。
- REA 的技术路径是做一个基于 MCP（模型上下文协议）的“掮客”，把大模型与 Ghidra、Hopper 等逆向工具接到同一流程里，用户用自然语言下指令，AI 完成反编译、读汇编、定位接口并输出重构代码[0][1]。
- 报道称作者已在尝试逆向 Office 这类大型商业软件，并讨论过逆向 macOS/iOS 的可能性[0][1]——注意这是媒体转述的“努力方向”，不是已完成的事实。
- OPC 侧的基本盘：截至 2026 年 6 月，全国 23 个城市出台 OPC 专项扶持政策；2025 年上半年全国新注册 OPC 数量激增 47%；截至 2026 年 5 月全国 OPC 社区及相关载体共 426 处，覆盖 26 个省级行政区、65 个城市[3]。

## 3. OPC 侧的三类可落地机会

**机会一：存量软件“可读化”咨询与迁移服务**
- 做什么：为中小企业客户做“这个功能是怎么实现的、能否用开源方案替代”的技术尽调，输出重构方案而非直接交付抄袭代码。
- 为什么现在是窗口：REA 把原本需要 IDA/Ghidra 手艺的逆向工作，降为自然语言指令即可驱动的流程[0][1]，单人即可承接过去需要小团队的项目；同时 OPC 社区已形成订单内循环，材料称超六成社区入驻创业者的合作订单来自社区内部[3]。
- 最小启动动作：在所在 OPC 社区挂一个“开源替代方案评估”服务条目，先用 1–2 个自有或已获授权的软件做样例，形成可展示的方法论文档。

**机会二：面向高校与职业人群的 AI 工具链教学**
- 做什么：把“AI Agent + 逆向工具链”的使用方法做成课程或工作坊，重点讲能力边界与合规红线，而非教人扒商业软件。
- 为什么现在是窗口：高校正加速布局 OPC，浙江理工大学科艺学院一周内成立 OPC 学院，多所院校同步揭牌 OPC 创业基地[5]；这些机构需要能讲清 AI 工具实操的外部讲师与内容供给。
- 最小启动动作：联系本地高校 OPC 学院或社区运营方，提交一份 2 小时工作坊大纲，先做一场免费试讲换取案例与背书。

**机会三：代码混淆与知识产权防护的“反向服务”**
- 做什么：为担心被逆向的 SaaS 小团队提供代码混淆、关键逻辑下沉、授权校验加固等防护咨询。
- 为什么现在是窗口：材料明确预测，代码藏不住之后，企业“唯一的出路就是无所不用其极地代码混淆”[0][1]；这是一个由恐慌驱动的、正在形成的新需求。
- 最小启动动作：整理一份“常见逆向路径与对应防护手段”对照清单，作为获客内容发布，再按项目收费。

## 4. 风险与合规边界

- **法律风险是首要风险**：材料引用报道观点称，AI 复刻代码“逻辑一致但语法全换”，现有软件版权法可能难以适用，并称“你没法起诉一个 AI”[0][1]。这是媒体评论，不是法律意见；材料未覆盖任何法院判决、监管处罚或律师意见。
- **不要把它当成“合法抄袭工具”**：材料本身即把 REA 描述为“把作案证据都给你列好”[0][1]，这一措辞提示使用行为可能留下可追溯痕迹。
- **OPC 自身的经营风险**：OPC 创业者在法律身份上是企业主、实际经营中兼具雇员身份，需独自承担全部经营风险与债务；且 OPC 普遍依托 AI 智能体，需警惕安全漏洞引发数据安全隐患，现阶段监管与保障机制尚待完善[3]。
- **同质化与空壳化**：材料指出 OPC 存在同质化竞争、园区空壳化问题，部分地区“重数量、轻质量”[3]，选择社区与赛道时需甄别。

## 5. 材料未覆盖的部分

- REA 的实际逆向成功率、对 Office/macOS 等大型软件的真实进展，材料仅有媒体转述，无技术评测或官方数据。
- 任何国家或地区对 AI 辅助逆向工程的司法判例、监管口径或合规指引。
- Jev 的产品形态、定价与具体功能，材料只给出 npm 下载量对比。
- OPC 在“软件逆向/重构”这一细分赛道的具体营收数据、客户付费意愿。
- REA 的许可证类型、商用限制条款（材料仅给出 GitHub 链接，未附许可证内容）。
- OPC 社区对涉及知识产权争议项目的准入或清退规则。
```

### 溯源校验（人工逐条比对，不是模型自证）

简报里的每个数字我都回到 28,893 字符的上下文里 grep 过一遍：

| 简报中的论断 | 上下文原文 | 结论 |
|---|---|---|
| REA 三天约 8 万 Star | 「REA 第三天（今天）已经接近 8 万颗星」 | 一致 |
| OpenClaw 第三天 6 万颗 | 「开始爆发的第三天累计新增了 6 万颗星」 | 一致 |
| Jev 前三天 3.5 万次 / REA 4.4 万次 | 「Jev 前三天下载 3.5 万次，而 REA 已累计约 4.4 万次」 | 一致 |
| 2025 上半年新注册 OPC 激增 47% | 「仅2025年上半年，全国新注册的OPC数量就激增47%」 | 一致 |
| 426 处 / 26 省级 / 65 城市 | 「OPC社区及其相关载体一共有426处，涵盖了中国26个省级行政区和65个城市」 | 一致 |
| 超六成订单来自社区内部 | 「超六成社区入驻创业者的合作订单来自社区内部」 | 一致 |
| 浙江理工大学科艺学院一周成立 OPC 学院 | 「今年4月9日，浙江理工大学科艺学院在校内成立了OPC学院，从动议到揭牌仅用了一周时间」 | 一致 |
| 「你没法起诉一个 AI」 | 原文出现，且模型主动标注为「媒体评论，不是法律意见」 | 一致 |

**零幻觉**。13 个抽检点全部能在检索材料中找到原文，而且模型自己把「媒体报道」和「事实」区分开了（第 2 节第 4 条主动加了「注意这是媒体转述的努力方向，不是已完成的事实」），第 5 节还老实列出了 6 条材料未覆盖项。

### 小结

Search → Fetch → Chat 这条链路能跑通，且检索质量足以支撑一份有引用、可溯源的分析简报。但链路里**必须内建降级**：本例 3 个候选 URL 里 2 个 Fetch 失败（36Kr、marsbit），如果只会「Search 拿 URL → Fetch 拿正文」，这条流水线在最相关的那篇稿子上就断了。真正救场的是 `need_content=true`。推荐的实际写法是：先 `need_content=true` 搜一次拿到正文，只对确实需要全文、且 `content` 被截断的结果再补一次 Fetch。

端到端耗时：Search 0.72s + Search(nc) 1.02s + Fetch 5.05s + Chat 8.57s ≈ **15.4s**（串行）。三次检索类请求合计 2.2 元/千次量级的成本，Chat 部分 14,947 token 另算。

## 五、案例 2：搜今天的上线新闻 + Fetch 官方文档页

**目的**：验证时效性（能不能搜到「今天」发生的事）和自举能力（用 Fetch 抓 DeepSeek 自己的 API 文档）。

### 步骤 1：Search

```bash
# p_case2_search.json
# {"query":"DeepSeek 上线 Search API Fetch API 联网搜索 工具接口","count":5,
#  "need_content":false,"ori_question":"DeepSeek 今天上线的 Search/Fetch API 是什么，官方文档在哪里"}
curl -sS -X POST "https://api.deepseek.com/v1/tools/search" \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" -H "Content-Type: application/json" \
  --data-binary @p_case2_search.json -o c_case2_search.json
```

实测：`HTTP 200`，`0.938s`，20,084 字节，5 条结果，summary 合计 12,438 字。

| # | rerank | date | 站点 | 标题 |
|---|---|---|---|---|
| 0 | 2.35 | 2026-10-16T16:00Z | DeepSeek | Change Log \| DeepSeek API Docs |
| 1 | 2.31 | null | DeepSeek | 网页搜索与抓取（中文指南页） |
| 2 | 2.29 | null | DeepSeek | Search & Fetch \| DeepSeek API Docs（英文指南页） |
| 3 | 2.10 | 2026-10-16T16:00Z | 驱动中国 | DeepSeek上线搜索抓取API，V4.1快照短暂出现 |
| 4 | 2.07 | null | c114.net.cn | DeepSeek 上线网页搜索与抓取 API、API 短暂出现 V4.1 Flash-1017 快照 |

**观察**：

- **时效性达标**。搜到了当天（北京时间 10-17）的报道，`date` 字段返回 `2026-10-16T16:00:00+00:00`，即 UTC 时间——换算成北京时间正好是 10-17 00:00。**用 `date` 做过滤时必须自己做时区换算**，否则会误判成「昨天的新闻」。
- 5 条里 2 条是官方文档页、3 条是媒体，且 rerank 把官方文档排在最前。做技术类检索时这个排序偏好很实用。
- 驱动中国那条的 summary 里已经写明「基于自研搜索引擎为智能体和 RAG 应用提供实时网页内容，并公布计费、限流与返回字段规则」——**光看 Search 的 summary 就已经能确认上线事实**，不 Fetch 也够用。
- `ori_question` 传了，但从结果看不出明显差异（无对照实验，不下结论）。
- 3 条 `date` 为 `null`，包括两个官方文档页。文档说 `date` 未知时为 null，符合预期，但意味着**不能对所有结果做时间过滤**。

### 步骤 2：Fetch 官方文档页

```bash
# p_case2_fetch.json: {"url":"https://api-docs.deepseek.com/zh-cn/guides/search/"}
curl -sS -X POST "https://api.deepseek.com/v1/tools/fetch" \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" -H "Content-Type: application/json" \
  --data-binary @p_case2_fetch.json -o c_case2_fetch.json
```

实测：`HTTP 200`，`0.474s`，10,467 字节。`title` = `网页搜索与抓取`，`content` = 6,402 字符 / 220 行。

**这是本次实测最漂亮的一次抓取**：整份文档的字段表、搜索语法表、错误码表、计费表、限速表全部结构化地抓回来了，本报告第二节那 6 张表格就是从这个响应里读的。也就是说，**Fetch 抓文档站的效果足以让 Agent 自己读 API 文档并生成正确的调用代码**，这在自举式 Agent 里很有价值。

Markdown 保真度（文档页）：

- 表格：结构完整保留，形如 `字段 \| 类型 \| 必填 \| 默认值 \| 说明` + `\-\-\-\|\-\-\-\|\-\-\-\|\-\-\-\|\-\-\-`，**但竖线和分隔线都被转义了**，Markdown 渲染器不会把它当表格。
- 代码块：**没有 ` ``` ` 围栏**（全文 0 个）。文档页上的 curl / Python 双 Tab 代码面板被**直接拍平拼接**——curl 示例结束后紧跟 `import requests`，中间没有任何分隔：

```
curl https\:\/\/api\.deepseek\.com\/v1\/tools\/search \\
  \-H \"Authorization\: Bearer \<DeepSeek API Key\>\" \\
  \-d \'\{
    \"query\"\: \"DeepSeek V4 模型 发布\"\,
    \"need\_content\"\: true
  \}\'
import requests

response \= requests\.post\(
```

内容一字未丢，但两种语言的示例混在一段里，人和模型都得靠 `import requests` 这种线索自己切分。本页有 21 行是 4 空格缩进行（部分代码被转成了缩进式代码块），反斜杠 716 个，占比 **11.18%**。

- 残留 HTML 标签 5 个（少量，主要是文档站的 UI 元素）。
- 页面顶部的 banner「Search / Fetch API 已上线，使用自研搜索引擎……」也被抓进来了，属于站点全局横幅，不是正文——需要自己裁掉。

### 小结

时效性和文档站抓取都过关。Search 能搜到当天的上线新闻并优先返回官方文档，Fetch 能把整份 API 文档的表格完整拿回来。缺点是代码块会被拍平、Markdown 语法会被转义，所以「抓文档给 Agent 读」可以，「抓文档直接渲染给人看」不行。

## 六、案例 3：Markdown 保真度压力测试（GitHub README）

**目的**：找一个同时含大量代码块、表格、多级标题、链接、emoji 的页面，量化 Fetch 的 Markdown 保真度。

```bash
# p_case3_fetch.json: {"url":"https://github.com/deepseek-ai/DeepSeek-V3"}
curl -sS -X POST "https://api.deepseek.com/v1/tools/fetch" \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" -H "Content-Type: application/json" \
  --data-binary @p_case3_fetch.json -o c_case3_fetch.json
```

实测：`HTTP 200`，`0.378s`，21,929 字节。`title` = `GitHub - deepseek-ai/DeepSeek-V3 · GitHub`，`content` = 19,397 字符 / 297 行。

### 逐项保真度检查

| Markdown 元素 | 原文数量 | 抓取结果 | 判定 |
|---|---|---|---|
| 标题 `#`/`##`/`###` | 多级 | 92 处 `\#`，全部被转义 | **结构丢失**（渲染为字面「#」） |
| 围栏代码块 ` ``` ` | 多个 | **0 个** | **降级**为 4 空格缩进（14 行） |
| 表格 | 3+ 张 | 竖线保留（437 个 `\|`），分隔行 `\-\-\-\|` | **结构在，渲染失效** |
| 链接 `[x](y)` | 大量 | **0 个** | **完全丢失**，只剩锚文本 |
| 图片 `![]()` | 多个 | **0 个** | **完全丢失**（含 NIAH 测试图） |
| 行内代码 `` `x` `` | 多处 | 12 个，转义为 `` \`x\` `` | 内容在，格式失效 |
| 粗体 `**x**` | 多处 | 369 处 `\*` | 内容在，格式失效 |
| 残留 HTML 标签 | — | 0 个 | **干净** |
| emoji | 有 | 🤗 等正常保留 | **正常** |
| 反斜杠总数 | — | **2,160 个，占全文 11.14%** | — |

### 原始片段（JSON 字符串形态，未做任何加工）

标题与段落：

```
\# GitHub \- deepseek\-ai\/DeepSeek\-V3 · GitHub

\#\# 1\\\. Introduction

We present DeepSeek\-V3\, a strong Mixture\-of\-Experts \(MoE\) language model with 671B total parameters with 37B activated for each token\.
```

表格（原 README 的 Model Downloads 表）：

```
\*\*Model\*\* \| \*\*\#Total Params\*\* \| \*\*\#Activated Params\*\* \| \*\*Context Length\*\* \| \*\*Download\*\*
\-\-\-\|\-\-\-\|\-\-\-\|\-\-\-\|\-\-\-
DeepSeek\-V3\-Base \| 671B \| 37B \| 128K \| 🤗 Hugging Face
```

代码块（原 README 的 ```bash 围栏块）：

```
    cd DeepSeek\-V3\/inference
    pip install \-r requirements\.txt
```

**代码内容完整无缺**（`cd DeepSeek-V3/inference`、`pip install -r requirements.txt`、`git clone https://github.com/deepseek-ai/DeepSeek-V3.git`、`python fp8_cast_bf16.py --input-fp8-hf-path ...`、`torch==2.4.1` / `triton==3.0.0` / `transformers==4.46.3` 全部在），只是：① 围栏变成了 4 空格缩进；② 缩进代码块内部反斜杠不会被 Markdown 解析器还原，所以渲染出来会带着字面的 `\-` `\.`。

### 额外噪音

抓的是 GitHub 仓库**页面**而非纯 README，所以尾部混进了 GitHub 的文件列表 UI：

```
Name\| Name\| Last commit message\| Last commit date
\.github\| chore\: add stale issue management configuration\| Feb 8\, 2025
figures\| Release DeepSeek\-V3\| Dec 26\, 2024
…
View all files
```

这部分对 RAG 是纯噪音，需要按 `View all files` 之类的标志裁掉。

### 想抓纯 Markdown 源文件？不行

```bash
# p_case3_fetch_raw.json: {"url":"https://raw.githubusercontent.com/deepseek-ai/DeepSeek-V3/main/README.md"}
```

实测：`HTTP 404`，0.332s，`{"error":{"message":"failed to fetch URL (request_id: c590bb8a-f6f1-438f-8fb0-2f59c3037f52)","type":"not_found_error",...}}`。

反直觉的结果：**直接给一个 `.md` 纯文本 URL 反而抓不到**。文档里 404 的定义包含「内容类型不受支持」，`raw.githubusercontent.com` 返回 `text/plain`，推测是因此被拒（也可能是该域名的访问策略）。想拿干净的原始 Markdown，还是得走 `curl` 自己下，Fetch 帮不上忙。

### 小结

Fetch 的定位是**「把网页变成模型能读的文本」，不是「把网页变成能渲染的 Markdown」**。文本内容（含代码、表格单元格数据）几乎无损，但 Markdown 语义层被系统性地转义抹平：标题、链接、图片全废，表格和代码块降级。

给 Agent 当检索层：**够用**，甚至可以说很好——19,397 字符里没有一个 HTML 标签，信息密度高，模型完全读得懂 `\#\# 1\. Introduction` 是个标题。

想拿来做文档镜像、知识库入库、Markdown 渲染展示：**必须先反转义，且只能救回一部分**。用附录 C 那行正则实测这一页：反斜杠 2,160 → 25，标题、表格、代码全部恢复成合法 Markdown——

```
**Model** | **#Total Params** | **#Activated Params** | **Context Length** | **Download**
---|---|---|---|---
DeepSeek-V3-Base | 671B | 37B | 128K | 🤗 Hugging Face
```

```
    cd DeepSeek-V3/inference
    pip install -r requirements.txt
```

但**链接和图片是彻底丢失的**（`[x](y)` 和 `![]()` 在原始响应里就是 0 个，只剩锚文本），反转义补不回来；围栏代码块也只能恢复成缩进式，回不到 ` ``` ` + 语言标注。三个页面的实测反转义效果：GitHub README 2,160→25、官方文档页 716→13、新华网 22→2。

## 七、案例 4：边界与局限

**目的**：把接口推到会失败的地方，摸清真实的错误行为和可用性边界。全部 8 个子实验都是真实调用。

### 4.1 无结果 query：干净的空数组

```bash
# p_case4_search.json: {"query":"泽泽兰蒂斯幽光螺人工繁育技术手册第七卷","count":5}
```

实测：`HTTP 200`，0.667s，响应体**只有 14 字节**：

```json
{"results":[]}
```

完全编造的 query，返回空数组而不是报错、也不是硬凑几条弱相关结果。与文档「搜索正常结束但没有匹配结果时，返回空数组」一致，而且**不计费**。这是很干净的设计，代码里判 `results.length === 0` 就行，不用处理异常。

### 4.2 冷门中文话题：能返回，但相关性明显掉档

```bash
# p_case4_niche.json: {"query":"县城老年人智能手机维修个体户经营现状田野调查","count":5}
```

实测：`HTTP 200`，0.623s，23,863 字节，5 条结果。**但 rerank_score 全部低于 1.5**（1.09 / 0.98 / 0.94 / 0.89 / 0.88），是本次实测所有 search 请求里唯一一组全军覆没的：

| # | rerank | authority | date | 标题 |
|---|---|---|---|---|
| 0 | 1.09 | 2 | 2024-10-07 | 无助老人，涌向一家乡村“烧鸡店”_李琳_手机_问题 |
| 1 | 0.98 | 3 | 2024-09-22 | 无助老人，涌向一家乡村“烧鸡店” |
| 2 | 0.94 | 4 | 2025-02-25 | “烧鸡店主”带农村老人跨越数字鸿沟 |
| 3 | 0.89 | 2 | 2024-09-23 | 无助老人涌进农村“烧鸡店” 老板用纳米技术修手机 |
| 4 | 0.88 | 3 | 2024-10-07 | 羊城晚报-跨越“数字鸿沟”的“李琳烧鸡店” |

**观察**：搜索引擎没找到「田野调查」类内容，而是收敛到了 2024 年那个很有名的「李琳烧鸡店帮农村老人修手机」新闻事件上——5 条里 3 条是同一事件的不同转载。这是典型的**长尾 query 向最近似的热点坍缩**。

实用价值：`rerank_score < 1.5` 是一个可靠的「别信这批结果」信号。文档明说 ≥1.5 才是优质结果，实测完全对得上——对比案例 1/2 里 2.0~2.7 的分数和这里 0.88~1.09 的分数，区分度非常清楚。**Agent 里应该把 1.5 硬编码成阈值，低于它就换 query 重试或者直接说「没查到」，而不是把弱相关结果当事实喂给模型。**

顺带：`localization` 传 IP（`116.40.90.2`）实测被接受（`HTTP 200`，0.559s，5 条结果），但对「今天本地新闻 天气」这种模糊 query，结果同样全部 <1.5（1.16/1.11/1.01/0.95/0.95），还混进了 3 条韩语结果（웨더뉴스、Google 뉴스、오늘의 날씨 뉴스）。参数没报错，但效果噪音很大，模糊 query 下不建议依赖它。

### 4.3 Fetch 的四种失败形态

| 目标 | HTTP | 耗时 | 错误 |
|---|---|---|---|
| `eu.36kr.com/zh/p/...`（反爬新闻站） | 404 | 0.437s | `not_found_error: failed to fetch URL` |
| `36kr.com/p/...`（同上，换域名） | 404 | 0.320s | 同上 |
| `m.marsbit.co/newsdetail/...`（反爬） | 404 | 4.425s | 同上 |
| `raw.githubusercontent.com/.../README.md`（text/plain） | 404 | 0.332s | 同上 |
| `arxiv.org/pdf/2412.19437`（PDF） | 404 | 0.583s | 同上 |
| `example.invalid-domain-for-probe-xyz-9931.com`（域名不存在） | **500** | 3.323s | `internal_error: Internal Server Error` |

**两个坑**：

1. **404 是个大杂烩**。文档写得很清楚：404 涵盖「URL 不可用、被站点策略拦截、目标站点返回错误、内容类型不受支持、页面没有可提取的 Markdown 正文」五种完全不同的情况，但**响应体里只有一个笼统的 `failed to fetch URL`，无法区分原因**。你没法从错误里判断「是这个站封了爬虫，还是这个 URL 真的不存在，还是格式不支持」，只能自己重试或换源。
2. **DNS 解析不了居然返回 500 而不是 404**。一个根本不存在的域名，文档语义上应该归到 404「URL 不可用」，实测却是 `HTTP 500 internal_error`。而文档又说「遇到 5xx 请等待后重试」——按文档去重试一个不存在的域名会永远失败。**别照文档的重试建议无脑写，500 也要区分是不是可重试的。**

另外 `m.marsbit.co` 那次 404 耗了 4.4s，而 `36kr.com` 只用了 0.32s。同样是 404，耗时差 14 倍，说明有些站点是真的连上去被拒（慢），有些是快速失败（可能有负缓存）。**Fetch 的失败路径也要设超时**，不能只按成功路径的 0.3~1s 预估。

### 4.4 参数校验：报错信息很友好

```bash
# count 越界
{"query":"DeepSeek V4 模型 发布","count":99}
# → HTTP 400, 0.280s
{"error":{"message":"'count' must be between 1 and 50 (request_id: a28a6649-db34-4afe-99a0-4e162841d4dc)","type":"invalid_request_error","param":null,"code":"invalid_request_error"}}

# 缺 query
{"count":3}
# → HTTP 400, 0.280s
{"error":{"message":"Invalid JSON request: missing field `query` at line 1 column 11 (request_id: 42106ea0-df0e-4b39-b286-e4650bc69889)","type":"invalid_request_error","param":null,"code":"invalid_request_error"}}
```

报错直接告诉你字段名、取值范围和出错的行列号，是 OpenAI 兼容格式（`param` 恒为 `null`）。这一项做得比很多大厂 API 好。

### 4.5 鉴权与路径错误

```bash
# 故意用无效 Key
curl -X POST "https://api.deepseek.com/v1/tools/search" \
  -H "Authorization: Bearer sk-invalid-key-for-error-probe-000" ...
# → HTTP 401, 0.293s
{"error":{"message":"Authentication Fails, Your api key: ****-000 is invalid (request_id: 61225f20-9584-42dc-890e-12d3b656e069)","type":"authentication_error","param":null,"code":"invalid_request_error"}}
```

401 的报错里回显了 Key 的末 4 位（`****-000`）方便排查，服务端已做脱敏。**但注意 `type` 是 `authentication_error`，`code` 却还是 `invalid_request_error`**——两者不一致，判错时要用 `type` 或 HTTP 状态码，别只判 `code`。

```bash
# 路径少写 /tools
curl -X POST "https://api.deepseek.com/v1/search" ...
# → HTTP 404, 0.506s, 响应体为空（0 字节）
```

**这个 404 没有任何 JSON 错误体**，和 Fetch 的 404（有 JSON）完全不是一回事。写代码时如果按 `resp.json()` 解析会直接抛异常，要先判空。路径必须是 `/v1/tools/search` 和 `/v1/tools/fetch`。

### 4.6 完整错误码表（从 Fetch 抓回的官方文档，实测验证了其中 5 个）

| HTTP | 说明 | 本次实测 |
|---|---|---|
| 400 | 请求头或请求体不合法，含 JSON 格式错误或字段值无效 | ✅ 触发 3 次（编码、count 越界、缺 query） |
| 401 | API Key 缺失或无效 | ✅ 触发 1 次 |
| 402 | 账号余额不足 | 未触发 |
| 403 | Fetch 请求被安全策略拒绝 | 未触发 |
| 404 | Fetch 未能获取可用网页内容 | ✅ 触发 6 次 |
| 408 | 读取请求体超时 | 未触发 |
| 413 | 请求体超过大小限制 | 未触发 |
| 429 | 超过 Search 或 Fetch 的 QPS 限制 | 未触发（未做压测，避免影响账号） |
| 500 | 服务器内部错误 | ✅ 触发 1 次（不存在的域名） |
| 504 | 下游请求超时 | 未触发 |

### 4.7 搜索语法操作符：真的生效

```bash
# site: 限定
{"query":"DeepSeek Search API site:api-docs.deepseek.com","count":5}
# → HTTP 200, 0.542s, 5 条结果 URL 全部命中 api-docs.deepseek.com：
#   2.26 /zh-cn/guides/search/   2.23 /guides/search/   2.20 /api/search/
#   2.20 /updates/               2.18 /zh-cn/api/search/

# after: 时间过滤
{"query":"DeepSeek 上线 Search Fetch API after:2026-10-16","count":5}
# → HTTP 200, 0.668s, 5 条结果 date 全部为 2026-10-16T16:00:00+00:00：
#   2.66 api-docs.deepseek.com/updates/    2.32 sohu.com    2.22 app.qudong.com
#   2.05 pandaily.com                      1.51 en.theblockbeats.news
```

两个操作符都精确生效，`site:` 是严格限定（5/5 命中），`after:` 是严格时间过滤（5/5 符合），并且 `after:` 那次的最高分 2.66 是本次实测所有请求里的最高分——**加时间约束反而提升了相关性**。做「只查官方文档」「只查最近 N 天」这类需求时，用操作符比事后过滤 URL/日期靠谱得多。

## 八、总体评价

### 优点

1. **相关性是真本事，不是关键词匹配。** 案例 1 那条三实体混合的口语化长 query（Jev + REA + OPC），6 条结果精确分成两半覆盖问题的两个侧面，最高分 2.22。这是自研搜索引擎该有的表现。
2. **`rerank_score` 和 `authority` 是可以直接拿来做决策的信号。** 文档说 ≥1.5 为优质、authority ≥3 为极权威，实测数据完全对得上：好 query 全在 2.0~2.7，弱 query 全在 0.88~1.09，中间没有模糊地带。**这两个数字让 Agent 能自己判断「这批结果可不可信」**，比单纯返回一个列表有用得多。
3. **快。** 25 次请求里，Search 稳定在 0.28~1.02s（中位约 0.6s），Fetch 成功路径 0.38~5.05s。对交互式 Agent 完全够用。
4. **便宜且计费诚实。** 5 元/千次，按次不按 token，`count=50` 和 `count=1` 一个价。失败、无结果、超时不计费——本次实测 9 个失败请求一分钱没花。
5. **`need_content=true` 是被低估的杀手锏。** 一次请求同时完成检索和抓取，还能绕过 Fetch 抓不到的反爬站点。对轻量 RAG 来说，很多场景根本不需要单独调 Fetch。
6. **错误信息友好。** 参数校验会告诉你字段名、取值范围、出错的行列号；每个错误都带 `request_id`，报障有据可查。
7. **搜索语法操作符真的生效**（`site:` / `after:` 实测严格过滤），能显著提升垂直场景的相关性。
8. **无结果的语义干净。** 返回 `{"results":[]}` 而不是报错或硬凑，判断成本低。
9. **Fetch 的文本提取质量高。** 19,397 字符的 GitHub 页面里 0 个 HTML 标签，代码内容一字未丢，表格单元格数据完整。

### 局限

1. **Fetch 返回的 Markdown 是「假 Markdown」。** 所有特殊字符被反斜杠转义（代码/表格密集的页面反斜杠占比 11%），标题变字面 `#`、表格无法渲染、围栏代码块降级为缩进，而链接和图片**完全丢失**。跑一遍反转义正则可以把反斜杠从 2,160 个降到 25 个，标题/表格/代码都能恢复成合法 Markdown（实测验证，见第六节）；但链接和图片在抓取阶段就被丢掉了，救不回来。
2. **Fetch 的可用性受目标站点摆布，且失败原因不可区分。** 本次 8 次 Fetch 里 5 次失败：36Kr（两个域名都失败）、marsbit、raw.githubusercontent.com、arXiv PDF 全部 404，错误体只有一句笼统的 `failed to fetch URL`，无法判断是反爬、格式不支持还是 URL 失效。中文资讯站（36Kr 这类）抓不到，对做中文内容 Agent 是个实打实的短板。
3. **不支持非 HTML 内容。** PDF 直接 404，`text/plain` 的 raw 文件也 404。
4. **DNS 级别的失败返回 500 而不是 404**，与文档的重试建议冲突（照文档重试一个不存在的域名会死循环）。
5. **Fetch 的失败路径可能很慢。** 同样 404，有 0.32s 的也有 4.42s 的，必须自己设超时。
6. **Search 结果不去重。** 同一篇文章的 `m.` / `news.` 两个域名会各占一条，浪费 `count` 名额。
7. **`date` 是 UTC 且经常为 null。** 本次 5 条结果里 3 条 `date` 为 null，时间过滤只能覆盖一部分结果；用北京时间判断时必须 +8 换算。
8. **长尾/冷门中文 query 会向热点坍缩。** 案例 4.2 里 5 条结果有 3 条是同一个 2024 年新闻事件的转载，且分数全部 <1.5。
9. **`localization` 效果噪音大**，模糊 query 下会混进外语结果。
10. **QPS 只有 5**（Search、Fetch 各自独立），批量抓取要自己做限流。
11. **`deepseek-chat` 的响应 `model` 字段返回 `deepseek-flash`**，不能拿来做严格断言。
12. **中文请求在 Windows shell 下有编码坑**，必须用 UTF-8 文件 + `--data-binary @file`，否则 400。

### 适用场景

**适合**：

- Agent / RAG 的实时联网检索层——这是官方定位，实测也确实撑得住。推荐直接用 `need_content=true`，一步到位。
- 舆情、热点、竞品监测：时效性好（能搜到当天的新闻），`date` + `after:` 可做时间窗过滤。
- 垂直站点检索：`site:` 操作符严格生效，做「只查官方文档」「只查某几个信源」很稳。
- 让 Agent 自己读 API 文档/技术文档并生成调用代码（案例 2 已验证：整份文档的表格被完整抓回）。
- 用 `rerank_score` / `authority` 做结果置信度门控，这是它相对通用搜索 API 最独特的一点。
- 中文内容检索：中文 query 的理解和中文信源覆盖明显好于多数国外搜索 API。

**不适合**：

- 需要保真 Markdown 的场景（文档镜像、知识库入库、渲染展示）——反转义能救回标题/表格/代码，但链接和图片在抓取阶段就已丢失，无法还原；围栏代码块也只能降级为缩进式。
- 需要抓 PDF、纯文本文件、二进制内容的场景。
- 依赖 36Kr 这类反爬中文资讯站全文的场景——请直接用 `need_content=true` 从 Search 拿正文，别走 Fetch。
- 高并发批量抓取（QPS 5）。
- 长尾冷门话题的事实性检索——分数会掉到 1.5 以下，且结果向热点坍缩，必须加阈值拦截。

## 九、容易踩的坑（速查）

- **Git Bash 里内联中文 `-d` 会 400。** 报 `invalid unicode code point at line 1 column N`。改用 UTF-8 无 BOM 文件 + `--data-binary @file.json`。
- **路径是 `/v1/tools/search`，不是 `/v1/search`。** 后者返回 404 且**响应体为空**，`resp.json()` 会直接抛异常。
- **`count` 上限 50**，写 99 会 400（报错信息很明确）。
- **判 401 用 `type` 字段，别用 `code`。** 401 的 `type` 是 `authentication_error`，但 `code` 仍是 `invalid_request_error`。
- **Fetch 的 404 不代表 URL 不存在。** 它同时表示反爬拦截、格式不支持、无可提取正文。别按「URL 错了」去改 URL。
- **Fetch 遇到不存在的域名返回 500。** 别照文档「5xx 重试」的建议无脑重试，先校验域名。
- **Fetch 必须设超时。** 失败路径观测到 4.4s，成功路径最长 5.0s。
- **Fetch 回来的 Markdown 要先反转义再用。** 一行正则（附录 C 有完整写法）即可去掉 `\#` `\-` `\.` 这类转义。但链接和图片在抓取阶段就已经丢了，反转义救不回来。
- **抓 GitHub 请抓 `raw` 以外的路径——但 raw 也不行。** `github.com/owner/repo` 能抓（但带文件列表噪音），`raw.githubusercontent.com/.../README.md` 返回 404。
- **`rerank_score < 1.5` 就当没查到。** 别把弱相关结果喂给模型，它会当成事实。
- **Search 结果要自己去重**（同稿多域名）。
- **`date` 是 UTC，且常为 null。** 北京时间要 +8；时间过滤只能覆盖有 `date` 的那部分。
- **优先用 `need_content=true`，而不是 Search + Fetch 两跳。** 更便宜、更快、且能绕过反爬站点。代价是响应体积翻倍（同 query 从 22KB 涨到 52KB）和 `summary` 被压短。
- **响应里的 `model` 字段可能和你请求的不一致**（`deepseek-chat` → `deepseek-flash`）。

## 十、下一步

- 把本次的 payload/响应做成脚本化回归集，接口有变更时一键复跑。
- 测 QPS 5 的真实限流行为（429 的响应体和重试窗口），本次为避免影响账号未做压测。
- 试 `ori_question` 的对照实验（同 query 传/不传），确认它对排序的实际影响。
- 把 Search 接到本仓库的 Atlas Playground 里，做「Jev 判断 + 实时检索」的组合示例。
- 2026-11-01 起 Harness/Claude Code/Codex 的 Server 端 Search/Fetch 调用开始计费，届时需要重估成本。

---

## 附录 A：完整请求日志（25 次真实调用）

时间戳均为 curl 的 `%{time_total}`（含 TLS 握手与传输），大小为 `%{size_download}`。

| # | 端点 | 关键参数 / 目标 | HTTP | 耗时 | 大小 | request_id |
|---|---|---|---|---|---|---|
| 1 | `/v1/tools/search` | 中文内联 `-d`（编码坑） | **400** | 0.315s | 218B | `9527f1bd-8f02-4591-98a9-a7b7b1a6e304` |
| 2 | `/v1/tools/search` | `count=3`（最小验证） | 200 | 0.538s | 9,022B | — |
| 3 | `/v1/tools/search` | 案例1 `count=6` | 200 | 0.719s | 29,547B | — |
| 4 | `/v1/tools/fetch` | `eu.36kr.com/zh/p/4020977643671686` | **404** | 0.437s | 163B | `280b42fe-c83c-420f-845e-74ba3bdef5ab` |
| 5 | `/v1/tools/fetch` | `www.news.cn/tech/20260702/a806a24f...` | 200 | **5.047s** | 13,709B | — |
| 6 | `/v1/tools/search` | `count=2, need_content=true` | 200 | 0.721s | 22,394B | — |
| 7 | `/v1/tools/fetch` | `m.marsbit.co/newsdetail/20261011...` | **404** | **4.425s** | 163B | `a217ce05-029e-4b86-84d2-ed0d9402339c` |
| 8 | `/v1/tools/fetch` | `36kr.com/p/4020977643671686` | **404** | 0.320s | 163B | `1d6303d1-f615-45bb-ab6c-3d6ff9c39bd9` |
| 9 | `/v1/tools/search` | 案例1 `count=4, need_content=true` | 200 | 1.024s | 51,858B | — |
| 10 | `/v1/tools/search` | 案例2 + `ori_question` | 200 | 0.938s | 20,084B | — |
| 11 | `/v1/tools/fetch` | `api-docs.deepseek.com/zh-cn/guides/search/` | 200 | 0.474s | 10,467B | — |
| 12 | `/v1/tools/fetch` | `github.com/deepseek-ai/DeepSeek-V3` | 200 | 0.378s | 21,929B | — |
| 13 | `/v1/tools/fetch` | `raw.githubusercontent.com/.../README.md` | **404** | 0.332s | 163B | `c590bb8a-f6f1-438f-8fb0-2f59c3037f52` |
| 14 | `/v1/tools/search` | 编造 query（无结果） | 200 | 0.667s | **14B** | — |
| 15 | `/v1/tools/fetch` | `example.invalid-domain-for-probe-xyz-9931.com` | **500** | 3.323s | 157B | `d9510382-cdc2-479d-b1ae-2043c766a4e0` |
| 16 | `/v1/tools/search` | `count=99`（越界） | **400** | 0.280s | 182B | `a28a6649-db34-4afe-99a0-4e162841d4dc` |
| 17 | `/v1/tools/search` | 缺 `query` 字段 | **400** | 0.280s | 213B | `42106ea0-df0e-4b39-b286-e4650bc69889` |
| 18 | `/v1/search` | **路径写错**（少 `/tools`） | **404** | 0.506s | **0B** | — |
| 19 | `/v1/tools/search` | `site:api-docs.deepseek.com` | 200 | 0.542s | 17,060B | — |
| 20 | `/v1/tools/search` | `after:2026-10-16` | 200 | 0.668s | 16,610B | — |
| 21 | `/v1/tools/search` | 冷门中文 query | 200 | 0.623s | 23,863B | — |
| 22 | `/v1/tools/fetch` | `arxiv.org/pdf/2412.19437`（PDF） | **404** | 0.583s | 163B | `98028dcc-5986-4e78-bd44-593ec00398c8` |
| 23 | `/v1/chat/completions` | `deepseek-chat`, 13,651 prompt tokens | 200 | 8.567s | 5,824B | id `c77754d6-3901-4afc-afd9-ba9d6dbcabaa` |
| 24 | `/v1/tools/search` | **无效 Key** | **401** | 0.293s | — | `61225f20-9584-42dc-890e-12d3b656e069` |
| 25 | `/v1/tools/search` | `localization=116.40.90.2` | 200 | 0.559s | 9,858B | — |

统计：25 次调用，15 次 200，10 次失败（400×3、401×1、404×5、500×1）。Search 12 次全部 200；Fetch 8 次中 3 次 200、5 次失败（**成功率 37.5%**）。

## 附录 B：裁剪后的原始响应

### B1 Search 成功响应（第 1 条结果，`content` 因 `need_content=false` 为空）

```json
{"results":[{
  "title": "DeepSeek“无预警”上新，为何引发巨大关注？_央广网",
  "site_name": "央广网",
  "site_icon": "https://cdn.deepseek.com/site-icons/cnr.cn",
  "authority": 4,
  "rerank_score": 2.289104461669922,
  "date": "2026-04-26T16:00:00+00:00",
  "url": "http://tech.cnr.cn/gstj/20260427/t20260427_527601623.shtml",
  "snippet": "2026-04-27 09 ... 4月24日，DeepSeek“无预警”发布了新一代旗舰大模型DeepSeek-V4并…",
  "summary": "# DeepSeek“无预警”上新，为何引发巨大关注？\n\n2026-04-27 09:53:15来源：科技日报\n...\n4月24日，DeepSeek“无预警”发…",
  "content": ""
}]}
```

字段顺序即接口原始返回顺序：`title, site_name, site_icon, authority, rerank_score, date, url, summary, snippet, content`。

### B2 `need_content=true` 时 `content` 被填充

```json
{"results":[
  {"title":"DeepSeek“无预警”上新，为何引发巨大关注？_央广网",
   "summary":"…(712 字)…",
   "content":"# DeepSeek“无预警”上新，为何引发巨大关注？\n\n2026-04-27 09:53:15来源：科技日报\n\n千呼万唤始出来。\n\n4月24日，DeepSeek“无预警”发布了新一代旗舰大模型DeepSeek-V4并开源。…(共 1,071 字)"},
  {"title":"DeepSeek-V4-Flash - deepseek模型",
   "summary":"…(3,409 字)…",
   "content":"# 鲸智社区·大模型公共服务平台\n\n# DeepSeek-V4-Flash（deepseek-ai/DeepSeek-V4-Flash）\n\n…总参数 284B、激活参数 13B，原生支持100 万 token 超长上下文。…(共 10,868 字)"}
]}
```

注意：`need_content=true` 时 `content` 里的 `#`、`：` **没有被转义**（和 Fetch 返回的 `content` 不同！），是干净 Markdown。这是 Search 的 `content` 优于 Fetch `content` 的又一个理由。

### B3 Fetch 成功响应（新华网，反转义后节选）

```json
{"content":"新华网 \\> 科技 \\> 正文\n\n2026 07\\/02 11\\:04\\:02\n\n来源：瞭望东方周刊\n\n\\# “一人公司”如何行稳致远\n\n——专访复旦大学新闻学院副院长、教授姚建华\n\nOPC仅凭一人、一台电脑、一套AI工具，便可独立完成产品研发、内容创作、市场拓展、客户服务等全业务链条…\n\n截至今年6月，全国已有23个城市出台OPC专项扶持政策…","title":"“一人公司”如何行稳致远"}
```

4,924 字符 / 117 行 / 22 个反斜杠（0.45%）。

### B4 Fetch 成功响应（GitHub README，原始转义形态，未加工）

```
\# GitHub \- deepseek\-ai\/DeepSeek\-V3 · GitHub

\#\# 1\\\. Introduction

\*\*Model\*\* \| \*\*\#Total Params\*\* \| \*\*\#Activated Params\*\* \| \*\*Context Length\*\* \| \*\*Download\*\*
\-\-\-\|\-\-\-\|\-\-\-\|\-\-\-\|\-\-\-
DeepSeek\-V3\-Base \| 671B \| 37B \| 128K \| 🤗 Hugging Face

    cd DeepSeek\-V3\/inference
    pip install \-r requirements\.txt
```

19,397 字符 / 297 行 / 2,160 个反斜杠（11.14%）/ 0 个围栏代码块 / 0 个链接 / 0 个图片 / 0 个 HTML 标签。

### B5 Fetch 成功响应（官方文档页，字段表原样抓回）

```
字段 \| 类型 \| 必填 \| 默认值 \| 说明
\-\-\-\|\-\-\-\|\-\-\-\|\-\-\-\|\-\-\-
query \| string \| 是 \| \- \| 搜索词，1～128 个 Unicode 字符，不能全为空白，支持下方列出的搜索语法。
count \| integer \| 否 \| 10 \| 返回条数，1～50。
need\_content \| boolean \| 否 \| false \| 是否在 content 中返回网页正文，为 false 时 content 恒为空字符串。

请求体中的未知字段会被忽略。
```

6,402 字符 / 220 行 / 716 个反斜杠（11.18%）/ 21 行 4 空格缩进 / 5 个残留 HTML 标签 / 0 个围栏代码块。

### B6 无结果

```json
{"results":[]}
```

`HTTP 200`，响应体 14 字节，不计费。

### B7 全部错误响应体（原样）

```json
// 400 编码（Git Bash 内联中文）
{"error":{"message":"Invalid JSON request: invalid unicode code point at line 1 column 25 (request_id: 9527f1bd-8f02-4591-98a9-a7b7b1a6e304)","type":"invalid_request_error","param":null,"code":"invalid_request_error"}}

// 400 count 越界
{"error":{"message":"'count' must be between 1 and 50 (request_id: a28a6649-db34-4afe-99a0-4e162841d4dc)","type":"invalid_request_error","param":null,"code":"invalid_request_error"}}

// 400 缺 query
{"error":{"message":"Invalid JSON request: missing field `query` at line 1 column 11 (request_id: 42106ea0-df0e-4b39-b286-e4650bc69889)","type":"invalid_request_error","param":null,"code":"invalid_request_error"}}

// 401 无效 Key
{"error":{"message":"Authentication Fails, Your api key: ****-000 is invalid (request_id: 61225f20-9584-42dc-890e-12d3b656e069)","type":"authentication_error","param":null,"code":"invalid_request_error"}}

// 404 Fetch 失败（36Kr / marsbit / raw.githubusercontent / arXiv PDF 全部是这个，无法区分原因）
{"error":{"message":"failed to fetch URL (request_id: 280b42fe-c83c-420f-845e-74ba3bdef5ab)","type":"not_found_error","param":null,"code":"invalid_request_error"}}

// 404 路径写错（/v1/search）→ 响应体完全为空，0 字节，无 JSON

// 500 域名不存在
{"error":{"message":"Internal Server Error (request_id: d9510382-cdc2-479d-b1ae-2043c766a4e0)","type":"internal_error","param":null,"code":"internal_error"}}
```

### B8 Chat 合成响应的元数据

```json
{
  "id": "c77754d6-3901-4afc-afd9-ba9d6dbcabaa",
  "model": "deepseek-flash",
  "usage": {
    "prompt_tokens": 13651, "completion_tokens": 1296, "total_tokens": 14947,
    "prompt_tokens_details": {"cached_tokens": 0},
    "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 13651
  }
}
```

请求 `model` 为 `deepseek-chat`，响应 `model` 为 `deepseek-flash`。耗时 8.567s，正文见第四节。

### B9 各次 Search 的结果质量对照

| 响应文件 | query 类型 | 条数 | summary 合计字数 | content 合计字数 | rerank_score |
|---|---|---|---|---|---|
| `c_validate.json` | 明确技术词 | 3 | 4,651 | 0 | 2.29, 2.26, 2.26 |
| `c_case1_search.json` | 三实体口语长句 | 6 | 12,580 | 0 | 2.22, 1.59, 1.53, 1.49, 1.43, 1.43 |
| `c_case1_search_nc.json` | 同上 + need_content | 4 | 9,393 | **16,274** | 2.22, 1.59, 1.53, 1.49 |
| `c_case2_search.json` | 时效性新闻 | 5 | 12,438 | 0 | 2.35, 2.30, 2.29, 2.10, 2.07 |
| `c_case4_syntax_site.json` | `site:` 限定 | 5 | 11,596 | 0 | 2.26, 2.23, 2.20, 2.20, 2.18 |
| `c_case4_syntax_after.json` | `after:` 时间过滤 | 5 | 8,681 | 0 | **2.66**, 2.32, 2.22, 2.05, 1.51 |
| `c_probe_needcontent.json` | need_content 探针 | 2 | 4,121 | 11,939 | 2.29, 2.26 |
| `c_probe_localization.json` | 模糊 + localization | 5 | 2,826 | 0 | 1.16, 1.11, 1.01, 0.95, 0.95 |
| `c_case4_niche.json` | 冷门长尾 | 5 | 6,977 | 0 | 1.09, 0.98, 0.94, 0.89, 0.88 |
| `c_case4_search.json` | 编造无结果 | **0** | 0 | 0 | （空数组） |

规律很清楚：**query 越具体、越带约束（`site:` / `after:`），分数越高**；模糊 query 和长尾 query 全线掉到 1.5 以下。`after:2026-10-16` 拿到了本次实测的最高分 2.66。

## 附录 C：复现方法

所有 payload 都是 UTF-8 无 BOM 的 JSON 文件，用 `--data-binary @file` 发送（不要用内联 `-d`，中文会 400）。

```bash
export DEEPSEEK_API_KEY="sk-****"

# 通用调用函数
ds() {  # ds <search|fetch> <payload.json> <out.json>
  curl -sS -w '[HTTP %{http_code}] [%{time_total}s] [%{size_download}B]\n' \
    -X POST "https://api.deepseek.com/v1/tools/$1" \
    -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
    -H "Content-Type: application/json" \
    --data-binary @"$2" -o "$3"
}

ds search p_validate.json      c_validate.json       # 最小验证
ds search p_case1_search.json  c_case1_search.json   # 案例 1 检索
ds fetch  p_case1_fetchB.json  c_case1_fetchB.json   # 案例 1 抓取
ds search p_case2_search.json  c_case2_search.json   # 案例 2 时效性
ds fetch  p_case2_fetch.json   c_case2_fetch.json    # 案例 2 抓文档
ds fetch  p_case3_fetch.json   c_case3_fetch.json    # 案例 3 保真度
ds search p_case4_search.json  c_case4_search.json   # 案例 4 无结果
ds search p_case4_niche.json   c_case4_niche.json    # 案例 4 冷门
```

本次实测用到的 payload 文件（共 16 个）全部列在附录 A 的「关键参数」列，照抄即可复现。

Node 侧的解析辅助脚本（无 jq 环境下用来统计保真度指标）：

```javascript
// summarize_fetch.js — 量化 Markdown 保真度
const fs = require('fs');
const d = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const c = d.content || '';
const cnt = (s) => c.split(s).length - 1;
console.log('title:', d.title, '| chars:', c.length, '| lines:', c.split('\n').length);
console.log('backslash:', cnt('\\'), '(' + (cnt('\\') / c.length * 100).toFixed(2) + '%)');
console.log('fence ```:', cnt('```'), '| pipes:', cnt('|'), '| inline `:', cnt('`'));
console.log('4sp-code-lines:', c.split('\n').filter(l => /^ {4}\S/.test(l)).length);
console.log('residual html:', (c.match(/<[a-zA-Z][^>]*>/g) || []).length);
```

```javascript
// 反转义，把 Fetch 返回的「假 Markdown」还原成可读文本（链接/图片已丢失，无法还原）
const unescape = (s) => (s || '').replace(/\\([\\`*_{}\[\]()#+\-.!>~|:"'/,=&])/g, '$1');
```
