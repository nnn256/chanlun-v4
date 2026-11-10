# 缠论V4 公网研究面板

浏览器界面支持沪深京5,580只股票搜索、真实日线/30分/5分、形成状态、笔/线段/中枢、原生买卖候选、扫描进度、旧模型回测归档及CSV导出。当前图表全部加载供应商可取历史，默认显示最近300根。

`server.py` 是独立只读展示服务，只绑定 `127.0.0.1:8810`。它从后台4.1.1引擎读取真实行情，过滤任务、文件路径和本机配置；公网不能创建任务、修改扫描配置或调用桌面控制接口。来源响应时间与确认结构时点分开保留。

前端在 `web/`，不需要构建工具。`config.js` 的空地址表示使用同源API。GitHub Pages副本由 `docs/` 提供，配置当前公网API地址。图表库Lightweight Charts 5.2.1保留LICENSE/NOTICE及可见归属。

后台引擎需要独立运行：

```powershell
$env:CHANLUN_APP_DATA_DIR = "$env:USERPROFILE/Documents/ChanlunV4PublicData"
& './缠论V4-4.1.1-实时K线版.exe' --headless --no-browser --web-port 8805 --tdx-port 18085
python public_web/server.py --port 8810
cloudflared tunnel --url http://127.0.0.1:8810 --protocol http2 --no-autoupdate
```

三条命令在独立终端运行。行情引擎和网页服务独立于桌面GUI；公开网关仅映射8810，不映射桌面任务管理端口。默认读取 `Documents/ChanlunV4PublicData/desktop-status.json`，可通过 `CHANLUN_PUBLIC_DESKTOP_STATUS` 指定其他可信本机引擎状态文件。扫描展示读取原核验版的已保存扫描记录，可用 `CHANLUN_PUBLIC_SCAN_STATUS` 指定。

当前公网API使用Cloudflare临时网关，依赖本机后台服务与网络持续运行；重新启动网关会生成新地址，需要同步GitHub Pages的 `docs/config.js`。GitHub Pages自身可持续提供页面文件，但不能运行TDX或Python计算。长期固定行情后端需要部署在持续在线的服务器或配置命名隧道。

数据不生成假K线、不把旧缓存标成刚成交；缺今日数据、停牌、休市或源头失败都在页面显示。旧收益回测与当前固定引擎分开记录；结构解释分歧仍须复核。
