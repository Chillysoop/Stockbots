(function () {
  "use strict";
  const form = document.getElementById("setup");
  const banners = document.getElementById("banners");
  const stepsEl = document.getElementById("bot-steps");

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined) el.append(kid instanceof Node ? kid : String(kid));
    return el;
  }

  function banner(text, bad) {
    banners.append(h("div", { class: "banner" + (bad ? " bad" : "") }, text));
  }

  async function load() {
    const res = await fetch("/api/setup");
    const s = await res.json();
    if (s.done) { location.href = "/login"; return; }
    if (!s.open) { banner(s.reason, true); return; }
    if (location.protocol === "http:" && !["localhost", "127.0.0.1"].includes(location.hostname)) {
      banner("This page isn't using HTTPS, so your keys travel unencrypted to the server. For better safety, point a domain at the server first (see the README), then come back here.", true);
    }
    banner(`Setup stays open for ${s.minutes_left} more minutes, and whoever finishes it first owns the bots. If it locks, restart the app container.`);
    if (s.mode === "live") banner("This server is set to LIVE mode: these bots would trade real money. Use live Alpaca keys only if you mean it.", true);

    s.bots.forEach((b, i) => {
      const toggle = h("input", { type: "checkbox", name: `${b.id}.enabled`, checked: true });
      const fields = h("div", { class: "bot-fields" },
        h("label", { class: "field" }, "Bot name", h("input", { type: "text", name: `${b.id}.name`, value: b.default_name, maxlength: 40 })),
        h("label", { class: "field" }, "Alpaca paper API key ID", h("input", { type: "text", name: `${b.id}.alpaca_key`, autocomplete: "off", spellcheck: "false" })),
        h("label", { class: "field" }, "Alpaca paper secret key", h("input", { type: "password", name: `${b.id}.alpaca_secret`, autocomplete: "off", spellcheck: "false" })),
        h("div", { class: "err", "data-err": `${b.id}.alpaca` }),
        h("details", { class: "rules" }, h("summary", {}, "Its starting rules"), h("p", {}, b.rules), h("p", {}, "You can edit these from the dashboard later.")));
      stepsEl.append(h("section", { class: "step bot", style: `--bot:var(--${b.id})` },
        h("h2", {}, h("span", { class: "n" }, String(3 + i)), `${b.default_name}`),
        h("p", { class: "help" }, `${b.ai_label} (${b.model}) makes the calls, trading as a ${b.style_label.toLowerCase()}. Give it its own Alpaca paper account: open a new paper account in Alpaca, switch to it, and generate its API keys.`),
        h("label", { class: "check" }, toggle, "Run this bot"),
        fields));
      toggle.addEventListener("change", sync);
    });
    sync();
    form.hidden = false;
  }

  // Hide the fields of bots that are off, and the AI keys nobody needs.
  function sync() {
    for (const id of ["claude", "jev", "openai"]) {
      const on = form.elements[`${id}.enabled`] ? form.elements[`${id}.enabled`].checked : true;
      const step = form.elements[`${id}.enabled`] && form.elements[`${id}.enabled`].closest(".step");
      if (step) step.querySelector(".bot-fields").hidden = !on;
      const keyBox = document.querySelector(`[data-bot="${id}"]`);
      if (keyBox) keyBox.hidden = !on;
    }
  }

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    document.querySelectorAll("[data-err]").forEach((e) => (e.textContent = ""));
    const val = (n) => (form.elements[n] ? form.elements[n].value : "");
    const body = {
      agree: [...form.querySelectorAll("input[name=agree]")].map((c) => c.checked),
      password: val("password"),
      jev_key: val("jev_key"),
      anthropic_key: val("anthropic_key"),
      openai_key: val("openai_key"),
      bots: {},
    };
    for (const id of ["claude", "jev", "openai"]) {
      const on = form.elements[`${id}.enabled`] ? form.elements[`${id}.enabled`].checked : true;
      body.bots[id] = { enabled: on, name: val(`${id}.name`), alpaca_key: val(`${id}.alpaca_key`), alpaca_secret: val(`${id}.alpaca_secret`) };
    }
    const go = document.getElementById("go");
    const progress = document.getElementById("progress");
    go.disabled = true;
    progress.textContent = "Checking keys with Alpaca and each AI…";
    try {
      const res = await fetch("/api/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Stockbots": "1" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) {
        progress.textContent = "All set. Opening the dashboard…";
        location.href = "/";
        return;
      }
      if (data.errors) {
        for (const [k, msg] of Object.entries(data.errors)) {
          const el = document.querySelector(`[data-err="${CSS.escape(k)}"]`);
          if (el) el.textContent = msg;
        }
        progress.textContent = "Fix the highlighted items, then try again.";
        const first = document.querySelector(".err:not(:empty)");
        if (first) first.scrollIntoView({ block: "center" });
      } else {
        progress.textContent = data.error || "Setup failed. Try again.";
      }
    } catch (e) {
      progress.textContent = "Can't reach the server. Try again.";
    } finally {
      go.disabled = false;
    }
  });

  load().catch(() => banner("Can't reach the server. Refresh to try again.", true));
})();
