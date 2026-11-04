(() => {
  "use strict";

  const sessionKeys = ["momo_token", "momo_user", "momo_role", "momo_expires_at", "momo_api_base"];
  const loginPaths = {
    customer: "/login/customer",
    agent: "/login/agent",
    admin: "/login/admin",
  };
  const dashboardPaths = {
    customer: "/customer",
    agent: "/agent",
    admin: "/admin",
  };
  let allowedViews = null;

  function clearSession() {
    sessionKeys.forEach((key) => sessionStorage.removeItem(key));
  }

  async function post(endpoint, payload) {
    let response;
    try {
      response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        cache: "no-store",
      });
    } catch {
      throw new Error("Unable to reach the authentication service. Check the connection and try again.");
    }

    let data = {};
    try {
      data = await response.json();
    } catch {
      throw new Error("The authentication service returned an invalid response.");
    }
    if (!response.ok) {
      throw new Error(data.error?.message || `Authentication failed (${response.status}).`);
    }
    return data;
  }

  function complete(data, expectedRole) {
    if (!data.token || !data.user || data.user.role !== expectedRole) {
      throw new Error("The authentication service returned an unexpected role.");
    }
    const expiresIn = Number(data.expires_in);
    if (!Number.isFinite(expiresIn) || expiresIn <= 0) {
      throw new Error("The authentication service returned an invalid session lifetime.");
    }
    sessionStorage.setItem("momo_token", data.token);
    sessionStorage.setItem("momo_user", JSON.stringify(data.user));
    sessionStorage.setItem("momo_role", expectedRole);
    sessionStorage.setItem("momo_expires_at", String(Date.now() + expiresIn * 1000));
    sessionStorage.setItem("momo_api_base", window.location.origin);
    window.location.assign(dashboardPaths[expectedRole]);
  }

  function showError(element, error) {
    element.textContent = error instanceof Error ? error.message : "Authentication failed. Try again.";
    element.className = "alert show error";
  }

  function roleFor(groups) {
    if (groups.includes("admins")) return "admin";
    if (groups.includes("agents")) return "agent";
    if (groups.includes("merchants")) return "merchant";
    if (groups.includes("customers")) return "customer";
    return null;
  }

  function dashboardSession() {
    const pathRole = ({ "/customer": "customer", "/agent": "agent", "/admin": "admin" })[
      window.location.pathname.replace(/\/+$/, "") || "/"
    ];
    const token = sessionStorage.getItem("momo_token");
    const role = sessionStorage.getItem("momo_role");
    const expiresAt = Number(sessionStorage.getItem("momo_expires_at"));
    const hasValidLocalSession = Boolean(token && Number.isFinite(expiresAt) && expiresAt > Date.now());

    if (pathRole && !hasValidLocalSession) {
      window.location.replace(loginPaths[pathRole]);
      return;
    }
    if (!token || !role || !Number.isFinite(expiresAt)) return;
    if (expiresAt <= Date.now()) {
      clearSession();
      if (pathRole) window.location.replace(loginPaths[pathRole]);
      return;
    }

    const apiBase = sessionStorage.getItem("momo_api_base") || window.location.origin;
    fetch(`${apiBase}/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    })
      .then(async (response) => {
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error?.message || "Your session has expired.");
        const actualRole = data.user?.role || roleFor(data.user?.groups || []);
        if (!["customer", "agent", "admin", "merchant"].includes(actualRole)) {
          throw new Error("Your session role could not be verified.");
        }
        if (pathRole && pathRole !== actualRole) {
          window.location.replace(dashboardPaths[actualRole] || "/login");
          return;
        }
        sessionStorage.setItem("momo_role", actualRole);
        sessionStorage.setItem("momo_user", JSON.stringify(data.user));
        applyRoleAccess(actualRole);
        const apiBaseField = document.getElementById("apiBase");
        const tokenField = document.getElementById("accessToken");
        const form = document.getElementById("connectForm");
        if (form && apiBaseField && tokenField) {
          apiBaseField.value = apiBase;
          tokenField.value = token;
          form.requestSubmit();
        }
      })
      .catch(() => {
        clearSession();
        window.location.replace(loginPaths[pathRole || role] || "/login");
      });

    window.setTimeout(() => {
      clearSession();
      window.location.replace(loginPaths[role] || "/login");
    }, Math.max(0, expiresAt - Date.now()));
  }

  function applyRoleAccess(role) {
    allowedViews = {
      customer: new Set(["overview", "wallets", "payments", "transactions", "ledger", "disputes"]),
      merchant: new Set(["overview", "wallets", "payments", "transactions", "ledger", "disputes"]),
      agent: new Set(["overview", "wallets", "transactions", "ledger", "disputes"]),
      admin: new Set(["overview", "wallets", "payments", "transactions", "ledger", "reconciliation", "risk", "disputes"]),
    }[role] || new Set(["overview"]);
    document.querySelectorAll(".nav-item[data-view]").forEach((item) => {
      item.hidden = !allowedViews.has(item.dataset.view);
    });
    const selectedView = window.location.hash.slice(1);
    if (selectedView && !allowedViews.has(selectedView)) {
      window.history.replaceState(null, "", "#overview");
      document.querySelector('.nav-item[data-view="overview"]')?.click();
    }
  }

  window.addEventListener("hashchange", () => {
    const requestedView = window.location.hash.slice(1);
    if (allowedViews && requestedView && !allowedViews.has(requestedView)) {
      window.history.replaceState(null, "", "#overview");
      document.querySelector('.nav-item[data-view="overview"]')?.click();
    }
  });

  document.addEventListener(
    "click",
    async (event) => {
      if (!(event.target instanceof Element) || !event.target.closest("#signoutBtn")) return;
      event.preventDefault();
      const token = sessionStorage.getItem("momo_token");
      const apiBase = sessionStorage.getItem("momo_api_base") || window.location.origin;
      if (token) {
        try {
          const response = await fetch(`${apiBase}/auth/logout`, {
            method: "POST",
            headers: { Authorization: `Bearer ${token}` },
            cache: "no-store",
          });
          if (!response.ok && response.status !== 401) {
            throw new Error(`The API could not confirm logout (${response.status}).`);
          }
        } catch (error) {
          window.alert(
            `${error instanceof Error ? error.message : "The API could not confirm logout."} ` +
              "This browser session will still be cleared; the access token may remain valid until it expires.",
          );
        }
      }
      clearSession();
      window.location.assign("/login");
    },
    true,
  );

  document.addEventListener(
    "submit",
    (event) => {
      if (!(event.target instanceof HTMLFormElement) || event.target.id !== "connectForm") return;
      const token = document.getElementById("accessToken")?.value.trim();
      const apiBase = document.getElementById("apiBase")?.value.trim().replace(/\/+$/, "");
      if (!token || !apiBase || token === sessionStorage.getItem("momo_token")) return;
      fetch(`${apiBase}/auth/me`, {
        headers: { Authorization: `Bearer ${token}` },
        cache: "no-store",
      })
        .then(async (response) => {
          const data = await response.json().catch(() => ({}));
          if (!response.ok) {
            throw new Error(data.error?.message || `Unable to verify access (${response.status}).`);
          }
          applyRoleAccess(data.user?.role || roleFor(data.user?.groups || []));
        })
        .catch((error) => {
          const errorElement = document.getElementById("connectError");
          if (errorElement) {
            errorElement.textContent = error.message;
            errorElement.classList.remove("hidden");
          }
        });
    },
    true,
  );

  window.MomoAuth = { clearSession, complete, dashboardSession, post, showError };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", dashboardSession, { once: true });
  } else {
    dashboardSession();
  }
})();
