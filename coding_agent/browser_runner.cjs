/* agent-lite 页面运行器：把本地页面真的在浏览器里跑一遍，把「看不见」变成「看得见」。
 *
 * 为什么需要它（来自真实 session 的教训）：前端/可视化类任务里，agent 只能靠静态推理，
 * 于是用户被迫手动开浏览器、抄控制台堆栈再粘回来 —— 3 轮往返只修掉 3 个运行时错误
 * （着色器编译失败、未定义变量、方法名写错），而这些错误 headless 跑一次就能全部拿到。
 *
 * 设计约束（都是踩过的坑）：
 *   · 复用用户已装的 Edge / Chrome（channel: msedge / chrome），不下 300MB Chromium；
 *   · 输出以**文本**为主（errors / warnings / blocked / 页面信息 / 交互是否改变画面），
 *     这样模型不吃图也能闭环；截图落盘给路径，只有体积小才由 Python 侧附加给模型；
 *   · 页面发起的网络请求在这里被拦截：只放行本地地址与白名单域名，其余 abort 并记录，
 *     免得浏览器变成刚封上的出网缺口；
 *   · 交互后做像素差分，把「拖拽/缩放到底生效没有」变成一个布尔值。
 *
 * 用法：node browser_runner.cjs <config.json>，往 stdout 打一行 JSON 报告。
 */
const fs = require('node:fs');

const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

function finish(report) {
  process.stdout.write(JSON.stringify(report));
}

function hostAllowed(url, allowed) {
  try {
    const parsed = new URL(url);
    if (parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost' || parsed.protocol === 'file:') {
      return true;
    }
    return allowed.some((domain) => parsed.hostname === domain || parsed.hostname.endsWith('.' + domain));
  } catch (err) {
    return false;
  }
}

(async () => {
  let playwright;
  try {
    ({ chromium } = require(config.playwright));
  } catch (err) {
    finish({ ok: false, fatal: `无法加载 playwright（${config.playwright}): ${err.message}` });
    return;
  }

  const report = {
    ok: false,
    fatal: null,
    channel: null,
    errors: [],
    warnings: [],
    blocked: [],
    contacted: [],
    page: { title: null, text: '', canvases: [], url: null },
    screenshots: {},
    changed: null,
    load_ms: null,
  };

  let browser = null;
  let launchError = '';
  for (const channel of config.channels) {
    try {
      browser = await chromium.launch(channel ? { channel, headless: true } : { headless: true });
      report.channel = channel || 'bundled-chromium';
      break;
    } catch (err) {
      launchError += `\n  ${channel || 'bundled'}: ${String(err.message).split('\n')[0]}`;
    }
  }
  if (!browser) {
    report.fatal = '无法启动浏览器（已依次尝试 ' + config.channels.join(' / ') + '）：' + launchError;
    finish(report);
    return;
  }

  try {
    const context = await browser.newContext({ viewport: config.viewport });
    const page = await context.newPage();

    page.on('pageerror', (err) => {
      report.errors.push('pageerror: ' + (err && err.message ? err.message : String(err)));
    });
    page.on('console', (msg) => {
      const type = msg.type();
      const text = msg.text();
      if (type === 'error') report.errors.push('console.error: ' + text);
      else if (type === 'warning') report.warnings.push(text);
    });

    // 网络闸门：本地地址与白名单之外的一律 abort 并记录
    await page.route('**/*', (route) => {
      const url = route.request().url();
      if (hostAllowed(url, config.allowed_hosts)) {
        report.contacted.push(url);
        return route.continue();
      }
      report.blocked.push({ url, reason: 'not in allowlist' });
      return route.abort();
    });

    const started = Date.now();
    await page.goto(config.url, { waitUntil: 'load', timeout: config.goto_timeout_ms });
    await page.waitForTimeout(config.wait_ms);
    report.load_ms = Date.now() - started;

    report.page = await page.evaluate(() => ({
      title: document.title,
      url: location.href,
      text: (document.body ? document.body.innerText : '').slice(0, 1200),
      canvases: [...document.querySelectorAll('canvas')].map((c) => ({ width: c.width, height: c.height })),
      elements: document.querySelectorAll('*').length,
    }));

    const before = await page.screenshot();
    if (config.screenshot_full) {
      fs.writeFileSync(config.screenshot_full, before);
      report.screenshots.full = config.screenshot_full;
    }
    if (config.screenshot_small) {
      fs.writeFileSync(config.screenshot_small, await page.screenshot({ type: 'jpeg', quality: config.jpeg_quality }));
      report.screenshots.small = config.screenshot_small;
    }

    // 交互：拖动 / 滚轮 / 点击；随后用像素差分回答「画面真的变了吗」
    for (const action of config.actions || []) {
      if (action.type === 'drag') {
        await page.mouse.move(action.from[0], action.from[1]);
        await page.mouse.down();
        await page.mouse.move(action.to[0], action.to[1], { steps: action.steps || 12 });
        await page.mouse.up();
      } else if (action.type === 'wheel') {
        await page.mouse.move(action.at ? action.at[0] : config.viewport.width / 2,
                              action.at ? action.at[1] : config.viewport.height / 2);
        await page.mouse.wheel(action.dx || 0, action.dy || 0);
      } else if (action.type === 'click') {
        await page.mouse.click(action.at[0], action.at[1]);
      } else if (action.type === 'wait') {
        await page.waitForTimeout(action.ms || 300);
      }
      await page.waitForTimeout(action.settle_ms || 400);
    }
    if ((config.actions || []).length) {
      const after = await page.screenshot();
      report.changed = Buffer.compare(before, after) !== 0;
      if (config.screenshot_after) {
        fs.writeFileSync(config.screenshot_after, after);
        report.screenshots.after = config.screenshot_after;
      }
    }

    report.ok = report.errors.length === 0;
    await context.close();
  } catch (err) {
    report.fatal = String((err && err.message) || err);
  } finally {
    await browser.close();
  }
  finish(report);
})().catch((err) => {
  finish({ ok: false, fatal: String((err && err.message) || err) });
});
