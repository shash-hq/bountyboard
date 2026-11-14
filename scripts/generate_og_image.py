import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_PATH = BASE_DIR / "static" / "img" / "og-card.png"

HTML_CONTENT = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      width: 1200px;
      height: 630px;
      background-color: #FFF6DC;
      color: #111111;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      padding: 60px;
      border: 12px solid #111111;
    }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .badge {
      background: #FFD60A;
      color: #111111;
      font-size: 24px;
      font-weight: 900;
      padding: 10px 24px;
      border: 4px solid #111111;
      box-shadow: 6px 6px 0 #111111;
      text-transform: uppercase;
      letter-spacing: 1px;
    }
    .tagline {
      font-size: 28px;
      font-weight: 800;
      background: #5CE1A8;
      border: 4px solid #111111;
      box-shadow: 6px 6px 0 #111111;
      padding: 10px 24px;
    }
    .main {
      margin-top: 30px;
    }
    h1 {
      font-size: 76px;
      font-weight: 900;
      line-height: 1.05;
      letter-spacing: -2px;
      margin-bottom: 24px;
      text-transform: uppercase;
    }
    .sub {
      font-size: 32px;
      font-weight: 600;
      color: #333333;
      max-width: 900px;
      line-height: 1.3;
    }
    .footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-top: 6px solid #111111;
      padding-top: 30px;
    }
    .features {
      display: flex;
      gap: 20px;
    }
    .chip {
      background: #FFFFFF;
      border: 3px solid #111111;
      box-shadow: 4px 4px 0 #111111;
      padding: 8px 18px;
      font-weight: 800;
      font-size: 20px;
    }
    .sticker {
      background: #FF5FA2;
      color: #FFFFFF;
      font-size: 28px;
      font-weight: 900;
      padding: 12px 28px;
      border: 4px solid #111111;
      box-shadow: 6px 6px 0 #111111;
      transform: rotate(-3deg);
    }
  </style>
</head>
<body>
  <div class="header">
    <div class="badge">₹ CAMPUS MICRO-TASKS</div>
    <div class="tagline">SETTLE OFFLINE (CASH / UPI)</div>
  </div>

  <div class="main">
    <h1>BOUNTY BOARD</h1>
    <div class="sub">Post small gigs, fix code, help classmates, and claim cash on campus. ₹10 to ₹10,000 bounties.</div>
  </div>

  <div class="footer">
    <div class="features">
      <div class="chip">DEBUGGING</div>
      <div class="chip">LAB REPORTS</div>
      <div class="chip">DESIGN &amp; SLIDES</div>
      <div class="chip">CAMPUS ERRANDS</div>
    </div>
    <div class="sticker">₹10 - ₹10,000</div>
  </div>
</body>
</html>
"""

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1200, "height": 630})
        await page.set_content(HTML_CONTENT)
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(OUTPUT_PATH))
        await browser.close()
        print(f"Generated social preview image at: {OUTPUT_PATH}")

if __name__ == "__main__":
    asyncio.run(main())
