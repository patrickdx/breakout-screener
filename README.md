# NASDAQ Breakout Screener

Daily screener that flags NASDAQ common stocks at or near their 52-week high,
enriched with ApeWisdom Reddit mention buzz. Runs on a GitHub Actions cron and
publishes a static dashboard to GitHub Pages.

**Live dashboard:** https://patrickdx.github.io/breakout-screener/

Run locally: `OUTPUT_DIR=site python momentum_update.py`, then open `site/index.html`.
