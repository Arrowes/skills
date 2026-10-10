# Body Fitness Coach

一个面向个人长期使用的 AI 健身与饮食教练：记录三餐和训练，汇总小米运动健康与训记数据，生成日报、周报和当天建议，并在清空聊天会话后继续工作。

> 当前状态：实现、迁移和本机验证均已完成。公共目录保持四个文件，私有 `Body` 目录只保留 `.venv` 与 `private.db`。

## 目标

- 清空 Codex/飞书聊天会话后，仍能从本地数据恢复工作。
- 日报和周报同时采用专业教练与营养师视角，关注训练效果、饮食和恢复。
- 训练模板只作为选择参考；日报分析上一日状态并给出今日建议，周报再按实际表现更新模板。
- 支持三餐记录、训练记录、小米身体数据、训记数据、日报、周报和提醒。
- 公共代码可安全上传 GitHub；个人身份、身体、饮食和训练数据只留在本机。
- 尽量减少文件、脚本入口和依赖维护成本。

## 非目标

- 不替代医生、康复治疗师或注册营养师的诊断和治疗。
- 不把训练安排固定成必须完成的日历；加班、疲劳或疼痛时允许顺延或休息。
- 不在公共仓库保存用户 ID、群聊 ID、身体指标、饮食记录、训练记录或访问凭据。
- 不维护独立测试框架、重复启动脚本、`requirements.txt` 或多份配置文件。

## 最终目录

公共仓库：

```text
E:\Study\AI\skills\body-fitness-coach\
├─ SKILL.md
├─ README.md
└─ scripts\
   ├─ coach.py
   └─ install.ps1
```

本机私有运行目录：

```text
E:\Study\AI\Body\
├─ private.db
└─ .venv\
```

公共代码与私有数据彻底分开。迁移完成后，`Body` 目录不再承担源码仓库职责。

## 文件职责

### `SKILL.md`

为新会话提供最短但足够的操作说明，使 Codex 能在没有旧聊天记录时：

- 识别餐食文字或照片，调用统一命令记录和评价饮食；
- 查询身体、睡眠、活动、训练和营养数据；
- 手动补发日报或周报；
- 按实际恢复情况建议训练、顺延或休息；
- 知道训记是训练计划和营养目标的重要来源；
- 在任何外部写入、模板覆盖或破坏性操作前再次确认权限与范围。

它只保存工作方式，不保存个人资料或长期状态。

### `scripts/coach.py`

唯一的 Python 业务入口，合并现有主程序和零散辅助脚本，负责：

- 三餐记录、照片解析结果落库和营养评价；
- 小米运动健康数据同步；
- 训记密钥导入、训练数据同步和模板管理；
- 日报、周报、餐食提醒和手动补发；
- 教练与营养师联合建议；
- 小米二维码登录和凭据初始化；
- 数据迁移、诊断、轻量自检和幂等保护。

计划保留的命令：

```text
coach.py doctor
coach.py daily
coach.py weekly
coach.py meal-reminder
coach.py food ...
coach.py plan-preview
coach.py set-secret ...
coach.py login-xiaomi
coach.py import-xunji-keys
coach.py migrate ...
coach.py self-check
```

所有命令接受统一的 `--data-dir`，也可读取 `BODY_FITNESS_DATA_DIR`。本机计划任务固定传入 `E:\Study\AI\Body`，公共脚本不硬编码私人路径。

### `scripts/install.ps1`

唯一的 PowerShell 安装和维护入口，负责：

- 创建或更新 `Body\.venv`；
- 安装直接依赖 `mi-fitness-mcp[api]` 及其传递依赖；
- 把 `config.json` 与旧 `state.db` 安全迁移为 `private.db`；
- 将本技能注册到 Codex 的技能目录；
- 注册或更新 Windows 计划任务；
- 运行迁移后的诊断和最小自检；
- 在验证失败时停止清理并保留旧文件。

公开脚本支持 `-DataDir` 参数。当前电脑安装时使用：

```powershell
.\scripts\install.ps1 -DataDir 'E:\Study\AI\Body'
```

## 为什么只保留两个脚本

现有功能不需要多层启动器：

- `coach.py` 吸收 `xiaomi_qr_login.py` 和 `import_xunji_keys.py`；
- Windows 计划任务直接调用 `.venv\Scripts\pythonw.exe coach.py <command>`；
- 删除 `run.ps1`、`run-daily.ps1` 和 `run-meal-reminder.ps1` 的重复环境设置；
- 不发布 `test_coach.py`，改为 `coach.py self-check`；
- 不维护 `requirements.txt`，安装脚本只声明直接依赖；
- 不创建额外配置模板、示例目录或 UI 元数据，除非以后出现明确需求。

这是 Ponytail 原则下的最小闭环：一个业务程序，一个安装程序，一个技能入口，一个说明文档。

## 私有数据

`private.db` 是项目目录内唯一的个人数据文件，存放：

- 飞书聊天目标与本机运行配置；
- 身体档案及健康偏好；
- 饮食、训练、身体指标和同步缓存；
- 日报与周报状态；
- 训练模板版本和提醒状态。

访问令牌、密码等秘密继续存放在 Windows Credential Manager，不写入数据库和 Git。公共仓库不得包含真实 ID、真实身体指标、餐食记录、训练记录或凭据。

## 运行流程

### 记录餐食

飞书中的餐食文字或照片由技能转换为统一的 `food` 命令。程序写入 `private.db`，立即返回营养评价；评价会结合当天总摄入、训练安排和恢复需要，而不是孤立评价单餐。

### 生成日报

日报汇总前一日饮食、训练、睡眠、活动和身体数据，并给出简短建议：

- 最新身体概况及相较上次测量的变化；
- 昨日训练与恢复分析；
- 今日训练选择、顺延或休息建议；
- 今日最重要的一项训练、饮食或恢复提醒。

身体变化仅在检测到新测量时呈现，同一条测量不重复展示。教练建议先综合可用的身体、睡眠、活动、训练和饮食数据，再用 1 至 2 句给出结论，不机械重复上方数据；不设置身心健康项目。热身动作也不在日报中重复展示，完整热身保留在训练模板或训记中。

### 生成周报

周报按恢复、训练、营养三个方面详细分析一周趋势，说明主要变化、下周行动和依据，并决定是否建议更新下周参考模板。模板更新基于实际完成情况，不因单日缺席就不断重排整周。

### 定时任务

计划保留两个 Windows 任务：

- `BodyCoach-DailyReport`：用户登录后延迟运行；数据库保证同一天只发送一次。周一的日报流程内顺带生成周报。
- `BodyCoach-MealReminder`：每天 23:30 检查当日记录，只在缺少必要餐食记录时提醒。

任务直接调用 `pythonw.exe`，不弹 PowerShell 窗口，也不依赖聊天会话持续存在。

## 清空会话后的行为

清空当前会话不会删除计划任务、`private.db`、Windows Credential Manager 中的凭据或公共技能。新会话重新加载 `SKILL.md` 后即可继续记录三餐、查询数据和补发报告。

只有以下情况会中断自动任务：

- 本机计划任务被删除或禁用；
- `Body` 目录、虚拟环境或 `private.db` 被移动或删除；
- 外部服务授权过期；
- 飞书 bridge 未运行或当前 profile 不可用。

## 迁移方案

迁移必须保持可回退，顺序如下：

1. 只读检查当前 `Body` 仓库、计划任务、数据库和凭据状态。
2. 备份 `config.json`、旧数据库和计划任务定义。
3. 在事务中创建 `private.db` 并导入旧数据；不删除来源。
4. 安装新技能、虚拟环境和计划任务。
5. 运行 `self-check`、`doctor`、日报预览和提醒预览。
6. 验证新数据写入、报告发送、任务退出码和次日幂等行为。
7. 将旧源码仓库打包为本地恢复副本。
8. 取得单独确认后，才把 `Body` 清理为 `.venv` 与 `private.db`。

任何一步失败，都停止后续清理并保留旧系统继续运行。

## 验收标准

- 公共目录只有 `SKILL.md`、`README.md`、`scripts/coach.py` 和 `scripts/install.ps1`。
- `git grep` 和历史新增提交中不存在私人 ID、身体数据、饮食训练记录或凭据。
- `Body` 运行目录最终只保留 `.venv` 与 `private.db`。
- `self-check` 与 `doctor` 通过。
- 手动运行日报、周报和餐食提醒均能生成预期结果。
- 两个计划任务使用隐藏进程运行，并返回成功退出码。
- 清空会话后，新会话可依靠技能与数据库继续工作。
- 旧系统备份可恢复，迁移前数据未丢失。

## 已验证命令

以下命令用于安装后验收；`--dry-run` 只读取并本地渲染，不发送飞书消息、不写训记，也不推进模板状态：

```powershell
$python = 'E:\Study\AI\Body\.venv\Scripts\python.exe'
$coach = 'E:\Study\AI\skills\body-fitness-coach\scripts\coach.py'

& $python $coach --data-dir 'E:\Study\AI\Body' self-check
& $python $coach --data-dir 'E:\Study\AI\Body' doctor
& $python $coach --data-dir 'E:\Study\AI\Body' daily --dry-run
& $python $coach --data-dir 'E:\Study\AI\Body' weekly --dry-run
& $python $coach --data-dir 'E:\Study\AI\Body' meal-reminder --dry-run
Get-ScheduledTask -TaskName 'BodyCoach-DailyReport','BodyCoach-MealReminder'
```

真实补发日报需单独确认后运行 `daily --force`。模板修改仍需展示差异并再次确认。

## 回退

安装器会在迁移前把旧 `Body` 内容归档到同级的 `Body-legacy-*.zip`，并排除可重建的 `.venv`。若新版本失败：

1. 禁用新计划任务；
2. 恢复旧任务定义与旧目录；
3. 使用备份的 `config.json` 和旧数据库重新启动；
4. 保留失败后的 `private.db` 供人工比对，不直接覆盖旧数据。

## 维护与发布边界

安装、迁移、真实日报和旧文件清理已经分别验证。上传 GitHub、覆盖训记模板或执行新的外部写入仍需针对当次目标明确授权，不能由既往批准自动推定。
