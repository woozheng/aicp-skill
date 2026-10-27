# AICP 移动壳 - HTML 调用底层能力文档

## 1. 概述

AICP 移动壳提供了一个统一的 JS 接口 `window.mobile`，让 HTML 页面可以调用手机/电脑的底层硬件和系统能力。

调用方式：

```javascript
const response = await window.mobile.插件名.方法名(参数);
// response 格式: { ok: true/false, ...数据 }
```

## 2. 环境检测

在调用任何功能前，先检测壳是否已连接：

```html
<script>
function checkShell() {
    if (window.mobile) {
        console.log('✅ AICP 壳已连接');
        return true;
    } else {
        console.log('⚠️ 请用 AICP 壳打开');
        return false;
    }
}
</script>
```

## 3. 相机插件 mobile/camera

### 3.1 拍照 take()

```javascript
async function takePhoto() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.camera.take();
        const data = response.result;
        if (data.ok) {
            console.log('拍照成功:', data.path);
            console.log('文件名:', data.name);
            console.log('大小:', data.size, 'bytes');
            // 显示图片
            const img = document.createElement('img');
            img.src = data.path;
            document.body.appendChild(img);
        } else {
            console.error('拍照失败:', data.error);
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

返回数据：

```json
{
    "ok": true,
    "path": "/storage/emulated/0/DCIM/photo.jpg",
    "name": "photo.jpg",
    "size": 123456
}
```

### 3.2 从相册选择 pick()

```javascript
async function pickFromGallery() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.camera.pick();
        const data = response.result;
        if (data.ok) {
            console.log('选择成功:', data.path);
            // 显示图片
            const img = document.createElement('img');
            img.src = data.path;
            document.body.appendChild(img);
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

## 4. GPS 定位插件 mobile/gps

### 4.1 获取当前位置 getCurrent()

```javascript
async function getLocation() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.gps.getCurrent();
        const data = response.result;
        if (data.ok) {
            console.log('📍 位置获取成功');
            console.log('纬度:', data.latitude);
            console.log('经度:', data.longitude);
            console.log('海拔:', data.altitude, 'm');
            console.log('速度:', data.speed, 'm/s');
        } else {
            console.error('获取位置失败:', data.error);
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

返回数据：

```json
{
    "ok": true,
    "latitude": 39.9042,
    "longitude": 116.4074,
    "altitude": 50.0,
    "speed": 0.0,
    "heading": 0.0,
    "accuracy": 10.0,
    "timestamp": "2024-01-01T12:00:00.000Z"
}
```

### 4.2 开始追踪位置 startTracking()

```javascript
async function startTracking() {
    if (!window.mobile) return;
    try {
        await window.mobile.gps.startTracking();
        console.log('✅ 位置追踪已开启');
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 4.3 停止追踪 stopTracking()

```javascript
async function stopTracking() {
    if (!window.mobile) return;
    try {
        await window.mobile.gps.stopTracking();
        console.log('✅ 位置追踪已停止');
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

## 5. 存储插件 mobile/storage

### 5.1 写入数据 set(key, value)

```javascript
async function saveData() {
    if (!window.mobile) return;
    try {
        await window.mobile.storage.set('username', '张三');
        await window.mobile.storage.set('age', 25);
        await window.mobile.storage.set('settings', JSON.stringify({ theme: 'dark' }));
        console.log('✅ 数据已保存');
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 5.2 读取数据 get(key)

```javascript
async function loadData() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.storage.get('username');
        const data = response.result;
        if (data.ok) {
            console.log('用户名:', data.value);
        } else {
            console.error('读取失败:', data.error);
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

返回数据：

```json
{
    "ok": true,
    "key": "username",
    "value": "张三"
}
```

### 5.3 删除数据 remove(key)

```javascript
async function removeData() {
    if (!window.mobile) return;
    try {
        await window.mobile.storage.remove('username');
        console.log('✅ 数据已删除');
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 5.4 清空所有数据 clear()

```javascript
async function clearAll() {
    if (!window.mobile) return;
    try {
        await window.mobile.storage.clear();
        console.log('✅ 所有数据已清空');
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

## 6. 通知插件 mobile/notify

### 6.1 发送通知 send(title, body)

```javascript
async function sendNotification() {
    if (!window.mobile) return;
    try {
        await window.mobile.notify.send('📢 新消息', '您有一条未读消息');
        console.log('✅ 通知已发送');
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

## 7. 电话插件 mobile/phone

### 7.1 拨打电话 call(number)

```javascript
async function makeCall() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.phone.call('10086');
        const data = response.result;
        if (data.ok) {
            console.log('✅ 已拨号:', data.number);
        } else {
            console.error('拨号失败:', data.error);
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

返回数据：

```json
{
    "ok": true,
    "number": "10086"
}
```

## 8. 文件系统插件 mobile/file_system

### 8.1 获取应用目录 getAppDir(type)

```javascript
async function getAppDir() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.fileSystem.getAppDir('documents');
        const data = response.result;
        if (data.ok) {
            console.log('📁 文档目录:', data.path);
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

参数说明：

| type | 说明 |
|------|------|
| documents | 应用文档目录 |
| temp | 临时目录 |
| support | 应用支持目录 |
| external_storage | 外部存储目录（Android） |

### 8.2 读取文件 read(path)

```javascript
async function readFile() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.fileSystem.read('/path/to/file.txt');
        const data = response.result;
        if (data.ok) {
            console.log('📄 文件内容:', data.content);
            console.log('📏 文件大小:', data.size, 'bytes');
        } else {
            console.error('读取失败:', data.error);
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

返回数据：

```json
{
    "ok": true,
    "path": "/path/to/file.txt",
    "content": "文件内容...",
    "size": 1234
}
```

### 8.3 写入文件 write(path, content, append)

```javascript
async function writeFile() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.fileSystem.write(
            '/path/to/file.txt',
            'Hello World!',
            false  // false=覆盖, true=追加
        );
        const data = response.result;
        if (data.ok) {
            console.log('✅ 写入成功');
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 8.4 列出目录 list(path, recursive, includeHidden)

```javascript
async function listFiles() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.fileSystem.list(
            '/path/to/dir',
            false,  // 是否递归
            false   // 是否包含隐藏文件
        );
        const data = response.result;
        if (data.ok) {
            console.log('📂 共', data.count, '个文件');
            data.files.forEach(f => {
                const icon = f.isDirectory ? '📁' : '📄';
                console.log(icon, f.name, f.size, 'bytes');
            });
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

返回数据：

```json
{
    "ok": true,
    "path": "/path/to/dir",
    "count": 10,
    "files": [
        {
            "name": "file1.txt",
            "path": "/path/to/dir/file1.txt",
            "isDirectory": false,
            "size": 1234,
            "modified": "2024-01-01T12:00:00.000Z"
        }
    ]
}
```

### 8.5 搜索文件 search(path, pattern, recursive, caseSensitive)

```javascript
async function searchFiles() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.fileSystem.search(
            '/path/to/dir',
            '.txt',
            false,  // 是否递归
            false   // 是否区分大小写
        );
        const data = response.result;
        if (data.ok) {
            console.log('🔍 找到', data.count, '个文件');
            data.files.forEach(f => console.log(f));
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 8.6 删除文件/目录 delete(path, recursive)

```javascript
async function deleteFile() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.fileSystem.delete(
            '/path/to/file.txt',
            false  // 是否递归删除目录
        );
        const data = response.result;
        if (data.ok) {
            console.log('✅ 删除成功');
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 8.7 移动文件 move(from, to)

```javascript
async function moveFile() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.fileSystem.move(
            '/path/from.txt',
            '/path/to.txt'
        );
        const data = response.result;
        if (data.ok) {
            console.log('✅ 移动成功');
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 8.8 复制文件 copy(from, to)

```javascript
async function copyFile() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.fileSystem.copy(
            '/path/from.txt',
            '/path/to.txt'
        );
        const data = response.result;
        if (data.ok) {
            console.log('✅ 复制成功');
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 8.9 创建目录 mkdir(path, recursive)

```javascript
async function createDir() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.fileSystem.mkdir(
            '/path/new_dir',
            true  // 是否递归创建父目录
        );
        const data = response.result;
        if (data.ok) {
            console.log('✅ 目录创建成功');
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 8.10 获取文件信息 info(path)

```javascript
async function getFileInfo() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.fileSystem.info('/path/to/file.txt');
        const data = response.result;
        if (data.ok) {
            console.log('📄 文件信息:');
            console.log('  名称:', data.name);
            console.log('  大小:', data.size, 'bytes');
            console.log('  修改时间:', data.modified);
            console.log('  是目录:', data.isDirectory);
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 8.11 检查文件是否存在 exists(path)

```javascript
async function checkExists() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.fileSystem.exists('/path/to/file.txt');
        const data = response.result;
        if (data.ok) {
            console.log('文件存在:', data.exists);
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

## 9. 分享插件 mobile/share

### 9.1 分享文本 text(content)

```javascript
async function shareText() {
    if (!window.mobile) return;
    try {
        await window.mobile.share.text('分享的内容');
        console.log('✅ 分享成功');
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 9.2 分享文件 file(path)

```javascript
async function shareFile() {
    if (!window.mobile) return;
    try {
        await window.mobile.share.file('/path/to/file.jpg');
        console.log('✅ 分享成功');
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

## 10. 网络状态插件 mobile/network

### 10.1 获取网络状态 getStatus()

```javascript
async function getNetworkStatus() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.network.getStatus();
        const data = response.result;
        if (data.ok) {
            console.log('📶 网络状态:');
            console.log('  连接:', data.connected ? '✅ 已连接' : '❌ 未连接');
            console.log('  类型:', data.type);  // wifi, mobile, none
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

返回数据：

```json
{
    "ok": true,
    "connected": true,
    "type": "wifi"
}
```

## 11. 电量插件 mobile/battery

### 11.1 获取电量状态 getStatus()

```javascript
async function getBatteryStatus() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.battery.getStatus();
        const data = response.result;
        if (data.ok) {
            console.log('🔋 电量状态:');
            console.log('  电量:', data.level, '%');
            console.log('  充电中:', data.is_charging ? '✅ 是' : '❌ 否');
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

返回数据：

```json
{
    "ok": true,
    "level": 85,
    "is_charging": true,
    "state": "charging"
}
```

## 12. 剪贴板插件 mobile/clipboard

### 12.1 复制到剪贴板 copy(text)

```javascript
async function copyText() {
    if (!window.mobile) return;
    try {
        await window.mobile.clipboard.copy('要复制的内容');
        console.log('✅ 已复制到剪贴板');
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 12.2 从剪贴板粘贴 paste()

```javascript
async function pasteText() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.clipboard.paste();
        const data = response.result;
        if (data.ok) {
            console.log('📋 粘贴内容:', data.text);
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

返回数据：

```json
{
    "ok": true,
    "text": "剪贴板中的内容"
}
```

## 13. 震动插件 mobile/vibrate

### 13.1 震动 vibrate(duration)

```javascript
async function vibratePhone() {
    if (!window.mobile) return;
    try {
        await window.mobile.vibrate.vibrate(200);  // 震动 200ms
        console.log('✅ 震动完成');
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

## 14. 进程控制插件 mobile/process

### 14.1 执行 Shell 命令 shell(command, timeout)

```javascript
async function runShell() {
    if (!window.mobile) return;
    try {
        // 列出当前目录
        const response = await window.mobile.process.shell('dir');
        const data = response.result;
        if (data.ok) {
            console.log('✅ 执行成功');
            console.log(data.stdout);
        } else {
            console.error('执行失败:', data.stderr);
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

返回数据：

```json
{
    "ok": true,
    "exit_code": 0,
    "stdout": "命令输出内容...",
    "stderr": ""
}
```

### 14.2 运行进程 run(path, args, timeout)

```javascript
async function runProcess() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.process.run('python', ['-c', 'print(123)']);
        const data = response.result;
        if (data.ok) {
            console.log('✅ 运行成功:', data.stdout);
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 14.3 后台启动 spawn(path, args)

```javascript
async function spawnProcess() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.process.spawn('python', ['-m', 'http.server', '8080']);
        const data = response.result;
        if (data.ok) {
            console.log('✅ 后台启动, PID:', data.pid);
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 14.4 打开文件/网址/应用 open(target)

```javascript
async function openTarget() {
    if (!window.mobile) return;
    try {
        // 打开网址
        await window.mobile.process.open('https://google.com');
        
        // 打开系统设置
        await window.mobile.process.open('android.settings.SETTINGS');
        
        // 打开本地文件（桌面端）
        await window.mobile.process.open('notepad.exe');
        
        console.log('✅ 已打开');
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 14.5 终止进程 kill(target)

```javascript
async function killProcess() {
    if (!window.mobile) return;
    try {
        // 通过进程名终止
        await window.mobile.process.kill('notepad.exe');
        
        // 通过 PID 终止
        await window.mobile.process.kill(12345);
        
        console.log('✅ 已终止');
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

## 15. 录音插件 mobile/audio

### 15.1 开始录音 start(path)

```javascript
async function startRecording() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.audio.start('/tmp/voice.aac');
        const data = response.result;
        if (data.ok) {
            console.log('🔴 录音中...');
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

### 15.2 停止录音 stop()

```javascript
async function stopRecording() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.audio.stop();
        const data = response.result;
        if (data.ok) {
            console.log('✅ 录音完成');
            console.log('文件:', data.path);
            console.log('大小:', data.size, 'bytes');
        }
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

返回数据：

```json
{
    "ok": true,
    "path": "/tmp/voice.aac",
    "size": 45678,
    "status": "stopped"
}
```

### 15.3 检查录音状态 isRecording()

```javascript
async function checkRecording() {
    if (!window.mobile) return;
    try {
        const response = await window.mobile.audio.isRecording();
        const data = response.result;
        console.log(data.recording ? '🔴 正在录音' : '⏹ 未录音');
    } catch(e) {
        console.error('错误:', e.message);
    }
}
```

## 16. 完整示例：一个简单的语音采集页面

```html
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>语音采集工具</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:-apple-system,sans-serif;background:#f5f5f5;padding:16px}
        .container{max-width:400px;margin:0 auto}
        h1{font-size:20px;margin-bottom:16px}
        .btn{display:block;width:100%;padding:14px;margin-bottom:8px;border:none;border-radius:8px;font-size:16px;cursor:pointer;text-align:center}
        .btn:active{opacity:0.7}
        .btn-blue{background:#6366f1;color:#fff}
        .btn-red{background:#ef4444;color:#fff}
        .btn-green{background:#10b981;color:#fff}
        .output{background:#fff;padding:12px;border-radius:8px;margin-top:12px;font-size:13px;max-height:300px;overflow:auto;white-space:pre-wrap;word-break:break-all}
        .status{text-align:center;padding:8px;border-radius:8px;margin-bottom:12px}
        .status.on{background:#dcfce7;color:#166534}
        .status.off{background:#fee2e2;color:#991b1b}
    </style>
</head>
<body>
<div class="container">
    <h1>🎤 语音采集工具</h1>
    
    <div id="status" class="status off">⏳ 检测壳...</div>
    
    <button class="btn btn-blue" onclick="takePhoto()">📷 拍照</button>
    <button class="btn btn-blue" onclick="pickImage()">🖼️ 相册</button>
    <button class="btn btn-green" onclick="getLocation()">📍 位置</button>
    <button class="btn btn-blue" onclick="startRecord()">🎤 开始录音</button>
    <button class="btn btn-red" onclick="stopRecord()">⏹ 停止录音</button>
    <button class="btn btn-blue" onclick="sendNotify()">🔔 通知</button>
    <button class="btn btn-blue" onclick="vibratePhone()">📳 震动</button>
    
    <div class="output" id="output">点击按钮查看结果...</div>
</div>

<script>
// ===== 状态检测 =====
function checkShell() {
    const el = document.getElementById('status');
    if (window.mobile) {
        el.textContent = '✅ 壳已连接';
        el.className = 'status on';
        return true;
    } else {
        el.textContent = '⚠️ 未连接，请用 AICP 壳打开';
        el.className = 'status off';
        return false;
    }
}
checkShell();

// ===== 输出 =====
function log(msg) {
    const el = document.getElementById('output');
    el.textContent = msg + '\n\n' + el.textContent;
}

// ===== 各功能 =====
async function takePhoto() {
    if (!checkShell()) return;
    try {
        const r = await window.mobile.camera.take();
        const d = r.result;
        log(d.ok ? '✅ 拍照成功\n路径: ' + d.path : '❌ 失败: ' + d.error);
    } catch(e) { log('❌ ' + e.message); }
}

async function pickImage() {
    if (!checkShell()) return;
    try {
        const r = await window.mobile.camera.pick();
        const d = r.result;
        log(d.ok ? '✅ 选择成功\n路径: ' + d.path : '❌ 失败: ' + d.error);
    } catch(e) { log('❌ ' + e.message); }
}

async function getLocation() {
    if (!checkShell()) return;
    try {
        const r = await window.mobile.gps.getCurrent();
        const d = r.result;
        log(d.ok ? '📍 位置\n纬度: ' + d.latitude + '\n经度: ' + d.longitude : '❌ 失败: ' + d.error);
    } catch(e) { log('❌ ' + e.message); }
}

async function startRecord() {
    if (!checkShell()) return;
    try {
        const r = await window.mobile.audio.start('/tmp/voice.aac');
        const d = r.result;
        log(d.ok ? '🔴 录音中...' : '❌ 失败: ' + d.error);
    } catch(e) { log('❌ ' + e.message); }
}

async function stopRecord() {
    if (!checkShell()) return;
    try {
        const r = await window.mobile.audio.stop();
        const d = r.result;
        log(d.ok ? '✅ 录音完成\n文件: ' + d.path + '\n大小: ' + (d.size/1024).toFixed(1) + 'KB' : '❌ 失败: ' + d.error);
    } catch(e) { log('❌ ' + e.message); }
}

async function sendNotify() {
    if (!checkShell()) return;
    try {
        await window.mobile.notify.send('语音采集', '录音已保存');
        log('✅ 通知已发送');
    } catch(e) { log('❌ ' + e.message); }
}

async function vibratePhone() {
    if (!checkShell()) return;
    try {
        await window.mobile.vibrate.vibrate(200);
        log('✅ 震动完成');
    } catch(e) { log('❌ ' + e.message); }
}
</script>
</body>
</html>
```

## 17. 错误处理最佳实践

```javascript
// 推荐：统一的调用封装
async function callMobile(plugin, method, ...args) {
    if (!window.mobile) {
        throw new Error('请用 AICP 壳打开');
    }
    if (!window.mobile[plugin]) {
        throw new Error('插件不存在: ' + plugin);
    }
    if (!window.mobile[plugin][method]) {
        throw new Error('方法不存在: ' + plugin + '.' + method);
    }
    try {
        const response = await window.mobile[plugin][method](...args);
        if (response && response.result && response.result.ok) {
            return response.result;
        } else {
            throw new Error(response?.result?.error || '未知错误');
        }
    } catch (e) {
        throw new Error(e.message || '调用失败');
    }
}

// 使用示例
async function example() {
    try {
        const result = await callMobile('camera', 'take');
        console.log('拍照成功:', result.path);
    } catch (e) {
        console.error('错误:', e.message);
    }
}
```

## 18. 插件列表速查

| 插件 | 方法 | 说明 | 平台 |
|------|------|------|------|
| camera | take() | 拍照 | 移动端 |
| camera | pick() | 从相册选择 | 移动端 |
| gps | getCurrent() | 获取当前位置 | 移动端 |
| gps | startTracking() | 开始位置追踪 | 移动端 |
| gps | stopTracking() | 停止位置追踪 | 移动端 |
| storage | set(key, value) | 写入数据 | 通用 |
| storage | get(key) | 读取数据 | 通用 |
| storage | remove(key) | 删除数据 | 通用 |
| storage | clear() | 清空所有数据 | 通用 |
| notify | send(title, body) | 发送通知 | 移动端 |
| phone | call(number) | 拨打电话 | 移动端 |
| vibrate | vibrate(duration) | 震动 | 移动端 |
| clipboard | copy(text) | 复制到剪贴板 | 通用 |
| clipboard | paste() | 从剪贴板粘贴 | 通用 |
| share | text(content) | 分享文本 | 移动端 |
| share | file(path) | 分享文件 | 移动端 |
| network | getStatus() | 获取网络状态 | 通用 |
| battery | getStatus() | 获取电量状态 | 移动端 |
| fileSystem | getAppDir(type) | 获取应用目录 | 通用 |
| fileSystem | read(path) | 读取文件 | 通用 |
| fileSystem | write(path, content, append) | 写入文件 | 通用 |
| fileSystem | list(path, recursive, includeHidden) | 列出目录 | 通用 |
| fileSystem | search(path, pattern, recursive, caseSensitive) | 搜索文件 | 通用 |
| fileSystem | delete(path, recursive) | 删除文件/目录 | 通用 |
| fileSystem | move(from, to) | 移动文件 | 通用 |
| fileSystem | copy(from, to) | 复制文件 | 通用 |
| fileSystem | mkdir(path, recursive) | 创建目录 | 通用 |
| fileSystem | info(path) | 获取文件信息 | 通用 |
| fileSystem | exists(path) | 检查文件是否存在 | 通用 |
| process | shell(command, timeout) | 执行 Shell 命令 | 桌面端 |
| process | run(path, args, timeout) | 运行进程 | 桌面端 |
| process | spawn(path, args) | 后台启动进程 | 桌面端 |
| process | open(target) | 打开文件/网址/应用 | 通用 |
| process | kill(target) | 终止进程 | 桌面端 |
| audio | start(path) | 开始录音 | 移动端 |
| audio | stop() | 停止录音 | 移动端 |
| audio | isRecording() | 检查录音状态 | 移动端 |

[DONE]

