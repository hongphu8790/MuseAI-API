(() => {
  "use strict";
  const el = (id) => document.getElementById(id);
  const list = el("accountList");
  const statusEl = el("authDialogStatus");
  const otpArea = el("otpArea");
  const otpSelect = el("otpAccountSelect");
  const poolHint = el("poolHint");
  let accounts = [];
  let refreshing = false;

  const api = async (url, options) => {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || ("HTTP " + response.status));
    return data;
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function accountStatus(account) {
    const labels = {
      ready: "Sẵn sàng",
      stored: "Đã lưu · chưa xác minh",
      needs_login: "Cần đăng nhập",
      sending_otp: "Đang gửi OTP",
      pending_otp: "Chờ OTP",
      error: "Lỗi phiên"
    };
    return labels[account.status] || account.status;
  }

  function render() {
    list.replaceChildren();
    const ready = accounts.filter(a => a.enabled && a.status === "ready");
    const free = ready.filter(a => !a.busy);
    const waiting = accounts.filter(a => ["pending_otp", "sending_otp"].includes(a.status));
    statusEl.textContent = ready.length + "/" + accounts.length + " tài khoản sẵn sàng";
    poolHint.textContent = "Pool: " + free.length + " tài khoản rảnh / " +
      ready.length + " sẵn sàng. Batch N task dùng N tài khoản khác nhau, chọn ngẫu nhiên.";

    const pill = el("authPill");
    pill.className = "pill auth-trigger " + (ready.length ? "ok" : "bad");
    pill.querySelector("span:last-child").textContent = ready.length
      ? ready.length + " TK sẵn sàng" : "Quản lý tài khoản";

    otpSelect.replaceChildren();
    for (const account of waiting) {
      const option = element("option", "", account.label);
      option.value = account.id;
      otpSelect.appendChild(option);
    }
    otpArea.classList.toggle("hidden", waiting.length === 0);

    if (!accounts.length) {
      list.appendChild(element("p", "muted small", "Chưa có tài khoản Muse. Thêm email để nhận OTP."));
    }

    for (const account of accounts) {
      const item = element("div", "account-item");
      const title = element("div", "account-item-head");
      const name = element("strong", "", account.label || account.email || "Muse");
      const badge = element("span", "account-status " + account.status, accountStatus(account));
      title.append(name, badge);
      const detail = element("p", "muted small",
        (account.busy ? "Đang chạy task · " : "") +
        (account.enabled ? "Đã bật" : "Tạm tắt") +
        (account.last_error ? " · " + account.last_error : ""));
      const actions = element("div", "account-actions");

      const makeButton = (label, action) => {
        const button = element("button", "ghost account-action", label);
        button.type = "button";
        button.dataset.action = action;
        button.dataset.id = account.id;
        button.disabled = Boolean(account.busy);
        actions.appendChild(button);
      };
      makeButton("Kiểm tra", "verify");
      makeButton(account.proxy_enabled ? "Đổi proxy" : "+ Proxy", "proxy");
      if (account.proxy_enabled) {
        makeButton("Xóa proxy", "clear_proxy");
        detail.textContent += " · Proxy: " + (account.proxy_label || "đã bật");
      }
      if (account.email && account.status !== "ready") {
        makeButton("Gửi lại OTP", "resend");
      }
      makeButton(account.enabled ? "Tạm tắt" : "Bật lại", "toggle");
      makeButton("Xóa", "remove");
      item.append(title, detail, actions);
      list.appendChild(item);
    }
  }

  async function refreshAccounts(refresh = false) {
    if (refreshing) return;
    refreshing = true;
    try {
      const data = await api("/api/accounts" + (refresh ? "?refresh=true" : ""));
      accounts = data.accounts || [];
      render();
      window.dispatchEvent(new CustomEvent("muse-accounts-updated", {
        detail: accounts
      }));
    } catch (error) {
      el("authMessage").textContent = "Không tải được danh sách tài khoản: " + error.message;
      el("authMessage").className = "message error";
    } finally {
      refreshing = false;
    }
  }

  window.refreshMuseAccounts = refreshAccounts;

  list.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const account = accounts.find(a => a.id === button.dataset.id);
    if (!account) return;
    const action = button.dataset.action;
    if (action === "remove" &&
        !window.confirm("Xóa tài khoản " + account.label +
          " khỏi pool và xóa session đã lưu trên máy?")) return;
    button.disabled = true;
    const msg = el("authMessage");
    msg.textContent = "";
    try {
      const url = "/api/accounts/" + encodeURIComponent(account.id);
      if (action === "proxy" || action === "clear_proxy") {
        let value = null;
        if (action === "proxy") {
          value = window.prompt(
            "Proxy cho " + account.label + " (http://host:port hoặc socks5://host:port). Để trống để hủy:",
            ""
          );
          if (!value || !value.trim()) return;
        }
        await api(url + "/proxy", {
          method: "PUT",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({proxy_url: value})
        });
      } else if (action === "verify") {
        await api(url + "/verify", {method: "POST"});
      } else if (action === "resend") {
        await api(url + "/resend", {method: "POST"});
      } else if (action === "toggle") {
        await api(url, {
          method: "PATCH",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({enabled: !account.enabled})
        });
      } else if (action === "remove") {
        await api(url, {method: "DELETE"});
      }
      await refreshAccounts();
    } catch (error) {
      msg.textContent = error.message;
      msg.className = "message error";
      button.disabled = false;
    }
  });

  el("refreshAccounts").addEventListener("click", () => refreshAccounts(true));
  refreshAccounts(true);
  // Refresh lightweight metadata while jobs are running; verification is manual.
  setInterval(() => refreshAccounts(false), 12000);
})();
