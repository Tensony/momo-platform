(() => {
  "use strict";
  const role = document.body.dataset.authRole;
  const settings = {
    customer: { endpoint: "/auth/login/customer", field: "phone" },
    agent: { endpoint: "/auth/login/agent", field: "agent_id" },
    admin: { endpoint: "/auth/login/admin", field: "username" },
    merchant: { endpoint: "/auth/login/merchant", field: "merchant_id" },
  }[role];
  const form = document.getElementById("loginForm");
  if (!settings || !form) return;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const alert = document.getElementById("authAlert");
    const button = form.querySelector('button[type="submit"]');
    alert.hidden = true;
    button.disabled = true;
    button.textContent = "Verifying credentials…";
    try {
      const data = await MomoAuth.post(settings.endpoint, {
        [settings.field]: form.elements.identifier.value.trim(),
        password: form.elements.password.value,
      });
      MomoAuth.complete(data, role);
    } catch (error) {
      alert.textContent = error instanceof Error ? error.message : "Sign-in failed. Try again.";
      alert.className = "auth-message";
      alert.hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = "Sign in";
    }
  });
})();
