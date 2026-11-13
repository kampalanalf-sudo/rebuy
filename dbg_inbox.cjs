const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 } });
  const p = await ctx.newPage();
  await p.goto('https://rebuy.website/#/login', { waitUntil: 'networkidle' });
  await p.fill('input[type=email]', 'demo@c-care.ug');
  await p.fill('input[type=password]', 'CCare-Demo-2026!');
  await Promise.all([p.waitForNavigation({ waitUntil: 'networkidle' }).catch(()=>{}), p.click('button:has-text("Sign in")')]);
  await p.waitForTimeout(2000);
  const api = await p.evaluate(async () => {
    const r = await fetch('/api/inbox');
    const j = await r.json().catch(() => null);
    return { status: r.status, convs: j?.conversations?.length ?? null, unread: j?.unread, first: j?.conversations?.[0] || null };
  });
  console.log('/api/inbox ->', JSON.stringify(api));
  const tr = await p.evaluate(async () => {
    const r = await fetch('/api/inbox/' + 61);
    return { status: r.status, body: (await r.text()).slice(0, 300) };
  });
  console.log('/api/inbox/61 ->', JSON.stringify(tr));
  await p.evaluate(() => { location.hash = '#/inbox'; });
  await p.waitForTimeout(4000);
  const html = await p.evaluate(() => {
    const main = document.querySelector('.main') || document.body;
    return main.innerText.slice(0, 700);
  });
  console.log('--- main text ---\n', html);
  await b.close();
})();
