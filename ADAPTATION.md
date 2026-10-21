# 自定义设备与电量提醒：给用户和 AI 的适配指南

把本仓库链接和下方模板一起交给能阅读、修改代码并执行测试的 AI，即可从现有工程开始适配。当前主程序内置三个设备类别；尚未提供通用设备添加、任意协议导入、隐藏或排序的图形界面。

## 可以怎样 DIY

| 需求 | 修改位置 | 目前限制 |
| --- | --- | --- |
| 改显示名称 | `core.MODELS`，及四种语言的 `locales/*.json` | 自定义名称不要当作型号识别依据 |
| 改型号说明、图标 | `ui.DeviceCard.SUBTITLES`、`icons.PATHS` | 界面使用设备 key 对应图标 |
| 显示、隐藏或调整顺序 | `ui.Panel` 创建设备卡片的位置 | 需要修改代码；隐藏卡片与停止读取、关闭提醒是不同操作 |
| 已有鼠标/键盘提醒开关、阈值 | 设置页，或 `core.defaults()` | 当前阈值 1–99%，鼠标和键盘分别保存 |
| 提醒声音 | 设置页的提示音开关；`ui.Controller.receive()` | 目前只支持系统提示音开关 |
| 调整提醒去重、恢复、暂停 | `core.AlertPolicy`、`ui.Controller.snooze()` | 目前每次低电量阶段只提醒一次，恢复超过阈值 5% 或充电后重新启用，暂停为 30 分钟 |
| 给其他设备加提醒、独立声音或重复间隔 | 同时扩展配置校验、策略、设置页和测试 | 当前耳机明确排除提醒；只改配置 JSON 不会生效 |
| 新增语言或修改文案 | `i18n.py`、`locales/*.json` | 详见 [LOCALIZATION.md](LOCALIZATION.md) |

## 先识别设备，再适配电量

Windows 查到的名称可能只是接收器名称。VID/PID 能帮助识别接收器，但不能单独证明某个无线设备在线，也不能推导出它的电量协议。

收集型号、连接方式、VID/PID、接口路径中的 MI/COL、Usage Page / Usage、输入和输出报告长度。仅在本地诊断时使用原始路径；公开问题或诊断文件中应去掉序列号和原始实例路径。

`hid_win.enumerate_hid()` 可以枚举现有 HID 接口。拔插接收器前后对比接口，比关掉无线设备后只查看 USB 列表更可靠。主工程没有集成独立 DeviceLab 实验版。

优先查找已有协议实现或厂商文档。只在已确认型号、接口和报告长度匹配时发送明确的电量查询命令，不对未知 HID 接口轮流试写。

## 增加一个已有类别的连接变体

例如同款鼠标新增一个经过验证的接收器 PID：

1. 核对协议与现有型号是否相同；不能仅因为品牌相同就复用。
2. ROG 鼠标/键盘的白名单在 `rog_proto.DEVICES`、`match_device()`；耳机白名单及解析在 `core.read_headset()`、`parse_headset()`。
3. 同时校验 VID/PID、接口标识、Usage、输入输出长度、回复前缀和电量范围。
4. 使用确切接口读写，保留读取锁、超时和无效回复处理；未收到有效电量时返回 `level=None` 和错误说明。
5. 测试有效回复、无关回复、越界电量、离线、充电和超时；在 README 中区分“协议实现”和“实机验证”。

## 增加第四个设备类别

当前 key 为 `mouse`、`keyboard`、`headset`。新增 key 必须贯穿下列位置，不能只向 `MODELS` 加一行：

1. `core.MODELS` 与 `read_all()`：注册类别并分派读取，返回统一的 `Reading`。
2. `ui.DeviceCard.SUBTITLES` 与 `icons.PATHS`：提供副标题和设备图标；卡片由 `MODELS` 顺序生成。
3. `core.read_poll_file()`：检查返回 key、顺序、数量和电量范围；局部读取仍需按 key 合并结果。
4. 更新 `chargedock.py --poll-key` 的允许值；该参数可重复，`ui.Poller` 会按本次查询的 key 列表检查结果。`polling.RetrySchedule` 依据 `MODELS` 注册每个设备的独立重试时间。
5. 如需唤醒刷新，更新 `wake_monitor.py` 的已知设备识别和通知过滤，并向回调传递对应 key，避免让读取回复反过来触发无限刷新，或让一个设备的活动解除其他设备的退避。
6. 如需提醒，更新下节列出的全部环节；为旧配置提供默认值，保留耳机默认不提醒的行为，除非用户明确要求改变。
7. 添加四国语言设备名称和相关文案，以及协议、配置、提醒、交互测试。

## 扩展电量警报

显示设置和提醒设置应分别保存。用户隐藏设备，不一定意味着希望取消低电量提醒；应明确这两个选项的含义。

新提醒字段至少要贯穿：

- `core.defaults()`：新配置的默认值。
- `core.validate_config()`：类型、范围和旧配置迁移；不直接信任 JSON。
- `core.AlertPolicy.evaluate()`：独立设备状态、首次提醒、恢复和暂停逻辑。
- `ui.Panel.build_settings()` 与 `Controller.alert_setting()`：用户可调整的控件和保存。
- `ui.DeviceCard.render()`、`Controller.update_presentation()`：低电量颜色和托盘状态。目前都有鼠标/键盘类别限制。
- `ui.Controller.receive()`：通知文案和声音。
- `polling.RetrySchedule`：读取失败的独立退避与恢复，和提醒暂停不是同一项设置。
- `test_core.py`、`test_ui.py`：边界、去重、充电、离线、保存及旧配置迁移。

保持原则：未知电量不是 0%；离线或读取错误不触发低电量警报；切换语言不能触发新提醒或重复查询。通知模板使用 `tr()` 和具名占位符，不拼接中文句子。

## 给 AI 的提问模板

```text
请基于 https://github.com/SgZincanya/ChargeDock 适配我的设备。

先阅读 README.md、ADAPTATION.md、LOCALIZATION.md 和 ATTRIBUTION.md。
品牌和完整型号：
连接方式（2.4G / USB / 蓝牙）：
已知 VID/PID、接口和报告长度（没有就先协助本地诊断）：
希望显示哪些设备、叫什么、什么顺序：
是否提醒、提醒阈值、声音和重复频率：
界面语言：

先判断现有协议是否支持，不要把选择型号当成已完成适配。
修改前检查设备 key、子进程返回、配置校验、唤醒和提醒中所有相关分支。
保留旧配置兼容与来源引用，新增文案覆盖中/日/英/韩。
使用隔离 --data-dir 测试，不修改我正在运行的程序或真实奥创组件。
完成相关测试，明确哪些功能实机验证过，再生成 EXE。
```

## 本地运行与验证

Windows 64 位，Python 3.11，安装 `requirements.txt` 后：

```powershell
python chargedock.py --data-dir .\dev-data
python -m unittest discover -p "test_*.py"
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm ChargeDock.spec
```

不要提交个人设置、诊断中的设备序列号或开发环境凭据。修改/复用代码时继续保留 [来源引用](ATTRIBUTION.md) 与 [许可证说明](LICENSING.md)。
