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

PLAT_B64 = "data:image/png;base64,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"
DUCAT_B64 = "data:image/png;base64,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"

PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WFM Profit — Сканер рынка Warframe</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Exo+2:ital,wght@0,500;0,600;0,700;0,800;0,900;1,700&family=Plus+Jakarta+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600;700&display=swap" rel="stylesheet">
<style>
:root{
 --bg:#07090e;
 --bg-elevated:#0d121c;
 --card:rgba(15, 21, 33, 0.78);
 --card-hover:rgba(21, 29, 46, 0.92);
 --line:rgba(255, 255, 255, 0.08);
 --line-hover:rgba(255, 255, 255, 0.16);
 --text:#f1f5f9;
 --soft:#94a3b8;
 --mute:#64748b;
 --gold:#f5c042;
 --gold-bright:#fde047;
 --gold-dim:rgba(245, 192, 66, 0.12);
 --gold-border:rgba(245, 192, 66, 0.32);
 --gold-gradient:linear-gradient(135deg, #fde047 0%, #f5c042 50%, #d97706 100%);
 --good:#10b981;
 --good-bright:#34d399;
 --good-dim:rgba(16, 185, 129, 0.14);
 --good-border:rgba(16, 185, 129, 0.35);
 --bad:#f43f5e;
 --bad-dim:rgba(244, 63, 94, 0.14);
 --blue:#38bdf8;
 --blue-dim:rgba(56, 189, 248, 0.14);
 --warn:#fbbf24;
 --warn-dim:rgba(251, 191, 36, 0.14);
 --r:16px;
 --r-sm:10px;
 --shadow:0 8px 32px -4px rgba(0, 0, 0, 0.5), 0 2px 8px -1px rgba(0, 0, 0, 0.3);
 --blur:blur(16px);
}
*{box-sizing:border-box;margin:0;padding:0}
html{color-scheme:dark;scroll-behavior:smooth}
body{
 min-height:100vh;
 background-color:var(--bg);
 color:var(--text);
 font:14px/1.55 'Plus Jakarta Sans',-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;
 background-image:
   radial-gradient(900px 500px at 85% -100px, rgba(245, 192, 66, 0.08), transparent 70%),
   radial-gradient(1000px 600px at 10% 200px, rgba(56, 189, 248, 0.05), transparent 75%),
   radial-gradient(800px 800px at 50% 1000px, rgba(16, 185, 129, 0.03), transparent 80%),
   linear-gradient(to bottom, transparent, rgba(7, 9, 14, 0.6));
 background-attachment:fixed;
 -webkit-font-smoothing:antialiased;
}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:#1f293d;border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:#2e3e5c}

.wrap{max-width:1220px;margin:0 auto;padding:0 24px 80px}
h1,h2,h3,.num{font-family:'Exo 2',sans-serif;letter-spacing:-0.01em}
button,input{font:inherit;color:inherit}
a{color:var(--blue);text-decoration:none;transition:color .15s}
a:hover{color:#7dd3fc;text-decoration:none}

/* CURRENCY ICONS */
.cur-icon{
 display:inline-block;
 vertical-align:-2.5px;
 width:16px;
 height:16px;
 object-fit:contain;
 margin-left:3px;
 filter:drop-shadow(0 1px 3px rgba(0,0,0,0.6));
}
.cur-icon.lg{
 width:20px;
 height:20px;
 vertical-align:-3px;
 margin-left:6px;
}
.cur-icon.hero{
 width:32px;
 height:32px;
 vertical-align:-5px;
 margin:0 12px 0 14px;
 filter:drop-shadow(0 0 12px rgba(56, 189, 248, 0.55));
}
.cur-icon.ducat{
 filter:drop-shadow(0 1px 4px rgba(245, 192, 66, 0.5));
}
.cur-box{
 display:inline-flex;
 align-items:center;
 font-family:'JetBrains Mono',monospace;
 font-weight:700;
}

/* HEADER */
.top{
 display:flex;
 align-items:center;
 justify-content:space-between;
 gap:20px;
 padding:26px 0 22px;
 border-bottom:1px solid rgba(255,255,255,0.06);
 margin-bottom:26px;
 flex-wrap:wrap;
}
.brand{display:flex;align-items:center;gap:14px}
.logo-box{
 position:relative;
 width:46px;
 height:46px;
 border-radius:12px;
 display:grid;
 place-items:center;
 background:linear-gradient(135deg, #1e190d 0%, #100d07 100%);
 border:1px solid rgba(245, 192, 66, 0.4);
 box-shadow:0 0 20px -3px rgba(245, 192, 66, 0.3), inset 0 1px 0 rgba(255, 255, 255, 0.15);
 color:var(--gold);
}
.brand-titles h1{
 font-size:24px;
 font-weight:800;
 line-height:1.15;
 text-transform:uppercase;
 letter-spacing:0.04em;
 display:flex;
 align-items:center;
 gap:8px;
}
.brand-titles h1 span.gold{
 background:var(--gold-gradient);
 -webkit-background-clip:text;
 -webkit-text-fill-color:transparent;
 filter:drop-shadow(0 2px 8px rgba(245, 192, 66, 0.25));
}
.brand-titles .meta{
 color:var(--soft);
 font-size:12.5px;
 margin-top:3px;
 display:flex;
 align-items:center;
 gap:8px;
 flex-wrap:wrap;
}
.pill-badge{
 display:inline-flex;
 align-items:center;
 gap:6px;
 padding:3px 9px;
 border-radius:6px;
 font-size:11.5px;
 font-weight:600;
 background:rgba(255,255,255,0.05);
 border:1px solid rgba(255,255,255,0.08);
 color:var(--soft);
}
.pill-badge.live{
 background:var(--good-dim);
 border-color:var(--good-border);
 color:var(--good-bright);
}
.pulse-dot{
 width:6px;
 height:6px;
 border-radius:50%;
 background:var(--good);
 box-shadow:0 0 8px var(--good);
 animation:pulse 2s infinite ease-in-out;
}
@keyframes pulse{
 0%,100%{transform:scale(1);opacity:1}
 50%{transform:scale(1.3);opacity:0.6}
}

.ghost{
 display:inline-flex;
 align-items:center;
 gap:8px;
 background:var(--card);
 border:1px solid var(--line);
 border-radius:var(--r-sm);
 padding:9px 16px;
 cursor:pointer;
 color:var(--soft);
 font-weight:600;
 font-size:13px;
 transition:all .2s;
 backdrop-filter:var(--blur);
}
.ghost:hover{
 border-color:var(--line-hover);
 color:var(--text);
 transform:translateY(-1px);
 background:var(--card-hover);
}

/* HERO SECTION */
.hero{
 display:grid;
 grid-template-columns:1.35fr 1fr;
 gap:16px;
 margin-bottom:26px;
}
.panel{
 background:var(--card);
 border:1px solid var(--line);
 border-radius:var(--r);
 padding:22px 24px;
 box-shadow:var(--shadow);
 backdrop-filter:var(--blur);
 -webkit-backdrop-filter:var(--blur);
 position:relative;
 overflow:hidden;
}
.panel::before{
 content:'';
 position:absolute;
 top:0;left:0;right:0;height:1px;
 background:linear-gradient(90deg, transparent, rgba(255,255,255,0.12), transparent);
}
.hero-main{
 display:flex;
 flex-direction:column;
 justify-content:space-between;
 background:linear-gradient(135deg, rgba(20, 27, 43, 0.85) 0%, rgba(13, 18, 29, 0.95) 100%);
 border:1px solid rgba(245, 192, 66, 0.2);
 box-shadow:var(--shadow), 0 0 35px -10px rgba(245, 192, 66, 0.12);
}
.hero-tag{
 display:inline-flex;
 align-items:center;
 gap:6px;
 font-size:11.5px;
 font-weight:700;
 text-transform:uppercase;
 letter-spacing:0.08em;
 color:var(--gold);
}
.hero .big{
 font:900 40px/1.1 'Exo 2',sans-serif;
 margin:10px 0 8px;
 display:flex;
 align-items:center;
 flex-wrap:wrap;
 background:linear-gradient(135deg, #ffffff 0%, #d1fae5 40%, #34d399 100%);
 -webkit-background-clip:text;
 -webkit-text-fill-color:transparent;
 filter:drop-shadow(0 2px 10px rgba(16, 185, 129, 0.25));
}
.hero .big span.plat-label{
 font-size:15px;
 font-weight:700;
 color:var(--soft);
 -webkit-text-fill-color:var(--soft);
 letter-spacing:0;
 margin-left:6px;
}
.hero p{
 margin:0;
 color:var(--soft);
 font-size:13.5px;
 line-height:1.6;
}
.hero-btn{
 margin-top:18px;
 display:inline-flex;
 align-items:center;
 gap:8px;
 background:var(--gold-gradient);
 color:#120e06;
 border:none;
 border-radius:10px;
 font-weight:700;
 font-size:14px;
 padding:11px 20px;
 cursor:pointer;
 box-shadow:0 4px 18px rgba(245, 192, 66, 0.35);
 transition:all .2s ease;
 align-self:flex-start;
}
.hero-btn:hover{
 transform:translateY(-2px);
 box-shadow:0 6px 24px rgba(245, 192, 66, 0.5);
 filter:brightness(1.06);
}
.hero-best-list{
 display:flex;
 flex-direction:column;
 gap:10px;
 margin-top:12px;
}
.best{
 display:flex;
 align-items:center;
 gap:14px;
 padding:10px 14px;
 border-radius:12px;
 background:rgba(255,255,255,0.025);
 border:1px solid rgba(255,255,255,0.05);
 cursor:pointer;
 transition:all .2s;
}
.best:hover{
 background:rgba(245, 192, 66, 0.05);
 border-color:var(--gold-border);
 transform:translateX(3px);
}
.best-rank{
 width:24px;
 height:24px;
 border-radius:7px;
 display:grid;
 place-items:center;
 font:800 12px 'Exo 2',sans-serif;
 background:rgba(255,255,255,0.06);
 color:var(--soft);
 flex-shrink:0;
}
.best:nth-child(1) .best-rank{background:var(--gold-dim);color:var(--gold);border:1px solid var(--gold-border)}
.best:nth-child(2) .best-rank{background:rgba(148, 163, 184, 0.15);color:#cbd5e1;border:1px solid rgba(148, 163, 184, 0.3)}
.best:nth-child(3) .best-rank{background:rgba(180, 83, 9, 0.2);color:#fdba74;border:1px solid rgba(180, 83, 9, 0.4)}
.best .bn{font-weight:700;font-size:14.5px;color:var(--text);transition:color .15s}
.best:hover .bn{color:var(--gold-bright)}
.best .ru{font-size:12px;color:var(--mute)}
.best .bp{
 margin-left:auto;
 font:800 20px 'Exo 2',sans-serif;
 color:var(--good-bright);
 display:inline-flex;
 align-items:center;
 text-shadow:0 0 16px rgba(16, 185, 129, 0.3);
}

/* STRATEGY MODES (TABS) */
.modes{
 display:grid;
 grid-template-columns:repeat(5,1fr);
 gap:12px;
 margin-bottom:18px;
}
.mode{
 background:var(--card);
 border:1px solid var(--line);
 border-radius:var(--r);
 padding:16px 14px 14px;
 cursor:pointer;
 text-align:left;
 position:relative;
 transition:all .2s ease;
 display:flex;
 flex-direction:column;
 backdrop-filter:var(--blur);
}
.mode:hover{
 border-color:var(--line-hover);
 background:var(--card-hover);
 transform:translateY(-2px);
}
.mode.on{
 border-color:var(--gold);
 background:linear-gradient(180deg, rgba(245, 192, 66, 0.12) 0%, rgba(15, 21, 33, 0.95) 70%);
 box-shadow:0 0 25px -4px rgba(245, 192, 66, 0.25), inset 0 2px 0 var(--gold);
}
.mode.on::after{display:none}
.mode-top-row{
 display:flex;
 align-items:center;
 justify-content:space-between;
 margin-bottom:10px;
}
.mode .ic-wrap{
 width:36px;
 height:36px;
 border-radius:10px;
 display:grid;
 place-items:center;
 background:rgba(255,255,255,0.05);
 border:1px solid rgba(255,255,255,0.08);
 color:var(--soft);
}
.mode.on .ic-wrap{
 background:var(--gold-dim);
 border-color:var(--gold-border);
 color:var(--gold-bright);
}
.mode-svg{width:20px;height:20px;stroke-width:2;stroke:currentColor;fill:none;stroke-linecap:round;stroke-linejoin:round}
.mode-cur-img{width:22px;height:22px;object-fit:contain}
.mode h3{
 margin:0 0 4px;
 font-size:15px;
 font-weight:700;
 color:var(--text);
}
.mode.on h3{color:var(--gold-bright)}
.mode .d{
 color:var(--soft);
 font-size:12px;
 line-height:1.4;
 min-height:34px;
 margin-bottom:8px;
}
.mode .c{
 margin-top:auto;
 font-size:11.5px;
 color:var(--mute);
 padding-top:6px;
 border-top:1px dashed rgba(255,255,255,0.06);
 display:flex;
 align-items:center;
 gap:4px;
 flex-wrap:wrap;
}
.mode-stat-cnt{
 color:var(--good-bright);
 font-weight:700;
 font-family:'JetBrains Mono',monospace;
}
.mode.on .mode-stat-cnt{color:var(--gold-bright)}
.mode-stat-p{
 display:inline-flex;
 align-items:center;
 font-weight:700;
 color:var(--good-bright);
 font-family:'JetBrains Mono',monospace;
}
.mode.on .mode-stat-p{color:var(--gold-bright)}

/* EXPLAINER */
.explain{
 background:linear-gradient(135deg, rgba(17, 24, 39, 0.7) 0%, rgba(11, 15, 25, 0.9) 100%);
 border:1px solid var(--line);
 border-left:3px solid var(--gold);
 border-radius:var(--r);
 padding:16px 22px;
 margin-bottom:16px;
 backdrop-filter:var(--blur);
 box-shadow:0 6px 24px rgba(0,0,0,0.25);
}
.explain-inner{
 display:flex;
 flex-direction:column;
 gap:10px;
}
.flow-header{
 display:flex;
 align-items:center;
 gap:12px;
 flex-wrap:wrap;
}
.flow-label{
 font-size:10.5px;
 font-weight:800;
 text-transform:uppercase;
 letter-spacing:0.08em;
 color:var(--gold);
 background:var(--gold-dim);
 border:1px solid var(--gold-border);
 padding:3px 8px;
 border-radius:6px;
 flex-shrink:0;
}
.explain .flow{
 display:flex;
 gap:8px;
 align-items:center;
 flex-wrap:wrap;
}
.flow-step{
 display:inline-flex;
 align-items:center;
 gap:6px;
 background:rgba(255,255,255,0.04);
 border:1px solid rgba(255,255,255,0.09);
 padding:4px 11px;
 border-radius:8px;
 font-size:12.5px;
 font-weight:600;
 color:var(--text);
}
.step-num{
 width:18px;
 height:18px;
 border-radius:50%;
 background:rgba(245, 192, 66, 0.15);
 color:var(--gold);
 display:grid;
 place-items:center;
 font:800 10.5px 'Exo 2',sans-serif;
}
.flow-arrow{
 color:var(--gold);
 opacity:0.6;
 display:grid;
 place-items:center;
}
.explain-text{
 margin:0;
 color:var(--soft);
 font-size:13px;
 line-height:1.55;
}
.explain-text b{
 color:var(--text);
 font-weight:600;
}

/* TOOLBAR & SEARCH */
.bar{
 display:flex;
 gap:10px;
 align-items:center;
 flex-wrap:wrap;
 margin-bottom:14px;
}
.search{
 flex:1 1 240px;
 position:relative;
 min-width:200px;
}
.search input{
 width:100%;
 background:var(--card);
 border:1px solid var(--line);
 border-radius:12px;
 padding:10px 16px 10px 38px;
 color:var(--text);
 font-size:13px;
 outline:none;
 transition:all .2s;
 backdrop-filter:var(--blur);
}
.search input:focus{
 border-color:var(--gold);
 background:rgba(24, 33, 52, 0.85);
 box-shadow:0 0 0 3px rgba(245, 192, 66, 0.15);
}
.search input::placeholder{color:var(--mute)}
.search-icon{
 position:absolute;
 left:13px;
 top:50%;
 transform:translateY(-50%);
 color:var(--mute);
 pointer-events:none;
 display:grid;
 place-items:center;
}
.seg{flex-wrap:wrap;max-width:100%;
 display:inline-flex;
 align-items:center;
 background:var(--card);
 border:1px solid var(--line);
 border-radius:12px;
 padding:3px;
 gap:2px;
 backdrop-filter:var(--blur);
 flex-shrink:0;
}
.seg .t{
 padding:5px 8px;
 color:var(--mute);
 font-size:11px;
 font-weight:700;
 text-transform:uppercase;
 letter-spacing:0.04em;
}
.seg button{
 white-space:nowrap;
 background:none;
 border:0;
 padding:6px 11px;
 border-radius:8px;
 cursor:pointer;
 color:var(--soft);
 font-size:12px;
 font-weight:600;
 transition:all .15s;
 display:inline-flex;
 align-items:center;
 gap:4px;
}
.seg button:hover{
 color:var(--text);
 background:rgba(255,255,255,0.05);
}
.seg button.on{
 background:rgba(255,255,255,0.1);
 color:var(--text);
 box-shadow:0 2px 6px rgba(0,0,0,0.2), inset 0 0 0 1px rgba(255,255,255,0.15);
}
.btn-toggle{
 display:inline-flex;
 align-items:center;
 gap:6px;
 background:var(--card);
 border:1px solid var(--line);
 border-radius:12px;
 padding:7px 13px;
 cursor:pointer;
 color:var(--soft);
 font-size:12px;
 font-weight:600;
 transition:all .2s;
 backdrop-filter:var(--blur);
 flex-shrink:0;
}
.btn-toggle:hover{
 border-color:var(--line-hover);
 color:var(--text);
 background:var(--card-hover);
}
.btn-toggle.on{
 background:var(--good-dim);
 border-color:var(--good-border);
 color:var(--good-bright);
 box-shadow:0 0 15px -3px rgba(16, 185, 129, 0.3);
}
.btn-toggle svg{
 color:var(--gold);
 transition:color .2s;
}
.btn-toggle.on svg{
 color:var(--good-bright);
}
.count{
 color:var(--soft);
 font-size:12.5px;
 margin:0 0 14px;
 display:flex;
 align-items:center;
 gap:6px;
}
.count b{color:var(--text);font-weight:700}

/* DEAL CARDS */
.list{
 display:flex;
 flex-direction:column;
 gap:10px;
}
.deal{
 background:var(--card);
 border:1px solid var(--line);
 border-radius:var(--r);
 overflow:hidden;
 transition:all .2s ease;
 box-shadow:0 4px 18px rgba(0,0,0,0.25);
 backdrop-filter:var(--blur);
}
.deal:hover{
 border-color:var(--line-hover);
 transform:translateY(-1px);
 box-shadow:0 8px 26px rgba(0,0,0,0.35);
}
.deal.open{
 border-color:rgba(245, 192, 66, 0.4);
 box-shadow:0 10px 30px rgba(0,0,0,0.4), 0 0 20px -5px rgba(245, 192, 66, 0.15);
}
.row{
 display:grid;
 grid-template-columns:58px minmax(0,1fr) auto auto 26px;
 gap:16px;
 align-items:center;
 padding:14px 18px;
 cursor:pointer;
 user-select:none;
}
.img{
 width:58px;
 height:58px;
 border-radius:12px;
 background:var(--bg-elevated) center/contain no-repeat;
 border:1px solid rgba(255,255,255,0.09);
 box-shadow:inset 0 0 12px rgba(0,0,0,0.6);
 transition:transform .2s;
 flex-shrink:0;
}
.deal:hover .img{transform:scale(1.04)}
.img.sm{width:36px;height:36px;border-radius:8px}
.nm{
 font-weight:700;
 font-size:16px;
 white-space:nowrap;
 overflow:hidden;
 text-overflow:ellipsis;
 color:var(--text);
}
.nm a.il{
 color:var(--text);
 transition:color .15s;
}
.deal:hover .nm a.il{color:var(--gold-bright)}
.ru{
 color:var(--soft);
 font-size:12.5px;
 white-space:nowrap;
 overflow:hidden;
 text-overflow:ellipsis;
 margin-top:1px;
}
.tags{
 display:flex;
 gap:6px;
 flex-wrap:wrap;
 margin-top:6px;
}
.tag{
 font-size:11.5px;
 font-weight:600;
 padding:3px 9px;
 border-radius:20px;
 background:rgba(255,255,255,0.04);
 border:1px solid rgba(255,255,255,0.08);
 color:var(--soft);
 white-space:nowrap;
 display:inline-flex;
 align-items:center;
 gap:5px;
}
.tag-svg{width:12px;height:12px;flex-shrink:0}
.tag.g{
 color:var(--good-bright);
 border-color:var(--good-border);
 background:var(--good-dim);
}
.tag.w{
 color:var(--warn);
 border-color:rgba(251, 191, 36, 0.3);
 background:var(--warn-dim);
}
.money{
 display:flex;
 align-items:center;
 gap:10px;
 font-variant-numeric:tabular-nums;
 white-space:nowrap;
 background:rgba(0,0,0,0.25);
 border:1px solid rgba(255,255,255,0.05);
 padding:6px 14px;
 border-radius:10px;
}
.money .a{
 color:var(--mute);
 font-size:11px;
 display:block;
 line-height:1;
 margin-bottom:3px;
 text-transform:uppercase;
 letter-spacing:0.04em;
 font-weight:600;
}
.money .v{
 font:700 15px 'JetBrains Mono',monospace;
 color:var(--text);
}
.money .arr{
 color:var(--gold);
 font-size:16px;
 opacity:0.8;
}
.profit{
 text-align:right;
 min-width:96px;
}
.profit .v{
 font:800 25px/1 'Exo 2',sans-serif;
 color:var(--good-bright);
 text-shadow:0 0 16px rgba(16, 185, 129, 0.35);
 display:inline-flex;
 align-items:center;
}
.profit .v.neg{
 color:var(--bad);
 text-shadow:0 0 16px rgba(244, 63, 94, 0.35);
}
.profit .s{
 color:var(--mute);
 font-size:11.5px;
 font-weight:600;
 text-transform:uppercase;
 letter-spacing:0.04em;
 margin-top:2px;
}
.chev{
 width:26px;
 height:26px;
 border-radius:50%;
 display:grid;
 place-items:center;
 color:var(--mute);
 font-size:16px;
 background:rgba(255,255,255,0.03);
 transition:all .2s;
}
.deal:hover .chev{color:var(--text);background:rgba(255,255,255,0.08)}
.deal.open .chev{
 transform:rotate(90deg);
 color:var(--gold);
 background:var(--gold-dim);
}
.nm .ext{
 font-size:.8em;
 color:var(--mute);
 opacity:0;
 margin-left:4px;
 transition:opacity .15s;
}
.row:hover .nm .ext{opacity:1}

/* STEPS ACCORDION */
.steps{
 display:none;
 border-top:1px solid rgba(255,255,255,0.08);
 background:rgba(8, 12, 19, 0.7);
 padding:18px 22px 20px;
 animation:fadeDown .2s ease-out;
}
@keyframes fadeDown{
 from{opacity:0;transform:translateY(-6px)}
 to{opacity:1;transform:translateY(0)}
}
.deal.open .steps{display:block}
.steps h4{
 margin:0 0 12px;
 font:700 12px 'Exo 2',sans-serif;
 text-transform:uppercase;
 letter-spacing:0.08em;
 color:var(--gold);
 display:flex;
 align-items:center;
 gap:6px;
}
.step{
 display:grid;
 grid-template-columns:28px 36px minmax(0,1fr) auto;
 gap:14px;
 align-items:center;
 padding:10px 0;
 border-bottom:1px dashed rgba(255,255,255,0.06);
 transition:opacity .2s;
}
.step:last-child{border-bottom:0}
.step.done{opacity:.45}
.step.done .st{text-decoration:line-through;color:var(--mute)}
.ck{
 width:24px;
 height:24px;
 border-radius:7px;
 border:1.5px solid rgba(255,255,255,0.2);
 background:rgba(255,255,255,0.03);
 cursor:pointer;
 display:grid;
 place-items:center;
 color:transparent;
 transition:all .15s;
}
.ck-svg{width:14px;height:14px;stroke:currentColor;stroke-width:2.5;fill:none;stroke-linecap:round;stroke-linejoin:round}
.ck:hover{
 border-color:var(--good-bright);
 background:rgba(16, 185, 129, 0.1);
}
.step.done .ck{
 background:var(--good);
 border-color:var(--good);
 color:#04150c;
 box-shadow:0 0 10px rgba(16, 185, 129, 0.4);
}
.st{
 color:var(--soft);
 font-size:13.5px;
 line-height:1.45;
}
.st b{color:var(--text);font-weight:700}
.who{
 display:inline-flex;
 align-items:center;
 gap:6px;
 color:var(--text);
 font-weight:600;
 text-decoration:none;
 transition:color .15s;
 vertical-align:middle;
 min-width:0;
 flex-shrink:1;
}
.who:hover{
 color:var(--gold-bright);
}
.st .who{
 padding:2px 8px;
 background:rgba(255,255,255,0.05);
 border-radius:6px;
 flex-shrink:0;
}
.who-name{
 overflow:hidden;
 text-overflow:ellipsis;
 white-space:nowrap;
 min-width:0;
}
.dot{
 width:8px;
 height:8px;
 border-radius:50%;
 background:var(--mute);
 display:inline-block;
 flex-shrink:0;
}
.dot.ingame{
 background:var(--good);
 box-shadow:0 0 8px var(--good);
}
.dot.online{
 background:var(--blue);
 box-shadow:0 0 8px var(--blue);
}
.act{display:flex;gap:8px}
.btn{
 background:rgba(255,255,255,0.06);
 border:1px solid rgba(255,255,255,0.12);
 border-radius:9px;
 padding:7px 13px;
 cursor:pointer;
 font-size:12.5px;
 font-weight:600;
 white-space:nowrap;
 color:var(--text);
 transition:all .15s;
}
.btn:hover{
 border-color:var(--line-hover);
 background:rgba(255,255,255,0.12);
 transform:translateY(-1px);
}
.btn.gold{
 background:var(--gold-gradient);
 color:#120e06;
 border:none;
 font-weight:700;
 box-shadow:0 3px 12px rgba(245, 192, 66, 0.25);
}
.btn.gold:hover{
 filter:brightness(1.1);
 box-shadow:0 4px 16px rgba(245, 192, 66, 0.4);
}
.note{
 margin-top:14px;
 padding:10px 14px;
 background:rgba(255,255,255,0.025);
 border-radius:10px;
 border:1px solid rgba(255,255,255,0.05);
 color:var(--mute);
 font-size:12.5px;
}

/* COMPACT GRID (DUCATS / SELL) */
.grid{
 display:grid;
 grid-template-columns:repeat(auto-fill,minmax(290px,1fr));
 gap:12px;
}
.mini{
 background:var(--card);
 border:1px solid var(--line);
 border-radius:var(--r);
 padding:14px;
 display:grid;
 grid-template-columns:48px minmax(0,1fr) auto;
 gap:12px;
 align-items:center;
 backdrop-filter:var(--blur);
 transition:all .2s;
}
.mini:hover{
 border-color:var(--line-hover);
 transform:translateY(-1px);
 box-shadow:0 6px 20px rgba(0,0,0,0.3);
}
.mini .img{width:48px;height:48px}
.mini .k{text-align:right}
.mini .k .v{
 font:800 22px/1 'Exo 2',sans-serif;
 color:var(--gold-bright);
 display:inline-flex;
 align-items:center;
}
.mini .k .s{color:var(--mute);font-size:11px;font-weight:600}
.mini .foot{
 grid-column:1/-1;
 display:flex;
 justify-content:space-between;
 align-items:center;
 gap:10px;
 border-top:1px dashed rgba(255,255,255,0.07);
 padding-top:10px;
 font-size:12.5px;
 color:var(--soft);
 min-width:0;
}
.mini .foot-buyer,
.mini .foot-seller{
 display:flex;
 align-items:center;
 gap:6px;
 min-width:0;
 flex:1 1 auto;
 overflow:hidden;
 font-size:12px;
 white-space:nowrap;
}
.mini .foot-qty{
 color:var(--mute);
 flex-shrink:0;
 white-space:nowrap;
 font-size:11.5px;
}
.mini .foot .btn{
 flex-shrink:0;
}
.tag-set{
 display:inline-flex;
 align-items:center;
 gap:4px;
 padding:2px 8px;
 border-radius:6px;
 background:rgba(56, 189, 248, 0.1);
 border:1px solid rgba(56, 189, 248, 0.25);
 color:var(--blue);
 font-size:11.5px;
 font-weight:600;
}
.verdict{
 font-size:11px;
 font-weight:700;
 padding:2px 8px;
 border-radius:6px;
 white-space:nowrap;
}
.verdict.sell{
 background:var(--good-dim);
 color:var(--good-bright);
 border:1px solid var(--good-border);
}
.verdict.baro{
 background:var(--gold-dim);
 color:var(--gold-bright);
 border:1px solid var(--gold-border);
}

/* BUNDLE CARDS (BARO PACKS) */
.bundle-card{
 background:var(--card);
 border:1px solid var(--line);
 border-radius:var(--r);
 padding:16px;
 display:flex;
 flex-direction:column;
 gap:12px;
 backdrop-filter:var(--blur);
 transition:all .2s;
}
.bundle-card:hover{
 border-color:var(--line-hover);
 transform:translateY(-2px);
 box-shadow:0 8px 24px rgba(0,0,0,0.35);
}
.bundle-head{
 display:flex;
 justify-content:space-between;
 align-items:flex-start;
 gap:10px;
}
.bundle-seller-box{
 display:flex;
 flex-direction:column;
 gap:5px;
 min-width:0;
}
.bundle-seller{font-weight:700;font-size:15px}
.bundle-pills{
 display:flex;
 align-items:center;
 gap:6px;
 flex-wrap:wrap;
 font-size:12px;
}
.bundle-pill{
 display:inline-flex;
 align-items:center;
 gap:4px;
 padding:2px 8px;
 background:rgba(255,255,255,0.04);
 border:1px solid rgba(255,255,255,0.08);
 border-radius:6px;
 font-weight:600;
 color:var(--text);
}
.bundle-pill.duc{color:var(--gold-bright)}
.bundle-pill.plat{color:var(--blue)}
.bundle-rate-badge{
 text-align:right;
 flex-shrink:0;
 background:rgba(245, 192, 66, 0.08);
 border:1px solid rgba(245, 192, 66, 0.25);
 border-radius:10px;
 padding:4px 10px;
}
.bundle-rate-badge .val{
 font:800 20px/1 'Exo 2',sans-serif;
 color:var(--gold-bright);
}
.bundle-rate-badge .lbl{
 font-size:11px;
 color:var(--gold);
 font-weight:600;
}
.bundle-items-list{
 display:flex;
 flex-direction:column;
 gap:5px;
 background:rgba(0,0,0,0.25);
 border:1px solid rgba(255,255,255,0.04);
 border-radius:10px;
 padding:10px 12px;
}
.bundle-item-row{
 display:flex;
 align-items:center;
 justify-content:space-between;
 gap:8px;
 font-size:12.5px;
}
.bir-left{
 display:flex;
 align-items:center;
 gap:6px;
 min-width:0;
 overflow:hidden;
}
.bir-qty{
 color:var(--gold-bright);
 font-weight:700;
 font-family:'JetBrains Mono',monospace;
 font-size:12px;
 flex-shrink:0;
}
.bir-name{
 white-space:nowrap;
 overflow:hidden;
 text-overflow:ellipsis;
 color:var(--text);
}
.bir-right{
 display:flex;
 align-items:center;
 gap:6px;
 flex-shrink:0;
 font-size:12px;
 font-family:'JetBrains Mono',monospace;
}
.bundle-action-btn{
 margin-top:2px;
 width:100%;
 padding:9px 14px;
 font-size:13px;
 border-radius:9px;
 display:flex;
 align-items:center;
 justify-content:center;
 gap:6px;
}

.sec{
 font:800 18px 'Exo 2',sans-serif;
 margin:26px 0 6px;
 display:flex;
 align-items:center;
 gap:10px;
}
.sec span{
 font:700 12px 'Plus Jakarta Sans',sans-serif;
 color:var(--gold);
 background:var(--gold-dim);
 border:1px solid var(--gold-border);
 padding:2px 9px;
 border-radius:12px;
}
.rates{margin-bottom:12px}
.rates .lbl{
 color:var(--gold);
 font-size:12px;
 font-weight:700;
 text-transform:uppercase;
 letter-spacing:0.08em;
 margin-bottom:12px;
}
.rgrid{
 display:grid;
 grid-template-columns:repeat(5,1fr);
 gap:10px;
}
.rate{
 background:rgba(12, 17, 28, 0.7);
 border:1px solid var(--line);
 border-radius:12px;
 padding:12px;
 backdrop-filter:var(--blur);
}
.rate .rd{
 font:800 24px 'Exo 2',sans-serif;
 color:var(--gold-bright);
 display:flex;
 align-items:center;
 gap:4px;
}
.rate .rd span{font-size:13px;color:var(--mute);font-weight:500}
.rate .rl{font-size:12.5px;color:var(--soft);margin-top:2px}
.rate .rl b{color:var(--text)}
.rin{
 display:flex;
 align-items:center;
 gap:6px;
 font-size:12px;
 color:var(--soft);
 margin:6px 0;
}
.rin input{
 width:54px;
 background:rgba(0,0,0,0.3);
 border:1px solid rgba(255,255,255,0.15);
 border-radius:6px;
 color:var(--gold-bright);
 font:700 14px 'JetBrains Mono',monospace;
 padding:4px 6px;
 text-align:center;
 outline:none;
}
.rin input:focus{border-color:var(--gold);box-shadow:0 0 8px rgba(245, 192, 66, 0.3)}

.more{
 display:block;
 margin:20px auto 0;
 padding:12px 28px;
 font-size:14px;
}
.empty{
 padding:50px 20px;
 text-align:center;
 color:var(--soft);
 border:1px dashed var(--line);
 border-radius:var(--r);
 background:rgba(255,255,255,0.015);
}

/* HELP PANEL */
.help{
 display:none;
 margin-bottom:24px;
 background:linear-gradient(135deg, rgba(18, 26, 44, 0.95) 0%, rgba(10, 14, 24, 0.98) 100%);
 border:1px solid rgba(245, 192, 66, 0.3);
 border-radius:var(--r);
 box-shadow:var(--shadow), 0 0 35px -8px rgba(245, 192, 66, 0.2);
 padding:22px 24px;
 position:relative;
 backdrop-filter:var(--blur);
 animation:helpFadeIn .22s ease-out;
}
@keyframes helpFadeIn{
 from{opacity:0;transform:translateY(-6px)}
 to{opacity:1;transform:translateY(0)}
}
.help.on{display:block}
.help-header{
 display:flex;
 justify-content:space-between;
 align-items:center;
 margin-bottom:18px;
 padding-bottom:14px;
 border-bottom:1px solid rgba(255,255,255,0.06);
}
.help-title-box{
 display:flex;
 align-items:center;
 gap:10px;
}
.help-title-icon{
 width:32px;
 height:32px;
 border-radius:8px;
 background:var(--gold-dim);
 border:1px solid var(--gold-border);
 color:var(--gold);
 display:grid;
 place-items:center;
}
.help-header h2{
 margin:0;
 font:800 17px 'Exo 2',sans-serif;
 color:var(--text);
}
.help-close-btn{
 width:28px;
 height:28px;
 border-radius:8px;
 background:rgba(255,255,255,0.05);
 border:1px solid rgba(255,255,255,0.1);
 color:var(--mute);
 cursor:pointer;
 display:grid;
 place-items:center;
 font-size:15px;
 transition:all .15s;
}
.help-close-btn:hover{
 color:var(--text);
 background:rgba(255,255,255,0.12);
}
.help-section-lbl{
 font-size:11px;
 font-weight:800;
 text-transform:uppercase;
 letter-spacing:0.08em;
 color:var(--gold);
 margin-bottom:10px;
}
.help .cols{
 display:grid;
 grid-template-columns:repeat(3,1fr);
 gap:14px;
}
.help-card{
 background:rgba(255,255,255,0.025);
 border:1px solid rgba(255,255,255,0.06);
 border-radius:12px;
 padding:15px 16px;
 transition:all .2s;
 display:flex;
 flex-direction:column;
 gap:6px;
}
.help-card:hover{
 background:rgba(255,255,255,0.04);
 border-color:rgba(255,255,255,0.12);
 transform:translateY(-1px);
}
.help-card-head{
 display:flex;
 align-items:center;
 gap:8px;
 margin-bottom:4px;
}
.help-badge{
 font:800 11px/1 'JetBrains Mono',monospace;
 padding:3px 7px;
 border-radius:6px;
 background:var(--gold-dim);
 border:1px solid var(--gold-border);
 color:var(--gold-bright);
}
.help-badge.feature{
 background:rgba(56, 189, 248, 0.1);
 border-color:rgba(56, 189, 248, 0.25);
 color:var(--blue);
}
.help-card h3{
 margin:0;
 font:700 14px 'Exo 2',sans-serif;
 color:var(--text);
}
.help-card p{
 margin:0;
 font-size:12.5px;
 color:var(--soft);
 line-height:1.55;
}
.help-card p b{color:var(--text)}
.help-status-list{
 list-style:none;
 padding:0;
 margin:0;
 display:flex;
 flex-direction:column;
 gap:6px;
}
.help-status-list li{
 display:flex;
 align-items:center;
 gap:8px;
 font-size:12.5px;
 color:var(--soft);
}
.help-status-list li b{color:var(--text)}

/* TOAST */
.toast{
 position:fixed;
 left:50%;
 bottom:28px;
 transform:translate(-50%, 25px);
 opacity:0;
 background:#121a2b;
 border:1px solid rgba(16, 185, 129, 0.4);
 padding:12px 22px;
 border-radius:14px;
 box-shadow:0 12px 40px rgba(0,0,0,0.6), 0 0 25px rgba(16, 185, 129, 0.2);
 transition:all .25s cubic-bezier(0.16, 1, 0.3, 1);
 pointer-events:none;
 font-size:14px;
 font-weight:600;
 z-index:9999;
 display:flex;
 align-items:center;
 gap:10px;
}
.toast.on{
 opacity:1;
 transform:translate(-50%, 0);
}
.toast b{color:var(--good-bright)}

@media (max-width:1080px){
 .modes{grid-template-columns:repeat(3,1fr)}
 .hero{grid-template-columns:1fr}
 .help .cols{grid-template-columns:1fr}
}
@media (max-width:720px){
 .wrap{padding:0 14px 60px}
 .modes{grid-template-columns:1fr 1fr}
 .rgrid{grid-template-columns:1fr 1fr}
 .row{grid-template-columns:46px minmax(0,1fr) auto;gap:12px;padding:12px}
 .row .money,.row .chev{display:none}
 .img{width:46px;height:46px}
 .step{grid-template-columns:24px minmax(0,1fr);gap:10px}
 .step .img{display:none}
 .step .act{grid-column:2}
 .hero .big{font-size:30px}
}
</style></head><body>
<div class="wrap">
 <header class="top">
  <div class="brand">
   <div class="logo-box" title="Warframe Market Profit">
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
     <polygon points="12 2 21 8.5 21 15.5 12 22 3 15.5 3 8.5 12 2"></polygon>
     <line x1="12" y1="2" x2="12" y2="22"></line>
     <line x1="3" y1="8.5" x2="21" y2="15.5"></line>
     <line x1="3" y1="15.5" x2="21" y2="8.5"></line>
    </svg>
   </div>
   <div class="brand-titles">
    <h1>WFM <span class="gold">PROFIT</span></h1>
    <div class="meta">
     <span class="pill-badge live"><span class="pulse-dot"></span> LIVE</span>
     <span id="upd" data-ts="__TS__">__META__</span>
     <span>__META2__</span>
    </div>
   </div>
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
   __ADMIN__
   <button class="ghost" id="helpBtn">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="10"></circle><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"></path><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>
    Инструкция
   </button>
  </div>
 </header>

 <div class="panel help" id="help">
  <div class="help-header">
   <div class="help-title-box">
    <div class="help-title-icon">
     <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="10"></circle><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"></path><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>
    </div>
    <h2>Инструкция: как зарабатывать платину</h2>
   </div>
   <button class="help-close-btn" id="helpClose" title="Закрыть инструкцию">✕</button>
  </div>

  <div class="help-section-lbl">3 простых шага к первой сделке</div>
  <div class="cols">
   <div class="help-card">
    <div class="help-card-head"><span class="help-badge">01</span><h3>Выбери способ</h3></div>
    <p>Начни со <b>«Сборки сетов»</b> — самый простой и надёжный доход. Покупаешь отдельные части у разных продавцов и продаёшь полный сет одним лотом.</p>
   </div>
   <div class="help-card">
    <div class="help-card-head"><span class="help-badge">02</span><h3>Открой сделку</h3></div>
    <p>Кликни на карточку — откроется пошаговый план: у кого купить, почём и кому продать. Нажми <b>«Написать»</b>, открой чат в игре и нажми <b>Ctrl+V</b>.</p>
   </div>
   <div class="help-card">
    <div class="help-card-head"><span class="help-badge">03</span><h3>Отмечай прогресс</h3></div>
    <p>Ставь галочки по мере покупок. Цены обновляются онлайн. Если игрок не отвечает — кликни на предмет и напиши следующему в списке.</p>
   </div>
  </div>

  <div class="help-section-lbl" style="margin-top:16px">Обозначения и полезные фишки</div>
  <div class="cols">
   <div class="help-card">
    <div class="help-card-head"><span class="help-badge feature">Статус</span><h3>Статусы игроков</h3></div>
    <ul class="help-status-list">
     <li><span class="dot ingame"></span> <b>В игре</b> — игрок прямо сейчас на миссии или в орбитере</li>
     <li><span class="dot online"></span> <b>На сайте</b> — авторизован на маркете, может быть AFK</li>
    </ul>
   </div>
   <div class="help-card">
    <div class="help-card-head"><span class="help-badge feature">Флип</span><h3>Продать сразу</h3></div>
    <p>Есть активный ордер покупателя, готового забрать сет прямо сейчас дороже твоих затрат на части. Прибыль моментальная, без ожидания.</p>
   </div>
   <div class="help-card">
    <div class="help-card-head"><span class="help-badge feature">Рынок</span><h3>Реальные продажи</h3></div>
    <p>Статистика фактических закрытых сделок. Если сет берут чаще 3–5 шт./день — продашь быстро. Если реже 1 шт./день — потребуется терпение.</p>
   </div>
  </div>
 </div>

 <section class="hero">
  <div class="panel hero-main">
   <div>
    <div class="hero-tag">
     <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>
     Прибыль прямо сейчас
    </div>
    <div class="big" id="heroSum"></div>
    <p id="heroTxt"></p>
   </div>
   <button class="hero-btn" id="heroGo">
    <span>Открыть лучшую сделку</span>
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="5" y1="12" x2="19" y2="12"></line><polyline points="12 5 19 12 12 19"></polyline></svg>
   </button>
  </div>
  <div class="panel" id="heroBest"></div>
 </section>

 <nav class="modes" id="modes"></nav>
 <div class="explain" id="explain"></div>
 <div class="bar" id="bar"></div>
 <div class="count" id="count"></div>
 <div id="list"></div>
</div>
<div class="toast" id="toast"></div>

<script>
const D=__DATA__, C=__CATALOG__;
const PLAT_ICON = "__PLAT_ICON__";
const DUCAT_ICON = "__DUCAT_ICON__";
const IMG="https://warframe.market/static/assets/", WFM="https://warframe.market/items/";
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const en=sl=>(C[sl]||{}).en||sl, ru=sl=>(C[sl]||{}).ru||"";
const itemUrl=sl=>WFM+encodeURIComponent(sl);
const ext=`target="_blank" rel="noopener"`;
const img=(sl,cls="")=>{const i=(C[sl]||{}).img;return `<a class="img ${cls}" href="${itemUrl(sl)}" ${ext} title="Открыть ${esc(en(sl))} на warframe.market" style="${i?`background-image:url('${IMG}${i}')`:""}"></a>`};
const nmL=sl=>`<a class="il" href="${itemUrl(sl)}" ${ext} title="Открыть на warframe.market"><b>${esc(en(sl))}</b></a>`;
const pl=v=>`<span class="cur-box">${v}<img class="cur-icon plat" src="${PLAT_ICON}" alt="p"></span>`;
const duc=v=>`<span class="cur-box">${v}<img class="cur-icon ducat" src="${DUCAT_ICON}" alt="дук."></span>`;
const sign=v=>(v>0?"+":"")+v;
const store={get(k,d){try{const v=localStorage.getItem("wfm_"+k);return v==null?d:JSON.parse(v)}catch(_){return d}},
             set(k,v){try{localStorage.setItem("wfm_"+k,JSON.stringify(v))}catch(_){}}};
const who=o=>o?`<a class="who il" href="https://warframe.market/profile/${encodeURIComponent(o.n)}" ${ext} title="Профиль игрока на warframe.market"><span class="dot ${esc(o.s)}"></span><span class="who-name">${esc(o.n)}</span></a>`:"—";
const plural=(n,a,b,c)=>{n=Math.abs(n)%100;const m=n%10;return n>10&&n<20?c:m>1&&m<5?b:m==1?a:c};

function wmsg(kind,o,sl,price,n){
 if(!o||!o.n)return "";
 if(n>1)return `/w ${o.n} Hi! I want to ${kind}: ${n}x "${en(sl)}" for ${price} platinum each (${n*price} total). (warframe.market)`;
 return `/w ${o.n} Hi! I want to ${kind}: "${en(sl)}" for ${price} platinum. (warframe.market)`}
function wbtn(kind,o,sl,price,label,n){const m=wmsg(kind,o,sl,price,n);if(!m)return "";
 return `<button class="btn ${label?"gold":""}" data-copy="${esc(m)}">${label||"Написать"}</button>`}
const mkt=sl=>`<a class="btn" href="${itemUrl(sl)}" ${ext}>На сайте ↗</a>`;

let toastT;
function toast(h){
 const t=$("#toast");
 t.innerHTML=`<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2.5"><polyline points="20 6 9 17 4 12"></polyline></svg><div>${h}</div>`;
 t.classList.add("on");
 clearTimeout(toastT);
 toastT=setTimeout(()=>t.classList.remove("on"),2600);
}
function copy(t){
 const ok=()=>toast("<b>Скопировано!</b> Открой чат в игре и нажми <b>Ctrl+V</b>");
 if(navigator.clipboard&&window.isSecureContext!==false){navigator.clipboard.writeText(t).then(ok,()=>fb())}else fb();
 function fb(){const a=document.createElement("textarea");a.value=t;document.body.appendChild(a);a.select();try{document.execCommand("copy");ok()}catch(_){prompt("Скопируй:",t)}a.remove()}}

/* ---------- РЕЖИМЫ (БЕЗ ЭМОДЗИ, ВЕКТОРНЫЕ ИКОНКИ) ---------- */
const MODES=[
 {id:"assemble",
  ic:`<svg viewBox="0 0 24 24" class="mode-svg"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>`,
  t:"Сборка сетов",d:"Купи части по отдельности → продай готовым сетом",
  flow:["Купить части","Собрать комплект","Продать сет"],
  ex:"Многие продают части по одной дешевле, а сет целиком покупают дороже. Собираешь детали у разных продавцов и продаёшь их одним лотом как готовый сет. <b>Самый простой и надёжный способ.</b>"},
 {id:"disassemble",
  ic:`<svg viewBox="0 0 24 24" class="mode-svg"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/></svg>`,
  t:"Разбор сетов",d:"Купи сет целиком → продай отдельные части",
  flow:["Купить сет целиком","Продать части поштучно"],
  ex:"Иногда редкие части по отдельности стоят заметно дороже сета. Покупаешь сет со скидкой и продаёшь детали разным покупателям."},
 {id:"flips",
  ic:`<svg viewBox="0 0 24 24" class="mode-svg"><path d="M7 16V4m0 0L3 8m4-4l4 4m6 4v12m0 0l4-4m-4 4l-4-4"/></svg>`,
  t:"Перепродажа",d:"Выстави ордер на покупку → продай дороже",
  flow:["Ордер на покупку","Ждёшь продавца","Продаёшь с наценкой"],
  ex:"Ставишь на сайте заказ на покупку чуть выше других ордеров. Продавцы пишут тебе сами. Затем выставляешь лот чуть дешевле рынка."},
 {id:"ducats",
  ic:`<img class="mode-cur-img" src="${DUCAT_ICON}" alt="Дукаты">`,
  t:"Дукаты для Баро",d:"Скупи хлам за платину → сдай за дукаты",
  flow:["Купить хлам по курсу чата","Сдать в Реле","Купить редкие товары Баро"],
  ex:"Дукаты нужны для покупок у Баро Ки'Тиира. Ориентиры курса: 45 дукатов ≈ 2p, 100 дукатов ≈ 6p. Ниже — самые выгодные предложения."},
 {id:"sell",
  ic:`<svg viewBox="0 0 24 24" class="mode-svg"><path d="M20 12V8H6a2 2 0 0 1-2-2c0-1.1.9-2 2-2h12v4"/><path d="M4 6v12a2 2 0 0 0 2 2h14v-4"/><circle cx="18" cy="12" r="2"/></svg>`,
  t:"Продать своё",d:"Узнай лучшую цену продажи своих предметов",
  flow:["Введи предмет","Узнай цену","Напиши покупателю"],
  ex:"Введи название того, что лежит в инвентаре. Увидишь, кто прямо сейчас покупает дороже всех, и стоит ли сдавать предмет Баро или продать за платину."},
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
  if(m.id==="sell")c=`<span class="mode-stat-cnt">${D.sell.length}</span> с покупателями`;
  else if(m.id==="ducats")c=`<span class="mode-stat-cnt">${g.length}</span> выгодных лотов`;
  else c=g.length?`<span class="mode-stat-cnt">${g.length}</span> в плюс · до <span class="mode-stat-p">+${g[0]?g[0].profit:0}<img class="cur-icon plat" src="${PLAT_ICON}" alt="p"></span>`:`<span class="mute">сейчас пусто</span>`;
  return `<button class="mode ${m.id===mode?"on":""}" data-mode="${m.id}">
   <div class="mode-top-row"><div class="ic-wrap">${m.ic}</div></div>
   <h3>${m.t}</h3>
   <div class="d">${m.d}</div>
   <div class="c">${c}</div>
  </button>`}).join("");
 const M=MODES.find(m=>m.id===mode);
 $("#explain").innerHTML=`<div class="explain-inner">
  <div class="flow-header">
   <div class="flow-label">Процесс</div>
   <div class="flow">${M.flow.map((f,idx)=>`<span class="flow-step"><span class="step-num">${idx+1}</span>${f}</span>`).join('<span class="flow-arrow"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"></polyline></svg></span>')}</div>
  </div>
  <p class="explain-text">${M.ex}</p>
 </div>`;
}

function renderHero(){
 const a=D.assemble.filter(r=>relP(r)>0).sort((x,y)=>relP(y)-relP(x)), now=D.assemble.filter(r=>r.now>0);
 const top5=a.slice(0,5).reduce((s,r)=>s+relP(r),0);
 $("#heroSum").innerHTML=`+${top5}<img class="cur-icon hero" src="${PLAT_ICON}" alt="p"><span class="plat-label">чистой платины</span>`;
 $("#heroTxt").innerHTML=`с 5 самых надёжных сборок сетов. Найдено <b>${a.length}</b> выгодных вариантов, из них <b>${now.length}</b> можно моментально сдать покупателям без ожидания.`;
 const pick=[...D.assemble.filter(r=>r.now>0).sort((x,y)=>y.now-x.now).slice(0,1).map(r=>({r,m:"assemble",p:r.now,t:"продать сразу"})),
  ...a.slice(0,1).map(r=>({r,m:"assemble",p:relP(r),t:"сборка сета"})),
  ...D.disassemble.filter(r=>r.profit>0).slice(0,1).map(r=>({r,m:"disassemble",p:r.profit,t:"разбор сета"}))];
 $("#heroBest").innerHTML=`<div class="hero-tag"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"></path><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"></path><path d="M4 22h16"></path><path d="M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22"></path><path d="M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22"></path><path d="M18 2H6v7a6 6 0 0 0 12 0V2z"></path></svg> ТОП ВЫГОДНЫХ СДЕЛОК</div><div class="hero-best-list">`+(pick.map((x,idx)=>
  `<div class="best" data-jump="${x.m}|${x.r.slug}">
    <div class="best-rank">#${idx+1}</div>
    ${img(x.r.slug,"sm").replace("<a ","<div ").replace("</a>","</div>").replace(/href="[^"]*"|target="_blank"|rel="noopener"/g,"")}
    <div style="min-width:0">
     <div class="bn nm">${esc(en(x.r.slug))}</div>
     <div class="ru">${x.t}</div>
    </div>
    <div class="bp">+${x.p}<img class="cur-icon lg plat" src="${PLAT_ICON}" alt="p"></div>
   </div>`).join("")||`<p class="meta" style="margin-top:12px">Пока нет выгодных сделок — обнови данные чуть позже.</p>`) + `</div>`;
}

function renderBar(){
 const M=mode, ph=M==="sell"?"Что у тебя в инвентаре? Например: Rhino, Nikana, Glaive...":"Поиск по названию предмета...";
 let h=`<label class="search"><span class="search-icon"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg></span><input id="q" type="search" placeholder="${ph}" value="${esc(q)}" autocomplete="off"></label>`;
 if(M==="assemble"||M==="disassemble"||M==="flips"){
  h+=`<div class="seg"><span class="t">Прибыль от</span>${[1,5,10,20].map(v=>`<button data-min="${v}" class="${minP===v?"on":""}">${v}<img class="cur-icon plat" src="${PLAT_ICON}" alt="p"></button>`).join("")}</div>`;
  h+=`<div class="seg"><span class="t">Сортировка</span><button data-sort="profit" class="${sortBy==="profit"?"on":""}">прибыль</button>${M==="assemble"?`<button data-sort="real" class="${sortBy==="real"?"on":""}" title="Учитывает реальные цены продаж и частоту ухода лотов">по надёжности</button>`:""}<button data-sort="roi" class="${sortBy==="roi"?"on":""}">окупаемость</button>${M!=="flips"?`<button data-sort="per" class="${sortBy==="per"?"on":""}">за сделку</button>`:""}</div>`;
  if(M!=="flips")h+=`<button data-now="1" class="btn-toggle ${onlyNow?"on":""}"><svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg><span>Только «продать сразу»</span></button>`;
 }
 $("#bar").innerHTML=h;
 const qi=$("#q");qi.oninput=()=>{q=qi.value.trim().toLowerCase();shown=30;renderList()};
}

const match=sl=>!q||en(sl).toLowerCase().includes(q)||ru(sl).toLowerCase().includes(q);

/* ---------- КАРТОЧКИ СДЕЛОК ---------- */
function stepRow(key,i,sl,text,act){
 const d=done[key+"#"+i];
 return `<div class="step ${d?"done":""}" data-step="${key}#${i}"><button class="ck" title="Отметить выполненным"><svg class="ck-svg" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"></polyline></svg></button>${img(sl,"sm")}<div class="st">${text}</div><div class="act">${act||""}</div></div>`}

function dealAssemble(r){
 const key="a:"+r.slug;let i=0;
 const steps=r.parts.map(p=>p.take.map((t,j)=>stepRow(key,i++,p.slug,
   `Купи ${nmL(p.slug)}${t.n>1?` <b>×${t.n}</b>`:""}${p.qty>1&&t.n<p.qty?` <span class="ru" style="display:inline">(${j+1}-й из ${p.qty} нужных)</span>`:""} за <b>${pl(t.o.p)}</b>${t.n>1?" за штуку":""} у ${who(t.o)}`
   +(p.alone&&j===0?` <a class="tag w il" href="${itemUrl(p.slug)}" ${ext} style="margin-left:4px" title="Посмотреть всех продавцов">редкая цена — следующий продаёт за ${Math.round(p.alone/p.qty)}p ↗</a>`:""),
   wbtn("buy",t.o,p.slug,t.o.p,"",t.n))).join("")).join("")
  + (r.now>0
   ? stepRow(key,i++,r.slug,`Продай сет сразу: ${who(r.buyer)} покупает за <b>${pl(r.buyer.p)}</b> → <b style="color:var(--good-bright)">${sign(r.now)}p</b>`,wbtn("sell",r.buyer,r.slug,r.buyer.p,"Предложить"))
   : "")
  + stepRow(key,i++,r.slug,`${r.now>0?"Или выставь":"Выставь"} ${nmL(r.slug)} на продажу за <b>${pl(r.get)}</b> (на 1p дешевле всех) и жди покупателя`,mkt(r.slug));
 return card(r,key,`${pl(r.cost)}`,"части",`${pl(r.now!=null&&r.now+r.cost>r.get?r.buyer.p:r.get)}`,"сет",steps,
  [r.now>0?`<span class="tag g"><svg class="tag-svg" viewBox="0 0 24 24" fill="currentColor"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>продать сразу ${sign(r.now)}p</span>`
   :r.buyer?`<span class="tag w"><svg class="tag-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>покупатели дают до ${r.buyer.p}p</span>`:`<span class="tag w"><svg class="tag-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>нет активных покупателей</span>`,
   r.safe!=null?`<span class="tag w"><svg class="tag-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>без дешёвого лота ${sign(r.safe)}p</span>`:"",
   stTag(r),`<span class="tag">${r.parts.length} ${plural(r.parts.length,"часть","части","частей")}</span>`,`<span class="tag">${r.trades} ${plural(r.trades,"сделка","сделки","сделок")}</span>`,`<span class="tag">окупаемость ${r.roi}%</span>`],
  `Покупать части лучше у тех, кто <span class="dot ingame"></span> в игре. Если кто-то не отвечает — открой предмет на сайте и напиши следующему.`);
}

function stTag(r){const st=r.st;if(!st)return `<span class="tag">нет данных о продажах</span>`;
 const speed=st.day>=10?"быстро":st.day>=3?"нормально":"медленно";
 const cls=st.day>=3?"g":"w";
 const warn=r.real!=null&&r.real<r.profit-3?` · прибыль ${sign(r.real)}p`:"";
 return `<a class="tag ${cls} il" href="${itemUrl(r.slug)}/statistics" ${ext} title="Реальные сделки за последние дни: ${st.lo}–${st.hi}p. Открыть статистику"><svg class="tag-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"></polyline><polyline points="17 6 23 6 23 12"></polyline></svg>~${st.p}p · ~${st.day} шт/день (${speed})${warn} ↗</a>`}
const relP=r=>{let v=r.real!=null?Math.min(r.profit,Math.max(r.real,r.now??-1e9)):r.profit;if(r.st&&r.st.day<1)v=Math.min(v,0);return v};

function dealDis(r){
 const key="d:"+r.slug;let i=0;
 const steps=stepRow(key,i++,r.slug,`Купи ${nmL(r.slug)} за <b>${pl(r.cost)}</b> у ${who(r.seller)}`,wbtn("buy",r.seller,r.slug,r.cost))
  + r.parts.map(p=>{const lp=Math.max(p.sell.p-1,1);
    const fast=p.buy&&p.buy.p>=lp-2?` · или сразу ${who(p.buy)} за <b>${pl(p.buy.p)}</b>`:(p.buy?` <span class="ru" style="display:inline">(сразу купят за ${p.buy.p}p)</span>`:"");
    return stepRow(key,i++,p.slug,`Продай ${nmL(p.slug)}${p.qty>1?` ×${p.qty}`:""} за <b>${pl(lp)}</b>${fast}`,
     (p.buy?wbtn("sell",p.buy,p.slug,p.buy.p,"Предложить"):"")+mkt(p.slug))}).join("");
 return card(r,key,pl(r.cost),"сет",pl(r.get),"части",steps,
  [r.now>0?`<span class="tag g"><svg class="tag-svg" viewBox="0 0 24 24" fill="currentColor"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>продать сразу ${sign(r.now)}p</span>`:`<span class="tag w"><svg class="tag-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>нужно ждать покупателей</span>`,`<span class="tag">${r.trades} ${plural(r.trades,"сделка","сделки","сделок")}</span>`,`<span class="tag">окупаемость ${r.roi}%</span>`],
  "Перед покупкой убедись, что части реально продаются: открой каждую на warframe.market и проверь спрос.");
}

function dealFlip(r){
 const key="f:"+r.slug;let i=0;
 const steps=(r.instant
  ? stepRow(key,i++,r.slug,`Купи у ${who(r.seller)} за <b>${pl(r.buy_at)}</b>`,wbtn("buy",r.seller,r.slug,r.buy_at))
    +stepRow(key,i++,r.slug,`Сразу продай ${who(r.buyer)} за <b>${pl(r.sell_at)}</b>`,wbtn("sell",r.buyer,r.slug,r.sell_at,"Предложить"))
  : stepRow(key,i++,r.slug,`Создай на сайте <b>заказ на покупку</b> за <b>${pl(r.buy_at)}</b> (лучший покупатель даёт ${r.buyer.p}p)`,mkt(r.slug))
    +stepRow(key,i++,r.slug,`Жди — продавцы сами напишут тебе в игре`,"")
    +stepRow(key,i++,r.slug,`Выставь на продажу за <b>${pl(r.sell_at)}</b> (дешевле всех продаёт ${who(r.seller)} за ${r.seller.p}p)`,mkt(r.slug)));
 return card(r,key,pl(r.buy_at),"купить",pl(r.sell_at),"продать",steps,
  [r.instant?`<span class="tag g"><svg class="tag-svg" viewBox="0 0 24 24" fill="currentColor"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>моментальный флип</span>`:`<span class="tag w"><svg class="tag-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>через ордер</span>`,`<span class="tag">${r.depth>=9?"высокая":r.depth>=6?"средняя":"низкая"} ликвидность</span>`,`<span class="tag">окупаемость ${r.roi}%</span>`],
  "Выбирай предметы с высокой ликвидностью и частыми продажами.");
}

function card(r,key,a,al,b,bl,steps,tags,note){
 return `<div class="deal" data-key="${key}"><div class="row">${img(r.slug)}
  <div style="min-width:0">
   <div class="nm"><a class="il" href="${itemUrl(r.slug)}" ${ext} title="Открыть на warframe.market">${esc(en(r.slug))} <span class="ext">↗</span></a></div>
   ${ru(r.slug)?`<div class="ru">${esc(ru(r.slug))}</div>`:""}
   <div class="tags">${tags.join("")}</div>
  </div>
  <div class="money"><div><span class="a">${al}</span><span class="v">${a}</span></div><span class="arr">→</span><div><span class="a">${bl}</span><span class="v">${b}</span></div></div>
  <div class="profit"><div class="v ${r.profit<0?"neg":""}">${sign(r.profit)}<img class="cur-icon lg plat" src="${PLAT_ICON}" alt="p"></div><div class="s">прибыль</div></div>
  <div class="chev">›</div></div>
  <div class="steps"><h4>Пошаговый план</h4>${steps}<div class="note">${note}</div></div></div>`}

function miniDucat(r){
 return `<div class="mini">${img(r.slug)}
  <div style="min-width:0">
   <div class="nm" style="font-size:14px"><a class="il" href="${itemUrl(r.slug)}" ${ext}>${esc(en(r.slug))}</a></div>
   <div class="ru">${pl(r.p)} → ${duc(r.d)}</div>
  </div>
  <div class="k"><div class="v">${r.dpp}</div><div class="s">дук./1p</div></div>
  <div class="foot">
   <div class="foot-seller" title="Продавец: ${esc(r.seller.n)} · ${r.seller.q} шт.">
    ${who(r.seller)}
    <span class="foot-qty">· ${r.seller.q} шт.</span>
   </div>
   ${wbtn("buy",r.seller,r.slug,r.p)}
  </div>
 </div>`;
}

function ratesHtml(){
 const rs=D.rates.filter(r=>[15,25,45,65,100].includes(r.d));
 return `<div class="panel rates"><div class="lbl">Курс хлама: за сколько платины брать 1 штуку</div><div class="rgrid">${rs.map(r=>{
  const n=D.ducats.filter(x=>x.d===r.d&&cheapLots(x).length).length;
  return `<div class="rate"><div class="rd">${r.d}<img class="cur-icon ducat" src="${DUCAT_ICON}" alt="дук."></div>
  <label class="rin">в чате берут по <input type="number" min="0.1" step="0.5" data-rate="${r.d}" value="${rateOf(r.d)}">p</label>
  <div class="rl">≈ <b>${Math.round(r.d/rateOf(r.d)*10)/10}</b> дук./1p</div>
  <div class="rl mute" style="font-size:11.5px">на сайте: от ${r.sell??"—"}p${r.bids?`, скупщики ${r.bid}p`:""}</div>
  <div class="rl" style="margin-top:4px">${n?`<b style="color:var(--good-bright)">${n}</b> лотов по курсу`:`<span class="mute">по курсу нет</span>`}</div></div>`}).join("")}</div>
  <p class="count" style="margin:12px 0 0">Цены из торгового чата («6 штук за 12p» = 2p/шт). Измени, если курс в чате поменялся — лоты пересчитаются. <a href="#" data-reset="1" style="margin-left:6px;color:var(--gold)">Сбросить курс</a></p></div>`}

function bundle(b){
 const msg=`/w ${b.seller.n} Hi! I want to buy: ${b.items.map(i=>`${i.n>1?i.n+"x ":""}"${en(i.slug)}"`).join(", ")} for ${b.p} platinum total. (warframe.market)`;
 return `<div class="bundle-card">
  <div class="bundle-head">
   <div class="bundle-seller-box">
    <div class="bundle-seller">${who(b.seller)}</div>
    <div class="bundle-pills">
     <span class="bundle-pill">${b.cnt} шт.</span>
     <span class="bundle-pill duc">${duc(b.d)}</span>
     <span class="bundle-pill plat">${pl(b.p)}</span>
    </div>
   </div>
   <div class="bundle-rate-badge">
    <div class="val">${b.dpp}</div>
    <div class="lbl">дук./1p</div>
   </div>
  </div>
  <div class="bundle-items-list">
   ${b.items.map(i=>`
    <div class="bundle-item-row">
     <div class="bir-left">
      ${i.n>1?`<span class="bir-qty">${i.n}×</span>`:""}
      <a class="bir-name il" href="${itemUrl(i.slug)}" ${ext}>${esc(en(i.slug))}</a>
     </div>
     <div class="bir-right">
      <span class="mute">${duc(i.d)}</span>
      <span class="mute">·</span>
      <span>${pl(i.p)}</span>
     </div>
    </div>`).join("")}
  </div>
  <button class="btn gold bundle-action-btn" data-copy="${esc(msg)}">
   Купить пачку за ${pl(b.p)} (Ctrl+V)
  </button>
 </div>`;
}

function miniSell(r){
 const isBaro = r.d && !r.set && (r.p <= 2 && r.d >= 45);
 return `<div class="mini">${img(r.slug)}
  <div style="min-width:0">
   <div class="nm" style="font-size:14.5px"><a class="il" href="${itemUrl(r.slug)}" ${ext}>${esc(en(r.slug))}</a></div>
   <div class="ru">${r.set ? `<span class="tag-set">Комплект (сет)</span>` : (r.d ? `У Баро: ${duc(r.d)}` : "")}</div>
  </div>
  <div class="k"><div class="v" style="color:var(--good-bright)">${pl(r.p)}</div><div class="s">покупают</div></div>
  <div class="foot">
   <div class="foot-buyer" title="Покупатель: ${esc(r.buyer.n)}">
    ${who(r.buyer)}
    ${isBaro ? `<span class="verdict baro" title="Дают мало платины — выгоднее сдать Баро Ки'Тииру">Сдай Баро</span>` : ""}
   </div>
   ${wbtn("sell",r.buyer,r.slug,r.p,"Продать")}
  </div>
 </div>`;
}

function renderList(){
 let rows=D[mode].filter(r=>match(r.slug)), html="";
 if(mode==="ducats"){rows=rows.filter(r=>cheapLots(r).length);
  const bs=buildBundles().filter(b=>b.items.some(i=>match(i.slug)));
  $("#count").textContent="";
  html=ratesHtml()
   +`<h3 class="sec">Пачкой у одного продавца <span>${bs.length}</span></h3><p class="count">Лоты не дороже курса чата. До 6 предметов за 1 сделку, сверху — максимум дукатов.</p>`
   +(bs.length?`<div class="grid">${bs.slice(0,shown).map(bundle).join("")}</div>`:`<div class="empty">На сайте сейчас нет пачек дешевле курса чата. Покупай у скупщиков в торговом чате или выстави свой ордер.</div>`)
   +`<h3 class="sec">По одной штуке <span>${rows.length}</span></h3><p class="count">Выгодно брать попутно с другими трейдами.</p>`
   +(rows.length?`<div class="grid">${rows.slice(0,shown).map(miniDucat).join("")}</div>`:"");
 }else if(mode==="sell"){
  $("#count").innerHTML=q?`Найдено позиций: <b>${rows.length}</b>`:`Самые дорогие запросы на покупку. Введи название предмета в поиск выше.`;
  html=rows.length?`<div class="grid">${rows.slice(0,shown).map(miniSell).join("")}</div>`:"";
 }else{
  rows=rows.filter(r=>r.profit>=minP);
  if(onlyNow&&mode!=="flips")rows=rows.filter(r=>r.now>0);
  const k=sortBy==="roi"?(r=>r.roi):sortBy==="per"?(r=>r.profit/(r.trades||1)):sortBy==="real"?relP:(r=>r.profit);
  rows.sort((a,b)=>k(b)-k(a));
  $("#count").innerHTML=`Найдено <b>${rows.length}</b> ${plural(rows.length,"сделка","сделки","сделок")} с прибылью от <b>${minP}</b><img class="cur-icon plat" src="${PLAT_ICON}" alt="p"> · нажми на сделку, чтобы открыть план`;
  const f=mode==="assemble"?dealAssemble:mode==="disassemble"?dealDis:dealFlip;
  html=rows.length?`<div class="list">${rows.slice(0,shown).map(f).join("")}</div>`:"";
 }
 if(!rows.length)html=`<div class="empty">Ничего не найдено. ${q?"Попробуй другое название в поиске.":"Уменьши минимальную прибыль в фильтре или обнови данные позже."}</div>`;
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
 if(t.closest("#helpBtn")||t.closest("#helpClose")){$("#help").classList.toggle("on");return}
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
                .replace("__PLAT_ICON__", PLAT_B64)
                .replace("__DUCAT_ICON__", DUCAT_B64)
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
