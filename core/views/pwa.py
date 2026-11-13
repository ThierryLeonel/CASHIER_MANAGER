"""Installable web app (PWA): the manifest, the app icons, the service worker and the offline page, so the site can be
installed on Android phones ("Add to home screen" / "Install app") and still used as a normal link in any browser."""
import io
import json

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import cache_control

ORANGE = (239, 125, 18)
THEME = "#1c1c1c"
SW_VERSION = "cm-v1"


def _icon_png(size, maskable=False):
    from PIL import Image, ImageDraw, ImageFont

    from core.services import branding

    logo = branding.logo_bytes()
    img = Image.new("RGBA", (size, size), ORANGE + (255,) if (maskable or not logo) else (255, 255, 255, 255))
    if logo:
        mark = Image.open(io.BytesIO(logo)).convert("RGBA")
        box = int(size * (0.62 if maskable else 0.8))  # maskable icons keep a safe zone around the mark
        mark.thumbnail((box, box))
        if maskable:
            pad = Image.new("RGBA", (int(size * 0.72),) * 2, (255, 255, 255, 255))
            ImageDraw.Draw(pad)
            img.alpha_composite(pad, ((size - pad.width) // 2, (size - pad.height) // 2))
        img.alpha_composite(mark, ((size - mark.width) // 2, (size - mark.height) // 2))
    else:
        if not maskable:  # rounded square on a transparent background
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            ImageDraw.Draw(img).rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22),
                                                  fill=ORANGE + (255,))
        letter = branding.company().initial or "C"
        fsize = int(size * (0.42 if maskable else 0.56))
        font = None
        for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
            try:
                font = ImageFont.truetype(path, fsize)
                break
            except OSError:
                continue
        if font is None:
            try:
                font = ImageFont.load_default(size=fsize)
            except TypeError:
                font = ImageFont.load_default()
        d = ImageDraw.Draw(img)
        d.text((size / 2, size / 2), letter, font=font, fill=(255, 255, 255, 255), anchor="mm")
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


@cache_control(public=True, max_age=86400)
def app_icon(request, size, purpose="any"):
    size = max(48, min(int(size), 1024))
    return HttpResponse(_icon_png(size, maskable=(purpose == "maskable")), content_type="image/png")


@cache_control(public=True, max_age=3600)
def manifest(request):
    from core.services import branding

    co = branding.company()
    name = str(co.display_name or "Cashier Manager")
    v = str(int(co.logo_updated.timestamp())) if getattr(co, "logo_updated", None) else "0"
    icons = [{"src": f"/app-icon/{s}.png?v={v}", "sizes": f"{s}x{s}", "type": "image/png", "purpose": "any"}
             for s in (192, 512)]
    icons += [{"src": f"/app-icon/{s}/maskable.png?v={v}", "sizes": f"{s}x{s}", "type": "image/png",
               "purpose": "maskable"} for s in (192, 512)]
    data = {
        "id": "/", "name": name, "short_name": name if len(name) <= 15 else name.split()[0][:15], "description": "Cash, supplies, agents, POS and shops",
        "start_url": "/?source=app", "scope": "/", "display": "standalone", "orientation": "portrait",
        "background_color": "#f6f4f0", "theme_color": THEME, "lang": "fr", "icons": icons,
        "shortcuts": [
            {"name": "OOS", "url": "/agent/oos/", "icons": [{"src": f"/app-icon/96.png?v={v}", "sizes": "96x96"}]},
            {"name": "Supply / Approvisionner", "url": "/agent/supply/",
             "icons": [{"src": f"/app-icon/96.png?v={v}", "sizes": "96x96"}]},
        ],
    }
    return HttpResponse(json.dumps(data), content_type="application/manifest+json")


SW = r"""/* Cashier Manager service worker: makes the site installable and shows a friendly page when offline.
   Pages are always fetched from the server (money data is never served from a stale cache); only the static
   files (styles, scripts, icons) are cached. */
const VERSION = '%(v)s';
const STATIC = 'static-' + VERSION;
const OFFLINE = '/offline/';
self.addEventListener('install', e => {
  e.waitUntil(caches.open(STATIC).then(c => c.addAll([OFFLINE, '%(css)s'])).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== STATIC).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
    return;
  }
  if (url.pathname.startsWith('/static/') || url.pathname.startsWith('/app-icon/')) {
    e.respondWith(caches.open(STATIC).then(c => c.match(req).then(hit => {
      const net = fetch(req).then(r => { if (r.ok) c.put(req, r.clone()); return r; }).catch(() => hit);
      return hit || net;
    })));
  }
});
"""


def service_worker(request):
    from django.templatetags.static import static

    resp = HttpResponse(SW % {"v": SW_VERSION, "css": static("core/app.css")}, content_type="application/javascript")
    resp["Service-Worker-Allowed"] = "/"
    resp["Cache-Control"] = "no-cache"
    return resp


def offline(request):
    return render(request, "core/offline.html")
