# Manhwa Chapter Tracker Bot (Asura Scans)

Ye Telegram bot Asura Scans ke **public series pages** ko check karta hai. Naya chapter detect hone par Telegram par title, chapter number, poster (agar page metadata mein ho) aur chapter link bhejta hai.

## Bahut zaroori limitation

Tumhare Asura bookmarks `Only you` (private) hain. Asura ka koi documented public API nahi mila jo third-party bot ko private bookmarks padhne de. Isliye ye safe starter version tumhare private bookmarks ko automatic import **nahi** karta.

Tum har bookmarked Manhwa ka Asura **series page URL** ek baar Telegram mein `/add` se add karoge. Iske baad bot un URLs ko automatically monitor karega. Password, login cookie ya session token ki zarurat nahi hai. Apna password/cookie kisi ko mat dena.

Asura ki official announcement ke mutabik Asura account ko Discord se link karne par bookmarks sync hote hain aur bookmarked series ke new chapters par Discord notifications milte hain:
https://asurascans.com/announcement/discord-notifications

Asura site ka current domain `asurascans.com` hai. Site ka markup/API badalne par scraper ko update karna pad sakta hai.

## Telegram commands

- `/start` — bot start aur tumhara Telegram numeric ID
- `/add https://asurascans.com/comics/series-slug` — series track karna shuru
- `/list` — tracked series ki list
- `/remove 1` — `/list` mein dikhne wali ID se series remove
- `/check` — abhi manual check
- `/help` — command help

`/add` karte waqt bot current latest chapter ko baseline maanega. Isse pehle se maujood chapters ke purane alerts nahi aayenge.

## GitHub + Render deployment (Web Service)

1. ZIP extract karo.
2. Saari files ek GitHub repository ke root mein upload karo.
3. Render par **New + → Web Service → Connect a repository** chuno.
4. Build command: `pip install -r requirements.txt`
5. Start command: `gunicorn --workers 1 --threads 4 --timeout 120 app:app`
6. Environment Variables mein ye set karo:
   - `TELEGRAM_BOT_TOKEN` = BotFather se mila token
   - `ALLOWED_TELEGRAM_USER_ID` = pehli baar blank chhod sakte ho. Deploy ke baad bot par `/start` bhejo; bot tumhara numeric ID batayega. Phir Render mein wahi ID set karke redeploy karo.
   - `CHECK_INTERVAL_SECONDS` = `1200` (20 minutes)
   - `DATABASE_PATH` = `data/tracker.db`
7. Deploy complete hone ke baad Telegram bot par `/start` bhejo.
8. Apne Asura Bookmarks se series kholo, us series page ka URL copy karo aur bot ko bhejo:
   `/add https://asurascans.com/comics/your-series-slug`

Health check: `/health`

## Important Render storage note

Free web services ka local filesystem persistent nahi hota. Service restart/redeploy hone par SQLite database reset ho sakta hai, aur tracked URLs dobara add karne pad sakte hain. Persistent disk use karne wale paid plan par `DATABASE_PATH=/var/data/tracker.db` set karke disk `/var/data` par mount karo. Is project mein credentials GitHub par mat daalna.

## Safety / privacy

- Bot token sirf Render Environment Variables mein rakho.
- GitHub par `.env`, database ya account password upload mat karo.
- Ye bot Asura mein login nahi karta aur private bookmarks scrape nahi karta.
- Bot Asura ke public series pages ko limited interval (default 20 minutes) par check karta hai. Website rate limits/terms ka respect karo.
- Asura page ka HTML badalne par chapter detection fail ho sakta hai. Aise mein `/check` ka result aur Render logs dekho.

## Local run (optional)

```bash
pip install -r requirements.txt
# Set TELEGRAM_BOT_TOKEN and other environment variables
python app.py
```
