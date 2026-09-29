# Market Stress Dashboard

A personal web page that checks 10 market stress indicators twice each weekday and shows an overall score, the worst indicators first, and how stress has trended over time.

## Setup (about 10 minutes)

1. **FRED API key:** register free at https://fred.stlouisfed.org/docs/api/api_key.html
2. **GitHub repo:** create a new repo and upload these files, keeping the `.github/workflows` folder.
3. **Secret:** in the repo go to Settings > Secrets and variables > Actions, and add `FRED_API_KEY`.
4. **Pages:** go to Settings > Pages and set Source to "GitHub Actions".
5. **First run:** Actions tab > Market stress dashboard > Run workflow. When it finishes, the page link appears in the run summary and under Settings > Pages. Bookmark it on your phone.

After that it updates itself at 06:00 UTC (before London) and 21:30 UTC (after the US close) on weekdays.

**Privacy:** on a free GitHub account, Pages requires a public repo, so the page and the log are public but unlisted and not indexed by search engines. Everything shown is public market data. For a fully private page, use a paid GitHub plan with a private repo.

## Check it on your own machine instead

```
pip install -r requirements.txt
FRED_API_KEY=your_key python crash_monitor.py
```
Then open `site/index.html` in your browser.

## Scoring

Each indicator scores 0 (green), 1 (amber) or 2 (red), for a total out of 20.

- **Red overall:** score 8+ or any cluster alert fires
- **Amber overall:** score 4+ or any single indicator red
- **Cluster alerts:** debt confidence (yields up, dollar down, weak auction) and liquidity stress (volatility, credit and funding all amber or worse)

Thresholds live in the `check_` functions. After a month or two of history in `crash_monitor_log.csv`, review and tune them.

## Notes

- FRED data reflects the previous close; Yahoo data is close to live at run time.
- Funding indicators often spike at month and quarter end; the page flags this.
- Auction comparisons use nominal notes and bonds only (FRNs and TIPS excluded).
- Optional: set a `SLACK_WEBHOOK_URL` environment variable to also get a short Slack summary.
