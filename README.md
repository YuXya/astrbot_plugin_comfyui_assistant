# ComfyUI 绘图助手

独立编写的 AstrBot 插件。AI 选择工作流、填写描述、图片和尺寸；程序负责持续跟进生成，发送开始提示和最终图片。生成状态与发送状态分别保存，查询不会删除任务，重发使用已保存的原图。

> 0.1.1 为测试版本：自动化与本机 ComfyUI 验证已通过，QQ 实际收图尚待联调。

## 使用

保持 ComfyUI 服务运行。在 AstrBot 插件列表中启用「ComfyUI 绘图助手」，打开它的 Plugin Pages → assistant。服务默认地址为 `http://127.0.0.1:8188`。

自然语言示例：

- 「画一个金发碧眼猫娘，竖图，768×1024。」
- 发送一张图片并说「把背景改成蓝色，人物保持不变，1024×768。」
- 发送两张图片并说「图一角色在左，图二角色在右，两个人拍掌。」
- 「刚才的图片没收到，查一下发送状态。」
- 「重发刚才任务的原图，不要重新画。」

管理页提供工作流上传、名称、启停、介绍、输入与输出绑定，以及任务状态、查询、原图重发、服务和保留时间设置。首版仅处理图片工作流。工作流须导出为 ComfyUI **API 格式**，不是画布格式。自动识别 `Simple String`、`ETN_LoadImageBase64`、常见空 Latent 尺寸入口与 `SaveImage`；其他节点可手动绑定。

上传时自动识别的入口顺序会存入数据库，后续调用按页面从上到下的顺序填入。多图槽位不得交换。最终输出仅采集管理页选择的节点，不会把其他预览结果一起发送。

## 默认提示词前缀

在 Plugin Pages → assistant → 工作流 → 编辑中填写「默认提示词前缀」。每个工作流独立保存，留空不添加；旧工作流默认留空。默认应用于第一个文字入口，有多个入口时可选择「前缀应用位置」，例如只选择正面描述入口。位置按节点及输入字段保存，删除或修改该入口后需要重新选择。

例如前缀填写 `masterpiece, best quality, source_anime,`，AI 填写「金发碧眼猫娘，坐在窗边」，插件会将前缀与描述用换行分隔后写入所选入口。AI 不需要重复填写前缀。支持中文、英文及多行内容；前缀和本次描述合计最多 20000 个字符，超出时明确报错。

前缀只影响新任务，不修改原工作流模板。查询、重启恢复及重发原图不会再次拼接；已有任务保留创建时的设置。如果绑定入口后还有提示词扩写，前缀也会经过扩写，不保证标签在扩写后逐字保留。示例标签需自行按模型用途配置，不会自动填入已有工作流。

## AI 工具

| 工具 | 作用 |
| --- | --- |
| `comfyui_workflows` | 列出可用工作流、介绍、输入顺序与尺寸限制 |
| `comfyui_generate` | 提交工作流、`texts`、`image_urls`、可选宽高和简短配文 |
| `comfyui_tasks` | 查询本人在当前会话中的任务，恢复对原任务的跟踪 |
| `comfyui_resend` | 按任务 ID 重发原图，不再次生成 |

工具自动使用真实会话和发起人，模型不能指定收件人。管理页可管理全部任务，但重发仍发往任务原会话。

宽高成对填写，省略则沿用模板。默认每边 256—2048、8 的倍数、总像素不超过 2,097,152；非法尺寸返回明确原因，不静默缩小。尺寸只改变当次副本，种子按原插件行为随机，其余模型、LoRA、扩写、采样参数保持模板值。

图片可来自当前消息、引用消息、同会话历史图片、QQ 工具提供的 URL、本地媒体缓存和本插件返回的 `comfy-media:任务ID:序号`。兼容图片助手在 `image_urls` 数组中返回的占位符，内部解析 Base64，不要求 AI 搬运大段二进制。图片数量不明确时返回错误，不用重复的第一张图片凑数。本地路径仅限插件数据及宿主媒体缓存目录；不接受任意磁盘文件。

## 任务与发送

任务及尝试记录保存在插件数据目录的 SQLite 中。生成结果下载到任务目录后再发送；模型结束回复不会取消后台任务。

- `completed` 仅表示生成完成；`sent` 表示宿主发送调用返回成功，不表示用户已经看到图片。
- 宿主返回 `False` 或明确拒绝时记为 `failed`，原图保留；可明确要求重发。
- 网络异常、超时或发送中重启时记为 `unknown`，即「发送结果未确认」，不自动重复发图。
- 提交响应丢失时用任务标识核对 ComfyUI 队列和历史，不盲目再次提交。
- 重启恢复未完成任务；长期无法查到原任务时保留状态，可主动查询继续跟踪。
- 任务默认保留 30 天，图片及含输入数据的执行快照文件保留 7 天；过期不自动重新画图。

正常成功任务发两条消息：开始提示、最终图片与配文。明确失败时会有失败说明。额外的用户查询、用户要求重发或同轮其他工具回复不计入这两条。插件只抑制自身绘图过程的进度与重复收尾，不屏蔽普通聊天。当前安装使用宿主非流式回复；流式或混合其他工具的对话需要另行验证。

## 安装与回退

Python 3.12，声明兼容 AstrBot `>=4.27.5,<5`。实际验收环境为 AstrBot 4.28.0；4.27.5 尚未做本插件整套实机验收。

在 AstrBot 插件页使用仓库链接 `https://github.com/YuXya/astrbot_plugin_comfyui_assistant` 安装；也可运行 `python scripts/package.py` 生成安装包后上传，或将源码放入 `data/plugins/astrbot_plugin_comfyui_assistant` 后加载。管理配置仅使用 Plugin Pages，无额外端口。

仓库不附带个人工作流、模型、图片或任务记录。安装后需自行上传 API 工作流，并在 ComfyUI 中安装它需要的模型和节点。

旧版 `astrbot_plugin_comfyui` 的 Krea2 工作流与说明可单独迁移。以下路径相对于 AstrBot 根目录；在源码项目目录运行时，请替换为实际路径：

```powershell
python scripts/migrate.py --source /path/to/AstrBot/data/plugin_data/astrbot_plugin_comfyui --data /path/to/AstrBot/data/plugin_data/astrbot_plugin_comfyui_assistant
```

迁移只读取 `Krea2*.json` 工作流及介绍，不导入旧任务或旧图片。迁移幂等，已有工作流不会被覆盖。模型与自定义节点仍由 ComfyUI 管理；本插件不下载模型。

回退时先停用本插件，再重新启用旧的 `astrbot_plugin_comfyui`。原配置、工作流和说明保留；新插件数据独立。安装切换前请自行备份旧插件配置与工作流。测试范围见 [验收记录](docs/validation.md)。不要同时启用两个插件让 AI 选择重复的绘图工具。

## 开发与检查

进入可导入 AstrBot 4.28.0 的 Python 3.12 环境；如 AstrBot 仅有源码，将其目录加入 `PYTHONPATH`。

```sh
python -m pip install -r requirements.txt pytest pytest-asyncio ruff
python -m pytest -q
python -m ruff check .
python scripts/package.py
```

管理页浏览器测试：安装 Node.js 与 Playwright，执行 `node tests/ui.cjs`。可通过环境变量 `PLAYWRIGHT_CHROMIUM_EXECUTABLE` 指定 Chromium/Edge 路径，否则使用 Playwright 默认的 Chromium。

本地生成验证会真实调用 ComfyUI（不会发送 QQ）：

```sh
python scripts/local_validation.py --source /path/to/old-plugin-data
python scripts/local_validation.py --source /path/to/old-plugin-data --remaining
```

核心调度与媒体处理在 `assistant/`，宿主适配和工具在 `main.py`，管理页在 `pages/assistant/`。自动化使用替身，不发真实 QQ 消息。`scripts/local_validation.py` 创建普通测试素材并调用本机 ComfyUI，仅检查执行、文件和尺寸，不显示图片。

代码和页面为本项目独立实现。迁入的用户工作流、其中模型与节点仍保留原有来源和各自许可；未将第三方插件源码打包。当前仓库暂未设置开源许可证。
