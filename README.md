# jianying-subtitle-fixer — 剪映字幕 AI 纠错

> 剪映语音识别快，但专有名词总识别错（Agent→"engine"、Claude Code→"克劳德扣"…），
> 每期视频手改字幕半小时。这个工具自动纠完再写回剪映草稿，你只管回去剪辑。

[![skill](https://img.shields.io/badge/Claude%20Code-Skill-blue)](./SKILL.md) [![license](https://img.shields.io/badge/license-MIT-green)](./LICENSE)

## 工作原理

```
剪映里识别字幕（快、免费、时间轴准）
        ↓ 完全退出剪映
本工具：jy-draftc 解密草稿 → 术语表规则 + glm-4-flash 双层纠错 → 回加密写回
        ↓
重开剪映：字幕已纠对 → 手动删减/加变速 → 导出
```

- **时间轴、样式、轨道零改动**，只改文本字段
- 每次运行自动备份，随时整体回滚
- 生成《修改对照表》（changes.md），每处改动可人工抽查

实测：29.5 分钟视频、598 条字幕，纠错 47 条（Agent 18 处、Claude Code 15 处…），约 4 分钟跑完。

## 双层纠错

| 层 | 机制 | 特点 |
|---|---|---|
| 规则层（主力） | `config/terms.json` 错→对映射 | 秒级、零成本、100% 可控，**越用越准** |
| LLM 层（兜底） | 智谱 glm-4-flash（免费）批量纠同音错字 | 30条/批 × 4路并发，写回前过术语校验+防幻觉检查 |

## 安装

### 作为 Claude Code Skill 使用（推荐）

```bash
git clone https://github.com/<你的用户名>/jianying-subtitle-fixer.git \
  ~/.claude/skills/jianying-subtitle-fixer
```

然后对 Claude 说「帮我纠一下 XX 草稿的字幕」即可。

### 直接当命令行工具使用

```bash
python scripts/fix_subtitles.py "草稿名" --dry-run   # 预览（不写回）
python scripts/fix_subtitles.py "草稿名"             # 正式写回
python scripts/fix_subtitles.py "草稿名" --no-llm    # 纯规则层，秒出
```

### 一次性准备（详见 references/setup.md）

1. **jy-draftc**：从 [wenshui330/jy-draftc](https://github.com/wenshui330/jy-draftc/releases)
   下载 Windows amd64 包，解压后把整个目录放进本项目的 `tools/`
   （用于剪映新版加密草稿的解密/回加密，调用剪映自带的 videoeditor.dll）
2. **剪映桌面版**：自动探测安装目录；探测不到时设环境变量
   `JY_INSTALL_DIR` 指向含 `videoeditor.dll` 的版本目录
3. **智谱 API key**（LLM 层可选）：环境变量 `ZHIPU_API_KEY`；
   如果你用智谱跑 Claude Code，会自动读取 `~/.claude/settings.json`，免配置

> 隐私说明：API key 仅在运行时从你的环境变量/本机配置读取，本项目代码与仓库中
> **不含任何密钥**；字幕数据不离开本机（仅纠错文本批次发送给智谱 API）。

## 术语表（核心资产）

`config/terms.json` 内置了一批 AI 领域常见错拼（克劳德扣→Claude Code、engine→Agent…）。
**每期视频跑完发现新错拼，加进 `fix` 映射，同系列视频下次基本零手改。**

```json
{
  "terms": ["Agent", "Claude Code", "MCP", "API"],
  "fix": {
    "克劳德扣": "Claude Code",
    "engine": "Agent"
  }
}
```

## 已知边界

- 依赖 [jy-draftc](https://github.com/wenshui330/jy-draftc) 对剪映版本的支持（当前 10.3~11.5），
  剪映大版本更新后请留意该项目发版
- 草稿里手动添加的标题文本也会一起被纠（对照表可见，一般无碍）
- 免费模型 glm-4-flash 偶有小幅越权改动，最终以对照表人工抽查为准

## 致谢

- [wenshui330/jy-draftc](https://github.com/wenshui330/jy-draftc) — 剪映草稿解密/回加密
- [GuanYixuan/pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft) — 剪映草稿格式的先行研究

## License

MIT
