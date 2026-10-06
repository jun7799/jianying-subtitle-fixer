# 一次性安装配置

## 1. 下载 jy-draftc

新版剪映（7+）的草稿文件是加密的，需要 jy-draftc 做解密/回加密
（它调用剪映自带的 `videoeditor.dll`，无自有解密实现）。

1. 打开 https://github.com/wenshui330/jy-draftc/releases
2. 下载 `jy-draftc-amd64-windows.zip`（校验同页 .sha256）
3. 解压后把整个 `jy-draftc-amd64-windows` 目录放到本 skill 的 `tools/` 下：

```
tools/
└── jy-draftc-amd64-windows/
    ├── jy-draftc.exe
    └── ...
```

> 支持剪映 10.3 ~ 11.5（以该项目 README 为准）。剪映大版本更新后若解密失败，
> 去该项目看是否有新版。

## 2. 剪映安装目录

脚本自动探测以下位置的 `videoeditor.dll`：

- `C:\Program Files\JianyingPro\*`
- `D:\软件\JianyingPro\*`、`D:\Apps\JianyingPro\*`、`E:\...`（同理）
- 环境变量 `JY_INSTALL_DIR` 指定的目录（优先）

自定义安装位置探测不到时：

```powershell
[Environment]::SetEnvironmentVariable("JY_INSTALL_DIR", "你的剪映版本目录", "User")
```

## 3. 草稿目录

默认 `%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft`，
非标准位置用环境变量 `JY_DRAFT_ROOT` 指定。

## 4. 智谱 API key（可选，仅 LLM 层需要）

- 方式一：环境变量 `ZHIPU_API_KEY`（[智谱开放平台](https://open.bigmodel.cn/)申请，glm-4-flash 免费）
- 方式二：什么都不做——如果你用智谱跑 Claude Code
  （`~/.claude/settings.json` 配了 `ANTHROPIC_BASE_URL` 指向 bigmodel），
  脚本会自动读取已有的 `ANTHROPIC_AUTH_TOKEN`，免配置

没有 key 时 LLM 层自动跳过，只跑术语表规则层（`--no-llm` 同效）。

## 5. 验证安装

```powershell
python scripts/fix_subtitles.py 任意已识别字幕的草稿名 --dry-run --no-llm
```

能出对照表和 SRT 即安装成功。
