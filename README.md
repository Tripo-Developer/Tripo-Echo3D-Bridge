# Tripo3D Echo3D Bridge

这是一个给合作方演示使用的独立 Python 桌面程序。Echo3D 没有本地客户端，因此桌面程序只提供原生窗口和本地 WebSocket 转发，不包含 Tripo 登录、资产下载、云端上传或 Go/Wails。

## 运行

```powershell
python -m pip install -r requirements.txt
python desktop_main.py
```

窗口启动后会监听 `127.0.0.1:60710`：

- Tripo Studio 前端连接 `ws://127.0.0.1:60710/`
- 合作方 Echo3D 前端连接 `ws://127.0.0.1:60710/echo3d`

双方使用现有 DCC `handshake`、`file_transfer`、逐分片 `file_transfer_ack` 和最终 `import_complete` 消息。桌面程序不选择 GLB；文件由 Tripo Studio 前端发送，Echo3D 前端负责组装和实际导入/上传。

## 打包

```powershell
./build.ps1
```

产物为 `dist/Tripo-Echo3D-Bridge.exe`，目标机器不需要安装 Python 或 Go。旧 Wails 和 SDK 登录 demo 已移入本机 `legacy/`，不参与版本管理、打包或发布。

## 合作方前端接入

```js
import { Echo3DBridgeClient } from './web/echo3d-bridge-client.js'
const bridge = new Echo3DBridgeClient({
  role: 'echo3d',
  async onTransfer(glb, metadata) {
    // 在这里调用合作方已有的上传/导入流程；下面函数需由合作方实现。
    await uploadToEcho3D(glb, metadata.fileName)
    return { success: true, message: 'echo3D 导入完成' }
  }
})
await bridge.connect()
```

SDK 自动组装分片并回 ACK，只有 `onTransfer` 返回明确结果才回报处理完成。
Bridge 不自带 Echo3D 云端账号或上传 API。关闭桌面窗口即释放端口。

开发验证：`python -m unittest discover -s tests -v`。端口同时绑定 IPv4/IPv6 loopback；
`bridge-client.html` 仅是可选的 DCC 协议诊断工具，默认端口 60710，不是应用界面。
