"use strict";
// Claude usage dashboard. No dependencies; charts are hand-built SVG.
// All text from the API is inserted with textContent, never innerHTML.

const NS = "http://www.w3.org/2000/svg";
const state = { range: "30d", measure: "no_cache_read", tz: undefined, data: null };
try {
  state.range = localStorage.getItem("cu-range") || state.range;
  state.measure = localStorage.getItem("cu-measure") || state.measure;
} catch (e) { /* storage unavailable: use defaults */ }

// ---------- helpers ----------
const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
function el(tag, attrs = {}, parent, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    // CSP forbids inline style attributes; CSSOM assignment is allowed.
    if (k === "style") node.style.cssText = v; else node.setAttribute(k, v);
  }
  if (text !== undefined) node.textContent = text;
  if (parent) parent.appendChild(node);
  return node;
}
function sv(tag, attrs = {}, parent) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}
const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
const fmtTokens = (n) => (n == null ? "–" : compact.format(n));
const fmtPct = (v) => (v == null ? "–" : `${Math.round(v * 10) / 10}%`);
const money = (v) => (v == null ? "–" : `$${v.toFixed(2)}`);
function fmtTime(ms, opts) {
  return new Intl.DateTimeFormat("en-US", { timeZone: state.tz, ...opts }).format(new Date(ms));
}
const ms = (iso) => (iso ? Date.parse(iso) : null);
function ago(iso) {
  const s = Math.max(0, (Date.now() - ms(iso)) / 1000);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function until(iso) {
  let s = (ms(iso) - Date.now()) / 1000;
  if (!(s > 0)) return "now";
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60);
  return d ? `in ${d}d ${h}h` : h ? `in ${h}h ${m}m` : `in ${m}m`;
}
function entityColor(key) {
  const k = (key || "").toLowerCase();
  if (k === "weekly") return css("--s-weekly");
  if (k === "session") return css("--s-session");
  if (k.includes("fable")) return css("--s-fable");
  if (k.includes("opus")) return css("--s-opus");
  if (k.includes("sonnet")) return css("--s-sonnet");
  return css("--s-other");
}
const FAMILY_LABEL = { fable: "Fable", opus: "Opus", sonnet: "Sonnet", haiku: "Haiku", other: "Other" };

async function api(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

// ---------- shared chart pieces ----------
function tooltip(container) {
  const tip = el("div", { class: "tip", role: "status" }, container);
  return {
    show(x, y, title, rows) {
      tip.replaceChildren();
      el("div", { class: "t" }, tip, title);
      for (const r of rows) {
        const row = el("div", { class: "row" }, tip);
        if (r.color) el("span", { class: "key-line", style: `background:${r.color}` }, row);
        el("b", {}, row, r.value);
        el("span", {}, row, r.label);
      }
      tip.style.display = "block";
      const cw = container.clientWidth;
      const tw = tip.offsetWidth;
      tip.style.left = `${Math.min(Math.max(0, x + 12), cw - tw)}px`;
      tip.style.top = `${Math.max(0, y - 10)}px`;
    },
    hide() { tip.style.display = "none"; },
  };
}

function legend(container, items, kind = "line") {
  if (items.length < 2) return;
  const lg = el("div", { class: "legend" }, container);
  for (const it of items) {
    const s = el("span", {}, lg);
    el("span", { class: kind === "line" ? "key-line" : "key-rect", style: `background:${it.color}` }, s);
    s.appendChild(document.createTextNode(it.label));
  }
}

function tableView(container, headers, rows) {
  const d = el("details", { class: "table" }, container);
  el("summary", {}, d, "Table");
  const wrap = el("div", { class: "table-wrap" }, d);
  const t = el("table", {}, wrap);
  const hr = el("tr", {}, el("thead", {}, t));
  headers.forEach((h) => el("th", {}, hr, h));
  const tb = el("tbody", {}, t);
  rows.forEach((r) => { const tr = el("tr", {}, tb); r.forEach((c) => el("td", {}, tr, c)); });
}

// Offset of the dashboard timezone from UTC at instant t, in ms.
function tzOffset(t) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: state.tz, hourCycle: "h23",
    year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", second: "numeric" })
    .formatToParts(new Date(t)).map((x) => [x.type, x.value]));
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second) - Math.floor(t / 1000) * 1000;
}
function localMidnight(y, m, d) { const g = Date.UTC(y, m, d); return g - tzOffset(g); }

// Ticks at local midnights (or 6-hour marks / month starts) in the dashboard timezone.
function timeTicks(from, to) {
  const H = 3600e3, D = 24 * H, span = to - from;
  const start = new Date(from + tzOffset(from));
  let y = start.getUTCFullYear(), m = start.getUTCMonth(), d = start.getUTCDate();
  const ticks = [];
  if (span > 60 * D) {
    for (let t = localMidnight(y, m, 1); t <= to; t = localMidnight(y, ++m, 1)) if (t >= from) ticks.push(t);
    return { ticks, fmt: { month: "short", year: "2-digit" } };
  }
  const stepDays = span <= 10 * D ? 1 : 7;
  for (let t = localMidnight(y, m, d); t <= to; d += stepDays, t = localMidnight(y, m, d)) {
    if (span <= 2 * D) { for (let h = 0; h < 24; h += 6) { const th = t + h * H; if (th >= from && th <= to) ticks.push(th); } }
    else if (t >= from) ticks.push(t);
  }
  return { ticks, fmt: span <= 2 * D ? { weekday: "short", hour: "numeric" } : { month: "short", day: "numeric" } };
}

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * p) return m * p;
  return 10 * p;
}

function valueAt(points, t) {
  let lo = 0, hi = points.length - 1, ans = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (points[mid][0] <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
  return ans < 0 ? null : points[ans][1];
}

// ---------- line chart (step-after: values hold until the next change point) ----------
function lineChart(root, { series, from, to, height = 220, events = [], refLabel, ariaLabel }) {
  root.replaceChildren();
  const visible = series.filter((s) => s.points.some((p) => p[1] != null));
  legend(root, visible.map((s) => ({ label: s.label, color: s.color })));
  const box = el("div", { class: "chart" }, root);
  if (!visible.length || from == null) { el("div", { class: "empty" }, box, "No data in this range yet."); return; }
  const W = Math.max(320, box.clientWidth), H = height, m = { l: 40, r: 12, t: 12, b: 26 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const x = (t) => m.l + ((t - from) / (to - from || 1)) * pw;
  const y = (v) => m.t + (1 - Math.min(v, 110) / 100) * ph;
  const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": ariaLabel }, box);

  for (const v of [0, 25, 50, 75, 100]) {
    sv("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), stroke: v === 0 ? css("--axis") : css("--grid"), "stroke-width": 1 }, svg);
    sv("text", { x: m.l - 6, y: y(v) + 4, "text-anchor": "end" }, svg).textContent = `${v}%`;
  }
  if (refLabel) {
    const t = sv("text", { x: W - m.r, y: y(100) - 4, "text-anchor": "end", class: "ref-label" }, svg);
    t.textContent = refLabel;
  }
  const { ticks, fmt } = timeTicks(from, to);
  const every = Math.max(1, Math.ceil(ticks.length / Math.floor(pw / 70)));
  ticks.forEach((t, i) => {
    if (i % every) return;
    sv("text", { x: x(t), y: H - 6, "text-anchor": "middle" }, svg).textContent = fmtTime(t, fmt);
  });

  for (const s of visible) {
    let d = "", open = false, lastY = null;
    for (const [t, v] of s.points) {
      if (v == null) { open = false; continue; }
      const px = Math.max(m.l, x(t)), py = y(v);
      if (!open) { d += `M${px},${py}`; open = true; } else { d += `H${px}V${py}`; }
      lastY = py;
    }
    const last = s.points[s.points.length - 1];
    if (open && last && last[1] != null) d += `H${x(to)}`;
    sv("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    if (open && lastY != null) sv("circle", { cx: x(to), cy: lastY, r: 4, fill: s.color, stroke: css("--surface"), "stroke-width": 2 }, svg);
  }

  const tip = tooltip(box);
  for (const e of events) {
    if (e.t < from || e.t > to) continue;
    const ex = x(e.t);
    sv("line", { x1: ex, x2: ex, y1: m.t, y2: H - m.b, stroke: css("--ink-2"), "stroke-width": 1, opacity: 0.5 }, svg);
    sv("rect", { x: ex - 4, y: m.t - 4, width: 8, height: 8, fill: css("--ink-2"), transform: `rotate(45 ${ex} ${m.t})` }, svg);
    const hit = sv("rect", { x: ex - 12, y: m.t - 12, width: 24, height: 24, class: "hit", tabindex: 0, "aria-label": e.message }, svg);
    const show = () => tip.show(ex, m.t + 12, fmtTime(e.t, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }),
      [{ value: e.severity === "limit" ? "▲ Limit" : "● Info", label: e.message }]);
    hit.addEventListener("pointerenter", show); hit.addEventListener("focus", show);
    hit.addEventListener("pointerleave", tip.hide); hit.addEventListener("blur", tip.hide);
  }

  const cross = sv("line", { y1: m.t, y2: H - m.b, stroke: css("--axis"), "stroke-width": 1, visibility: "hidden" }, svg);
  const overlay = sv("rect", { x: m.l, y: m.t + 14, width: pw, height: ph - 14, class: "hit", tabindex: 0,
    "aria-label": `${ariaLabel}. Use arrow keys to read values.` }, svg);
  let kt = to;
  const readout = (t) => {
    const px = x(t);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
    tip.show(px, m.t + 20, fmtTime(t, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }),
      visible.map((s) => ({ color: s.color, value: fmtPct(valueAt(s.points, t)), label: s.label })));
  };
  overlay.addEventListener("pointermove", (ev) => {
    const r = svg.getBoundingClientRect();
    const t = from + ((ev.clientX - r.left) * (W / r.width) - m.l) / pw * (to - from);
    readout(Math.min(to, Math.max(from, t)));
  });
  overlay.addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
    ev.preventDefault();
    kt = Math.min(to, Math.max(from, kt + (ev.key === "ArrowRight" ? 1 : -1) * (to - from) / 50));
    readout(kt);
  });
  const hide = () => { tip.hide(); cross.setAttribute("visibility", "hidden"); };
  overlay.addEventListener("pointerleave", hide); overlay.addEventListener("blur", hide);
}

// ---------- column charts ----------
function topRounded(x, y, w, h, r) {
  r = Math.min(r, h, w / 2);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

function columns(root, { cats, stacks, height = 220, fmtValue, ariaLabel, faded = [] }) {
  root.replaceChildren();
  const used = stacks.filter((s) => s.values.some((v) => v > 0));
  legend(root, used.map((s) => ({ label: s.label, color: s.color })), "rect");
  const box = el("div", { class: "chart" }, root);
  if (!cats.length || !used.length) { el("div", { class: "empty" }, box, "No data in this range yet."); return; }
  const W = Math.max(320, box.clientWidth), H = height, m = { l: 48, r: 12, t: 22, b: 26 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const totals = cats.map((_, i) => used.reduce((a, s) => a + (s.values[i] || 0), 0));
  const ymax = niceMax(Math.max(...totals));
  const y = (v) => m.t + (1 - v / ymax) * ph;
  const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": ariaLabel }, box);
  for (let i = 0; i <= 4; i++) {
    const v = (ymax / 4) * i;
    sv("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), stroke: i === 0 ? css("--axis") : css("--grid"), "stroke-width": 1 }, svg);
    sv("text", { x: m.l - 6, y: y(v) + 4, "text-anchor": "end" }, svg).textContent = fmtValue(v);
  }
  const band = pw / cats.length, bw = Math.max(2, Math.min(24, band * 0.7));
  const every = Math.max(1, Math.ceil(cats.length / Math.floor(pw / 64)));
  const tip = tooltip(box);
  cats.forEach((c, i) => {
    const cx = m.l + band * i + band / 2;
    if (i % every === 0) sv("text", { x: cx, y: H - 6, "text-anchor": "middle" }, svg).textContent = c.label;
    let base = y(0);
    const segs = used.map((s) => ({ s, v: s.values[i] || 0 })).filter((g) => g.v > 0);
    segs.forEach((g, j) => {
      const h = y(0) - y(g.v);
      const top = j === segs.length - 1;
      const gap = j > 0 ? 2 : 0;
      const hh = Math.max(0, h - gap);
      const yTop = base - h;
      const d = top ? topRounded(cx - bw / 2, yTop, bw, hh, 4) : `M${cx - bw / 2},${yTop}h${bw}v${hh}h${-bw}Z`;
      const p = sv("path", { d, fill: g.s.color, class: "seg-hover", tabindex: 0, opacity: faded[i] ? 0.45 : 1,
        "aria-label": `${c.label}, ${g.s.label}: ${fmtValue(g.v)}` }, svg);
      const show = () => tip.show(cx, yTop, c.tip || c.label,
        [{ color: g.s.color, value: fmtValue(g.v), label: g.s.label }, ...(c.extra || [])]);
      p.addEventListener("pointerenter", show); p.addEventListener("focus", show);
      p.addEventListener("pointerleave", tip.hide); p.addEventListener("blur", tip.hide);
      base -= h;
    });
    if (c.flag) {
      const t = sv("text", { x: cx, y: Math.max(12, y(totals[i]) - 6), "text-anchor": "middle", class: "ref-label" }, svg);
      t.textContent = band >= 90 ? "▲ possible change" : "▲";
    }
  });
}

// ---------- sections ----------
function renderHealth(sum) {
  const h = $("health");
  h.replaceChildren();
  const p = sum.poller || {};
  let level = "", text;
  const interval = p.interval_s || 60;
  if (!p.last_poll_at) { level = "warning"; text = "No polls yet"; }
  else if ((p.consecutive_failures || 0) > 0) { level = "critical"; text = `Polling failing: ${p.last_error || "error"}`; }
  else if ((Date.now() - ms(p.last_poll_at)) / 1000 > interval * 3 + 120) { level = "warning"; text = `Poller stale, last poll ${ago(p.last_poll_at)}`; }
  else text = `Polling every ${interval < 90 ? `${Math.round(interval)}s` : `${Math.round(interval / 60)} min`} · updated ${ago(p.last_ok_at)}`;
  el("span", { class: `dot ${level}` }, h);
  el("span", {}, h, (level === "critical" ? "▲ " : "") + text);
}

function meter(parent, pct, color) {
  const level = pct >= 90 ? "critical" : pct >= 75 ? "warning" : "";
  const mt = el("div", { class: `meter ${level}`, role: "meter", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": pct ?? 0 }, parent);
  const fill = el("span", {}, mt);
  fill.style.width = `${Math.min(100, Math.max(0, pct || 0))}%`;
  if (color && !level) fill.style.background = color;
  return level;
}

function renderTiles(sum) {
  const L = sum.latest;
  const hero = $("weekly-hero"); hero.replaceChildren();
  el("div", { class: "status-label" }, hero, "Weekly limit, all models");
  if (!L) { el("div", { class: "empty" }, hero, "Waiting for the first successful poll."); return; }
  el("div", { class: "value" }, hero, fmtPct(L.weekly_pct));
  const level = meter(hero, L.weekly_pct);
  if (level) el("div", { class: "status-label" }, hero, level === "critical" ? "▲ Near the limit" : "● Getting close");
  el("div", { class: "status-label" }, hero, `Resets ${fmtTime(ms(L.weekly_resets_at), { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })} · ${until(L.weekly_resets_at)}`);
  if (sum.pace) {
    const pr = sum.pace.projected_pct;
    el("div", { class: "status-label" }, hero, pr >= 100
      ? `▲ At this pace you'd reach the limit before it resets (${Math.round(sum.pace.elapsed_pct)}% of the week gone)`
      : `At this pace: about ${Math.round(pr)}% by reset (${Math.round(sum.pace.elapsed_pct)}% of the week gone)`);
  }

  const st = $("session-tile"); st.replaceChildren();
  el("div", { class: "label" }, st, "5-hour session");
  el("div", { class: "value" }, st, fmtPct(L.session_pct));
  meter(st, L.session_pct, css("--s-session"));
  el("div", { class: "meta" }, st, L.session_resets_at ? `Resets ${until(L.session_resets_at)}` : "No active session");

  const mx = $("mix-tile"); mx.replaceChildren();
  el("div", { class: "label" }, mx, "This week, by surface");
  if (!sum.breakdown.length) el("div", { class: "meta" }, mx, "Not reported");
  for (const b of sum.breakdown) {
    if (!b.pct) continue;
    const row = el("div", { class: "meta" }, mx);
    el("b", { style: "color:var(--ink);font-weight:600" }, row, `${b.pct}%`);
    row.appendChild(document.createTextNode(` ${b.label}`));
  }

  const bt = $("bucket-tiles"); bt.replaceChildren();
  for (const b of L.buckets) {
    const c = el("section", { class: "card span-3 tile" }, bt);
    el("div", { class: "label" }, c, b.label);
    el("div", { class: "value" }, c, fmtPct(b.pct));
    meter(c, b.pct, entityColor(b.name));
    el("div", { class: "meta" }, c, b.resets_at ? `Resets ${until(b.resets_at)}` : "");
  }
  const cr = el("section", { class: "card span-3 tile" }, bt);
  el("div", { class: "label" }, cr, "Usage credits this month");
  el("div", { class: "value" }, cr, money(L.credits_spent_month));
  el("div", { class: "meta" }, cr, L.extra_usage_enabled === false
    ? `Extra usage off${L.extra_usage_disabled_reason ? ` (${L.extra_usage_disabled_reason.replaceAll("_", " ")})` : ""}`
    : L.credits_balance != null ? `${money(L.credits_balance)} left under the cap` : "");
}

function renderSeries(ser) {
  const weekly = [{ key: "weekly", label: "All models", color: entityColor("weekly"), points: ser.weekly }]
    .concat(ser.buckets.map((b) => ({ key: b.name, label: b.label.replace(" weekly limit", ""), color: entityColor(b.name), points: b.points })));
  lineChart($("weekly-chart"), { series: weekly, from: ser.from, to: ser.to, events: ser.events, refLabel: "Limit", ariaLabel: "Weekly usage over time" });
  const dayFmt = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };
  tableView($("weekly-chart"), ["Time", ...weekly.map((s) => s.label)],
    ser.weekly.filter((p) => p[1] != null).slice(-200).reverse().map((p) =>
      [fmtTime(p[0], dayFmt), ...weekly.map((s) => fmtPct(valueAt(s.points, p[0])))]));

  lineChart($("session-chart"), { series: [{ key: "session", label: "Session", color: entityColor("session"), points: ser.session }],
    from: ser.from, to: ser.to, height: 180, refLabel: "Limit", ariaLabel: "Session usage over time" });
  tableView($("session-chart"), ["Time", "Session"],
    ser.session.filter((p) => p[1] != null).slice(-200).reverse().map((p) => [fmtTime(p[0], dayFmt), fmtPct(p[1])]));
}

function renderTokens(daily, sum) {
  const days = daily.days;
  const fams = ["fable", "opus", "sonnet", "other"];
  const fold = (t, f) => f === "other"
    ? ["haiku", "other"].reduce((a, k) => a + ((t[k] || {})[state.measure] || 0), 0)
    : ((t[f] || {})[state.measure] || 0);
  const cats = days.map((d) => ({ label: fmtTime(Date.parse(`${d.day}T12:00:00Z`), { month: "short", day: "numeric", timeZone: "UTC" }), tip: d.day }));
  const stacks = fams.map((f) => ({ key: f, label: f === "other" ? "Other (incl. Haiku)" : FAMILY_LABEL[f], color: entityColor(f),
    values: days.map((d) => fold(d.tokens, f)) }));
  columns($("tokens-chart"), { cats, stacks, fmtValue: fmtTokens, ariaLabel: "Claude Code tokens per day by model family" });
  tableView($("tokens-chart"), ["Day", ...stacks.map((s) => s.label), "Total"],
    days.slice().reverse().map((d, i) => {
      const idx = days.length - 1 - i;
      const vals = stacks.map((s) => s.values[idx]);
      return [d.day, ...vals.map(fmtTokens), fmtTokens(vals.reduce((a, b) => a + b, 0))];
    }));
  const src = sum.sources || [];
  $("tokens-sub").textContent = src.length
    ? `By model family. From ${src.map((s) => `${s.source} (${s.received_at ? `sent ${ago(s.received_at)}` : "no data"})`).join(", ")}.`
    : "By model family. No machine is sending Claude Code token totals yet; set up cc-push on your Mac.";
}

function renderImplied(est) {
  const root = $("implied-chart");
  const wins = est.windows.filter((w) => w.implied);
  if (!wins.length) {
    root.replaceChildren();
    el("div", { class: "empty" }, root,
      "Not enough data yet. This needs Claude Code token totals from cc-push and at least 5% weekly usage in a window.");
    return;
  }
  const label = (w) => `${fmtTime(ms(w.start), { month: "short", day: "numeric" })}–${fmtTime(ms(w.end), { month: "short", day: "numeric" })}`;
  const faded = wins.map((w) => !w.complete || w.partial_tokens || w.partial_polls);
  const cats = wins.map((w, i) => ({
    label: fmtTime(ms(w.end), { month: "short", day: "numeric" }), tip: `Week ending ${label(w)}`, flag: w.flag,
    extra: [
      { value: fmtPct(w.weekly_pct), label: "weekly used" },
      { value: w.cc_share_pct == null ? "–" : `${w.cc_share_pct}%`, label: "of it Claude Code" },
      ...(faded[i] ? [{ value: "●", label: w.complete ? "partial data" : "window in progress" }] : []),
      ...(w.flag ? [{ value: "▲", label: w.flag.message }] : []),
    ],
  }));
  columns(root, { cats, faded, fmtValue: fmtTokens, ariaLabel: "Implied weekly limit in tokens per window",
    stacks: [{ key: "weekly", label: "Implied tokens at 100%", color: entityColor("weekly"), values: wins.map((w) => w.implied[state.measure]) }] });
  tableView(root, ["Week", "Weekly used", "Claude Code share", "CC tokens", "Implied at 100%", "Note"],
    wins.slice().reverse().map((w) => [label(w), fmtPct(w.weekly_pct), w.cc_share_pct == null ? "–" : `${w.cc_share_pct}%`,
      fmtTokens(w.tokens[state.measure]), fmtTokens(w.implied[state.measure]),
      w.flag ? `▲ ${w.flag.message}` : !w.complete ? "in progress" : (w.partial_tokens || w.partial_polls) ? "partial data" : ""]));
}

function renderEvents(ev) {
  const ul = $("events"); ul.replaceChildren();
  if (!ev.events.length) { el("li", {}, ul, "No changes detected yet."); return; }
  for (const e of ev.events) {
    const li = el("li", {}, ul);
    el("time", { datetime: e.at }, li, fmtTime(ms(e.at), { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }));
    const body = el("div", {}, li);
    el("span", { class: "badge" }, body, e.severity === "limit" ? "▲ Limit" : "● Info");
    body.appendChild(document.createTextNode(e.message));
  }
}

function renderAll() {
  const d = state.data;
  if (!d) return;
  renderHealth(d.sum); renderTiles(d.sum); renderSeries(d.ser); renderTokens(d.daily, d.sum); renderImplied(d.est); renderEvents(d.ev);
}

async function load() {
  const app = $("app");
  app.classList.add("loading");
  try {
    const q = `?range=${encodeURIComponent(state.range)}`;
    const [sum, ser, daily, est, ev] = await Promise.all([
      api("/api/summary"), api(`/api/series${q}`), api(`/api/daily${q}`), api("/api/estimate"), api("/api/events")]);
    state.tz = sum.tz;
    state.data = { sum, ser, daily, est, ev };
    renderAll();
  } catch (err) {
    const h = $("health"); h.replaceChildren();
    el("span", { class: "dot critical" }, h);
    el("span", {}, h, `▲ Can't load data (${err.message})`);
  } finally {
    app.classList.remove("loading");
  }
}

function initControls() {
  const seg = $("range");
  const mark = () => seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === state.range)));
  seg.addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    state.range = b.dataset.v; mark();
    try { localStorage.setItem("cu-range", state.range); } catch (e) { /* ignore */ }
    load();
  });
  mark();
  const sel = $("measure");
  sel.value = state.measure;
  sel.addEventListener("change", () => {
    state.measure = sel.value;
    try { localStorage.setItem("cu-measure", state.measure); } catch (e) { /* ignore */ }
    renderAll();
  });
  let rt, lastW = 0;
  new ResizeObserver((entries) => {
    const w = Math.round(entries[0].contentRect.width);
    if (w === lastW) return;  // only width changes need a re-render; height changes come from rendering itself
    lastW = w; clearTimeout(rt); rt = setTimeout(renderAll, 150);
  }).observe($("app"));
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", renderAll);
}

initControls();
load();
setInterval(load, 60000);
