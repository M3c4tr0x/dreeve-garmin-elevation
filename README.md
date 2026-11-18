# dreeve-garmin-elevation

🇬🇧 English · [🇫🇷 Français](README.fr.md)

Automatic import of **Garmin Connect → [Dreeve](https://dreeve.app)** activities, with the **elevation gain corrected by Garmin** instead of the watch's raw GPS value. Nothing else changes: cadence, sport type (trail, road…), heart rate, calories and laps are kept.

## The problem

Garmin watches without a barometer (Forerunner 245, 55, 165…) measure altitude with GPS. It is noisy, and the elevation gain written in the `.fit` is inflated. Dreeve takes that number as is. Real examples: 234 m instead of 158, 323 m instead of 283.

Garmin Connect corrects the altitude with a terrain map ("Elevation Corrections"), but only on its own side:

- importing the `.fit` into Dreeve gives the raw elevation gain;
- importing the corrected TCX loses cadence, the "trail" sport type and other data;
- Dreeve does not correct altitude itself ([#2356](https://github.com/dreeveapp/dreeve/issues/2356), declined).

## How it works

```
Garmin Connect ──► garmin-connector ──► garmin/inbox/<id>.fit
                                              │
                   dreeve-garmin-elevation ◄──┘  downloads the TCX of the same activity,
                         │                       replaces the altitude of the .fit records,
                         ▼                       writes the gain/loss shown by Garmin Connect,
                   Dreeve watch/ ──► import      recomputes lap gains and the CRC
```

- **`garmin-connector`**: the [official Dreeve connector](https://github.com/dreeveapp/dreeve-garmin-connector). It drops the `.fit` files into an **inbox** rather than Dreeve's `watch` folder.
- **`dreeve-garmin-elevation`** (`app/fixer.py`): checks the inbox every 5 minutes, corrects each `.fit` (`app/fit_alt_fix.py`) and drops it into the `watch` folder. It runs in **the same image as the connector**: there is nothing to build.
- **Dreeve** imports the `watch` folder every 5 minutes, as usual.

**No activity is ever lost.** If the correction is impossible (no altitude, Garmin corrected nothing, file is not a `.fit`, error), the original `.fit` goes to Dreeve as is and the log says so.

## Requirements

- A **Dreeve** instance running under Docker, with its `watch` folder.
- **Docker Compose v2** (`docker compose`).
- A **Garmin Connect** account and a watch **without a barometer**. With a barometer the altitude is already good: this gateway adds nothing over the connector alone.
- On Garmin Connect, the **"Elevation Corrections"** option enabled (on the web: open an activity, then the gear icon). Without it, the TCX is identical to the `.fit` and activities go through uncorrected.

## Installation

### 1. Find Dreeve's `watch` folder

It is the host folder mounted on `/var/www/watch` in the Dreeve container:
```bash
docker inspect <dreeve-container> -f '{{range .Mounts}}{{if eq .Destination "/var/www/watch"}}{{.Source}}{{end}}{{end}}'
```
Replace `<dreeve-container>` with the name of the Dreeve container (`docker ps` shows it).

### 2. Get this repository and configure it

```bash
git clone https://github.com/M3c4tr0x/dreeve-garmin-elevation.git && cd dreeve-garmin-elevation
cp .env.example .env
```
In `.env`, fill in:
- `GARMIN_EMAIL`: the Garmin account email;
- `DREEVE_WATCH_DIR`: the path found in step 1;
- `SINCE`: leave `now` if Dreeve already holds your history (see "Older activities").

### 3. Log in to Garmin, once

The password is read without echo. It is written neither to `.env`, nor to the compose file, nor to the shell history. The MFA code is asked for if the account has one.
```bash
read -rsp 'Garmin password: ' GP; echo
GARMIN_PASSWORD="$GP" docker compose run --rm -e GARMIN_PASSWORD garmin-connector login
unset GP
```
The session is saved in `garmin/tokens/`. Both containers then reuse it without a password.

> ⚠️ **Do not rerun the login in a loop.** Garmin quickly limits logins (`429 IP rate limited`). On a 429, let the command run to the end: the library tries other login methods and often gets through. If it still fails, wait several hours before trying again.

### 4. Start

```bash
docker compose up -d
```

### 5. Check on your next activity

After syncing the watch, allow up to about 1 h 15: the connector runs every hour, `dreeve-garmin-elevation` waits 5 minutes, then Dreeve imports within 5 minutes.
```bash
docker compose logs -f garmin-connector dreeve-garmin-elevation
```
The expected line (log messages are in French):
```
25091328234.fit : altitude corrigee (4638 points, 12 tours), D+ 323 -> 283 m (Garmin)
```
The first number is the watch's raw elevation gain. The second, Garmin Connect's, is the one that reaches Dreeve.

## Reading the `dreeve-garmin-elevation` log

| Line | Meaning |
|---|---|
| `altitude corrigee … D+ x -> y m (Garmin)` | All good. |
| `… (TCX)` instead of `(Garmin)` | Garmin's summary was not available: the gain was recomputed from the TCX. It is correct, but may differ by a few metres from the app. |
| `pas d'altitude dans le TCX, transmis tel quel` | Activity without GPS (strength, indoor trainer…). This is normal. |
| `TCX identique au FIT … transmis tel quel` | Garmin did not correct the altitude. Check the "Elevation Corrections" option. |
| `session Garmin en pause … (essai non compte)` | Garmin refused or throttled a call. The file waits for the pause to end (1 h). |
| `transmis SANS correction` | Failed after several attempts, or after 12 h. The activity is in Dreeve with the raw gain: see "Reimporting an activity". |

## Operations

- **Reimporting an activity** (one that went through uncorrected, for example):
  1. Delete it in the Dreeve admin, otherwise it ignores the duplicate.
  2. Get its original `.fit` from Garmin Connect (web): open the activity, then the gear icon, then "Export Original", and unzip.
  3. Rename it `<Garmin id>.fit`, using the number in the activity URL, then drop it into `garmin/inbox/`. It is processed within 5 to 10 minutes.
- **Shoes, name, description and photos**: none of these are filled in automatically. Add them in Dreeve after each activity.
- **Garmin session lost**: the connector log reports an authentication error. Redo step 3, **once**, then run `docker compose restart`.
- **Updating**: `docker compose pull && docker compose up -d`. Both containers use the same image. After an update, check in the log that an activity is processed correctly: a new version of the `garminconnect` library may change its API.
- **File permissions**: the containers run as root, like the Dreeve image. If your Dreeve runs as another user, check that it can read and delete the files dropped into `watch`.

## Older activities

With `SINCE=now`, only activities recorded after the first start are fetched. To correct activities already in Dreeve, two options:
- **reimport** them one by one (see above);
- delete them from Dreeve, set a date in `SINCE` **before the first start**, then start. `SINCE` is read only once: after that, you have to empty `garmin/state/`. The connector spreads the downloads out so it does not get blocked by Garmin.

Warning: deleting an activity from Dreeve also deletes what was added by hand (shoes, description, photos).

## Settings (`docker-compose.yml`, `dreeve-garmin-elevation` service)

| Variable | Default | Purpose |
|---|---|---|
| `FIX_INTERVAL` | 300 | Seconds between two runs. |
| `FIX_MIN_AGE` | 300 | Minimum age of a `.fit` before processing. |
| `FIX_MAX_ATTEMPTS` | 6 | Real download failures before forwarding without correction. Pauses imposed by Garmin do not count. |
| `FIX_GIVE_UP_AGE` | 43200 | Beyond this age in seconds (12 h), the `.fit` is forwarded as is no matter what. |
| `FIX_AUTH_BACKOFF` | 3600 | Length of the pause after Garmin refuses a call. |
| `FIX_THRESHOLD_M` | 3 | Threshold in metres for recomputing lap gains. |

For the connector's settings (activity types, pace, etc.), see the [Dreeve documentation](https://docs.dreeve.app).

## Security

- `dreeve-garmin-elevation` **never** logs in with a password. It reuses the connector's session from a copy of its tokens, mounted read-only. On every run that has a file to process, it starts from the current tokens, which the connector renews on its side.
- Atomic writes into `watch` (`.part` file, then rename): Dreeve never reads a half-written file.
- `garmin/tokens/` gives access to the Garmin account. Do not share it, and do not commit it: it is in `.gitignore`.

## Rolling back

```bash
docker compose down
```
Dreeve is not modified: dropping files into `watch` by hand keeps working. Activities already imported keep their corrected elevation gain.

## Using it without Docker

`app/fit_alt_fix.py` also corrects a file by hand (Python 3, no dependencies):
```bash
python app/fit_alt_fix.py activity.fit activity.tcx activity_fixed.fit
```
The TCX is exported from Garmin Connect (activity, then the gear icon, then "Export to TCX"). In this mode, the elevation gain is recomputed from the TCX, without the exact figure from the Garmin app.

## Development

This project was written with the help of [Claude Code](https://claude.com/claude-code) (Anthropic's AI assistant), then reviewed and tested on my own activities.

## License

[GPL-3.0](LICENSE), like Dreeve's [Garmin connector](https://github.com/dreeveapp/dreeve-garmin-connector).
