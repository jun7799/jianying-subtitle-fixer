---
name: jianying-subtitle-fixer
description: 剪映字幕AI纠错流水线。当用户说"纠字幕"、"字幕纠错"、"改字幕"、"修正字幕"、"字幕识别错了"、"处理字幕"，或提到录完视频要在剪映里改专有名词字幕（如 Agent、Claude Code、MCP 识别错）时使用。流程：剪映智能字幕识别 → 本工具解密草稿 → 术语表规则+智谱glm-4-flash双层纠错 → 回加密写回 → 用户回剪映手动剪辑。
---

# 剪映字幕 AI 纠错

## 解决什么问题

剪映的语音识别快且免费，但专有名词（Agent、Claude Code、MCP、WorkBuddy…）总识别错，
每期视频手改字幕要花半小时。本工具自动完成：**解密草稿 → 双层纠错 → 回加密写回**，
时间轴和样式零改动，改完回剪映直接开始剪辑。

## 前置条件（一次性，详见 references/setup.md）

1. `tools/` 下放好 `jy-draftc.exe`（从 GitHub wenshui330/jy-draftc 下载）
2. 本机装有剪映桌面版（自动探测，或设 `JY_INSTALL_DIR` 指向含 videoeditor.dll 的版本目录）
3. 智谱 API key（环境变量 `ZHIPU_API_KEY`；用智谱跑 Claude Code 的可自动读取 settings.json）

## 标准工作流

```
1. 用户在剪映里：新建草稿 → 放视频 → 「文本→智能字幕」识别完
2. 完全退出剪映（含托盘）——脚本会检测，剪映开着拒绝写回
3. 跑脚本（dry-run 先看效果）：
   python scripts/fix_subtitles.py "草稿名" --dry-run
4. 让用户看 workspace/<草稿名>/changes.md 对照表，确认改动
5. 正式写回（去掉 --dry-run）
6. 用户重开剪映 → 字幕已纠好 → 手动删减/加变速 → 导出
```

## 执行命令模板

```powershell
& "<skill目录>/scripts/fix_subtitles.py" "草稿名" --dry-run   # 预览
& "<skill目录>/scripts/fix_subtitles.py" "草稿名"             # 正式写回
& "<skill目录>/scripts/fix_subtitles.py" "草稿名" --no-llm    # 纯规则，秒出
```

## 参数

| 参数 | 作用 |
|---|---|
| `--dry-run` | 只出对照表+SRT 不写回（剪映开着也能跑） |
| `--no-llm` | 只跑术语表规则替换，不调 API |
| `--batch N` | LLM 每批条数，默认 30 |

## 术语表维护（核心！越用越准）

`config/terms.json`：
- `terms`：权威写法（写回前强制校验大小写，如 api→API）
- `fix`：确定无疑的"错→对"映射（如 `克劳德扣→Claude Code`、`engine→Agent`）

**每次跑完发现新错拼，主动询问用户是否沉淀进术语表。**
用户是内容创作者，同一系列的视频术语高度重复，术语表是最有效的资产。
注意：加长短语映射（如"手抽injection→手搓Agent"）可能被已有的短映射（"手抽→手搓"）
先命中而失效——优先加目标词本身的映射（如"injection→Agent"）。

## 双层纠错机制

- **规则层**（主力）：术语表精确替换，秒级、零成本、100%可控
- **LLM 层**（兜底）：glm-4-flash 批量纠同音错字（30条/批×4路并发），
  写回前过术语校验 + 长度突变检测（防幻觉重写）

## 安全机制

- 剪映运行中拒绝写回；每次运行自动备份到 `workspace/<草稿名>/backups/`
- 只改文本字段，时间轴/样式/轨道不动；回滚 = 用备份覆盖 `draft_content.json`

## 已知边界

- 依赖 jy-draftc 对剪映版本的支持（当前支持 10.3~11.5，剪映大版本更新后留意该项目）
- 草稿里手动加的标题文本也会一起被纠（对照表可见，一般无碍）
- glm-4-flash 偶尔会小幅越权（删语气词/补字），prompt 已加"宁可漏改不可错改"约束，
  最终以对照表人工抽查为准
