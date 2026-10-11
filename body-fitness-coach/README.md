# Body Fitness Coach

本地优先的 AI 健身与饮食教练：记录三餐和训练，汇总小米运动健康与训记数据，生成日报、周报和提醒。聊天会话可以重置，历史数据仍由 Skill 自己恢复。

## 目录

```text
body-fitness-coach/
├─ .gitignore
├─ SKILL.md
├─ README.md
├─ scripts/
│  ├─ coach.py
│  └─ install.ps1
└─ .private/                 # 仅本机存在，整体被 Git 忽略
   ├─ private.db             # 历史记录和运行状态
   ├─ knowledge/
   │  ├─ BODY_KNOWLEDGE.md  # 可读的个人身体知识库
   │  └─ sources/           # 按内容哈希去重的原始报告、图片和附件
   └─ .venv/                 # Python 运行环境
```

不再使用独立的 `E:\Study\AI\Body` 目录。`coach.py` 默认从 Skill 根目录下的 `.private/private.db` 读取历史数据；只有明确使用自定义位置时才需要 `--data-dir` 或 `BODY_FITNESS_DATA_DIR`。

## 重置会话后启用

新会话中发送：

```text
请使用 body-fitness-coach，先运行 doctor，然后继续作为我的 AI 健身教练。
```

Skill 会先检查：

- 数据库是否指向当前 Skill 的 `.private/private.db`；
- 历史状态和写入记录是否存在；
- 小米、训记、飞书和模型分析是否可用。

确认后即可继续记录餐食、分析训练或生成报告，不需要重新导入历史数据。

## 文件职责

- `SKILL.md`：新会话的操作规则、授权边界和报告要求。
- `scripts/coach.py`：唯一业务入口，处理数据、餐食、训练、日报、周报、提醒、诊断和轻量自检。
- `scripts/install.ps1`：唯一安装维护入口，建立私有环境、注册 Skill 和 Windows 计划任务。
- `.gitignore`：排除整个 `.private/`、Python 缓存和字节码。

不维护重复启动脚本、独立测试框架或 `requirements.txt`。

## 安装与维护

默认安装：

```powershell
powershell -File .\scripts\install.ps1
```

预览变更：

```powershell
powershell -File .\scripts\install.ps1 -WhatIf
```

安装器默认使用当前 Skill 的 `.private/`，并注册：

- `BodyCoach-DailyReport`：登录后生成日报。
- `BodyCoach-WeeklyReport`：每周日 23:00 生成周报；关机错过后在下次登录时按周从旧到新补发，每张成功后保存进度。
- `BodyCoach-MealReminder`：每天 23:30 检查缺失餐食。

任务直接运行 `.private/.venv/Scripts/pythonw.exe`，不依赖聊天会话，也不弹 PowerShell 窗口。

## 常用命令

在 Skill 根目录执行：

```powershell
$python = '.\.private\.venv\Scripts\python.exe'
$coach = '.\scripts\coach.py'

& $python $coach doctor
& $python $coach self-check
& $python $coach daily --dry-run
& $python $coach weekly --dry-run
& $python $coach meal-reminder --dry-run
& $python $coach knowledge status
& $python $coach knowledge search --query '血脂'
```

其他命令可通过以下方式查看：

```powershell
& $python $coach --help
```

真实补发日报需要再次确认后使用 `daily --force`。训练模板修改必须先展示差异并单独确认。

## 个人身体知识库

体检报告、医院报告、体测、身体照片、补剂、器材条件等长期有用的信息记录在一个可读文档中；分类按实际内容动态产生，不预建空目录。原件存于 `sources/`，同一文件只保留一份；结构化指标、来源哈希和检索索引复用 `private.db`。

新增资料前先检索同类历史，写入时区分原始事实、历史对比、分析和不确定项。多页 PDF 可分 5–10 页处理，但必须记录总页数和已覆盖页，原 PDF 始终是最终依据。Markdown 只保留关键事实与结论，不保存全文 OCR，也不重复小米、训记的日常数据。

飞书文档是本地 Markdown 的单向镜像：本地写入优先，远端失败会保留待同步状态；原 PDF 和图片默认不上传。首次创建或绑定：

```powershell
& $python $coach knowledge sync --create
# 或绑定已有文档
& $python $coach knowledge sync --doc '<飞书文档链接>'
```

日报、周报和三餐提醒不会读取知识库；只有训练计划、补剂评估、报告解读、伤病适配等确实相关的任务才做定向检索。

## 报告规则

- 日报只保留必要数据和 1 至 2 句综合建议。
- 日报仅将已校验汇总交给模型；周报保留最近一周趋势数据。
- 身体变化仅在出现新测量时展示。
- 热身动作保留在训练模板或训记中，不重复塞入日报。
- 周报先在本地聚合 7 天活动、恢复、训练和营养事实，再把精简摘要交给模型分析。
- 训练滚动顺序只在完成独立力量训练后推进。
- 所有建议只作健身与生活方式参考，不替代医疗诊断。

## 隐私与发布

`private.db` 和 `knowledge/` 可包含聊天目标、身体指标、饮食、训练、报告原件、同步缓存和报告状态。访问令牌及密码保存在 Windows Credential Manager。

以下内容不得进入 Git：

- `.private/` 中的任何文件；
- 用户、群聊、消息等真实 ID；
- 身体、饮食和训练记录；
- 小米、训记或飞书凭据。

发布前至少运行：

```powershell
git check-ignore body-fitness-coach/.private/private.db
git status --short
```

## 验收标准

- `doctor` 指向 `.private/private.db`，并显示已有历史计数。
- `self-check`、日报预览、周报预览和餐食提醒预览通过。
- 两个计划任务使用 `.private/.venv/Scripts/pythonw.exe` 且最近运行结果正常。
- 删除聊天会话后，显式启用 Skill 能直接读取历史数据。
- Git 状态中不出现 `.private/` 或任何私人数据。
