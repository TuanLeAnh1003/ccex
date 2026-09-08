"""How fast an account is being spent, and when that lands it on its cap.

Percentages arrive as snapshots; a rate needs two of them. So every observer -- the
statusline recorder on each render, the watch view on each tick -- drops a sample in a
small ring per account, and the estimate is read back out of that. Samples are only
appended when a number actually moves, so an idle account costs nothing and a ring of
two hundred covers hours of real work.
"""
import datetime, os, time

from ccexlib import USAGE_DIR, fresh, hm, save, snap_path

KEEP = 200              # samples per account; only changes are recorded
LOOKBACK = 45 * 60      # a rate older than this says nothing about what you are doing now
MIN_SPAN = 300          # a percent gained in a minute would extrapolate to nonsense


def hist_path(email):
    return snap_path(email).replace(".json", ".burn.json")


def note(email, five, seven):
    """Record a sample, if either number has moved since the last one.

    Two writers (a statusline render, a watch tick) can race here; the loser's sample is
    lost, which costs nothing -- the next one lands. Nothing else reads this file.
    """
    if not email or (five is None and seven is None):
        return
    p = hist_path(email)
    ring = (fresh(p).get("samples") or [])[-KEEP:]
    now = int(time.time())
    if ring:
        last = ring[-1]
        if last[1] == five and last[2] == seven:
            return                        # nothing moved; the old sample still stands
        if now - last[0] < 5:
            ring.pop()                    # same instant, newer reading: replace it
    ring.append([now, five, seven])
    try:
        os.makedirs(USAGE_DIR, exist_ok=True)
        save(p, {"email": email, "samples": ring[-KEEP:]}, unique=True)
    except OSError:
        pass


GUESS_LOG = os.path.join(USAGE_DIR, "guess.log")   # every estimate, scored against what came next


def guess_path(email):
    return snap_path(email).replace(".json", ".guess.json")


def note_guess(email, key, was, guess, per_hour, blind_for):
    """Remember the estimate standing in for this window, so the next reading can score it.

    An estimate is only worth anything if it can be checked, and the only thing that can
    check it is the reading that ends the blackout. So the standing guess is kept where that
    reading will find it: overwritten as it grows, and read once when the truth arrives.
    """
    if not email:
        return
    p = guess_path(email)
    have = fresh(p).get("guesses") or {}
    have[key] = {"was": was, "guess": round(guess, 1), "rate": round(per_hour, 1),
                 "blind_for": int(blind_for), "at": int(time.time())}
    try:
        os.makedirs(USAGE_DIR, exist_ok=True)
        save(p, {"email": email, "guesses": have}, unique=True)
    except OSError:
        pass


def score(email, util):
    """Score any standing estimate for this account against the reading that just landed.

    Called from a render, which is the one moment a measured number exists to compare
    against. A window that reset while we were blind cannot be scored -- the estimate was
    answering a question that stopped being asked -- so it is dropped rather than logged as
    a miss it did not make.
    """
    if not email:
        return
    p = guess_path(email)
    guesses = fresh(p).get("guesses") or {}
    if not guesses:
        return
    lines = []
    for key, g in guesses.items():
        actual = ((util or {}).get(key) or {}).get("utilization")
        if actual is None:
            continue
        if actual + 0.5 < g["was"]:
            lines.append("%s %s reset while blind, estimate %.0f%% not scored" % (
                email, key, g["guess"]))
            continue
        off = g["guess"] - actual
        lines.append("%s %s estimate %.0f%% vs actual %.0f%% (%+.1f after %s blind from %.0f%% at %.1f%%/h)"
                     % (email, key, g["guess"], actual, -off, hm(g["blind_for"]),
                        g["was"], g["rate"]))
    try:
        os.remove(p)                  # scored once; the next blackout files its own
    except OSError:
        pass
    if not lines:
        return
    try:
        with open(GUESS_LOG, "a") as f:      # O_APPEND, so two renders cannot interleave
            for l in lines:
                f.write("%s  ccex: %s\n" % (
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), l))
    except OSError:
        pass


def rate(email, key, now=None):
    """Percent per hour this window is climbing, or None if we cannot honestly say.

    Only the run of samples since the window last reset counts: a reset drops the
    percentage to zero, and averaging across that would report a negative burn on an
    account that is in fact filling up again.
    """
    col = 1 if key == "five_hour" else 2
    now = now or time.time()
    ring = [s for s in (fresh(hist_path(email)).get("samples") or [])
            if s[col] is not None and now - s[0] <= LOOKBACK]
    if len(ring) < 2:
        return None
    run = [ring[-1]]
    for s in reversed(ring[:-1]):
        if s[col] > run[0][col]:
            break                         # older reading was higher: the window reset in between
        run.insert(0, s)
    if len(run) < 2:
        return None
    span = run[-1][0] - run[0][0]
    climb = run[-1][col] - run[0][col]
    if span < MIN_SPAN or climb <= 0:
        return None
    return climb / (span / 3600.0)


def eta(pct, cap, per_hour, resets_at=None, now=None):
    """(seconds until this window hits its cap, or None; True if it resets first).

    A window that refills before you could spend it never triggers a switch, so saying
    "in 6 hours" for a window that resets in one would be a lie with a number on it.
    """
    now = now or time.time()
    if pct is None or cap is None or not per_hour or pct >= cap:
        return None, False
    secs = (cap - pct) / per_hour * 3600.0
    if resets_at and resets_at - now < secs:
        return None, True
    return secs, False
