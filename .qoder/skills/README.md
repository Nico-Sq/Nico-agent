# 项目级 Skill 安装说明

本目录保存前端设计与审查使用的两个第三方 Skill，供 Qoder 在项目内发现和调用。

## ui-ux-pro-max

- 来源：<https://github.com/nextlevelbuilder/ui-ux-pro-max-skill> 的 `.claude/skills/ui-ux-pro-max`
- 版本：2.15.0
- 许可证：MIT，见 `ui-ux-pro-max/LICENSE`
- 内容：`SKILL.md`、`data/`（样式、配色、字体、UX 规则、图标、图表与 22 个技术栈数据）、`references/`、`scripts/`
- 本地改动：`SKILL.md` 中的 `${CLAUDE_PLUGIN_ROOT}/.claude/skills/ui-ux-pro-max` 路径改写为 `.qoder/skills/ui-ux-pro-max`，其余内容保持上游原样
- 未复制：上游 `scripts/tests/`（26 个测试与夹具文件，不属于运行依赖）
- 检索脚本：`python .qoder/skills/ui-ux-pro-max/scripts/search.py "<query>" --domain <domain>`，只依赖 Python 3 标准库

## impeccable

- 来源：<https://github.com/pbakaus/impeccable> 的 `.qoder/skills/impeccable`
- 版本：4.8.0
- 许可证：Apache 2.0，见 `impeccable/LICENSE`
- 内容：`SKILL.md`、`reference/`（设计与审查手册）、`scripts/`（启动器与浏览器采集脚本）
- 引擎：`scripts/impeccable.cmd` 首次运行会下载对应版本的引擎二进制，并按 `scripts/engine-checksums.txt` 校验；本机已缓存 engine 0.1.18 到 `%USERPROFILE%\.impeccable\bin\0.1.18\`
- 引擎不可用时，按 `SKILL.md` 的 `Launcher unavailable` 降级路径直接读取项目上下文与 `reference/craft-floor.md`

## 更新方式

两个 Skill 都是上游快照。更新时重新下载上游对应目录并整体覆盖，同时更新本文件的版本与来源记录。
