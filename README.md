# Antoinette's show

A private morning news podcast. Every morning at about 5:15 (4:15 in winter) it:

1. reads new stories from the free outlets in `feeds.txt` (ABC, The Guardian, The Conversation, Pearls and Irritations, Michael West Media, RenewEconomy and Al Jazeera),
2. has Claude pick 9 to 11 stories on Australian politics, environment and climate, nature and design, and write a spoken briefing in its own words that names each outlet,
3. reads it aloud with a Google voice (about 12 minutes),
4. adds it to the podcast feed, which keeps the last two weeks, and
5. emails a large-print list of the same stories with links.

The script and audio for each run are also saved with the run (Actions → the run → Artifacts) for a week.

## One-time setup (Indigo)

### 1. Turn on the web hosting
Repository **Settings → Pages → Build and deployment → Source: GitHub Actions**.

The feed address will be `https://<your-github-name>.github.io/<this-repo-name>/feed.xml`. The repository is public, which is what makes the hosting free. It holds only the code, so there's nothing private in it. The feed is hidden from the Apple Podcasts directory, but anyone who has the address can listen.

### 2. Anthropic (writes the script)
1. Go to console.anthropic.com and sign up. This is separate from a Claude.ai subscription.
2. **Billing**: buy US$10 of credit. Leave auto-reload off, so it can never spend more than that.
3. **API keys → Create key**. Name it "Antoinette's show" and copy it.

### 3. Google Cloud (the voice)
1. Go to console.cloud.google.com and sign in with a Google account. Accept the terms and add a card when asked. Normal use stays within the free monthly allowance.
2. At the top, **Select a project → New project**. Name it "antoinettes-show" and create it.
3. Search for **Cloud Text-to-Speech API** and press **Enable**.
4. Go to **APIs & Services → Credentials → Create credentials → API key**. Copy the key.
5. Edit that key. Under **API restrictions**, choose **Restrict key** and tick only **Cloud Text-to-Speech API**. Save.
6. Go to **Billing → Budgets & alerts → Create budget**. Set it to $1 so you get an email if it ever starts charging.

### 4. Gmail app password (sends the email)
GitHub won't show you the value of an existing secret, so make a new app password: myaccount.google.com → search "App passwords" → create one called "Antoinette's show".

### 5. Add the secrets
Repository **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | the key from step 2 |
| `GOOGLE_TTS_KEY` | the key from step 3 |
| `EMAIL_FROM` | the Gmail address that sends it (yours) |
| `APP_PASSWORD` | the app password from step 4 |
| `BRIEFING_TO` | Antoinette's email address. Add yours too, separated by a comma, if you want a copy |

### 6. Choose a voice
**Actions → Voice samples → Run workflow**. A few minutes later you'll get an email with eight short samples. Pick one, then go to **Settings → Secrets and variables → Actions → Variables → New repository variable**, with the name `VOICE` and the value set to the voice's name (for example `en-AU-Chirp3-HD-Aoede`). If you skip this step, the show uses Aoede.

### 7. Test episode
**Actions → Antoinette's show (daily episode) → Run workflow**, with "Test run" ticked. The email comes only to you, and the episode goes into the feed so you can listen to it. When it sounds right, you're done: the daily runs start by themselves.

If a run fails, GitHub emails you. The email still goes out if only the voice step fails.

## Setting up Antoinette's iPhone (for whoever helps her)

You'll need the feed address from step 1 above. Indigo will send it to you.

1. Open the **Podcasts** app (purple icon).
2. Tap **Library** at the bottom, then the **⋯** button (or **Edit**) at the top right, then **Follow a Show by URL**.
3. Paste the feed address and tap **Follow**. "Antoinette's show" appears in the Library.
4. Open the show, tap **⋯** at the top right, then **Settings**. Turn on **Automatically Download** and **Notifications**.
5. Try it: say **"Hey Siri, play Antoinette's show"**. The latest episode should start. If Siri gets it wrong, try "Hey Siri, play the podcast Antoinette's show".
6. In **Mail**, find the morning email from "Antoinette's show". Tap the sender's name and choose **Add to VIP**, so it's easy to find and never goes to junk.
7. Show her that pressing the **side button** (or the lock-screen play button) pauses and resumes, and that the episode is split into chapters, one per story, so she can skip a story.

## Changing things
- **Sources:** edit `feeds.txt`.
- **Length, number of stories, speed, name:** the settings near the top of `briefing.py` (`TARGET_MINUTES`, `STORIES`, `SPEAKING_RATE`, `SHOW_NAME`).
- **Time:** the `cron` line in `.github/workflows/briefing.yml`.
- **What the writer is told:** `PICK_PROMPT` and `WRITE_PROMPT` in `briefing.py`.

## Costs
- Claude writes the script: a few US dollars a month, taken from the prepaid credit.
- Google voice (Chirp 3 HD): the first million characters a month are free. A 12-minute episode a day is about a third of that.
- GitHub hosting and Actions: free.
