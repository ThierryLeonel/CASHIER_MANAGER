"""Company profile (name, contact details, logo) of the partner using this installation."""
import io

from django.conf import settings
from django.utils import timezone

from core.models import CompanyProfile

LOGO_MAX_PX = 600
LOGO_MAX_BYTES = 3 * 1024 * 1024


def company():
    """The profile (without the logo bytes). Created on first use, with the COMPANY_NAME setting if set."""
    prof = CompanyProfile.objects.defer("logo").filter(pk=1).first()
    if prof is None:
        CompanyProfile.objects.get_or_create(pk=1, defaults={"name": getattr(settings, "COMPANY_NAME", ""),
                                                             "address": getattr(settings, "COMPANY_ADDRESS", "")})
        prof = CompanyProfile.objects.defer("logo").get(pk=1)
    return prof


def logo_bytes():
    data = CompanyProfile.objects.filter(pk=1).values_list("logo", flat=True).first()
    return bytes(data) if data else None


class LogoError(ValueError):
    pass


def normalise_logo(uploaded):
    """Validate an uploaded image (PNG, JPEG, WEBP or GIF) and return PNG bytes, at most 600 px wide/high."""
    from PIL import Image, UnidentifiedImageError

    if uploaded.size > LOGO_MAX_BYTES:
        raise LogoError("too_big")
    try:
        img = Image.open(uploaded)
        img.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise LogoError("not_image")
    if img.format not in ("PNG", "JPEG", "WEBP", "GIF"):
        raise LogoError("not_image")
    img = img.convert("RGBA")
    img.thumbnail((LOGO_MAX_PX, LOGO_MAX_PX))
    bbox = img.getbbox()  # trim fully transparent borders
    if bbox:
        img = img.crop(bbox)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def save_logo(prof, data):
    prof.logo = data
    prof.logo_updated = timezone.now() if data else None
