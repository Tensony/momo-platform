(() => {
  "use strict";
  const form = document.getElementById("registerForm");
  const message = document.getElementById("registerMessage");
  const submit = document.getElementById("registerSubmit");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    message.hidden = true;
    const password = form.elements.password.value;
    if (password !== document.getElementById("confirmPassword").value) {
      message.textContent = "Passwords do not match.";
      message.className = "auth-message";
      message.hidden = false;
      return;
    }
    submit.disabled = true;
    submit.textContent = "Creating account…";
    try {
      const data = await MomoAuth.post("/auth/register", {
        full_name: form.elements.full_name.value.trim(),
        phone: form.elements.phone.value.trim(),
        currency: form.elements.currency.value,
        password,
      });
      MomoAuth.complete(data, "customer");
    } catch (error) {
      message.textContent = error.message;
      message.className = "auth-message";
      message.hidden = false;
    } finally {
      submit.disabled = false;
      submit.textContent = "Create customer wallet";
    }
  });
})();
