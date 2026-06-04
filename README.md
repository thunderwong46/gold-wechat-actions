# 每日黄金微信推送

这个模板用于把每日黄金 24 小时判断报告放到 GitHub Actions 云端运行。电脑、Codex、浏览器都不用在线。

## 使用步骤

1. 新建一个 GitHub 仓库，私有或公开都可以。
2. 把本文件夹里的所有内容上传到仓库根目录。
3. 在仓库里进入 `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`。
4. 添加密钥：
   - `SERVERCHAN_SENDKEY`：你的 Server酱 SendKey。
   - `TWELVE_DATA_API_KEY`：推荐。用于抓取 XAU/USD 现货黄金 5分钟级最新行情。可在 Twelve Data 注册免费 key。
   - `OPENAI_API_KEY`：可选。填了会用 AI 润色和综合判断；不填也会生成规则版报告。
   - `SMTP_HOST`：发件邮箱的 SMTP 服务器，例如 Gmail 可用 `smtp.gmail.com`。
   - `SMTP_PORT`：SMTP 端口，常见为 `465`。
   - `SMTP_USERNAME`：发件邮箱账号。
   - `SMTP_PASSWORD`：发件邮箱的 SMTP 密码或应用专用密码。
   - `SMTP_FROM`：可选。发件人地址；不填则默认使用 `SMTP_USERNAME`。
   - `SMTP_SECURITY`：可选。默认 `ssl`；如果你的邮箱服务使用 587 端口，可填 `starttls`。
5. 进入 `Actions`，启用工作流。

本项目交给 cron-job.org 定时触发，电脑、Codex、浏览器都不用在线。GitHub Actions 页面也可以手动运行，运行时选择：

- `daily`：生成每日黄金24小时判断报告，推送微信，同时发送邮件到 `thunderwong46@gmail.com`，并保存当天报告和行情快照。
- `weekly`：生成每周复盘报告，推送微信，同时发送邮件到 `thunderwong46@gmail.com`，统计本周预测和真实金价之间的差距。

## cron-job.org 配置

日报任务：

- 时间：每天北京时间 09:00
- URL：`https://api.github.com/repos/thunderwong46/gold-wechat-actions/actions/workflows/daily-gold-report.yml/dispatches`
- Method：`POST`
- Body：

```json
{"ref":"main","inputs":{"mode":"daily"}}
```

周报复盘任务：

- 时间：每周六北京时间 20:00
- URL：同上
- Method：`POST`
- Body：

```json
{"ref":"main","inputs":{"mode":"weekly"}}
```

两个任务使用同一组 Headers：

- `Authorization`：`Bearer 你的 GitHub Token`
- `Accept`：`application/vnd.github+json`
- `Content-Type`：`application/json`
- `X-GitHub-Api-Version`：`2022-11-28`

## 报告结构

- 先看结论：用大白话说明未来24小时更可能涨、跌，还是先别动。
- 小白操作卡：只告诉你在哪些价格附近可以考虑买，哪些情况直接观望。
- 市场检查：黄金价格、美元指数、美国10年期收益率。
- 判断原因：把“美元、利率、动能、新闻风险”翻译成容易理解的话。
- 关键价位：买入观察区、认错离场位置、卖出/减仓区。
- 三种执行情景：跌下来再买、涨上去再跟、直接观望。
- 风险控制：仓位、止损、重大数据前后的处理方式。
- 小白词典：解释买入观察区、卖出/减仓区、站稳、止损。

## 说明

- 定时由 cron-job.org 控制；GitHub Actions 只负责接到请求后生成报告、推送微信、保存归档。
- 日报和每周复盘都会同时发送到邮箱 `thunderwong46@gmail.com`。如果没有配置 SMTP 密钥，邮件会跳过，微信推送仍会继续。
- 报告会先读取最新金价，并在正文里显示“金价更新时间”。
- 最新金价优先级：Twelve Data 的 XAU/USD 5分钟级行情；其次是 Yahoo Finance 的黄金期货/现货行情。
- 如果金价不是 90 分钟内更新的数据，报告会自动降低信心，并提示先观望，不给进场建议。
- 报告还会尝试读取美元指数、美国10年期收益率和近24小时新闻。
- 数据源可能出现限流或短时不可用。脚本会在报告里标注缺失数据，并降低信心。
- 本报告是市场信息整理和情景推演，不构成个性化投资建议。

## 历史归档

每次工作流运行后，会自动保存两类文件：

- `reports/YYYY/YYYY-MM-DD.md`：当天推送给微信的完整报告，方便直接阅读。
- `data/YYYY/YYYY-MM-DD.json`：当天报告对应的行情快照、判断结论、关键价位、新闻线索，以及后续回填的24小时真实金价。
- `reviews/YYYY/YYYY-MM-DD.md`：每周六推送的复盘报告。
- `reviews/YYYY/YYYY-MM-DD.json`：每周复盘的结构化数据，方便以后继续统计。

日报每次运行时，会自动检查之前还没复盘的报告。如果报告已经发布约24小时，就抓取最新金价，写入 `outcome_24h`。周报会读取本周数据，比较预测方向和真实变化，统计判断准确率。
