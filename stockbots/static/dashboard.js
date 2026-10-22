// Stockbots dashboard. Polls /api/state and redraws. No frameworks.
(function () {
  "use strict";

  const REFRESH_MS = 10000;
  const lanes = {}; // bot id -> element refs, built once so edits aren't wiped by refreshes
  let lastState = null;

  // ---- helpers ----------------------------------------------------------

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style") el.setAttribute("style", v);
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  function svg(tag, attrs) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    return el;
  }

  const money = (v) =>
    v === null || v === undefined ? "–" : v.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const money2 = (v) =>
    v === null || v === undefined ? "–" : v.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const pct = (v, digits = 2) => (v === null || v === undefined || isNaN(v) ? "–" : (v > 0 ? "+" : "") + v.toFixed(digits) + "%");
  const sign = (v) => (v > 0.0001 ? "gain" : v < -0.0001 ? "loss" : "");
  const etFmt = (iso, opts) => {
    if (!iso) return "";
    return new Date(iso).toLocaleString("en-US", { timeZone: "America/New_York", ...opts });
  };
  const localTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const botColor = (id) => `var(--${id})`;

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json", "X-Stockbots": "1" },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      credentials: "same-origin",
    });
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("logged out");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }

  let toastTimer;
  function toast(message) {
    let el = document.querySelector(".toast");
    if (!el) {
      el = h("div", { class: "toast", role: "status" });
      document.body.append(el);
    }
    el.textContent = message;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.remove(), 4500);
  }

  // ---- numbers per bot --------------------------------------------------

  // The server keeps each bot's return day by day, across brokers.
  function series(bot) {
    const race = bot.race || {};
    return (race.timestamp || []).map((t, i) => [t * 1000, (race.return_pct || [])[i] || 0]);
  }

  function returnPct(bot) {
    const s = series(bot);
    return s.length ? s[s.length - 1][1] : null;
  }

  // ---- header and race ---------------------------------------------------

  function renderHeader(state) {
    const mode = document.getElementById("mode");
    const live = state.mode === "live";
    mode.textContent = live ? "Live money" : "Paper";
    mode.className = "mode-pill" + (live ? " live" : "");
    const c = state.clock;
    const market = document.getElementById("market");
    market.replaceChildren();
    if (!c) {
      market.append("Waiting for Alpaca…");
    } else if (c.is_open) {
      market.append(h("strong", {}, "Market open"), `, closes at ${etFmt(c.next_close, { hour: "numeric", minute: "2-digit" })} ET`);
    } else {
      const when = c.next_open ? etFmt(c.next_open, { weekday: "short", hour: "numeric", minute: "2-digit" }) + " ET" : "next trading day";
      market.append(h("strong", {}, "Market closed"), `, opens ${when}`);
    }
  }

  function renderRace(bots) {
    const title = document.getElementById("race-title");
    const sub = document.getElementById("race-sub");
    const chart = document.getElementById("chart");
    const ranked = bots
      .map((b) => ({ b, r: returnPct(b), s: series(b) }))
      .sort((x, y) => (y.r ?? -1e9) - (x.r ?? -1e9));
    const withData = ranked.filter((x) => x.s.length >= 2);
    const leader = ranked[0];
    if (!bots.length) {
      title.textContent = "No bots are running";
      sub.textContent = "Finish Setup to start them.";
    } else if (bots.length === 1) {
      const r = ranked[0].r;
      title.textContent = r === null || Math.abs(r) < 0.005 ? `${bots[0].name} is ready` : `${bots[0].name}: ${pct(r)}`;
      sub.textContent = "Add more bots any time to race them against each other.";
    } else if (!leader || leader.r === null || Math.abs(leader.r) < 0.005 && ranked.every((x) => Math.abs(x.r || 0) < 0.005)) {
      title.textContent = bots.length === 3 ? "All three start level" : "Both start level";
      sub.textContent = "The race chart fills in as the bots trade.";
    } else {
      const gap = ranked.length > 1 && ranked[1].r !== null ? leader.r - ranked[1].r : null;
      title.textContent = `${leader.b.name} leads, ${pct(leader.r)}`;
      sub.textContent = gap !== null ? `${gap.toFixed(2)} points ahead of ${ranked[1].b.name}. Returns since each bot started.` : "Returns since each bot started.";
    }
    chart.replaceChildren();
    if (!withData.length) {
      chart.append(h("p", { class: "empty" }, "No account history yet. Lines appear after the first day of trading."));
      return ranked;
    }
    // Draw at the real pixel width so text stays readable on phones.
    const W = Math.max(chart.clientWidth || 1000, 300);
    const narrow = W < 640;
    const H = narrow ? 230 : 280, padL = 44, padR = narrow ? 118 : 210, padT = 16, padB = 28;
    const nameSize = narrow ? 13 : 17, valSize = narrow ? 12 : 15, labelGap = narrow ? 32 : 40;
    let t0 = Infinity, t1 = -Infinity, y0 = 0, y1 = 0;
    for (const { s } of withData) {
      for (const [t, v] of s) {
        t0 = Math.min(t0, t); t1 = Math.max(t1, t);
        y0 = Math.min(y0, v); y1 = Math.max(y1, v);
      }
    }
    const span = Math.max(y1 - y0, 1);
    y0 -= span * 0.12; y1 += span * 0.12;
    const x = (t) => padL + ((t - t0) / Math.max(t1 - t0, 1)) * (W - padL - padR);
    const y = (v) => padT + (1 - (v - y0) / (y1 - y0)) * (H - padT - padB);
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Return of each bot over time" });

    // zero line and a few gridlines
    const step = niceStep((y1 - y0) / 4);
    for (let v = Math.ceil(y0 / step) * step; v <= y1; v += step) {
      const yy = y(v);
      root.append(svg("line", { x1: padL, x2: W - padR, y1: yy, y2: yy, stroke: "var(--line)", "stroke-width": Math.abs(v) < 1e-9 ? 1.6 : 0.8 }));
      const label = svg("text", { x: padL - 8, y: yy + 4, "text-anchor": "end", "font-size": 12, fill: "var(--ink-3)" });
      label.textContent = (v > 0 ? "+" : "") + v.toFixed(step < 1 ? 1 : 0) + "%";
      root.append(label);
    }
    // date labels at the ends
    for (const [t, anchor] of [[t0, "start"], [t1, "end"]]) {
      const d = svg("text", { x: x(t), y: H - 6, "text-anchor": anchor, "font-size": 12, fill: "var(--ink-3)" });
      d.textContent = new Date(t).toLocaleDateString([], { month: "short", day: "numeric" });
      root.append(d);
    }
    // lines
    const ends = [];
    for (const { b, s } of withData) {
      const d = s.map(([t, v], i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
      root.append(svg("path", { d, fill: "none", stroke: botColor(b.id), "stroke-width": 3.2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
      const [lt, lv] = s[s.length - 1];
      root.append(svg("circle", { cx: x(lt), cy: y(lv), r: 5, fill: botColor(b.id) }));
      ends.push({ b, yy: y(lv), v: lv, xx: x(lt) });
    }
    // end labels, nudged apart so they never overlap
    ends.sort((a, b) => a.yy - b.yy);
    for (let i = 1; i < ends.length; i++) ends[i].yy = Math.max(ends[i].yy, ends[i - 1].yy + labelGap);
    for (const e of ends) {
      const tx = W - padR + (narrow ? 10 : 16);
      const name = svg("text", { x: tx, y: e.yy - 2, "font-size": nameSize, "font-weight": 700, fill: botColor(e.b.id), "font-family": "var(--display)" });
      name.textContent = e.b.name;
      const val = svg("text", { x: tx, y: e.yy + valSize + 2, "font-size": valSize, fill: "var(--ink-2)" });
      val.textContent = pct(e.v);
      root.append(name, val);
    }
    chart.append(root);
    return ranked;
  }

  function niceStep(raw) {
    const p = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-6))));
    for (const m of [1, 2, 2.5, 5, 10]) if (raw <= m * p) return m * p;
    return 10 * p;
  }

  // ---- lanes --------------------------------------------------------------

  function buildLane(bot) {
    const refs = {};
    refs.rank = h("span", { class: "rank" });
    refs.name = h("h3", {});
    refs.who = h("div", { class: "who" });
    refs.equity = h("div", { class: "value" });
    refs.today = h("div", { class: "value" });
    refs.ret = h("div", { class: "value" });
    refs.status = h("div", { class: "status" });
    refs.positions = h("div", {});
    refs.pause = h("button", { class: "btn", type: "button", onclick: () => togglePause(bot.id) });
    refs.flatten = h("button", { class: "btn danger", type: "button", onclick: () => flatten(bot.id) }, "Close all positions");
    refs.move = h("button", { class: "btn quiet", type: "button", onclick: () => prepareMove(bot.id) }, "Prepare to move broker");
    refs.moveNote = h("div", { class: "lane-section move-note", hidden: true });
    refs.rulesText = h("textarea", { "aria-label": `Rules for ${bot.name}`, maxlength: 1500 });
    refs.rulesSave = h("button", { class: "btn primary", type: "button", onclick: () => saveRules(bot.id) }, "Save rules");
    refs.rulesReset = h("button", { class: "btn quiet", type: "button", onclick: () => saveRules(bot.id, true) }, "Use the default rules");
    refs.details = h("details", { class: "rules" },
      h("summary", {}, "Trading rules the AI follows"),
      h("p", {}, "The AI reads these with every decision. Coins, sizes and stops are set by the code, not by these words."),
      refs.rulesText,
      h("div", { class: "rule-actions" }, refs.rulesSave, refs.rulesReset));
    refs.root = h("article", { class: "lane", style: `--bot:${botColor(bot.id)}`, "aria-label": bot.name },
      h("div", { class: "lane-top" },
        h("div", { class: "lane-title" }, refs.rank, h("div", {}, refs.name, refs.who)),
        h("div", { class: "figures" },
          h("div", { class: "figure" }, refs.equity, h("div", { class: "label" }, "Account value")),
          h("div", { class: "figure" }, refs.today, h("div", { class: "label" }, "Today")),
          h("div", { class: "figure" }, refs.ret, h("div", { class: "label" }, "Since start"))),
        refs.status),
      refs.moveNote,
      h("div", { class: "lane-section" }, h("h4", {}, "Holding"), refs.positions),
      h("div", { class: "lane-section" }, refs.details),
      h("div", { class: "controls" }, refs.pause, refs.flatten, refs.move));
    refs.rulesText.value = bot.rules || "";
    refs.rulesDirty = false;
    refs.rulesText.addEventListener("input", () => (refs.rulesDirty = true));
    return refs;
  }

  function renderLanes(ranked) {
    const container = document.getElementById("lanes");
    ranked.forEach(({ b, r }, i) => {
      let refs = lanes[b.id];
      if (!refs) {
        refs = lanes[b.id] = buildLane(b);
      }
      container.append(refs.root); // re-appending keeps the leader first
      refs.rank.textContent = r === null ? "" : String(i + 1);
      refs.rank.setAttribute("aria-label", r === null ? "" : `Rank ${i + 1}`);
      refs.name.textContent = b.name;
      refs.who.textContent = `${b.ai_label} (${b.model}), ${b.style_label.toLowerCase()}, on ${b.broker_label}`;
      const showMove = lastState && (lastState.mode === "live" || b.broker !== "alpaca");
      refs.move.hidden = !showMove || b.moving;
      refs.moveNote.hidden = !b.moving;
      if (b.moving) {
        const envName = `LIVE_BROKER_${b.id.toUpperCase()}`;
        refs.moveNote.replaceChildren(
          h("h4", {}, b.ready_to_move ? "Ready to move" : "Closing positions before the move…"),
          h("p", { class: "none" }, b.ready_to_move
            ? `Nothing is held at ${b.broker_label}. In Docker Manager, set ${envName} and the new broker's keys, redeploy, check the new balance here, then press Resume.`
            : `Waiting for ${b.broker_label} to finish closing positions and canceling orders.`));
      }
      const acct = b.account || {};
      refs.equity.textContent = money(acct.equity);
      refs.today.textContent = pct(acct.day_pl_pct);
      refs.today.className = "value " + sign(acct.day_pl_pct || 0);
      refs.ret.textContent = pct(r);
      refs.ret.className = "value " + sign(r || 0);

      const status = [];
      const running = b.status === "running";
      status.push(h("div", {}, h("span", { class: "dot " + (b.paused ? "off" : running ? "on" : "off") }),
        b.problem ? `Can't run on ${b.broker_label}: ${b.problem}` :
        b.paused ? "Paused: no new trades. Its stops stay in place." :
        running ? (b.entry_blocker ? `Running, not buying: ${b.entry_blocker}` : "Running, looking for trades") :
        `${b.status}${b.status_detail ? ": " + b.status_detail : ""}`));
      const aiOk = b.model_status === "ok" || b.model_status === "waiting";
      status.push(h("div", { class: aiOk ? "" : "warn" },
        aiOk ? `${b.ai_label} is making the calls` : b.model_status.replace(/^code is choosing/, "Code is choosing")));
      const cost = b.spend_today_usd > 0 && b.spend_today_usd < 1 ? "$" + b.spend_today_usd.toFixed(3) : money2(b.spend_today_usd);
      status.push(h("div", {}, `AI cost today ${cost} of ${money2(b.spend_cap_usd)} (${b.calls_today} calls). Trades today ${b.entries_today} of ${b.max_entries}.`));
      refs.status.replaceChildren(...status);

      const pos = b.positions || [];
      if (!pos.length) {
        refs.positions.replaceChildren(h("p", { class: "none" }, "Nothing. Cash is a position too."));
      } else {
        const rows = pos.map((p) =>
          h("tr", {},
            h("td", {}, p.symbol),
            h("td", {}, String(p.qty)),
            h("td", {}, money2(p.avg_entry_price)),
            h("td", {}, money2(p.current_price)),
            h("td", { class: sign(p.unrealized_plpc) }, pct(p.unrealized_plpc, 1)),
            h("td", {}, p.stop_price ? money2(p.stop_price) : "–")));
        refs.positions.replaceChildren(h("table", { class: "positions" },
          h("thead", {}, h("tr", {}, ...["Stock", "Shares", "Paid", "Now", "P&L", "Stop"].map((t) => h("th", { scope: "col" }, t)))),
          h("tbody", {}, rows)));
      }
      refs.pause.textContent = b.paused ? "Resume" : "Pause";
      if (!refs.rulesDirty && document.activeElement !== refs.rulesText) refs.rulesText.value = b.rules || "";
    });
  }

  // ---- decisions and notices ---------------------------------------------

  const AI_NAMES = { jev: "Jev", claude: "Claude", openai: "OpenAI" };

  function renderDecisions(decisions, bots) {
    const names = Object.fromEntries(bots.map((b) => [b.id, b.name]));
    const list = document.getElementById("decisions");
    if (!decisions.length) {
      list.replaceChildren(h("li", {}, h("span", {}), h("div", { class: "d-meta" }, "No decisions yet. Bots only decide when the market is open and a move is possible.")));
      return;
    }
    list.replaceChildren(...decisions.slice(0, 60).map((d) => {
      let by;
      if (AI_NAMES[d.decided_by]) {
        const conf = d.confidence !== null && d.confidence !== undefined ? ` ${Math.round(d.confidence * 100)}%` : "";
        by = h("span", { class: "chip ai" }, AI_NAMES[d.decided_by] + conf);
      } else {
        by = h("span", { class: "chip code" }, { code: "Code", rules: "Rules", owner: "You" }[d.decided_by] || d.decided_by);
      }
      const outcome = {
        submitted: h("span", { class: "chip" }, "Sent to Alpaca"),
        held: h("span", { class: "chip" }, "Held"),
        vetoed: h("span", { class: "chip vetoed" }, "Blocked"),
        error: h("span", { class: "chip error" }, "Error"),
      }[d.outcome] || null;
      // The move text already says what was bought; show the order detail only when something went wrong.
      const detail = [d.reason, d.outcome === "vetoed" || d.outcome === "error" ? d.detail : null].filter(Boolean).join(". ");
      return h("li", { style: `--bot:${botColor(d.bot)}` },
        h("time", { datetime: d.ts }, localTime(d.ts)),
        h("div", {},
          h("div", { class: "d-move" }, h("span", { class: "bot-name" }, names[d.bot] || d.bot), " ", d.move_text || d.move),
          h("div", { class: "d-meta" }, by, outcome, detail)));
    }));
  }

  function renderEvents(events, bots) {
    const names = Object.fromEntries(bots.map((b) => [b.id, b.name]));
    const list = document.getElementById("events");
    if (!events.length) {
      list.replaceChildren(h("li", {}, "Nothing yet."));
      return;
    }
    list.replaceChildren(...events.map((e) =>
      h("li", { class: e.level === "error" ? "error" : "" },
        h("time", { datetime: e.ts }, localTime(e.ts)), " ",
        e.bot ? `${names[e.bot] || e.bot}: ` : "", e.message)));
  }

  // ---- actions --------------------------------------------------------------

  async function togglePause(id) {
    const bot = lastState && lastState.bots.find((b) => b.id === id);
    if (!bot) return;
    try {
      const r = await api(`/api/bots/${id}/${bot.paused ? "resume" : "pause"}`, { method: "POST", body: {} });
      toast(r.message);
      refresh();
    } catch (e) { toast(e.message); }
  }

  async function flatten(id) {
    const bot = lastState && lastState.bots.find((b) => b.id === id);
    if (!bot) return;
    if (!confirm(`Close every ${bot.name} position at market price now?`)) return;
    try {
      const r = await api(`/api/bots/${id}/flatten`, { method: "POST", body: {} });
      toast(r.message);
      refresh();
    } catch (e) { toast(e.message); }
  }

  async function prepareMove(id) {
    const bot = lastState && lastState.bots.find((b) => b.id === id);
    if (!bot) return;
    if (!confirm(`Move ${bot.name} off ${bot.broker_label}? It pauses and sells every position there at market price now.`)) return;
    try {
      const r = await api(`/api/bots/${id}/prepare-move`, { method: "POST", body: {} });
      toast(r.message);
      refresh();
    } catch (e) { toast(e.message); }
  }

  async function saveRules(id, reset) {
    const refs = lanes[id];
    try {
      const r = await api(`/api/bots/${id}/rules`, { method: "POST", body: { rules: reset ? "" : refs.rulesText.value } });
      refs.rulesText.value = r.rules;
      refs.rulesDirty = false;
      toast(reset ? "Default rules restored." : "Rules saved. The AI uses them from its next decision.");
    } catch (e) { toast(e.message); }
  }

  document.getElementById("reopen").addEventListener("click", async () => {
    const ok = confirm(
      "This stops the bots and opens Setup again so you can change keys or turn bots on and off. " +
      "Open positions and their stops stay at Alpaca. Finish Setup right away: until you do, anyone who " +
      "reaches the page first could claim it. Continue?");
    if (!ok) return;
    try {
      await api("/api/setup/reopen", { method: "POST", body: {} });
      location.href = "/setup";
    } catch (e) { toast(e.message); }
  });

  document.getElementById("logout").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST", body: {} }).catch(() => {});
    location.href = "/login";
  });

  // ---- backtests -------------------------------------------------------------

  const btBot = document.getElementById("bt-bot");
  const btMonths = document.getElementById("bt-months");
  const btStatus = document.getElementById("bt-status");
  const btGo = document.getElementById("bt-go");
  let btTimer = null;

  function fillBacktestBots(bots) {
    const have = [...btBot.options].map((o) => o.value).join(",");
    const want = bots.map((b) => b.id).join(",");
    if (have === want) return;
    const keep = btBot.value;
    btBot.replaceChildren(...bots.map((b) => h("option", { value: b.id }, b.name)));
    if (bots.some((b) => b.id === keep)) btBot.value = keep;
    fillMonths();
  }

  function fillMonths() {
    const bot = lastState && lastState.bots.find((b) => b.id === btBot.value);
    const intraday = bot && bot.style === "momentum";
    const options = intraday ? [[3, "3 months"], [6, "6 months"], [12, "12 months"]] : [[12, "1 year"], [24, "2 years"], [36, "3 years"]];
    btMonths.replaceChildren(...options.map(([v, t]) => h("option", { value: v }, t)));
    btMonths.value = intraday ? "6" : "24";
  }
  btBot.addEventListener("change", fillMonths);

  async function loadBacktests() {
    let data;
    try { data = await api("/api/backtests"); } catch (e) { return; }
    const run = data.running;
    btGo.disabled = !!run;
    btStatus.textContent = run ? `${run.name}: ${run.status}…` : "";
    const list = document.getElementById("bt-list");
    if (!data.reports.length) {
      list.replaceChildren(h("li", {}, "No backtests yet. A 2-year run takes about a minute."));
    } else {
      list.replaceChildren(...data.reports.map((r) => {
        if (!r.ok) return h("li", { class: "error" }, `${r.name}, ${r.months} months: failed. ${r.error || ""}`);
        const vs = r.return_pct - r.spy_return_pct;
        return h("li", {},
          h("a", { href: `/backtests/${encodeURIComponent(r.id)}` }, `${r.name}, ${r.start} to ${r.end}`),
          `: ${pct(r.return_pct, 1)} vs SPY ${pct(r.spy_return_pct, 1)} (${vs >= 0 ? "ahead" : "behind"} by ${Math.abs(vs).toFixed(1)} points), worst drop ${pct(r.max_drawdown_pct, 1)}.`);
      }));
    }
    clearTimeout(btTimer);
    btTimer = run ? setTimeout(loadBacktests, 3000) : null;  // poll quickly only while one is running
  }

  document.getElementById("bt-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    btGo.disabled = true;
    try {
      await api("/api/backtests", { method: "POST", body: { bot: btBot.value, months: Number(btMonths.value) } });
      btStatus.textContent = "Starting…";
      loadBacktests();
    } catch (e) {
      btGo.disabled = false;
      toast(e.message);
    }
  });

  // ---- loop -----------------------------------------------------------------

  async function refresh() {
    try {
      const state = await api("/api/state");
      state.bots = (state.bots || []).filter((b) => b.enabled);
      lastState = state;
      renderHeader(state);
      const ranked = renderRace(state.bots);
      renderLanes(ranked);
      renderDecisions(state.decisions || [], state.bots);
      renderEvents(state.events || [], state.bots);
      fillBacktestBots(state.bots);
      if (!btTimer) loadBacktests();
    } catch (e) {
      if (e.message !== "logged out") document.getElementById("market").textContent = "Can't reach the server. Retrying…";
    }
  }

  refresh();
  setInterval(refresh, REFRESH_MS);
})();
