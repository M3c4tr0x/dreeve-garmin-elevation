"""Conteneur dreeve-garmin-elevation : boucle entre le connecteur Garmin et Dreeve.

    garmin-connector -> INBOX_DIR/<activityId>.fit
    fixer            -> telecharge le TCX (altitude corrigee) de la meme activite,
                        corrige le .fit, le depose dans WATCH_DIR
    Dreeve           -> importe WATCH_DIR

Tourne dans l'image du connecteur (meme version de garminconnect, meme format de
jetons). Ne se connecte jamais avec un mot de passe : il reprend la session creee
par `docker compose run --rm garmin-connector login`, a partir d'une copie des
jetons (le dossier des jetons est monte en lecture seule).

Si la correction est impossible (pas de TCX, pas d'altitude, Garmin n'a pas
corrige, activite d'interieur...), le fichier d'origine passe tel quel : aucune
activite n'est perdue.
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fit_alt_fix  # noqa: E402

INBOX = Path(os.environ.get('INBOX_DIR', '/inbox'))
WATCH = Path(os.environ.get('WATCH_DIR', '/watch'))
STATE = Path(os.environ.get('STATE_DIR', '/state'))
TOKENS = Path(os.environ.get('GARMINTOKENS', '/tokens'))
INTERVAL = int(os.environ.get('FIX_INTERVAL', '300'))        # s entre deux passages
MIN_AGE = int(os.environ.get('FIX_MIN_AGE', '300'))          # laisse Garmin finir sa correction
MAX_ATTEMPTS = int(os.environ.get('FIX_MAX_ATTEMPTS', '6'))  # puis le .fit passe tel quel
THRESHOLD = float(os.environ.get('FIX_THRESHOLD_M', '3'))
AUTH_BACKOFF = int(os.environ.get('FIX_AUTH_BACKOFF', '3600'))
# Pendant une pause imposee par Garmin, les passages ne comptent pas comme des essais.
# Filet de securite : au-dela de cet age, le .fit passe tel quel quoi qu'il arrive.
GIVE_UP_AGE = int(os.environ.get('FIX_GIVE_UP_AGE', str(12 * 3600)))

logging.basicConfig(level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('dreeve-garmin-elevation')
STATE_FILE = STATE / 'attempts.json'


class RetryLater(Exception):
    pass


class Paused(RetryLater):
    """Garmin en pause (refus, limitation) : aucun appel fait, l'essai ne compte pas."""


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(st):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(st))
    tmp.replace(STATE_FILE)


def deliver(name, data):
    """Ecriture atomique : Dreeve ne voit jamais un fichier a moitie ecrit."""
    dst = WATCH / name
    tmp = WATCH / f'.{name}.part'
    with tmp.open('wb') as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.chmod(0o644)
    tmp.replace(dst)


class Garmin:
    """Session reprise depuis une copie des jetons, jamais par mot de passe."""

    def __init__(self):
        self.api = None
        self.blocked_until = 0

    def reset(self):
        """Oublie la session : la prochaine sera reprise des jetons actuels du connecteur,
        qui les renouvelle de son cote (une session gardee des jours finit refusee)."""
        self.api = None

    def _session(self):
        if self.api is not None:
            return self.api
        if time.time() < self.blocked_until:
            raise Paused('session Garmin en pause, nouvel essai plus tard')
        from garminconnect import Garmin as G
        copy = Path(tempfile.mkdtemp(prefix='tokens-'))
        if TOKENS.is_dir():
            shutil.copytree(TOKENS, copy, dirs_exist_ok=True)
            store = copy
        elif TOKENS.is_file():
            store = copy / TOKENS.name
            shutil.copy2(TOKENS, store)
        else:
            self.blocked_until = time.time() + AUTH_BACKOFF
            raise RetryLater(f'aucun jeton dans {TOKENS} : lancer le login du connecteur')
        try:
            api = G()  # sans identifiants : aucune connexion par mot de passe possible
            api.login(str(store))
        except Exception as exc:  # noqa: BLE001
            self.blocked_until = time.time() + AUTH_BACKOFF
            raise RetryLater(f'reprise de session Garmin impossible : {exc}') from exc
        self.api = api
        log.info('Session Garmin reprise depuis les jetons du connecteur')
        return api

    def tcx(self, activity_id):
        from garminconnect import (Garmin as G, GarminConnectAuthenticationError,
                                   GarminConnectTooManyRequestsError)
        for attempt in (1, 2):
            api = self._session()
            try:
                data = api.download_activity(activity_id, dl_fmt=G.ActivityDownloadFormat.TCX)
                break
            except GarminConnectAuthenticationError as exc:
                self.api = None
                if attempt == 1:
                    # Session perimee : une seule reprise immediate depuis les jetons du connecteur.
                    log.info('Session Garmin refusee, reprise depuis les jetons actuels du connecteur')
                    continue
                self.blocked_until = time.time() + AUTH_BACKOFF
                raise Paused(f'Garmin refuse ({type(exc).__name__}) : pause {AUTH_BACKOFF} s') from exc
            except GarminConnectTooManyRequestsError as exc:
                self.api = None
                self.blocked_until = time.time() + AUTH_BACKOFF
                raise Paused(f'Garmin refuse ({type(exc).__name__}) : pause {AUTH_BACKOFF} s') from exc
            except Exception as exc:  # noqa: BLE001
                raise RetryLater(f'telechargement du TCX {activity_id} : {exc}') from exc
        return data.decode('utf-8', 'replace') if isinstance(data, bytes) else data

    def totals(self, activity_id):
        """(D+, D-) corriges affiches par Garmin Connect, ou None si indisponible."""
        try:
            a = self._session().get_activity(activity_id)
        except Exception as exc:  # noqa: BLE001 - le recalcul sur le TCX prend le relais
            log.warning('resume Garmin %s indisponible (%s) : D+ recalcule sur le TCX', activity_id, exc)
            return None
        s = (a or {}).get('summaryDTO') or a or {}
        up = s.get('elevationGain')
        if up is None:
            return None
        return up, s.get('elevationLoss')


def process(path, get_tcx, st, get_totals=None):
    """Traite un fichier de l'inbox. Renvoie True s'il a ete livre."""
    name = path.name
    if path.suffix.lower() != '.fit':
        deliver(name, path.read_bytes())
        log.info('%s : pas un .fit, transmis tel quel', name)
        return True
    activity_id = path.stem
    raw = path.read_bytes()
    try:
        if not activity_id.isdigit():
            raise fit_alt_fix.NotApplicable('nom de fichier sans identifiant Garmin')
        tcx = get_tcx(activity_id)
        garmin_totals = get_totals(activity_id) if get_totals else None
        fixed, rep = fit_alt_fix.fix(raw, tcx, THRESHOLD, session_totals=garmin_totals)
        rep['source'] = 'Garmin' if garmin_totals else 'TCX'
    except fit_alt_fix.NotApplicable as exc:
        deliver(name, raw)
        log.info('%s : %s, transmis tel quel', name, exc)
        return True
    except RetryLater as exc:
        age = time.time() - path.stat().st_mtime
        n = st.get(name, 0) + (0 if isinstance(exc, Paused) else 1)
        st[name] = n
        if n >= MAX_ATTEMPTS or age >= GIVE_UP_AGE:
            deliver(name, raw)
            log.warning('%s : %s ; %d essais en %d min, transmis SANS correction',
                        name, exc, n, age // 60)
            return True
        if isinstance(exc, Paused):
            log.info('%s : %s (essai non compte, %d/%d)', name, exc, n, MAX_ATTEMPTS)
        else:
            log.warning('%s : %s (essai %d/%d)', name, exc, n, MAX_ATTEMPTS)
        return False
    except Exception:  # noqa: BLE001 - un fichier inattendu ne doit pas bloquer les autres
        deliver(name, raw)
        log.exception('%s : erreur inattendue, transmis tel quel', name)
        return True
    deliver(name, fixed)
    old, new = rep['session'] or (None, None)
    log.info('%s : altitude corrigee (%d points, %d tours), D+ %s -> %s m (%s)',
             name, rep['points'], rep['laps'], old, new, rep['source'])
    return True


def cycle(garmin):
    st = load_state()
    now = time.time()
    if hasattr(garmin, 'reset') and any(
            p.is_file() and not p.name.startswith('.') for p in INBOX.iterdir()):
        garmin.reset()  # du travail : session neuve, reprise des jetons du moment
    for path in sorted(INBOX.iterdir()):
        if not path.is_file() or path.name.startswith('.'):
            continue  # .part du connecteur, fichiers caches
        if now - path.stat().st_mtime < MIN_AGE:
            continue
        if process(path, garmin.tcx, st, getattr(garmin, 'totals', None)):
            path.unlink()
            st.pop(path.name, None)
    save_state(st)


def main():
    WATCH.mkdir(parents=True, exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)
    for leftover in WATCH.glob('.*.part'):
        leftover.unlink(missing_ok=True)
    log.info('Demarrage : %s -> %s, passage toutes les %d s', INBOX, WATCH, INTERVAL)
    garmin = Garmin()
    while True:
        try:
            cycle(garmin)
        except Exception:  # noqa: BLE001
            log.exception('Passage en echec')
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
