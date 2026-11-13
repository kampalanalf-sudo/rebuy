const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const ctx = await b.newContext({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
  const p = await ctx.newPage();
  const errors = [];
  p.on('pageerror', e => errors.push('PAGEERROR ' + e.message));
  p.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });

  await p.goto('https://rebuy.website/#/login', { waitUntil: 'networkidle' });
  await p.fill('input[type=email]', 'demo@c-care.ug');
  await p.fill('input[type=password]', 'CCare-Demo-2026!');
  await Promise.all([p.waitForNavigation({ waitUntil: 'networkidle' }).catch(()=>{}), p.click('button:has-text("Sign in")')]);
  await p.waitForTimeout(3500);
  console.log('after login:', p.url());

  const sw = await p.evaluate(async () => {
    const reg = await navigator.serviceWorker.getRegistration();
    return { scope: reg?.scope, active: reg?.active?.state, controlled: !!navigator.serviceWorker.controller };
  });
  console.log('service worker:', JSON.stringify(sw));

  const caches = await p.evaluate(async () => {
    const out = {};
    for (const n of await caches.keys()) out[n] = (await (await caches.open(n)).keys()).length;
    return out;
  });
  console.log('caches:', JSON.stringify(caches));

  // login → dashboard KPIs actually rendered (proves the metric fixes shipped)
  await p.goto("https://rebuy.website/#/dashboard", { waitUntil: "networkidle" });
  await p.waitForSelector(".card.kpi", { timeout: 15000 }).catch(()=>{});
  await p.waitForTimeout(3000);
  const kpi = await p.evaluate(() => [...document.querySelectorAll('.card.kpi')].map(c =>
    `${c.querySelector('.label')?.innerText.trim()}: ${c.querySelector('.value')?.innerText.trim()} — ${c.querySelector('.foot')?.innerText.trim()}`));
  kpi.forEach(k => console.log('  ', k));

  // privacy + terms reachable from the login page
  await p.goto('https://rebuy.website/#/login', { waitUntil: 'networkidle' });
  const links = await p.evaluate(() => [...document.querySelectorAll('a[href*="privacy"], a[href*="terms"]')]
    .map(a => a.getAttribute('href')));
  console.log('legal links on login:', JSON.stringify(links));

  console.log('errors:', errors.length ? errors.slice(0,4) : 'none');
  await b.close();
})();
