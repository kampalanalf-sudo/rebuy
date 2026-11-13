const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 } });
  const p = await ctx.newPage();
  const errs = [];
  p.on('pageerror', e => errs.push(e.message));
  await p.goto('https://rebuy.website/#/login', { waitUntil: 'networkidle' });
  await p.fill('input[type=email]', 'demo@c-care.ug');
  await p.fill('input[type=password]', 'CCare-Demo-2026!');
  await Promise.all([p.waitForNavigation({ waitUntil: 'networkidle' }).catch(()=>{}), p.click('button:has-text("Sign in")')]);
  await p.waitForTimeout(2500);
  // hash-only navigation: wait for the actual view to swap
  await p.evaluate(() => { location.hash = '#/inbox'; });
  await p.waitForFunction(() => document.body.innerText.includes('Conversations'), { timeout: 30000 }).catch(()=>{});
  await p.waitForTimeout(2500);
  console.log('url:', p.url());
  const txt = await p.evaluate(() => document.body.innerText.slice(0, 260).replace(/\s+/g, ' '));
  console.log('has Conversations:', txt.includes('Conversations'));
  const el = await p.evaluate(() => document.querySelector('h3') ? document.querySelector('h3').innerText : '(no h3)');
  console.log('first h3:', el);
  console.log('errors:', errs.length ? errs : 'none');
  await b.close();
})();
