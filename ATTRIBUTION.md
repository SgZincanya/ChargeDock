# 来源与致谢

电量岛基于已有的 Windows HID 访问代码，增加 ROG / SteelSeries 电量协议、Qt 托盘界面、提醒及设备活动检测。下列项目是具体的代码来源或协议参考，并非本项目的作者、赞助方或官方支持方。

## 直接沿用的代码

### yszyszysz233 / 2.4G-battery-viewer

- 原项目：<https://github.com/yszyszysz233/2.4G-battery-viewer>
- 基准提交：[`f0e983e88900592237c7df9ae6b80b9dbeb3020b`](https://github.com/yszyszysz233/2.4G-battery-viewer/tree/f0e983e88900592237c7df9ae6b80b9dbeb3020b)
- 对应文件：[`hid_win.py`](https://github.com/yszyszysz233/2.4G-battery-viewer/blob/f0e983e88900592237c7df9ae6b80b9dbeb3020b/hid_win.py)，提供 Windows HID 枚举、能力查询和读写。
- 本仓库保留上游 Git 历史，以便核对来源和改动。
- 该基准提交未包含 LICENSE 文件。本仓库没有为上游代码新增或替换许可证；引用和保留历史不代表获得上游额外授权。

感谢原作者提供外设电量工具和纯 ctypes 的 Windows HID 实现。

## 电量协议参考

下列项目用于理解设备编号、接口、查询指令和回复字段。本项目使用 Python 实现协议，不直接引入相应的 C++ / C# 实现文件。

### seerge / g-helper

- 项目：<https://github.com/seerge/g-helper>
- 参考快照：[`af7e1c002b1597269d8a977f2464fdab4f5d9fe6`](https://github.com/seerge/g-helper/tree/af7e1c002b1597269d8a977f2464fdab4f5d9fe6)
- [ChakramX.cs](https://github.com/seerge/g-helper/blob/af7e1c002b1597269d8a977f2464fdab4f5d9fe6/app/Peripherals/Mouse/Models/ChakramX.cs)：魔刃 X 型号与电量字段。
- [AsusMouse.cs](https://github.com/seerge/g-helper/blob/af7e1c002b1597269d8a977f2464fdab4f5d9fe6/app/Peripherals/Mouse/AsusMouse.cs)：ROG 鼠标 HID 通信方式。
- [Azoth.cs](https://github.com/seerge/g-helper/blob/af7e1c002b1597269d8a977f2464fdab4f5d9fe6/app/Peripherals/Keyboard/Models/Azoth.cs)：夜魔型号与电量字段。
- [AsusKeyboard.cs](https://github.com/seerge/g-helper/blob/af7e1c002b1597269d8a977f2464fdab4f5d9fe6/app/Peripherals/Keyboard/AsusKeyboard.cs)：ROG 键盘 HID 通信方式。
- 上游许可证：[GPL-3.0](https://github.com/seerge/g-helper/blob/af7e1c002b1597269d8a977f2464fdab4f5d9fe6/LICENSE)。此处为上游声明，不表示本项目替所有文件统一授予 GPL 许可。
- 对应本项目实现：`rog_proto.py`。

### Sapd / HeadsetControl

- 项目：<https://github.com/Sapd/HeadsetControl>
- 发布整理时核对的快照：[`91ee690a5e302a10a6f4f75a46be05846a38a3b0`](https://github.com/Sapd/HeadsetControl/tree/91ee690a5e302a10a6f4f75a46be05846a38a3b0)
- [steelseries_arctis_nova_pro_wireless.hpp](https://github.com/Sapd/HeadsetControl/blob/91ee690a5e302a10a6f4f75a46be05846a38a3b0/lib/devices/steelseries_arctis_nova_pro_wireless.hpp)：Nova Pro Wireless 基站的电量查询指令、0–8 档电量和连接状态。
- 上游许可证：[GPL-3.0](https://github.com/Sapd/HeadsetControl/blob/91ee690a5e302a10a6f4f75a46be05846a38a3b0/license)。
- 对应本项目实现：`core.py` 的耳机查询与回复解析。

### ShadowBitBasher / DJI-Mic-Control

- 项目：<https://github.com/ShadowBitBasher/DJI-Mic-Control>
- 核对提交：[`9ba76880807a71d4eaba74c785dbee186a98f43b`](https://github.com/ShadowBitBasher/DJI-Mic-Control/tree/9ba76880807a71d4eaba74c785dbee186a98f43b)
- 参考 `PROTOCOL.md`、`crates/protocol/src/{packet,crc,settings,state}.rs`、
  `models/mic_mini.rs` 和 `crates/device/src/{actor,manager}.rs`。
- 本分支以 Python 改写帧、CRC、v1/v2 槽和控制字段；独立实现 Windows WinUSB、
  管理子进程和 ACK 加真实回报确认，不引入上游程序、Rust/Tauri 运行时或驱动安装器。
- 上游当前 LICENSE 为 **Unlicense**，原文附于 `licenses/DJI-Mic-Control-Unlicense.txt`。
  注意接收 CRC8 的反射寄存器初值与文档写法不同，本机实际数据已校验；不会照搬上游不校验接收 CRC 的行为。

### usokawa / dji-mic-mo

- 项目：<https://github.com/usokawa/dji-mic-mo>
- 核对提交：[`7c2dd5b0262f35b475193224fca2681d75987e9c`](https://github.com/usokawa/dji-mic-mo/tree/7c2dd5b0262f35b475193224fca2681d75987e9c)
- 仅补充参考 `dji-mic-mo.py` 的 `RULES`：Mic Mini 2 接收器不允许写入降噪开关/强度，
  以及各 TX 槽中降噪状态字段。此限制与本机实际 `0xFF` ACK 一致；不引用其驱动替换建议。
- 上游 **BSD-2-Clause**，完整原文（含版权声明）附于 `licenses/dji-mic-mo-BSD-2-Clause.txt`。
- 本项目不引入其 pyusb/BLE 运行时，也不涉及内部录音、删除或格式化功能。

WinUSB 枚举依据 Microsoft 的 [WinUSB 驱动注册说明](https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/winusb-installation)
和 SetupAPI / Configuration Manager 接口，不使用本机特定 GUID。

## 运行时与打包依赖

Python、PySide6 / Qt / Shiboken、PyInstaller 和 Microsoft Visual C++ 运行库的说明见 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)，附带的许可证文本位于 [licenses/](licenses/)。

## 图标与产品名称

`assets/app.svg` 是本项目制作的电量岛图标；`app.png` 和 `app.ico` 为其导出资源，界面符号在 `icons.py` 中绘制。ROG、ASUS、SteelSeries 等产品名称仅用于标识兼容设备。
