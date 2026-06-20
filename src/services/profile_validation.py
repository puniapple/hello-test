"""Проверка готовности профиля для запуска поиска."""

# Минимум полей, которые должны быть заполнены чтобы матчер мог работать
REQUIRED_FIELDS = ["target_roles", "expertise", "current_role_summary"]
MIN_FILLED_FIELDS = 3


def is_profile_ready(profile_data: dict | None) -> tuple[bool, str]:
    """Проверяет, достаточно ли в профиле информации для запуска поиска.

    Возвращает (готов, сообщение_для_юзера).
    """
    if not profile_data:
        return False, (
            "Сначала расскажи о себе через /edit_profile — "
            "без профиля мне не от чего отталкиваться в поиске."
        )

    # Считаем непустые поля
    filled = sum(
        1 for k, v in profile_data.items()
        if v and (not isinstance(v, (list, dict)) or len(v) > 0)
    )

    if filled < MIN_FILLED_FIELDS:
        return False, (
            "Профиль слишком пустой — расскажи хотя бы про опыт, "
            "целевые роли и ожидания. Продолжи диалог в /edit_profile."
        )

    return True, ""