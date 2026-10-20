# CIVconvert — Commercial Invoice PDF → CargoWise CSV

把客户的 Commercial Invoice（CIV）PDF 直接转换成 CargoWise 导入用的 CSV。
不再需要在线 PDF→Excel 工具，也不需要手工复制粘贴。

## 处理流程

1. **读取 PDF**：直接读 PDF 的文字层，按**表头文字**（Model / Description / HS Code …）定位明细表格。
   结果是确定的——不受在线转换工具、列位置变化的影响。
2. **填入 CargoWise 模板**（模板固定保存在 `config/templates/`，不需要每次上传）：

   | 模板列 | 表头 | 内容 |
   |---|---|---|
   | A | LNO | 1…N 连续编号（按实际行数） |
   | D | Product Code | PDF 的 Model（原样） |
   | E | Goods Description | PDF 的 Description，删除中文说明，只保留英文 |
   | F | Tariff | PDF 的 United States HS Code（原样） |

3. **自动校验**（不通过不会中断，只标记“需复核”并说明原因）：
   - 明细的数量、金额合计 = PDF 的 Subtotal 行（确认没有漏行、读错）
   - 每行 数量 × 单价 = 金额
   - HS Code 格式；去中文后描述是否为空/过短；输出是否残留非英文字符
4. **输出** `CargoWise Input - <PDF 文件名>.csv`
   （例：`8100800394 Commercial Invoice.pdf` → `CargoWise Input - 8100800394 Commercial Invoice.csv`）

## 本地使用（Windows）

| 方式 | 操作 |
|---|---|
| 网页 | 双击 `start-local.bat` → 浏览器打开 http://127.0.0.1:8000 → 拖入 PDF → 下载 CSV |
| 拖放 | 把 PDF 拖到 `convert.bat` 上 → CSV 生成在 PDF 同一文件夹 |
| 命令行 | `.venv\Scripts\python -m civconvert convert "8100800394 Commercial Invoice.pdf"` |

首次运行会自动创建 `.venv` 并安装依赖（需要 Python 3.10+）。

命令行选项：`--customer istone`（指定客户）、`--merge`（多个 PDF 合并为一张发票）、
`--out-dir 文件夹`、`--xlsx`（同时输出填好的 Excel 供核对）、`--json`。

其他命令：`python -m civconvert customers`（列出客户）、`python -m civconvert check-config`（检查配置）。

## 新增客户（不需要改代码）

每个客户一个 YAML 文件：`config/customers/<客户代码>.yaml`。

1. 复制 `config/customers/_example_new_customer.yaml`（里面有全部选项的说明），改名为 `<客户代码>.yaml`
2. 修改：
   - `detect` — 用 PDF 里的哪些文字识别这个客户（或发件人邮箱）
   - `table.columns` — 这个客户的表头叫什么（例如 `Part No` 而不是 `Model`）
   - `mapping` — 哪个字段填进模板哪一列，要不要去中文等
   - 客户要用不同的 CargoWise 模板：把模板 xlsx 和一个同名 yaml 放进 `config/templates/`，在客户配置里 `template:` 指向它
3. `python -m civconvert check-config` 检查，再用该客户的 PDF 测试
4. 提交到 GitHub → Northflank 自动重新部署

`mapping` 里的 `expect` 是保护：如果模板被替换、列被挪动，转换会直接报错，而不是悄悄填错列。

## 一张发票被拆成多个 PDF

- 网页：选择多个 PDF，勾选“这些 PDF 是同一张发票”
- 命令行：`--merge`；API：`"merge": true`
- 某个客户总是这样：在其配置里写 `output.multi_file: merge`，默认就合并

合并时：表格可以跨文件续接（后面的文件没有表头也可以），LNO 连续编号，Subtotal 校验针对合并后的整张发票，
各文件的发票号不一致会提示。

## API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/api/customers` | 已配置的客户 |
| POST | `/api/convert` | multipart 上传 `files`（可多个）、可选 `customer`、`merge`；默认直接返回 CSV 文件，`format=json` 返回 JSON |
| POST | `/api/convert/json` | JSON（base64）进出，给 Power Automate 用 |

设置环境变量 `API_KEY` 后，所有 `/api/*` 请求都需要请求头 `X-API-Key: <key>`（可设多个，用逗号分隔，方便换 key）。
接口文档：`/docs`。

`/api/convert/json` 请求示例：

```json
{
  "sender": "someone@istone.com",
  "customer": "istone",
  "merge": false,
  "files": [{ "name": "8100800394 Commercial Invoice.pdf", "contentBytes": "JVBERi0xLjQK..." }]
}
```

`customer`、`sender`、`merge` 都可省略。返回：

```json
{
  "status": "ok",
  "skipped": ["image001.png"],
  "results": [{
    "status": "ok",
    "filename": "CargoWise Input - 8100800394 Commercial Invoice.csv",
    "content_base64": "TE5PLEVudHJ5L0xpbmUgTm9zLi...",
    "line_count": 20,
    "checks": [{ "name": "Subtotal amount", "ok": true, "detail": "lines 48,561 vs subtotal 48,561" }],
    "warnings": []
  }]
}
```

`status`：`ok` = 全部校验通过；`review` = 已生成 CSV，但有需要人工确认的地方（见 `warnings` / `checks`）；
`partial` / `error` = 部分/全部文件失败（见各 result 的 `error`）。

## Power Automate Cloud 对接（后续）

1. 触发器 **When a new email arrives (V3)**（Office 365 Outlook）：Include Attachments = Yes，
   按需要设置 From / Subject 过滤
2. **HTTP** 动作（Premium）：
   - Method `POST`，URI `https://<Northflank 地址>/api/convert/json`
   - Headers：`X-API-Key` = 你的 key，`Content-Type` = `application/json`
   - Body：
     ```
     {
       "sender": "@{triggerOutputs()?['body/from']}",
       "files": @{triggerOutputs()?['body/attachments']}
     }
     ```
     附件数组可以原样传入；签名图片等非 PDF 附件会被自动跳过。
3. **Apply to each** `@{body('HTTP')?['results']}`：
   - **Create file**（OneDrive / SharePoint）：File name = `@{items('Apply_to_each')?['filename']}`，
     File content = `@{base64ToBinary(items('Apply_to_each')?['content_base64'])}`
   - 条件 `status` 等于 `review` 时，再发一封邮件把 `warnings` 发给操作人员

## 部署到 Northflank

1. 把代码推到 GitHub（`git push -u origin main`）
2. Northflank → **Create new → Service → Combined（Build and deploy a Git repo）**
   - Repository：`jun2369/CIVconvert`，Branch：`main`
   - Build options：**Dockerfile**（Build context `/`，Dockerfile `/Dockerfile`）
   - Networking：Port **8080**，Protocol HTTP，勾选 **Publicly expose**
   - Environment variables：`API_KEY` = 一串足够长的随机字符（建议放在 Secret group 里）
   - Resources：最小规格即可
   - Health checks（可选）：HTTP `GET /healthz`，端口 8080
3. 创建后会自动构建，完成后得到一个 `https://….code.run` 地址，打开即为网页版

之后每次 push 到 `main`，Northflank 自动重新构建部署。

客户配置也可以不放在镜像里：挂一个 Volume，把 `config/` 放进去，并设置
`CIVCONVERT_CONFIG_DIR=<挂载路径>`，这样改配置不需要重新构建。

## CSV 格式说明

在 `config/templates/cargowise_civ_lines.yaml` 中设置：

- `encoding: utf-8`（默认，无 BOM；内容为纯英文时与 Excel “CSV (逗号分隔)” 完全一致）。
  如 CargoWise 导入需要 BOM，改为 `utf-8-sig`
- 模板中部分表头含不可见控制字符（xlsx 中存为 `_x001F_`），默认 `header_control_chars: keep`，
  与 Excel“另存为 CSV”的结果一致；改为 `strip` 可去掉
- 不要用 Excel 打开 CSV 后再保存：Excel 可能改掉 HS Code 等数字格式（例如去掉开头的 0）

## 项目结构

```
civconvert/            代码
  extract.py             PDF 表格读取（pdfplumber）
  transforms.py          字段转换（strip_chinese 等）
  pipeline.py            主流程 + 校验
  api.py / static/       API 与网页
  cli.py                 命令行
config/
  customers/*.yaml       客户配置（识别规则、表头、映射、校验）
  templates/             CargoWise 模板（xlsx + yaml）
samples/               本地测试用的真实发票（不进 git）
tests/                 测试：.venv\Scripts\python -m pytest
```
