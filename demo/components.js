const STATUS = {
  COMPLETED: ["✓", "Completed", "success"],
  PENDING: ["⏱", "Pending", "pending"],
  REVIEW: ["⚠", "Review", "review"],
  FLAGGED: ["⚠", "Flagged", "review"],
  REVERSED: ["↺", "Reversed", "neutral"],
  FAILED: ["✕", "Failed", "danger"],
  REJECTED: ["✕", "Rejected", "danger"],
  DISCREPANCY: ["⌁", "Discrepancy", "danger"],
};

class AmountDisplay extends HTMLElement {
  connectedCallback() {
    const minor = Number(this.getAttribute("value"));
    const currency = this.getAttribute("currency") || "ZMW";
    if (!Number.isFinite(minor)) {
      this.textContent = "—";
      return;
    }
    this.textContent = new Intl.NumberFormat("en-ZM", {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(minor / 100);
    this.classList.add("amount-display", `amount-${this.getAttribute("variant") || "inline"}`);
  }
}

class StatusBadge extends HTMLElement {
  connectedCallback() {
    const value = (this.getAttribute("status") || "UNKNOWN").toUpperCase();
    const [icon, label, tone] = STATUS[value] || ["•", value.replaceAll("_", " "), "neutral"];
    this.innerHTML = `<span class="status ${tone}"><span aria-hidden="true">${icon}</span> ${label}</span>`;
  }
}

class WalletPicker extends HTMLElement {
  set wallets(value) {
    this._wallets = Array.isArray(value) ? value : [];
    this.render();
  }

  connectedCallback() {
    this.render();
  }

  render() {
    this.innerHTML = '<label>Find a wallet<input type="search" aria-label="Search wallets"></label><div role="listbox"></div>';
    const input = this.querySelector("input");
    const list = this.querySelector('[role="listbox"]');
    input.addEventListener("input", () => {
      const term = input.value.toLowerCase();
      list.replaceChildren(...(this._wallets || [])
        .filter((wallet) => `${wallet.id} ${wallet.owner_id} ${wallet.kind}`.toLowerCase().includes(term))
        .map((wallet) => {
          const option = document.createElement("button");
          option.type = "button";
          option.setAttribute("role", "option");
          option.textContent = `${wallet.kind} · ${String(wallet.id).slice(-4)} · ${wallet.owner_id} · ${wallet.currency} ${Number(wallet.balance || 0) / 100}`;
          option.addEventListener("click", () => this.dispatchEvent(new CustomEvent("wallet-select", { detail: wallet, bubbles: true })));
          return option;
        }));
    });
    input.dispatchEvent(new Event("input"));
  }
}

class ConfirmSheet extends HTMLElement {
  show(summary, confirm) {
    this.replaceChildren();
    const dialog = document.createElement("dialog");
    dialog.innerHTML = `<form method="dialog"><h2>Confirm transaction</h2><pre></pre><button value="cancel">Cancel</button><button type="button">Hold to confirm</button></form>`;
    dialog.querySelector("pre").textContent = summary;
    const button = dialog.querySelector('button[type="button"]');
    let timer;
    const cancel = () => window.clearTimeout(timer);
    button.addEventListener("pointerdown", () => {
      timer = window.setTimeout(() => {
        dialog.close();
        confirm();
      }, 2000);
    });
    ["pointerup", "pointerleave", "pointercancel"].forEach((name) => button.addEventListener(name, cancel));
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    this.append(dialog);
    dialog.showModal();
  }
}

class DataTable extends HTMLElement {
  connectedCallback() {
    this.setAttribute("role", "region");
    this.setAttribute("tabindex", "0");
    this.setAttribute("aria-label", this.getAttribute("label") || "Data table");
  }
}

class Receipt extends HTMLElement {
  set detail(value) {
    const row = (label, text) => `<div><dt>${label}</dt><dd>${text}</dd></div>`;
    this.innerHTML = `<article class="receipt"><h2>MoMo · Official electronic receipt</h2><strong>${value.amount}</strong><dl>${row("Transaction", value.id)}${row("Type", value.type)}${row("From → To", `${value.from} → ${value.to}`)}${row("Timestamp", value.timestamp)}${row("Idempotency key", value.key)}${row("Status", value.status)}</dl><p>Validated on immutable double-entry ledger · SHA-256 chain updated</p></article>`;
  }
}

class IntegrityList extends HTMLElement {
  set invariants(value) {
    this.replaceChildren(...(value || []).map(({ name, ok }) => {
      const item = document.createElement("div");
      item.className = `integrity-item ${ok ? "ok" : "fail"}`;
      item.textContent = `${ok ? "✓" : "✕"} ${name} · ${ok ? "OK" : "Review required"}`;
      return item;
    }));
  }
}

class ToastHost extends HTMLElement {
  connectedCallback() {
    this.setAttribute("role", "status");
    this.setAttribute("aria-live", "polite");
    this.setAttribute("aria-atomic", "true");
    this.queue = [];
  }

  announce(message) {
    this.queue.push(String(message));
    if (this.busy) return;
    this.busy = true;
    const next = () => {
      this.textContent = this.queue.shift() || "";
      if (!this.textContent) {
        this.busy = false;
        return;
      }
      window.setTimeout(next, 3500);
    };
    next();
  }
}

customElements.define("amount-display", AmountDisplay);
customElements.define("status-badge", StatusBadge);
customElements.define("wallet-picker", WalletPicker);
customElements.define("confirm-sheet", ConfirmSheet);
customElements.define("data-table", DataTable);
customElements.define("receipt-view", Receipt);
customElements.define("integrity-list", IntegrityList);
customElements.define("toast-host", ToastHost);
