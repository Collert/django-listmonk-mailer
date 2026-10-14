SECRET_KEY = "tests"
USE_TZ = True
INSTALLED_APPS = []
DATABASES = {}
DEFAULT_FROM_EMAIL = "Site <noreply@example.com>"

# The test runner replaces MAILERS with locmem backends; tests set their own
# MAILERS through the pytest-django ``settings`` fixture.
MAILERS = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
