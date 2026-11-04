(() => {
  "use strict";

  const homes = {
    customer: "/customer",
    agent: "/agent",
    admin: "/admin",
  };
  const login = { customer: "/login/customer", agent: "/login/agent", admin: "/login/admin" };
  const navigation = {
    customer: [
      ["Overview", "/customer", "dashboard"],
      ["Send money", "/customer/send-money", "send-money"],
      ["Merchant payment", "/customer/merchant-payment", "merchant-payment"],
      ["Cash out", "/customer/cash-out", "cash-out"],
      ["Transactions", "/customer/transactions", "transactions"],
      ["Profile", "/customer/profile", "profile"],
    ],
    agent: [
      ["Overview", "/agent", "dashboard"],
      ["Cash in", "/agent/cash-in", "cash-in"],
      ["Cash out", "/agent/cash-out", "cash-out"],
      ["Transactions", "/agent/transactions", "transactions"],
      ["Profile", "/agent/profile", "profile"],
    ],
    admin: [
      ["Overview", "/admin", "dashboard"],
      ["Users", "/admin/users", "users"],
      ["Transactions", "/admin/transactions", "transactions"],
      ["Analytics & integrity", "/admin/analytics", "analytics"],
      ["Profile", "/admin/profile", "profile"],
    ],
  };
  const sessionKeys = ["momo_token", "momo_user", "momo_role", "momo_expires_at", "momo_api_base"];
  const pendingKey = "momo_pending_operation";
  const $ = (selector, root = document) => root.querySelector(selector);
  const money = (minor, currency = "ZMW") =>
    new Intl.NumberFormat("en-ZM", { style: "currency", currency }).format(Number(minor || 0) / 100);
  let currentUserId = "";

  function clearSession() {
    sessionKeys.forEach((key) => sessionStorage.removeItem(key));
  }

  function userPendingKey() {
    return `${pendingKey}:${currentUserId}`;
  }

  async function request(path, options = {}) {
    const token = sessionStorage.getItem("momo_token");
    const response = await fetch(path, {
      ...options,
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
        Authorization: `Bearer ${token}`,
      },
      cache: "no-store",
    });
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) {
      clearSession();
      window.location.replace(login[document.body.dataset.requiredRole] || "/login");
    }
    if (!response.ok) {
      const error = new Error(data.error?.message || `Request failed (${response.status}).`);
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return { data, response };
  }

  function setNotice(message, tone = "") {
    const notice = $("#pageNotice");
    if (!notice) return;
    notice.textContent = message;
    notice.className = `page-notice${tone ? ` ${tone}` : ""}`;
    notice.hidden = !message;
  }

  function renderNavigation(role) {
    const nav = $("#portalNav");
    const links = navigation[role] || [];
    nav.replaceChildren(...links.map(([label, href, page]) => {
      const link = document.createElement("a");
      link.href = href;
      link.textContent = label;
      if (document.body.dataset.page === page) link.setAttribute("aria-current", "page");
      return link;
    }));
    const consoleLink = document.createElement("a");
    consoleLink.href = "/demo";
    consoleLink.textContent = "Full operations console";
    nav.append(consoleLink);
    $("#portalRole").textContent = role[0].toUpperCase() + role.slice(1);
  }

  async function loadDashboard(role) {
    const { data: walletData } = await request("/wallets?limit=50&offset=0");
    const wallets = walletData.wallets || [];
    const walletList = $("#walletList");
    if (walletList) {
      if (!wallets.length) {
        walletList.textContent = "No wallets were returned for this account.";
      } else {
        const table = document.createElement("table");
        table.className = "data-table";
        table.innerHTML = "<thead><tr><th>Wallet</th><th>Type</th><th>Status</th><th>Available balance</th></tr></thead>";
        const body = document.createElement("tbody");
        wallets.forEach((wallet) => {
          const row = document.createElement("tr");
          [wallet.id, wallet.kind, wallet.status, money(wallet.balance, wallet.currency)].forEach((value, index) => {
            const cell = document.createElement("td");
            cell.textContent = String(value ?? "—");
            if (index === 0) cell.className = "code";
            row.append(cell);
          });
          body.append(row);
        });
        table.append(body);
        walletList.replaceChildren(table);
      }
    }
    const balance = $("#walletBalance");
    if (balance) {
      balance.textContent = wallets.length
        ? wallets.map((wallet) => `${money(wallet.balance, wallet.currency)} · ${wallet.kind}`).join(" / ")
        : "No wallet available";
    }

    const { data: transactionData } = await request("/transactions?limit=10&offset=0");
    const transactionList = $("#recentTransactions");
    if (transactionList) {
      const transactions = transactionData.transactions || [];
      transactionList.replaceChildren();
      if (!transactions.length) {
        transactionList.textContent = "No recent transactions.";
      } else {
        const table = document.createElement("table");
        table.className = "data-table";
        table.innerHTML = "<thead><tr><th>Reference</th><th>Type</th><th>Status</th><th>Amount</th></tr></thead>";
        const body = document.createElement("tbody");
        transactions.forEach((transaction) => {
          const row = document.createElement("tr");
          [
            transaction.transaction_id,
            transaction.type,
            transaction.status,
            money(transaction.amount, transaction.currency),
          ].forEach((value) => {
            const cell = document.createElement("td");
            cell.textContent = String(value ?? "—");
            row.append(cell);
          });
          body.append(row);
        });
        table.append(body);
        transactionList.replaceChildren(table);
      }
    }
    if (role === "admin") await loadIntegrity();
  }

  async function loadTransactions() {
    const params = new URLSearchParams({ limit: "50", offset: "0" });
    const { data } = await request(`/transactions?${params}`);
    const transactions = data.transactions || [];
    const target = $("#transactionRows");
    if (!target) return;
    target.replaceChildren();
    if (!transactions.length) {
      target.textContent = "No transactions found.";
      return;
    }
    transactions.forEach((transaction) => {
      const row = document.createElement("tr");
      [
        transaction.transaction_id,
        transaction.type,
        `${transaction.from_wallet} → ${transaction.to_wallet}`,
        transaction.status,
        money(transaction.amount, transaction.currency),
        new Date(transaction.created_at).toLocaleString(),
      ].forEach((value, index) => {
        const cell = document.createElement("td");
        cell.textContent = String(value ?? "—");
        if (index === 0 || index === 2) cell.className = "code";
        row.append(cell);
      });
      target.append(row);
    });
  }

  async function loadIntegrity() {
    const target = $("#integrityResult");
    if (!target) return;
    try {
      const { data } = await request("/admin/integrity");
      target.textContent = JSON.stringify(data, null, 2);
      target.className = "code";
    } catch (error) {
      target.textContent = error.message;
      target.className = "page-notice error";
    }
  }

  function readPendingOperation() {
    try {
      return JSON.parse(sessionStorage.getItem(userPendingKey()) || "null");
    } catch {
      sessionStorage.removeItem(userPendingKey());
      return null;
    }
  }

  function renderPendingLookup(form, pending) {
    const button = $("button[type=submit]", form);
    button.disabled = Boolean(pending);
    if (!pending) return;
    setNotice(`Request ${pending.key} has no confirmed outcome. Check this original idempotency key before attempting another transaction.`, "warning");
    const lookup = document.createElement("button");
    lookup.type = "button";
    lookup.className = "button secondary";
    lookup.textContent = "Check original request";
    lookup.addEventListener("click", () => lookupPendingOperation(form, pending, lookup));
    $("#pageNotice").append(document.createElement("br"), lookup);
  }

  async function lookupPendingOperation(form, pending, button) {
    button.disabled = true;
    button.textContent = "Checking…";
    try {
      const query = new URLSearchParams({ type: pending.type, key: pending.key });
      const { data } = await request(`/operations?${query}`);
      const transaction = data.transaction;
      const status = transaction?.status;
      const completedWithoutTransaction = !transaction
        && data.operation_status === "COMPLETED"
        && Number(data.response_code) >= 400;
      const terminal = ["COMPLETED", "FAILED", "REJECTED", "REVERSED"].includes(status);
      if (terminal || completedWithoutTransaction) {
        sessionStorage.removeItem(userPendingKey());
        setNotice(
          `Original request status: ${status || data.operation_status}.${transaction?.transaction_id ? ` Reference: ${transaction.transaction_id}.` : ""}`,
          status === "COMPLETED" ? "" : "error",
        );
        form.reset();
        $("button[type=submit]", form).disabled = false;
        return;
      }
      setNotice(`Original request status: ${data.operation_status || status || "not confirmed"}. The request remains locked against accidental resubmission.`, "warning");
      $("#pageNotice").append(document.createElement("br"), button);
      button.disabled = false;
      button.textContent = "Check original request again";
    } catch (error) {
      setNotice(`${error.message} The original operation is still unconfirmed; do not submit a new request.`, "warning");
      $("#pageNotice").append(document.createElement("br"), button);
      button.disabled = false;
      button.textContent = "Check original request again";
    }
  }

  function createIdempotencyKey() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  async function submitTransaction(form, role) {
    const button = $("button[type=submit]", form);
    if (readPendingOperation()) {
      renderPendingLookup(form, readPendingOperation());
      return;
    }
    const type = form.dataset.transactionType;
    const endpoint = form.dataset.endpoint;
    const amountInput = form.elements.amount;
    const amount = Number(amountInput.value);
    const from = form.elements.from_wallet.value.trim();
    const to = form.elements.to_wallet.value.trim();
    if (!Number.isFinite(amount) || amount <= 0 || Math.abs(amount * 100 - Math.round(amount * 100)) > 1e-6) {
      setNotice("Enter a positive amount with no more than two decimal places.", "error");
      return;
    }
    if (!from || !to || from === to) {
      setNotice("Enter different source and destination wallet IDs.", "error");
      return;
    }
    const currency = form.elements.currency.value;
    const payload = { from_wallet: from, to_wallet: to, amount: Math.round(amount * 100), currency };
    const summary = `Operation: ${form.dataset.operationLabel}\nFrom: ${from}\nTo: ${to}\nAmount: ${money(payload.amount, currency)}`;
    if (!window.confirm(`${summary}\n\nSubmit this request to the platform?`)) return;

    const key = createIdempotencyKey();
    const pending = { type, key, owner: currentUserId };
    sessionStorage.setItem(userPendingKey(), JSON.stringify(pending));
    button.disabled = true;
    setNotice("Processing with the platform. Keep this page open while the server responds.");
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 30000);
    try {
      const { data } = await request(endpoint, {
        method: "POST",
        headers: { "Idempotency-Key": key },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      const status = data.status || "UNKNOWN";
      const reference = data.transaction_id ? ` Reference: ${data.transaction_id}.` : "";
      const terminal = ["COMPLETED", "FAILED", "REJECTED", "REVERSED"].includes(status);
      if (terminal) sessionStorage.removeItem(userPendingKey());
      setNotice(`Platform response: ${status}.${reference}${data.message ? ` ${data.message}` : ""}`,
        status === "COMPLETED" ? "" : "warning");
      if (!terminal) renderPendingLookup(form, pending);
      form.reset();
      if (role === "admin" && $("#integrityResult")) await loadIntegrity();
    } catch (error) {
      const result = error.data || {};
      const status = result.status || result.transaction?.status;
      const terminal = ["COMPLETED", "FAILED", "REJECTED", "REVERSED"].includes(status);
      if (terminal) {
        sessionStorage.removeItem(userPendingKey());
        setNotice(
          `Platform response: ${status}.${result.transaction_id ? ` Reference: ${result.transaction_id}.` : ""} ${result.message || error.message}`,
          status === "COMPLETED" ? "" : "error",
        );
      } else if (!error.status || error.name === "AbortError" || error.status >= 500
        || result.error?.code === "REQUEST_IN_PROGRESS") {
        setNotice("The outcome is not confirmed. Do not retry with a new request. Check the original idempotency key below before taking further action.", "warning");
        renderPendingLookup(form, pending);
      } else {
        sessionStorage.removeItem(userPendingKey());
        setNotice(result.error?.message || result.message || error.message, "error");
      }
    } finally {
      window.clearTimeout(timeout);
      button.disabled = Boolean(readPendingOperation());
    }
  }

  async function initialize() {
    if (document.body.dataset.publicPage === "true") return;
    const token = sessionStorage.getItem("momo_token");
    const requiredRole = document.body.dataset.requiredRole;
    if (!token) {
      window.location.replace(login[requiredRole] || "/login");
      return;
    }
    let data;
    try {
      ({ data } = await request("/auth/me"));
    } catch (error) {
      if (error.status === 401) {
        clearSession();
        window.location.replace(login[requiredRole] || "/login");
      } else {
        setNotice(`Unable to verify your session with the platform: ${error.message}`, "error");
      }
      return;
    }
    const role = data.user?.role;
    if (!navigation[role]) {
      clearSession();
      window.location.replace("/login");
      return;
    }
    if (requiredRole && role !== requiredRole) {
      window.location.replace(homes[role]);
      return;
    }
    currentUserId = data.user?.id || "";
    sessionStorage.setItem("momo_role", role);
    sessionStorage.setItem("momo_user", JSON.stringify(data.user));
    renderNavigation(role);
    $("#portalName").textContent = data.user.username || "Signed-in user";
    const title = $("#pageTitle");
    if (title) title.textContent = document.body.dataset.title || "MoMo Platform";
    if ($("#profileName")) $("#profileName").textContent = data.user.username || data.user.id || "Verified user";
    if ($("#profileRole")) $("#profileRole").textContent = role;

    const signout = $("#signout");
    signout?.addEventListener("click", async () => {
      signout.disabled = true;
      try {
        await request("/auth/logout", { method: "POST" });
      } catch (error) {
        if (error.status !== 401) {
          window.alert(`${error.message} This browser session will be cleared, but the access token may remain valid until expiry.`);
        }
      }
      clearSession();
      window.location.assign("/login");
    });

    const page = document.body.dataset.page;
    try {
      if (page === "dashboard") await loadDashboard(role);
      if (page === "transactions") await loadTransactions();
      if (page === "analytics") {
        $("#runIntegrity")?.addEventListener("click", loadIntegrity);
        await loadIntegrity();
      }
    } catch (error) {
      setNotice(error.message, "error");
    }
    const transactionForm = $("[data-transaction-type]");
    if (transactionForm) {
      const pending = readPendingOperation();
      if (pending) renderPendingLookup(transactionForm, pending);
      transactionForm.addEventListener("submit", (event) => {
        event.preventDefault();
        submitTransaction(transactionForm, role);
      });
    }
  }

  document.addEventListener("DOMContentLoaded", initialize, { once: true });
})();
