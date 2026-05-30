/**
 * QwenPaw 会话增强工具 — 前端 UI
 *
 * 在每个 Chat 消息底部注入 回退 / 分支 / 删除 按钮，
 * 通过 HTTP API 调用后端 plugin.py 中的会话操作。
 *
 * 设计原则：
 * - 不修改 QwenPaw 核心前端代码
 * - 通过 MutationObserver 监听 DOM，在 React 渲染后注入按钮
 * - 使用 window.QwenPaw.host 获取 React、antd、API 工具
 * - 所有后端调用走 /api/session-tools/ 路由
 */

(function () {
  "use strict";

  // ── 配置 ─────────────────────────────────────────────────────────────

  const PLUGIN_NAME = "SessionTools";
  const CHECK_INTERVAL = 1000; // 主检查周期 (ms)
  const MESSAGE_OBSERVE_DELAY = 300; // 等待 React 渲染完成

  // ── 工具函数 ─────────────────────────────────────────────────────────

  function getApiUrl(path) {
    try {
      if (window.QwenPaw && window.QwenPaw.host && window.QwenPaw.host.getApiUrl) {
        return window.QwenPaw.host.getApiUrl(path);
      }
    } catch (_) {}
    // fallback: 直接从 window.location 构造
    var base = window.location.origin;
    return base + "/api" + path;
  }

  function getApiToken() {
    try {
      if (window.QwenPaw && window.QwenPaw.host && window.QwenPaw.host.getApiToken) {
        return window.QwenPaw.host.getApiToken();
      }
    } catch (_) {}
    return "";
  }

  function getSessionId() {
    return window.currentSessionId || "";
  }

  function getUserSessionInfo() {
    return {
      user_id: window.currentUserId || "default",
      channel: window.currentChannel || "console",
    };
  }

  async function callApi(method, path, params) {
    var url = getApiUrl(path);
    var query = [];
    if (params) {
      for (var key in params) {
        if (params.hasOwnProperty(key) && params[key] !== undefined) {
          query.push(encodeURIComponent(key) + "=" + encodeURIComponent(params[key]));
        }
      }
    }
    if (query.length > 0) {
      url += "?" + query.join("&");
    }

    var token = getApiToken();
    var headers = { "Content-Type": "application/json" };
    if (token) {
      headers["Authorization"] = "Bearer " + token;
    }

    try {
      var resp = await fetch(url, {
        method: method,
        headers: headers,
      });
      if (!resp.ok) {
        var errText = await resp.text().catch(function () { return ""; });
        console.error("[" + PLUGIN_NAME + "] API error", resp.status, errText);
        return null;
      }
      return await resp.json();
    } catch (e) {
      console.error("[" + PLUGIN_NAME + "] fetch error", e);
      return null;
    }
  }

  function createButton(label, icon, className, onClick) {
    var btn = document.createElement("button");
    btn.textContent = icon + " " + label;
    btn.className = "qps-btn " + (className || "");
    btn.title = label;
    btn.style.cssText =
      "background:none;border:none;cursor:pointer;color:var(--text-color,#888);" +
      "font-size:12px;padding:2px 6px;border-radius:4px;transition:all .15s;" +
      "opacity:0.6;line-height:1.4;white-space:nowrap;";
    btn.addEventListener("mouseenter", function () {
      btn.style.opacity = "1";
      btn.style.color = "var(--primary-color,#1677ff)";
      btn.style.background = "var(--hover-bg,rgba(0,0,0,0.04))";
    });
    btn.addEventListener("mouseleave", function () {
      btn.style.opacity = "0.6";
      btn.style.color = "var(--text-color,#888)";
      btn.style.background = "transparent";
    });
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      onClick(e);
    });
    return btn;
  }

  // ── 会话变更监听 ─────────────────────────────────────────────────────

  var _lastSessionId = null;

  function checkSessionChange() {
    var sid = getSessionId();
    if (sid && sid !== _lastSessionId) {
      _lastSessionId = sid;
      console.log("[" + PLUGIN_NAME + "] 会话已切换:", sid);
      // 等待 DOM 渲染后注入按钮
      setTimeout(injectButtonsToAllMessages, MESSAGE_OBSERVE_DELAY);
    }
  }

  // ── 按钮注入逻辑 ─────────────────────────────────────────────────────

  var _injectionCount = 0;

  function getMessageIndex(footerEl) {
    // 使用 [data-role] 精确定位气泡容器，避免 [class*="bubble"]
    // 误匹配 footer 自身和列表容器的 bug
    var bubbleEl = footerEl.closest('[data-role]') || footerEl.parentElement;
    if (!bubbleEl) return -1;

    // 查找消息列表容器
    var chatContainer = document.querySelector('[class*="chat-anywhere-message-list"]') ||
                        document.querySelector('[class*="message-list"]');

    // 只查 [data-role] 元素 = 真正的消息气泡，排除列表容器/bubble-footer等
    var allBubbles;
    if (chatContainer) {
      allBubbles = chatContainer.querySelectorAll('[data-role]');
    } else {
      // fallback
      allBubbles = document.querySelectorAll('[data-role]');
    }

    for (var i = 0; i < allBubbles.length; i++) {
      if (allBubbles[i] === bubbleEl || allBubbles[i].contains(bubbleEl)) {
        return i;
      }
    }
    return -1;
  }

  function injectButtons(footerEl) {
    if (!footerEl || footerEl.querySelector(".qps-btn")) {
      return; // 已注入
    }

    var msgIndex = getMessageIndex(footerEl);
    if (msgIndex < 0) return;

    var sessionId = getSessionId();
    if (!sessionId) return;

    var info = getUserSessionInfo();

    // 创建按钮容器
    var actionBar = document.createElement("div");
    actionBar.className = "qps-actions";
    actionBar.style.cssText =
      "display:inline-flex;align-items:center;gap:2px;margin-left:4px;";

    // 回退按钮
    actionBar.appendChild(createButton("", "⏪", "qps-rewind", function () {
      handleRewind(footerEl, sessionId, info, msgIndex);
    }));

    // 分支按钮
    actionBar.appendChild(createButton("", "🍴", "qps-fork", function () {
      handleFork(sessionId, info, msgIndex);
    }));

    // 删除按钮
    actionBar.appendChild(createButton("", "🗑", "qps-delete", function () {
      handleDelete(footerEl, sessionId, info, msgIndex);
    }));

    // 注入到 footer 的 actions 区域
    var actionsContainer = footerEl.querySelector('[class*="actions"]');
    if (actionsContainer) {
      actionsContainer.appendChild(actionBar);
    } else {
      footerEl.appendChild(actionBar);
    }
  }

  function injectButtonsToAllMessages() {
    if (_injectionCount > 200) {
      // 防止无限循环
      return;
    }
    _injectionCount++;

    var footers = document.querySelectorAll('[class*="bubble-footer"]');
    footers.forEach(function (footer) {
      injectButtons(footer);
    });
  }

  // ── 按钮事件处理 ─────────────────────────────────────────────────────

  async function handleRewind(footerEl, sessionId, info, msgIndex) {
    var result = await callApi("POST", "/session-tools/session/" + encodeURIComponent(sessionId) + "/rewind", {
      to_message_index: msgIndex,
      user_id: info.user_id,
      channel: info.channel,
    });

    if (result && result.success) {
      var rounds = result.rewound_rounds || 0;
      showToast("⏪ 已回退 " + rounds + " 轮对话，刷新页面生效");
      setTimeout(function () { window.location.reload(); }, 1500);
    } else {
      showToast("❌ 回退失败");
    }
  }

  async function handleFork(sessionId, info, msgIndex) {
    var result = await callApi("POST", "/session-tools/session/" + encodeURIComponent(sessionId) + "/fork", {
      at_message_index: msgIndex,
      user_id: info.user_id,
      channel: info.channel,
    });

    if (result && result.success) {
      showToast("🍴 已分叉新会话: " + (result.fork_name || result.new_session_id));

      // 跳转到新会话
      var newId = result.new_session_id;
      if (newId) {
        // 先设 sessionId 再刷新
        window.currentSessionId = newId;
        setTimeout(function () { window.location.reload(); }, 1000);
      }
    } else {
      showToast("❌ 分叉失败");
    }
  }

  async function handleDelete(footerEl, sessionId, info, msgIndex) {
    // 确认对话框
    if (!window.confirm("确定要删除这条消息吗？")) return;

    var result = await callApi("DELETE", "/session-tools/session/" + encodeURIComponent(sessionId) + "/message/" + msgIndex, {
      user_id: info.user_id,
      channel: info.channel,
    });

    if (result && result.success) {
      showToast("🗑 已删除消息，刷新页面生效");
      setTimeout(function () { window.location.reload(); }, 1500);
    } else {
      showToast("❌ 删除失败");
    }
  }

  // ── Toast 通知 ───────────────────────────────────────────────────────

  function showToast(message) {
    // 优先使用 antd message API
    try {
      if (window.QwenPaw && window.QwenPaw.host && window.QwenPaw.host.antd) {
        var antd = window.QwenPaw.host.antd;
        if (antd.message && antd.message.info) {
          antd.message.info(message);
          return;
        }
      }
    } catch (_) {}

    // fallback: 原生 toast
    var toast = document.createElement("div");
    toast.textContent = message;
    toast.style.cssText =
      "position:fixed;bottom:80px;left:50%;transform:translateX(-50%);" +
      "background:rgba(0,0,0,0.8);color:#fff;padding:8px 20px;border-radius:8px;" +
      "z-index:99999;font-size:14px;max-width:80vw;text-align:center;" +
      "transition:opacity .3s;";
    document.body.appendChild(toast);
    setTimeout(function () {
      toast.style.opacity = "0";
      setTimeout(function () { toast.remove(); }, 300);
    }, 2500);
  }

  // ── MutationObserver ─────────────────────────────────────────────────

  function startObserver() {
    var target = document.body || document.documentElement;

    var observer = new MutationObserver(function (mutations) {
      var shouldInject = false;
      for (var i = 0; i < mutations.length; i++) {
        var mutation = mutations[i];
        if (mutation.type === "childList" && mutation.addedNodes.length > 0) {
          for (var j = 0; j < mutation.addedNodes.length; j++) {
            var node = mutation.addedNodes[j];
            if (node.nodeType === 1) {
              // 检查是否有消息相关的 class
              var cls = node.className || "";
              if (typeof cls === "string" &&
                  (cls.indexOf("bubble") >= 0 ||
                   cls.indexOf("chat") >= 0 ||
                   cls.indexOf("message") >= 0)) {
                shouldInject = true;
                break;
              }
            }
          }
        }
        if (shouldInject) break;
      }

      if (shouldInject) {
        setTimeout(injectButtonsToAllMessages, MESSAGE_OBSERVE_DELAY);
      }
    });

    observer.observe(target, {
      childList: true,
      subtree: true,
    });
  }

  // ── 初始化 ───────────────────────────────────────────────────────────

  function init() {
    console.log("[" + PLUGIN_NAME + "] 前端 UI 已加载");

    // 定期检查会话变更
    setInterval(checkSessionChange, CHECK_INTERVAL);

    // 立即检查一次
    setTimeout(checkSessionChange, 500);

    // 启动 DOM 观察
    setTimeout(startObserver, 1000);

    // 初次注入（等 React 完全渲染）
    setTimeout(injectButtonsToAllMessages, 2000);
    setTimeout(injectButtonsToAllMessages, 4000);
  }

  // 等待 QwenPaw host 就绪
  function waitForHost() {
    if (window.QwenPaw && window.QwenPaw.host) {
      init();
    } else {
      setTimeout(waitForHost, 500);
    }
  }

  waitForHost();
})();
