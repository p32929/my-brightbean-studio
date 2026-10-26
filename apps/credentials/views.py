"""Settings → Platform keys: edit the instance's platform app credentials.

Values are written to the env file named by ``PLATFORM_ENV_FILE``. Settings
re-read that file with overwrite on import, so after a save the gunicorn
master is sent SIGHUP (fresh workers re-import settings) and the studio worker
notices the file change and restarts itself (see run_studio_worker).
"""

from __future__ import annotations

import logging
import os
import signal
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.social_accounts.oauth_aliases import to_url_slug

from .envfile import read_env_file, update_env_file

logger = logging.getLogger(__name__)

# (env var, label, is_secret, required)
PLATFORM_KEY_GROUPS = [
    {
        "title": "Facebook & Instagram (via Facebook Page)",
        "callbacks": ["facebook", "instagram"],
        "extra_urls": [("Webhook callback URL", "/webhooks/facebook/")],
        "help": "Meta app → App settings → Basic. Add both callback URLs under Facebook Login → Settings → Valid OAuth Redirect URIs.",
        "fields": [
            ("PLATFORM_FACEBOOK_APP_ID", "App ID", False, True),
            ("PLATFORM_FACEBOOK_APP_SECRET", "App Secret", True, True),
            ("PLATFORM_FACEBOOK_CONFIG_ID", "Login for Business config ID (optional)", False, False),
            ("FACEBOOK_WEBHOOK_VERIFY_TOKEN", "Webhook verify token (paste the same value in Meta → Webhooks)", False, False),
        ],
    },
    {
        "title": "Instagram (direct Instagram login)",
        "callbacks": ["instagram_login"],
        "help": "Only needed to connect Instagram professional accounts without a Facebook Page.",
        "fields": [
            ("PLATFORM_INSTAGRAM_APP_ID", "Instagram App ID", False, True),
            ("PLATFORM_INSTAGRAM_APP_SECRET", "Instagram App Secret", True, True),
        ],
    },
    {
        "title": "Threads",
        "callbacks": ["threads"],
        "help": "Meta app → Use cases → Access the Threads API → Settings. These are the Threads App ID / Secret, not the Facebook ones.",
        "fields": [
            ("PLATFORM_THREADS_APP_ID", "Threads App ID", False, True),
            ("PLATFORM_THREADS_APP_SECRET", "Threads App Secret", True, True),
        ],
    },
    {
        "title": "LinkedIn (personal profile)",
        "callbacks": ["linkedin_personal"],
        "help": "LinkedIn developer app with “Sign In with LinkedIn using OpenID Connect” and “Share on LinkedIn”.",
        "fields": [
            ("PLATFORM_LINKEDIN_PERSONAL_CLIENT_ID", "Client ID", False, True),
            ("PLATFORM_LINKEDIN_PERSONAL_CLIENT_SECRET", "Client Secret", True, True),
        ],
    },
    {
        "title": "LinkedIn (company page)",
        "callbacks": ["linkedin_company"],
        "help": "Requires LinkedIn's Community Management API product on the app.",
        "fields": [
            ("PLATFORM_LINKEDIN_COMPANY_CLIENT_ID", "Client ID", False, True),
            ("PLATFORM_LINKEDIN_COMPANY_CLIENT_SECRET", "Client Secret", True, True),
        ],
    },
    {
        "title": "YouTube & Google Business Profile",
        "callbacks": ["youtube", "google_business"],
        "help": "Google Cloud Console → APIs & Services → Credentials → OAuth client ID (Web application).",
        "fields": [
            ("PLATFORM_GOOGLE_CLIENT_ID", "Client ID", False, True),
            ("PLATFORM_GOOGLE_CLIENT_SECRET", "Client Secret", True, True),
        ],
    },
    {
        "title": "TikTok",
        "callbacks": ["tiktok"],
        "help": "TikTok for Developers app with Login Kit and Content Posting API. Note: Client Key, not Client ID.",
        "fields": [
            ("PLATFORM_TIKTOK_CLIENT_KEY", "Client Key", False, True),
            ("PLATFORM_TIKTOK_CLIENT_SECRET", "Client Secret", True, True),
        ],
    },
    {
        "title": "Pinterest",
        "callbacks": ["pinterest"],
        "help": "Pinterest developer app → App ID and App secret key.",
        "fields": [
            ("PLATFORM_PINTEREST_APP_ID", "App ID", False, True),
            ("PLATFORM_PINTEREST_APP_SECRET", "App Secret", True, True),
        ],
    },
    {
        "title": "Sign in with Google (optional)",
        "extra_urls": [("Authorized redirect URI", "/accounts/google/login/callback/")],
        "help": "Lets people log in to BrightBean with Google. Unrelated to posting.",
        "fields": [
            ("GOOGLE_AUTH_CLIENT_ID", "Client ID", False, True),
            ("GOOGLE_AUTH_CLIENT_SECRET", "Client Secret", True, True),
        ],
    },
]


def _reload_web_workers() -> bool:
    """SIGHUP the gunicorn master so new workers re-read the env file."""
    ppid = os.getppid()
    try:
        cmdline = Path(f"/proc/{ppid}/cmdline").read_bytes()
    except OSError:
        return False
    if b"gunicorn" not in cmdline:
        return False
    os.kill(ppid, signal.SIGHUP)
    return True


def _build_groups(values: dict[str, str]) -> list[dict]:
    app_url = settings.APP_URL.rstrip("/")
    groups = []
    for group in PLATFORM_KEY_GROUPS:
        fields = [
            {
                "key": key,
                "label": label,
                "secret": secret,
                "required": required,
                "is_set": bool(values.get(key)),
                "value": "" if secret else values.get(key, ""),
            }
            for key, label, secret, required in group["fields"]
        ]
        urls = [
            ("Callback URL", f"{app_url}/social-accounts/callback/{to_url_slug(platform)}/")
            for platform in group.get("callbacks", [])
        ]
        urls += [(label, f"{app_url}{path}") for label, path in group.get("extra_urls", [])]
        groups.append(
            {
                "title": group["title"],
                "help": group["help"],
                "fields": fields,
                "urls": urls,
                "configured": all(f["is_set"] for f in fields if f["required"]),
            }
        )
    return groups


@login_required
@require_http_methods(["GET", "POST"])
def platform_keys(request):
    # 403, not a login redirect: a signed-in non-superuser bounced to the login
    # page is sent straight back here by allauth, looping forever.
    if not request.user.is_superuser:
        raise PermissionDenied("Only instance administrators can edit platform keys.")
    env_path = settings.PLATFORM_ENV_FILE
    writable = bool(env_path) and os.access(env_path, os.W_OK)

    if request.method == "POST" and writable:
        current = read_env_file(env_path)
        updates = {}
        for key, _label, secret, _required in (f for g in PLATFORM_KEY_GROUPS for f in g["fields"]):
            submitted = request.POST.get(key, "").strip()
            if secret:
                if request.POST.get(f"clear_{key}"):
                    submitted = ""
                elif not submitted:
                    continue  # blank secret box = keep the saved value
            if submitted != current.get(key, ""):
                updates[key] = submitted
        if updates:
            try:
                update_env_file(env_path, updates)
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect("platform_keys:index")
            logger.info("Platform keys updated: %s", ", ".join(sorted(updates)))
            if _reload_web_workers():
                messages.success(request, "Saved. BrightBean is reloading — the new keys apply within a few seconds.")
            else:
                messages.success(request, "Saved to the env file. Restart BrightBean to apply the new keys.")
        else:
            messages.info(request, "Nothing changed.")
        return redirect("platform_keys:index")

    values = read_env_file(env_path) if env_path else {}
    return render(
        request,
        "credentials/platform_keys.html",
        {
            "settings_active": "platform_keys",
            "groups": _build_groups(values),
            "env_path": env_path,
            "writable": writable,
        },
    )
