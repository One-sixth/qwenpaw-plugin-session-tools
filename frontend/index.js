/**
 * QwenPaw 会话增强工具 — 前端 UI（QwenPaw 2.x 版本）
 *
 * ★ 使用 ctx.data.created_at 时间匹配定位消息，不再依赖消息 ID 或 DOM 索引 ★
 *
 * 消息定位方案说明（2026-07-12 最终方案）：
 *   - 所有按钮统一使用 ctx.data.created_at 定位消息
 *   - requestActions（用户消息）：ctx.data = {id, created_at, input}
 *   - actions（助手消息）：ctx.data = {data: {output, created_at, completed_at, ...}}
 *   - 后端 find_message_by_created_at() 做秒级模糊匹配（补 LOCAL_TZ 本地时区）
 *
 * 已废弃方案（保留注释供参考）：
 *   - message_id 注入方案：改 envelope.py / utils.py 注入 msg.id → 前端拿不到
 *   - 文本+时间+邻居三层匹配：Markdown 渲染不一致、时间格式不匹配
 */

(function () {
  "use strict";

  const PLUGIN_NAME = "SessionTools";

  // ── Debug 开关 ─────────────────────────────────────────
  // localStorage.setItem('SessionTools.debug', 'true') 开启探查按钮 🔍📄
  // localStorage.removeItem('SessionTools.debug') 关闭后刷新
  const DEBUG = localStorage.getItem(PLUGIN_NAME + '.debug') === 'true';
  console.log("[" + PLUGIN_NAME + "] debug mode:", DEBUG);

  // ── 工具函数 ─────────────────────────────────────────────────────────

  /**
   * 获取 API URL
   * @param {string} path - API 路径
   * @returns {string} 完整的 API URL
   */
  function getApiUrl(path) {
    try {
      if (window.QwenPaw && window.QwenPaw.host && window.QwenPaw.host.getApiUrl) {
        return window.QwenPaw.host.getApiUrl(path);
      }
    } catch (_) {}
    var base = window.location.origin;
    return base + "/api" + path;
  }

  /**
   * 获取 API 认证 token
   * @returns {string} 认证 token
   */
  function getApiToken() {
    try {
      if (window.QwenPaw && window.QwenPaw.host && window.QwenPaw.host.getApiToken) {
        return window.QwenPaw.host.getApiToken();
      }
    } catch (_) {}
    return "";
  }

  /**
   * 获取当前会话 ID
   * @returns {string} 当前会话 ID
   */
  function getSessionId() {
    if (window.QwenPaw && window.QwenPaw.host && window.QwenPaw.host.getCurrentSessionId) {
      return window.QwenPaw.host.getCurrentSessionId() || "";
    }
    return window.currentSessionId || "";
  }

  /**
   * 获取当前用户和会话信息
   * @returns {Object} 包含 user_id 和 channel 的对象
   */
  function getUserSessionInfo() {
    return {
      user_id: window.currentUserId || "default",
      channel: window.currentChannel || "console",
    };
  }

  /**
   * 直接从 ctx.data.message_id 获取消息 ID
   * @deprecated 已废弃，所有按钮改用 ctx.data.created_at 定位。
   *             保留仅供调试参考，不再被任何按钮调用。
   * @param {Object} ctx - onClick 的上下文对象
   * @returns {string|null} 消息 ID 或 null
   */
  function getMessageId(ctx) {
    // ★ 调试：打印 ctx.data 的所有 key
    try {
      if (ctx && ctx.data) {
        var keys = Object.keys(ctx.data);
        var vals = {};
        keys.forEach(function(k) {
          var v = ctx.data[k];
          if (typeof v === 'string') vals[k] = v.substring(0, 40);
          else vals[k] = typeof v;
        });
        console.log('[' + PLUGIN_NAME + '] ctx.data keys:', JSON.stringify(vals));
      }
    } catch(e) {
      console.log('[' + PLUGIN_NAME + '] debug error:', e);
    }

    // 优先：ctx.data.message_id（actions 的 data 在 mg() 中已注入 message_id）
    if (ctx && ctx.data && ctx.data.message_id) {
      return ctx.data.message_id;
    }
    // requestActions 的 data: {id, created_at, input} — id 是原始 msg.id
    // actions 的 response id 是 "response_xxx" 格式，过滤掉
    if (ctx && ctx.data && ctx.data.id && typeof ctx.data.id === 'string' && ctx.data.id.indexOf('response_') !== 0) {
      return ctx.data.id;
    }
    // actions 兜底：从 output[0] 中取 message_id
    if (ctx && ctx.data && ctx.data.output && ctx.data.output[0]) {
      var msg = ctx.data.output[0];
      if (msg.message_id) return msg.message_id;
    }
    return null;
  }

  /**
   * 调用后端 API
   * @param {string} method - HTTP 方法
   * @param {string} path - API 路径
   * @param {Object} params - 查询参数
   * @returns {Promise<Object|null>} 响应数据
   */
  async function callApi(method, path, params) {
    var url = getApiUrl(path);
    var queryParts = [];
    if (params) {
      for (var key in params) {
        if (params.hasOwnProperty(key) && params[key] !== undefined && params[key] !== null && params[key] !== "") {
          queryParts.push(encodeURIComponent(key) + "=" + encodeURIComponent(String(params[key])));
        }
      }
    }
    if (queryParts.length > 0) {
      url += "?" + queryParts.join("&");
    }

    try {
      var token = getApiToken();
      var headers = { "Content-Type": "application/json" };
      if (token) {
        headers["Authorization"] = "Bearer " + token;
      }

      var resp = await fetch(url, {
        method: method,
        headers: headers,
      });
      return await resp.json();
    } catch (e) {
      console.error("[" + PLUGIN_NAME + "] API call failed:", e);
      return null;
    }
  }

  /**
   * 显示 Toast 通知
   * @param {string} text - 通知文本
   * @param {string} type - 类型: success/error/info
   */
  function showToast(text, type) {
    type = type || "info";
    try {
      if (window.QwenPaw && window.QwenPaw.host && window.QwenPaw.host.antd && window.QwenPaw.host.antd.message) {
        window.QwenPaw.host.antd.message[type](text);
        return;
      }
    } catch (_) {}
    console.log("[" + PLUGIN_NAME + "] " + type + ": " + text);
  }

  /**
   * 刷新当前页面（延迟 2 秒，让用户看到提示）
   */
  function refreshPage() {
    setTimeout(function () {
      window.location.reload();
    }, 2000);
  }

  // ── 按钮创建 ─────────────────────────────────────────────────────────

  /**
   * 创建回退按钮配置
   * ★ 使用 ctx.data.created_at 定位消息 ★
   * @returns {Object} ChatActionSpec
   */
  function makeRewindButton() {
    return {
      id: PLUGIN_NAME + ".rewind",
      icon: createIcon("⏪"),
      onClick: function (ctx) {
        if (!ctx || !ctx.data) {
          showToast("ctx.data 为空", "error");
          return;
        }

        var createdAt = ctx.data.created_at;
        if (createdAt === undefined || createdAt === null) {
          showToast("ctx.data.created_at 不存在", "error");
          return;
        }

        if (!confirm("确认要回退到该消息（删除之后所有消息）吗？")) return;

        var sessionId = getSessionId();
        if (!sessionId) {
          showToast("无法获取会话 ID", "error");
          return;
        }
        var userInfo = getUserSessionInfo();
        var params = {
          session_id: sessionId,
          user_id: userInfo.user_id,
          channel: userInfo.channel,
          created_at: String(createdAt),
        };

        callApi("POST", "/session-tools/session/" + encodeURIComponent(sessionId) + "/rewind", params)
          .then(function (result) {
            if (result && result.success) {
              showToast("⏪ 已回退", "success");
              if (result.rewound_message) {
                // 尝试将消息填入输入框
                try {
                  var inputEl = document.querySelector(".qwenpaw-sender-content");
                  if (inputEl && !inputEl.value) {
                    inputEl.value = result.rewound_message;
                    inputEl.dispatchEvent(new Event("input", { bubbles: true }));
                  }
                } catch (_) {}
              }
              refreshPage();
            } else {
              var detail = result ? (result.detail || JSON.stringify(result)) : "未知错误";
              showToast("回退失败: " + detail, "error");
            }
          });
      },
    };
  }

  /**
   * 创建分叉按钮配置
   * ★ 使用 ctx.data.created_at 定位消息 ★
   * @returns {Object} ChatActionSpec
   */
  function makeForkButton() {
    return {
      id: PLUGIN_NAME + ".fork",
      icon: createIcon("🍴"),
      onClick: function (ctx) {
        if (!ctx || !ctx.data) {
          showToast("ctx.data 为空", "error");
          return;
        }

        var createdAt = ctx.data.created_at;
        if (createdAt === undefined || createdAt === null) {
          showToast("ctx.data.created_at 不存在", "error");
          return;
        }

        if (!confirm("确认要从此处分叉新会话吗？")) return;

        var sessionId = getSessionId();
        if (!sessionId) {
          showToast("无法获取会话 ID", "error");
          return;
        }
        var userInfo = getUserSessionInfo();
        var params = {
          session_id: sessionId,
          user_id: userInfo.user_id,
          channel: userInfo.channel,
          created_at: String(createdAt),
        };

        callApi("POST", "/session-tools/session/" + encodeURIComponent(sessionId) + "/fork", params)
          .then(function (result) {
            if (result && result.success) {
              var name = result.session_name || "新会话";
              showToast("🍴 已分叉: " + name + "，2秒后刷新", "success");
              refreshPage();
            } else {
              var detail = result ? (result.detail || JSON.stringify(result)) : "未知错误";
              showToast("分叉失败: " + detail, "error");
            }
          });
      },
    };
  }

  /**
   * 创建删除按钮配置
   * ★ 使用 ctx.data.created_at 定位消息 ★
   * @returns {Object} ChatActionSpec
   */
  function makeDeleteButton() {
    return {
      id: PLUGIN_NAME + ".delete",
      icon: createIcon("🗑"),
      onClick: function (ctx) {
        if (!ctx || !ctx.data) {
          showToast("ctx.data 为空", "error");
          return;
        }

        var createdAt = ctx.data.created_at;
        if (createdAt === undefined || createdAt === null) {
          showToast("ctx.data.created_at 不存在", "error");
          return;
        }

        if (!confirm("确认要删除该轮对话（用户消息+助手回复）吗？")) return;

        var sessionId = getSessionId();
        if (!sessionId) {
          showToast("无法获取会话 ID", "error");
          return;
        }
        var userInfo = getUserSessionInfo();
        var params = {
          session_id: sessionId,
          user_id: userInfo.user_id,
          channel: userInfo.channel,
          created_at: String(createdAt),
        };

        callApi("DELETE", "/session-tools/session/" + encodeURIComponent(sessionId) + "/message", params)
          .then(function (result) {
            if (result && result.success) {
              showToast("🗑 已删除", "success");
              refreshPage();
            } else {
              var detail = result ? (result.detail || JSON.stringify(result)) : "未知错误";
              showToast("删除失败: " + detail, "error");
            }
          });
      },
    };
  }

  /**
   * 创建重新生成按钮 — 仅助手消息
   * 先 rewind 到该消息，再把最后用户消息填入输入框并触发发送
   * @returns {Object} ChatActionSpec
   */
  function makeRegenButton() {
    return {
      id: PLUGIN_NAME + ".regen",
      icon: createIcon("🔄"),
      onClick: function (ctx) {
        if (!ctx || !ctx.data) {
          showToast("ctx.data 为空", "error");
          return;
        }

        var createdAt = ctx.data.created_at;
        if (createdAt === undefined || createdAt === null) {
          showToast("ctx.data.created_at 不存在", "error");
          return;
        }

        var sessionId = getSessionId();
        if (!sessionId) {
          showToast("无法获取会话 ID", "error");
          return;
        }
        var userInfo = getUserSessionInfo();

        if (!confirm("确认要重新生成该助手消息吗？")) return;

        callApi("POST", "/session-tools/session/" + encodeURIComponent(sessionId) + "/regen", {
          session_id: sessionId,
          user_id: userInfo.user_id,
          channel: userInfo.channel,
          created_at: String(createdAt),
        }).then(function (result) {
          if (result && result.success) {
            showToast("🔄 已回退，正在重新发送...", "success");
            // 把文本填入输入框并触发发送
            // 注意：QwenPaw 输入框是 React 受控组件，需要用 native setter 技巧
            try {
              var inputEl = document.querySelector("textarea.qwenpaw-sender-input");
              if (inputEl) {
                // 使用原生 setter 绕过 React value 代理
                var nativeSetter = Object.getOwnPropertyDescriptor(
                  window.HTMLTextAreaElement.prototype, "value"
                ).set;
                nativeSetter.call(inputEl, result.rewound_message || "");
                inputEl.dispatchEvent(new Event("input", { bubbles: true }));

                // 轮询等待发送按钮可用，最多等 1 秒
                var maxAttempts = 10;
                var attempt = 0;
                (function tryClick() {
                  var sendBtn = document.querySelector("button.qwenpaw-sender-actions-btn");
                  if (sendBtn && !sendBtn.disabled) {
                    sendBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
                    setTimeout(refreshPage, 1500);
                  } else if (attempt < maxAttempts) {
                    attempt++;
                    setTimeout(tryClick, 100);
                  } else {
                    console.log("[" + PLUGIN_NAME + "] 发送按钮在 1 秒内未就绪");
                  }
                })();
              }
            } catch (_) {}
          } else {
            var detail = result ? (result.detail || JSON.stringify(result)) : "未知错误";
            showToast("重新生成失败: " + detail, "error");
          }
        });
      },
    };
  }

  /**
   * 创建检查按钮 — 用 created_at 匹配会话文件中的消息并展示完整 JSON
   * @returns {Object} ChatActionSpec
   */
  function makeInspectButton() {
    return {
      id: PLUGIN_NAME + ".inspect",
      icon: createIcon("📄"),
      onClick: function (ctx) {
        if (!ctx || !ctx.data) {
          showToast("ctx.data 为空", "error");
          return;
        }

        var createdAt = ctx.data.created_at;
        if (createdAt === undefined || createdAt === null) {
          showToast("ctx.data.created_at 不存在", "error");
          return;
        }

        var sessionId = getSessionId();
        if (!sessionId) {
          showToast("无法获取会话 ID", "error");
          return;
        }
        var userInfo = getUserSessionInfo();

        // 调后端 API
        callApi("GET", "/session-tools/session/" + encodeURIComponent(sessionId) + "/message", {
          session_id: sessionId,
          user_id: userInfo.user_id,
          channel: userInfo.channel,
          created_at: String(createdAt),
        }).then(function (result) {
          if (result && result.success) {
            var msgStr = JSON.stringify(result.message, null, 2);
            // 打开新页签展示
            try {
              var win = window.open('', '_blank');
              if (win) {
                win.document.write('<!DOCTYPE html><html><head><meta charset="utf-8"><title>📄 消息 JSON</title>');
                win.document.write('<style>');
                win.document.write('body{background:#1a1a2e;color:#e0e0e0;font-family:Consolas,monospace;font-size:13px;line-height:1.6;padding:24px;margin:0;}');
                win.document.write('pre{white-space:pre-wrap;word-break:break-all;margin:0;}');
                win.document.write('h2{color:#ffd700;margin-top:0;}');
                win.document.write('.meta{color:#888;font-size:12px;margin-bottom:16px;}');
                win.document.write('</style></head><body>');
                win.document.write('<h2>📄 消息 JSON</h2>');
                win.document.write('<div class="meta">index: ' + result.index + '</div>');
                win.document.write('<hr style="border-color:#444">');
                win.document.write('<pre>' + escapeHtml(msgStr) + '</pre>');
                win.document.write('</body></html>');
                win.document.close();
                return;
              }
            } catch (_) {}
            // 兜底
            console.log('[' + PLUGIN_NAME + '] message JSON:', msgStr);
            alert('消息 JSON 已打印到控制台');
          } else {
            var detail = result ? (result.detail || JSON.stringify(result)) : "未知错误";
            showToast("获取消息失败: " + detail, "error");
          }
        });
      },
    };
  }

  /**
   * 深度探测 ctx.data：展开所有 key，把对象/数组序列化展示
   */
  function deepProbe(data) {
    if (!data || typeof data !== 'object') return { value: String(data) };
    var result = {};
    var seen = new WeakSet();
    (function walk(obj, prefix) {
      if (!obj || typeof obj !== 'object') { result[prefix] = String(obj); return; }
      if (seen.has(obj)) { result[prefix] = '[Circular]'; return; }
      seen.add(obj);
      var keys = Object.keys(obj);
      for (var i = 0; i < keys.length; i++) {
        var k = keys[i];
        var v = obj[k];
        var fullKey = prefix ? prefix + '.' + k : k;
        if (v === null) {
          result[fullKey] = 'null';
        } else if (Array.isArray(v)) {
          result[fullKey] = 'Array[' + v.length + ']';
          if (v.length > 0 && typeof v[0] === 'object' && v[0] !== null) {
            walk(v[0], fullKey + '[0]');
          } else if (v.length > 0) {
            result[fullKey + '[0]'] = String(v[0]);
          }
        } else if (typeof v === 'object') {
          result[fullKey] = typeof v;
          walk(v, fullKey);
        } else {
          result[fullKey] = String(v);
        }
      }
    })(data, '');
    return result;
  }

  /**
   * 创建调试按钮 — 显示完整 ctx.data
   * @returns {Object} ChatActionSpec
   */
  function makeDebugButton() {
    return {
      id: PLUGIN_NAME + ".debug",
      icon: createIcon("🔍"),
      onClick: function (ctx) {
        if (!ctx || !ctx.data) {
          showToast("ctx.data 为空", "error");
          return;
        }

        // 展平探测
        var flat = deepProbe(ctx.data);
        var lines = [];
        var keys = Object.keys(flat);
        for (var i = 0; i < keys.length; i++) {
          var k = keys[i];
          var label = k ? k : '(data)';
          lines.push(label + ' => ' + flat[k]);
        }
        var detail = lines.join('\n');

        // 打开新页签显示完整结果
        try {
          var win = window.open('', '_blank');
          if (win) {
            win.document.write('<!DOCTYPE html><html><head><meta charset="utf-8"><title>🔍 ctx.data 探测结果</title>');
            win.document.write('<style>');
            win.document.write('body{background:#1a1a2e;color:#e0e0e0;font-family:Consolas,monospace;font-size:13px;line-height:1.6;padding:24px;margin:0;}');
            win.document.write('pre{white-space:pre-wrap;word-break:break-all;margin:0;}');
            win.document.write('h2{color:#ffd700;margin-top:0;}');
            win.document.write('.key{color:#7ec8e3;}.sep{color:#888;}.val{color:#a8d8a8;}');
            win.document.write('</style></head><body>');
            win.document.write('<h2>🔍 ctx.data 探测结果</h2><hr style="border-color:#444">');
            win.document.write('<pre>');
            for (var j = 0; j < keys.length; j++) {
              var k2 = keys[j];
              var label2 = k2 ? k2 : '(data)';
              win.document.write('<span class="key">' + escapeHtml(label2) + '</span><span class="sep"> => </span><span class="val">' + escapeHtml(flat[k2]) + '</span>\n');
            }
            win.document.write('</pre>');
            win.document.write('</body></html>');
            win.document.close();
            return;
          }
        } catch (_) {}

        // 兜底：新窗口被拦截，退回到 console + alert
        console.log('[' + PLUGIN_NAME + '] ctx.data probe:', JSON.stringify(flat, null, 2));
        alert(detail);
      },
    };
  }

  /**
   * 转义 HTML 特殊字符，防止注入
   * @param {string} str - 原始字符串
   * @returns {string} 转义后的字符串
   */
  function escapeHtml(str) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  /**
   * 创建图标元素 — 使用 React.createElement 生成 ReactElement
   * @param {string} emoji - 图标 emoji
   * @returns {React.ReactElement}
   */
  function createIcon(emoji) {
    var React = window.QwenPaw && window.QwenPaw.host && window.QwenPaw.host.React;
    if (React) {
      return React.createElement('span', {
        style: { fontSize: '14px', lineHeight: '1', cursor: 'pointer' }
      }, emoji);
    }
    // 兜底（不应走到这里）
    var span = document.createElement("span");
    span.textContent = emoji;
    span.style.fontSize = "14px";
    span.style.lineHeight = "1";
    span.style.cursor = "pointer";
    return span;
  }

  // ── 注册按钮 ─────────────────────────────────────────────────────────

  function registerButtons() {
    var chat = window.QwenPaw && window.QwenPaw.chat;
    if (!chat || !chat.requestActions) {
      console.warn("[" + PLUGIN_NAME + "] QwenPaw chat API not ready, retrying...");
      setTimeout(registerButtons, 500);
      return;
    }

    try {
      // 用户消息气泡按钮：⏪ 🍴
      chat.requestActions.add(PLUGIN_NAME, makeRewindButton());
      chat.requestActions.add(PLUGIN_NAME, makeForkButton());
      if (DEBUG) {
        chat.requestActions.add(PLUGIN_NAME, makeDebugButton());
        chat.requestActions.add(PLUGIN_NAME, makeInspectButton());
      }
      // 助手消息气泡按钮：🗑 🔄 ⏪ 🍴
      chat.actions.add(PLUGIN_NAME, makeDeleteButton());
      chat.actions.add(PLUGIN_NAME, makeRegenButton());
      chat.actions.add(PLUGIN_NAME, makeRewindButton());
      chat.actions.add(PLUGIN_NAME, makeForkButton());
      if (DEBUG) {
        chat.actions.add(PLUGIN_NAME, makeDebugButton());
        chat.actions.add(PLUGIN_NAME, makeInspectButton());
      }
      console.log("[" + PLUGIN_NAME + "] 按钮注册完成" + (DEBUG ? " (debug 模式)" : ""));
    } catch (e) {
      console.error("[" + PLUGIN_NAME + "] 注册按钮失败:", e);
    }
  }

  // ── 初始化 ───────────────────────────────────────────────────────────

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", registerButtons);
  } else {
    registerButtons();
  }

  // ====================================================================
  // 以下是被新的 message_id 方案替代的旧版文本匹配代码
  // 全部注释保留，方便以后参考
  // ====================================================================
  /*
  const NEIGHBOR_COUNT = 2;

  function extractBubbleInfo(bubbleEl) {
    // ... 从 DOM 提取文本、时间、role
  }

  function collectBubbleAndNeighbors() {
    // ... 收集当前气泡 + 前后邻居信息
  }

  function buildApiParams(info, sessionId, userInfo) {
    // ... 构建包含文本、时间、邻居的 API 参数
  }
  */

})();