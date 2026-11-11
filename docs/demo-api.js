
// ---------------------------------------------------------------------------
// Port of app/engine.py — the parts the shop UI depends on. Kept in one place
// so the demo behaves like the server: cycle maths, risk bands, rendering.
// ---------------------------------------------------------------------------
const Rebuy = (() => {
  const CATEGORY_DEFAULT_CYCLES = {"Grains & Flour":30,"Cooking Oil":45,"Sugar":30,"Salt":60,"Beverages":14,"Water":7,"Airtime & Data":14,"Fuel & Gas":30,"Charcoal":21,"Personal Care":45,"Cosmetics":60,"Cleaning":30,"Baby Care":21,"Dairy":5,"Bakery":3,"Meat & Fish":5,"Produce":4,"Pharmacy":30,"Stationery":60,"Hardware":180,"Electronics":365,"Pet Care":30,"Agrovet":30,"Hair & Beauty":21,"Auto Service":90,"Laundry":7,"Fitness":30,"General":30};

  const RISK_BANDS = {
    new:      {label: "New",      colour: "#6366f1", blurb: "First purchase — welcome them"},
    healthy:  {label: "Healthy",  colour: "#10b981", blurb: "Buying at a normal rhythm"},
    due:      {label: "Due",      colour: "#f59e0b", blurb: "About to run out"},
    overdue:  {label: "Overdue",  colour: "#f97316", blurb: "Past their usual cycle"},
    at_risk:  {label: "At risk",  colour: "#ef4444", blurb: "Rhythm has broken"},
    lost:     {label: "Lost",     colour: "#6b7280", blurb: "Worth a win-back offer"},
  };

  function addDays(dateStr, n) {
    const d = new Date(dateStr + "T00:00:00");
    d.setDate(d.getDate() + n);
    return d.toISOString().slice(0, 10);
  }

  function diffDays(a, b) {
    const x = new Date(a + "T00:00:00"), y = new Date(b + "T00:00:00");
    return Math.round((x - y) / 86400000);
  }

  // effective_cycle(): a bulk purchase should not trigger the same reminder as
  // a single unit — scale the interval by how many units were bought.
  function effectiveCycle(product, qty) {
    const kind = (product && product.kind) || "goods";
    const base = Number((product && product.cycle_days) || CATEGORY_DEFAULT_CYCLES[(product || {}).category] || 30);
    if (kind === "service") return [Math.max(1, Math.round(base)), "service"];
    const baseline = Number((product && product.baseline_qty) || 1) || 1;
    const units = Math.max(1, Number(qty) || 1);
    const scaled = Math.round(base * Math.max(1, units / baseline));
    return [Math.min(scaled, 365), "goods"];
  }

  function riskBand(customer, avgCycle) {
    if (!customer.visit_count) return "new";
    if (!customer.last_purchase_at) return "new";
    const cycle = Math.max(6, Number(avgCycle) || 30);
    const ratio = diffDays(TODAY, customer.last_purchase_at) / cycle;
    if (ratio < 0.8) return "healthy";
    if (ratio < 1.15) return "due";
    if (ratio < 1.7) return "overdue";
    if (ratio < 3.2) return "at_risk";
    return "lost";
  }

  function money(n, currency) {
    const v = Number(n || 0);
    try {
      return new Intl.NumberFormat("en-UG", {style: "currency", currency: currency || "UGX",
        maximumFractionDigits: 0}).format(v);
    } catch (e) {
      return (currency || "UGX") + " " + Math.round(v).toLocaleString();
    }
  }

  // render(): two-pass {placeholder} substitution, same aliases as the server
  function render(body, ctx) {
    let out = String(body || "");
    for (let pass = 0; pass < 2; pass++) {
      out = out.replace(/\{([a-z_]+)\}/gi, (m, key) => {
        const v = ctx[key];
        return v === undefined || v === null ? "" : String(v);
      });
    }
    return out.replace(/\s{2,}/g, " ").trim();
  }

  return {CATEGORY_DEFAULT_CYCLES, RISK_BANDS, addDays, diffDays, effectiveCycle, riskBand, money, render};
})();


// ---------------------------------------------------------------------------
// Static API: answers the same URLs the real server does, from demo.json.
// Reads are served from the snapshot; writes are refused with a clear message
// so nobody thinks they changed anything.
// ---------------------------------------------------------------------------
(function () {
  const realFetch = window.fetch.bind(window);
  let demo = null;
  let ready = null;

  const READ_ONLY = "This is a read-only demo snapshot on GitHub Pages. Run the app locally (python3 serve.py) for the full write flows.";

  function load() {
    if (!ready) {
      ready = realFetch("demo.json", {cache: "no-store"})
        .then(r => {
          if (!r.ok) throw new Error("demo.json HTTP " + r.status);
          return r.json();
        })
        .then(d => (demo = d))
        .catch(err => { ready = null; throw err; });   // allow a retry on the next call
    }
    return ready;
  }

  function nowIso() {
    return new Date().toISOString().slice(0, 19).replace("T", " ");
  }

  function kpis() {
    const d = demo;
    const sentReminders = d.reminders.filter(r => r.sent_at || ["sent","delivered","replied","converted","failed"].includes(r.status));
    const converted = d.reminders.filter(r => r.status === "converted");
    const dueToday = d.reminders.filter(r => r.is_due);
    const revenue = d.sales.reduce((a, s) => a + Number(s.total || 0), 0);
    const recovered = converted.reduce((a, r) => a + Number(r.revenue || 0), 0);
    const outbound = d.messages.filter(m => m.direction === "out");
    const inbound = d.messages.filter(m => m.direction === "in");
    const healthy = d.customers.filter(c => ["healthy", "due"].includes(c.risk_band));
    const atRisk = d.customers.filter(c => ["at_risk", "lost"].includes(c.risk_band));
    return {
      revenue_total: revenue,
      revenue_recovered: recovered,
      revenue_at_risk: atRisk.reduce((a, c) => a + Number(c.lifetime_value || c.total_spent || 0), 0),
      recovery_rate: sentReminders.length ? Math.round(1000 * converted.length / sentReminders.length) / 10 : 0,
      reminders_sent: sentReminders.length,
      conversions: converted.length,
      replies: inbound.length,
      reply_rate: outbound.length ? Math.round(1000 * inbound.length / outbound.length) / 10 : 0,
      due_today: dueToday.length,
      customers: d.customers.length,
      active_customers: d.customers.filter(c => c.opted_in).length,
      loyalty_points_outstanding: d.customers.reduce((a, c) => a + Number(c.loyalty_points || 0), 0),
      retention_score: d.customers.length ? Math.round(100 * healthy.length / d.customers.length) : 0,
      at_risk: atRisk.length,
      avg_basket: d.sales.length ? Math.round(revenue / d.sales.length) : 0,
      messages_out: outbound.length,
      messages_in: inbound.length,
    };
  }

  function series(kind, days) {
    const d = demo;
    const source = kind === "messages" ? d.messages : d.sales;
    const dateKey = kind === "messages" ? "created_at" : "sale_date";
    const buckets = {};
    source.forEach(row => {
      const key = String(row[dateKey] || "").slice(0, 10);
      if (!key) return;
      buckets[key] = buckets[key] || {value: 0, n: 0};
      buckets[key].value += kind === "messages" ? 1 : Number(row.total || 0);
      buckets[key].n += 1;
    });
    const out = [];
    for (let i = days - 1; i >= 0; i--) {
      const day = Rebuy.addDays(d.today, -i);
      const b = buckets[day] || {value: 0, n: 0};
      out.push({day: day, value: Math.round(b.value), n: b.n});
    }
    return out;
  }

  function segments() {
    const d = demo;
    const counts = {at_risk: 0, lost: 0, due: 0, vip: 0, lapsed_60: 0, new: 0, services: 0};
    d.customers.forEach(c => {
      if (c.risk_band === "at_risk") counts.at_risk++;
      if (c.risk_band === "lost") counts.lost++;
      if (c.risk_band === "due") counts.due++;
      if (Number(c.total_spent) >= 250000) counts.vip++;
      if (c.last_purchase_at && Rebuy.diffDays(d.today, c.last_purchase_at) >= 60) counts.lapsed_60++;
      if (Number(c.visit_count) <= 1) counts.new++;
    });
    counts.services = d.products.filter(p => p.kind === "service").length;
    return Object.entries(counts).map(([key, count]) => ({
      key: key, count: count,
      label: key.replace(/_/g, " ").replace(/\b\w/g, m => m.toUpperCase()),
    }));
  }

  function withCustomer(r) {
    const c = demo.customers.find(x => x.id === r.customer_id) || {};
    return Object.assign({}, r, {customer: c.name || "Customer", phone: c.phone || "",
                                 risk_band: c.risk_band || "new"});
  }

  function route(path, url) {
    const p = path.replace(/\/+$/, "") || "/";
    const q = url.searchParams;
    const d = demo;

    if (p === "/api/health") return {ok: true, time: nowIso(), shops: 1, backend: "static-demo",
      note: "Snapshot generated " + d.generated_at};
    if (p === "/api/meta/cycles") return {categories: d.meta.category_cycles, risk_bands: d.meta.risk_bands};
    if (p === "/api/auth/me") return {authenticated: true, user: d.user, shop: d.shop};

    if (p === "/api/shop") {
      const due = d.reminders.filter(r => r.is_due).length;
      return {shop: d.shop, counts: {
        customers: d.customers.length, products: d.products.length, sales: d.sales.length,
        reminders_due: due, reminders_open: d.reminders.filter(r => r.status === "scheduled").length,
        messages: d.messages.length, coupons: d.coupons.length,
      }};
    }

    if (p === "/api/onboarding") {
      const steps = [
        ["add_products", "Add your products", d.products.length > 0],
        ["add_customers", "Get customers in", d.customers.length > 0],
        ["record_sale", "Record a sale", d.sales.length > 0],
        ["connect_channel", "Connect WhatsApp", true],
        ["first_reminder", "Send a reminder", d.messages.length > 0],
        ["loyalty", "Turn on loyalty", !!d.shop.loyalty_enabled],
      ].map(([key, label, done]) => ({key: key, label: label, done: done}));
      return {onboarding: steps, steps: steps, completed: steps.filter(s => s.done).length, total: steps.length};
    }

    if (p === "/api/dashboard") {
      const days = Number(q.get("days") || 30);
      const riskMix = Object.entries(d.customers.reduce((a, c) => (a[c.risk_band] = (a[c.risk_band] || 0) + 1, a), {}))
        .map(([band, n]) => ({band: band, n: n})).sort((a, b) => b.n - a.n);
      const channelMix = Object.entries(d.messages.reduce((a, m) => (a[m.channel] = (a[m.channel] || 0) + 1, a), {}))
        .map(([channel, n]) => ({channel: channel, n: n,
          failed: d.messages.filter(m => m.channel === channel && m.status === "failed").length}));
      const topProducts = Object.values(d.sale_items.reduce((a, it) => {
        const k = it.product_name;
        a[k] = a[k] || {name: k, purchases: 0, revenue: 0, cycle: it.cycle_days, risk_band: "healthy"};
        a[k].purchases += 1;
        a[k].revenue += Number(it.line_total || 0);
        return a;
      }, {})).sort((x, y) => y.revenue - x.revenue).slice(0, 8);

      const upcoming = d.reminders.filter(r => r.status === "scheduled").slice(0, 8).map(withCustomer);
      const needsAction = d.messages.filter(m => m.direction === "in").slice(0, 6).map(m => {
        const c = d.customers.find(x => x.id === m.customer_id) || {};
        return {customer: c.name || "Customer", phone: c.phone || "", body: m.body,
                created_at: m.created_at, intent: m.intent || "", customer_id: m.customer_id};
      });
      const recentSales = d.sales.slice(0, 6).map(s => Object.assign({}, s, {
        customer: (d.customers.find(c => c.id === s.customer_id) || {}).name || "Walk-in"}));

      return {
        shop: d.shop, kpis: kpis(), risk_mix: riskMix, channel_mix: channelMix,
        revenue_series: series("sales", days), message_series: series("messages", days),
        top_products: topProducts, upcoming: upcoming, needs_action: needsAction,
        recent_sales: recentSales,
      };
    }

    if (p === "/api/customers") {
      const per = Number(q.get("per") || 25), page = Number(q.get("page") || 1);
      const term = (q.get("q") || "").toLowerCase();
      const band = q.get("band") || "";
      let rows = d.customers.slice();
      if (term) rows = rows.filter(c => (c.name + " " + (c.phone || "")).toLowerCase().includes(term));
      if (band) rows = rows.filter(c => c.risk_band === band);
      const sort = q.get("sort") || "spend";
      rows.sort((a, b) => sort === "recent"
        ? String(b.last_purchase_at).localeCompare(String(a.last_purchase_at))
        : sort === "name" ? a.name.localeCompare(b.name)
        : Number(b.total_spent) - Number(a.total_spent));
      const pages = Math.max(1, Math.ceil(rows.length / per));
      const stats = {
        total: d.customers.length,
        opted_in: d.customers.filter(c => c.opted_in).length,
        at_risk: d.customers.filter(c => ["at_risk", "lost"].includes(c.risk_band)).length,
        new_30d: d.customers.filter(c => c.visit_count <= 1).length,
        avg_spend: d.customers.length ? Math.round(d.customers.reduce((a, c) => a + Number(c.total_spent || 0), 0) / d.customers.length) : 0,
      };
      return {rows: rows.slice((page - 1) * per, page * per), customers: rows, total: rows.length,
              page: page, pages: pages, per: per, stats: stats,
              bands: Object.entries(d.customers.reduce((a, c) => (a[c.risk_band] = (a[c.risk_band] || 0) + 1, a), {}))
                .map(([band, n]) => ({band: band, n: n}))};
    }

    let m = p.match(/^\/api\/customers\/(\d+)$/);
    if (m) {
      const id = Number(m[1]);
      const c = d.customers.find(x => x.id === id);
      if (!c) return {error: "customer not found", status: 404};
      return {
        customer: c,
        sales: d.sales.filter(s => s.customer_id === id).map(s => Object.assign({}, s,
          {items: d.sale_items.filter(it => it.sale_id === s.id)})),
        reminders: d.reminders.filter(r => r.customer_id === id).map(withCustomer),
        messages: d.messages.filter(x => x.customer_id === id),
        loyalty: d.loyalty_events.filter(e => e.customer_id === id),
        coupons: d.coupons.filter(cp => cp.customer_id === id),
      };
    }

    if (p === "/api/reminders") {
      const due = q.get("due") || "", status = q.get("status") || "", kind = q.get("kind") || "";
      let rows = d.reminders.slice();
      if (due === "today") rows = rows.filter(r => r.is_due);
      else if (due === "week") rows = rows.filter(r => r.days_until !== null && r.days_until >= 0 && r.days_until <= 7);
      if (status) rows = rows.filter(r => r.status === status);
      if (kind) rows = rows.filter(r => r.kind === kind);
      const counts = d.reminders.reduce((a, r) => (a[r.status] = (a[r.status] || 0) + 1, a), {});
      return {
        rows: rows.slice(0, 200).map(withCustomer),
        counts: {scheduled: counts.scheduled || 0, sent: counts.sent || 0, delivered: counts.delivered || 0,
                 replied: counts.replied || 0, converted: counts.converted || 0, failed: counts.failed || 0,
                 cancelled: counts.cancelled || 0, due_today: d.reminders.filter(r => r.is_due).length},
        stats: {upcoming: d.reminders.filter(r => r.status === "scheduled").length,
                due_today: d.reminders.filter(r => r.is_due).length,
                sent: d.reminders.filter(r => r.sent_at).length,
                converted: d.reminders.filter(r => r.status === "converted").length},
        recent: d.reminders.filter(r => r.sent_at).slice(0, 25).map(withCustomer),
      };
    }

    if (p === "/api/inbox") {
      const byCustomer = {};
      d.messages.forEach(msg => {
        const k = msg.customer_id || 0;
        byCustomer[k] = byCustomer[k] || [];
        byCustomer[k].push(msg);
      });
      const convos = Object.entries(byCustomer).map(([cid, msgs]) => {
        const id = Number(cid);
        const cust = d.customers.find(x => x.id === id) || {};
        const sorted = msgs.slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
        return {customer_id: id, customer: cust.name || "Customer", name: cust.name || "Customer",
                phone: cust.phone || "", risk_band: cust.risk_band || "new", opted_in: cust.opted_in,
                last_at: sorted[0] && sorted[0].created_at, at: sorted[0] && sorted[0].created_at,
                preview: sorted[0] && sorted[0].body, last_body: sorted[0] && sorted[0].body,
                direction: sorted[0] && sorted[0].direction,
                out_count: msgs.filter(x => x.direction === "out").length,
                in_count: msgs.filter(x => x.direction === "in").length,
                messages: sorted.reverse()};
      }).sort((a, b) => String(b.last_at).localeCompare(String(a.last_at)));
      return {conversations: convos.slice(0, 60), threads: convos.slice(0, 60),
        stats: {total: d.messages.length,
          out: d.messages.filter(x => x.direction === "out").length,
          inbound: d.messages.filter(x => x.direction === "in").length,
          failed: d.messages.filter(x => x.status === "failed").length,
          queued: d.messages.filter(x => x.status === "queued").length}};
    }

    if (p === "/api/products") {
      return {rows: d.products, products: d.products,
        categories: Object.entries(d.products.reduce((a, x) => (a[x.category] = (a[x.category] || 0) + 1, a), {}))
          .map(([category, n]) => ({category: category, n: n}))};
    }

    if (p === "/api/sales") {
      const per = Number(q.get("per") || 25), page = Number(q.get("page") || 1);
      const rows = d.sales.map(s => Object.assign({}, s, {
        customer: (d.customers.find(c => c.id === s.customer_id) || {}).name || "Walk-in",
        items: d.sale_items.filter(it => it.sale_id === s.id)}));
      const revenue = rows.reduce((a, s) => a + Number(s.total || 0), 0);
      return {rows: rows.slice((page - 1) * per, page * per), sales: rows, total: rows.length,
        page: page, pages: Math.max(1, Math.ceil(rows.length / per)), per: per,
        summary: {count: rows.length, revenue: revenue,
          basket: rows.length ? Math.round(revenue / rows.length) : 0,
          items: d.sale_items.length}};
    }

    m = p.match(/^\/api\/sales\/(\d+)$/);
    if (m) {
      const s = d.sales.find(x => x.id === Number(m[1]));
      if (!s) return {error: "sale not found", status: 404};
      return {sale: s, items: d.sale_items.filter(it => it.sale_id === s.id),
        customer: d.customers.find(c => c.id === s.customer_id) || null};
    }

    if (p === "/api/reports") {
      const days = Number(q.get("days") || 30);
      const kinds = ["replenish", "service_due", "followup", "winback", "loyalty_reward", "birthday"];
      const byKind = kinds.map(k => {
        const rows = d.reminders.filter(r => r.kind === k);
        const sent = rows.filter(r => r.sent_at).length;
        const converted = rows.filter(r => r.status === "converted").length;
        return {kind: k, sent: sent, converted: converted,
          revenue: Math.round(rows.reduce((a, r) => a + Number(r.revenue || 0), 0)),
          rate: sent ? Math.round(1000 * converted / sent) / 10 : 0};
      });
      const byChannel = ["whatsapp", "sms", "email"].map(ch => {
        const rows = d.reminders.filter(r => r.channel === ch && r.sent_at);
        return {channel: ch, sent: rows.length,
          delivered: rows.filter(r => ["delivered", "replied", "converted"].includes(r.status)).length,
          converted: rows.filter(r => r.status === "converted").length,
          failed: rows.filter(r => r.status === "failed").length,
          revenue: Math.round(rows.reduce((a, r) => a + Number(r.revenue || 0), 0))};
      });
      const byProduct = Object.values(d.reminders.reduce((a, r) => {
        const k = r.product_name || "(other)";
        a[k] = a[k] || {product_name: k, product: k, reminders: 0, sent: 0, converted: 0, revenue: 0};
        a[k].reminders += 1;
        if (r.sent_at) a[k].sent += 1;
        if (r.status === "converted") a[k].converted += 1;
        a[k].revenue += Number(r.revenue || 0);
        return a;
      }, {})).sort((a, b) => b.revenue - a.revenue).slice(0, 12);
      const funnel = [
        {step: "Reminders sent", n: d.reminders.filter(r => r.sent_at).length},
        {step: "Delivered", n: d.reminders.filter(r => ["delivered", "replied", "converted"].includes(r.status)).length},
        {step: "Replied", n: d.reminders.filter(r => ["replied", "converted"].includes(r.status)).length},
        {step: "Bought", n: d.reminders.filter(r => r.status === "converted").length},
      ];
      const cohorts = [0, 1, 2, 3].map(i => {
        const month = Rebuy.addDays(d.today, -30 * (i + 1)).slice(0, 7);
        const fresh = d.customers.filter(c => String(c.first_seen || "").slice(0, 7) === month);
        const returned = fresh.filter(c => Number(c.visit_count) > 1);
        return {cohort: month, customers: fresh.length, returned: returned.length,
          rate: fresh.length ? Math.round(1000 * returned.length / fresh.length) / 10 : 0};
      }).reverse();
      return {window_days: days, by_kind: byKind, by_channel: byChannel, by_product: byProduct,
        funnel: funnel, cohorts: cohorts, revenue_series: series("sales", days),
        message_series: series("messages", days),
        coupons: {issued: d.coupons.length, redeemed: d.coupons.filter(c => c.status === "used").length,
          active: d.coupons.filter(c => c.status === "active").length},
        insight: "Replenishment reminders drive most recovered revenue here — " +
          (byKind[0] ? byKind[0].revenue.toLocaleString() : "0") + " from " + (byKind[0] ? byKind[0].sent : 0) + " messages.",
        kpis: kpis()};
    }

    if (p === "/api/campaigns") return {campaigns: d.campaigns, segments: segments(), templates: d.templates};
    if (p === "/api/templates") return {templates: d.templates};
    if (p === "/api/templates/preview" || p === "/api/campaigns/preview") {
      const t = d.templates[0] || {body: "", subject: ""};
      return {html: t.body, body: t.body, subject: t.subject || ""};
    }

    if (p === "/api/loyalty") {
      const leaderboard = d.customers.slice().sort((a, b) => Number(b.loyalty_points) - Number(a.loyalty_points)).slice(0, 20);
      return {
        enabled: !!d.shop.loyalty_enabled, rule: d.shop.loyalty_rule || "buy_5_get_1",
        leaderboard: leaderboard, customers: leaderboard,
        events: d.loyalty_events.slice(0, 100),
        stats: {ready: d.customers.filter(c => Number(c.loyalty_points) >= 5).length,
          points_issued: d.customers.reduce((a, c) => a + Number(c.loyalty_points || 0), 0),
          members: d.customers.filter(c => Number(c.loyalty_points) > 0).length},
      };
    }

    if (p === "/api/coupons") {
      return {rows: d.coupons.slice(0, 200), coupons: d.coupons.slice(0, 200),
        stats: {total: d.coupons.length, used: d.coupons.filter(c => c.status === "used").length,
          active: d.coupons.filter(c => c.status === "active").length,
          discount: d.coupons.filter(c => c.status === "used").reduce((a, c) => a + Number(c.discount_value || 0), 0)}};
    }

    if (p === "/api/settings/channels") {
      return {channels: [
        {channel: "whatsapp", driver: "sandbox", enabled: true, live: false,
         numbers: d.shop.whatsapp_number ? [d.shop.whatsapp_number] : []},
        {channel: "sms", driver: "sandbox", enabled: true, live: false, numbers: []},
        {channel: "email", driver: "sandbox", enabled: true, live: false, sender: d.user && d.user.email},
      ], health: {configured: false, mode: "sandbox"}};
    }
    if (p === "/api/settings") return {rows: [], shop: d.shop};
    if (p.startsWith("/api/import/") || p === "/api/receipts") {
      return {jobs: [], receipts: [], sample: "", rows: [], preview: [], columns: []};
    }
    return null;
  }

  // Minimal Response stand-in: the SPA only uses ok/status/json()/text(),
  // and this keeps the shim working where `Response` is absent.
  function reply(body, status) {
    // A real Response lets you read the body once. Mirroring that exactly would
    // be easy to trip over, so each reader gets its own copy of the payload.
    return {
      ok: status >= 200 && status < 300,
      status: status,
      headers: {get: () => "application/json"},
      json: () => Promise.resolve(JSON.parse(JSON.stringify(body))),
      text: () => Promise.resolve(JSON.stringify(body)),
    };
  }

  window.fetch = function (input, init) {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (!url.pathname.startsWith("/api/")) return realFetch(input, init);
    const method = ((init && init.method) || "GET").toUpperCase();
    const isWrite = method !== "GET" && method !== "HEAD";
    if (url.pathname === "/api/auth/login") {
      return load().then(() => reply({ok: true, user: demo.user, shop: demo.shop}, 200));
    }
    if (url.pathname === "/api/auth/logout") return Promise.resolve(reply({ok: true}, 200));
    if (url.pathname === "/api/auth/me") {
      // never let a snapshot hiccup look like "you are logged out"
      return load().then(() => reply({authenticated: true, user: demo.user, shop: demo.shop}, 200))
        .catch(e => reply({authenticated: true, user: demo.user || {name: "Demo", role: "owner"},
                           shop: demo.shop || {name: "Rebuy demo"}, warning: String(e.message)}, 200));
    }
    return load().then(() => {
      if (isWrite) {
        // say so plainly instead of pretending the write happened
        return reply({error: READ_ONLY, demo: true}, 403);
      }
      const out = route(url.pathname, url);
      if (out === null) return reply({error: "not found in demo snapshot"}, 404);
      if (out && out.status && out.error) return reply(out, out.status);
      return reply(out, 200);
    }).catch(e => reply({error: "demo load failed: " + e.message}, 500));
  };

  // banner so it is obvious this is a canned snapshot
  document.addEventListener("DOMContentLoaded", () => {
    load().then(() => {
      const bar = document.createElement("div");
      bar.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:99999;background:#0f172a;color:#e2e8f0;" +
        "font:500 12px/1.5 system-ui,sans-serif;padding:9px 16px;text-align:center;border-top:1px solid #1e293b";
      bar.innerHTML = "<b>Live demo snapshot</b> &middot; " + demo.shop.name + " &middot; data as of " +
        demo.generated_at.slice(0, 16) + " &middot; read-only: buttons that save are disabled here. " +
        "Full app + API: run <code>python3 serve.py</code>, or see the Supabase deployment.";
      document.body.appendChild(bar);
      document.body.style.paddingBottom = "44px";
    });
  });
})();
