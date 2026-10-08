"""/debug 日志查看页（内嵌 HTML，零静态文件依赖；设计=log\\debug-endpoint\\方案设计-v1.md §D7）。

安全纪律（评审甲-必须修改项1）：
- 日志行一律经 ``element.textContent`` 渲染，绝不拼接 innerHTML——
  query.py trace 中间件会把无鉴权请求的 url.path 写入日志，若日志行
  含 ``<img onerror>`` 类 payload 且页面 innerHTML 渲染，将在管理员
  浏览器执行并读走 localStorage 中的 DEBUG_TOKEN（stored-XSS 链路）。
- token 仅存 localStorage，仅经 X-Debug-Token 请求头发送，不进 URL。
"""

from __future__ import annotations

DEBUG_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>news-flash-dedup · 日志 Debug</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background: #f5f7fa; color: #333; padding: 16px; }
  .bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; background: #fff; border-radius: 8px; padding: 12px 16px; box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 12px; }
  .bar label { font-size: 13px; color: #555; font-weight: 600; }
  input, select { padding: 6px 10px; border: 1px solid #ddd; border-radius: 6px; font-size: 13px; }
  input:focus, select:focus { border-color: #4f6df5; outline: none; }
  #token { width: 260px; }
  #q { width: 220px; }
  button { padding: 7px 16px; border: none; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; background: #4f6df5; color: #fff; }
  button:hover { background: #3d5de0; }
  #status { font-size: 12px; color: #888; }
  #status.err { color: #e74c3c; }
  #log { background: #1a1a2e; color: #d4d4d4; border-radius: 8px; padding: 12px 16px; font-family: Consolas, "Courier New", monospace; font-size: 12.5px; line-height: 1.55; white-space: pre-wrap; word-break: break-all; min-height: 300px; max-height: 75vh; overflow-y: auto; }
  #log .lv-error { color: #ff6b6b; font-weight: 600; }
  #log .lv-warning { color: #f0b429; }
  .hint { font-size: 12px; color: #999; margin-bottom: 8px; }
</style>
</head>
<body>
<div class="bar">
  <label>Token</label><input id="token" type="password" placeholder="X-Debug-Token">
  <label>日志文件</label><select id="file"></select>
  <label>行数</label><select id="lines">
    <option>50</option><option selected>100</option><option>200</option>
    <option>500</option><option>1000</option>
  </select>
  <label>过滤</label><input id="q" type="text" placeholder="子串过滤，如 ERROR（窗口内）">
  <button id="refresh">刷新</button>
  <label><input id="auto" type="checkbox" style="width:auto"> 自动(5s)</label>
  <span id="status"></span>
</div>
<div class="hint">过滤语义：先取日志末尾 N 行，再在窗口内做子串过滤（大小写不敏感）。</div>
<div id="log"></div>
<script>
"use strict";
var $ = function (id) { return document.getElementById(id); };
var tokenEl = $("token"), fileEl = $("file"), linesEl = $("lines"),
    qEl = $("q"), statusEl = $("status"), logEl = $("log"), autoEl = $("auto");
var timer = null;

tokenEl.value = localStorage.getItem("debug_token") || "";
tokenEl.addEventListener("change", function () {
  localStorage.setItem("debug_token", tokenEl.value);
});

function setStatus(msg, isErr) {
  statusEl.textContent = msg;
  statusEl.className = isErr ? "err" : "";
}

function headers() { return { "X-Debug-Token": tokenEl.value }; }

function loadFiles() {
  return fetch("/debug/api/logs", { headers: headers() }).then(function (r) {
    if (r.status === 403) { setStatus("token 无效或未配置", true); return; }
    if (!r.ok) { setStatus("logs 列表失败: HTTP " + r.status, true); return; }
    return r.json().then(function (data) {
      var cur = fileEl.value;
      fileEl.innerHTML = "";
      (data.files || []).forEach(function (f) {
        fileEl.add(new Option(f.name + " (" + f.size + "B)", f.name));
      });
      if (cur) fileEl.value = cur;
      if (!fileEl.value && fileEl.options.length) fileEl.selectedIndex = 0;
    });
  }).catch(function (e) { setStatus("网络错误: " + e.message, true); });
}

var LEVEL_RE = /\\b(ERROR|CRITICAL)\\b/i;
var WARN_RE = /\\bWARNING\\b/i;

function render(lines, truncated) {
  logEl.innerHTML = "";
  lines.forEach(function (line) {
    var div = document.createElement("div");
    div.textContent = line;              // XSS 防线：绝不 innerHTML 拼接
    if (LEVEL_RE.test(line)) div.className = "lv-error";
    else if (WARN_RE.test(line)) div.className = "lv-warning";
    logEl.appendChild(div);
  });
  if (truncated) {
    var mark = document.createElement("div");
    mark.textContent = "…（响应过大已截断，仅显示末尾部分）…";
    mark.className = "lv-warning";
    logEl.insertBefore(mark, logEl.firstChild);
  }
  logEl.scrollTop = logEl.scrollHeight;
}

function refresh() {
  var params = new URLSearchParams();
  if (fileEl.value) params.set("file", fileEl.value);
  params.set("lines", linesEl.value);
  if (qEl.value.trim()) params.set("q", qEl.value.trim());
  return fetch("/debug/api/log?" + params.toString(), { headers: headers() }).then(function (r) {
    if (r.status === 403) { setStatus("token 无效或未配置", true); render([], false); return; }
    if (!r.ok) { setStatus("读取失败: HTTP " + r.status, true); return; }
    return r.json().then(function (data) {
      setStatus("更新于 " + new Date().toLocaleTimeString() +
                " · 窗口 " + data.total_in_window + " 行 · 命中 " + data.lines.length + " 行", false);
      render(data.lines || [], !!data.truncated);
    });
  }).catch(function (e) { setStatus("网络错误: " + e.message, true); });
}

$("refresh").addEventListener("click", refresh);
qEl.addEventListener("keydown", function (e) { if (e.key === "Enter") refresh(); });
fileEl.addEventListener("change", refresh);
linesEl.addEventListener("change", refresh);
autoEl.addEventListener("change", function () {
  if (timer) { clearInterval(timer); timer = null; }
  if (autoEl.checked) timer = setInterval(refresh, 5000);
});

loadFiles().then(refresh);
</script>
</body>
</html>
"""

__all__ = ["DEBUG_PAGE_HTML"]
