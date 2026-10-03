"""'What changed' alerts for saved routes (spec C3).

Web/simulator subscribers pull via GET /api/alerts. SMS subscribers (subscribed through the Twilio
webhook) are pushed after any state change, at most MAX_PER_DAY per subscriber per Gaza day, through
the Twilio REST API. Push is a no-op unless TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM are set.
"""
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

import httpx

import sms
from state import STATES

MAX_PER_DAY = 3
GAZA_TZ = timezone(timedelta(hours=3))
log = logging.getLogger("tareeq.alerts")


def twilio_send(to, body):
    sid, token, frm = (os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"))
    if not (sid and token and frm):
        log.info("alert not sent (Twilio not configured)")
        return False
    r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                   data={"To": to, "From": frm, "Body": body}, auth=(sid, token), timeout=15)
    if r.status_code >= 300:
        log.warning("Twilio send failed: %s", r.status_code)
        return False
    return True


class Alerts:
    def __init__(self, store, router, subscriptions, send=twilio_send):
        self.store, self.router = store, router   # callables: the world can be reset
        self.subs = subscriptions
        self.send = send
        self.sent = {}                            # sender -> (gaza day, count)
        self.lock = threading.Lock()

    def collect(self, sender):
        """Alert text if a segment on the subscriber's route changed since the last alert, else None."""
        sub = self.subs.get(sender)
        if not sub:
            return None
        evs = [e for e in self.store().events if e["ts"] > sub["since"] and e["seg"] in sub["segs"]]
        if not evs:
            return None
        sub["since"] = self.store().now()
        r = self.router().route(sub["from"]["node"], sub["to"]["node"])
        sub["segs"] = {s for l in (r or {}).get("legs", []) for s in l["segments"]}
        self.subs[sender] = sub   # write-through for persistent storage
        worst = max(evs, key=lambda e: STATES.index(e["to"]))
        msg = sms.fit(f"تنبيه: تغيّر طريقك ({sms.STATE_AR[worst['to']]}). " + sms.route_reply(sub["from"], sub["to"], r))
        return {"alerts": [msg], "events": evs, "route": r}

    def dispatch(self):
        """Push pending alerts to SMS subscribers, respecting the daily cap."""
        day = datetime.fromtimestamp(self.store().now(), GAZA_TZ).date()
        with self.lock:
            for sender, sub in list(self.subs.items()):
                if sub.get("channel") != "twilio":
                    continue
                d, n = self.sent.get(sender, (day, 0))
                if d != day:
                    n = 0
                if n >= MAX_PER_DAY:
                    continue      # don't consume events; the next day's first alert summarises the route
                a = self.collect(sender)
                if not a:
                    continue
                body = a["alerts"][0]
                if n == MAX_PER_DAY - 1:
                    body = sms.fit("آخر تنبيه اليوم. " + body)
                self.sent[sender] = (day, n + 1)
                threading.Thread(target=self.send, args=(sender, body), daemon=True).start()

    def unsubscribe(self, sender):
        self.subs.pop(sender, None)
        self.sent.pop(sender, None)
