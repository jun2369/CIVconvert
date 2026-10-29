# CIVconvert — Commercial Invoice PDF / Excel / 图片 → CargoWise CSV

把 Commercial Invoice（CIV，PDF、Excel 或图片 / 扫描件）直接转换成 CargoWise 导入用的 CSV。
不再需要在线 PDF→Excel 工具，也不需要手工复制粘贴；任何供应商、任何版式都可以，不需要先为它写配置。

## 处理流程

1. **读取 PDF、Excel 或图片**（`.pdf`、`.xlsx` / `.xlsm`、`.xls`；图片 `.png`、`.jpg` / `.jpeg`、`.tif` / `.tiff`（可多页）、`.bmp`、`.gif`、`.webp`、`.heic`（iPhone 照片））：按**表头文字**定位明细表格，结果是确定的。
   - **图片和扫描件**（没有文字层的 PDF 页）先用 **OCR** 识别文字（`civconvert/ocr.py`：RapidOCR / PP-OCR 中英文模型，在服务器本机运行，不上传到任何外部服务），同时从图片里找出表格线；之后和普通 PDF 完全一样读取。小图（截图）会先放大再识别；盖在表格线上的印章不影响（被盖住的线会接上）。OCR 可能认错字，所以这样读出的发票一定标记“需复核”（检查项 “OCR 识别”），请对照左边的图片核对后再导出
   - 合计行没有写 “Total”、只有数量 / 金额合计数的（很多中国供应商的发票这样），也认作合计行用于核对；数量合计写在别的列下面（例如 “Unit” 下）但正好等于合计时也算
   - 表头文字被拆成两行的（“Quantit” / “y”、“Specificati” / “on”）当作一个词；HS 编码写在品名里的（“Automotive Tempered Glass HS:7007.11.0010”）取出来作为税号，品名里去掉；表头和内容对不上的——“Description” 下面其实是产品编号、旁边 “Hs Code” 下面是品名（福耀玻璃 Fuyao 的发票）——按内容对应
   - 不是商业发票的单据（到货通知、HS-7 申报表、提单、空运单、装箱单、原产地证 …）：转换失败的原因直接写明“this is an Arrival Notice, not a Commercial Invoice”，而不是笼统的“没找到表格”。更多单据类型可以在 `config/generic.yaml` 的 `other_documents` 里加，不用改代码
   - 有客户配置的客户（目前 iStone）：按它的配置读，结果和以前完全一样
   - 其他任何供应商、任何版式：**通用读取**（`config/generic.yaml`）——按表头的常见写法找列：
     Model / Part No / Item# → 产品编号，Description → 品名，HS / HTS / Tariff → 税号，
     Qty / Unit Price / Amount 用于校验（中英文写法都认）。同一个文件里有几张表（装箱单、合同、发票）时，
     挑最像发票明细的那张（有 HS、单价、金额的优先，标题是 Commercial Invoice 的加分）。
     没有表格线的表按位置读：文字从表头左边开始、数字对齐表头右边；换行的描述会合并到同一行；
     表格可以跨页（下一页不重复表头也可以），以最后一个 Total 行做合计核对。
     Excel 里还认这些写法（如印度出口商常见的版式）：表头分两行写（"Quantity" 下面 "Pcs"、"Rate" 下面 "USD"）；
     一个产品的编号 / 品名 / HTS 是合并单元格、跨了几个尺码行时，每个尺码行是一行（各带自己的数量、单价、金额，
     编号 / 品名 / HTS 取合并单元格的）；表格中间重复的表头跳过；合计行可以在表格下面隔几行空行，
     "Total" 写在这一行任意一格（如 "Total Pcs :"）；发票号 / 日期写在标签下面一格
     （"Invoice no & date" 下面 "AI-083-26-27 Dt. 28.09.2026"）
   - 客户配置认出了客户、但表格版式变了：自动改用通用读取（并标记“需复核”），不会拒绝
2. **填入 CargoWise 模板**（模板固定保存在 `config/templates/`，不需要每次上传）：

   | 模板列 | 表头 | 内容 |
   |---|---|---|
   | A | LNO | 1…N 连续编号（按实际行数） |
   | D | Product Code | 发票的 Model / Part No / Item#（原样） |
   | E | Goods Description | 发票的 Description，删除中文说明，只保留英文 |
   | F | Tariff | 发票的 HS Code / HTS（原样；Excel 里被存成数字而丢掉的开头 0 会补回） |

3. **自动校验**（不通过不会中断，只标记“需复核”并说明原因）：
   - 明细的数量、金额合计 = PDF 的 Subtotal 行（确认没有漏行、读错）
   - 每行 数量 × 单价 = 金额
   - HS Code 格式；去中文后描述是否为空/过短；输出是否残留非英文字符
   - 同一个 Model 出现在多行时 HS Code 是否一致（`checks.consistent`）
4. **输出** `CargoWise Input - <文件名>.csv`
   （例：`8100800394 Commercial Invoice.pdf` → `CargoWise Input - 8100800394 Commercial Invoice.csv`）

## 本地使用（Windows）

| 方式 | 操作 |
|---|---|
| 网页 | 双击 `start-local.bat` → 浏览器打开 http://127.0.0.1:8000 → 用 Nimbus 账户登录 → 上传 PDF → 在线表格核对 / 修改 → 导出 CSV |
| 拖放 | 把 PDF 拖到 `convert.bat` 上 → CSV 生成在 PDF 同一文件夹 |
| 命令行 | `.venv\Scripts\python -m civconvert convert "8100800394 Commercial Invoice.pdf"` |

首次运行会自动创建 `.venv` 并安装依赖（需要 Python 3.10+）。

命令行选项：`--customer istone`（指定客户）、`--merge`（多个 PDF 合并为一张发票）、
`--out-dir 文件夹`、`--xlsx`（同时输出填好的 Excel 供核对）、`--json`。

其他命令：`python -m civconvert customers`（列出客户）、`python -m civconvert check-config`（检查配置）、
`python -m civconvert migrate`（建立 / 更新数据库表；网页服务启动时也会自动执行）。

本地网页的数据保存在 `data/civconvert.db`（SQLite，不进 git）。本地测试不想用 Nimbus 时，
可以 `set AUTH_DEV_LOGIN=1` 后再启动：登录页会多一个“开发环境登录”（只输邮箱）。**生产环境不要设置它。**

## 网页：文档、在线表格、状态

**登录**：用公司 Nimbus 账户（邮箱 + 密码）登录。与 HTS rule engine 相同：本系统把邮箱和密码转发给
`{NIMBUS_API_URL}/api/v2/userauth/auth/sign-in` 验证，**不保存密码**，也不保存 Nimbus token；
登录后是本系统自己的会话（HttpOnly cookie，默认 7 天不使用即过期，退出登录立即失效）。
同一邮箱 15 分钟内输错 5 次会暂时锁定（防止猜密码）。第一次登录时自动建立用户。

**不走 Nimbus 的账号**：有一个固定账号 `TEST001`（密码见 `civconvert/settings.py` 的 `LOCAL_LOGINS`），
直接由本系统验证，**不转发给 Nimbus**，用户名也不必是邮箱。给没有 Nimbus 账户的人试用用。
它和普通用户完全一样（只看得到自己上传的文档），同样有 15 分钟 5 次的限流，操作记录里标 `via: local`。
环境变量 `LOCAL_LOGINS`（`用户名:密码`，多个用逗号隔开）会**替换**这个账号；把它设成空值就一个都没有，只剩 Nimbus 登录。
注意本仓库是公开的，这个密码等于公开，所以它只是试用账号，别给它放要紧的东西。

**文档**：每次上传**一个**文件（PDF 或 Excel）= 一个文档（一张发票，包含它的全部明细行）。一次选多个文件会被拒绝（网页和服务器都会检查），
避免不同发票混在一起、分析出错。以前合并上传的多 PDF 文档照常可以打开、编辑和重新转换。
每个文档保存：原始 PDF、从 PDF 读出的数据、模板表格、发票号、创建人、创建 / 修改时间、状态、导出记录、操作记录和版本快照。

| 状态（英文界面） | 含义 |
|---|---|
| 处理中（Processing） | PDF 正在识别 / 转换（通常几秒） |
| 编辑中（Editing） | 转换完成，正在检查和修改表格；有校验问题时另标“需复核” |
| 已完成（Completed） | 点了 Confirm / 确认完成（会先弹窗确认）：信息完整，可以交付 CargoWise；表格变为只读，可“重新编辑” |
| 失败（Failed） | 识别或转换失败（例如没认出客户、扫描件）；可以选客户后“重新转换” |

**在线表格**（打开文档后）：列 = CargoWise 模板的全部列（210 列），**表头不能改，列不能删除或移动**，只能编辑数据：

- 单击选中、拖动或 Shift 多选、Ctrl 加选、点行号选整行、点列字母选整列、Ctrl+A 全选
- 直接打字（支持中文输入法）/ 双击 / F2 编辑；Enter、Tab、方向键移动；上方公式栏也可以编辑
- Ctrl+C / Ctrl+X / Ctrl+V：可以和 Excel 互相复制粘贴；表格内复制公式，引用会像 Excel 一样自动调整
- Delete 清空所选；工具栏或右键菜单：插入行、删除整行（LNO 自动重新编号，公式引用自动调整）
- Ctrl+Z / Ctrl+Y 撤销 / 重做；Ctrl+S 保存
- 公式：`=I2*J2`、`=G2+H2-K2`、`=SUM(G2:G9)`、`=ROUND(G2*H2,2)`、`=IF(F2="","缺 HS",F2)` 等，
  运算符 `+ - * / ^ % &` 和比较，支持
  SUM AVERAGE MIN MAX COUNT COUNTA PRODUCT ROUND ROUNDUP ROUNDDOWN INT ABS MOD POWER SQRT
  IF IFERROR AND OR NOT CONCAT CONCATENATE LEFT RIGHT MID LEN UPPER LOWER TRIM VALUE。
  公式和计算结果都会保存；导出 CSV 时写入计算结果。看起来是数字的文字参与计算：
  "50"、"1,600.00"，以及和 Excel 一样的 "$20"（= 20）、"-$1,234.50"、"20%"（= 0.2）
- 像 Excel 一样点选单元格：输入 `=`、运算符、`(` 或 `,` 之后，点一下单元格就填入它的地址；
  拖动 = 区域（`G2:G9`），Shift+点击 = 扩展成区域，紧接着再点别的格子 = 替换刚填入的地址；
  直接打字输入公式时方向键也可以点选（Shift+方向键扩展）。在上方 fx 公式栏输入时同样可以点选。
  正在输入的公式引用到的单元格会用彩色框标出
- 自动填充（像 Excel 的填充柄）：拖动选中区域右下角的小方块往下（或往右、往上）拉。两个以上同单位的数字接着递增并保留单位和格式
  （`$10`、`$20` → `$30`、`$40`…；`1,234.50`、`10%`、`12.5 kg` 同样）；只选一个数字 = 复制；结尾带数字的文字加 1
  （`Item 1` → `Item 2`，`INV-007` → `INV-008`）；公式的引用自动调整（`=I2*J2` → `=I3*J3`）；其他文字复制。
  拖的时候按住 Ctrl 在“复制 / 递增”之间切换；双击小方块 = 填到最后一行；撤销一次整体撤回；不会新增行
- 问题标记：红色 = 错误（必填列为空、公式错误、PDF 里数量×单价≠金额），黄色 = 警告（HS Code 格式、
  同一 Model 的 HS Code 不一致）；工具栏 ‹ › 逐个跳转；单元格右上角蓝色三角 = 与转换结果不同（被修改过）
- 底部状态栏：所选单元格的求和 / 平均、当前行在 PDF 中的原始内容

**保存**：修改后约 1.5 秒自动保存，也可以点 Save；右上角显示最近保存时间。每次修改同时暂存在浏览器本机，
断网时显示“网络中断”并自动重试；关闭页面前会先保存；意外关闭 / 崩溃后再次打开会提示“恢复这些修改”。
两个人（或两个窗口）同时编辑同一文档时，不会互相覆盖：后保存的一方会看到提示，选择重新加载或覆盖。
自动保存每 10 分钟、每次手动保存和 Confirm 时会留一个版本快照；“历史”里能看到所有操作记录、版本和导出记录。

**导出**：编辑过程中随时可以“导出 CSV”（导出前会先保存，内容 = 表格当前数据）；Confirm 后导出的标记为最终版。
CSV 的表头、列顺序、编码和换行与以前完全相同（未修改过的文档导出的 CSV 与直接转换逐字节一致）。

**工作面板**（首页）：文档总数、编辑中、已完成、失败或需人工检查；
文档列表可按状态（编辑中 / 已完成）、创建日期筛选，按发票号 / 文档名搜索，直接打开、预览、导出 CSV、Confirm。

**只能看到自己的文档**：每个人的工作面板、统计和搜索只包含自己上传的文档，没有“全部文档”；
别人的文档即使拿到链接也打不开（接口返回 404“文档不存在”，导出、原始 PDF、历史同样如此）。

**保存期限**：文档在**上传 4 个月后自动删除**（原始 PDF、表格、版本快照、导出记录和操作记录一起删除），为数据库腾出空间。
服务启动时和之后每 6 小时检查一次；文档页顶部显示“将于 … 自动删除”的日期。
期限用环境变量 `DOCUMENT_RETENTION_MONTHS` 设置（默认 4，`0` = 不自动删除）。

**语言和日夜间模式**：页面默认英文；右上角 **中文 / EN** 切换语言，**☾ / ☀** 切换夜间 / 白天模式，
选择保存在本机浏览器（没选过时跟随电脑的深色 / 浅色设置）。在表格页切换语言前会先保存未保存的修改。
服务器返回的错误和校验问题带有 `code` / `params`，由网页按当前语言显示（例如 `{"code": "not_pdf", "params": {"name": "x.pdf"}}`）；
转换失败时的技术原因（例如“PDF 没有文字层”）仍显示服务器的英文原文。

## 导出前核对：Preview 对比预览

在文档的表格页点工具栏的 **Preview**（中文界面：对比预览；或在工作面板的列表里点 Preview / 预览），右侧打开对比界面：

- **左边 原始 Invoice**：PDF 页面图片，默认自动裁出明细表格（表头、明细行、Subtotal），可切换“整页”；
  多页发票用 ‹ › 翻页。左侧编号 = 第几行（= LNO），Σ = Subtotal 行（合计校验通过为绿色，不通过为红色）
- **右边 转换结果**：把导出的 Excel 读回来显示（列字母、表头、列宽、字体与导出文件一致），
  重点是目标列 D / E / F；被转换过的单元格（例如去掉了中文）右上角有蓝色小三角，悬停可看 PDF 原文
- 点任意一行：两边同一行同时高亮并对齐到同一高度；悬停单元格时，另一侧对应单元格也会高亮；
  底部显示这一行的 PDF 原文、导出结果和校验结果
- 问题标记：红色 = 错误（空值、数量×单价≠金额、去中文后为空…），黄色 = 警告（HS Code 格式、同一 Model 的 HS Code 不一致…）；
  顶部“上一个 / 下一个”逐个跳到有问题的行
- 缩放（− / + / Ctrl+滚轮）、按住拖动、适应宽度、适应窗口、1:1 恢复默认比例、全屏；“同步滚动”打开时两边跟着同一行滚动；
  窗口较窄时自动改为上下排列（也可以手动切换）
- 键盘：↑ ↓ 逐行，+ / − / 0 缩放，F 全屏，Esc 关闭；双击右侧单元格复制内容

左边是保存的原始 PDF（Excel 发票则画出读取的那张工作表，带 Excel 的列字母和行号），
右边是表格**当前**的数据（删除 / 插入行之后也能对上原来的行）。
图片和扫描件的预览：左边显示原图，表格行的位置来自 OCR。

## 通用读取认不出某种表头时（不需要改代码）

在 `config/generic.yaml` 里对应字段下加一条写法（正则，不区分大小写），例如某供应商把品名列叫 `Goods Name`：
在 `description:` 下加 `'goods\s*name'`。`not:` 里是“不算该字段”的写法（例如 `HTS Description` 不是品名）。
改完提交到 GitHub，部署后生效。

## 新增客户配置（可选，不需要改代码）

一般不需要：没有配置的供应商由通用读取处理。只有某个客户要特殊处理（不同的模板、特别的表头、
固定的校验）时，才给它加一个 YAML 文件：`config/customers/<客户代码>.yaml`。

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

- 网页：**每次只能上传一个文件**（PDF 或 Excel，避免不同发票混在一起），所以网页上不能合并；需要合并时用命令行或 API
- 命令行：`--merge`；API：`"merge": true`
- 某个客户总是这样：在其配置里写 `output.multi_file: merge`，默认就合并

合并时：表格可以跨文件续接（后面的文件没有表头也可以），LNO 连续编号，Subtotal 校验针对合并后的整张发票，
各文件的发票号不一致会提示。

## API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/api/customers` | 已配置的客户 |
| POST | `/api/convert` | multipart 上传 `files`（PDF 或 Excel，可多个）、可选 `customer`、`merge`；默认直接返回 CSV 文件，`format=json` 返回 JSON |
| POST | `/api/convert/json` | JSON（base64）进出，给 Power Automate 用；PDF 和 Excel 附件都处理，其他附件跳过 |
| POST | `/api/preview` | multipart 上传**一张发票**的 PDF（可多个）、可选 `customer`；返回转换结果 + 页面图片、每行在页面上的位置、导出 Excel 的目标列 |
| POST | `/api/intake/email?owner=<OP 邮箱>` | Power Automate Cloud 发来的邮件（`X-API-Key`）：挑出 CIV 附件，生成文档（见下面“Power Automate Cloud 自动接收邮件”） |
| GET | `/api/intake/today` | 网页顶部的“今天接收了 N 封邮件……”（已登录用户自己的） |
| | `/api/auth/*` | 登录（`login`）、退出（`logout`）、当前用户（`me`） |
| | `/api/documents/*` | 文档：上传、列表 / 筛选、统计、列表里改发票号 / 客户名称、表格保存、导出、Confirm、重新编辑、重新转换、原始 PDF、预览、操作记录、版本（见 `civconvert/documents_api.py`） |

转换结果 JSON 里的 `flags` 是按行、按单元格定位的问题（`line`、`level`、`fields` = PDF 字段、`columns` = 模板列字母）。
`flags`、`checks` 和错误回应都有英文 `message` / `detail` / `error`，另外带 `code` + `params`（网页用来显示中文 / 英文）。

转换接口（`/api/convert*`、`/api/preview`、`/api/customers`）需要**已登录**，或请求头 `X-API-Key: <key>`
（环境变量 `API_KEY`，可设多个，用逗号分隔，方便换 key）。不再允许匿名调用。
文档接口只接受已登录的用户；会修改数据的请求还必须带请求头 `X-CIV-Request: 1`（防跨站请求伪造，网页会自动带上）。
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

## Power Automate Cloud 自动接收邮件

Power Automate 收到符合条件的邮件后，把整封邮件（含附件）发给 CIV Converter。系统挑出其中的 CIV 附件，
每个 CIV 生成一个文档，在后台转换，和手动上传完全一样，出现在指定 OP 的文档列表里（来源 = **自动 / Auto**）。
手动拖拽上传照常可用（来源 = **手动 / Manual**）；自动接收出问题时，OP 仍可手动上传。

1. 触发器 **When a new email arrives (V3)**（Office 365 Outlook）：Folder、From 等按需要设置；
   **Include Attachments = Yes**，Only with Attachments = Yes
2. **HTTP** 动作（Premium）：
   - Method `POST`
   - URI `https://<Northflank 地址>/api/intake/email?owner=<OP 的邮箱>`：文档放进这个 OP 的列表
     （此人需要先用 Nimbus 账号登录过一次 CIV Converter）
   - Headers：`X-API-Key` = 服务器环境变量 `API_KEY` 里的一个 key；`Content-Type` = `application/json`
   - Body：表达式 `triggerBody()`，整封邮件原样发送（Outlook 各版本触发器的字段名都认）
3. 返回 JSON：`status` = `ok`（生成了文档）/ `no_civ`（没有找到 CIV：已记录，网页顶部会提示）/
   `duplicate`（同一封邮件之前收过，例如 Power Automate 重试：不会重复生成）；`attachments` 是每个附件的结果和原因。
   出错时（key 不对、owner 不存在、没带附件内容……）返回 4xx 和 `error` 说明，Power Automate 的运行记录里能看到

**哪个附件是 CIV**（`civconvert/intake.py`）：只看 PDF / Excel（图片附件仍然跳过：邮件里的图片多是 logo、签名；扫描件 PDF 会用 OCR 读前 3 页来判断）；再按内容判断：
有 “Commercial Invoice”、价格 / 金额、HS 编码的算；装箱单、提单、AWB、Proforma 等不算；
邮件里只有一个 PDF / Excel 且有发票迹象的也算；同一张发票同时附了 PDF 和 Excel 只转换一次（用 PDF）。

**网页**：列表多了“来源”列；收到过自动邮件的用户，顶部显示“今天通过 Power Automate Cloud 接收了 N 封邮件、M 个附件……”，
点“查看”能看到每封邮件、每个附件的处理结果。每封邮件都记录在数据库（`intake_messages`），和文档一样保留 4 个月。

`/api/convert/json` 仍可用：直接返回 CSV，不生成文档。

## 部署到 Northflank

1. 把代码推到 GitHub（`git push -u origin main`）
2. **数据库**：Northflank 项目 → **Addons → Create new addon → PostgreSQL**（最小规格即可，建议打开备份）。
   创建后在 addon 的 **Connection details** 里找到连接字符串（`postgresql://…`）
3. Northflank → **Create new → Service → Combined（Build and deploy a Git repo）**
   - Repository：`jun2369/CIVconvert`，Branch：`main`
   - Build options：**Dockerfile**（Build context `/`，Dockerfile `/Dockerfile`）
   - Networking：Port **8080**，Protocol HTTP，勾选 **Publicly expose**
   - Environment variables（建议放在 Secret group 里；名字和说明见 `.env.example`）：
     - `DATABASE_URL` = 上面的 PostgreSQL 连接字符串（也可以在 addon 里用 “Link to service / secret group” 自动注入）
     - `NIMBUS_API_URL` = `https://nimbusgroup.us`
     - `API_KEY` = 一串足够长的随机字符（Power Automate 等程序调用转换接口用）
     - `LOCAL_LOGINS` =（可选）`用户名:密码`，换掉代码里固定的 `TEST001` 试用账号；设成空值则关闭它
   - Resources：nf-compute-20（0.2 vCPU / 512 MB）起
   - Health checks（可选）：HTTP `GET /healthz`，端口 8080
4. 创建后会自动构建，完成后得到一个 `https://….code.run` 地址，打开即为网页版（先用 Nimbus 账户登录）

之后每次 push 到 `main`，Northflank 自动重新构建部署；数据库表结构的变更在服务启动时自动执行（Alembic）。
Docker 镜像里设置了 `REQUIRE_DATABASE_URL=1`：没有设置 `DATABASE_URL` 时服务会拒绝启动，
避免把数据写进容器自己的磁盘（重新部署就会丢失）。

## 迁移到其他平台

所有与平台有关的东西都是环境变量，代码里没有 Northflank 专有的部分：

- **应用**：标准 Docker 镜像（端口 `PORT`，健康检查 `/healthz`），可以部署到任何能跑容器的平台
  （Azure Container Apps、AWS ECS / App Runner、Google Cloud Run、Kubernetes、自己的服务器 …）。
  应用本身不保存状态（登录会话也在数据库里），可以同时运行多个实例
- **数据库**：通过 SQLAlchemy 访问，只用了通用的字段类型，PostgreSQL / MySQL / SQLite 都可以；
  表结构由 `civconvert/migrations/`（Alembic）管理。原始 PDF 默认也存在数据库里，
  所以搬家 = 用 `pg_dump` 导出、在新平台 `pg_restore`，再把新的 `DATABASE_URL` 配给新服务
- **原始 PDF** 也可以改存到磁盘（`FILE_STORAGE=local` + `LOCAL_FILE_DIR`，例如挂载的 Volume）；
  每个文件都记录了自己存在哪里，切换后旧文件仍然能读。将来要放对象存储（S3 等），只需在 `civconvert/storage.py` 加一种
- **后台任务**（PDF 转换）目前在服务进程内的线程里运行；`civconvert/jobs.py` 只有一个 `submit()` 接口，
  需要时可以换成真正的任务队列
- 有的平台在发布时单独执行数据库迁移：用 `python -m civconvert migrate`

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
  extract.py             PDF 表格读取（pdfplumber，按客户配置）
  generic.py             通用读取：任何供应商的 PDF / Excel / 图片，自动找明细表
  ocr.py                 图片和扫描件：OCR 识别文字、找表格线，交给 PDF 读取
  intake.py              Power Automate Cloud 发来的邮件：挑出 CIV 附件，生成“自动”文档
  sources.py             文件类型（PDF / xlsx / xls）、读 Excel
  transforms.py          字段转换（strip_chinese 等）
  pipeline.py            主流程 + 校验
  preview.py             Preview 对比预览的数据（页面图片、行位置、读回导出的 Excel）
  auth.py                Nimbus 登录、LOCAL_LOGINS 账号、会话、登录限流、API key
  documents.py           文档：上传、转换、保存、导出、Confirm、统计、操作记录
  documents_api.py       文档的 HTTP 接口
  sheet.py               在线表格的数据（来自转换结果、校验、导出）
  formula.py             公式引擎（服务器端，保存和导出时计算）
  db.py / models.py      数据库连接、表结构；migrations/ = 表结构变更（Alembic）
  storage.py / jobs.py   原始 PDF 存储、后台任务
  api.py                 HTTP 服务与页面
  static/                网页：login.html、index.html + dashboard.js（工作面板）、
                         editor.html + editor.js + sheet.js + formula.js（在线表格）、preview.js（对比预览）
  cli.py                 命令行
config/
  generic.yaml           通用读取：各字段表头的常见写法、挑表规则、发票号 / 日期
  customers/*.yaml       客户配置（识别规则、表头、映射、校验）
  templates/             CargoWise 模板（xlsx + yaml）
samples/               本地测试用的真实发票（不进 git）
tests/                 测试：.venv\Scripts\python -m pytest
                         （formula_cases.json 同时检查 Python 和浏览器两套公式引擎；
                          设置 TEST_DATABASE_URL 可以对 PostgreSQL 跑数据库测试）
.env.example           所有环境变量的名字和说明（不含任何密钥）
```
