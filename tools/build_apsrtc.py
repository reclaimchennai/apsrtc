#!/usr/bin/env python3
"""APSRTC open data: one combined, de-duplicated GTFS feed plus the observations behind it.

    build_apsrtc.py --web DIR --neo DIR --trips DIR --punct FILE --out REPO

Inputs (collected by reclaim.city from the public APSRTC Live Track app; no scrapers here):
  --web    the bus map's data: stops.json, services.json, segs.json
  --neo    the Neo2308 community GTFS (Aug 2026), merged where it adds a service we lack
  --trips  service-day records (raw/trips/YYYYMMDD.csv.gz): vehicle, depot, type, delays
  --punct  a folder of punctuality/YYYY-MM-DD.csv.gz: per service-day-stop scheduled vs GPS times (parse.py)

Output (REPO/data):
  gtfs/            GTFS: agency, feed_info, calendar, routes, trips, stops, stop_times, shapes
  apsrtc-gtfs.zip  the same, zipped
  stop_delays.csv.gz  per service and stop: the median lateness from a week of GPS (minutes)
  service_days.csv.gz per service and day: vehicle, depot, type, start/end delays, GPS fixes
  punctuality/     per service day (YYYY-MM-DD.csv.gz) and stop: scheduled and observed times, delay, dwell
  stats.json       the counts the README quotes

Routes are corridors, as on the map (maps.reclaim.city/apsrtc): every service between the
same two termini, city and intercity kept apart; route_id is the map's route slug.
No driver data exists in any input or output.
"""
import argparse, collections, csv, glob, gzip, io, json, os, re, shutil, zipfile

CITY = re.compile(r'CITY|METRO')


def slug(x):
    return re.sub(r'^-|-$', '', re.sub(r'[^a-z0-9]+', '-', str(x or '').lower()))


def hhmmss(t, base):
    """'HH:MM' -> GTFS time; times after midnight on a trip that started the day before go past 24:00."""
    if not t or not re.match(r'^\d{1,2}:\d{2}', t):
        return None
    h, m = int(t[:2]), int(t[3:5])
    x = h * 60 + m
    if base is not None and x < base - 60:
        x += 1440
    return f'{x // 60:02d}:{x % 60:02d}:00', x


def decode(flat):
    out, x, y = [], 0, 0
    for i in range(0, len(flat), 2):
        x += flat[i]; y += flat[i + 1]
        out.append((x / 1e5, y / 1e5))
    return out


def main():
    ap = argparse.ArgumentParser()
    for k in ('web', 'neo', 'trips', 'punct', 'out'):
        ap.add_argument('--' + k, required=True)
    a = ap.parse_args()
    stops = json.load(open(os.path.join(a.web, 'stops.json')))
    sv = json.load(open(os.path.join(a.web, 'services.json')))
    segs = json.load(open(os.path.join(a.web, 'segs.json')))['segs']
    S, TYPES = sv['services'], sv['types']
    SEG = {(g[0], g[1]): g[3] for g in segs}

    # ── corridors, exactly as the map groups them (apsrtc/app.js loadNetwork) ──
    groups = {}
    for k, v in S.items():
        st = v[6]
        if not st or len(st) < 2:
            continue
        typ = TYPES[v[1]] if v[1] < len(TYPES) else 'OTHER'
        city = bool(CITY.search(typ))
        x, y = st[0], st[-1]
        gk = ('c:' if city else 'i:') + (x + '|' + y if x < y else y + '|' + x)
        g = groups.setdefault(gk, {'city': city, 'codes': collections.Counter(), 'types': collections.Counter(), 'pats': {}, 'keys': []})
        g['codes'][k.split('_')[0]] += 1; g['types'][typ] += 1
        g['pats'].setdefault('>'.join(st), {'stops': st, 'first': k})
        g['keys'].append(k)
    routes, svc_route, taken = [], {}, set()
    for gk, g in groups.items():
        num = g['codes'].most_common(1)[0][0]
        p0 = next(iter(g['pats'].values()))
        e = sorted([(stops.get(p0['stops'][0]) or [''])[0], (stops.get(p0['stops'][-1]) or [''])[0]])
        rid, d = slug(f'{num}-{e[0]}-{e[1]}')[:80], 2
        while rid in taken:
            rid = slug(f'{num}-{e[0]}-{e[1]}')[:76] + f'-{d}'; d += 1
        taken.add(rid)
        typ = g['types'].most_common(1)[0][0]
        routes.append((rid, num, e, g['city'], typ, len(g['keys'])))
        for k in g['keys']:
            svc_route[k] = rid

    out = os.path.join(a.out, 'data'); g_dir = os.path.join(out, 'gtfs')
    shutil.rmtree(g_dir, ignore_errors=True); os.makedirs(g_dir)
    W = lambda name, hdr: (lambda f: (f, csv.writer(f)))(open(os.path.join(g_dir, name), 'w', newline=''))
    nice = lambda s: ' '.join(w.capitalize() if len(w) > 3 or not w.isalpha() else w for w in str(s or '').split()).title() if s and s == s.upper() else s

    f, w = W('agency.txt', None)
    w.writerow(['agency_id', 'agency_name', 'agency_url', 'agency_timezone', 'agency_lang', 'agency_phone'])
    w.writerow(['APSRTC', 'Andhra Pradesh State Road Transport Corporation', 'https://apsrtc.ap.gov.in', 'Asia/Kolkata', 'en', '149']); f.close()
    f, w = W('feed_info.txt', None)
    w.writerow(['feed_publisher_name', 'feed_publisher_url', 'feed_lang', 'feed_version', 'feed_contact_url'])
    w.writerow(['reclaim.city', 'https://maps.reclaim.city/apsrtc/', 'en', sv.get('v', ''), 'https://github.com/reclaimchennai/apsrtc']); f.close()
    f, w = W('calendar.txt', None)
    w.writerow(['service_id', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday', 'start_date', 'end_date'])
    w.writerow(['daily', 1, 1, 1, 1, 1, 1, 1, '20261101', '20271031']); f.close()

    f, w = W('routes.txt', None)
    w.writerow(['route_id', 'agency_id', 'route_short_name', 'route_long_name', 'route_desc', 'route_type'])
    for rid, num, e, city, typ, n in routes:
        w.writerow([rid, 'APSRTC', num, f'{nice(e[0])} – {nice(e[1])}', f'{"City" if city else "Intercity"} · {nice(typ)} · {n} trips/day', 3])
    f.close()

    # ── trips, stop_times, shapes (one shape per stop pattern), delays ──
    used_stops, shape_of = set(), {}
    ft, wt = W('trips.txt', None); wt.writerow(['route_id', 'service_id', 'trip_id', 'trip_short_name', 'trip_headsign', 'shape_id', 'trip_type'])
    fs, ws = W('stop_times.txt', None); ws.writerow(['trip_id', 'arrival_time', 'departure_time', 'stop_id', 'stop_sequence', 'timepoint'])
    fsh, wsh = W('shapes.txt', None); wsh.writerow(['shape_id', 'shape_pt_lat', 'shape_pt_lon', 'shape_pt_sequence'])
    fd = open(os.path.join(out, 'stop_delays.csv'), 'w', newline=''); wd = csv.writer(fd)
    wd.writerow(['trip_id', 'stop_sequence', 'stop_id', 'scheduled', 'median_delay_min'])
    seen_trip, ntrips, nst = set(), 0, 0
    for k, v in S.items():
        st, tm, dl = v[6], v[7], v[8] if len(v) > 8 else []
        if k not in svc_route or len(st) < 2:
            continue
        pk = '>'.join(st)
        if pk not in shape_of:
            sid = f's{len(shape_of)}'; shape_of[pk] = sid
            pts = []
            for x, y in zip(st, st[1:]):
                g = SEG.get((x, y))
                c = decode(g) if g else [(stops[x][2], stops[x][1]), (stops[y][2], stops[y][1])] if x in stops and y in stops else []
                pts.extend(c[1:] if pts else c)
            for i, (lo, la) in enumerate(pts):
                wsh.writerow([sid, f'{la:.5f}', f'{lo:.5f}', i + 1])
        t0 = hhmmss(tm[0] if tm else v[3], None)
        base = t0[1] if t0 else None
        rows = []
        for i, s_ in enumerate(st):
            t = hhmmss(tm[i] if i < len(tm) else None, base)
            rows.append((s_, t))
        if not any(t for _, t in rows):
            continue
        # GTFS needs times at the first and last stop
        if not rows[0][1] or not rows[-1][1]:
            continue
        wt.writerow([svc_route[k], 'daily', k, v[0], nice(v[5]), shape_of[pk], nice(TYPES[v[1]]) if v[1] < len(TYPES) else ''])
        for i, (s_, t) in enumerate(rows):
            used_stops.add(s_)
            ws.writerow([k, t[0] if t else '', t[0] if t else '', s_, i + 1, 1 if t else 0]); nst += 1
            if i < len(dl) and dl[i] is not None:
                wd.writerow([k, i + 1, s_, tm[i] if i < len(tm) else '', dl[i]])
        seen_trip.add((pk, rows[0][1][0])); ntrips += 1

    # ── the community feed: only services we do not already have (same stops, same start) ──
    neo_added, neo_routes = 0, set()
    try:
        nst_ = collections.defaultdict(list)
        for r in csv.DictReader(open(os.path.join(a.neo, 'stop_times.txt'))):
            nst_[r['trip_id']].append(r)
        nstops = {r['stop_id']: r for r in csv.DictReader(open(os.path.join(a.neo, 'stops.txt')))}
        ntr = {r['trip_id']: r for r in csv.DictReader(open(os.path.join(a.neo, 'trips.txt')))}
        for tid, rows in nst_.items():
            rows.sort(key=lambda r: int(r['stop_sequence']))
            pk = '>'.join(r['stop_id'] for r in rows)
            if (pk, rows[0]['departure_time']) in seen_trip or any(pk == p for p, _ in list(seen_trip)[:0]):
                continue
            if any(r['stop_id'] not in stops and r['stop_id'] not in nstops for r in rows):
                continue
            rid = 'neo-' + slug(ntr.get(tid, {}).get('route_id', tid))
            neo_routes.add(rid)
            wt.writerow([rid, 'daily', 'neo-' + tid, ntr.get(tid, {}).get('route_id', ''), '', '', ''])
            for r in rows:
                used_stops.add(r['stop_id'])
                ws.writerow(['neo-' + tid, r['arrival_time'], r['departure_time'], r['stop_id'], r['stop_sequence'], 1]); nst += 1
            neo_added += 1
        with open(os.path.join(g_dir, 'routes.txt'), 'a', newline='') as fr:
            wr = csv.writer(fr)
            for rid in sorted(neo_routes):
                wr.writerow([rid, 'APSRTC', rid[4:].upper(), '', 'From the Neo2308 community GTFS (Aug 2026), not yet seen in our collection', 3])
    except FileNotFoundError:
        nstops = {}
    ft.close(); fs.close(); fsh.close(); fd.close()

    f, w = W('stops.txt', None)
    w.writerow(['stop_id', 'stop_name', 'stop_lat', 'stop_lon'])
    for s_ in sorted(used_stops):
        if s_ in stops:
            n, la, lo = stops[s_]
            w.writerow([s_, nice(n), f'{la:.6f}', f'{lo:.6f}'])
        elif s_ in nstops:
            r = nstops[s_]; w.writerow([s_, nice(r['stop_name']), r['stop_lat'], r['stop_lon']])
    f.close()

    zp = os.path.join(out, 'apsrtc-gtfs.zip')
    with zipfile.ZipFile(zp, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for n in sorted(os.listdir(g_dir)):
            z.write(os.path.join(g_dir, n), n)

    # the big files live only in the zip; the repo keeps the small ones browsable
    for n in ('shapes.txt', 'stop_times.txt'):
        os.remove(os.path.join(g_dir, n))
    gz = lambda p: (shutil.copyfileobj(open(p, 'rb'), gzip.open(p + '.gz', 'wb', 9)), os.remove(p))
    gz(os.path.join(out, 'stop_delays.csv'))

    # ── service days: what ran, on which bus, how late ──
    keep = ['serviceDocId', 'date', 'vehicle', 'depot', 'serviceType', 'dcp', 'trip', 'oprsNo', 'origin', 'destination',
            'n_stops', 'sched_start', 'sched_end', 'gps_start', 'gps_end', 'start_delay_min', 'end_delay_min', 'stops_observed', 'fixes']
    days, ndays = set(), 0
    with open(os.path.join(out, 'service_days.csv'), 'w', newline='') as fo:
        wo = csv.writer(fo); wo.writerow(keep)
        for p in sorted(glob.glob(os.path.join(a.trips, '*.csv.gz'))):
            for r in csv.DictReader(io.TextIOWrapper(gzip.open(p), encoding='utf-8')):
                wo.writerow([r.get(c, '') for c in keep]); days.add(r.get('date')); ndays += 1
    # punctuality: one gzipped CSV per service day (a single file would be 600 MB unzipped)
    pdir = os.path.join(out, 'punctuality'); shutil.rmtree(pdir, ignore_errors=True); os.makedirs(pdir)
    prow = 0
    for p in sorted(glob.glob(os.path.join(a.punct, '*.csv.gz'))):
        shutil.copy(p, pdir); prow += sum(1 for _ in gzip.open(p)) - 1
    gz(os.path.join(out, 'service_days.csv'))

    stats = dict(routes=len(routes) + len(neo_routes), routes_corridors=len(routes), routes_city=sum(1 for r in routes if r[3]), trips=ntrips + neo_added, trips_from_neo2308=neo_added,
                 stop_times=nst, stops=len(used_stops), shapes=len(shape_of), service_types=dict(collections.Counter(r[4] for r in routes)),
                 service_days=ndays, days=sorted(d for d in days if d), stop_delays=sum(1 for _ in gzip.open(os.path.join(out, 'stop_delays.csv.gz'))) - 1,
                 punctuality_rows=prow, punctuality_days=sorted(os.path.basename(p)[:10] for p in glob.glob(os.path.join(pdir, '*.csv.gz'))), feed_version=sv.get('v', ''))
    json.dump(stats, open(os.path.join(out, 'stats.json'), 'w'), indent=1)
    print(json.dumps(stats, indent=1))


if __name__ == '__main__':
    main()
