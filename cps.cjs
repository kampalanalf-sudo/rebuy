const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const ctx = await b.newContext();
  const p = await ctx.newPage();
  await p.goto('https://rebuy.website/#/login', { waitUntil: 'networkidle' });
  await p.fill('input[type=email]', 'demo@c-care.ug');
  await p.fill('input[type=password]', 'CCare-Demo-2026!');
  await Promise.all([p.waitForNavigation({ waitUntil: 'networkidle' }).catch(()=>{}), p.click('button:has-text("Sign in")')]);
  await p.waitForTimeout(1500);
  const d = await p.evaluate(async () => {
    const shop = await (await fetch('/api/shop')).json();
    const onboard = await (await fetch('/api/onboarding')).json().catch(() => null);
    return { shop: shop.shop, counts: shop.counts, onboard };
  });
  console.log(JSON.stringify(d, null, 2).slice(0, 1600));
  await b.close();
})();
