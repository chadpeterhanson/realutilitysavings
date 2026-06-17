# DEPLOY — get a clickable link for your 10 testers

Goal: a normal `https://...` link you send to people. They click it, go through
the website, and see their current bill compared to cheaper plans. No install
for them.

You only set this up once. Pick ONE option below.

---

## Option A — Render (recommended, free, ~10 min)

Render gives you a public HTTPS link and keeps the app running.

1. Make a free account at https://render.com
2. Put this folder in a GitHub repo (or use Render's "deploy from a Zip"/manual
   option). Easiest: create a new GitHub repo, upload all these files to it.
3. In Render: **New → Web Service → connect that repo.**
4. Render reads `render.yaml` automatically. Confirm:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn -w 2 -b 0.0.0.0:$PORT server:app`
5. Click **Create Web Service.** In ~2–3 minutes you get a link like
   `https://real-utility-savings.onrender.com`
6. Send people that link with **`/site`** on the end:
   `https://real-utility-savings.onrender.com/site`

Free tier note: the app "sleeps" after inactivity, so the first click after a
quiet spell takes ~30 seconds to wake. Fine for a small test group.

---

## Option B — Railway / Fly.io (similar)

Both read the included `Procfile`. Create a project, point it at this folder/
repo, deploy. You get an HTTPS URL the same way. Send it with `/site` on the end.

---

## Option C — your own computer + tunnel (fastest, but your PC must stay on)

No account needed, but the link only works while your machine is running.

```
./run.sh
```
then in a second terminal:
```
cloudflared tunnel --url http://localhost:5001
```
(or `ngrok http 5001`). It prints a temporary HTTPS link. Add `/site`.

---

## BEFORE you send the link — make the comparison REAL

Out of the box the comparison plans are a small illustrative set. For testers to
see **real** alternative plans, switch on live data once:

```
python3 refresh_plans.py --full
```

- On Render/Railway: run this in the service's shell/console after first deploy,
  OR run it locally and commit the generated `plan_cache/plans_SA.json` into the
  repo so it ships with the app.
- Confirm it worked: open `your-link/api/health` — it should say
  `"plan_data": "live"`.

Then spot-check 3–4 of the plans against the retailers' own price fact sheets so
you trust the numbers before people rely on them.

---

## What to tell your 10 people

Send the link plus a short note, e.g.:

> Here's a tool I'm testing that compares your energy plan to others:
> [your-link]/site — click **Get Started**.
> Have your latest electricity bill handy: you'll enter your usage rate
> (c/kWh), daily supply charge, and feed-in rate if you have solar.
> It's a test build and not financial advice; your details are used only for
> the calculation and aren't stored. Tell me if the "what you pay now" figure
> looks about right, and if anything was confusing.

That last question is the important one — they're the only people who know their
real bill, so their "yes that's about right / no that's way off" is how you find
out if the engine is accurate.

---

## One responsibility that comes with real people's data

If testers upload their actual interval-data file or enter real tariffs, that's
their personal energy information. This build parses it in memory and doesn't
store it, but say so in your note (above), and don't present the savings as
advice to act on. See `TESTING_RUNBOOK.md` section 6 for the full pre-launch list.
