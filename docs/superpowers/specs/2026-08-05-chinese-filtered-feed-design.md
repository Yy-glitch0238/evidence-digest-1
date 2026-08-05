# Evidence Digest 中文筛选 RSS 设计

## 1. 目标

目标仓库：`Yy-glitch0238/evidence-digest-1`。

为用户当前选择的期刊生成一个面向 Inoreader 的中文 Atom 订阅源。该订阅源只收录有 PubMed 摘要的实质性文献，排除新闻、期刊公告、社论、评论、来信、勘误和撤稿。每篇文章由 Cloudflare Workers AI 生成中文的研究目的、方法、主要结果和结论，并在标题中显示期刊的标准 MEDLINE 缩写。

系统每周日北京时间 23:00 自动抓取，也保留 GitHub Actions 的手动运行入口。用户可以在一周内任何时间刷新。

## 2. 非目标

- 不改变现有英文网站、英文主题 Feed 或 `feeds/all.xml`。
- 不让 AI 决定文章是否收录，也不让 AI 改写原始 PubMed 数据。
- 不根据标题推测没有摘要的文章内容。
- 不删除原始归档中的噪声记录；筛选只作用于中文 AI 与中文 RSS 的发布路径。
- 不在 Evidence Digest 网站中增加公开的“立即抓取”按钮。即时刷新使用受 GitHub 登录保护的 `Run workflow`。

## 3. 用户可见结果

新增订阅地址：

`https://Yy-glitch0238.github.io/evidence-digest-1/feeds/selected-journals-zh.xml`

每条 Atom 记录使用独立且稳定的中文 Feed ID：`urn:evidence-digest:zh:<pmid>`。卡片标题格式为：

`【Nat Immunol】原始英文标题`

其中期刊名来自现有 `journal.ta`，即 PubMed/MEDLINE 标准缩写，而不是自定义且可能产生歧义的缩写。

Atom 的纯文本 `summary` 和 HTML `content` 都包含：

1. 研究目的
2. 研究方法
3. 主要结果
4. 结论
5. PubMed 原文链接

Inoreader 卡片预览可能只显示前几行；打开卡片后可查看完整四段内容。中文 Feed 最多携带最近 200 篇合格文献，足以覆盖预期每周 50–150 篇的峰值，Inoreader 会继续保留以前已经接收的条目。

## 4. 数据流

```text
PubMed 抓取
  -> 原始记录归档（保持现状）
  -> 确定性资格筛选
  -> 只对合格且未缓存的文章调用 Cloudflare Workers AI
  -> 校验并持久化中文摘要缓存
  -> 构建 selected-journals-zh.xml
  -> GitHub Pages
  -> Inoreader
```

原始归档仍是事实来源。中文摘要是可重建的派生数据。构建中文 Feed 时还必须确认文章的 `journal.ta` 仍存在于当前 `pipeline/config/journals.json`，防止被用户移除的旧期刊通过历史归档重新出现。

## 5. 确定性资格筛选

新增一个无网络、无随机性的筛选单元。文章必须同时满足：

- `hasAbstract` 为 `true` 且 `abstract` 非空；
- `journal.ta` 位于当前期刊配置；
- 不含下列 PubMed publication types：`News`、`Editorial`、`Comment`、`Letter`、`Published Erratum`、`Retraction of Publication`、`Retracted Publication`；
- 标题不命中保守的非研究模式。

标题模式采用不区分大小写、优先从标题开头匹配的规则，覆盖：

- `Correction:`、`Author Correction:`、`Publisher Correction:`；
- `Erratum:`、`Retraction:`、`Retraction Note:`；
- `Reviewer Highlight`；
- 明确的期刊月度或年度 `News` 汇总；
- `Editorial Board`、`Issue Highlights`、`Table of Contents`。

不得使用“标题中任意出现 news 就排除”之类的宽泛规则，以免误伤正常研究标题。综述、系统综述、Meta 分析、临床试验、观察性研究、基础研究和病例报告均可保留，只要满足上述规则。

筛选失败的记录不会调用 AI，也不会进入中文 Feed，但仍保留在原始档案及现有英文产品中。

## 6. 中文摘要生成

### 6.1 服务与模型

- 服务：Cloudflare Workers AI REST API。
- 模型：`@cf/qwen/qwen3-30b-a3b-fp8`。
- GitHub Secrets：`CLOUDFLARE_ACCOUNT_ID`、`CLOUDFLARE_AI_TOKEN`。
- 密钥只作为 Actions 环境变量使用，不写入代码、数据文件或日志。

### 6.2 输入与提示规则

每次请求只发送文章 PMID、英文标题和 PubMed 英文摘要。系统提示要求：

- 仅根据提供的标题和摘要总结；
- 使用简体中文和医学专业但清晰的语言；
- 不补充摘要之外的数字、机制、样本或结论；
- 摘要没有说明的字段写“摘要未说明”；
- 返回固定 JSON 对象，字段为 `objective`、`methods`、`results`、`conclusion`。

### 6.3 校验

程序接受响应前必须验证：

- JSON 可解析；
- 四个字段全部存在且为非空字符串；
- 每个字段在配置的最大长度内；
- 输出不包含模型思考标记、Markdown 代码围栏或额外键；
- 至少包含中文字符。

验证失败或遇到 429、5xx、连接超时，按指数退避最多重试两次。未成功的文章不进入本次中文 Feed，并在下一次工作流重试。

## 7. 缓存与数据结构

中文摘要按生成日期追加到 `data/enrichment/zh/<YYYY-MM-DD>.jsonl.gz`。每条缓存记录至少包含：

- `pmid`
- `sourceHash`
- `model`
- `promptVersion`
- `objective`
- `methods`
- `results`
- `conclusion`
- `generatedAt`

`sourceHash` 是标题、摘要、模型 ID 和提示版本的 SHA-256。构建时为每个 PMID 选择最新且通过校验的记录。只有 source hash 不存在或发生变化时才重新调用 AI，因此同一篇文章不会在每周构建中重复消耗额度。

首次运行从当前服务窗口中按新到旧处理最多 200 篇合格且缺少缓存的文献；以后优先处理本次新抓取的文章，再补齐历史缺口。单次上限为 200，能够覆盖预期周峰值，同时避免异常积压意外耗尽每日免费额度。

为该缓存增加独立契约或校验函数，不改变现有 `Study` 归档 schema。

## 8. Atom Feed 生成

在现有 Feed 生成器旁新增中文专用 Feed 路径，不改变英文 Feed 的输出。构建步骤：

1. 从服务窗口读取归档；
2. 应用资格筛选；
3. 只保留存在有效中文缓存的文章；
4. 按现有 `_sort_key` 排序；
5. 截取最近 200 篇；
6. 写入 `feeds/selected-journals-zh.xml`。

XML 必须正确转义标题、中文字段和链接。`summary` 提供便于卡片预览的纯文本，`content type="html"` 提供带段落和 PubMed 链接的完整正文。Feed 级 `updated` 使用构建时间；条目使用缓存的 `generatedAt` 作为 `updated`，并使用论文的 `entryDate` 作为 `published`。

## 9. GitHub Actions 调度

Harvest 工作流改为：

- 定时：`0 15 * * 0`，即每周日 UTC 15:00 / 北京时间 23:00；
- 首次计划时间：若修改在此前部署，则为 2026-08-09 23:00（北京时间）；
- 定时运行回查最近 8 天，依赖 PMID seen store 去重，以覆盖索引延迟和周界重叠；
- 保留 `workflow_dispatch`；手动运行默认同样回查 8 天，同时允许用户覆盖天数。

工作流顺序为：

1. Validate configuration
2. Harvest PubMed
3. Enrich eligible records in Chinese
4. Commit archive, state, and successful enrichment cache
5. Check health
6. Build API, web app, English feeds, and Chinese feed
7. Deploy GitHub Pages

AI 单篇失败属于软失败：记录警告和计数，但不阻断抓取、构建或部署。配置完全缺失时中文摘要步骤跳过并给出明确警告，已有中文缓存仍可用于构建。

## 10. 安全与运行约束

- Cloudflare Token 采用最小权限，只允许调用 Workers AI。
- Actions 日志不打印请求头、Token 或完整 API 响应。
- 每次 AI 请求设置连接与读取超时。
- 逐篇或小批量顺序处理，避免瞬时速率峰值。
- 单次最多处理 200 篇；当前每周 50–150 篇规模预计远低于 Cloudflare 每日 10,000 Neurons 免费额度。
- 免费额度耗尽时只暂停缺失摘要，下一次运行继续；不自动启用付费方案。

## 11. 测试策略

### 11.1 筛选测试

- 每种明确排除的 publication type 均有独立测试；
- 每种标题排除模式均有阳性测试；
- 正常标题中包含普通词义的 `news` 不应被误删；
- 无摘要文章排除；
- 有摘要的综述、RCT、观察研究、基础研究和病例报告保留；
- 已从期刊配置移除的历史文章排除。

### 11.2 AI 客户端与缓存测试

- 测试正确 JSON、缺字段、额外字段、非中文、超长内容和代码围栏；
- 使用本地假响应测试 429、5xx、超时和两次重试，不在 CI 中调用真实 Cloudflare；
- 相同 source hash 命中缓存；标题、摘要、模型或提示版本变化导致重新生成；
- 缓存文件可重复读取，损坏的单条记录不会破坏其他记录。

### 11.3 Feed 与构建测试

- Atom XML 可被 `ElementTree` 重新解析；
- 标题包含标准期刊缩写；
- `summary` 和 `content` 包含四个中文标签和 PubMed 链接；
- 英文 Feed 输出保持字节级兼容；
- 中文 Feed 不包含被排除、无摘要或未完成中文缓存的文章；
- 上限、排序、稳定 ID 和去重符合设计。

### 11.4 工作流测试

- CI 在没有真实 Cloudflare Secrets 时运行全部单元测试；
- 验证 cron、8 天默认窗口和手动入口；
- 完整 Python、Web、Worker 和契约测试全部通过后才上线。

## 12. 上线与回退

上线顺序：

1. 合并代码与测试；
2. 在仓库 Secrets 添加 `CLOUDFLARE_ACCOUNT_ID` 和 `CLOUDFLARE_AI_TOKEN`；
3. 手动运行一次 Harvest，回查 8 天并生成最近最多 200 篇合格文献的缓存；
4. 检查 Actions 摘要中的抓取数、排除数、AI 成功数与失败数；
5. 在浏览器打开新 Atom URL，确认四段中文内容；
6. 在 Inoreader 添加新 Feed；
7. 确认正常后删除旧订阅。

新 Feed 的条目数量少于当前 51 篇是预期结果，因为新闻、公告、勘误和无摘要记录会被移除。

回退时只需重新使用原英文 Feed。原始归档和所有英文 Feed 未改变，因此无需恢复数据或重新抓取。

## 13. 验收标准

- 每周日北京时间 23:00 自动运行，并可在 GitHub Actions 随时手动运行；
- 自动和手动默认回查 8 天且不产生重复 PMID；
- 中文 Feed 仅含当前期刊配置内、有摘要且未命中噪声规则的文章；
- 每条中文 Feed 记录的标题包含 MEDLINE 期刊缩写，正文包含四段中文摘要和 PubMed 链接；
- 相同输入不重复调用 Cloudflare AI；
- 单篇 AI 失败不会使 Harvest 或 Pages 部署失败，并会在后续运行重试；
- 现有英文产品和原始档案保持不变；
- 所有自动测试通过，并在 Inoreader 完成一次人工验收。
