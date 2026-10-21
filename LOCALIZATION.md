# 语言接口

内置简体中文 `zh-CN`、日语 `ja-JP`、英语 `en-US`、韩语 `ko-KR`。默认配置为 `"language": "system"`，按 Windows 用户界面语言选择；不支持的系统语言回退英语。中文系统变体目前均使用简体中文。

设置页“语言”菜单可以立即切换，无需重新启动。语言选择保存到原有 `settings.json`，不会重置提醒阈值或设备读取状态。“跟随系统”在启动或重新选择时解析系统语言。

## 调用接口

`i18n.py` 不依赖 Qt，提供：

```python
from i18n import tr, set_language, translate_message

set_language("ja-JP")
text = tr("已连接")
message = tr("{device}剩余 {level}%", device="Chakram X", level=18)
```

JSON 文件中的中文原文是稳定的消息标识（msgid），值为对应翻译。修改中文原文时，需要同步四个语言文件和调用位置。`translate_message()` 用于后端中文状态/错误的展示转换；协议解析和休眠判断仍依据原始状态，不依赖译文。

找不到译文时回退英语；英语也没有该消息时保留原文，因此自定义设备名称不会变成空白。操作系统异常中的原始细节保留系统原语言。

## 修改或新增语言

1. 在 `locales/` 中编辑或增加 UTF-8 JSON 文件，复制 `zh-CN.json` 的全部消息标识。
2. 保留 `{device}`、`{level}` 等具名占位符，以及数字、百分号和必要空格。
3. 新增语言时同步 `i18n.LANGUAGES`、`resolve_language()` 的语言映射，以及测试中的语言列表。
4. `core.validate_config()` 根据 `LANGUAGES` 校验并保留设置；设置页语言选项也由它生成。
5. `ChargeDock.spec` 自动收集整个 `locales/` 文件夹，打包后不依赖在线翻译服务。

## 给界面增加文案

- 通过 `ui.label()` 创建文本，或使用 `TranslatedLabel`；它保留原文并支持即时重译。
- 新增菜单按钮的选项通过 `MenuChoice.addItem()` 添加原文；选项文本翻译后，配置中保存的值保持不变。
- 普通按钮用 `sourceText` 属性记录原文；辅助名称/图标按钮提示用 `sourceTip`，额外帮助用 `sourceHelp`，由 `Panel.retranslate()` 更新。
- 新增开关需保留 `source_text`；通知等即时消息直接调用 `tr()`。
- 动态句子使用具名占位符，避免把句子拆成多个片段。语言改变后的设备状态与托盘展示由 `Controller.update_presentation()` 刷新，不重新发送电量警报。

## 验证

`test_i18n.py` 检查四种语言的消息集合、占位符、回退、配置校验、即时切换、托盘菜单、设备状态和不重复提醒。新增文案后运行全部测试，并查看四种语言的主界面与设置页，尤其注意长文本、菜单宽度和日/韩文字体回退。
