THEME_COOKIE_NAME = "service2_theme"
THEME_COOKIE_MAX_AGE = 365 * 24 * 60 * 60
THEME_VALUES = frozenset({"auto", "light", "dark"})


def get_theme_preference(request):
    value = (request.COOKIES.get(THEME_COOKIE_NAME) or "").strip().lower()
    return value if value in THEME_VALUES else "auto"


def set_theme_cookie(response, preference, *, request):
    if preference not in THEME_VALUES:
        raise ValueError("Unsupported theme preference")

    response.set_cookie(
        THEME_COOKIE_NAME,
        preference,
        max_age=THEME_COOKIE_MAX_AGE,
        secure=request.is_secure(),
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return response
