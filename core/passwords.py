"""Password rules. Commercial agents may sign in with a PIN — exactly 4 digits, or 8 digits or more; everyone else
(and an agent who chooses a real password) gets the standard rules: at least 10 characters, not common, not only
digits, not like the username."""
from django.contrib.auth.password_validation import get_password_validators
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

STANDARD = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


def is_pin(password):
    return password.isascii() and password.isdigit() and (len(password) == 4 or len(password) >= 8)


def is_agent(user):
    return getattr(user, "role", None) == "AGENT"


class RoleAwarePasswordValidator:
    def __init__(self):
        self.inner = get_password_validators(STANDARD)

    def validate(self, password, user=None):
        if is_agent(user) and password.isascii() and password.isdigit():
            if not is_pin(password):
                raise ValidationError(_("A PIN must have exactly 4 digits, or 8 digits or more."), code="pin_length")
            sims = {getattr(user, "username", "") or ""}
            cp = getattr(user, "counterparty", None)
            if cp is not None:
                sims.add(cp.business_sim)
            if password in sims or len(set(password)) == 1:
                raise ValidationError(_("Choose a PIN that is not your SIM number and not one digit repeated."),
                                      code="pin_weak")
            return
        errors = []
        for v in self.inner:
            try:
                v.validate(password, user)
            except ValidationError as e:
                errors.extend(e.error_list)
        if errors:
            raise ValidationError(errors)

    def get_help_text(self):
        return " ".join([v.get_help_text() for v in self.inner]
                        + [_("Commercial agents may use a PIN instead: exactly 4 digits, or 8 digits or more.")])
