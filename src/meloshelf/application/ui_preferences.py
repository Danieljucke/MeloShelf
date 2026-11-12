"""Interface language and theme. Only fixed lists are accepted, so nothing a client sends can reach a path,
a query or the page as markup; a stored value that stops being valid reads back as unset."""
LANGUAGES = ('en', 'fr', 'es', 'pt', 'ja', 'zh-Hans', 'ko', 'hi')
THEMES = ('light', 'dark', 'system')
LANGUAGE_KEY = 'ui_language'
THEME_KEY = 'ui_theme'


from meloshelf.application import error_codes as ec
def _stored(server, key: str, allowed: tuple[str, ...]):
    value = server.store.get_setting(key, None)
    return value if isinstance(value, str) and value in allowed else None


def get_preferences(server) -> tuple[int, dict]:
    return 200, {'language': _stored(server, LANGUAGE_KEY, LANGUAGES), 'theme': _stored(server, THEME_KEY, THEMES)}


def set_preferences(server, payload: dict) -> tuple[int, dict]:
    if not isinstance(payload, dict):
        return 400, ec.body('preferences_not_object')
    changes = {}
    for field, key, allowed in (('language', LANGUAGE_KEY, LANGUAGES), ('theme', THEME_KEY, THEMES)):
        if field in payload:
            value = payload[field]
            if not isinstance(value, str) or value not in allowed:
                return 400, ec.body('preference_invalid', field=field, allowed=', '.join(allowed))
            changes[key] = value
    if not changes:
        return 400, ec.body('preferences_empty')
    for key, value in changes.items():
        server.store.set_setting(key, value)
    return get_preferences(server)
