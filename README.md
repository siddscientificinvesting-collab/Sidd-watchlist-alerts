# Watchlist Alerts: NSE + BSE filings on Telegram

This tool checks NSE and BSE corporate announcements for the stocks in `watchlist.csv`, and:

- sends each new filing to **Telegram** with the PDF attached (Telegram keeps the PDFs permanently),
- sends a **daily summary** on Telegram at 22:00 IST,
- keeps a log of every filing in `logs/announcements_<month>.csv`,
- lets you download all alerted PDFs to your PC with one command (`sync-pdfs`).

It runs free on **GitHub Actions**, so your PC does not need to be on.

| When | How often |
|---|---|
| Mon–Fri 08:30–16:25 IST (market hours) | every 5 min |
| Mon–Fri other hours | every 30 min |
| Sat–Sun | every 2 hours |
| Daily summary | 22:00 IST |

GitHub can start scheduled runs 5–15 minutes late when it is busy. That is normal.

When the same filing is posted on both NSE and BSE, you get one alert marked "also filed on BSE/NSE".

---

## 1. Edit your watchlist

Open `watchlist.csv` in Excel or Notepad. Put one stock per row:

```
watchlist,name,nse_symbol,bse_code,isin
Main,Engineers India,ENGINERSIN,532178,INE510A01028
```

- `watchlist`: the name of the list. Easier: use the web app (section 3b).

- `nse_symbol`: the symbol shown on nseindia.com. Leave it blank if the stock is BSE-only.
- `bse_code`: the 6-digit code shown on bseindia.com. Leave it blank if the stock is NSE-only.
- `isin`: optional, but it makes NSE matching more reliable.

Each stock costs one BSE request per run. Up to about 100 stocks is fine.

## 2. Create a Telegram bot (5 minutes)

1. In Telegram, search for **@BotFather** (blue tick), send `/newbot` and follow the steps.
   It gives you a **bot token** that looks like `123456789:AAE...`. Keep it secret.
2. Open your new bot and press **Start**, or send it any message, for example `hi`.
3. On your PC, open PowerShell in this folder and run (put your token in place of `<token>`):
   ```
   $env:TELEGRAM_BOT_TOKEN="<token>"; python watchlist_alerts.py telegram-chat-id
   ```
   This prints your **chat id**, a number like `987654321`.
4. Optional test: run
   ```
   $env:TELEGRAM_CHAT_ID="<chat id>"; python watchlist_alerts.py test-telegram
   ```
   You should get "✅ Watchlist alerts are connected".

## 3. Put it on GitHub

1. Sign in at github.com. Create a free account if you don't have one.
2. Click **New repository**, name it `watchlist-alerts` and choose **Public**.
   Public repositories get unlimited free Actions minutes. Your secrets stay hidden, but the watchlist and the filings log are visible to anyone.
   A private repository gets 2,000 free minutes a month and this schedule needs about 2,900. If you choose private, change `*/5` to `*/10` in `.github/workflows/watchlist-check.yml`.
   Do not tick "Add a README". Leave the repository empty.
3. Upload this folder. In PowerShell in this folder, run:
   ```
   git init -b main
   git add .
   git commit -m "Watchlist alerts"
   git remote add origin https://github.com/<your-username>/watchlist-alerts.git
   git push -u origin main
   ```
   The first push opens a GitHub sign-in window.
4. On the repository page, go to **Settings → Secrets and variables → Actions → New repository secret** and add two secrets:

   | Name | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | the bot token from step 2 |
   | `TELEGRAM_CHAT_ID` | the chat id from step 2 |

5. Go to the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
6. Click **Watchlist check → Run workflow** to test it now. Within about a minute you should get Telegram alerts for filings from the last day. After that, you get only new filings, automatically.
7. Optionally, run **Daily summary → Run workflow** once to see the summary message.

## 3b. The watchlist web app (pick companies by name, several named lists)

The `docs/` folder holds a web page for managing your watchlists:
search 5,000+ NSE/BSE companies by name, symbol, BSE code or ISIN, filter by industry,
create named lists ("Main", "Results season", …), and save straight to `watchlist.csv` on GitHub.
Each Telegram alert shows the list(s) the stock is in. It also has a **Recent alerts** tab and a **Run check now** button.

1. Upload the `docs` folder (`docs/index.html` and `docs/companies.json`) to the repository.
2. Turn on GitHub Pages: **Settings → Pages → Build and deployment → Deploy from a branch → `main` / `/docs` → Save**.
   After a minute the page is at `https://<your-username>.github.io/<repository-name>/`. It works on your phone too.
3. Create a token so the page can save: github.com → your photo → **Settings → Developer settings →
   Personal access tokens → Fine-grained tokens → Generate new token**.
   - Repository access: **Only select repositories** → this repository.
   - Permissions: **Contents: Read and write**, **Actions: Read and write**.
4. Open the page → **Settings** → paste the token → **Save**. The token stays in that browser only.

`watchlist.csv` now has a `watchlist` column (the list name). A stock in two lists has two rows.
Old files without the column still work.

**Alert filters per list.** Under the list name, **Alerts for this list → Change** lets you choose:
- **All announcements** (optionally skipping routine filings such as newspaper ads and trading-window notices), or
- **Only selected types**: orders & contracts, results, board outcomes, earnings calls, dividends/bonus/split/buyback,
  acquisitions/mergers, capacity/expansion, fund raising, credit ratings, management changes, litigation/tax,
  press releases, promoter/insider deals; plus your own words (e.g. `defence, railway`).

These settings are saved in `lists.json`. A stock in several lists alerts if any of its lists wants the filing,
and the alert names only those lists. The types are defined in `docs/alert_types.json`.

To refresh the company list (new listings), run on your PC and upload `docs/companies.json` again:
```
python build_companies.py
```
It merges NSE's and BSE's official lists with your `Industry Mapping.csv` (for industries).

## 4. Day-to-day use

- **Add or remove stocks**: edit `watchlist.csv` on github.com (pencil icon → **Commit changes**). The next run uses the new list.
- **Check that it's running**: open the **Actions** tab. Green ticks mean successful runs, and a red cross means a failed one (click it to see why).
- **Get the PDFs on your PC**: every PDF is in your Telegram chat. To download all of them to your H: drive, run:
  ```
  git pull
  python watchlist_alerts.py sync-pdfs --to "H:\AWork\Watchlist PDFs"
  ```
  Files that are already downloaded are skipped, so this is safe to run every day.
- **Pause everything**: go to Actions → Watchlist check → `...` → **Disable workflow**.

---

## If something goes wrong

- **"NSE has failed 3 checks in a row"** on Telegram: NSE sometimes blocks cloud servers. BSE alerts keep working, and nearly all NSE companies also file on BSE. If NSE stays blocked, you can run the check from your PC instead (below).
- **No alerts at all**: open the Actions tab and click the latest run.
  - A red cross with a Telegram error usually means a wrong token or chat id. Fix the secret and run it again.
- **Runs stopped**: GitHub pauses schedules in repositories with no activity for 60 days. The daily summary commit prevents this. If it still happens, open Actions and click **Enable workflow**.
- **Run it on your PC instead** (same alerts, no GitHub needed):
  ```
  pip install -r requirements.txt
  python watchlist_alerts.py check --dry-run    # preview: sends and saves nothing
  python watchlist_alerts.py check              # real run
  ```
  Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as environment variables first, and schedule it with Windows Task Scheduler.

## Commands

| Command | What it does |
|---|---|
| `check` | look for new filings and send alerts (`--dry-run` previews, `--days N` looks back N days) |
| `summary` | send the daily summary |
| `sync-pdfs --to <folder>` | download every alerted PDF |
| `telegram-chat-id` | show your chat id |
| `test-telegram` | send a test message |

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SEND_PDF_TO_TELEGRAM` | `1` | `0` = send the link only, no attachment |
| `BSE_ENABLED` | `1` | `0` = skip BSE (set in the workflow, because BSE blocks GitHub's servers) |
| `SAVE_PDFS` | `1` on PC, `0` in the cloud | `1` = also save each PDF into `pdfs/` during the check |
| `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` | not set | set all three to get the daily summary by email instead of Telegram |
