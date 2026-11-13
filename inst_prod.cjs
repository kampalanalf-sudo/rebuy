/* Ask Chrome itself whether this page is installable.
 * Page.getInstallabilityErrors is the same check that decides whether the
 * install icon appears in the address bar — stronger evidence than a
 * hand-rolled checklist. */
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch({ args: ['--no-sandbox'] });
  const ctx = await b.newContext({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
  const p = await ctx.newPage();
  await p.goto('https://rebuy.website/#/login', { waitUntil: 'networkidle' });
  await p.waitForTimeout(2500);           // let the SW install and activate
  await p.goto('https://rebuy.website/#/dashboard', { waitUntil: 'networkidle' });
  await p.waitForTimeout(2000);

  const cdp = await ctx.newCDPSession(p);
  await cdp.send('Page.enable');
  const errs = await cdp.send('Page.getInstallabilityErrors');
  console.log('installabilityErrors:', JSON.stringify(errs.installabilityErrors, null, 2) || '[]');

  const man = await cdp.send('Page.getAppManifest');
  console.log('manifest url:', man.url, '| errors:', JSON.stringify(man.errors));

  const parsed = JSON.parse(man.data || '{}');
  console.log('name:', parsed.name, '| short_name:', parsed.short_name);
  console.log('display:', parsed.display, '| start_url:', parsed.start_url, '| id:', parsed.id);
  console.log('icons:', (parsed.icons || []).map(i => `${i.sizes}${i.purpose ? '/' + i.purpose : ''}`).join(', '));
  await b.close();
})();
