
// ---------------------------------------------------------------------------
// Admin cockpit on GitHub Pages: every response in admin.json was produced by
// the real handler in app/api_admin.py, so the numbers the cockpit renders are
// the ones the server actually computed.
// ---------------------------------------------------------------------------
(function () {
  const realFetch = window.fetch.bind(window);
  let snap = null;
  let ready = null;
  const READ_ONLY = "Read-only demo snapshot — the admin actions (pause a shop, edit credentials, run the queue) are disabled here.";

  function load() {
    if (!ready) {
      ready = realFetch("admin.json", {cache: "no-store"})
        .then(r => { if (!r.ok) throw new Error("admin.json HTTP " + r.status); return r.json(); })
        .then(d => (snap = d))
        .catch(e => { ready = null; throw e; });
    }
    return ready;
  }

  function reply(body, status) {
    return {
      ok: status >= 200 && status < 300,
      status: status,
      headers: {get: () => "application/json"},
      json: () => Promise.resolve(JSON.parse(JSON.stringify(body))),
      text: () => Promise.resolve(JSON.stringify(body)),
    };
  }

  function route(path, url) {
    const p = path.replace(/\/+$/, "") || "/";
    const days = url.searchParams.get("days") || "30";
    if (p === "/api/auth/me") {
      return {authenticated: true, user: {id: 0, name: "Demo Admin", email: "admin@rebuy.app",
        role: "admin", shop_id: null}, shop: null};
    }
    if (p === "/api/auth/login") return {ok: true, user: {id: 0, name: "Demo Admin", email: "admin@rebuy.app", role: "admin"}};
    if (p === "/api/admin/overview") return snap.overview;
    if (p === "/api/admin/revenue") return snap.revenue;
    if (p === "/api/admin/shops") {
      const page = Number(url.searchParams.get("page") || 1);
      const per = Number(url.searchParams.get("per") || 25);
      const rows = (snap.shops && snap.shops.rows) || [];
      const term = (url.searchParams.get("search") || "").toLowerCase();
      const status = url.searchParams.get("status") || "";
      let filtered = rows;
      if (term) filtered = filtered.filter(s => (s.name + " " + (s.owner_email || "") + " " + (s.city || "")).toLowerCase().includes(term));
      if (status) filtered = filtered.filter(s => s.status === status);
      return {rows: filtered.slice((page - 1) * per, page * per), shops: filtered, total: filtered.length,
              page: page, pages: Math.max(1, Math.ceil(filtered.length / per)), per: per,
              stats: (snap.shops && snap.shops.stats) || {}};
    }
    let m = p.match(/^\/api\/admin\/shops\/(\d+)$/);
    if (m) return Number(m[1]) === 1 ? snap.shop_detail_1 : snap.shop_detail_1;
    if (p === "/api/admin/messaging") return snap.messaging;
    if (p === "/api/admin/retention") return snap.retention;
    if (p === "/api/admin/queue") return snap.queue;
    if (p === "/api/admin/capacity") return snap.capacity;
    if (p === "/api/admin/credentials") return snap.credentials;
    if (p === "/api/admin/audit") return snap.audit;
    if (p === "/api/admin/seed" || p === "/api/admin/tick" || p === "/api/admin/credentials/test") {
      return {error: READ_ONLY, demo: true};
    }
    return null;
  }

  window.fetch = function (input, init) {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (!url.pathname.startsWith("/api/")) return realFetch(input, init);
    const method = ((init && init.method) || "GET").toUpperCase();
    if (url.pathname === "/api/auth/logout") return Promise.resolve(reply({ok: true}, 200));
    if (method !== "GET" && method !== "HEAD") {
      return load().then(() => reply({error: READ_ONLY, demo: true}, 403))
        .catch(() => reply({error: READ_ONLY, demo: true}, 403));
    }
    return load().then(() => {
      const out = route(url.pathname, url);
      if (out === null) return reply({error: "not in demo snapshot"}, 404);
      return reply(out, 200);
    }).catch(e => reply({error: "snapshot failed: " + e.message}, 500));
  };

  document.addEventListener("DOMContentLoaded", () => {
    load().then(() => {
      const bar = document.createElement("div");
      bar.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:99999;background:#0b1220;color:#cbd5e1;" +
        "font:500 12px/1.5 system-ui,sans-serif;padding:9px 16px;text-align:center;border-top:1px solid #1e293b";
      bar.innerHTML = "<b>Admin demo snapshot</b> &middot; " + snap.overview.kpis.shops_total +
        " shops &middot; generated " + snap.generated_at.slice(0, 16) +
        " &middot; every figure below came from the real API handler &middot; writes are disabled";
      document.body.appendChild(bar);
      document.body.style.paddingBottom = "46px";
    }).catch(e => console.error(e));
  });
})();
