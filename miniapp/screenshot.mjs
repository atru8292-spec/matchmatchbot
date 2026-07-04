// Скриншоты интерфейса через системный Chrome (без качки chromium).
// Использование: node screenshot.mjs <url> <out.png> [dark] [width] [height]
import { chromium } from "playwright-core";

const url = process.argv[2] || "http://127.0.0.1:5173/app/";
const out = process.argv[3] || "shots/preview.png";
const dark = process.argv[4] === "dark";
const width = Number(process.argv[5] || 390); // Telegram mobile-ширина
const height = Number(process.argv[6] || 844);

const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage({
  viewport: { width, height },
  deviceScaleFactor: 2,
  colorScheme: dark ? "dark" : "light",
});
await page.goto(url, { waitUntil: "networkidle" });
// синхронизируем класс темы с colorScheme
if (dark) await page.evaluate(() => document.documentElement.classList.add("dark"));
// Ждём, пока React смонтируется и данные догрузятся (иначе ловим пустой кадр до
// резолва запроса). Ждём любой значимый контент внутри #root, но не падаем если пусто.
await page
  .waitForFunction(() => {
    const root = document.getElementById("root");
    return root && root.querySelector("main") && root.innerText.trim().length > 0;
  }, { timeout: 5000 })
  .catch(() => {});
await page.waitForTimeout(500); // дать шрифтам/данным дорисоваться
await page.screenshot({ path: out, fullPage: true });
await browser.close();
console.log("saved", out);
