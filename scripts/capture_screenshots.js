const { chromium, devices } = require('playwright');
const path = require('path');

async function capture() {
  const browser = await chromium.launch({
    channel: 'chrome',
    headless: true
  });

  const assetsDir = path.resolve(__dirname, '..', 'assets');

  // 1. Desktop Light (Pipeline Board)
  {
    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      deviceScaleFactor: 2
    });
    const page = await context.newPage();
    await page.goto('http://localhost:3333');
    await page.waitForTimeout(3000);
    await page.evaluate(() => {
      localStorage.setItem('pmqa_theme', 'light');
      document.documentElement.classList.remove('dark');
    });
    await page.waitForTimeout(1000);
    await page.screenshot({ path: path.join(assetsDir, 'pipeline-board.png') });
    console.log('Captured pipeline-board.png');

    // 2. Mobile Connect Modal
    await page.evaluate(() => {
      if (typeof openMobileConnectModal === 'function') openMobileConnectModal();
    });
    await page.waitForTimeout(1500);
    await page.screenshot({ path: path.join(assetsDir, 'mobile-connect-modal.png') });
    console.log('Captured mobile-connect-modal.png');

    // Close modal
    await page.evaluate(() => {
      if (typeof closeMobileConnectModal === 'function') closeMobileConnectModal();
    });
    await page.waitForTimeout(500);

    // 3. QA & Heavy Gates Tab
    await page.evaluate(() => {
      if (typeof switchTab === 'function') switchTab('qa');
    });
    await page.waitForTimeout(2000);
    await page.screenshot({ path: path.join(assetsDir, 'qa-station.png') });
    console.log('Captured qa-station.png');

    // 4. Dedicated QA Slide-Over Drawer
    await page.evaluate(() => {
      if (typeof openQaDrawerById === 'function') openQaDrawerById('PQC-24');
    });
    await page.waitForTimeout(1500);
    await page.screenshot({ path: path.join(assetsDir, 'qa-card-drawer.png') });
    console.log('Captured qa-card-drawer.png');

    // Close drawer
    await page.evaluate(() => {
      if (typeof closeQaDrawer === 'function') closeQaDrawer();
    });
    await page.waitForTimeout(500);

    // 5. Active Work & DoD Tab
    await page.evaluate(() => {
      if (typeof switchTab === 'function') switchTab('sprint-dod');
    });
    await page.waitForTimeout(2000);
    await page.screenshot({ path: path.join(assetsDir, 'active-work-dod.png') });
    console.log('Captured active-work-dod.png');

    await context.close();
  }

  // 6. Desktop Dark (Pipeline Board in Carbon Zinc)
  {
    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      deviceScaleFactor: 2
    });
    const page = await context.newPage();
    await page.goto('http://localhost:3333');
    await page.waitForTimeout(2000);
    await page.evaluate(() => {
      localStorage.setItem('pmqa_theme', 'dark');
      document.documentElement.classList.add('dark');
    });
    await page.waitForTimeout(1000);
    await page.screenshot({ path: path.join(assetsDir, 'pipeline-board-dark.png') });
    console.log('Captured pipeline-board-dark.png');
    await context.close();
  }

  // 7. Mobile Views (iPhone 14)
  {
    const iPhone = devices['iPhone 14'];
    const context = await browser.newContext({
      ...iPhone,
      deviceScaleFactor: 2
    });
    const page = await context.newPage();
    await page.goto('http://localhost:3333');
    await page.waitForTimeout(3000);
    await page.evaluate(() => {
      localStorage.setItem('pmqa_theme', 'light');
      document.documentElement.classList.remove('dark');
      if (typeof scrollToColumn === 'function') scrollToColumn('qa');
    });
    await page.waitForTimeout(1000);
    await page.screenshot({ path: path.join(assetsDir, 'mobile-pipeline.png') });
    console.log('Captured mobile-pipeline.png');

    // Mobile navigation sidebar open
    await page.evaluate(() => {
      if (typeof toggleMobileSidebar === 'function') toggleMobileSidebar();
    });
    await page.waitForTimeout(1000);
    await page.screenshot({ path: path.join(assetsDir, 'mobile-sidebar.png') });
    console.log('Captured mobile-sidebar.png');

    // Close mobile sidebar and switch to QA tab
    await page.evaluate(() => {
      if (typeof toggleMobileSidebar === 'function') toggleMobileSidebar();
      if (typeof switchTab === 'function') switchTab('qa');
    });
    await page.waitForTimeout(2000);
    await page.screenshot({ path: path.join(assetsDir, 'mobile-qa.png') });
    console.log('Captured mobile-qa.png');

    await context.close();
  }

  await browser.close();
  console.log('All screenshots captured successfully!');
}

capture().catch(err => {
  console.error(err);
  process.exit(1);
});
