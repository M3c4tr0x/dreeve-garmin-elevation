"""Remplace l'altitude GPS d'un .fit Garmin par l'altitude corrigee (relief) du
.tcx Garmin Connect de la meme sortie, puis recalcule le D+/D- de la session et
des tours, et le CRC. Tout le reste du .fit (cadence, sport, calories, FC...) est
conserve octet pour octet.

En ligne de commande :
    python fit_alt_fix.py sortie.fit sortie.tcx sortie_corrigee.fit [seuil_m]
"""
import bisect
import re
import struct
import sys
from datetime import datetime

FIT_EPOCH = 631065600  # 1989-12-31T00:00:00Z
CRC_TABLE = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
             0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]
BASE = {0: 'B', 1: 'b', 2: 'B', 3: 'h', 4: 'H', 5: 'i', 6: 'I', 8: 'f',
        9: 'd', 10: 'B', 11: 'H', 12: 'I', 13: 'B', 14: 'q', 15: 'Q', 16: 'Q'}


class NotApplicable(Exception):
    """Pas de correction possible : le .fit doit passer tel quel."""


def crc16(data, crc=0):
    for b in data:
        for nib in (b & 0xF, b >> 4):
            tmp = CRC_TABLE[crc & 0xF]
            crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ CRC_TABLE[nib]
    return crc


def _scan(d):
    if len(d) < 14 or d[8:12] != b'.FIT':
        raise NotApplicable('pas un fichier FIT')
    hs = d[0]
    end = hs + struct.unpack('<I', d[4:8])[0]
    if end + 2 > len(d):
        raise NotApplicable('fichier FIT tronque')
    p, defs, msgs, last_ts = hs, {}, [], None
    while p < end:
        h = d[p]; p += 1
        if h & 0x80:  # horodatage compresse
            lt = (h >> 5) & 3
            off = h & 0x1F
            ts = None
            if last_ts is not None:
                ts = (last_ts & ~0x1F) + off
                if off < (last_ts & 0x1F):
                    ts += 0x20
                last_ts = ts
        elif h & 0x40:  # message de definition
            lt = h & 0xF
            e = '>' if d[p + 1] else '<'
            g = struct.unpack(e + 'H', d[p + 2:p + 4])[0]
            n = d[p + 4]; p += 5
            fs = []
            for _ in range(n):
                fs.append((d[p], d[p + 1], d[p + 2] & 0x1F)); p += 3
            dev = 0
            if h & 0x20:
                nd = d[p]; p += 1
                for _ in range(nd):
                    dev += d[p + 1]; p += 3
            defs[lt] = (e, g, fs, dev)
            continue
        else:
            lt = h & 0xF
            ts = None
        if lt not in defs:
            raise NotApplicable('FIT illisible (definition manquante)')
        e, g, fs, dev = defs[lt]
        fields = {}
        for num, sz, bt in fs:
            fields[num] = (p, sz, bt); p += sz
        p += dev
        msg = {'g': g, 'e': e, 'f': fields, 'ts': ts}
        if 253 in fields:
            msg['ts'] = _val(d, msg, 253)
            last_ts = msg['ts']
        msgs.append(msg)
    return msgs, end


def _val(d, m, num):
    if num not in m['f']:
        return None
    o, sz, bt = m['f'][num]
    f = BASE.get(bt)
    if not f or struct.calcsize(f) != sz:
        return None
    return struct.unpack(m['e'] + f, d[o:o + sz])[0]


def _put(d, m, num, v):
    if num not in m['f']:
        return False
    o, sz, bt = m['f'][num]
    f = BASE.get(bt)
    if not f or struct.calcsize(f) != sz:
        return False
    d[o:o + sz] = struct.pack(m['e'] + f, v)
    return True


def _read_tcx(text):
    pts = []
    for tp in re.findall(r'<Trackpoint>(.*?)</Trackpoint>', text, re.S):
        tm = re.search(r'<Time>([^<]+)</Time>', tp)
        al = re.search(r'<AltitudeMeters>([^<]+)</AltitudeMeters>', tp)
        if tm and al:
            dt = datetime.fromisoformat(tm.group(1).replace('Z', '+00:00'))
            pts.append((dt.timestamp() - FIT_EPOCH, float(al.group(1))))
    pts.sort()
    return [p[0] for p in pts], [p[1] for p in pts]


def _interp(ts, xs, ys):
    i = bisect.bisect_left(xs, ts)
    if i <= 0:
        return ys[0]
    if i >= len(xs):
        return ys[-1]
    x0, x1 = xs[i - 1], xs[i]
    if x1 == x0:
        return ys[i]
    return ys[i - 1] + (ys[i] - ys[i - 1]) * (ts - x0) / (x1 - x0)


def fix(fit_bytes, tcx_text, threshold=3.0, min_change=0.5, session_totals=None):
    """Renvoie (fit_corrige, rapport). Leve NotApplicable si rien a corriger.

    session_totals : (D+, D-) a ecrire dans la session, par exemple les valeurs
    corrigees affichees par Garmin Connect. Sans lui, la session recoit le D+/D-
    recalcule sur l'altitude du TCX. Les tours recoivent toujours le recalcul.
    """
    d = bytearray(fit_bytes)
    hs = d[0]
    msgs, end = _scan(d)
    xs, ys = _read_tcx(tcx_text)
    if len(xs) < 10:
        raise NotApplicable('pas d\'altitude dans le TCX')

    series, diffs = [], []
    for m in msgs:
        if m['g'] != 20 or m['ts'] is None:
            continue
        old78, old2 = _val(d, m, 78), _val(d, m, 2)
        if old78 not in (None, 0xFFFFFFFF):
            old = old78 / 5 - 500
        elif old2 not in (None, 0xFFFF):
            old = old2 / 5 - 500
        else:
            continue
        a = _interp(m['ts'], xs, ys)
        diffs.append(abs(a - old))
        raw = int(round((a + 500) * 5))
        if old78 not in (None, 0xFFFFFFFF):
            _put(d, m, 78, raw)
        if old2 not in (None, 0xFFFF):
            _put(d, m, 2, min(raw, 0xFFFE))
        series.append((m['ts'], a))

    if len(series) < 10:
        raise NotApplicable('pas d\'altitude dans le FIT')
    if sum(diffs) / len(diffs) < min_change:
        raise NotApplicable('TCX identique au FIT : Garmin n\'a pas corrige l\'altitude')

    # D+/D- par hysteresis, attribue au tour ou le seuil est franchi
    steps, ref = [], series[0][1]
    for ts, a in series:
        if abs(a - ref) >= threshold:
            steps.append((ts, a - ref)); ref = a

    def totals(t0, t1):
        up = sum(v for t, v in steps if t0 <= t <= t1 and v > 0)
        dn = -sum(v for t, v in steps if t0 <= t <= t1 and v < 0)
        return int(round(up)), int(round(dn))

    report = {'points': len(series), 'session': None, 'laps': 0}
    for m in msgs:
        if m['g'] not in (18, 19):
            continue
        t0, t1 = _val(d, m, 2), m['ts']
        if t0 is None or t1 is None:
            continue
        fa, fd = (22, 23) if m['g'] == 18 else (21, 22)
        up, dn = totals(t0, t1)
        if m['g'] == 18 and session_totals:
            up = int(round(session_totals[0]))
            if session_totals[1] is not None:
                dn = int(round(session_totals[1]))
        old = _val(d, m, fa)
        _put(d, m, fa, up); _put(d, m, fd, dn)
        if m['g'] == 18:
            report['session'] = (old, up)
        else:
            report['laps'] += 1

    if hs == 14 and struct.unpack('<H', d[12:14])[0] != 0:
        d[12:14] = struct.pack('<H', crc16(d[:12]))
    d[end:end + 2] = struct.pack('<H', crc16(d[:end]))
    return bytes(d), report


if __name__ == '__main__':
    src, tcx, dst = sys.argv[1:4]
    thr = float(sys.argv[4]) if len(sys.argv) > 4 else 3.0
    out, rep = fix(open(src, 'rb').read(), open(tcx, encoding='utf8').read(), thr)
    open(dst, 'wb').write(out)
    print(rep)
