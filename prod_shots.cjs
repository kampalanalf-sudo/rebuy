const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const jobs = [
    ['/#/dashboard', 'shot-phone-dashboard.png',  { width: 412, height: 915 }, 2.5, true],
    ['/#/reminders', 'shot-phone-reminders.png',  { width: 412, height: 915 }, 2.5, true],
    ['/#/customers', 'shot-phone-customers.png',  { width: 412, height: 915 }, 2.5, true],
    ['/#/dashboard', 'shot-dashboard.png',        { width: 1440, height: 1000 }, 1, false],
    ['/#/reminders', 'shot-reminders.png',        { width: 1440, height: 1000 }, 1, false],
    ['/#/inbox',     'shot-inbox.png',            { width: 1440, height: 1000 }, 1, false],
  ];
  for (const [route, file, vp, dpr, mobile] of jobs) {
    const ctx = await b.newContext({ viewport: vp, deviceScaleFactor: dpr, isMobile: mobile, hasTouch: mobile });
    const p = await ctx.newPage();
    await p.goto('https://rebuy.website/#/login', { waitUntil: 'networkidle' });
    await p.fill('input[type=email]', 'demo@c-care.ug');
    await p.fill('input[type=password]', 'CCare-Demo-2026!');
    await Promise.all([p.waitForNavigation({ waitUntil: 'networkidle' }).catch(()=>{}), p.click('button:has-text("Sign in")')]);
    await p.goto('https://rebuy.website/#/dashboard', { waitUntil: 'networkidle' });
    await p.waitForSelector('.card.kpi .value', { timeout: 45000 }).catch(() => {});
    // Hash-only navigation: the SPA swaps the view without a page load, so a
    // goto() to a different hash can be a no-op if the router has not booted.
    if (route !== '/#/dashboard') {
      // route is "/#/inbox"; assigning that to location.hash would produce
      // "##/inbox" and the router would see the path "/#/inbox".
      await p.evaluate((r) => { location.hash = r.replace(/^#/, ''); }, route.replace(/^\/#/, '#'));
    }
    // Wait for the view to actually render: "Loading…" is a real state on a
    // cold serverless function and screenshotting it produces a blank shot.
    await p.waitForFunction(() => !document.body.innerText.includes('Loading…'), { timeout: 45000 }).catch(() => {});
    await p.waitForTimeout(2500);
    await p.screenshot({ path: 'static/icons/' + file, type: 'png' });
    console.log('captured', file, 'from', route);
    await ctx.close();
  }
  await b.close();
})();
