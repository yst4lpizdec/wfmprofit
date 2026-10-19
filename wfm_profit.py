import argparse
import html
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone

BASE_URL = os.environ.get("WFM_BASE_URL", "https://api.warframe.market/v2")
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
REPORT_PATH = os.environ.get("WFM_REPORT") or os.path.join(HERE, "wfm_report.html")
CACHE_DIR = os.environ.get("WFM_CACHE") or CACHE_DIR

ITEMS_TTL = 24 * 3600      # список предметов
SETS_TTL = 3 * 24 * 3600   # состав сетов меняется редко
PRICES_TTL = 10 * 60       # цены — 10 минут
STATS_TTL = 3 * 3600       # история продаж — 3 часа

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ----------------------------------------------------------------- утилиты

def g(d, *keys, default=None):
    """Достаёт первое существующее поле (API бывает camelCase и snake_case)."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def item_name(it):
    i18n = g(it, "i18n", default={}) or {}
    en = i18n.get("en") if isinstance(i18n, dict) else None
    return (g(en, "name", "itemName") if en else None) or g(it, "itemName", "item_name", "name") or g(it, "slug", "urlName", "url_name", default="?")


def item_slug(it):
    return g(it, "slug", "urlName", "url_name")


def unwrap(payload):
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    if isinstance(payload, dict) and "payload" in payload:  # старый v1
        return payload["payload"]
    return payload


class Cache:
    def __init__(self, name, ttl):
        self.path = os.path.join(CACHE_DIR, name + ".json")
        self.ttl = ttl
        self.data = {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            now = time.time()
            self.data = {k: v for k, v in raw.items() if ttl is None or now - v.get("t", 0) < ttl}
        except Exception:
            self.data = {}

    def get(self, key):
        v = self.data.get(key)
        return v["v"] if v else None

    def set(self, key, value):
        self.data[key] = {"t": time.time(), "v": value}

    def save(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)


# ----------------------------------------------------------------- клиент API

class WFM:
    def __init__(self, platform="pc", crossplay=True, rps=2.8):
        self.headers = {
            "User-Agent": "wfm-profit/1.0 (personal tool)",
            "Accept": "application/json",
            "Language": "ru",
            "Platform": platform,
            "Crossplay": "true" if crossplay else "false",
        }
        self.min_gap = 1.0 / rps
        self._last = 0.0
        self._lock = threading.Lock()

    def _wait(self):
        with self._lock:
            now = time.monotonic()
            delay = self._last + self.min_gap - now
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()

    def get(self, path, params=None, base=None):
        url = (base or BASE_URL) + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(6):
            self._wait()
            req = urllib.request.Request(url, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in (429, 500, 502, 503, 504, 509):
                    time.sleep(2 ** attempt)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(2 ** attempt)
        raise RuntimeError("Не удалось получить " + url)

    # --- эндпоинты
    def items(self):
        return unwrap(self.get("/items")) or []

    def item_set(self, slug):
        d = unwrap(self.get("/item/%s/set" % slug))
        if isinstance(d, dict):
            return g(d, "items", default=[]) or []
        return d or []

    def stats(self, slug):
        """История реальных сделок (v1). -> payload или None."""
        d = self.get("/items/%s/statistics" % slug, base=BASE_URL.replace("/v2", "/v1"))
        return unwrap(d) if d else None

    def top_orders(self, slug):
        """Возвращает (sells, buys) — только онлайн/ingame игроки."""
        d = unwrap(self.get("/orders/item/%s/top" % slug))
        if isinstance(d, dict) and ("sell" in d or "buy" in d):
            return d.get("sell") or [], d.get("buy") or []
        # запасной вариант — полный список ордеров
        d = unwrap(self.get("/orders/item/%s" % slug)) or []
        if isinstance(d, dict):
            d = g(d, "orders", default=[]) or []
        sells, buys = [], []
        for o in d:
            st = g(g(o, "user", default={}), "status", default="offline")
            if st not in ("ingame", "online"):
                continue
            if g(o, "visible", default=True) is False:
                continue
            t = g(o, "type", "orderType", "order_type")
            (sells if t == "sell" else buys).append(o)
        sells.sort(key=lambda o: g(o, "platinum", default=10**9))
        buys.sort(key=lambda o: -g(o, "platinum", default=0))
        return sells[:5], buys[:5]


# ----------------------------------------------------------------- сбор данных

def summarize(sells, buys):
    def pack(o):
        u = g(o, "user", default={}) or {}
        return {
            "p": g(o, "platinum", default=0),
            "q": g(o, "quantity", default=1),
            "n": g(u, "ingameName", "ingame_name", default=""),
            "s": g(u, "status", default=""),
            "r": g(u, "reputation", default=0),
        }
    sells = sorted((pack(o) for o in sells), key=lambda x: x["p"])
    buys = sorted((pack(o) for o in buys), key=lambda x: -x["p"])
    # приоритет тем, кто прямо в игре
    return {"sell": sells[:5], "buy": buys[:5]}


def summarize_stats(payload):
    """Сводка по завершённым сделкам: цена и сколько штук продаётся в день."""
    closed = g(payload, "statistics_closed", default={}) or {}
    days = [e for e in closed.get("90days") or [] if not e.get("mod_rank")]
    hours = [e for e in closed.get("48hours") or [] if not e.get("mod_rank")]
    last7 = days[-7:]
    vol7 = sum(e.get("volume") or 0 for e in last7)
    vol48 = sum(e.get("volume") or 0 for e in hours)

    def wavg(rows):
        v = sum(e.get("volume") or 0 for e in rows)
        if not v:
            return None
        return round(sum((e.get("wa_price") or e.get("avg_price") or 0) * (e.get("volume") or 0) for e in rows) / v)
    price = wavg(hours) if vol48 >= 3 else wavg(last7)
    if price is None:
        return None
    return {"p": price, "day": round(vol7 / 7.0, 1) if last7 else round(vol48 / 2.0, 1), "v48": vol48,
            "lo": min((e.get("min_price") or price) for e in (hours if vol48 >= 3 else last7)),
            "hi": max((e.get("max_price") or price) for e in (hours if vol48 >= 3 else last7))}


def progress(i, n, label):
    if i < n and i % max(1, n // 50):
        return
    w = 30
    done = int(w * i / max(n, 1))
    sys.stdout.write("\r  %s [%s%s] %d/%d " % (label, "#" * done, "." * (w - done), i, n))
    sys.stdout.flush()
    if i >= n:
        sys.stdout.write("\n")


def collect(api, limit=None, fresh=False, offline=False):
    ttl = (lambda t: None if offline else t)
    items_c = Cache("items", ttl(ITEMS_TTL))
    sets_c = Cache("sets", ttl(SETS_TTL))
    prices_c = Cache("prices_" + api.headers["Platform"], ttl(PRICES_TTL))
    stats_c = Cache("stats_" + api.headers["Platform"], ttl(STATS_TTL))
    if fresh:
        prices_c.data = {}

    print("1/4  Список предметов...")
    items = items_c.get("all_ru") or (items_c.get("all") if offline else None)
    if not items:
        if offline:
            raise RuntimeError("Нет кэша — сначала запусти без --offline")
        items = api.items()
        items_c.set("all_ru", items)
        items_c.save()
    print("     предметов: %d" % len(items))

    prime_sets = [it for it in items
                  if "set" in (g(it, "tags", default=[]) or [])
                  and "prime" in (g(it, "tags", default=[]) or [])]
    if not prime_sets:  # если теги не пришли — по названию
        prime_sets = [it for it in items if (item_slug(it) or "").endswith("_prime_set")]
    prime_sets.sort(key=lambda it: item_slug(it))
    if limit:
        prime_sets = prime_sets[:limit]
    vaulted = {item_slug(it): bool(g(it, "vaulted", default=False)) for it in items}

    print("2/4  Состав прайм-сетов (%d)..." % len(prime_sets))
    sets = []
    for i, it in enumerate(prime_sets, 1):
        slug = item_slug(it)
        members = sets_c.get(slug)
        if members is None and offline:
            continue
        if members is None:
            raw = api.item_set(slug)
            members = [{
                "slug": item_slug(m),
                "name": item_name(m),
                "root": bool(g(m, "setRoot", "set_root", default=item_slug(m) == slug)),
                "qty": g(m, "quantityInSet", "quantity_in_set", default=1) or 1,
                "ducats": g(m, "ducats", default=0) or 0,
            } for m in raw]
            sets_c.set(slug, members)
            if i % 20 == 0:
                sets_c.save()
        progress(i, len(prime_sets), "сеты ")
        if members:
            sets.append({"slug": slug, "name": item_name(it), "members": members,
                         "vaulted": vaulted.get(slug, False)})
    sets_c.save()

    all_slugs = []
    seen = set()
    for s in sets:
        for m in s["members"]:
            if m["slug"] not in seen:
                seen.add(m["slug"])
                all_slugs.append(m["slug"])

    need = [] if offline else [sl for sl in all_slugs if prices_c.get(sl) is None]
    eta = len(need) / (1.0 / api.min_gap)
    print("3/4  Цены (%d предметов, нужно скачать %d, ~%d мин)..." % (len(all_slugs), len(need), round(eta / 60 + 0.5)))
    for i, sl in enumerate(need, 1):
        try:
            sells, buys = api.top_orders(sl)
            prices_c.set(sl, summarize(sells, buys))
        except Exception as e:
            print("\n     ! %s: %s" % (sl, e))
        if i % 25 == 0:
            prices_c.save()
        progress(i, len(need), "цены ")
    prices_c.save()

    prices = {sl: prices_c.get(sl) for sl in all_slugs if prices_c.get(sl) is not None}

    # история реальных продаж — только для сетов (это то, что мы продаём при сборке)
    set_slugs = [s["slug"] for s in sets]
    need = [] if offline else [sl for sl in set_slugs if stats_c.get(sl) is None]
    print("4/4  Реальные продажи сетов (нужно скачать %d, ~%d мин)..." % (len(need), round(len(need) * api.min_gap / 60 + 0.5)))
    for i, sl in enumerate(need, 1):
        try:
            payload = api.stats(sl)
            stats_c.set(sl, summarize_stats(payload) if isinstance(payload, dict) else None)
        except Exception as e:
            print("\n     ! %s: %s" % (sl, e))
        if i % 25 == 0:
            stats_c.save()
        progress(i, len(need), "продажи ")
    stats_c.save()
    stats = {sl: stats_c.get(sl) for sl in set_slugs if stats_c.get(sl)}

    # каталог для отчёта: английское имя (для /w), русское, картинка
    by_slug = {item_slug(it): it for it in items}
    catalog = {}
    for sl in all_slugs:
        it = by_slug.get(sl, {})
        i18n = g(it, "i18n", default={}) or {}
        en = i18n.get("en") or {}
        ru = i18n.get("ru") or {}
        catalog[sl] = {
            "en": en.get("name") or item_name(it) or sl,
            "ru": ru.get("name") if ru.get("name") and ru.get("name") != en.get("name") else "",
            "img": en.get("thumb") or en.get("icon") or "",
        }
    for s in sets:
        for m in s["members"]:
            catalog.setdefault(m["slug"], {"en": m["name"], "ru": "", "img": ""})
    return sets, prices, catalog, stats


# ----------------------------------------------------------------- анализ

def best(p, side):
    lst = (p or {}).get(side) or []
    return lst[0] if lst else None


def fill(orders, need):
    """Набирает нужное количество по самым дешёвым лотам. -> (цена, [(лот, сколько)]) или None."""
    got, cost, take = 0, 0, []
    for o in orders or []:
        if got >= need:
            break
        n = min(max(o.get("q") or 1, 1), need - got)
        take.append({"o": o, "n": n})
        cost += o["p"] * n
        got += n
    return (cost, take) if got >= need else None


def quart(xs):
    """Нижняя четверть — цена именно «хлама», а не ходовых частей того же номинала."""
    xs = sorted(xs)
    return xs[len(xs) // 4] if xs else None


def analyze(sets, prices, stats=None):
    stats = stats or {}
    """Готовит списки сделок. В строках только slug'и — имена/картинки в каталоге."""
    assemble, disassemble, ducats, flips, sell = [], [], [], [], []
    seen = set()

    for s in sets:
        setp = prices.get(s["slug"])
        parts = [m for m in s["members"] if not m["root"]]
        if not parts:
            continue
        set_sell, set_buy = best(setp, "sell"), best(setp, "buy")

        rows, ok, all_buy = [], True, True
        for m in parts:
            pp = prices.get(m["slug"]) or {}
            sells = pp.get("sell") or []
            pb = best(pp, "buy")
            f = fill(sells, m["qty"])
            row = {"slug": m["slug"], "qty": m["qty"], "sell": sells[0] if sells else None, "buy": pb}
            if f:
                row["cost"], row["take"] = f
                # Цена держится на одном лоте, который сильно дешевле остальных?
                if len(sells) >= 2 and sells[0]["p"] <= 0.6 * sells[1]["p"] and sells[1]["p"] - sells[0]["p"] >= 3:
                    alt = fill(sells[1:], m["qty"])
                    row["alone"] = alt[0] if alt else None
            else:
                ok = False
            all_buy &= pb is not None
            rows.append(row)

        if ok and set_sell:
            cost = sum(r["cost"] for r in rows)
            sellers = {t["o"]["n"] for r in rows for t in r["take"]}
            lst = max(set_sell["p"] - 1, 1)
            safe = cost + sum((r["alone"] - r["cost"]) for r in rows if r.get("alone"))
            st = stats.get(s["slug"])
            assemble.append({
                "slug": s["slug"], "cost": cost, "get": lst, "profit": lst - cost,
                "st": st, "real": (min(st["p"], lst) - cost) if st else None,
                "now": (set_buy["p"] - cost) if set_buy else None, "buyer": set_buy,
                "safe": (lst - safe) if safe != cost else None,
                "roi": round(100.0 * (lst - cost) / cost) if cost else 0,
                "trades": len(sellers) + 1, "parts": rows,
            })
        if set_sell and all(r["sell"] for r in rows):
            rev = sum(max(r["sell"]["p"] - 1, 1) * r["qty"] for r in rows)
            now = (sum(r["buy"]["p"] * r["qty"] for r in rows) - set_sell["p"]) if all_buy else None
            disassemble.append({
                "slug": s["slug"], "cost": set_sell["p"], "seller": set_sell, "st": stats.get(s["slug"]),
                "get": rev, "profit": rev - set_sell["p"], "now": now,
                "roi": round(100.0 * (rev - set_sell["p"]) / set_sell["p"]) if set_sell["p"] else 0,
                "trades": 1 + sum(r["qty"] for r in rows), "parts": rows,
            })

        for m in s["members"]:
            if m["slug"] in seen:
                continue
            seen.add(m["slug"])
            pp = prices.get(m["slug"])
            ps, pb = best(pp, "sell"), best(pp, "buy")
            if m["ducats"] and ps and not m["root"]:
                ducats.append({"slug": m["slug"], "d": m["ducats"], "p": ps["p"],
                               "dpp": round(m["ducats"] / ps["p"], 1), "seller": ps,
                               "lots": [{"p": o["p"], "q": o.get("q") or 1, "n": o["n"], "s": o.get("s", "")} for o in pp["sell"]],
                               "bid": pb["p"] if pb else None})
            if ps and pb:
                # Ставим заказ на +1 к лучшему покупателю, продаём на -1 к лучшему продавцу
                buy_at, sell_at = pb["p"] + 1, max(ps["p"] - 1, 1)
                profit = sell_at - buy_at
                if pb["p"] >= ps["p"]:  # можно купить и тут же продать
                    buy_at, sell_at, profit = ps["p"], pb["p"], pb["p"] - ps["p"]
                flips.append({"slug": m["slug"], "buy_at": buy_at, "sell_at": sell_at,
                              "profit": profit, "instant": pb["p"] >= ps["p"],
                              "roi": round(100.0 * profit / buy_at) if buy_at else 0,
                              "depth": len(pp["sell"]) + len(pp["buy"]),
                              "seller": ps, "buyer": pb})
            if pb:
                sell.append({"slug": m["slug"], "p": pb["p"], "buyer": pb, "d": m["ducats"],
                             "low": ps["p"] if ps else None, "set": m["root"]})

    # --- дукаты: рыночный курс по номиналам и «оптовые» продавцы
    tiers = {}
    for r in ducats:
        t = tiers.setdefault(r["d"], {"sell": [], "bid": []})
        t["sell"].append(r["p"])
        if r["bid"]:
            t["bid"].append(r["bid"])
    rates = [{"d": d, "n": len(t["sell"]), "sell": quart(t["sell"]), "bid": quart(t["bid"]),
              "bids": len(t["bid"])} for d, t in sorted(tiers.items())]

    # если покупатель уже даёт больше, чем выйдет с лота, — считаем по лучшему варианту
    for r in assemble + disassemble:
        if r["now"] is not None and r["now"] > r["profit"]:
            r["profit"] = r["now"]
            base = r["cost"]
            r["roi"] = round(100.0 * r["profit"] / base) if base else 0
    assemble.sort(key=lambda r: -r["profit"])
    disassemble.sort(key=lambda r: -r["profit"])
    ducats.sort(key=lambda r: (-r["dpp"], r["p"]))
    flips.sort(key=lambda r: -r["profit"])
    sell.sort(key=lambda r: -r["p"])
    return {"assemble": assemble, "disassemble": disassemble,
            "ducats": ducats, "rates": rates,
            "flips": flips, "sell": sell}


# ----------------------------------------------------------------- отчёт

PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WFM Profit</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Exo+2:wght@500;600;700;800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
 --bg:#0a0d12;--bg2:#0f141b;--card:#131a23;--card2:#18212c;--line:#222c39;--line2:#2c3847;
 --text:#e8ecf2;--soft:#b3bdcb;--mute:#7c8898;
 --gold:#e3bf6b;--gold2:#f3d58f;--golddim:rgba(227,191,107,.12);
 --good:#46d39a;--gooddim:rgba(70,211,154,.12);--bad:#ff7a7a;--blue:#69a7ff;--warn:#f0a35a;
 --r:14px;
}
*{box-sizing:border-box}
html{color-scheme:dark}
body{margin:0;background:var(--bg);color:var(--text);font:14.5px/1.5 Inter,"Segoe UI",system-ui,sans-serif;
 background-image:radial-gradient(1200px 500px at 70% -200px,rgba(227,191,107,.07),transparent 60%)}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px 60px}
h1,h2,h3,.num{font-family:"Exo 2",Inter,sans-serif}
button{font:inherit;color:inherit}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}

/* header */
.top{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:22px 0 18px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:38px;height:38px;border-radius:10px;display:grid;place-items:center;
 background:linear-gradient(145deg,#2a2212,#14110a);border:1px solid #4b3d1d;color:var(--gold);font:800 18px "Exo 2",Inter,"Segoe UI",system-ui,sans-serif}
.brand h1{margin:0;font-size:22px;font-weight:700;letter-spacing:.3px}
.brand h1 b{color:var(--gold)}
.meta{color:var(--mute);font-size:12.5px}
.ghost{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 14px;cursor:pointer;color:var(--soft)}
.ghost:hover{border-color:var(--line2);color:var(--text)}

/* hero */
.hero{display:grid;grid-template-columns:1.4fr 1fr;gap:14px;margin-bottom:22px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:18px 20px}
.hero .big{font:800 34px/1.1 "Exo 2",Inter,"Segoe UI",system-ui,sans-serif;color:var(--gold2);margin:4px 0 6px}
.hero .lbl{color:var(--mute);font-size:12.5px;text-transform:uppercase;letter-spacing:.08em}
.hero p{margin:0;color:var(--soft)}
.best{display:flex;align-items:center;gap:12px;cursor:pointer}
.best:hover .bn{color:var(--gold2)}
.best .bn{font-weight:600}
.best .bp{margin-left:auto;font:700 20px "Exo 2",Inter,"Segoe UI",system-ui,sans-serif;color:var(--good)}

/* strategy picker */
.modes{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:18px}
.mode{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:14px 14px 12px;cursor:pointer;text-align:left;position:relative;transition:border-color .15s,background .15s}
.mode:hover{border-color:var(--line2);background:var(--card2)}
.mode.on{border-color:var(--gold);background:linear-gradient(180deg,var(--golddim),transparent 70%),var(--card)}
.mode .ic{font-size:20px;line-height:1}
.mode h3{margin:8px 0 2px;font-size:15.5px;font-weight:700}
.mode .d{color:var(--mute);font-size:12.5px;line-height:1.35;min-height:34px}
.mode .c{margin-top:8px;font-size:12.5px;color:var(--soft)}
.mode .c b{color:var(--good)}

/* explainer */
.explain{display:flex;gap:18px;align-items:flex-start;background:var(--bg2);border:1px solid var(--line);border-radius:var(--r);padding:14px 18px;margin-bottom:14px}
.explain .flow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-weight:600}
.explain .flow span{background:var(--card2);border:1px solid var(--line2);padding:4px 10px;border-radius:8px}
.explain .flow i{color:var(--gold);font-style:normal}
.explain p{margin:6px 0 0;color:var(--soft);font-size:13.5px}

/* toolbar */
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.search{flex:1 1 260px;position:relative}
.search input{width:100%;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px 10px 36px;color:var(--text);font:inherit;outline:none}
.search input:focus{border-color:var(--gold)}
.search::before{content:"⌕";position:absolute;left:12px;top:50%;transform:translateY(-52%);color:var(--mute);font-size:17px}
.seg{display:flex;flex-wrap:wrap;max-width:100%;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:3px}
.seg button{white-space:nowrap;background:none;border:0;padding:6px 11px;border-radius:7px;cursor:pointer;color:var(--mute);font-size:13px}
.seg button.on{background:var(--card2);color:var(--text);box-shadow:inset 0 0 0 1px var(--line2)}
.seg .t{padding:6px 8px 6px 10px;color:var(--mute);font-size:12px;align-self:center}
.count{color:var(--mute);font-size:13px;margin:0 0 10px}

/* deal cards */
.list{display:flex;flex-direction:column;gap:8px}
.deal{background:var(--card);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;transition:border-color .15s}
.deal:hover{border-color:var(--line2)}
.deal.open{border-color:#3a4a5e}
.row{display:grid;grid-template-columns:52px minmax(0,1fr) auto auto 22px;gap:14px;align-items:center;padding:12px 16px;cursor:pointer}
.img{width:52px;height:52px;border-radius:10px;background:var(--bg2) center/contain no-repeat;border:1px solid var(--line)}
.img.sm{width:34px;height:34px;border-radius:8px}
.nm{font-weight:600;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ru{color:var(--mute);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:5px}
.tag{font-size:11.5px;padding:2px 8px;border-radius:20px;background:var(--bg2);border:1px solid var(--line);color:var(--soft);white-space:nowrap}
.tag.g{color:var(--good);border-color:rgba(70,211,154,.35);background:var(--gooddim)}
.tag.w{color:var(--warn);border-color:rgba(240,163,90,.35)}
.money{display:flex;align-items:center;gap:8px;font-variant-numeric:tabular-nums;color:var(--soft);white-space:nowrap}
.money .a{color:var(--mute);font-size:12px;display:block;line-height:1.1}
.money .v{font-weight:600;color:var(--text)}
.money .arr{color:var(--gold);font-size:16px}
.profit{text-align:right;min-width:86px}
.profit .v{font:800 24px/1 "Exo 2",Inter,"Segoe UI",system-ui,sans-serif;color:var(--good)}
.profit .v.neg{color:var(--bad)}
.profit .s{color:var(--mute);font-size:12px}
.chev{color:var(--mute);transition:transform .2s}
.deal.open .chev{transform:rotate(90deg);color:var(--gold)}
a.il{color:inherit;text-decoration:none}
a.il:hover{color:var(--gold2);text-decoration:underline;text-underline-offset:3px}
a.tag.il:hover{text-decoration:none;filter:brightness(1.25)}
a.img{display:block;transition:border-color .15s}a.img:hover{border-color:var(--gold)}
.nm .ext{font-size:.75em;color:var(--mute);opacity:0;transition:opacity .15s}
.row:hover .nm .ext{opacity:1}
.pl::after{content:"p";margin-left:1px;font-size:.78em;opacity:.75}

/* steps */
.steps{display:none;border-top:1px solid var(--line);background:var(--bg2);padding:14px 16px 16px}
.deal.open .steps{display:block}
.steps h4{margin:2px 0 10px;font:600 12px Inter;text-transform:uppercase;letter-spacing:.08em;color:var(--mute)}
.step{display:grid;grid-template-columns:26px 34px minmax(0,1fr) auto;gap:12px;align-items:center;padding:8px 0;border-bottom:1px dashed var(--line)}
.step:last-child{border-bottom:0}
.step.done{opacity:.45}
.step.done .st{text-decoration:line-through}
.ck{width:22px;height:22px;border-radius:6px;border:1.5px solid var(--line2);background:none;cursor:pointer;display:grid;place-items:center;color:transparent;font-size:13px}
.step.done .ck{background:var(--good);border-color:var(--good);color:#06130d}
.st{color:var(--soft)}
.st b{color:var(--text);font-weight:600}
.st .who{display:inline-flex;align-items:center;gap:5px;color:var(--text)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--mute);display:inline-block}
.dot.ingame{background:var(--good)}.dot.online{background:var(--blue)}
.act{display:flex;gap:6px}
.btn{background:var(--card2);border:1px solid var(--line2);border-radius:8px;padding:6px 11px;cursor:pointer;font-size:12.5px;white-space:nowrap;color:var(--text)}
.btn:hover{border-color:var(--gold);color:var(--gold2)}
.btn.gold{background:var(--gold);color:#1b1408;border-color:var(--gold);font-weight:600}
.btn.gold:hover{background:var(--gold2);color:#1b1408}
.note{margin-top:10px;color:var(--mute);font-size:12.5px}

/* compact grid (ducats / sell) */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px}
.mini{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;display:grid;grid-template-columns:44px minmax(0,1fr) auto;gap:10px;align-items:center}
.mini .img{width:44px;height:44px}
.mini .k{text-align:right}
.mini .k .v{font:800 20px/1 "Exo 2",Inter,"Segoe UI",system-ui,sans-serif;color:var(--gold2)}
.mini .k .s{color:var(--mute);font-size:11.5px}
.mini .foot{grid-column:1/-1;display:flex;justify-content:space-between;align-items:center;gap:8px;border-top:1px dashed var(--line);padding-top:9px;font-size:12.5px;color:var(--soft)}
.verdict{font-size:11.5px;font-weight:600;padding:2px 8px;border-radius:20px}
.verdict.sell{background:var(--gooddim);color:var(--good)}
.verdict.baro{background:var(--golddim);color:var(--gold2)}

.sec{font:700 16px "Exo 2",Inter,system-ui,sans-serif;margin:22px 0 4px}
.sec span{font:500 12px Inter;color:var(--mute);background:var(--card);border:1px solid var(--line);padding:1px 8px;border-radius:10px;margin-left:6px;vertical-align:2px}
.rates{margin-bottom:6px}.rates .lbl{color:var(--mute);font-size:12.5px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px}
.rgrid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
.rate{background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.rate .rd{font:800 22px "Exo 2",Inter,system-ui,sans-serif;color:var(--gold2)}.rate .rd span{font-size:12px;color:var(--mute);font-weight:500}
.rate .rl{font-size:12.5px;color:var(--soft)}.rate .rl b{color:var(--text)}
.mute{color:var(--mute)}
.rin{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--soft);margin:4px 0}
.rin input{width:58px;background:var(--card);border:1px solid var(--line2);border-radius:6px;color:var(--gold2);font:600 14px Inter;padding:3px 6px}
.rin input:focus{outline:none;border-color:var(--gold)}
@media (max-width:680px){.rgrid{grid-template-columns:1fr 1fr}}
.more{display:block;margin:14px auto 0;padding:10px 22px}
.empty{padding:40px;text-align:center;color:var(--mute);border:1px dashed var(--line);border-radius:var(--r)}

/* help */
.help{display:none;margin-bottom:18px}
.help.on{display:block}
.help .cols{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
.help h3{margin:0 0 6px;font-size:15px;color:var(--gold2)}
.help p,.help li{color:var(--soft);font-size:13.5px;margin:0 0 6px}
.help ul{padding-left:18px;margin:0}

.toast{position:fixed;left:50%;bottom:24px;transform:translate(-50%,20px);opacity:0;background:#1d2733;border:1px solid var(--line2);padding:11px 16px;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.4);transition:.2s;pointer-events:none;font-size:13.5px;max-width:calc(100% - 32px)}
.toast.on{opacity:1;transform:translate(-50%,0)}
.toast b{color:var(--good)}

@media (max-width:1000px){.modes{grid-template-columns:repeat(3,1fr)}.hero{grid-template-columns:1fr}.help .cols{grid-template-columns:1fr}}
@media (max-width:680px){
 .wrap{padding:0 14px 50px}.modes{grid-template-columns:1fr 1fr}
 .row{grid-template-columns:44px minmax(0,1fr) auto;gap:10px}
 .row .money,.row .chev{display:none}.img{width:44px;height:44px}
 .step{grid-template-columns:24px minmax(0,1fr);}.step .img{display:none}.step .act{grid-column:2}
}
</style></head><body>
<div class="wrap">
 <div class="top">
  <div class="brand"><div class="logo">P</div><div><h1>WFM <b>Profit</b></h1><div class="meta"><span id="upd" data-ts="__TS__">__META__</span>__META2__</div></div></div>
  <div style="display:flex;gap:8px;flex-wrap:wrap">__ADMIN__<button class="ghost" id="helpBtn">Как этим пользоваться?</button></div>
 </div>

 <div class="panel help" id="help"><div class="cols">
  <div><h3>1. Выбери способ заработка</h3><p>Сверху пять способов. Начни с «Сборки сетов» — это самый простой и стабильный заработок. Цифра на карточке — сколько выгодных сделок найдено.</p></div>
  <div><h3>2. Открой сделку</h3><p>Клик по сделке покажет пошаговый план: у кого купить, за сколько и кому продать. Кнопка <b>«Написать»</b> копирует сообщение — открой чат в Warframe и нажми Ctrl+V.</p></div>
  <div><h3>3. Отмечай прогресс</h3><p>Ставь галочки на выполненных шагах — они запоминаются. Цены живые: если продавец уже ушёл, пиши следующему на warframe.market.</p></div>
 </div>
 <div class="cols" style="margin-top:14px">
  <div><h3>Значки</h3><ul><li><span class="dot ingame"></span> игрок сейчас в игре — ответит быстрее</li><li><span class="dot online"></span> онлайн на сайте</li></ul></div>
  <div><h3>«Можно продать сразу»</h3><p>Есть покупатель, который уже готов заплатить больше, чем ты потратишь. Не нужно ждать — это самые надёжные сделки.</p></div>
  <div><h3>📈 Реальные продажи</h3><p>За сколько сет на самом деле продавали последние дни и сколько штук уходит в день. Сортировка «надёжность» ставит наверх сделки, где прибыль подтверждается реальными продажами. Если сет продают меньше 1 штуки в день — не берись.</p></div>
  <div><h3>Сделок</h3><p>Сколько раз придётся торговать. В Warframe есть дневной лимит трейдов (зависит от ранга мастерства), так что прибыль на одну сделку тоже важна.</p></div>
 </div></div>

 <div class="hero">
  <div class="panel"><div class="lbl">Можно заработать прямо сейчас</div><div class="big" id="heroSum"></div><p id="heroTxt"></p><button class="btn gold" id="heroGo" style="margin-top:14px;padding:9px 16px">Открыть лучшую сделку →</button></div>
  <div class="panel" id="heroBest"></div>
 </div>

 <div class="modes" id="modes"></div>
 <div class="explain" id="explain"></div>
 <div class="bar" id="bar"></div>
 <div class="count" id="count"></div>
 <div id="list"></div>
</div>
<div class="toast" id="toast"></div>

<script>
const D=__DATA__, C=__CATALOG__;
const IMG="https://warframe.market/static/assets/", WFM="https://warframe.market/items/";
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const en=sl=>(C[sl]||{}).en||sl, ru=sl=>(C[sl]||{}).ru||"";
const itemUrl=sl=>WFM+encodeURIComponent(sl);
const ext=`target="_blank" rel="noopener"`;
const img=(sl,cls="")=>{const i=(C[sl]||{}).img;return `<a class="img ${cls}" href="${itemUrl(sl)}" ${ext} title="Открыть ${esc(en(sl))} на warframe.market" style="${i?`background-image:url('${IMG}${i}')`:""}"></a>`};
const nmL=sl=>`<a class="il" href="${itemUrl(sl)}" ${ext} title="Открыть на warframe.market"><b>${esc(en(sl))}</b></a>`;
const pl=v=>`<span class="pl">${v}</span>`;
const sign=v=>(v>0?"+":"")+v;
const store={get(k,d){try{const v=localStorage.getItem("wfm_"+k);return v==null?d:JSON.parse(v)}catch(_){return d}},
             set(k,v){try{localStorage.setItem("wfm_"+k,JSON.stringify(v))}catch(_){}}};
const who=o=>o?`<a class="who il" href="https://warframe.market/profile/${encodeURIComponent(o.n)}" ${ext} title="Профиль игрока на warframe.market"><span class="dot ${esc(o.s)}"></span>${esc(o.n)}</a>`:"—";
const plural=(n,a,b,c)=>{n=Math.abs(n)%100;const m=n%10;return n>10&&n<20?c:m>1&&m<5?b:m==1?a:c};

function wmsg(kind,o,sl,price,n){
 if(!o||!o.n)return "";
 if(n>1)return `/w ${o.n} Hi! I want to ${kind}: ${n}x "${en(sl)}" for ${price} platinum each (${n*price} total). (warframe.market)`;
 return `/w ${o.n} Hi! I want to ${kind}: "${en(sl)}" for ${price} platinum. (warframe.market)`}
function wbtn(kind,o,sl,price,label,n){const m=wmsg(kind,o,sl,price,n);if(!m)return "";
 return `<button class="btn ${label?"gold":""}" data-copy="${esc(m)}">${label||"Написать"}</button>`}
const mkt=sl=>`<a class="btn" href="${itemUrl(sl)}" ${ext}>На сайте ↗</a>`;

let toastT;
function toast(h){const t=$("#toast");t.innerHTML=h;t.classList.add("on");clearTimeout(toastT);toastT=setTimeout(()=>t.classList.remove("on"),2600)}
function copy(t){const ok=()=>toast("<b>Скопировано.</b> Открой чат в Warframe и нажми Ctrl+V");
 if(navigator.clipboard&&window.isSecureContext!==false){navigator.clipboard.writeText(t).then(ok,()=>fb())}else fb();
 function fb(){const a=document.createElement("textarea");a.value=t;document.body.appendChild(a);a.select();try{document.execCommand("copy");ok()}catch(_){prompt("Скопируй:",t)}a.remove()}}

/* ---------- режимы ---------- */
const MODES=[
 {id:"assemble",ic:"🧩",t:"Сборка сетов",d:"Купи части по отдельности → продай целым сетом",
  flow:["Купить части","Продать сет"],
  ex:"Многие продают части по одной дешевле, а сет целиком покупают дороже. Собираешь части у разных продавцов и продаёшь их одним лотом как сет. <b>Самый простой способ — начни с него.</b>"},
 {id:"disassemble",ic:"🔧",t:"Разбор сетов",d:"Купи сет целиком → продай по частям",
  flow:["Купить сет","Продать части"],
  ex:"Иногда части по отдельности стоят дороже сета. Покупаешь сет и продаёшь части разным людям. Продажа частей занимает больше времени — нужно несколько покупателей."},
 {id:"flips",ic:"🔁",t:"Перепродажа",d:"Купи дешевле → выставь дороже",
  flow:["Заказ на покупку","Ждёшь продавца","Продаёшь дороже"],
  ex:"Ставишь на сайте заказ на покупку чуть выше остальных покупателей — продавцы сами напишут тебе. Потом выставляешь предмет чуть дешевле остальных продавцов. Нужно терпение, зато работает почти на любом предмете."},
 {id:"ducats",ic:"🪙",t:"Дукаты для Баро",d:"Скупи дешёвые части за платину → сдай за дукаты",
  flow:["Купить хлам по курсу чата","Сдать в Реле","Дукаты → товары Баро"],
  ex:"Дукаты нужны для покупок у Баро Ки'Тиира. Главный источник хлама — <b>торговый чат</b>: там его скупают пачками по 6 штук. Сайт выгоден, только если там продают <b>не дороже, чем в чате</b>. Ниже — только такие лоты. Ориентир по чату: 45 дукатов ≈ 2p, 100 дукатов ≈ 6p за штуку."},
 {id:"sell",ic:"💰",t:"Продать своё",d:"Нафармил из реликвий — узнай, почём продать",
  flow:["Найди предмет","Напиши покупателю"],
  ex:"Введи название того, что у тебя лежит в инвентаре. Увидишь, кто прямо сейчас покупает дороже всех, и что выгоднее: продать за платину или сдать Баро за дукаты."},
];
const CHAT_DEF={15:1,25:1,45:2,65:3,100:6};
let chat=Object.assign({},CHAT_DEF,store.get("chat",{}));
const rateOf=d=>chat[d]!=null?chat[d]:Math.max(1,Math.round(d/15));
function cheapLots(r){return r.lots.filter(o=>o.p<=rateOf(r.d))}
function buildBundles(){const by={};
 for(const r of D.ducats)for(const o of cheapLots(r)){(by[o.n]=by[o.n]||{seller:o,items:[]}).items.push({slug:r.slug,d:r.d,p:o.p,q:o.q})}
 const out=[];for(const k in by){const e=by[k];e.items.sort((a,b)=>b.d/b.p-a.d/a.p);let left=6;const pick=[];
  for(const it of e.items){if(left<=0)break;const n=Math.min(it.q,left);pick.push({...it,n});left-=n}
  const cnt=pick.reduce((s,i)=>s+i.n,0);if(cnt<2)continue;
  const d=pick.reduce((s,i)=>s+i.d*i.n,0),p=pick.reduce((s,i)=>s+i.p*i.n,0);
  out.push({seller:e.seller,items:pick,cnt,d,p,dpp:Math.round(d/p*10)/10})}
 return out.sort((a,b)=>b.d-a.d||b.dpp-a.dpp)}
const good=(id)=>id==="ducats"?D.ducats.filter(r=>cheapLots(r).length):id==="sell"?D.sell:D[id].filter(r=>r.profit>0);

let mode=store.get("mode","assemble"); if(!MODES.find(m=>m.id===mode))mode="assemble";
let q="", minP=store.get("minP",1), sortBy="real", onlyNow=false, shown=30;
let done=store.get("done",{});

function renderModes(){
 $("#modes").innerHTML=MODES.map(m=>{const g=good(m.id);let c;
  if(m.id==="sell")c=`${D.sell.length} предметов с покупателями`;
  else if(m.id==="ducats")c=`<b>${g.length}</b> не дороже, чем в чате`;
  else c=g.length?`<b>${g.length}</b> в плюс · до <b>+${g[0]?g[0].profit:0}p</b>`:"сейчас пусто";
  return `<button class="mode ${m.id===mode?"on":""}" data-mode="${m.id}"><div class="ic">${m.ic}</div><h3>${m.t}</h3><div class="d">${m.d}</div><div class="c">${c}</div></button>`}).join("");
 const M=MODES.find(m=>m.id===mode);
 $("#explain").innerHTML=`<div><div class="flow">${M.flow.map(f=>`<span>${f}</span>`).join("<i>→</i>")}</div><p>${M.ex}</p></div>`;
}

function renderHero(){
 const a=D.assemble.filter(r=>relP(r)>0).sort((x,y)=>relP(y)-relP(x)), now=D.assemble.filter(r=>r.now>0);
 const top5=a.slice(0,5).reduce((s,r)=>s+relP(r),0);
 $("#heroSum").innerHTML=`+${top5} <span style="font-size:.55em;color:var(--soft)">платины</span>`;
 $("#heroTxt").innerHTML=`с пяти самых надёжных сборок (с учётом реальных цен продаж). Всего найдено <b>${a.length}</b> выгодных сборок, из них <b>${now.length}</b> можно продать покупателю сразу, без ожидания.`;
 const pick=[...D.assemble.filter(r=>r.now>0).sort((x,y)=>y.now-x.now).slice(0,1).map(r=>({r,m:"assemble",p:r.now,t:"сразу"})),
  ...a.slice(0,1).map(r=>({r,m:"assemble",p:relP(r),t:"сборка"})),
  ...D.disassemble.filter(r=>r.profit>0).slice(0,1).map(r=>({r,m:"disassemble",p:r.profit,t:"разбор"}))];
 $("#heroBest").innerHTML=`<div class="lbl" style="margin-bottom:10px">Лучшие сделки</div>`+(pick.map(x=>
  `<div class="best" data-jump="${x.m}|${x.r.slug}" style="padding:6px 0">${img(x.r.slug,"sm").replace("<a ","<div ").replace("</a>","</div>").replace(/href="[^"]*"|target="_blank"|rel="noopener"/g,"")}<div style="min-width:0"><div class="bn nm" style="font-size:14px">${esc(en(x.r.slug))}</div><div class="ru">${x.t}</div></div><div class="bp">+${x.p}p</div></div>`).join("")||`<p class="meta">Пока нет выгодных сделок — попробуй обновить позже.</p>`);
}

function renderBar(){
 const M=mode, ph=M==="sell"?"Что у тебя есть? Например: Rhino, Soma, Nikana…":"Поиск по названию…";
 let h=`<label class="search"><input id="q" type="search" placeholder="${ph}" value="${esc(q)}" autocomplete="off"></label>`;
 if(M==="assemble"||M==="disassemble"||M==="flips"){
  h+=`<div class="seg"><span class="t">Прибыль от</span>${[1,5,10,20].map(v=>`<button data-min="${v}" class="${minP===v?"on":""}">${v}p</button>`).join("")}</div>`;
  h+=`<div class="seg"><span class="t">Сортировка</span><button data-sort="profit" class="${sortBy==="profit"?"on":""}">прибыль</button>${M==="assemble"?`<button data-sort="real" class="${sortBy==="real"?"on":""}" title="Учитывает реальные цены продаж и как быстро сет уходит">надёжность</button>`:""}<button data-sort="roi" class="${sortBy==="roi"?"on":""}">% окупаемости</button>${M!=="flips"?`<button data-sort="per" class="${sortBy==="per"?"on":""}">за сделку</button>`:""}</div>`;
  if(M!=="flips")h+=`<div class="seg"><button data-now="1" class="${onlyNow?"on":""}">⚡ только «продать сразу»</button></div>`;
 }
 $("#bar").innerHTML=h;
 const qi=$("#q");qi.oninput=()=>{q=qi.value.trim().toLowerCase();shown=30;renderList()};
}

const match=sl=>!q||en(sl).toLowerCase().includes(q)||ru(sl).toLowerCase().includes(q);

/* ---------- карточки ---------- */
function stepRow(key,i,sl,text,act){
 const d=done[key+"#"+i];
 return `<div class="step ${d?"done":""}" data-step="${key}#${i}"><button class="ck" title="Отметить">✓</button>${img(sl,"sm")}<div class="st">${text}</div><div class="act">${act||""}</div></div>`}

function dealAssemble(r){
 const key="a:"+r.slug;let i=0;
 const steps=r.parts.map(p=>p.take.map((t,j)=>stepRow(key,i++,p.slug,
   `Купи ${nmL(p.slug)}${t.n>1?` <b>×${t.n}</b>`:""}${p.qty>1&&t.n<p.qty?` <span class="ru" style="display:inline">(${j+1}-й из ${p.qty} нужных)</span>`:""} за <b>${pl(t.o.p)}</b>${t.n>1?" за штуку":""} у ${who(t.o)}`
   +(p.alone&&j===0?` <a class="tag w il" href="${itemUrl(p.slug)}" ${ext} style="margin-left:4px" title="Посмотреть всех продавцов">редкая цена — следующий продаёт за ${Math.round(p.alone/p.qty)}p ↗</a>`:""),
   wbtn("buy",t.o,p.slug,t.o.p,"",t.n))).join("")).join("")
  + (r.now>0
   ? stepRow(key,i++,r.slug,`Продай сет сразу: ${who(r.buyer)} покупает за <b>${pl(r.buyer.p)}</b> → <b style="color:var(--good)">${sign(r.now)}p</b>`,wbtn("sell",r.buyer,r.slug,r.buyer.p,"Предложить"))
   : "")
  + stepRow(key,i++,r.slug,`${r.now>0?"Или выставь":"Выставь"} ${nmL(r.slug)} на продажу за <b>${pl(r.get)}</b> (на 1p дешевле всех) и жди покупателя`,mkt(r.slug));
 return card(r,key,`${pl(r.cost)}`,"части",`${pl(r.now!=null&&r.now+r.cost>r.get?r.buyer.p:r.get)}`,"сет",steps,
  [r.now>0?`<span class="tag g">⚡ можно продать сразу ${sign(r.now)}p</span>`
   :r.buyer?`<span class="tag w">⏳ покупатели сета дают до ${r.buyer.p}p — продажа может занять время</span>`:`<span class="tag w">⏳ на сет сейчас нет покупателей</span>`,
   r.safe!=null?`<span class="tag w">⚠ держится на одном дешёвом лоте · без него ${sign(r.safe)}p</span>`:"",
   stTag(r),`<span class="tag">${r.parts.length} ${plural(r.parts.length,"часть","части","частей")}</span>`,`<span class="tag">${r.trades} ${plural(r.trades,"сделка","сделки","сделок")}</span>`,`<span class="tag">окупаемость ${r.roi}%</span>`],
  `Покупать части лучше у тех, кто <span class="dot ingame"></span> в игре. Если кто-то не отвечает — открой предмет на сайте и напиши следующему продавцу.`);
}
function stTag(r){const st=r.st;if(!st)return `<span class="tag">нет данных о продажах</span>`;
 const speed=st.day>=10?"быстро":st.day>=3?"нормально":"медленно";
 const cls=st.day>=3?"g":"w";
 const warn=r.real!=null&&r.real<r.profit-3?` · по ним прибыль ${sign(r.real)}p`:"";
 return `<a class="tag ${cls} il" href="${itemUrl(r.slug)}/statistics" ${ext} title="Реальные сделки за последние дни: ${st.lo}–${st.hi}p. Открыть график">📈 продают по ~${st.p}p · ~${st.day} шт/день (${speed})${warn} ↗</a>`}
const relP=r=>{let v=r.real!=null?Math.min(r.profit,Math.max(r.real,r.now??-1e9)):r.profit;if(r.st&&r.st.day<1)v=Math.min(v,0);return v};
function dealDis(r){
 const key="d:"+r.slug;let i=0;
 const steps=stepRow(key,i++,r.slug,`Купи ${nmL(r.slug)} за <b>${pl(r.cost)}</b> у ${who(r.seller)}`,wbtn("buy",r.seller,r.slug,r.cost))
  + r.parts.map(p=>{const lp=Math.max(p.sell.p-1,1);
    const fast=p.buy&&p.buy.p>=lp-2?` · или сразу ${who(p.buy)} за <b>${pl(p.buy.p)}</b>`:(p.buy?` <span class="ru" style="display:inline">(сразу купят за ${p.buy.p}p)</span>`:"");
    return stepRow(key,i++,p.slug,`Продай ${nmL(p.slug)}${p.qty>1?` ×${p.qty}`:""} за <b>${pl(lp)}</b>${fast}`,
     (p.buy?wbtn("sell",p.buy,p.slug,p.buy.p,"Предложить"):"")+mkt(p.slug))}).join("");
 return card(r,key,pl(r.cost),"сет",pl(r.get),"части",steps,
  [r.now>0?`<span class="tag g">⚡ можно продать сразу ${sign(r.now)}p</span>`:`<span class="tag w">нужно ждать покупателей</span>`,`<span class="tag">${r.trades} ${plural(r.trades,"сделка","сделки","сделок")}</span>`,`<span class="tag">окупаемость ${r.roi}%</span>`],
  "Перед покупкой проверь, что части реально продаются: открой каждую на сайте и посмотри, сколько там покупателей.");
}
function dealFlip(r){
 const key="f:"+r.slug;let i=0;
 const steps=(r.instant
  ? stepRow(key,i++,r.slug,`Купи у ${who(r.seller)} за <b>${pl(r.buy_at)}</b>`,wbtn("buy",r.seller,r.slug,r.buy_at))
   +stepRow(key,i++,r.slug,`Сразу продай ${who(r.buyer)} за <b>${pl(r.sell_at)}</b>`,wbtn("sell",r.buyer,r.slug,r.sell_at,"Предложить"))
  : stepRow(key,i++,r.slug,`Создай на сайте <b>заказ на покупку</b> за <b>${pl(r.buy_at)}</b> (сейчас лучший покупатель даёт ${r.buyer.p}p)`,mkt(r.slug))
   +stepRow(key,i++,r.slug,`Жди — продавцы сами напишут тебе в игре`,"")
   +stepRow(key,i++,r.slug,`Выставь на продажу за <b>${pl(r.sell_at)}</b> (сейчас дешевле всех продаёт ${who(r.seller)} за ${r.seller.p}p)`,mkt(r.slug)));
 return card(r,key,pl(r.buy_at),"купить",pl(r.sell_at),"продать",steps,
  [r.instant?`<span class="tag g">⚡ купи и сразу продай</span>`:`<span class="tag w">нужно ждать</span>`,`<span class="tag">${r.depth>=9?"много":r.depth>=6?"средне":"мало"} ордеров</span>`,`<span class="tag">окупаемость ${r.roi}%</span>`],
  "Большой разрыв цен иногда означает, что предмет плохо продаётся. Выбирай предметы, где много ордеров.");
}
function card(r,key,a,al,b,bl,steps,tags,note){
 return `<div class="deal" data-key="${key}"><div class="row">${img(r.slug)}
  <div style="min-width:0"><div class="nm"><a class="il" href="${itemUrl(r.slug)}" ${ext} title="Открыть на warframe.market">${esc(en(r.slug))} <span class="ext">↗</span></a></div>${ru(r.slug)?`<div class="ru">${esc(ru(r.slug))}</div>`:""}<div class="tags">${tags.join("")}</div></div>
  <div class="money"><div><span class="a">${al}</span><span class="v">${a}</span></div><span class="arr">→</span><div><span class="a">${bl}</span><span class="v">${b}</span></div></div>
  <div class="profit"><div class="v ${r.profit<0?"neg":""}">${sign(r.profit)}<span style="font-size:.6em">p</span></div><div class="s">прибыль</div></div>
  <div class="chev">›</div></div>
  <div class="steps"><h4>План</h4>${steps}<div class="note">${note}</div></div></div>`}

function miniDucat(r){
 return `<div class="mini">${img(r.slug)}<div style="min-width:0"><div class="nm" style="font-size:14px"><a class="il" href="${itemUrl(r.slug)}" ${ext}>${esc(en(r.slug))}</a></div><div class="ru">${pl(r.p)} → ${r.d} дукатов</div></div>
 <div class="k"><div class="v">${r.dpp}</div><div class="s">дук. за 1p</div></div>
 <div class="foot"><span>у ${who(r.seller)} · ${r.seller.q} шт.</span>${wbtn("buy",r.seller,r.slug,r.p)}</div></div>`}
function ratesHtml(){
 const rs=D.rates.filter(r=>[15,25,45,65,100].includes(r.d));
 return `<div class="panel rates"><div class="lbl">Курс хлама: за сколько платины брать 1 штуку</div><div class="rgrid">${rs.map(r=>{
  const n=D.ducats.filter(x=>x.d===r.d&&cheapLots(x).length).length;
  return `<div class="rate"><div class="rd">${r.d}<span> дук.</span></div>
  <label class="rin">в чате берут по <input type="number" min="0.1" step="0.5" data-rate="${r.d}" value="${rateOf(r.d)}">p</label>
  <div class="rl">≈ <b>${Math.round(r.d/rateOf(r.d)*10)/10}</b> дук. за 1p</div>
  <div class="rl mute">на сайте: от ${r.sell??"—"}p${r.bids?`, скупщики ${r.bid}p`:""}</div>
  <div class="rl">${n?`<b style="color:var(--good)">${n}</b> лотов по курсу`:`<span class="mute">по курсу нет</span>`}</div></div>`}).join("")}</div>
  <p class="count" style="margin:10px 0 0">По умолчанию — цены скупщиков из торгового чата («6 штук за 12p» = 2p за штуку). Поменяй, если в чате сейчас другой курс, — списки ниже пересчитаются. <a href="#" data-reset="1">Сбросить</a></p></div>`}
function bundle(b){
 const msg=`/w ${b.seller.n} Hi! I want to buy: ${b.items.map(i=>`${i.n>1?i.n+"x ":""}"${en(i.slug)}"`).join(", ")} for ${b.p} platinum total. (warframe.market)`;
 return `<div class="mini" style="grid-template-columns:minmax(0,1fr) auto"><div style="min-width:0"><div class="nm" style="font-size:14px">${who(b.seller)}</div><div class="ru">${b.cnt} шт. · ${b.d} дукатов за ${b.p}p</div></div>
 <div class="k"><div class="v">${b.dpp}</div><div class="s">дук. за 1p</div></div>
 <div class="foot" style="flex-direction:column;align-items:stretch"><div style="display:flex;flex-direction:column;gap:3px">${b.items.map(i=>`<span>${i.n>1?i.n+"× ":""}<a class="il" href="${itemUrl(i.slug)}" ${ext}>${esc(en(i.slug))}</a> <span class="mute">· ${i.d} дук. · ${i.p}p</span></span>`).join("")}</div>
 <button class="btn gold" data-copy="${esc(msg)}">Написать — купить всё за ${b.p}p</button></div></div>`}
function miniSell(r){
 const plat=r.d?r.p/r.d:null, baro=r.d&&plat<0.1;
 return `<div class="mini">${img(r.slug)}<div style="min-width:0"><div class="nm" style="font-size:14px"><a class="il" href="${itemUrl(r.slug)}" ${ext}>${esc(en(r.slug))}</a></div><div class="ru">${r.d?`или ${r.d} дукатов у Баро`:(r.set?"целый сет":"")}</div></div>
 <div class="k"><div class="v" style="color:var(--good)">${pl(r.p)}</div><div class="s">покупают</div></div>
 <div class="foot"><span>${r.d?`<span class="verdict ${baro?"baro":"sell"}">${baro?"За платину копейки — сдай Баро":"Продавай за платину"}</span>`:who(r.buyer)}</span>${wbtn("sell",r.buyer,r.slug,r.p,"Продать")}</div></div>`}

function renderList(){
 let rows=D[mode].filter(r=>match(r.slug)), html="";
 if(mode==="ducats"){rows=rows.filter(r=>cheapLots(r).length);
  const bs=buildBundles().filter(b=>b.items.some(i=>match(i.slug)));
  $("#count").textContent="";
  html=ratesHtml()
   +`<h3 class="sec">Пачкой у одного продавца <span>${bs.length}</span></h3><p class="count">Только лоты не дороже курса чата. Один трейд — до 6 предметов, сверху — больше всего дукатов за сделку.</p>`
   +(bs.length?`<div class="grid">${bs.slice(0,shown).map(bundle).join("")}</div>`:`<div class="empty">На сайте сейчас нет пачек дешевле, чем в чате. Покупай хлам у скупщиков в торговом чате или выставь свой заказ на покупку.</div>`)
   +`<h3 class="sec">По одной штуке <span>${rows.length}</span></h3><p class="count">Выгодно, только если покупаешь заодно с другими сделками — каждый трейд на счету.</p>`
   +(rows.length?`<div class="grid">${rows.slice(0,shown).map(miniDucat).join("")}</div>`:"");
 }else if(mode==="sell"){
  $("#count").textContent=q?`Найдено: ${rows.length}`:`Самые дорогие запросы на покупку. Начни вводить название, чтобы найти своё.`;
  html=rows.length?`<div class="grid">${rows.slice(0,shown).map(miniSell).join("")}</div>`:"";
 }else{
  rows=rows.filter(r=>r.profit>=minP);
  if(onlyNow&&mode!=="flips")rows=rows.filter(r=>r.now>0);
  const k=sortBy==="roi"?(r=>r.roi):sortBy==="per"?(r=>r.profit/(r.trades||1)):sortBy==="real"?relP:(r=>r.profit);
  rows.sort((a,b)=>k(b)-k(a));
  $("#count").textContent=`${rows.length} ${plural(rows.length,"сделка","сделки","сделок")} с прибылью от ${minP}p · нажми на сделку, чтобы увидеть план`;
  const f=mode==="assemble"?dealAssemble:mode==="disassemble"?dealDis:dealFlip;
  html=rows.length?`<div class="list">${rows.slice(0,shown).map(f).join("")}</div>`:"";
 }
 if(!rows.length)html=`<div class="empty">Ничего не найдено. ${q?"Попробуй другое название.":"Попробуй уменьшить минимальную прибыль или обнови данные позже."}</div>`;
 if(rows.length>shown)html+=`<button class="ghost more" id="more">Показать ещё (${rows.length-shown})</button>`;
 $("#list").innerHTML=html;
}

function renderAll(){renderModes();renderBar();renderList()}

document.addEventListener("click",e=>{
 const t=e.target;
 const cp=t.closest("[data-copy]"); if(cp){e.stopPropagation();copy(cp.dataset.copy);return}
 if(t.closest("a"))return;
 const m=t.closest("[data-mode]"); if(m){mode=m.dataset.mode;store.set("mode",mode);q="";shown=30;onlyNow=false;if(sortBy==="real"&&mode!=="assemble")sortBy="profit";if(mode==="assemble")sortBy="real";renderAll();return}
 const ck=t.closest(".ck"); if(ck){const s=ck.closest(".step"),k=s.dataset.step;done[k]=!done[k];if(!done[k])delete done[k];store.set("done",done);s.classList.toggle("done");return}
 const mn=t.closest("[data-min]"); if(mn){minP=+mn.dataset.min;store.set("minP",minP);renderBar();renderList();return}
 const so=t.closest("[data-sort]"); if(so){sortBy=so.dataset.sort;renderBar();renderList();return}
 if(t.closest("[data-now]")){onlyNow=!onlyNow;renderBar();renderList();return}
 if(t.closest("[data-reset]")){e.preventDefault();chat={...CHAT_DEF};store.set("chat",{});renderModes();renderList();return}
 if(t.closest("#more")){shown+=30;renderList();return}
 if(t.closest("#heroGo")){const r=D.assemble[0];if(r){mode="assemble";q="";minP=1;onlyNow=false;shown=30;renderAll();const el=document.querySelector(`.deal[data-key="a:${r.slug}"]`);if(el){el.classList.add("open");el.scrollIntoView({behavior:"smooth",block:"center"})}}return}
 if(t.closest("#helpBtn")){$("#help").classList.toggle("on");return}
 const j=t.closest("[data-jump]"); if(j){const [mm,sl]=j.dataset.jump.split("|");mode=mm;q="";minP=1;onlyNow=false;shown=200;renderAll();
   const el=document.querySelector(`.deal[data-key="${mm[0]}:${sl}"]`);if(el){el.classList.add("open");el.scrollIntoView({behavior:"smooth",block:"center"})}return}
 const row=t.closest(".row"); if(row){row.parentElement.classList.toggle("open");return}
});
document.addEventListener("change",e=>{const i=e.target.closest("[data-rate]");if(!i)return;const v=parseFloat(i.value);
 if(v>0){chat[i.dataset.rate]=v;const sv={};for(const k in chat)if(chat[k]!==CHAT_DEF[k])sv[k]=chat[k];store.set("chat",sv);renderModes();renderList()}});
if(!store.get("seenHelp",false)){$("#help").classList.add("on");store.set("seenHelp",true)}
(function(){const u=$("#upd"),ts=Date.parse(u.dataset.ts);if(!ts)return;
 const d=new Date(ts),m=Math.round((Date.now()-ts)/60000);
 const ago=m<1?"только что":m<60?`${m} мин назад`:m<1440?`${Math.floor(m/60)} ч назад`:`${Math.floor(m/1440)} дн назад`;
 u.innerHTML=`Обновлено ${d.toLocaleString("ru-RU",{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"})} <b style="color:${m>120?"var(--warn)":"var(--soft)"}">(${ago})</b>`;
 if(m>120)u.title="Данные устарели — цены могли измениться";})();
renderHero();renderAll();
</script></body></html>"""


def write_report(result, catalog, platform, n_sets, n_prices):
    meta = "Обновлено %s" % datetime.now().strftime("%d.%m.%Y в %H:%M")
    meta2 = " · %s · %d сетов · только игроки онлайн" % (platform.upper(), n_sets)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    admin = os.environ.get("WFM_ADMIN_URL") or (
        "https://github.com/%s/actions/workflows/update.yml" % os.environ["GITHUB_REPOSITORY"]
        if os.environ.get("GITHUB_REPOSITORY") else "")
    admin_html = ('<a class="ghost" href="%s" target="_blank" rel="noopener" title="Для админа: Run workflow → обновит отчёт за ~6 минут">↻ Обновить сейчас</a>' % html.escape(admin)) if admin else ""
    used = set()
    for rows in result.values():
        for r in rows:
            if "slug" in r:
                used.add(r["slug"])
            for p in r.get("parts", []) + r.get("items", []):
                used.add(p["slug"])
    cat = {k: v for k, v in catalog.items() if k in used}
    js = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = (PAGE.replace("__META__", html.escape(meta)).replace("__META2__", html.escape(meta2))
                .replace("__TS__", ts).replace("__ADMIN__", admin_html)
                .replace("__CATALOG__", js(cat))
                .replace("__DATA__", js(result)))
    if os.path.dirname(REPORT_PATH):
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(page)
    return REPORT_PATH


def print_top(result, catalog):
    nm = lambda sl: (catalog.get(sl) or {}).get("en", sl)
    print("\n  Лучшие сборки сетов:")
    for r in [r for r in result["assemble"] if r["profit"] > 0][:5]:
        print("    %-30s части %4dp -> сет %4dp  = +%dp" % (nm(r["slug"])[:30], r["cost"], r["get"], r["profit"]))


def main():
    ap = argparse.ArgumentParser(description="Поиск заработка платины на warframe.market")
    ap.add_argument("--platform", default="pc", choices=["pc", "ps4", "xbox", "switch", "mobile"])
    ap.add_argument("--no-crossplay", action="store_true", help="не учитывать игроков с других платформ")
    ap.add_argument("--limit", type=int, help="сканировать только N сетов (для быстрого теста)")
    ap.add_argument("--fresh", action="store_true", help="игнорировать кэш цен")
    ap.add_argument("--offline", action="store_true", help="пересобрать отчёт из кэша, ничего не скачивая")
    ap.add_argument("--no-open", action="store_true", help="не открывать отчёт в браузере")
    a = ap.parse_args()

    t0 = time.time()
    api = WFM(platform=a.platform, crossplay=not a.no_crossplay)
    try:
        sets, prices, catalog, stats = collect(api, limit=a.limit, fresh=a.fresh, offline=a.offline)
    except KeyboardInterrupt:
        print("\nОстановлено. Скачанные цены сохранены в кэш — следующий запуск продолжит.")
        return
    result = analyze(sets, prices, stats)
    path = write_report(result, catalog, a.platform, len(sets), len(prices))
    print_top(result, catalog)
    print("\nГотово за %d с. Отчёт: %s" % (time.time() - t0, path))
    if not a.no_open:
        webbrowser.open("file:///" + path.replace("\\", "/"))


if __name__ == "__main__":
    main()
